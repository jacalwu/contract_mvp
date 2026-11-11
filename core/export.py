"""Printable exports of a contract under review.

Two formats are produced from the same snapshot the document view renders:
* Markdown — annotation references and a change-history appendix, easy to diff or paste.
* Standalone HTML — paper-styled, print-optimised, with the reviewer colours preserved.
"""
from __future__ import annotations

import html as html_lib
from pathlib import Path

import config
from core import richtext
from core.constants import Actor
from core.review import build_payload


def _safe(text: str) -> str:
    return html_lib.escape(text or "")


def _number_annotations(annotations: list[dict]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, item in enumerate(annotations, start=1):
        mapping[item["id"]] = index
    return mapping


def build_markdown(contract_id: str, actor: Actor) -> str:
    payload = build_payload(contract_id, actor)
    contract = payload["contract"]
    annotations = payload["annotations"]
    numbers = _number_annotations(annotations)

    lines: list[str] = [
        f"# {contract['title']}",
        "",
        f"**Status:** {contract['status_label']} ({contract['status']}) · **Version:** v{contract['version']}",
        "",
        "**Reviewers:** "
        + ", ".join(f"{person['name']} ({person['role']})" for person in payload["people"]),
        "",
        "---",
        "",
    ]

    for index, section in enumerate(payload["sections"], start=1):
        lines.append(f"## {index}. {section['title']}")
        lines.append("")
        section_annotations = [
            item for item in annotations if item["section_id"] == section["id"] and item["start"] is not None
        ]
        refs = [(item["end"], str(numbers[item["id"]])) for item in section_annotations]
        lines.append(richtext.insert_references(section["markdown"], refs))
        lines.append("")

        section_changes = [
            change for change in payload["changes"] if change["section_id"] == section["id"] and change["status"] != "MERGED"
        ]
        for change in section_changes:
            lines.append(
                f"> **Tracked change · {change['operation']} · {change['status']}** — "
                f"{change['author']} on {change['when']}"
            )
            lines.append(">")
            for op in change["diff"]:
                if op["op"] == "del":
                    lines.append(f"> ~~{op['text']}~~")
                elif op["op"] == "ins":
                    lines.append(f"> **{op['text']}**")
            lines.append("")

    if annotations:
        lines.extend(["---", "", "## Comments", ""])
        for item in annotations:
            status = " (resolved)" if item["resolved"] else ""
            lines.append(f"[^{numbers[item['id']]}]: **{item['author']}** · {item['when']}{status}")
            if item["quote"]:
                lines.append(f"    > {item['quote']}")
            lines.append(f"    {item['text']}")
            for reply in item["replies"]:
                lines.append(f"    - **{reply['author']}** · {reply['when']}: {reply['text']}")
            lines.append("")

    merged = [change for change in payload["changes"] if change["status"] == "MERGED"]
    if merged:
        lines.extend(["---", "", "## Change history", "", "| Section | Change | By | When |", "| --- | --- | --- | --- |"])
        for change in merged:
            lines.append(
                f"| {change['section_title']} | {change['operation']} | {change['author']} | "
                f"{change['merged_at'] or change['when']} |"
            )
        lines.append("")

    return "\n".join(lines)


_STYLE = """
:root { color-scheme: light; }
body { margin:0; background:#eef1f5; font-family: Georgia, "Times New Roman", serif; color:#111827; }
.page { max-width: 820px; margin: 24px auto; background:#fff; padding: 56px 64px;
        box-shadow: 0 10px 30px rgba(15,23,42,.12); }
h1 { font-size: 26px; margin: 0 0 6px; }
.meta { font-family: Arial, Helvetica, sans-serif; font-size: 12px; color:#4b5563; margin-bottom: 18px; }
.meta span { display:inline-block; margin-right:14px; }
h2 { font-size: 16px; margin: 26px 0 8px; }
p.body { font-size: 14px; line-height: 1.9; margin: 0 0 10px; white-space: pre-wrap; }
h3 { font-size: 14px; margin: 18px 0 6px; }
blockquote { margin: 8px 0; padding: 6px 12px; border-left: 3px solid #d1d5db; color:#374151; font-style: italic; }
ul, ol { margin: 6px 0 10px; padding-left: 26px; font-size: 14px; line-height: 1.8; }
table.doc-table { border-collapse: collapse; width: 100%; margin: 8px 0 12px; font-size: 13px; }
table.doc-table th, table.doc-table td { border: 1px solid #d1d5db; padding: 6px 8px; text-align: left; }
table.doc-table th { background: #f3f4f6; }
hr { border: 0; border-top: 1px solid #e5e7eb; margin: 16px 0; }
code { background: #f3f4f6; padding: 1px 4px; border-radius: 3px; font-size: 12px; }
del { color: #6b7280; }
.ann.rev { background: #fef3c7; }
.ann { background: color-mix(in srgb, var(--c) 12%, transparent); border-bottom: 2px solid var(--c);
       padding: 0 1px; border-radius: 2px; }
.ref { color: var(--c); font-family: Arial, Helvetica, sans-serif; font-size: 10px; font-weight: 700; }
.change { font-family: Arial, Helvetica, sans-serif; font-size: 12px; border-left: 3px solid #f59e0b;
          background:#fffbeb; padding: 8px 10px; margin: 8px 0; }
.change .by { color:#92400e; font-weight: 600; }
.ins { background:#dcfce7; color:#166534; }
.del { background:#fee2e2; color:#991b1b; text-decoration: line-through; }
.notes { margin-top: 34px; border-top: 1px solid #e5e7eb; padding-top: 14px;
         font-family: Arial, Helvetica, sans-serif; font-size: 12px; }
.notes h3 { font-size: 13px; margin: 0 0 10px; }
.note { margin-bottom: 10px; padding-left: 10px; border-left: 2px solid var(--c); }
.note .who { font-weight: 700; color: var(--c); }
.note .quote { color:#6b7280; font-style: italic; }
.note .reply { margin-top: 4px; }
@media print {
  body { background:#fff; }
  .page { box-shadow:none; margin:0; padding:0 12mm; max-width:none; }
  .notes { break-before: page; }
}
"""


def build_print_html(contract_id: str, actor: Actor) -> str:
    payload = build_payload(contract_id, actor)
    contract = payload["contract"]
    annotations = payload["annotations"]
    numbers = _number_annotations(annotations)

    body: list[str] = [
        f"<h1>{_safe(contract['title'])}</h1>",
        "<div class=\"meta\">"
        f"<span>{_safe(contract['status_label'])} · v{contract['version']}</span>"
        f"<span>Reviewers: {_safe(', '.join(person['name'] for person in payload['people']))}</span>"
        "</div>",
    ]

    for index, section in enumerate(payload["sections"], start=1):
        marks = [
            {
                "id": item["id"],
                "start": item["start"],
                "end": item["end"],
                "color": item["color"],
                "ref": numbers[item["id"]],
                "kind": "comment",
            }
            for item in annotations
            if item["section_id"] == section["id"] and item["start"] is not None and not item["resolved"]
        ]
        marks.extend(
            {
                "start": mark["start"],
                "end": mark["end"],
                "color": mark.get("color", "#d97706"),
                "label": mark.get("label", ""),
                "kind": "revision",
            }
            for mark in payload["revisions"].get(section["id"], [])
        )
        body.append(f"<h2>{index}. {_safe(section['title'])}</h2>")
        body.append(richtext.render_html(richtext.parse(section["markdown"]), marks))

        for change in payload["changes"]:
            if change["section_id"] != section["id"] or change["status"] == "MERGED":
                continue
            ops = "".join(
                f"<span class=\"{'ins' if op['op'] == 'ins' else 'del'}\">{_safe(op['text'])}</span>"
                for op in change["diff"]
            )
            body.append(
                "<div class=\"change\">"
                f"<span class=\"by\">Tracked change · {_safe(change['operation'])} · {_safe(change['status'])}</span>"
                f" — {_safe(change['author'])} on {_safe(change['when'])}<br>{ops}</div>"
            )

    if annotations:
        body.append("<div class=\"notes\"><h3>Comments</h3>")
        for item in annotations:
            style = f"style=\"--c:{item['color']}\""
            body.append(
                f"<div class=\"note\" {style}>"
                f"<span class=\"who\">{numbers[item['id']]}. {_safe(item['author'])}</span>"
                f" · {_safe(item['when'])}{' · resolved' if item['resolved'] else ''}<br>"
                + (f"<span class=\"quote\">“{_safe(item['quote'])}”</span><br>" if item["quote"] else "")
                + f"{_safe(item['text'])}"
                + "".join(
                    f"<div class=\"reply\"><strong>{_safe(reply['author'])}</strong> · {_safe(reply['when'])}: "
                    f"{_safe(reply['text'])}</div>"
                    for reply in item["replies"]
                )
                + "</div>"
            )
        body.append("</div>")

    title = _safe(contract["title"])
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{title} — review copy</title><style>{_STYLE}</style></head>"
        f"<body><div class=\"page\">{''.join(body)}</div></body></html>"
    )


def write_exports(contract_id: str, actor: Actor) -> dict[str, Path]:
    config.init_dirs()
    stamp = contract_id.replace("/", "-")
    markdown_path = config.EXPORT_DIR / f"{stamp}_review.md"
    html_path = config.EXPORT_DIR / f"{stamp}_review.html"
    markdown_path.write_text(build_markdown(contract_id, actor), encoding="utf-8")
    html_path.write_text(build_print_html(contract_id, actor), encoding="utf-8")
    return {"markdown": markdown_path, "html": html_path}
