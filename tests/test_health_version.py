"""
Regression test for the version-string drift fixed in backend/main.py:
the FastAPI app version ("2.0.0") and /api/health's own reported version
("2.0.0", a separate hardcoded literal) had silently drifted from the
README's documented current version (v2.1) since neither was ever bumped
together. /api/health now reads app.version directly instead of carrying
its own copy, so this can't recur -- this test locks that in.
"""
import asyncio

from backend.main import app, health


def test_health_reports_the_same_version_as_the_app(monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "_db_pool", None)

    result = asyncio.run(health())

    assert result["version"] == app.version
