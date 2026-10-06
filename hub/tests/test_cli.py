import io
import socket
import stat
from pathlib import Path

import pytest
from sqlmodel import Session, select

from acm_hub.cli import main
from acm_hub.db import make_engine
from acm_hub.models import ApiToken, MemoryRecord, User

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The CLI must work with no network at all: any attempt to connect fails the test."""

    def deny(*a, **k):
        raise AssertionError("the offline CLI tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


def run(monkeypatch, capsys, *argv, stdin=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def make_admin(monkeypatch, capsys, email="andy@example.com"):
    return run(
        monkeypatch, capsys, "user", "create", "--admin", "--password-stdin", email, stdin=PASSWORD + "\n"
    )


def test_setup_flow_and_files_are_private(settings, monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, "setup-code")
    assert code == 0 and len(out.strip().split("-")) == 3
    assert make_admin(monkeypatch, capsys)[0] == 0
    code, _, err = run(monkeypatch, capsys, "setup-code")
    assert code == 2 and "already exists" in err  # setup closes once an admin exists
    db = Path(settings.db_path)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600 and stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    with Session(make_engine(settings)) as s:
        u = s.exec(select(User)).one()
        assert u.is_admin and u.password_hash.startswith("$argon2id$")


def test_weak_password_rejected(settings, monkeypatch, capsys):
    code, _, err = run(monkeypatch, capsys, "user", "create", "--password-stdin", "a@b.co", stdin="short\n")
    assert code == 2 and "at least" in err


def test_edit_show_list_with_established_guard(settings, monkeypatch, capsys, tmp_path):
    make_admin(monkeypatch, capsys)
    rec = tmp_path / "rule.md"
    rec.write_text(
        "---\nname: never-merge-red\ndescription: No red merges\nmetadata:\n  type: rule\n  scope: user\n  confidence: established\n---\n\nv1\n"
    )
    code, out, _ = run(monkeypatch, capsys, "import", str(rec), "--apply")
    assert code == 0 and "1 create" in out
    assert "never-merge-red" in run(monkeypatch, capsys, "list")[1]
    code, out, _ = run(monkeypatch, capsys, "show", "never-merge-red")
    assert "v1" in out and "confidence: established" in out
    body = tmp_path / "b.txt"
    body.write_text("v2")
    code, _, err = run(monkeypatch, capsys, "edit", "never-merge-red", "--body-file", str(body))
    assert code == 3 and "--confirm-established" in err
    code, out, _ = run(
        monkeypatch,
        capsys,
        "edit",
        "never-merge-red",
        "--body-file",
        str(body),
        "--confirm-established",
        "--note",
        "rewording",
    )
    assert code == 0 and "updated" in out
    code, out, err = run(monkeypatch, capsys, "show", "never-merge-red", "--history")
    assert "v2" in out and "cli" in err and "rewording" in err
    run(monkeypatch, capsys, "edit", "never-merge-red", "--tier", "core", "--confirm-established")
    assert "core" in run(monkeypatch, capsys, "list", "--tier", "core")[1]


def test_export_then_import_into_a_fresh_hub(settings, monkeypatch, capsys, tmp_path):
    make_admin(monkeypatch, capsys)
    for name in ("alpha-note", "beta-note"):
        f = tmp_path / f"{name}.md"
        f.write_text(
            f"---\nname: {name}\ndescription: {name} desc\nmetadata:\n  type: feedback\n  scope: user\n---\n\nbody of {name}\n"
        )
        run(monkeypatch, capsys, "import", str(f), "--apply")
    out_dir = tmp_path / "backup"
    code, out, _ = run(monkeypatch, capsys, "export", "--out", str(out_dir), "--with-history")
    assert code == 0 and "2 record(s)" in out
    assert (out_dir / "manifest.json").exists() and (out_dir / "user" / "MEMORY.md").exists()
    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    zpath = tmp_path / "b.zip"
    assert run(monkeypatch, capsys, "export", "--zip", str(zpath))[0] == 0 and zpath.exists()

    monkeypatch.setenv("MEMORY_HUB_DB_PATH", str(tmp_path / "fresh" / "hub.sqlite3"))  # a brand-new instance
    make_admin(monkeypatch, capsys, "me@new.example")
    code, out, _ = run(monkeypatch, capsys, "import", str(out_dir))  # dry run
    assert code == 0 and "2 create" in out and "Dry run" in out
    assert run(monkeypatch, capsys, "list")[1].strip() == ""
    assert "Applied" in run(monkeypatch, capsys, "import", str(zpath), "--apply")[1]
    assert run(monkeypatch, capsys, "list")[1].count("note") == 2
    assert "2 unchanged" in run(monkeypatch, capsys, "import", str(out_dir), "--apply")[1]  # idempotent


def test_token_lifecycle_from_the_host(settings, monkeypatch, capsys):
    make_admin(monkeypatch, capsys)
    code, out, _ = run(
        monkeypatch, capsys, "token", "create", "--label", "ci", "--read-write", "--expires-days", "7"
    )
    raw = next(line for line in out.splitlines() if line.startswith("acm_live_"))
    with Session(make_engine(settings)) as s:
        t = s.exec(select(ApiToken)).one()
        assert t.token_hash != raw and t.access_level == "read_write"
    code, out, _ = run(monkeypatch, capsys, "token", "list")
    assert "ci" in out and "active" in out
    tid = out.split("\t")[0]
    run(monkeypatch, capsys, "token", "revoke", tid)
    assert "revoked" in run(monkeypatch, capsys, "token", "list")[1]
    assert run(monkeypatch, capsys, "token", "create", "--label", "x", "--expires-days", "99999")[0] == 2


def test_ambiguous_actor_requires_as(settings, monkeypatch, capsys):
    make_admin(monkeypatch, capsys)
    run(monkeypatch, capsys, "user", "create", "--password-stdin", "b@example.com", stdin=PASSWORD + "\n")
    code, _, err = run(monkeypatch, capsys, "list")
    assert code == 2 and "--as" in err
    assert run(monkeypatch, capsys, "list", "--as", "b@example.com")[0] == 0


def test_doctor_and_reindex(settings, monkeypatch, capsys, tmp_path):
    make_admin(monkeypatch, capsys)
    f = tmp_path / "n.md"
    f.write_text(
        "---\nname: findable-thing\ndescription: d\nmetadata:\n  type: feedback\n  scope: user\n---\n\nneedle\n"
    )
    run(monkeypatch, capsys, "import", str(f), "--apply")
    code, out, _ = run(monkeypatch, capsys, "doctor")
    assert code == 0 and "integrity: ok" in out and "journal mode: wal" in out
    with Session(make_engine(settings)) as s:
        s.exec(__import__("sqlalchemy").text("DELETE FROM memory_fts"))  # type: ignore[call-overload]
        s.commit()
    assert run(monkeypatch, capsys, "doctor")[0] == 1
    assert run(monkeypatch, capsys, "reindex")[0] == 0
    assert run(monkeypatch, capsys, "doctor")[0] == 0
    assert "findable-thing" in run(monkeypatch, capsys, "list", "-q", "needle")[1]
    with Session(make_engine(settings)) as s:
        assert s.exec(select(MemoryRecord)).one().name == "findable-thing"


# --- Phase 2: consolidate + review, offline -----------------------------------------------------------------


def _import_md(monkeypatch, capsys, tmp_path, name, body, conf="observed", typ="feedback"):
    f = tmp_path / f"{name}.md"
    f.write_text(
        f"---\nname: {name}\ndescription: {name} about webhook retries\nmetadata:\n  type: {typ}\n  scope: user\n  confidence: {conf}\n---\n\n{body}\n"
    )
    assert run(monkeypatch, capsys, "import", str(f), "--apply")[0] == 0


def test_consolidate_and_review_work_fully_offline(settings, monkeypatch, capsys, tmp_path):
    make_admin(monkeypatch, capsys)
    text = "Always send an idempotency key on webhook retries so a replay can never double charge a customer."
    _import_md(monkeypatch, capsys, tmp_path, "retry-keys-one", text)
    _import_md(monkeypatch, capsys, tmp_path, "retry-keys-two", text)
    code, out, _ = run(monkeypatch, capsys, "consolidate", "--dry-run")
    assert code == 0 and "Dry run" in out and "'merge': 1" in out
    assert run(monkeypatch, capsys, "review")[1].strip() == ""  # dry run changed nothing
    code, out, _ = run(monkeypatch, capsys, "consolidate")
    assert code == 0 and "acm review" in out
    code, out, _ = run(monkeypatch, capsys, "review")
    assert "merge" in out and "mechanical" in out
    pid = out.split("\t")[0]
    assert "why:" in run(monkeypatch, capsys, "review", "show", pid)[1]
    assert run(monkeypatch, capsys, "consolidate")[1].count("new proposals: none") == 1  # idempotent
    code, out, _ = run(monkeypatch, capsys, "review", "approve", pid)
    assert code == 0 and "Approved" in out
    listing = run(monkeypatch, capsys, "list")[1]
    assert listing.count("retry-keys") == 1  # one was retired into history
    assert listing.count("\n") == 1
    assert "superseded" in run(monkeypatch, capsys, "list", "--status", "superseded")[1]
    code, _, err = run(monkeypatch, capsys, "review", "approve", pid)
    assert code == 1 and "already applied" in err


def test_merge_keeps_the_established_record_untouched_and_needs_no_confirmation(
    settings, monkeypatch, capsys, tmp_path
):
    make_admin(monkeypatch, capsys)
    text = "Always send an idempotency key on webhook retries so a replay can never double charge a customer."
    _import_md(monkeypatch, capsys, tmp_path, "bedrock-keys", text, conf="established", typ="rule")
    _import_md(monkeypatch, capsys, tmp_path, "plain-keys", text)
    run(monkeypatch, capsys, "consolidate")
    pid = run(monkeypatch, capsys, "review")[1].split("\t")[0]
    assert "bedrock-keys" in run(monkeypatch, capsys, "review", "show", pid)[1]
    assert (
        run(monkeypatch, capsys, "review", "approve", pid)[0] == 0
    )  # the *weaker* record is retired; the established one is not altered
    shown = run(monkeypatch, capsys, "show", "bedrock-keys")[1]
    assert "confidence: established" in shown and text in shown
    assert "plain-keys" not in run(monkeypatch, capsys, "list")[1]


def test_rejecting_a_proposal_means_it_is_not_raised_again(settings, monkeypatch, capsys, tmp_path):
    make_admin(monkeypatch, capsys)
    text = "Always send an idempotency key on webhook retries so a replay can never double charge a customer."
    _import_md(monkeypatch, capsys, tmp_path, "keys-one", text)
    _import_md(monkeypatch, capsys, tmp_path, "keys-two", text)
    run(monkeypatch, capsys, "consolidate")
    pid = run(monkeypatch, capsys, "review")[1].split("\t")[0]
    assert run(monkeypatch, capsys, "review", "reject", pid, "--note", "they differ")[0] == 0
    assert "suppressed (recently rejected): 1" in run(monkeypatch, capsys, "consolidate")[1]
    assert run(monkeypatch, capsys, "review")[1].strip() == ""
    assert (
        "rejected" in run(monkeypatch, capsys, "review", "--all")[1]
        or pid in run(monkeypatch, capsys, "review", "--all")[1]
    )


def test_user_set_email_changes_a_local_admins_address(settings, monkeypatch, capsys):
    make_admin(monkeypatch, capsys, "andy@example.com")
    code, out, _ = run(monkeypatch, capsys, "user", "set-email", "andy@example.com", "andy+hub@example.com")
    assert code == 0 and "andy+hub@example.com" in out
    _, listing, _ = run(monkeypatch, capsys, "user", "list")
    assert "andy+hub@example.com" in listing and "andy@example.com\t" not in listing


def test_user_set_email_refuses_bad_input(settings, monkeypatch, capsys):
    make_admin(monkeypatch, capsys, "andy@example.com")
    run(monkeypatch, capsys, "user", "create", "--oidc", "bob@example.com")
    assert (
        run(monkeypatch, capsys, "user", "set-email", "andy@example.com", "bob@example.com")[0] != 0
    )  # taken
    assert run(monkeypatch, capsys, "user", "set-email", "andy@example.com", "not-an-email")[0] != 0
    assert run(monkeypatch, capsys, "user", "set-email", "ghost@example.com", "g@example.com")[0] != 0
