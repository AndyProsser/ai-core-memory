import re

from acm_hub.app import CSP


def test_favicon_is_served_without_login(hub):
    client, _ = hub
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"
    assert r.content[:4] == b"\x00\x00\x01\x00"  # ICO header


def test_pages_link_the_icons_and_the_csp_allows_them(authed):
    client, _, _ = authed
    html = client.get("/memory").text
    assert 'rel="icon" href="/static/favicon.svg"' in html
    assert 'rel="apple-touch-icon"' in html
    assert "img-src 'self'" in CSP


def test_no_page_relies_on_inline_script_or_javascript_urls(authed):
    # script-src 'self' blocks both; the error page used to ship a javascript: "Go back" link
    client, _, _ = authed
    for path in ("/memory", "/review", "/settings", "/no-such-page"):
        html = client.get(path).text
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), path
        assert "javascript:" not in html, path


def test_animated_logo_respects_reduced_motion(hub):
    client, _ = hub
    svg = client.get("/static/logo-animated.svg").text
    assert "@keyframes" in svg
    assert "prefers-reduced-motion: reduce" in svg
    assert "<script" not in svg


def test_version_is_shown_in_the_app_and_on_public_pages(authed, hub):
    from acm_hub import __version__

    client, _, _ = authed
    assert f"Memory hub v{__version__}" in client.get("/memory").text
    client.cookies.clear()
    assert f"Memory hub v{__version__}" in client.get("/login").text


def test_logo_asset_in_docs_matches_the_app_logo():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert (root / "docs/assets/logo.svg").read_text() == (
        root / "hub/src/acm_hub/web/static/logo.svg"
    ).read_text()
