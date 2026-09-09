"""Configurable content themes shared by ingestion, translation and output.

Themes describe domain vocabulary and prompt hints; they do not add a second
business pipeline. The built-in gaming theme preserves today's behaviour while
``generic`` gives new sources a useful default.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ThemePackage:
    key: str
    label: str
    config: dict[str, Any] = field(default_factory=dict)
    version: str = "1"


BUILTIN_THEMES: dict[str, ThemePackage] = {
    "generic": ThemePackage(
        key="generic", label="通用资讯",
        config={"prompt": "准确翻译并保留来源、数字、链接和专有名词。",
                "aliases": [], "topics": []}),
    "gaming": ThemePackage(
        key="gaming", label="游戏资讯",
        config={"prompt": "使用游戏术语表，保留版本号、角色名、数值和技能名。",
                "aliases": ["原神", "崩坏：星穹铁道", "绝区零"],
                "topics": ["角色", "版本", "卡池", "活动"]}),
}


def get_theme(key: str | None) -> ThemePackage:
    """Resolve a theme key with a stable generic fallback."""
    name = (key or "generic").strip().lower()
    from .paths import EXTENSIONS_DIR
    if name and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in name):
        path = EXTENSIONS_DIR / "themes" / f"{name}.json"
        if path.is_file():
            return load_theme_file(path)
    return BUILTIN_THEMES.get(name, BUILTIN_THEMES["generic"])


def load_theme_file(path: str | Path) -> ThemePackage:
    """Load and validate one local JSON theme package."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("theme package must be an object")
    key = str(data.get("key") or "").strip().lower()
    if not key or len(key) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in key):
        raise ValueError("theme key is invalid")
    config = data.get("config") or {}
    if not isinstance(config, dict):
        raise ValueError("theme config must be an object")
    if not isinstance(config.get("terms", {}), dict):
        raise ValueError("theme terms must be a mapping")
    if any(not isinstance(config.get(key, []), list) for key in ("aliases", "topics")):
        raise ValueError("theme aliases and topics must be lists")
    return ThemePackage(key=key, label=str(data.get("label") or key)[:120],
                        config=config, version=str(data.get("version") or "1"))


def load_themes(directory: str | Path) -> list[ThemePackage]:
    """Load local ``*.json`` packages; malformed files are skipped by caller."""
    root = Path(directory)
    out = list(BUILTIN_THEMES.values())
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        try:
            theme = load_theme_file(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        out = [x for x in out if x.key != theme.key]
        out.append(theme)
    return out
