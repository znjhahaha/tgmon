"""知识库：术语实体、别名、导入器、实体标注。

设计要点见 tgmon/kb/importer.py 的模块 docstring。核心是「一个实体多个 surface
form」—— 卡芙卡有 卡妈/妈妈/Kafka 三个写法但只有一个规范中文名和一套属性。
"""
from __future__ import annotations

from .annotate import annotate, entities_context
from .importer import import_all_games, import_genshin, import_hsr, import_zzz
from .wiki import MediaWikiAdapter, normalize_api_url, parse_infobox, parse_page

__all__ = [
    "annotate", "entities_context",
    "import_all_games", "import_genshin", "import_hsr", "import_zzz",
    "MediaWikiAdapter", "parse_infobox", "parse_page", "normalize_api_url",
]
