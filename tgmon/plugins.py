"""Small, process-isolated plugin host for tgmon.

Plugins use a line-delimited JSON protocol on stdin/stdout. A plugin can
observe events and expose its own tools without importing tgmon internals into
the worker process. The host never starts plugins automatically; deployment
can opt in per plugin after inspecting its manifest.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import queue
import threading
import uuid
import tempfile
from concurrent.futures import Future, TimeoutError
from filelock import FileLock
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    entrypoint: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    commands: tuple[str, ...] = field(default_factory=tuple)
    tools: tuple[dict, ...] = field(default_factory=tuple)

    @classmethod
    def load(cls, path: str | Path) -> "PluginManifest":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("plugin manifest must be an object")
        name = str(data.get("name") or "").strip()
        if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in name.lower()):
            raise ValueError("plugin name is invalid")
        entrypoint = str(data.get("entrypoint") or "").strip()
        if not entrypoint or Path(entrypoint).is_absolute() or ".." in Path(entrypoint).parts:
            raise ValueError("plugin entrypoint must be relative")
        capabilities = tuple(str(x) for x in (data.get("capabilities") or []) if str(x))
        tools = data.get("tools") or []
        if not isinstance(tools, list) or any(not isinstance(t, dict) or not t.get("name") for t in tools):
            raise ValueError("plugin tools must have names and JSON schemas")
        return cls(name=name, version=str(data.get("version") or "1"),
                   entrypoint=entrypoint, capabilities=capabilities,
                   commands=tuple(data.get("commands") or []), tools=tuple(tools))


class PluginManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifests: dict[str, PluginManifest] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._errors: dict[str, str] = {}
        self._outputs: dict[str, queue.Queue] = {}
        self._inputs: dict[str, queue.Queue] = {}
        self._pending: dict[tuple[str, str], Future] = {}
        self._lock = threading.RLock()
        self._config_versions: dict[str, str] = {}
        self._generations: dict[str, str] = {}
        self.discover()

    def discover(self) -> list[PluginManifest]:
        manifests = {}
        for manifest_path in sorted(self.root.glob("*/plugin.json")):
            try:
                manifest = PluginManifest.load(manifest_path)
                if manifest.name != manifest_path.parent.name:
                    raise ValueError("plugin directory must match manifest name")
                manifests[manifest.name] = manifest
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._errors[manifest_path.parent.name] = str(exc)[:500]
        self._manifests = manifests
        return list(manifests.values())

    def install(self, source: str | Path) -> PluginManifest:
        source = Path(source).resolve()
        manifest = PluginManifest.load(source / "plugin.json")
        destination = (self.root / manifest.name).resolve()
        if self.root.resolve() not in destination.parents:
            raise ValueError("plugin destination escapes plugin root")
        if source == destination or source in destination.parents or destination in source.parents:
            raise ValueError("plugin source overlaps the installation directory")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise ValueError("plugin package cannot contain symlinks")
        with FileLock(str(self.root / "install.lock")):
            stage = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
            backup = self.root / f".previous-{manifest.name}-{uuid.uuid4().hex}"
            try:
                shutil.copytree(source, stage, dirs_exist_ok=True)
                if not (stage / manifest.entrypoint).is_file():
                    raise ValueError("plugin entrypoint is missing")
                self.stop(manifest.name)
                if destination.exists():
                    destination.rename(backup)
                try:
                    stage.rename(destination)
                except OSError:
                    if backup.exists():
                        backup.rename(destination)
                    raise
                if backup.exists():
                    shutil.rmtree(backup)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        self._manifests[manifest.name] = manifest
        self._errors.pop(manifest.name, None)
        return manifest

    def start(self, name: str) -> bool:
        with self._lock:
            return self._start(name)

    def _start(self, name: str) -> bool:
        if name not in self._manifests:
            self.discover()
        manifest = self._manifests.get(name)
        if manifest is None:
            raise KeyError(name)
        if self.is_running(name):
            return True
        directory = self.root / manifest.name
        entrypoint = (directory / manifest.entrypoint).resolve()
        if directory.resolve() not in entrypoint.parents or not entrypoint.is_file():
            raise ValueError("plugin entrypoint is missing")
        env = {k: v for k, v in os.environ.items() if k.startswith("TGMON_PLUGIN_") or
               k.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG")}
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", str(entrypoint)], cwd=str(directory),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self._processes[name] = proc
            output = queue.Queue(maxsize=256)
            self._outputs[name] = output
            inputs = queue.Queue(maxsize=256)
            self._inputs[name] = inputs
            self._errors.pop(name, None)

            def _pump() -> None:
                if proc.stdout is None:
                    return
                while True:
                    line = proc.stdout.readline(262145)
                    if not line:
                        break
                    if len(line) > 262144:
                        self._errors[name] = "plugin output exceeds limit"
                        proc.terminate()
                        break
                    try:
                        value = json.loads(line)
                        if isinstance(value, dict):
                            future = self._pending.get((name, str(value.get("id", ""))))
                            if future is not None and not future.done():
                                if value.get("error"):
                                    future.set_exception(RuntimeError(str(value["error"])))
                                else:
                                    future.set_result(value.get("result"))
                            else:
                                output.put_nowait(value)
                    except json.JSONDecodeError:
                        continue
                    except queue.Full:
                        self._errors[name] = "plugin output queue is full"

            def _write() -> None:
                while proc.poll() is None:
                    try:
                        line = inputs.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    try:
                        proc.stdin.write(line)
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        break

            def _stderr() -> None:
                if proc.stderr:
                    for line in proc.stderr:
                        self._errors[name] = line.strip()[:500]

            threading.Thread(target=_pump, name=f"tgmon-plugin-{name}",
                             daemon=True).start()
            threading.Thread(target=_write, name=f"tgmon-plugin-input-{name}", daemon=True).start()
            threading.Thread(target=_stderr, name=f"tgmon-plugin-error-{name}", daemon=True).start()
            return True
        except OSError as exc:
            self._errors[name] = str(exc)[:500]
            return False

    def is_running(self, name: str) -> bool:
        proc = self._processes.get(name)
        return proc is not None and proc.poll() is None

    def stop(self, name: str) -> bool:
        proc = self._processes.pop(name, None)
        if proc is None:
            return False
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        self._outputs.pop(name, None)
        self._inputs.pop(name, None)
        self._config_versions.pop(name, None)
        for (plugin, _), pending in list(self._pending.items()):
            if plugin == name and not pending.done():
                pending.set_exception(RuntimeError("Plugin stopped"))
        return True

    def emit(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        for name, proc in list(self._processes.items()):
            if proc.poll() is not None:
                self._errors[name] = f"plugin exited with code {proc.returncode}"
                continue
            try:
                self._inputs[name].put_nowait(line)
            except (queue.Full, KeyError) as exc:
                self._errors[name] = str(exc)[:500]

    def request(self, name: str, method: str, params: dict, timeout: float = 5):
        if not self.is_running(name):
            raise RuntimeError("Plugin is not enabled")
        request_id, future = uuid.uuid4().hex, Future()
        self._pending[name, request_id] = future
        try:
            line = json.dumps({"id": request_id, "method": method, "params": params},
                              ensure_ascii=False) + "\n"
            if len(line.encode()) > 262144:
                raise ValueError("Plugin request exceeds limit")
            self._inputs[name].put_nowait(line)
            return future.result(timeout=timeout)
        except TimeoutError:
            self.stop(name)
            self._errors[name] = "Plugin request timed out"
            raise
        finally:
            self._pending.pop((name, request_id), None)

    def configuration(self) -> dict:
        path = self.root / "config.json"
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def configure(self, name: str, *, enabled: bool, config: dict | None = None) -> None:
        with FileLock(str(self.root / "config.lock")):
            state = self.configuration()
            state[name] = {"enabled": enabled, "generation": uuid.uuid4().hex,
                           "config": config if config is not None
                           else state.get(name, {}).get("config", {})}
            temp = self.root / f"config-{uuid.uuid4().hex}.tmp"
            temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.root / "config.json")
        self.sync()

    def sync(self) -> None:
        self.discover()
        state = self.configuration()
        for name in list(self._manifests):
            generation = state.get(name, {}).get("generation", "")
            if generation != self._generations.get(name):
                self.stop(name)
                self._errors.pop(name, None)
                self._generations[name] = generation
            if state.get(name, {}).get("enabled"):
                if not self.is_running(name) and name not in self._errors:
                    self.start(name)
                if self.is_running(name):
                    config = state.get(name, {}).get("config", {})
                    revision = json.dumps(config, sort_keys=True, ensure_ascii=False)
                    if self._config_versions.get(name) != revision:
                        self._inputs[name].put_nowait(json.dumps({"method": "configure", "params": config}) + "\n")
                        self._config_versions[name] = revision
            elif self.is_running(name):
                self.stop(name)

    def close(self) -> None:
        for name in list(self._processes):
            self.stop(name)

    def read(self, name: str, limit: int = 50) -> list[dict[str, Any]]:
        """Read already-buffered plugin output without blocking the worker."""
        if name not in self._processes:
            return []
        out = []
        output = self._outputs.get(name)
        if output is None:
            return out
        for _ in range(max(0, limit)):
            try:
                value = output.get_nowait()
            except queue.Empty:
                break
            out.append(value)
        return out

    def status(self, name: str) -> dict[str, Any]:
        manifest = self._manifests.get(name)
        proc = self._processes.get(name)
        if proc and proc.poll() is not None and name not in self._errors:
            self._errors[name] = f"plugin exited with code {proc.returncode}"
        return {"name": name, "version": manifest.version if manifest else "",
                "capabilities": list(manifest.capabilities) if manifest else [],
                "status": "running" if self.is_running(name)
                else "error" if name in self._errors else "stopped",
                "pid": proc.pid if proc and proc.poll() is None else None,
                "error": self._errors.get(name, "")}
