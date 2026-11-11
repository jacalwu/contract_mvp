"""Browser smoke test against a running Streamlit app (default http://127.0.0.1:8501).

Creates a demo contract through the UI, visits every page via the sidebar navigation,
posts a comment, and fails on any Streamlit traceback / error banner.

Run after: python -m streamlit run app.py --server.headless true --server.port 8501
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from playwright.sync_api import sync_playwright  # noqa: E402

from core.pdf import _browser_candidates  # noqa: E402

BASE_URL = os.getenv("MVP_SMOKE_URL", "http://127.0.0.1:8501").rstrip("/")
PAGES = [
    ("Dashboard", "dashboard"),
    ("Contract Viewer", "viewer"),
    ("Change Requests", "changes"),
    ("Approvals", "approvals"),
    ("Timeline", "timeline"),
    ("PDF & Signature", "pdf-signature"),
]
SHOT_DIR = BASE_DIR / "data" / "smoke"
ERROR_MARKERS = ("Traceback (most recent call last)", "Something went wrong", "ModuleNotFoundError")


def _wait(page, seconds: float = 3.0) -> None:
    page.wait_for_timeout(int(seconds * 1000))


def _check(page, name: str, failures: list[str]) -> str:
    body = page.locator("body").inner_text()
    for marker in ERROR_MARKERS:
        if marker in body:
            failures.append(f"{name}: {marker}")
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOT_DIR / f"{name}.png"), full_page=False)
    print(f"[{'FAIL' if any(f.startswith(name + ':') for f in failures) else 'OK'}] {name}: {len(body)} chars")
    return body


def _navigate(page, link_name: str) -> None:
    page.get_by_role("link", name=link_name).click(timeout=15_000)
    _wait(page, 2.5)


def main() -> int:
    executables = _browser_candidates()
    if not executables:
        print("No Chrome/Edge executable found for browser smoke test")
        return 1
    failures: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(executables[0]))
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        console_errors: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
        _wait(page, 4)
        _check(page, "dashboard", failures)
        # always start from a fresh DRAFT contract so edit widgets are guaranteed visible
        try:
            page.get_by_text("New contract", exact=False).first.click(timeout=10_000)
            _wait(page, 0.8)
            page.get_by_role("button", name="Load demo contract").click(timeout=10_000)
            _wait(page, 3)
            print("[OK] fresh DRAFT demo contract created via UI")
        except Exception as exc:
            failures.append(f"demo creation: {str(exc)[:160]}")

        # add an extra reviewer from the Contract Viewer (Admin is the default identity)
        try:
            _navigate(page, "Contract Viewer")
            page.get_by_text("Reviewers & stakeholders", exact=False).first.click(timeout=10_000)
            _wait(page, 1)
            page.get_by_placeholder("Dana Reviewer").first.fill("Dana Reviewer")
            page.get_by_placeholder("dana@company.com").first.fill("dana@company.com")
            page.get_by_role("button", name="Add reviewer").first.click(timeout=10_000)
            _wait(page, 3)
            body = _check(page, "viewer-added-reviewer", failures)
            if "Dana Reviewer" not in body:
                failures.append("added reviewer did not appear")
            else:
                print("[OK] extra reviewer added via Contract Viewer")
        except Exception as exc:
            failures.append(f"add reviewer: {str(exc)[:160]}")

        for link_name, shot_name in PAGES[1:]:
            try:
                _navigate(page, link_name)
            except Exception as exc:
                failures.append(f"{shot_name} navigation: {str(exc)[:160]}")
                continue
            _check(page, shot_name, failures)

        # one real interaction: post a comment on the first section
        try:
            _navigate(page, "Contract Viewer")
            page.get_by_text("Section 1 discussion", exact=False).first.click(timeout=10_000)
            _wait(page, 1)
            page.get_by_placeholder("Write a comment...").first.fill("Smoke test comment")
            page.get_by_role("button", name="Post comment").first.click(timeout=10_000)
            _wait(page, 3)
            # st.rerun collapses the expander, so open it again to verify the stored comment
            page.get_by_text("Section 1 discussion", exact=False).first.click(timeout=10_000)
            _wait(page, 1)
            body = _check(page, "viewer-after-comment", failures)
            if "Smoke test comment" not in body:
                failures.append("comment submission did not persist")
        except Exception as exc:
            failures.append(f"comment interaction: {str(exc)[:160]}")

        fatal_console = [msg for msg in console_errors if "Traceback" in msg or "Uncaught" in msg]
        if fatal_console:
            failures.append(f"browser console: {fatal_console[:2]}")
        browser.close()

    if failures:
        print("\nSMOKE FAILED:")
        for failure in failures:
            print(" -", failure)
        return 1
    print("\nSMOKE PASSED: all 6 pages + creation + comment interaction")
    return 0


if __name__ == "__main__":
    sys.exit(main())
