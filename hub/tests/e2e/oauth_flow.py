"""Play an OAuth client against a live hub with a real browser. See tests/e2e/README.md. Use a throwaway hub."""

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from playwright.sync_api import sync_playwright

HUB = os.environ.get("ACM_E2E_URL", "http://127.0.0.1:8000").rstrip("/")
EMAIL, PASSWORD = os.environ.get("ACM_E2E_EMAIL", ""), os.environ.get("ACM_E2E_PASSWORD", "")
PORT = int(os.environ.get("ACM_E2E_CALLBACK_PORT", "53999"))
REDIRECT = f"http://127.0.0.1:{PORT}/callback"
got: dict[str, dict[str, str]] = {}


class Callback(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/callback":  # ignore the browser's /favicon.ico probe
            got["query"] = dict(urllib.parse.parse_qsl(parts.query))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"callback received")

    def log_message(self, *a) -> None:  # noqa: ANN002
        pass


def post(path: str, *, data: dict | None = None, js: dict | None = None, headers: dict | None = None):  # noqa: ANN201
    body = json.dumps(js).encode() if js is not None else urllib.parse.urlencode(data or {}).encode()
    kind = "application/json" if js is not None else "application/x-www-form-urlencoded"
    req = urllib.request.Request(HUB + path, body, {"Content-Type": kind, **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name: str, ok: bool) -> None:
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        sys.exit(1)


def main() -> int:
    if not (EMAIL and PASSWORD):
        print("set ACM_E2E_EMAIL and ACM_E2E_PASSWORD (a user on a throwaway hub with OAuth switched on)")
        return 2
    server = HTTPServer(("127.0.0.1", PORT), Callback)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    status, reg = post(
        "/register",
        js={"redirect_uris": [REDIRECT], "client_name": "E2E client", "token_endpoint_auth_method": "none"},
    )
    check("register a public client (no secret issued)", status == 201 and not reg.get("client_secret"))
    cid = reg["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    auth = (
        HUB
        + "/authorize?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "e2e-state",
                "scope": "memory:read offline_access",
                "resource": HUB + "/mcp",
            }
        )
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get("ACM_E2E_CHROMIUM") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        page.goto(auth)
        check("signed-out browser is sent to sign in", "/login" in page.url)
        page.fill("[name=email]", EMAIL)
        page.fill("[name=password]", PASSWORD)
        page.click("button.primary")
        page.wait_for_url("**/oauth/consent**")
        page.check("input[value=all]")  # the consent screen's "all my projects" choice
        page.click("button[value=approve]")
        for _ in range(60):
            if got:
                break
            page.wait_for_timeout(100)
        browser.close()
    check("approval redirect reached the client (CSP allowed it)", "code" in got.get("query", {}))
    q = got["query"]
    check("state and issuer came back", q.get("state") == "e2e-state" and q.get("iss") == HUB)

    status, tok = post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": cid,
            "code": q["code"],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    check("exchange the code", status == 200 and tok.get("refresh_token"))
    hdr = {"Authorization": "Bearer " + tok["access_token"], "Accept": "application/json, text/event-stream"}
    status, _ = post(
        "/mcp", js={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers=hdr
    )
    check("the token works on /mcp", status == 200)
    status, new = post(
        "/token",
        data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": tok["refresh_token"]},
    )
    check("refresh rotates the refresh token", status == 200 and new["refresh_token"] != tok["refresh_token"])
    status, _ = post(
        "/mcp", js={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=hdr
    )
    check("the superseded access token is dead", status == 401)
    status, again = post(
        "/token",
        data={"grant_type": "refresh_token", "client_id": cid, "refresh_token": tok["refresh_token"]},
    )
    check(
        "replaying the old refresh token is refused", status == 400 and again.get("error") == "invalid_grant"
    )
    status, _ = post(
        "/mcp",
        js={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        headers={
            "Authorization": "Bearer " + new["access_token"],
            "Accept": "application/json, text/event-stream",
        },
    )
    check("...and that ended the whole grant", status == 401)
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
