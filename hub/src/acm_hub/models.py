"""Database tables. Mirrors the data model in docs/ARCHITECTURE.md § Data model."""

from datetime import UTC, datetime
from typing import Any

from pydantic import NaiveDatetime
from sqlalchemy import JSON, Column, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from .ids import new_id


def utcnow() -> datetime:
    """Naive UTC; SQLite has no timezone type, so everything is stored as UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


class InstanceSettings(SQLModel, table=True):
    __tablename__ = "instance_settings"
    id: int = Field(default=1, primary_key=True)
    instance_id: str = Field(default_factory=new_id)
    deployment_mode: str = "solo"  # solo | team | multi_team
    core_token_budget: int = 2000
    default_token_expiry_days: int = 90
    max_token_expiry_days: int = 365
    local_login_enabled: bool = True
    oidc_provisioning: str = "auto"  # auto | invite
    setup_code_hash: str | None = None
    # Lifecycle / consolidation (docs/ARCHITECTURE.md § How memory changes over time)
    stale_after_days_observed: int = Field(default=90, sa_column_kwargs={"server_default": "90"})
    stale_after_days_confirmed: int = Field(default=365, sa_column_kwargs={"server_default": "365"})
    review_established_days: int = Field(default=365, sa_column_kwargs={"server_default": "365"})
    auto_apply_proposals: bool = Field(
        default=False, sa_column_kwargs={"server_default": "0"}
    )  # observed-only, low-risk kinds
    last_consolidation_at: NaiveDatetime | None = None


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: str = Field(default_factory=new_id, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str | None = None
    auth_provider: str = "local"  # local | oidc
    external_id: str | None = Field(default=None, index=True)  # "<issuer>|<sub>" for OIDC
    is_admin: bool = False
    is_active: bool = Field(
        default=True, sa_column_kwargs={"server_default": "1"}
    )  # False = can't sign in; tokens/sessions are revoked
    theme_preference: str = "system"  # system | light | dark
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class Team(SQLModel, table=True):
    __tablename__ = "teams"
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    slug: str = Field(index=True, unique=True)
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class TeamMember(SQLModel, table=True):
    __tablename__ = "team_members"
    team_id: str = Field(foreign_key="teams.id", primary_key=True)
    user_id: str = Field(foreign_key="users.id", primary_key=True)
    role: str = "member"  # owner | member


class Project(SQLModel, table=True):
    __tablename__ = "projects"
    id: str = Field(default_factory=new_id, primary_key=True)
    slug: str = Field(index=True, unique=True)
    team_id: str | None = Field(default=None, foreign_key="teams.id")
    owner_user_id: str | None = Field(default=None, foreign_key="users.id")
    visibility: str = "private"  # private | team | public
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class ApiToken(SQLModel, table=True):
    __tablename__ = "api_tokens"
    id: str = Field(default_factory=new_id, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(index=True, unique=True)  # SHA-256 hex; the raw token is never stored
    prefix: str  # first characters of the token, for display only
    label: str
    project_ids: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )  # empty = all the user's projects
    include_user_scope: bool = False  # may this token see/write the owner's personal (user-scope) memory?
    access_level: str = "read_only"  # read_only | read_write
    expires_at: NaiveDatetime | None = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    last_used_at: NaiveDatetime | None = None
    revoked_at: NaiveDatetime | None = None
    # Set when this token was issued through OAuth (docs/SECURITY.md § MCP OAuth). Such tokens are managed as a
    # connected app, not as hand-made tokens, and revoking one ends the whole grant.
    grant_id: str | None = Field(default=None, index=True)


class WebSession(SQLModel, table=True):
    __tablename__ = "web_sessions"
    id: str = Field(primary_key=True)  # SHA-256 of the cookie value
    user_id: str = Field(foreign_key="users.id", index=True)
    csrf_token: str
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    authenticated_at: NaiveDatetime = Field(default_factory=utcnow)  # last (re-)authentication
    last_seen_at: NaiveDatetime = Field(default_factory=utcnow)
    expires_at: NaiveDatetime


class AuthFlow(SQLModel, table=True):
    """In-flight OIDC authorization-code flow (state/nonce/PKCE), short-lived."""

    __tablename__ = "auth_flows"
    state_hash: str = Field(primary_key=True)
    nonce: str
    code_verifier: str
    binding_hash: str  # hash of a cookie set in the initiating browser (login-CSRF defence)
    reauth: bool = False
    created_at: NaiveDatetime = Field(default_factory=utcnow)


class MemoryRecord(SQLModel, table=True):
    __tablename__ = "memory_records"
    id: str = Field(default_factory=new_id, primary_key=True)
    scope: str  # project | team | user  (session scope never reaches the hub)
    type: str  # user | feedback | project | reference | intent | rule
    confidence: str = "observed"  # observed | confirmed | established
    tier: str = "associated"  # core | associated
    status: str = "active"  # active | superseded | stale | archived
    name: str
    description: str
    body: str = ""
    topics: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    project_id: str | None = Field(default=None, foreign_key="projects.id", index=True)
    team_id: str | None = Field(default=None, foreign_key="teams.id", index=True)
    user_id: str | None = Field(default=None, foreign_key="users.id", index=True)
    valid_from: NaiveDatetime | None = None
    valid_to: NaiveDatetime | None = None
    last_reinforced: NaiveDatetime | None = None
    reinforcement_count: int = 0
    last_retrieved: NaiveDatetime | None = None
    source: str = "dream-cycle"
    source_trust: str = "internal"  # internal | external (plugin/unattributed content)
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    updated_at: NaiveDatetime = Field(default_factory=utcnow)

    __table_args__ = (Index("ix_memory_records_scope_status", "scope", "status"),)


class MemoryLink(SQLModel, table=True):
    __tablename__ = "memory_links"
    from_id: str = Field(foreign_key="memory_records.id", primary_key=True)
    to_id: str = Field(foreign_key="memory_records.id", primary_key=True)
    kind: str = Field(default="related", primary_key=True)  # related | supersedes


class MemoryRevision(SQLModel, table=True):
    __tablename__ = "memory_revisions"
    id: str = Field(default_factory=new_id, primary_key=True)
    memory_record_id: str = Field(foreign_key="memory_records.id", index=True)
    # Snapshot of the record as of this revision (for an unapplied revision: the *incoming* content).
    name: str
    description: str
    body: str
    type: str
    scope: str
    confidence: str
    tier: str
    status: str
    topics: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    changed_by_user_id: str | None = Field(default=None, foreign_key="users.id")
    changed_by_token_id: str | None = Field(default=None, foreign_key="api_tokens.id")
    changed_by_label: str | None = None  # e.g. OS username for CLI changes
    changed_at: NaiveDatetime = Field(default_factory=utcnow)
    change_note: str | None = None
    change_source: str = (
        "mcp-write"  # dream-cycle | mcp-write | import | ui | cli | plugin | mechanical | api
    )
    flagged: bool = False  # called out for human attention (confirmed-record change, import conflict, ...)
    applied: bool = True  # False = a pending conflict awaiting a human decision


class Reinforcement(SQLModel, table=True):
    """One row per (record, independent source): a single session can only reinforce a record once."""

    __tablename__ = "reinforcements"
    record_id: str = Field(foreign_key="memory_records.id", primary_key=True)
    source_ref: str = Field(primary_key=True)
    reinforced_at: NaiveDatetime = Field(default_factory=utcnow)
    by_token_id: str | None = Field(default=None, foreign_key="api_tokens.id")
    by_user_id: str | None = Field(default=None, foreign_key="users.id")


class Proposal(SQLModel, table=True):
    """A suggested change awaiting a human decision (the consolidation queue)."""

    __tablename__ = "proposals"
    id: str = Field(default_factory=new_id, primary_key=True)
    kind: str  # merge | supersede | promote_scope | promote_core | demote_core | mark_stale | archive | review_established | conflict
    status: str = "pending"  # pending | applied | rejected | expired
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    target_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    rationale: str = ""
    generated_by: str = "mechanical"  # mechanical | dream-skill | llm-worker
    generated_by_token_id: str | None = Field(default=None, foreign_key="api_tokens.id")
    dedupe_key: str = Field(index=True)
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    decided_by_user_id: str | None = Field(default=None, foreign_key="users.id")
    decided_by_label: str | None = None  # "auto" for auto-applied, OS user for CLI
    decided_at: NaiveDatetime | None = None
    decision_note: str | None = None


class PluginInstance(SQLModel, table=True):
    """One configured use of a plugin (e.g. "Slack #memory" using the apprise plugin). Holds no secret values."""

    __tablename__ = "plugin_instances"
    id: str = Field(default_factory=new_id, primary_key=True)
    plugin_key: str = Field(index=True)
    name: str
    owner_user_id: str = Field(
        foreign_key="users.id"
    )  # whose memory a source captures into / whose user-scope a sink may see
    enabled: bool = False
    config: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))  # non-secret settings
    secret_refs: dict = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )  # {secret name: ENV VAR NAME}, never values
    scopes: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )  # deny by default: empty sees nothing
    projects: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )  # project slugs; empty = all within allowed scopes
    events: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )  # subscribed event types
    egress: str = "metadata"  # metadata | full
    user_scope_ack: bool = False  # admin acknowledged that this instance may see the owner's personal memory
    pull_interval_minutes: int = 60  # source plugins
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    last_run_at: NaiveDatetime | None = None
    last_status: str | None = None  # ok | error
    last_error: str | None = None
    consecutive_failures: int = 0
    last_digest_at: NaiveDatetime | None = None


class Event(SQLModel, table=True):
    """The outbox: written in the same transaction as the change that caused it. Payloads are minimal
    (ids, names, links) — bodies are never copied here."""

    __tablename__ = "events"
    id: str = Field(default_factory=new_id, primary_key=True)
    type: str = Field(index=True)
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    scope: str | None = None  # project | team | user | None (scope-less, e.g. plugin.failed)
    project_slug: str | None = None
    team_slug: str | None = None
    owner_user_id: str | None = None  # whose user-scope memory it concerns / who it's for
    instance_id: str | None = None  # set = deliver only to this instance (digests, tests)
    origin_instance_id: str | None = (
        None  # the instance whose own activity caused it: never echoed back to it
    )
    created_at: NaiveDatetime = Field(default_factory=utcnow, index=True)
    dispatched_at: NaiveDatetime | None = None


class PluginDelivery(SQLModel, table=True):
    __tablename__ = "plugin_deliveries"
    event_id: str = Field(foreign_key="events.id", primary_key=True)
    instance_id: str = Field(foreign_key="plugin_instances.id", primary_key=True)
    status: str = "pending"  # pending | delivered | dead
    attempts: int = 0
    next_attempt_at: NaiveDatetime = Field(default_factory=utcnow, index=True)
    last_error: str | None = None
    delivered_at: NaiveDatetime | None = None


class InboxItem(SQLModel, table=True):
    __tablename__ = "inbox_items"
    id: str = Field(default_factory=new_id, primary_key=True)
    owner_user_id: str = Field(foreign_key="users.id", index=True)
    source: str  # mcp | ui | cli | plugin:<key>
    scope: str = "user"
    project_id: str | None = Field(default=None, foreign_key="projects.id")
    title: str
    body: str = ""
    external_ref: str | None = None
    captured_at: NaiveDatetime = Field(default_factory=utcnow)
    status: str = "new"  # new | harvested | dismissed

    __table_args__ = (UniqueConstraint("owner_user_id", "source", "external_ref", name="uq_inbox_external"),)


# Re-exported for type checkers that dislike the Any in Column(JSON).
JSONValue = Any


# --- MCP OAuth (docs/SECURITY.md § MCP OAuth) --------------------------------------------------------------


class OAuthClient(SQLModel, table=True):
    """A client that registered itself (RFC 7591). Always a public client: PKCE, no stored secret."""

    __tablename__ = "oauth_clients"
    id: str = Field(primary_key=True)  # the client_id
    client_name: str | None = None  # self-asserted: shown on the consent screen as untrusted text
    redirect_uris: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    last_used_at: NaiveDatetime | None = None


class OAuthRequest(SQLModel, table=True):
    """An /authorize request parked while the signed-in person decides on the consent screen."""

    __tablename__ = "oauth_requests"
    id: str = Field(primary_key=True)
    client_id: str = Field(index=True)
    redirect_uri: str
    redirect_uri_provided_explicitly: bool = True
    state: str | None = None
    scopes: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    code_challenge: str
    resource: str | None = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    expires_at: NaiveDatetime


class OAuthCode(SQLModel, table=True):
    """A single-use, short-lived authorization code. Only its hash is stored."""

    __tablename__ = "oauth_codes"
    code_hash: str = Field(primary_key=True)
    client_id: str = Field(index=True)
    user_id: str = Field(foreign_key="users.id")
    redirect_uri: str
    redirect_uri_provided_explicitly: bool = True
    code_challenge: str
    resource: str | None = None
    scopes: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    project_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    include_user_scope: bool = False
    access_level: str = "read_only"
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    expires_at: NaiveDatetime
    used_at: NaiveDatetime | None = None
    grant_id: str | None = None  # set once exchanged, so a replayed code can revoke what it produced


class OAuthGrant(SQLModel, table=True):
    """What a person allowed one client to do. The access tokens it mints are ordinary ApiToken rows."""

    __tablename__ = "oauth_grants"
    id: str = Field(default_factory=new_id, primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    client_id: str = Field(index=True)
    project_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    include_user_scope: bool = False
    access_level: str = "read_only"
    scopes: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    resource: str | None = None
    created_at: NaiveDatetime = Field(default_factory=utcnow)
    last_refreshed_at: NaiveDatetime | None = None
    expires_at: NaiveDatetime  # absolute lifetime of the refresh chain
    revoked_at: NaiveDatetime | None = None
    refresh_hash: str = Field(index=True, unique=True)  # current refresh token (hash)
    prev_refresh_hash: str | None = Field(default=None, index=True)  # the one just replaced: reuse => theft
