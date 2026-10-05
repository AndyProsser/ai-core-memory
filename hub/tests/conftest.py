import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from acm_hub.access import Principal, principal_for_user
from acm_hub.app import create_app
from acm_hub.auth import issue_setup_code
from acm_hub.config import Settings
from acm_hub.db import make_engine, migrate
from acm_hub.models import InstanceSettings, User


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_DB_PATH", str(tmp_path / "data" / "hub.sqlite3"))
    monkeypatch.setenv("MEMORY_HUB_SECRET_KEY", "test-secret")
    for k in ("MEMORY_HUB_OIDC_ISSUER", "MEMORY_HUB_OIDC_CLIENT_ID", "MEMORY_HUB_OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    return Settings()


@pytest.fixture()
def engine(settings):
    eng = make_engine(settings)
    migrate(eng)
    with Session(eng) as s:
        s.add(InstanceSettings(id=1))
        s.commit()
    return eng


@pytest.fixture()
def db(engine):
    with Session(engine) as s:
        yield s


def make_user(db: Session, email="andy@example.com", admin=True) -> User:
    u = User(email=email, is_admin=admin)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def user(db):
    return make_user(db)


@pytest.fixture()
def human(db, user) -> Principal:
    return principal_for_user(db, user, kind="session")


def token_principal(user, **kw) -> Principal:
    return Principal(user_id=user.id, email=user.email, kind="token", token_id=None, **kw)


# --- shared web fixtures (used by test_web.py, test_plugins_ui.py, ...) ---------------------------------

PASSWORD = "correct horse battery staple"


@pytest.fixture()
def hub(settings):
    root = create_app(settings)
    with TestClient(root) as client:
        yield client, root.fastapi


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" content="([^"]+)"', html)
    assert m, "no csrf meta on page"
    return m.group(1)


def setup_admin(client, app, email="admin@example.com"):
    with Session(app.state.engine) as s:
        code = issue_setup_code(s)
    r = client.post(
        "/setup",
        data={
            "setup_code": code,
            "email": email,
            "password": PASSWORD,
            "confirm": PASSWORD,
            "deployment_mode": "solo",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return r


@pytest.fixture()
def authed(hub):
    client, app = hub
    setup_admin(client, app)
    page = client.get("/memory")
    assert page.status_code == 200
    return client, app, csrf_of(page.text)


from .remote_support import (  # noqa: E402, F401 — fixtures shared by the remote/search plugin tests
    _clean,
    start,
)
