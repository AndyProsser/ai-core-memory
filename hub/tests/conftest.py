import pytest
from sqlmodel import Session

from acm_hub.access import Principal, principal_for_user
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
