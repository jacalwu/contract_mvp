"""Full UI lifecycle acceptance test against a running Streamlit app.

Creates a fresh demo contract through the UI, then drives the whole workflow:
review -> change request -> merge -> approval -> lock -> PDF -> eSignature -> executed.

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


def main() -> int:
    executables = _browser_candidates()
    if not executables:
        print("No Chrome/Edge executable found")
        return 1
    failures: list[str] = []

    def wait(page, seconds: float = 2.5) -> None:
        page.wait_for_timeout(int(seconds * 1000))

    def body(page) -> str:
        return page.locator("body").inner_text()

    def nav(page, name: str) -> None:
        page.get_by_role("link", name=name).click(timeout=15_000)
        wait(page)

    def open_audit_tab(page, tab_name: str) -> None:
        nav(page, "Contract Audit History")
        page.get_by_role("tab", name=tab_name).click(timeout=15_000)
        wait(page)

    def click(page, name: str, exact: bool = False) -> bool:
        for _attempt in range(2):
            for locator in page.get_by_role("button", name=name, exact=exact).all():
                try:
                    if locator.is_enabled():
                        locator.click(timeout=10_000)
                        wait(page)
                        return True
                except Exception:
                    continue
            wait(page, 1.5)
        return False

    def step(page, label: str, name: str, exact: bool = False, seconds: float = 2.5) -> None:
        if not click(page, name, exact):
            snippet = body(page)[-350:].replace("\n", " | ")
            failures.append(f"{label}: button '{name}' not found/enabled; tail={snippet}")
            return
        wait(page, seconds)
        print(f"[OK] {label}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(executables[0]))
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
        wait(page, 4)

        # fresh contract
        page.get_by_text("New contract", exact=False).first.click(timeout=10_000)
        wait(page, 0.6)
        page.get_by_role("button", name="Load demo contract").click(timeout=10_000)
        wait(page, 3)
        print("[OK] created fresh demo contract")

        step(page, "submit for review", "Submit for review")
        if "IN_REVIEW" not in body(page):
            failures.append("status did not become IN_REVIEW")

        # Contract Viewer / Change Requests / Approvals are now tabs under
        # "Contract Audit History" rather than top-level sidebar links.
        open_audit_tab(page, "✏️ Change Requests")
        # default operation is INSERT: append a new sentence to the current clause.
        # Streamlit text_area defers input until Ctrl+Enter / blur.
        textarea = page.locator("textarea").first
        textarea.fill("Late payment incurs 2% interest.")
        textarea.press("Control+Enter")
        wait(page, 2)
        if "Preview" not in body(page):
            textarea.press("Tab")
            wait(page, 2)
        step(page, "propose change", "Submit change request", seconds=3)
        if "PENDING" not in body(page):
            failures.append("change request was not created")
        step(page, "accept change", "Accept", exact=True)
        step(page, "merge change", "Merge into text", seconds=3)
        if "MERGED" not in body(page):
            failures.append("change was not merged")

        nav(page, "Dashboard")
        step(page, "submit for approval", "Submit for approval")
        if "PENDING_APPROVAL" not in body(page):
            failures.append("status did not become PENDING_APPROVAL")

        open_audit_tab(page, "✅ Approvals")
        for index in range(3):
            if not click(page, "Approve", exact=True):
                failures.append(f"approval #{index + 1} button not available")
                break
            wait(page, 2)
        if "APPROVED" not in body(page) and "Approved" not in body(page):
            failures.append("approvals did not complete")

        nav(page, "Dashboard")
        step(page, "lock contract", "Lock contract")
        if "LOCKED" not in body(page) and "Locked" not in body(page):
            failures.append("contract was not locked")

        nav(page, "PDF & Signature")
        step(page, "generate PDF", "Generate final PDF", seconds=6)
        if "SHA256" not in body(page):
            failures.append("PDF was not generated")
        step(page, "send for signature", "Send for signature", seconds=3)
        if "SIGNING" not in body(page):
            failures.append("signature request was not sent")
        for index in range(8):
            if "Executed · EXECUTED" in body(page):
                break
            if not click(page, "Simulate signature"):
                break
            wait(page, 2)
        if "Executed · EXECUTED" not in body(page):
            failures.append("contract did not reach EXECUTED")

        nav(page, "Timeline")
        text = body(page)
        if "PDF_GENERATED" not in text or "CONTRACT_EXECUTED" not in text:
            failures.append("audit timeline is missing key events")
        page.screenshot(path=str(BASE_DIR / "data" / "smoke" / "executed-timeline.png"))
        print("[OK] timeline audited")
        browser.close()

    if failures:
        print("\nE2E UI FAILED:")
        for failure in failures:
            print(" -", failure)
        return 1
    print("\nE2E UI PASSED: full workflow through the browser")
    return 0


if __name__ == "__main__":
    sys.exit(main())
