# Browser end-to-end checks (opt-in)

These drive a **running** hub with a real Chromium. They are not collected by `pytest` (no `test_` prefix, they need a
live server and Playwright), but they exist because the in-process tests cannot see what a browser does: once, every
form returned 403 in a real browser while all the unit tests passed, because the tests post the CSRF token from the page's
meta tag and a browser submits the form's own hidden field.

```bash
pip install playwright && playwright install chromium     # or use an existing Chromium via PLAYWRIGHT_BROWSERS_PATH
# (point ACM_E2E_CHROMIUM at a Chromium binary if Playwright can't find its own)
# start a throwaway hub (see docs/DEPLOYMENT.md), create an admin, then:
ACM_E2E_URL=http://127.0.0.1:8000 ACM_E2E_EMAIL=you@example.com ACM_E2E_PASSWORD=... python tests/e2e/browser_smoke.py
ACM_E2E_URL=... ACM_E2E_EMAIL=... ACM_E2E_PASSWORD=... python tests/e2e/oauth_flow.py   # needs MEMORY_HUB_OAUTH_ENABLED=true on the hub
ACM_E2E_URL=... ACM_E2E_EMAIL=... ACM_E2E_PASSWORD=... python tests/e2e/encrypted_memory.py   # optional ACM_E2E_SHOTS=<dir> saves screenshots
```

- `browser_smoke.py` signs in and submits the main forms (record, project, user, token mint/revoke, theme), failing on any 403.
- `oauth_flow.py` plays an OAuth client on a loopback port: registers, sends the browser through sign-in and the consent
  screen, follows the approval redirect (which proves the page's CSP lets it through), exchanges the code, calls MCP,
  rotates the refresh token, and checks that replaying the old one ends the grant. Use a throwaway hub.
- `encrypted_memory.py` enables encrypted private memory from Settings, checks the recovery key appears once, that locking,
  a wrong passphrase and the right one behave as described, and fails on any 403/422/500. Use a throwaway hub.
- `connections.py` adds a Memos connection through Settings → Connections, checks the token never appears in any page, that
  Test connection reports a result, that a loopback address is refused, and that Remove destroys the saved token. Use a throwaway hub.
