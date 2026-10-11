import pytest

from mcp_server import database, tmdb


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Every test gets its own empty database and an empty TMDB cache."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.init_db()
    tmdb.clear_cache()
    yield
