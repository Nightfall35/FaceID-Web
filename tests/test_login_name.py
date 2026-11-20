"""/api/login returns a display name for the login loader (FACEID_OPERATOR_NAME, default ALTAIR)."""
import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def _make(name=None):
        monkeypatch.setenv("FACEID_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("FACEID_PASSWORD", "testpw")
        if name is None:
            monkeypatch.delenv("FACEID_OPERATOR_NAME", raising=False)
        else:
            monkeypatch.setenv("FACEID_OPERATOR_NAME", name)
        import app.main as m
        importlib.reload(m)
        return TestClient(m.app)
    yield _make
    os.environ.pop("FACEID_OPERATOR_NAME", None)


def test_login_returns_default_name(make_client):
    c = make_client()
    r = c.post("/api/login", data={"password": "testpw"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "name": "ALTAIR"}


def test_login_returns_configured_name(make_client):
    c = make_client("  Altair Ibn-La'Ahad\n")
    assert c.post("/api/login", data={"password": "testpw"}).json()["name"] == "Altair Ibn-La'Ahad"


def test_failed_login_has_no_name(make_client):
    c = make_client("Altair")
    r = c.post("/api/login", data={"password": "nope"})
    assert r.status_code == 401
    assert "Altair" not in r.text
