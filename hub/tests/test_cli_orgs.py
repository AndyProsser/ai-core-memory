import pytest
from sqlmodel import Session, select

from acm_hub.db import make_engine
from acm_hub.models import ApiToken, InstanceSettings, User

from .test_cli import PASSWORD, make_admin, no_network, run  # noqa: F401

ADMIN = "andy@example.com"


def mk(monkeypatch, capsys, email):
    assert (
        run(monkeypatch, capsys, "user", "create", "--password-stdin", email, stdin=PASSWORD + "\n")[0] == 0
    )


@pytest.fixture()
def hub(settings, monkeypatch, capsys):
    make_admin(monkeypatch, capsys, ADMIN)
    for n in ("alice", "bob"):
        mk(monkeypatch, capsys, f"{n}@example.com")
    with Session(make_engine(settings)) as s:
        s.get(InstanceSettings, 1).deployment_mode = "multi_team"
        s.commit()
    return settings


def test_team_lifecycle_and_role_rules(hub, monkeypatch, capsys):
    r = lambda *a: run(monkeypatch, capsys, *a)  # noqa: E731
    assert r("team", "create", "eng", "--name", "Eng", "--owner", "alice@example.com", "--as", ADMIN)[0] == 0
    code, _, err = r("team", "add", "eng", "bob@example.com", "--as", ADMIN)
    assert code == 1 and "owner" in err  # the admin isn't a member of it, so can't manage it
    assert r("team", "add", "eng", "bob@example.com", "--as", "alice@example.com")[0] == 0
    code, out, _ = r("team", "show", "eng", "--as", "bob@example.com")
    assert "alice@example.com\towner" in out and "bob@example.com\tmember" in out
    assert r("team", "show", "eng", "--as", ADMIN)[0] == 1  # admin can't read membership
    code, _, err = r(
        "team", "set-role", "eng", "alice@example.com", "--role", "member", "--as", "alice@example.com"
    )
    assert code == 1 and "owner" in err  # last owner protected
    assert r("team", "list", "--as", ADMIN)[1].strip().endswith("not a member")


def test_project_commands_and_confirmed_delete(hub, monkeypatch, capsys):
    r = lambda *a: run(monkeypatch, capsys, *a)  # noqa: E731
    r("team", "create", "eng", "--name", "Eng", "--owner", "alice@example.com", "--as", ADMIN)
    A = "alice@example.com"
    assert r("project", "create", "alpha", "--visibility", "team", "--team", "eng", "--as", A)[0] == 0
    assert "alpha\tteam\teng" in r("project", "list", "--as", A)[1]
    assert "alpha" not in r("project", "list", "--as", "bob@example.com")[1]  # not a member yet
    code, _, err = r("project", "delete", "alpha", "--as", A)
    assert code == 2 and "--confirm alpha" in err
    assert r("project", "delete", "alpha", "--confirm", "alpha", "--as", A)[0] == 0
    assert "alpha" not in r("project", "list", "--as", A)[1]


def test_user_deactivate_revokes_and_blocks(hub, monkeypatch, capsys):
    r = lambda *a: run(monkeypatch, capsys, *a)  # noqa: E731
    B = "bob@example.com"
    assert r("token", "create", "--as", B, "--label", "ci")[0] == 0
    assert r("user", "deactivate", B, "--as", ADMIN)[0] == 0
    assert "deactivated" in r("user", "list")[1]
    with Session(make_engine(hub)) as s:
        assert all(t.revoked_at for t in s.exec(select(ApiToken)).all())
        assert s.exec(select(User).where(User.email == B)).one().is_active is False
    assert r("user", "deactivate", ADMIN, "--as", ADMIN)[0] == 1  # not yourself / the last admin
    assert r("user", "activate", B, "--as", ADMIN)[0] == 0
    assert r("user", "deactivate", B, "--as", "alice@example.com")[0] == 1  # members can't


def test_reset_password_prints_temporary_password_once(hub, monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, "user", "reset-password", "bob@example.com", "--as", ADMIN)
    assert code == 0 and len(out.strip().splitlines()[-1]) >= 12
