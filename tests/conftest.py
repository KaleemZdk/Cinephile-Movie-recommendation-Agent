"""Shared fixtures. Every test gets its own empty database and an empty TMDB cache."""

import pytest

from cinephile import tmdb
from cinephile.storage import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    tmdb.clear_cache()
