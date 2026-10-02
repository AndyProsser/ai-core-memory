"""Engine creation (SQLite in WAL mode with FTS5) and migrations."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from .config import Settings


def make_engine(settings: Settings) -> Engine:
    path = Path(settings.db_path)
    new_dir = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if new_dir:
        os.chmod(path.parent, 0o700)
    existed = path.exists()
    engine = create_engine(settings.db_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    if not existed:
        path.touch(mode=0o600)
    # The DB file holds every record and hashed credentials: owner-only, always.
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            path.chmod(0o600)
    except OSError:
        pass
    return engine


def migrate(engine: Engine) -> None:
    """Bring the schema to head (idempotent)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
