"""Submit the hub's main forms from a real browser and fail on any 403. See tests/e2e/README.md."""

import os
import sys

from playwright.sync_api import sync_playwright

HUB = os.environ.get("ACM_E2E_URL", "http://127.0.0.1:8000").rstrip("/")
EMAIL, PASSWORD = os.environ.get("ACM_E2E_EMAIL", ""), os.environ.get("ACM_E2E_PASSWORD", "")
PROJECT = os.environ.get("ACM_E2E_PROJECT", "e2e-project")
results: list[tuple[str, bool]] = []


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def main() -> int:
    if not (EMAIL and PASSWORD):
        print("set ACM_E2E_EMAIL and ACM_E2E_PASSWORD (an admin on a throwaway hub)")
        return 2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get("ACM_E2E_CHROMIUM") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        posts: list[tuple[str, int]] = []
        page.on(
            "response",
            lambda r: (
                posts.append((r.url.replace(HUB, ""), r.status)) if r.request.method == "POST" else None
            ),
        )
        page.goto(HUB + "/login")
        page.fill("[name=email]", EMAIL)
        page.fill("[name=password]", PASSWORD)
        page.click("button.primary")
        page.wait_for_url("**/memory")
        step("sign in", True)

        page.goto(HUB + "/memory/new")
        page.fill("#name", "e2e-record")
        page.fill("#description", "Created by a real form post")
        page.fill("#body", "hello from chromium")
        page.select_option("#scope", "user")
        page.click("form:has(#name) button.primary")
        page.wait_for_load_state()
        step("create a record", "e2e-record" in page.content(), str(posts[-1]))

        page.goto(HUB + "/settings/projects")
        page.fill("input[name=slug]", PROJECT)
        page.click("button:has-text('Create project')")
        page.wait_for_load_state()
        step("create a project", PROJECT in page.content(), str(posts[-1]))

        page.goto(HUB + "/settings")
        form = "form[action='/settings/tokens']"
        page.fill(f"{form} input[name=label]", "e2e")
        page.fill(f"{form} input[name=password]", PASSWORD)
        page.click(f"{form} button[type=submit]")
        page.wait_for_load_state()
        step("mint an API token", "Copy your token now" in page.content(), str(posts[-1]))
        page.goto(HUB + "/settings")
        page.click("button:has-text('Revoke')")
        page.wait_for_load_state()
        step("revoke it", "revoked" in page.content().lower(), str(posts[-1]))

        step(
            "no 403 in the whole session",
            not [p for p in posts if p[1] == 403],
            str([p for p in posts if p[1] == 403]),
        )
        browser.close()
    print("\nALL PASSED" if all(ok for _, ok in results) else "\nSOME FAILED")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
