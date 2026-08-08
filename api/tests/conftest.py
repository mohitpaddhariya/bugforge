"""Shared fixtures. Tests run inside the api container against the live db."""
import os

import httpx
import pytest

API = os.environ.get("BUGFORGE_API_URL", "http://localhost:8000")
DEBUG = f"{API}/api/debug"


@pytest.fixture
def flags():
    """Set flags for a test and always put them back."""
    touched: dict[str, bool] = {}

    def _set(key: str, enabled: bool) -> None:
        if key not in touched:
            cur = httpx.get(f"{DEBUG}/flags", timeout=10).json()
            items = cur.get("flags", cur)
            touched[key] = bool(items.get(key) if isinstance(items, dict) else False)
        httpx.post(f"{DEBUG}/flags", json={"key": key, "enabled": enabled}, timeout=10)

    yield _set
    for key, was in touched.items():
        httpx.post(f"{DEBUG}/flags", json={"key": key, "enabled": was}, timeout=10)


@pytest.fixture
def client():
    with httpx.Client(base_url=API, timeout=20) as c:
        yield c


@pytest.fixture
def login(client):
    def _login(email: str, password: str = "password123") -> httpx.Client:
        c = httpx.Client(base_url=API, timeout=20)
        r = c.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return c

    return _login
