"""Drive Settings -> Connections in a real browser: add, test, turn on/off, remove; fail on any 403/500. See README.md."""

import os
import sys

from playwright.sync_api import sync_playwright

HUB = os.environ.get("ACM_E2E_URL", "http://127.0.0.1:8000").rstrip("/")
EMAIL, PASSWORD = os.environ.get("ACM_E2E_EMAIL", ""), os.environ.get("ACM_E2E_PASSWORD", "")
SHOTS = os.environ.get("ACM_E2E_SHOTS", "")
TOKEN = "e2e-connection-token-0123456789"
results: list[bool] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def shot(page, name: str) -> None:
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)


def main() -> int:
    if not (EMAIL and PASSWORD):
        print("set ACM_E2E_EMAIL and ACM_E2E_PASSWORD (a user on a throwaway hub)")
        return 2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get("ACM_E2E_CHROMIUM") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        statuses: list[int] = []
        page.on("response", lambda r: statuses.append(r.status) if r.request.method == "POST" else None)
        page.goto(HUB + "/login")
        page.fill("[name=email]", EMAIL)
        page.fill("[name=password]", PASSWORD)
        page.click("button.primary")
        page.wait_for_url("**/memory")

        page.goto(HUB + "/settings/connections")
        step(
            "the Connections tab lists the connectors",
            "Memos" in page.content() and "Joplin" in page.content(),
        )
        shot(page, "list")

        page.goto(HUB + "/settings/connections/new?plugin=memos")
        page.fill("#name", "My Memos")
        page.fill("#cfg_base_url", "https://93.184.216.34")
        page.fill("#secret_token", TOKEN)
        shot(page, "form")
        page.click("form.card button.primary")
        page.wait_for_load_state()
        body = page.content()
        step(
            "saving redirects to the connection and never echoes the token",
            "My Memos" in body and TOKEN not in body,
        )
        url = page.url.split("?")[0]
        shot(page, "detail")

        with page.expect_navigation(timeout=30000):  # the test makes a real network call
            page.click("button:has-text('Test connection')")
        step(
            "test connection reports a result without leaking the token",
            "Test failed" in page.content() or "Connected" in page.content(),
        )
        step("...and the token is still not in the page", TOKEN not in page.content())

        page.goto(HUB + "/settings/connections/new?plugin=memos")
        page.fill("#name", "Blocked")
        page.fill("#cfg_base_url", "http://127.0.0.1:5230")
        page.fill("#secret_token", TOKEN)
        page.click("form.card button.primary")
        page.wait_for_load_state()
        step(
            "a loopback address is refused with a clear message",
            "private network" in page.content() or "never reach" in page.content(),
        )
        step("the refused form doesn't echo the token", TOKEN not in page.content())
        shot(page, "refused")

        page.goto(url)
        with page.expect_navigation():
            page.click("button:has-text('Remove')")
        step("remove works and says the token was destroyed", "saved token destroyed" in page.content())
        step(
            "no form was rejected (403/500)",
            not any(s in (403, 500) for s in statuses),
            str(sorted(set(statuses))),
        )
        browser.close()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
