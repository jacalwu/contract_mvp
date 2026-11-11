"""Final contract PDF: Jinja2 HTML rendered through WeasyPrint / Playwright / Chrome headless.

A dependency-free minimal PDF writer is the last fallback so the MVP always produces
a downloadable, hashable PDF on any machine.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

import config
from core.audit import log_event
from core.constants import (
    E_PDF_GENERATED,
    LOCKED,
    ROLE_ADMIN,
    Actor,
    DomainError,
    new_id,
    now,
)
from core.contracts import list_sections, list_stakeholders
from core.workflow import get_contract, require_role, require_status
from db.database import sql, tx

_ENV = Environment(
    loader=FileSystemLoader(str(config.TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)


def render_contract_html(contract: dict, sections: list[dict], stakeholders: list[dict], generated_at: datetime) -> str:
    template = _ENV.get_template("contract.html")
    return template.render(
        contract=contract,
        sections=sections,
        stakeholders=stakeholders,
        generated_at=generated_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _browser_candidates() -> list[Path]:
    candidates = [
        os.getenv("MVP_CHROME_PATH", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    return [Path(item) for item in candidates if item and Path(item).exists()]


def _engine_weasyprint(html_text: str, out: Path, html_path: Path, plain_lines: list[str]) -> None:
    from weasyprint import HTML

    HTML(string=html_text, base_url=str(config.BASE_DIR)).write_pdf(str(out))


def _engine_playwright(html_text: str, out: Path, html_path: Path, plain_lines: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    launches: list[dict] = []
    for exe in _browser_candidates():
        launches.append({"executable_path": str(exe)})
    launches.extend([{"channel": "chrome"}, {"channel": "msedge"}, {}])
    last_error: Exception | None = None
    with sync_playwright() as playwright:
        for kwargs in launches:
            try:
                browser = playwright.chromium.launch(headless=True, **kwargs)
                break
            except Exception as exc:  # try the next available browser
                last_error = exc
        else:
            raise RuntimeError(f"no usable Chromium browser: {last_error}")
        try:
            page = browser.new_page()
            page.set_content(html_text, wait_until="load", timeout=30_000)
            page.pdf(
                path=str(out),
                format="A4",
                print_background=True,
                margin={"top": "18mm", "bottom": "18mm", "left": "18mm", "right": "18mm"},
            )
        finally:
            browser.close()


def _engine_headless(html_text: str, out: Path, html_path: Path, plain_lines: list[str]) -> None:
    executables = _browser_candidates()
    if not executables:
        raise RuntimeError("no Chrome/Edge executable found")
    exe = str(executables[0])
    last_error: Exception | None = None
    for headless_flag in ("--headless=new", "--headless"):
        try:
            subprocess.run(
                [
                    exe,
                    headless_flag,
                    "--disable-gpu",
                    "--no-sandbox",
                    "--no-first-run",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={out}",
                    "--virtual-time-budget=4000",
                    html_path.as_uri(),
                ],
                capture_output=True,
                timeout=90,
                check=True,
            )
            if out.exists() and out.stat().st_size > 0:
                return
            last_error = RuntimeError("browser produced no PDF")
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"headless print failed: {last_error}")


def _pdf_escape(text: str) -> str:
    latin = text.encode("latin-1", "replace").decode("latin-1")
    return latin.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _engine_minimal(html_text: str, out: Path, html_path: Path, plain_lines: list[str]) -> None:
    """Pure-python PDF 1.4 writer (Helvetica, Latin-1 only). Emergency fallback."""
    wrapped: list[str] = []
    for line in plain_lines:
        text = line.rstrip()
        if not text:
            wrapped.append("")
            continue
        while len(text) > 95:
            wrapped.append(text[:95])
            text = text[95:]
        wrapped.append(text)

    page_size = 42
    pages = [wrapped[i : i + page_size] for i in range(0, len(wrapped), page_size)] or [[""]]
    objects: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for index, page_lines in enumerate(pages):
        stream = "BT\n/F1 10 Tf\n13 TL\n50 780 Td\n"
        for line in page_lines:
            stream += f"({_pdf_escape(line)}) Tj\nT*\n"
        stream += "ET\n"
        stream_bytes = stream.encode("latin-1", "replace")
        page_id = 4 + 2 * index
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>"
            ).encode("ascii")
        )
        objects.append(f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii") + stream_bytes + b"\nendstream")

    chunks: list[bytes] = [b"%PDF-1.4\n"]
    offsets: list[int] = []
    current = len(chunks[0])
    for number, body in enumerate(objects, start=1):
        offsets.append(current)
        chunk = f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        chunks.append(chunk)
        current += len(chunk)
    xref_start = current
    xref = f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    for offset in offsets:
        xref += f"{offset:010d} 00000 n \n".encode("ascii")
    xref += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n"
    ).encode("ascii")
    out.write_bytes(b"".join(chunks) + xref)


_ENGINES = {
    "weasyprint": _engine_weasyprint,
    "playwright": _engine_playwright,
    "headless": _engine_headless,
    "minimal": _engine_minimal,
}
_AUTO_ORDER = ("weasyprint", "playwright", "headless", "minimal")


def _plain_lines(contract: dict, sections: list[dict], stakeholders: list[dict], generated_at: datetime) -> list[str]:
    lines = [
        contract["title"],
        f"Contract ID: {contract['contract_id']}    Version: v{contract['version']}",
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "PARTIES",
    ]
    lines += [f"- {item['name']} <{item['email']}> ({item['role']})" for item in stakeholders]
    lines += ["", "TERMS", ""]
    for index, section in enumerate(sections, start=1):
        lines.append(f"{index}. {section['title']}")
        lines.extend((section["content"] or "(deleted)").splitlines())
        lines.append("")
    lines += ["SIGNATURES", ""]
    for item in stakeholders:
        lines.append(f"{item['name']}: ____________________________    Date: ____________")
    return lines


def _validate_pdf(path: Path) -> None:
    if not path.exists() or path.stat().st_size < 100:
        raise RuntimeError("engine produced an empty file")
    if not path.read_bytes()[:5].startswith(b"%PDF"):
        raise RuntimeError("engine output is not a PDF")


def generate_pdf(contract_id: str, actor: Actor, engine: str | None = None) -> dict:
    """Requires LOCKED. Produces contract_v{version}.pdf, stores sha256 and an audit event."""
    require_role(actor, {ROLE_ADMIN}, "Generate PDF")
    contract = get_contract(contract_id)
    require_status(contract, {LOCKED}, "Generate PDF")
    sections = list_sections(contract_id)
    stakeholders = list_stakeholders(contract_id)
    if not sections:
        raise DomainError("The contract has no sections to render into a PDF.")

    config.init_dirs()
    target_dir = config.PDF_DIR / contract_id
    target_dir.mkdir(parents=True, exist_ok=True)
    version = int(contract["version"])
    sequence = 1
    stem = f"contract_v{version}"
    while (target_dir / f"{stem}.pdf").exists():
        sequence += 1
        stem = f"contract_v{version}_{sequence}"

    html_path = target_dir / f"{stem}.html"
    pdf_path = target_dir / f"{stem}.pdf"
    generated_at = now()
    html_text = render_contract_html(contract, sections, stakeholders, generated_at)
    html_path.write_text(html_text, encoding="utf-8")
    plain_lines = _plain_lines(contract, sections, stakeholders, generated_at)

    requested = (engine or config.PDF_ENGINE or "auto").lower()
    order = _AUTO_ORDER if requested == "auto" else (requested,)
    if any(name not in _ENGINES for name in order):
        raise DomainError(f"Unknown PDF engine: {requested}")

    used_engine, errors = "", []
    for name in order:
        try:
            pdf_path.unlink(missing_ok=True)
            _ENGINES[name](html_text, pdf_path, html_path, plain_lines)
            _validate_pdf(pdf_path)
            used_engine = name
            break
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    if not used_engine:
        pdf_path.unlink(missing_ok=True)
        raise DomainError("PDF generation failed: " + " | ".join(errors))

    pdf_bytes = pdf_path.read_bytes()
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    try:
        relative_path = str(pdf_path.relative_to(config.BASE_DIR)).replace("\\", "/")
    except ValueError:
        # custom MVP_PDF_DIR may live outside the project root
        relative_path = str(pdf_path).replace("\\", "/")
    pdf_id = new_id("PDF")
    with tx() as con:
        sql(
            con,
            """INSERT INTO generated_pdfs
               (pdf_id, contract_id, version, file_path, sha256_hash, engine, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [pdf_id, contract_id, version, relative_path, sha256, used_engine, generated_at],
        )
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_PDF_GENERATED,
            target_id=pdf_id,
            after_value={
                "file": pdf_path.name,
                "version": version,
                "sha256": sha256,
                "engine": used_engine,
            },
            at=generated_at,
        )
    return {
        "pdf_id": pdf_id,
        "path": pdf_path,
        "relative_path": relative_path,
        "filename": pdf_path.name,
        "version": version,
        "sha256": sha256,
        "engine": used_engine,
        "size": len(pdf_bytes),
        "html_path": html_path,
    }


def pdf_absolute_path(relative_path: str) -> Path:
    path = Path(relative_path)
    return path if path.is_absolute() else config.BASE_DIR / path
