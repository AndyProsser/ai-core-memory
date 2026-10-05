"""A complete remote plugin: an RSS/Atom feed reader that brings new entries into the hub's inbox.

Run it as its own service (it needs nothing from the hub except the shared signing key):

    FEED_PLUGIN_SECRET=<32+ random characters> uvicorn app:app --port 9000

then register it with the hub (docs/PLUGINS.md § Remote plugins):

    MEMORY_HUB_REMOTE_PLUGINS='[{"key":"feed","url":"http://127.0.0.1:9000","secret_env":"FEED_PLUGIN_SECRET"}]'

It holds its own configuration and fetches the feed itself, so the hub never touches the network on its behalf.
Feeds are untrusted input: DOCTYPE/ENTITY declarations are refused (no entity-expansion tricks), responses are
size-capped, and by default only https URLs are fetched (set FEED_ALLOW_HTTP=1 for a feed on your own network).
"""

from __future__ import annotations

import html
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from acm_hub.plugins.remote_sdk import (
    create_app,
)  # a service outside this repo can vendor remote_sdk.py instead

MAX_BYTES = 2_000_000
TAGS = re.compile(r"<[^>]+>")
ATOM = "{http://www.w3.org/2005/Atom}"

MANIFEST = {
    "key": "feed",
    "name": "RSS / Atom feed",
    "description": "Captures new entries from a feed into your inbox. Runs as a separate service.",
    "kind": "source",
    "config": [
        {
            "name": "feed_url",
            "type": "string",
            "required": True,
            "label": "Feed URL",
            "help": "The RSS or Atom feed to follow.",
        },
        {
            "name": "max_items",
            "type": "integer",
            "default": 20,
            "help": "Most recent entries to capture per check.",
        },
        # Named fields the hub understands for sources: where captured items are filed.
        {"name": "inbox_scope", "type": "select", "options": ["user", "project", "team"], "default": "user"},
        {"name": "inbox_project", "type": "string", "help": "Project slug, when the inbox scope is project."},
    ],
}


def _fetch(url: str) -> bytes:
    parts = urlsplit(url)
    allow_http = os.environ.get("FEED_ALLOW_HTTP") == "1"
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        raise ValueError("only https feed URLs are fetched (the operator can set FEED_ALLOW_HTTP=1)")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "acm-remote-feed",
            "Accept": "application/atom+xml, application/rss+xml, text/xml",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — scheme checked above
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("the feed is too large")
    return data


def parse_feed(data: bytes, limit: int) -> list[dict]:
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise ValueError("feeds with DOCTYPE/ENTITY declarations are refused")
    root = ET.fromstring(data)  # noqa: S314 — DOCTYPE/ENTITY refused above
    items = []
    if root.tag == f"{ATOM}feed":
        for e in root.findall(f"{ATOM}entry"):
            link = next(
                (a.get("href") for a in e.findall(f"{ATOM}link") if a.get("rel", "alternate") == "alternate"),
                None,
            )
            body = (e.findtext(f"{ATOM}summary") or e.findtext(f"{ATOM}content") or "").strip()
            items.append((e.findtext(f"{ATOM}title") or "", body, e.findtext(f"{ATOM}id") or link, link))
    else:
        for e in root.iter("item"):
            items.append(
                (
                    e.findtext("title") or "",
                    (e.findtext("description") or "").strip(),
                    e.findtext("guid") or e.findtext("link"),
                    e.findtext("link"),
                )
            )
    out = []
    for title, body, ref, link in items[:limit]:
        text = html.unescape(TAGS.sub("", body)).strip()
        out.append(
            {
                "title": html.unescape(title).strip() or "(untitled)",
                "body": (text + (f"\n\n{link}" if link else "")).strip(),
                "external_ref": ref,
            }
        )
    return out


def validate(instance: dict, config: dict) -> dict:
    url = str(config.get("feed_url") or "")
    if not url.startswith(("https://", "http://")):
        return {"ok": False, "error": "Feed URL must start with https://"}
    if config.get("inbox_scope") == "project" and not config.get("inbox_project"):
        return {"ok": False, "error": "Pick an inbox project when the scope is project."}
    return {"ok": True}


def pull(instance: dict, config: dict, since: str | None, limit: int) -> dict:
    n = max(1, min(int(config.get("max_items") or 20), limit))
    return {"items": parse_feed(_fetch(str(config["feed_url"])), n)}


app = create_app(
    secret=os.environ.get("FEED_PLUGIN_SECRET", ""), manifest=MANIFEST, pull=pull, validate=validate
)
