"""Browser test for the document review surface (inline annotations + tracked changes).

Run after starting the app:
    python -m streamlit run app.py --server.headless true --server.port 8501
    python tests/smoke_review_ui.py
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
SHOT_DIR = BASE_DIR / "data" / "smoke"


def _wait(page, seconds: float = 2.5) -> None:
    page.wait_for_timeout(int(seconds * 1000))


def _component(page):
    frames = [frame for frame in page.frames if "component" in frame.url and "annotator" in frame.url]
    return frames[0] if frames else None


def _open_demo_contract(page, failures: list[str]) -> None:
    try:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
        _wait(page, 4)
        dialog = page.locator("[role=dialog]")
        if dialog.count() and dialog.first.is_visible():
            page.keyboard.press("Escape")
            _wait(page, 0.5)
        page.locator("summary", has_text="New contract").first.click(timeout=15_000)
        _wait(page, 0.8)
        page.get_by_role("button", name="Load demo contract").click(timeout=15_000)
        _wait(page, 3.5)
        print("[OK] fresh DRAFT contract created")
    except Exception as exc:  # pragma: no cover - diagnostic path
        failures.append(f"demo contract: {str(exc)[:160]}")


def _select_text(frame, section_index: int, start: int, end: int) -> None:
    frame.evaluate(
        """([index, start, end]) => {
            const el = [...document.querySelectorAll('.text')][index];
            if (!el) throw new Error('section not found');
            const node = el.firstChild;
            const range = document.createRange();
            range.setStart(node, start);
            range.setEnd(node, Math.min(end, node.nodeValue.length));
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            document.querySelector('.paper').dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        }""",
        [section_index, start, end],
    )


def main() -> int:
    executables = _browser_candidates()
    if not executables:
        print("No Chrome/Edge executable found")
        return 1

    failures: list[str] = []
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(executables[0]))
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        console_errors: list[str] = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        _open_demo_contract(page, failures)
        page.get_by_role("link", name="Document Review").click(timeout=15_000)
        _wait(page, 4.5)
        frame = _component(page)
        if frame is None:
            failures.append("annotator component did not load")
            print("SMOKE REVIEW FAILED:", failures)
            browser.close()
            return 1

        try:
            body = page.locator("body").inner_text()
            for marker in ("Traceback (most recent call last)", "Something went wrong"):
                if marker in body:
                    failures.append(f"review page: {marker}")
            assert frame.locator(".pill.status").inner_text().strip() == "Draft"
            print("[OK] document rendered for a DRAFT contract")
        except Exception as exc:
            failures.append(f"render: {str(exc)[:160]}")

        # 1. select text and add an inline annotation
        try:
            _select_text(frame, 0, 0, 26)
            _wait(page, 0.8)
            assert frame.evaluate("document.querySelector('.selbar').classList.contains('open')")
            frame.locator(".selbar textarea").fill("Please make this obligation mutual.")
            frame.get_by_role("button", name="Add comment").click()
            _wait(page, 4)
            frame = _component(page)
            assert frame.evaluate("document.querySelectorAll('.ann:not(.rev)').length") >= 1
            assert frame.evaluate("document.querySelectorAll('.card').length") >= 1
            print("[OK] inline annotation anchored to the selected text")
        except Exception as exc:
            failures.append(f"annotate: {str(exc)[:160]}")

        # 2. open the thread and reply
        try:
            frame.locator(".ann:not(.rev)").first.click()
            _wait(page, 0.6)
            frame.locator(".popover textarea").fill("Agreed, sending the redline today.")
            frame.get_by_role("button", name="Reply").click()
            _wait(page, 4)
            frame = _component(page)
            assert frame.evaluate("document.querySelectorAll('.popover .p-reply').length") >= 1
            print("[OK] threaded reply visible in the annotation popover")
        except Exception as exc:
            failures.append(f"reply: {str(exc)[:160]}")

        # 3. propose a tracked change on the payment clause
        try:
            _select_text(frame, 1, 0, 23)
            _wait(page, 0.8)
            frame.get_by_role("button", name="Suggest edit").click()
            _wait(page, 0.4)
            frame.locator(".selbar textarea").fill("Payment within 45 days of invoice.")
            frame.get_by_role("button", name="Submit change").click()
            _wait(page, 4.5)
            frame = _component(page)
            assert frame.evaluate("document.querySelectorAll('.change').length") >= 1
            assert frame.evaluate("document.querySelectorAll('.change .ins').length") >= 1
            assert frame.evaluate("document.querySelectorAll('.change .del').length") >= 1
            head = frame.locator(".change .chg-head").first.inner_text()
            assert "Tracked change" in head and "Admin" in head
            print("[OK] tracked change rendered with insert/delete marks and author")
        except Exception as exc:
            failures.append(f"tracked change: {str(exc)[:160]}")

        # 4. accept and merge, then check the revision mark in the body
        try:
            frame.get_by_role("button", name="Accept").first.click()
            _wait(page, 4)
            frame = _component(page)
            frame.locator(".change .btn.primary", has_text="Merge into document").first.click()
            _wait(page, 4.5)
            frame = _component(page)
            marks = frame.evaluate("document.querySelectorAll('.ann.rev').length")
            assert marks >= 1, "merged revision not marked in the document"
            title = frame.evaluate("(document.querySelector('.ann.rev') || {}).title || ''")
            assert "Revised by" in title, title
            text = frame.evaluate("[...document.querySelectorAll('.text')].map(e => e.innerText)[1]")
            assert "45 days" in text
            page.screenshot(path=str(SHOT_DIR / "review-merged.png"), full_page=False)
            print("[OK] merged revision highlighted with author/time in the document")
        except Exception as exc:
            failures.append(f"merge: {str(exc)[:160]}")

        # 5. printable exports are offered on the page
        try:
            body = page.locator("body").inner_text()
            assert "Download Markdown" in body and "Download printable HTML" in body
            print("[OK] printable exports available")
        except Exception as exc:
            failures.append(f"exports: {str(exc)[:160]}")

        if console_errors:
            failures.append(f"console errors: {console_errors[:2]}")
        page.screenshot(path=str(SHOT_DIR / "review-final.png"), full_page=False)
        browser.close()

    if failures:
        print("\nSMOKE REVIEW FAILED:")
        for item in failures:
            print(" -", item)
        return 1
    print("\nSMOKE REVIEW PASSED: inline annotations, replies, tracked changes and revision marks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
