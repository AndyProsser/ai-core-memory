"""Drive Settings -> Encrypted private memory in a real browser, in light and dark. See tests/e2e/README.md."""

import os
import re
import sys

from playwright.sync_api import sync_playwright

HUB = os.environ.get("ACM_E2E_URL", "http://127.0.0.1:8000").rstrip("/")
EMAIL, PASSWORD = os.environ.get("ACM_E2E_EMAIL", ""), os.environ.get("ACM_E2E_PASSWORD", "")
SHOTS = os.environ.get("ACM_E2E_SHOTS", "")
PASS = "an e2e memory passphrase"
BODY = "e2e-private-body-text"
results: list[bool] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def shot(page, name: str) -> None:
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)


def main() -> int:
    if not (EMAIL and PASSWORD):
        print("set ACM_E2E_EMAIL and ACM_E2E_PASSWORD (an admin on a throwaway hub)")
        return 2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get("ACM_E2E_CHROMIUM") or None, args=["--no-sandbox"]
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        statuses: list[int] = []
        page.on("response", lambda r: statuses.append(r.status) if r.request.method == "POST" else None)
        page.goto(HUB + "/login")
        page.fill("[name=email]", EMAIL)
        page.fill("[name=password]", PASSWORD)
        page.click("button.primary")
        page.wait_for_url("**/memory")

        page.goto(HUB + "/memory/new")
        page.fill("#name", "e2e-private")
        page.fill("#description", "A private note")
        page.fill("#body", BODY)
        page.select_option("#scope", "user")
        page.click("form:has(#name) button.primary")
        page.wait_for_load_state()
        rec_url = page.url
        step("create a private record", BODY in page.content())

        page.goto(HUB + "/settings")
        form = "form[action='/settings/memory-key/enable']"
        page.fill(f"{form} input[name=passphrase]", PASS)
        page.fill(f"{form} input[name=confirm]", PASS)
        page.click(f"{form} button.primary, {form} button[type=submit]")
        page.wait_for_load_state()
        text = page.inner_text("body")
        key = re.search(r"[A-Z2-7]{8}(?:-[A-Z2-7]{8}){5,}", text)
        step("enabling shows the recovery key once", bool(key) and 403 not in statuses, str(statuses[-1:]))
        shot(page, "recovery")
        page.goto(HUB + "/settings")
        step("the key is gone on reload", not (key and key.group(0) in page.content()))
        for theme in ("light", "dark"):
            page.emulate_media(color_scheme=theme)
            page.evaluate(f"document.documentElement.dataset.pref = '{theme}'")
            shot(page, f"settings-{theme}")

        page.goto(rec_url)
        step("this session reads the body", BODY in page.content())

        page.goto(HUB + "/settings")
        page.click("form[action='/settings/memory-key/lock'] button")
        page.wait_for_load_state()
        page.goto(rec_url)
        locked = BODY not in page.content() and "lock" in page.content().lower()
        step("locking hides the body", locked)
        shot(page, "locked")

        page.goto(HUB + "/settings")
        uform = "form[action='/settings/memory-key/unlock']"
        page.fill(f"{uform} input[name=passphrase]", "definitely wrong passphrase")
        page.click(f"{uform} button")
        page.wait_for_load_state()
        page.goto(rec_url)
        step("a wrong passphrase doesn't unlock", BODY not in page.content())
        page.goto(HUB + "/settings")
        page.fill(f"{uform} input[name=passphrase]", PASS)
        page.click(f"{uform} button")
        page.wait_for_load_state()
        page.goto(rec_url)
        step("the right passphrase does", BODY in page.content())
        step(
            "no form was rejected",
            not any(s in (403, 422, 500) for s in statuses),
            str(sorted(set(statuses))),
        )
        browser.close()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
