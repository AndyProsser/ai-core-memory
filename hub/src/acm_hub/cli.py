"""`acm` — the offline, no-AI, no-network operator CLI.

Works directly on the SQLite file (safe alongside a running hub: WAL mode). Records every change as
a revision with change_source='cli' and the OS username. See docs/ARCHITECTURE.md § Import / export.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, col, func, select

from . import __version__, crypto_store, orgs
from . import compile as compile_mod
from . import oauth as oauth_mod
from . import proposals as proposals_mod
from .access import AccessError, NotFound, Principal, principal_for_user
from .auth import has_admin, issue_setup_code, mint_token
from .config import get_settings
from .consolidate import run_consolidation
from .db import make_engine, migrate
from .exportimport import (
    export_files,
    import_files,
    new_export_name,
    parse_record,
    read_dir,
    read_zip,
    to_zip,
    write_dir,
)
from .models import ApiToken, InstanceSettings, MemoryRecord, Project, Team, User, utcnow
from .records import (
    Conflict,
    RecordIn,
    ValidationFailed,
    get_record,
    history,
    list_records,
    project_slug,
    write_record,
)
from .security import check_password_policy, hash_password

EMAIL_HINT = "set --as EMAIL (or ACM_USER) when the hub has more than one user"


class CliError(Exception):
    pass


def _open() -> Session:
    settings = get_settings()
    engine = make_engine(settings)
    migrate(engine)
    s = Session(engine)
    if s.get(InstanceSettings, 1) is None:
        s.add(InstanceSettings(id=1))
        s.commit()
    return s


def _actor(db: Session, email: str | None, args: argparse.Namespace | None = None) -> Principal:
    import os

    email = email or os.environ.get("ACM_USER")
    if email:
        user = db.exec(select(User).where(User.email == email.strip().lower())).first()
        if not user:
            raise CliError(f"No user {email!r}. Create one with `acm user create`.")
    else:
        users = db.exec(select(User)).all()
        if not users:
            raise CliError("No users yet. Run `acm user create --admin you@example.com` first.")
        if len(users) > 1:
            raise CliError(f"More than one user: {EMAIL_HINT}.")
        user = users[0]
    p = principal_for_user(db, user, kind="cli", label=getpass.getuser())
    if args is not None and getattr(args, "unlock", False):
        _unlock(db, p)
    return p


def _unlock(db: Session, p: Principal) -> None:
    """Open this person's encrypted private memory for the one command being run (--unlock)."""
    import os

    from . import crypto_store, keys

    if not keys.is_enabled(db, p.user_id):
        return  # nothing is encrypted, so there is nothing to unlock
    passphrase = os.environ.get("ACM_PASSPHRASE") or getpass.getpass("Memory passphrase: ")
    dek = keys.unlock(db, p.user_id, passphrase)
    if dek is None:
        raise CliError("That isn't your memory passphrase.")
    p.dek = dek
    crypto_store.attach_keys(db, p.user_id, dek)


def _password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Confirm: "):
            raise CliError("The passwords don't match.")
    if problem := check_password_policy(pw):
        raise CliError(problem)
    return pw


# --- commands -------------------------------------------------------------------------------------------


def cmd_migrate(args: argparse.Namespace) -> int:
    _open().close()
    print("Database is up to date.")
    return 0


def cmd_setup_code(args: argparse.Namespace) -> int:
    with _open() as db:
        if has_admin(db):
            raise CliError(
                "An admin already exists; first-run setup is closed. Use `acm user create --admin` to add another."
            )
        print(issue_setup_code(db))
    return 0


def cmd_user_create(args: argparse.Namespace) -> int:
    email = args.email.strip().lower()
    with _open() as db:
        if db.exec(select(User).where(User.email == email)).first():
            raise CliError(f"{email} already exists.")
        if args.oidc:
            user = User(
                email=email, auth_provider="oidc", is_admin=args.admin
            )  # invited: links on first verified SSO login
        else:
            user = User(email=email, password_hash=hash_password(_password(args)), is_admin=args.admin)
        db.add(user)
        db.commit()
        print(
            f"Created {'admin ' if args.admin else ''}user {email}"
            + (" (invited via SSO)" if args.oidc else "")
        )
    return 0


def cmd_user_list(args: argparse.Namespace) -> int:
    with _open() as db:
        for u in db.exec(select(User).order_by(col(User.created_at))).all():
            kind = "admin" if u.is_admin else "member"
            sso = "sso" if u.auth_provider == "oidc" else "local"
            state = "active" if u.is_active else "deactivated"
            print(f"{u.email}\t{kind}\t{sso}\t{state}\t{'linked' if u.external_id else ''}")
    return 0


def cmd_user_set_password(args: argparse.Namespace) -> int:
    with _open() as db:
        user = db.exec(select(User).where(User.email == args.email.strip().lower())).first()
        if not user:
            raise CliError("No such user.")
        user.password_hash = hash_password(_password(args))
        user.auth_provider = "local" if user.auth_provider == "local" else user.auth_provider
        db.add(user)
        db.commit()
        print("Password updated.")
    return 0


def cmd_token_create(args: argparse.Namespace) -> int:
    with _open() as db:
        p = _actor(db, args.user, args)
        user = db.get(User, p.user_id)
        project_ids = []
        for slug in args.project or []:
            proj = db.exec(select(Project).where(Project.slug == slug)).first()
            if not proj:
                raise CliError(f"No project {slug!r}.")
            project_ids.append(proj.id)
        try:
            raw, tok = mint_token(
                db,
                user,
                label=args.label,
                project_ids=project_ids,  # type: ignore[arg-type]
                access_level="read_write" if args.read_write else "read_only",
                expires_days=args.expires_days,
                include_user_scope=args.include_user_scope,
            )
        except ValueError as e:
            raise CliError(str(e)) from e
        db.commit()
        print(
            f"Token for {user.email} ({tok.access_level}, expires {tok.expires_at:%Y-%m-%d}). Shown once — copy it now:\n\n{raw}\n"
        )
    return 0


def cmd_token_list(args: argparse.Namespace) -> int:
    with _open() as db:
        p = _actor(db, args.user, args)
        for t in db.exec(
            select(ApiToken).where(ApiToken.user_id == p.user_id).order_by(col(ApiToken.created_at))
        ).all():
            state = (
                "revoked"
                if t.revoked_at
                else ("expired" if t.expires_at and t.expires_at <= utcnow() else "active")
            )
            print(
                f"{t.id}\t{t.prefix}…\t{t.label}\t{t.access_level}\t{state}\texpires {t.expires_at:%Y-%m-%d}"
            )
    return 0


def cmd_token_revoke(args: argparse.Namespace) -> int:
    with _open() as db:
        tok = db.get(ApiToken, args.id)
        if not tok:
            raise CliError("No such token id (see `acm token list`).")
        oauth_mod.revoke_api_token(db, tok)  # an OAuth app's token ends its whole grant
        db.commit()
        print("Revoked. It stops working immediately.")
    return 0


def _brief(db: Session, r: MemoryRecord) -> str:
    where = project_slug(db, r) or r.scope
    return f"{r.id}\t{'core' if r.tier == 'core' else 'assoc'}\t{r.type}\t{r.confidence}\t{r.status}\t{where}\t{r.name}"


def cmd_list(args: argparse.Namespace) -> int:
    with _open() as db:
        p = _actor(db, args.user, args)
        rows = list_records(
            db,
            p,
            q=args.query,
            scope=args.scope,
            type=args.type,
            tier=args.tier,
            confidence=args.confidence,
            status=None if args.status == "all" else args.status,
            topic=args.topic,
            project=args.project,
        )
        for r in rows:
            print(_brief(db, r))
        print(f"{len(rows)} record(s)", file=sys.stderr)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from .exportimport import render_record

    with _open() as db:
        p = _actor(db, args.user, args)
        r = get_record(db, p, args.ref, project=args.project)
        print(render_record(db, r))
        if args.history:
            for rev in history(db, p, r):
                flag = " [flagged]" if rev.flagged else ""
                pend = " [PENDING]" if not rev.applied else ""
                print(
                    f"# {rev.changed_at:%Y-%m-%d %H:%M} {rev.change_source}{flag}{pend} — {rev.change_note or ''}",
                    file=sys.stderr,
                )
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    with _open() as db:
        p = _actor(db, args.user, args)
        r = get_record(db, p, args.ref, project=args.project)
        body = None
        if args.body_file:
            body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text()
        data = RecordIn(
            id=r.id,
            description=args.description,
            body=body,
            tier=args.tier,
            confidence=args.confidence,
            status=args.status,
            type=args.type,
            topics=[t.strip() for t in args.topics.split(",")] if args.topics is not None else None,
        )
        res = write_record(
            db, p, data, change_source="cli", note=args.note, confirm_established=args.confirm_established
        )
        db.commit()
        print(f"{res.action}: {res.record.name}")
        for n in res.notices:
            print(f"note: {n}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    with _open() as db:
        p = _actor(db, args.user, args)
        files = export_files(
            db,
            p,
            scope=args.scope,
            project=args.project,
            with_history=args.with_history,
            with_inbox=args.with_inbox,
        )
        n = sum(
            1
            for f in files
            if f.endswith(".md") and not f.endswith("MEMORY.md") and not f.startswith(("_history", "_inbox"))
        )
        if args.zip:
            dest = Path(args.zip)
            dest.write_bytes(to_zip(files))
            dest.chmod(0o600)
            print(f"Wrote {n} record(s) to {dest}")
        else:
            out = Path(args.out or new_export_name())
            write_dir(files, out)
            print(f"Wrote {n} record(s) to {out}/")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        raise CliError(f"{path} doesn't exist.")
    files = (
        read_dir(path)
        if path.is_dir()
        else (read_zip(path.read_bytes()) if path.suffix == ".zip" else {path.name: path.read_bytes()})
    )
    with _open() as db:
        p = _actor(db, args.user, args)
        report = import_files(db, p, files, apply=args.apply, change_source="cli")
        for i in report.items:
            print(f"{i.action:<9} {i.name}" + (f"  — {i.detail}" if i.detail else ""))
        s = report.summary
        print(
            f"\n{s['create']} create · {s['update']} update · {s['unchanged']} unchanged · {s['conflict']} held for review · {s['error']} skipped"
            + (f" · {s['inbox']} inbox" if s["inbox"] else "")
        )
        if args.apply:
            db.commit()
            print("Applied.")
        else:
            db.rollback()
            print("Dry run — nothing changed. Re-run with --apply to import.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    settings = get_settings()
    print(f"hub {__version__}; database {settings.db_path}")
    with _open() as db:
        row = db.execute(text("PRAGMA integrity_check")).scalar()
        print(f"sqlite integrity: {row}")
        ok &= row == "ok"
        mode = db.execute(text("PRAGMA journal_mode")).scalar()
        print(f"journal mode: {mode}")
        n = db.execute(text("SELECT count(*) FROM memory_records")).scalar()
        f = db.execute(text("SELECT count(*) FROM memory_fts")).scalar()
        print(f"records: {n}; search index rows: {f}")
        if n != f:
            print("  ! search index is out of sync; run `acm reindex`")
            ok = False
        print(f"admin exists: {has_admin(db)}; users: {len(db.exec(select(User)).all())}")
        mode_bits = settings.db_path.stat().st_mode & 0o077
        if mode_bits:
            print("  ! database file is accessible to other users (expected 0600)")
            ok = False
    print(
        f"SSO: {'configured' if settings.oidc_enabled else 'not configured'}; public URL {settings.public_url}"
    )
    if settings.public_url.startswith("http://") and "localhost" not in settings.public_url:
        print("  ! public URL is plain http; use HTTPS before exposing the hub beyond your LAN")
    print("OK" if ok else "Problems found.")
    return 0 if ok else 1


def cmd_reindex(args: argparse.Namespace) -> int:
    from .records import _fts_sync

    with _open() as db:
        db.execute(text("DELETE FROM memory_fts"))
        rows = db.exec(select(MemoryRecord)).all()
        for r in rows:
            _fts_sync(db, r)
        db.commit()
        print(f"Reindexed {len(rows)} record(s).")
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    """The mechanical pass: decay, duplicate candidates, core budget, review reminders. Instance-wide unless --as."""
    with _open() as db:
        scope_to = _actor(db, args.user, args) if args.user else None
        rep = run_consolidation(db, scope_to=scope_to, dry_run=args.dry_run)
        if not args.dry_run:
            db.commit()
        d = rep.as_dict()
        print(f"{'Would scan' if args.dry_run else 'Scanned'} {d['scanned']} active record(s).")
        print(f"  marked stale automatically (observed, unreinforced): {d['auto_staled']}")
        print(
            f"  new proposals: {d['proposed'] or 'none'}   auto-applied: {d['auto_applied']}   closed as out of date: {d['expired']}   suppressed (recently rejected): {d['suppressed']}"
        )
        if args.dry_run:
            print("Dry run — nothing changed.")
        elif d["proposed"]:
            print("Review them with `acm review`.")
    return 0


def _print_proposal(db: Session, prop, *, detail: bool = False) -> None:  # noqa: ANN001
    v = proposals_mod.view(db, prop)
    flag = " [needs --confirm-established]" if v.needs_confirm else ""
    stale = f" [OUT OF DATE: {v.stale_reason}]" if v.stale_reason else ""
    print(f"{prop.id}\t{prop.kind}\t{prop.generated_by}\t{v.summary}{flag}{stale}")
    if detail:
        print(f"  why: {prop.rationale}")
        for r in v.records.values():
            print(f"  - {r.name} ({r.confidence}, {r.tier}, {r.status}): {r.description}")
        if v.incoming is not None:
            print("  incoming version:\n    " + v.incoming.body.replace("\n", "\n    "))
        merged = prop.payload.get("merged")
        if merged:
            print(
                "  proposed merged text:\n    "
                + (merged.get("body") or merged.get("description") or "").replace("\n", "\n    ")
            )


def cmd_review(args: argparse.Namespace) -> int:
    with _open() as db:
        p = _actor(db, args.user, args)
        sub = args.rcmd or "list"
        if sub == "list":
            rows = proposals_mod.list_proposals(db, p, status=None if args.all else "pending")
            for prop in rows:
                _print_proposal(db, prop)
            print(f"{len(rows)} proposal(s)" + ("" if args.all else " pending"), file=sys.stderr)
            return 0
        prop = proposals_mod.get_proposal(db, p, args.id)
        if sub == "show":
            _print_proposal(db, prop, detail=True)
            return 0
        try:
            proposals_mod.decide(
                db,
                p,
                args.id,
                approve=sub == "approve",
                note=args.note,
                confirm_established=getattr(args, "confirm_established", False),
            )
        except proposals_mod.ProposalExpired as e:
            db.commit()  # keep the "expired" closure
            raise CliError(str(e)) from e
        db.commit()
        print(("Approved and applied." if sub == "approve" else "Rejected.") + f" ({prop.kind})")
    return 0


def cmd_plugins(args: argparse.Namespace) -> int:
    """Inspect and switch plugin instances straight from the database. Deliberately never loads or runs plugin code:
    the offline CLI stays free of third-party code, and `disable` is the emergency off-switch that works with the hub down."""
    from .models import PluginInstance

    with _open() as db:
        if args.pcmd in (None, "list"):
            rows = db.exec(select(PluginInstance).order_by(col(PluginInstance.created_at))).all()
            for i in rows:
                state = "on" if i.enabled else "off"
                sees = ",".join(i.scopes) or "nothing"
                print(
                    f"{i.id}\t{state}\t{i.plugin_key}\t{i.name}\tsees: {sees}\t{i.egress}\t{i.last_status or '-'}"
                    + (f"\t{i.last_error}" if i.last_error else "")
                )
            print(f"{len(rows)} plugin instance(s)", file=sys.stderr)
            return 0
        inst = db.get(PluginInstance, args.id)
        if inst is None:
            raise CliError("No such plugin instance (see `acm plugins`).")
        if args.pcmd == "disable":
            inst.enabled = False
            msg = f"Disabled {inst.name}. It will not send or pull anything."
        else:
            inst.enabled = True
            inst.consecutive_failures = 0
            msg = f"Enabled {inst.name}."
        db.add(inst)
        db.commit()
        print(msg)
    return 0


def _user(db: Session, email: str) -> User:
    user = db.exec(select(User).where(User.email == email.strip().lower())).first()
    if not user:
        raise CliError(f"No user {email!r} (see `acm user list`).")
    return user


def cmd_user_admin(args: argparse.Namespace) -> int:
    with _open() as db:
        actor = _actor(db, args.user, args)
        user = _user(db, args.email)
        if args.ucmd in ("deactivate", "activate"):
            orgs.set_user_active(db, actor, user, args.ucmd == "activate")
            db.commit()
            print(
                f"Deactivated {user.email}: signed out everywhere, tokens revoked. Their memory is untouched."
                if args.ucmd == "deactivate"
                else f"Reactivated {user.email}. Revoked tokens stay revoked; they can mint new ones."
            )
        elif args.ucmd in ("make-admin", "remove-admin"):
            orgs.set_user_admin(db, actor, user, args.ucmd == "make-admin")
            db.commit()
            print(f"{user.email} is {'now' if args.ucmd == 'make-admin' else 'no longer'} an admin.")
        else:  # reset-password
            temp = orgs.reset_password(db, actor, user)
            db.commit()
            print(f"Temporary password for {user.email} (shown once; they are signed out everywhere):")
            print(temp)
    return 0


def _team(db: Session, slug: str):  # noqa: ANN202
    try:
        return orgs.get_team(db, slug)
    except NotFound as e:
        raise CliError(f"No team {slug!r} (see `acm team list`).") from e


def cmd_team(args: argparse.Namespace) -> int:
    with _open() as db:
        actor = _actor(db, args.user, args)
        sub = args.tcmd
        if sub in (None, "list"):
            for t in orgs.visible_teams(db, actor):
                role = orgs.team_role(db, actor.user_id, t.id) or "not a member"
                print(f"{t.slug}\t{t.name}\t{role}")
            return 0
        if sub == "create":
            t = orgs.create_team(db, actor, args.name, args.slug, _user(db, args.owner))
            db.commit()
            print(f"Created team {t.slug} with {args.owner} as its owner.")
            return 0
        team = _team(db, args.slug)
        if sub == "show":
            if orgs.team_role(db, actor.user_id, team.id) is None:
                raise AccessError("Only a team's members can see who is on it.")
            for u, role in orgs.members(db, team):
                print(f"{u.email}\t{role}")
        elif sub == "add":
            orgs.add_member(db, actor, team, _user(db, args.email), args.role)
            db.commit()
            print(f"Added {args.email} to {team.slug} as {args.role}.")
        elif sub == "set-role":
            orgs.set_member_role(db, actor, team, _user(db, args.email), args.role)
            db.commit()
            print(f"{args.email} is now {args.role} of {team.slug}.")
        elif sub == "remove":
            orgs.remove_member(db, actor, team, _user(db, args.email))
            db.commit()
            print(
                f"Removed {args.email} from {team.slug}. Their access to the team's projects ended immediately."
            )
        elif sub == "delete":
            if args.confirm != team.slug:
                raise CliError(f"Deleting a team is permanent. Re-run with --confirm {team.slug}.")
            n = orgs.delete_team(db, actor, team, purge_archived=args.purge_archived)
            db.commit()
            print(
                f"Deleted team {team.slug}." + (f" Permanently removed {n} archived record(s)." if n else "")
            )
    return 0


def cmd_project(args: argparse.Namespace) -> int:
    with _open() as db:
        actor = _actor(db, args.user, args)
        sub = args.pcmd
        if sub in (None, "list"):
            for pr in orgs.visible_projects(db, actor):
                team = db.get(Team, pr.team_id).slug if pr.team_id else "-"
                print(f"{pr.slug}\t{pr.visibility}\t{team}")
            return 0
        team = _team(db, args.team) if getattr(args, "team", None) else None
        if sub == "create":
            pr = orgs.create_project(db, actor, args.slug, visibility=args.visibility, team=team)
            db.commit()
            print(f"Created project {pr.slug} ({pr.visibility}).")
            return 0
        pr = db.exec(select(Project).where(Project.slug == args.slug)).first()
        if pr is None or pr.id not in {p.id for p in orgs.visible_projects(db, actor)}:
            raise CliError(f"No project {args.slug!r} (see `acm project list`).")
        if sub == "set":
            orgs.update_project(db, actor, pr, visibility=args.visibility, team=team)
            db.commit()
            print(f"{pr.slug} is now {pr.visibility}.")
        elif sub == "delete":
            if args.confirm != pr.slug:
                raise CliError(f"Deleting a project is permanent. Re-run with --confirm {pr.slug}.")
            n = orgs.delete_project(db, actor, pr, purge_archived=args.purge_archived)
            db.commit()
            print(
                f"Deleted project {pr.slug}." + (f" Permanently removed {n} archived record(s)." if n else "")
            )
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    """Turn core + rule records into the instruction file another AI tool reads. Offline, deterministic."""
    targets = list(compile_mod.TARGETS) if args.target == "all" else [args.target]
    projects = set(args.project or [])
    if args.from_dir:
        items = []
        files = read_dir(Path(args.from_dir))
        for path, data in sorted(files.items()):
            if (
                not path.endswith(".md")
                or path.endswith("MEMORY.md")
                or path.startswith(("_history", "_inbox"))
            ):
                continue
            try:
                parsed = parse_record(data.decode("utf-8"), path_hint=path)
            except (ValidationFailed, UnicodeDecodeError):
                continue  # not a record (README, template); `acm import` is the place to report those
            d = parsed.data
            items.append(
                compile_mod.Item(
                    name=d.name or "",
                    description=d.description or "",
                    body=d.body or "",
                    type=d.type or "",
                    scope=d.scope or "",
                    tier=d.tier or "associated",
                    confidence=d.confidence or "observed",
                    status=d.status or "active",
                    project=d.project,
                )
            )
    else:
        with _open() as db:
            p = _actor(db, args.user, args)
            items = [
                compile_mod.Item(
                    name=r.name,
                    description=r.description,
                    body=r.body,
                    type=r.type,
                    scope=r.scope,
                    tier=r.tier,
                    confidence=r.confidence,
                    status=r.status,
                    project=project_slug(db, r),
                )
                for r in list_records(db, p, status="active", limit=100_000)
                if not crypto_store.is_locked(r)  # a locked record has nothing to compile
            ]
    chosen = compile_mod.select_items(
        items,
        projects=projects,
        include_user_scope=args.include_user_scope,
        include_observed=args.include_observed,
    )
    out_dir = Path(args.out or ".")
    for key in targets:
        target = compile_mod.TARGETS[key]
        if args.stdout:
            print(compile_mod.render_target(target, chosen), end="")
            continue
        dest = compile_mod.write_target(
            out_dir,
            target,
            chosen,
            include_user_scope=args.include_user_scope,
            allow_in_repo=args.allow_in_repo,
        )
        print(f"Wrote {len(chosen)} record(s) to {dest} ({target.note})")
    return 0


def cmd_oauth(args: argparse.Namespace) -> int:
    """Inspect and end OAuth grants straight from the database, with the hub down if need be. Like `acm plugins`,
    it never loads protocol code: it is the offline off-switch for connected apps."""
    from .models import OAuthClient, OAuthGrant

    with _open() as db:
        actor = _actor(db, args.user, args)
        everyone = getattr(args, "all_users", False)
        if everyone and not actor.is_admin:
            raise AccessError("Only an admin can look at everyone's connected apps.")
        mine = select(OAuthGrant).order_by(col(OAuthGrant.created_at))
        if not everyone:
            mine = mine.where(OAuthGrant.user_id == actor.user_id)
        if args.ocmd in (None, "list"):
            rows = db.exec(mine).all()
            for g in rows:
                c = db.get(OAuthClient, g.client_id)
                u = db.get(User, g.user_id)
                state = "revoked" if g.revoked_at else ("expired" if g.expires_at <= utcnow() else "active")
                where = "all projects" if not g.project_ids else f"{len(g.project_ids)} project(s)"
                print(
                    f"{g.id}\t{u.email if u else '?'}\t{(c.client_name if c else None) or '(unnamed)'}\t"
                    f"{g.access_level}\t{where}{' +personal' if g.include_user_scope else ''}\t{state}"
                )
            print(f"{len(rows)} grant(s)", file=sys.stderr)
            return 0
        # revoke: one grant, or every grant of this user (or of everyone, for an admin)
        if not args.id and not args.all_grants:
            raise CliError("Name a grant id, or pass --all to end every grant in scope.")
        targets = [db.get(OAuthGrant, args.id)] if args.id else list(db.exec(mine).all())
        n = 0
        for g in targets:
            if g is None or (not actor.is_admin and g.user_id != actor.user_id):
                raise CliError("No such grant (see `acm oauth list`).")
            if g.revoked_at is None:
                oauth_mod.revoke_grant(db, g)
                n += 1
        db.commit()
        print(f"Ended {n} grant(s). Their access and refresh tokens stop working immediately.")
    return 0


def _secret_reader(args: argparse.Namespace):  # noqa: ANN202
    """Passphrases come from prompts, or, with --passphrase-stdin, one per line from standard input (scripting/tests)."""

    def read(prompt: str, *, confirm: bool = False) -> str:
        if args.passphrase_stdin:
            return sys.stdin.readline().rstrip("\n")
        value = getpass.getpass(prompt)
        if confirm and value != getpass.getpass("Confirm: "):
            raise CliError("The passphrases don't match.")
        return value

    return read


def cmd_key(args: argparse.Namespace) -> int:
    """Manage encrypted private memory from the host (docs/SECURITY.md § Encrypted private memory)."""
    from . import keys

    read = _secret_reader(args)
    with _open() as db:
        actor = _actor(db, args.user)
        user = db.get(User, actor.user_id)
        sub = args.kcmd
        if sub in (None, "status"):
            uk = keys.get_keys(db, user.id)
            if uk is None:
                print("Encrypted private memory is off.")
            else:
                n = db.exec(
                    select(func.count())
                    .select_from(MemoryRecord)
                    .where(MemoryRecord.user_id == user.id, col(MemoryRecord.encrypted).is_(True))
                ).one()
                print(
                    f"Encrypted private memory is on since {uk.created_at:%Y-%m-%d}: {n} record(s) encrypted."
                )
            return 0
        if sub == "enable":
            res = keys.enable(db, user, read("New memory passphrase: ", confirm=True))
            db.commit()
            keys.scrub(db)
            print(f"Encrypted {res.records} private record(s).")
            print(
                "Recovery key (shown once; write it down — without it and the passphrase nothing can be recovered):"
            )
            print(res.recovery_key)
        elif sub == "change-passphrase":
            old = read("Current memory passphrase: ")
            keys.change_passphrase(db, user, old, read("New memory passphrase: ", confirm=True))
            db.commit()
            print("Memory passphrase changed. Your recovery key still works.")
        elif sub == "recovery-key":
            text = keys.regenerate_recovery_key(db, user, read("Memory passphrase: "))
            db.commit()
            print("New recovery key (shown once; the old one no longer works):")
            print(text)
        elif sub == "recover":
            recovery = read("Recovery key: ") if args.passphrase_stdin else input("Recovery key: ")
            keys.recover(db, user, recovery, read("New memory passphrase: ", confirm=True))
            db.commit()
            print("Recovered. The new passphrase is set.")
        elif sub == "disable":
            n = keys.disable(db, user, read("Memory passphrase: "))
            db.commit()
            print(f"Encryption is off. {n} private record(s) are stored as plain text again.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "acm_hub.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level="info",
        proxy_headers=get_settings().trust_proxy,
        forwarded_allow_ips="*" if get_settings().trust_proxy else None,
    )
    return 0


# --- parser ---------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="acm", description="Offline operator CLI for the memory hub (no network, no AI)."
    )
    ap.add_argument("--version", action="version", version=f"acm {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_: str, user: bool = True):  # noqa: ANN001, ANN202
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        if user:
            p.add_argument("--as", dest="user", metavar="EMAIL", help=f"act as this user ({EMAIL_HINT})")
            p.add_argument(
                "--unlock",
                action="store_true",
                help="open your encrypted private memory for this command (asks for the passphrase, or ACM_PASSPHRASE)",
            )
        return p

    add("migrate", cmd_migrate, "create/upgrade the database schema", user=False)
    add(
        "setup-code",
        cmd_setup_code,
        "print a one-time code to claim first-run setup in the browser",
        user=False,
    )
    add("doctor", cmd_doctor, "check database integrity, search index and configuration", user=False)
    add("reindex", cmd_reindex, "rebuild the full-text search index", user=False)

    u = sub.add_parser("user", help="manage users")
    usub = u.add_subparsers(dest="ucmd", required=True)
    c = usub.add_parser("create", help="create a user")
    c.set_defaults(fn=cmd_user_create)
    c.add_argument("email")
    c.add_argument("--admin", action="store_true")
    c.add_argument(
        "--oidc",
        action="store_true",
        help="invite an SSO user (no local password); links on first verified SSO login",
    )
    c.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin instead of prompting"
    )
    usub.add_parser("list", help="list users").set_defaults(fn=cmd_user_list)
    sp = usub.add_parser("set-password", help="set or reset a local password")
    sp.set_defaults(fn=cmd_user_set_password)
    sp.add_argument("email")
    sp.add_argument("--password-stdin", action="store_true")

    t = sub.add_parser("token", help="manage API tokens")
    tsub = t.add_subparsers(dest="tcmd", required=True)
    tc = tsub.add_parser("create", help="mint an API token (shown once)")
    tc.set_defaults(fn=cmd_token_create)
    tc.add_argument("--as", dest="user", metavar="EMAIL")
    tc.add_argument("--label", required=True)
    tc.add_argument(
        "--project",
        action="append",
        help="limit to this project slug (repeatable); default all the user's projects",
    )
    tc.add_argument("--read-write", action="store_true", help="default is read-only")
    tc.add_argument("--expires-days", type=int, default=None)
    tc.add_argument(
        "--include-user-scope",
        action="store_true",
        help="allow the token to read/write personal (user-scope) memory",
    )
    tl = tsub.add_parser("list", help="list your tokens")
    tl.set_defaults(fn=cmd_token_list)
    tl.add_argument("--as", dest="user", metavar="EMAIL")
    tr = tsub.add_parser("revoke", help="revoke a token by id")
    tr.set_defaults(fn=cmd_token_revoke)
    tr.add_argument("id")

    ls = add("list", cmd_list, "list records")
    ls.add_argument("-q", "--query")
    for f in ("scope", "type", "tier", "confidence", "topic", "project"):
        ls.add_argument(f"--{f}")
    ls.add_argument(
        "--status", default="active", help="active (default), stale, superseded, archived, or all"
    )

    sh = add("show", cmd_show, "print one record (by id or name)")
    sh.add_argument("ref")
    sh.add_argument("--project")
    sh.add_argument("--history", action="store_true", help="also list revisions (to stderr)")

    ed = add("edit", cmd_edit, "change a record")
    ed.add_argument("ref")
    ed.add_argument("--project")
    ed.add_argument("--description")
    ed.add_argument("--body-file", help="file with the new body ('-' for stdin)")
    ed.add_argument("--type")
    ed.add_argument("--tier", choices=["core", "associated"])
    ed.add_argument("--confidence", choices=["observed", "confirmed", "established"])
    ed.add_argument("--status", choices=["active", "superseded", "stale", "archived"])
    ed.add_argument("--topics", help="comma-separated; replaces the existing topics")
    ed.add_argument("--note", help="revision note")
    ed.add_argument(
        "--confirm-established", action="store_true", help="required to change an established record"
    )

    ex = add("export", cmd_export, "export records as markdown (+ manifest)")
    ex.add_argument("--out", help="output directory (default: ./acm-export-<timestamp>)")
    ex.add_argument("--zip", metavar="FILE", help="write a .zip instead of a directory")
    ex.add_argument("--scope")
    ex.add_argument("--project")
    ex.add_argument("--with-history", action="store_true")
    ex.add_argument("--with-inbox", action="store_true")

    im = add("import", cmd_import, "import an export directory/zip or a record .md (dry run unless --apply)")
    im.add_argument("path")
    im.add_argument("--apply", action="store_true", help="actually write; default is a dry run")

    co = add(
        "consolidate",
        cmd_consolidate,
        "run the mechanical consolidation pass now (decay, duplicates, core budget)",
    )
    co.add_argument(
        "--dry-run", action="store_true", help="report what would happen without changing anything"
    )

    rv = sub.add_parser("review", help="review and decide consolidation proposals (no AI involved)")
    rv.set_defaults(fn=cmd_review, rcmd=None, all=False)
    rv.add_argument("--as", dest="user", metavar="EMAIL")
    rv.add_argument("--all", action="store_true", help="include decided/expired proposals")
    rsub = rv.add_subparsers(dest="rcmd")
    rl = rsub.add_parser("list", help="list pending proposals (default)")
    rl.add_argument("--all", action="store_true")
    rs = rsub.add_parser("show", help="show a proposal in full")
    rs.add_argument("id")
    ra = rsub.add_parser("approve", help="approve and apply a proposal")
    ra.add_argument("id")
    ra.add_argument("--note")
    ra.add_argument(
        "--confirm-established", action="store_true", help="required when it touches an established record"
    )
    rj = rsub.add_parser("reject", help="reject a proposal (it won't be re-raised for 30 days)")
    rj.add_argument("id")
    rj.add_argument("--note")

    pl = sub.add_parser(
        "plugins", help="list plugin instances, or switch one off/on (never runs plugin code)"
    )
    pl.set_defaults(fn=cmd_plugins, pcmd=None)
    psub = pl.add_subparsers(dest="pcmd")
    psub.add_parser("list", help="list instances (default)")
    pd = psub.add_parser("disable", help="emergency off-switch for an instance")
    pd.add_argument("id")
    pe = psub.add_parser("enable", help="turn an instance back on")
    pe.add_argument("id")

    sv = add("serve", cmd_serve, "run the hub (web UI, REST, MCP)", user=False)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)

    for name in ("deactivate", "activate", "make-admin", "remove-admin", "reset-password"):
        ua = usub.add_parser(
            name,
            help={
                "deactivate": "block sign-in and revoke tokens (memory is kept)",
                "activate": "allow a deactivated user to sign in again",
                "make-admin": "grant the admin platform role (no memory access comes with it)",
                "remove-admin": "remove the admin role (the last admin can't be removed)",
                "reset-password": "set a one-time temporary password (local accounts)",
            }[name],
        )
        ua.set_defaults(fn=cmd_user_admin)
        ua.add_argument("email")
        ua.add_argument("--as", dest="user", metavar="EMAIL", help=f"act as this admin ({EMAIL_HINT})")

    tm = sub.add_parser("team", help="manage teams and their members")
    tm.set_defaults(fn=cmd_team, tcmd=None, user=None)
    tmsub = tm.add_subparsers(dest="tcmd")
    tmsub.add_parser("list", help="teams you can see (default)")
    tmc = tmsub.add_parser("create", help="create a team with a first owner (admin)")
    tmc.add_argument("slug")
    tmc.add_argument("--name", required=True)
    tmc.add_argument("--owner", required=True, metavar="EMAIL")
    tms = tmsub.add_parser("show", help="list a team's members (members only)")
    tms.add_argument("slug")
    for name, role_req in (("add", False), ("set-role", True), ("remove", None)):
        tp = tmsub.add_parser(name, help=f"{name.replace('-', ' ')} a team member (the team's owners)")
        tp.add_argument("slug")
        tp.add_argument("email")
        if role_req is not None:
            tp.add_argument("--role", choices=["member", "owner"], required=role_req, default="member")
    tmd = tmsub.add_parser("delete", help="delete a team (it must have no live projects or records)")
    tmd.add_argument("slug")
    tmd.add_argument("--confirm", default="", metavar="SLUG", help="type the slug to confirm")
    tmd.add_argument(
        "--purge-archived", action="store_true", help="also permanently delete its archived records"
    )

    for leaf in tmsub.choices.values():
        leaf.add_argument("--as", dest="user", metavar="EMAIL", help=f"act as this user ({EMAIL_HINT})")

    pj = sub.add_parser("project", help="manage projects and who can see them")
    pj.set_defaults(fn=cmd_project, pcmd=None, user=None)
    pjsub = pj.add_subparsers(dest="pcmd")
    pjsub.add_parser("list", help="projects you can see (default)")
    for name in ("create", "set"):
        pp = pjsub.add_parser(name, help=f"{name} a project")
        pp.add_argument("slug")
        pp.add_argument(
            "--visibility", choices=["private", "team", "public"], required=name == "set", default="private"
        )
        pp.add_argument("--team", metavar="SLUG", help="owning team (for team visibility)")
    pjd = pjsub.add_parser("delete", help="delete a project (it must have no live records)")
    pjd.add_argument("slug")
    pjd.add_argument("--confirm", default="", metavar="SLUG", help="type the slug to confirm")
    pjd.add_argument(
        "--purge-archived", action="store_true", help="also permanently delete its archived records"
    )

    for leaf in pjsub.choices.values():
        leaf.add_argument("--as", dest="user", metavar="EMAIL", help=f"act as this user ({EMAIL_HINT})")

    cp = add("compile", cmd_compile, "write core + rule memory into another AI tool's instruction file")
    cp.add_argument("target", choices=[*compile_mod.TARGETS, "all"], help="which tool's file to generate")
    cp.add_argument(
        "--project", action="append", metavar="SLUG", help="include this project's records (repeatable)"
    )
    cp.add_argument(
        "--from-dir",
        metavar="DIR",
        help="read plain record files (an export or memory/data) instead of the hub",
    )
    cp.add_argument("--out", metavar="DIR", help="directory to write into (default: current directory)")
    cp.add_argument("--stdout", action="store_true", help="print instead of writing a file")
    cp.add_argument(
        "--include-user-scope", action="store_true", help="also include personal (user-scope) records"
    )
    cp.add_argument(
        "--include-observed", action="store_true", help="also include unconfirmed (observed) records"
    )
    cp.add_argument(
        "--allow-in-repo", action="store_true", help="allow writing user-scope memory inside a git work tree"
    )

    oa = sub.add_parser("oauth", help="list and end OAuth grants (apps you signed in, e.g. Claude.ai)")
    oa.set_defaults(fn=cmd_oauth, ocmd=None, user=None, all_users=False)
    osub = oa.add_subparsers(dest="ocmd")
    ol = osub.add_parser("list", help="list grants (default)")
    ol.add_argument("--all-users", action="store_true", help="every user's grants (admin)")
    orv = osub.add_parser("revoke", help="end a grant (access and refresh tokens die at once)")
    orv.add_argument("id", nargs="?", help="grant id from `acm oauth list`")
    orv.add_argument("--all", dest="all_grants", action="store_true", help="end every grant in scope")
    orv.add_argument("--all-users", action="store_true", help="with --all: everyone's grants (admin)")
    for leaf in (ol, orv):
        leaf.add_argument("--as", dest="user", metavar="EMAIL", help=f"act as this user ({EMAIL_HINT})")

    ky = sub.add_parser("key", help="manage encrypted private memory (passphrase, recovery key)")
    ky.set_defaults(fn=cmd_key, kcmd=None, user=None, passphrase_stdin=False)
    ksub = ky.add_subparsers(dest="kcmd")
    for name, help_ in (
        ("status", "is it on?"),
        ("enable", "encrypt your private memory under a new passphrase (prints a recovery key once)"),
        ("change-passphrase", "change the memory passphrase"),
        ("recovery-key", "replace the recovery key (prints the new one once)"),
        ("recover", "forgot the passphrase: set a new one with the recovery key"),
        ("disable", "decrypt your private memory back to plain text on disk"),
    ):
        kp = ksub.add_parser(name, help=help_)
        kp.add_argument("--as", dest="user", metavar="EMAIL", help=f"act as this user ({EMAIL_HINT})")
        kp.add_argument(
            "--passphrase-stdin", action="store_true", help="read secrets one per line from stdin"
        )

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (ValidationFailed, AccessError, NotFound) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Conflict as e:
        hint = " (re-run with --confirm-established)" if e.needs_confirmation else ""
        print(f"conflict: {e}{hint}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
