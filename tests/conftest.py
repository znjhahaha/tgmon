"""测试环境隔离：tgmon.db 的 engine 在首次 import 时绑定 TGMON_DB。

conftest 先于所有测试模块加载，这里把 DB 指到临时文件，
避免测试建表/写数据污染本地开发库 db/tgmon.db。
（test_e2e_route_check 不碰 DB 表，不受影响。）
"""
from __future__ import annotations

import os
import tempfile
import uuid

os.environ["TGMON_DB"] = os.path.join(
    tempfile.gettempdir(), f"tgmon_test_{os.getpid()}_{uuid.uuid4().hex}.db")
