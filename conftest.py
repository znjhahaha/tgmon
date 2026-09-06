"""Windows test isolation.

The default pytest temp root can inherit a deny ACL in managed workspaces;
keep function-scoped temporary files inside the repository instead.
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path() -> Path:
    root = Path(__file__).parent / ".pytest-tmp"
    root.mkdir(exist_ok=True)
    path = root / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
