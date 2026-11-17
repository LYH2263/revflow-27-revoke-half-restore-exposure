from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("FIXTURES_DIR", str(ROOT / "fixtures"))
os.environ.setdefault("DATA_DIR", str(ROOT / "backend" / "data_test"))


@pytest.fixture(scope="module", autouse=True)
def _isolated_db(request):
    """每个测试模块一个独立 SQLite 库，避免模块间数据串扰。"""
    from app import db as dbmod

    data_dir = ROOT / "backend" / "data_test" / request.module.__name__
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    old = (dbmod.DATA_DIR, dbmod.DB_PATH)
    dbmod.DATA_DIR = data_dir
    dbmod.DB_PATH = data_dir / "app.db"
    yield
    dbmod.DATA_DIR, dbmod.DB_PATH = old
