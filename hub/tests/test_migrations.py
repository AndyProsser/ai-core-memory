from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlmodel import SQLModel

import acm_hub.models  # noqa: F401


def _include(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    return not (type_ == "table" and name.startswith("memory_fts"))  # FTS5 is hand-managed


def test_models_and_migrations_agree(engine):
    """Fails if a model changed without a migration (and proves autogenerate won't drop the FTS index)."""
    with engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"include_object": _include}), SQLModel.metadata
        )
    assert diff == [], f"models and migrations have drifted: {diff}"


def test_fts_table_survives_migrations(engine):
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM memory_fts")).scalar() == 0
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert {"memory_fts", "proposals", "reinforcements"} <= names


def test_upgrade_from_phase1_database_keeps_data(settings, monkeypatch):
    """An existing Phase 1 database (head = 0001) upgrades in place without losing rows."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlmodel import Session

    from acm_hub.db import make_engine

    eng = make_engine(settings)
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(acm_hub.models.__file__).parent / "migrations"))
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001")
        conn.execute(
            text(
                "INSERT INTO instance_settings (id, instance_id, deployment_mode, core_token_budget, default_token_expiry_days, "
                "max_token_expiry_days, local_login_enabled, oidc_provisioning) VALUES (1,'X','solo',2000,90,365,1,'auto')"
            )
        )
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    with Session(eng) as s:
        row = s.execute(
            text(
                "SELECT stale_after_days_observed, stale_after_days_confirmed, review_established_days, auto_apply_proposals "
                "FROM instance_settings WHERE id = 1"
            )
        ).one()
    assert tuple(row) == (90, 365, 365, 0)  # defaults backfilled for the existing row
