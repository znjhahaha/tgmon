"""General theme and plugin extension contracts."""
from __future__ import annotations

import json
import sys
import time

from tgmon.plugins import PluginManager
from tgmon.themes import get_theme, load_theme_file


def test_builtin_themes_are_not_game_bound():
    assert get_theme("generic").key == "generic"
    assert "prompt" in get_theme("generic").config


def test_theme_file_loads_structured_config(tmp_path):
    path = tmp_path / "theme.json"
    path.write_text(json.dumps({"key": "sports", "label": "体育",
                                "config": {"prompt": "赛事实体", "aliases": ["NBA"]}}),
                    encoding="utf-8")
    theme = load_theme_file(path)
    assert theme.key == "sports"
    assert theme.config["aliases"] == ["NBA"]


def test_plugin_manager_runs_isolated_json_plugin(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.json").write_text(json.dumps({
        "name": "echo", "version": "1.0", "entrypoint": "plugin.py",
        "capabilities": ["event"],
    }), encoding="utf-8")
    (source / "plugin.py").write_text(
        "import sys\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write(line); sys.stdout.flush()\n",
        encoding="utf-8")
    manager = PluginManager(tmp_path / "installed")
    manifest = manager.install(source)
    assert manifest.name == "echo"
    assert manager.start("echo") is True
    try:
        payload = {"type": "message", "content": "hello"}
        manager.emit(payload)
        deadline = time.time() + 2
        received = []
        while time.time() < deadline and not received:
            received = manager.read("echo")
            if not received:
                time.sleep(0.02)
        assert received and received[0] == payload
    finally:
        manager.stop("echo")
    assert manager.status("echo")["status"] == "stopped"
