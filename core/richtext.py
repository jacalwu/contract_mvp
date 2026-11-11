"""Markdown-lite document model for contract sections.

Sections used to be plain prose. Reviewers now need a simple word-processor:
headings, lists, tables, bold/italic. Rather than storing HTML (which is unsafe
to render and impossible to diff), a section stays a *text* column holding a
small Markdown subset. This module turns that text into blocks the UI and the
print export can render, and it defines the one thing every other feature
depends on: the **plain text projection**.

Inline comment anchors are character offsets into that projection, so adding a
`**` around a word does not move every anchor in the section.

Supported syntax
    # H1   ## H2   ### H3
    - item / * item            (bullets)
    1. item                    (ordered)
    > quote
    | a | b |                  (table, second row is the --- separator)
    ---                        (horizontal rule)
    **bold**  __bold__  *italic*  _italic_  ++underline++  ~~strike~~  `code`

Plain text of a section = block texts joined by a newline. For sections that
never used markup this is byte-identical to the stored content, which keeps
every offset written before this module existed valid.
"""
from __future__ import annotations

import html as html_lib
import re

_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_TABLE_SEP = re.compile(r"^\|?[\s:|-]+\|?$")

# longest marker first so ** wins over *
_MARKERS: tuple[tuple[str, str], ...] = (
    ("**", "bold"),
    ("__", "bold"),
    ("~~", "strike"),
    ("++", "underline"),
    ("`", "code"),
    ("*", "italic"),
    ("_", "italic"),
)

BLOCK_TYPES = ("p", "h1", "h2", "h3", "ul", "ol", "quote", "table", "hr")


def _new_block(kind: str) -> dict:
    return {
        "type": kind,
        "runs": [],
        "items": [],
        "rows": [],
        "text": "",
        "start": 0,
        "end": 0,
        "src_start": 0,
        "src_end": 0,
    }


def _run(text: str, src: int, marks: dict) -> dict:
    run = {"text": text, "src": src}
    for key in ("bold", "italic", "underline", "strike", "code"):
        if marks.get(key):
            run[key] = True
    return run


def _find_unescaped(source: str, marker: str, start: int) -> int:
    index = source.find(marker, start)
    while index > 0 and source[index - 1] == "\\":
        index = source.find(marker, index + len(marker))
    return index


def _inline(source: str, src: int, marks: dict | None = None, depth: int = 0) -> list[dict]:
    """Split one block's inline source into styled runs, tracking source offsets."""
    marks = marks or {}
    if depth > 6:
        return [_run(source, src, marks)]
    runs: list[dict] = []
    buf: list[str] = []
    buf_src: int | None = None
    i = 0

    def flush() -> None:
        nonlocal buf, buf_src
        if buf:
            start = src if buf_src is None else buf_src
            runs.append(_run("".join(buf), start, marks))
        buf = []
        buf_src = None

    while i < len(source):
        if source[i] == "\\" and i + 1 < len(source) and source[i + 1] in "\\`*_+~|":
            if buf_src is None:
                buf_src = src + i
            buf.append(source[i + 1])
            i += 2
            continue
        hit = None
        for marker, key in _MARKERS:
            if not source.startswith(marker, i):
                continue
            close = _find_unescaped(source, marker, i + len(marker))
            if close <= i + len(marker):
                continue
            if not source[i + len(marker) : close].strip():
                continue
            hit = (marker, key, close)
            break
        if hit:
            marker, key, close = hit
            flush()
            inner = dict(marks)
            inner[key] = True
            runs.extend(_inline(source[i + len(marker) : close], src + i + len(marker), inner, depth + 1))
            i = close + len(marker)
            continue
        if buf_src is None:
            buf_src = src + i
        buf.append(source[i])
        i += 1
    flush()
    return runs


def _finalize(block: dict, chunks: list[tuple[str, int, list[dict]]]) -> None:
    """chunks: [(raw source, src offset, runs)] joined by a newline.

    The block text is rebuilt from the runs, so inline markers never reach the
    plain projection that annotation offsets are measured against.
    """
    parts: list[str] = []
    cursor = 0
    collected: list[dict] = []
    for index, (_raw, _src, chunk_runs) in enumerate(chunks):
        if index:
            cursor += 1  # the joining newline belongs to the block text
        chunk_text = "".join(run["text"] for run in chunk_runs)
        local = 0
        for run in chunk_runs:
            collected.append({**run, "p": cursor + local})
            local += len(run["text"])
        parts.append(chunk_text)
        cursor += len(chunk_text)
    block["text"] = "\n".join(parts)
    block["runs"] = collected


def parse(text: str) -> list[dict]:
    """Parse section markdown into blocks with plain-text offsets resolved."""
    text = text or ""
    blocks: list[dict] = []
    lines: list[str] = []
    offsets: list[int] = []
    pos = 0
    for raw in text.split("\n"):
        lines.append(raw)
        offsets.append(pos)
        pos += len(raw) + 1

    i = 0
    total = len(lines)
    while i < total:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue
        line_start = offsets[i]

        heading = _HEADING.match(stripped)
        if heading:
            level = len(heading.group(1))
            block = _new_block(f"h{level}")
            body = heading.group(2).strip()
            body_src = line_start + raw.index(body) if body else line_start
            _finalize(block, [(body, body_src, _inline(body, body_src))])
            block["src_start"] = line_start
            block["src_end"] = offsets[i] + len(raw)
            blocks.append(block)
            i += 1
            continue

        if _RULE.match(stripped):
            block = _new_block("hr")
            block["src_start"] = line_start
            block["src_end"] = offsets[i] + len(raw)
            blocks.append(block)
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < total and _TABLE_SEP.match(lines[i + 1].strip()):
            block = _new_block("table")
            block["src_start"] = line_start
            rows: list[dict] = []
            cells_flat: list[dict] = []
            j = i
            row_index = 0
            while j < total and lines[j].strip().startswith("|"):
                if j == i + 1:
                    j += 1
                    continue  # separator row
                cells = []
                for cell_text, cell_rel_src in _split_row(lines[j]):
                    cell_src = offsets[j] + cell_rel_src
                    runs = _inline(cell_text, cell_src)
                    cells.append(
                        {"runs": runs, "text": "".join(run["text"] for run in runs), "start": 0, "src": cell_src}
                    )
                    cells_flat.append(cells[-1])
                rows.append({"cells": cells, "header": row_index == 0})
                row_index += 1
                j += 1
            cursor = 0
            for cell in cells_flat:
                cell["start"] = cursor
                local = 0
                for run in cell["runs"]:
                    run["p"] = local
                    local += len(run["text"])
                cursor += len(cell["text"]) + 1
            block["rows"] = rows
            block["runs"] = [
                {**run, "p": cell["start"] + run["p"]} for cell in cells_flat for run in cell["runs"]
            ]
            block["text"] = "\n".join(cell["text"] for cell in cells_flat)
            block["src_end"] = offsets[j - 1] + len(lines[j - 1])
            blocks.append(block)
            i = j
            continue

        if _QUOTE.match(raw):
            block = _new_block("quote")
            block["src_start"] = line_start
            chunks = []
            j = i
            while j < total and _QUOTE.match(lines[j]):
                body = _QUOTE.match(lines[j]).group(1)
                body_src = offsets[j] + lines[j].index(body) if body.strip() else offsets[j]
                chunks.append((body, body_src, _inline(body, body_src)))
                j += 1
            _finalize(block, chunks)
            block["src_end"] = offsets[j - 1] + len(lines[j - 1])
            blocks.append(block)
            i = j
            continue

        if _BULLET.match(raw.replace("\t", "    ")) or _ORDERED.match(stripped):
            ordered = bool(_ORDERED.match(stripped))
            block = _new_block("ol" if ordered else "ul")
            block["src_start"] = line_start
            chunks = []
            j = i
            while j < total:
                current = lines[j]
                pattern = _ORDERED if ordered else _BULLET
                match = pattern.match(current.strip() if ordered else current.replace("\t", "    "))
                if not match:
                    break
                body = match.group(1).strip()
                body_src = offsets[j] + current.index(body) if body and body in current else offsets[j]
                chunks.append((body, body_src, _inline(body, body_src)))
                j += 1
            items = []
            for _text, _src, runs in chunks:
                local = 0
                item_runs = []
                for run in runs:
                    item_runs.append({**run, "p": local})
                    local += len(run["text"])
                items.append({"runs": item_runs, "text": "".join(r["text"] for r in item_runs), "start": 0})
            block["items"] = items
            _finalize(block, chunks)
            cursor = 0
            for item in block["items"]:
                item["start"] = cursor
                cursor += len(item["text"]) + 1
            block["src_end"] = offsets[j - 1] + len(lines[j - 1])
            blocks.append(block)
            i = j
            continue

        # paragraph: consecutive lines, hard breaks kept as newlines
        block = _new_block("p")
        block["src_start"] = line_start
        chunks = []
        j = i
        while j < total:
            current = lines[j]
            if not current.strip():
                break
            if j > i and (
                _HEADING.match(current.strip())
                or _RULE.match(current.strip())
                or _BULLET.match(current.replace("\t", "    "))
                or _ORDERED.match(current.strip())
                or _QUOTE.match(current)
                or (current.strip().startswith("|") and j + 1 < total and _TABLE_SEP.match(lines[j + 1].strip()))
            ):
                break
            body = current.strip()
            body_src = offsets[j] + current.index(body) if body else offsets[j]
            chunks.append((body, body_src, _inline(body, body_src)))
            j += 1
        _finalize(block, chunks)
        block["src_end"] = offsets[j - 1] + len(lines[j - 1])
        blocks.append(block)
        i = j

    cursor = 0
    for index, block in enumerate(blocks):
        block["index"] = index
        block["start"] = cursor
        block["end"] = cursor + len(block["text"])
        cursor = block["end"] + 1
        for run in block["runs"]:
            run["abs"] = block["start"] + run["p"]
        block["line"] = index + 1
    return blocks


def _split_row(raw: str) -> list[tuple[str, int]]:
    """Split a table row into (cell text, source offset) pairs, honouring \\|."""
    line = raw.rstrip()
    start = 1 if line.startswith("|") else 0
    end = len(line) - 1 if line.endswith("|") and len(line) > 1 else len(line)
    body = line[start:end]
    pieces: list[tuple[str, int]] = []
    buffer: list[str] = []
    piece_start = 0
    i = 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            buffer.append("\\|")
            i += 2
            continue
        if body[i] == "|":
            pieces.append(("".join(buffer), piece_start))
            buffer = []
            i += 1
            piece_start = i
            continue
        buffer.append(body[i])
        i += 1
    pieces.append(("".join(buffer), piece_start))
    cells: list[tuple[str, int]] = []
    for raw_piece, offset in pieces:
        text = raw_piece.strip()
        src = start + offset + (raw_piece.find(text) if text else 0)
        cells.append((text, src))
    return cells


def replace_plain_range(text: str, start: int, end: int, replacement: str) -> str:
    """Replace a plain-text range in the raw markdown.

    A reviewer selects rendered words, so their offsets know nothing about the
    markup. The range is expanded to the whole block it touches when the offset
    falls on a block boundary, otherwise mapped back through the run offsets.
    """
    blocks = parse(text)
    if not blocks:
        return replacement

    def locate(position: int) -> dict:
        for block in blocks:
            if block["start"] <= position <= block["end"]:
                return block
        return blocks[-1]

    first = locate(start)
    last = locate(end)
    src_start = map_plain_to_source(first, max(0, start - first["start"]))
    src_end = map_plain_to_source(last, max(0, end - last["start"]), bias="end")
    if src_end < src_start:
        src_end = src_start
    return text[:src_start] + replacement + text[src_end:]


def plain(text: str) -> str:
    """The projection every annotation offset is measured against."""
    return "\n".join(block["text"] for block in parse(text))


def plain_blocks(text: str) -> list[dict]:
    return parse(text)


def map_plain_to_source(block: dict, offset: int, bias: str = "start") -> int:
    """Translate a plain offset inside `block` back to an index in the raw text.

    `bias` decides where a boundary lands: "start" keeps opening markers outside
    a range, "end" keeps closing markers outside, so formatting survives an edit
    that touches a styled word.
    """
    runs = block.get("runs") or []
    if not runs:
        return block["src_start"]
    for run in runs:
        start = run["p"]
        length = len(run["text"])
        if start <= offset < start + length:
            return run["src"] + (offset - start)
        if bias == "end" and offset == start + length:
            return run["src"] + length
    last = runs[-1]
    return last["src"] + len(last["text"])


def insert_references(text: str, refs: list[tuple[int, str]]) -> str:
    """Insert `[^n]` markers at plain offsets, writing back into the raw markdown."""
    if not refs:
        return text
    splices: list[tuple[int, str]] = []
    for block in parse(text):
        for offset, label in refs:
            if block["start"] <= offset <= block["end"]:
                splices.append((map_plain_to_source(block, offset - block["start"]), f"[^{label}]"))
    result = text
    for position, marker in sorted(splices, key=lambda item: item[0], reverse=True):
        result = result[:position] + marker + result[position:]
    return result


def _esc(value: str) -> str:
    return html_lib.escape(value or "", quote=False)


def _decorate(text: str, block_start: int, decorations: list[dict] | None) -> str:
    if not decorations:
        return _esc(text)
    local: list[dict] = []
    for item in decorations:
        start = max(item["start"], block_start)
        end = min(item["end"], block_start + len(text))
        if start >= end:
            continue
        local.append({**item, "start": start - block_start, "end": end - block_start})
    if not local:
        return _esc(text)
    local.sort(key=lambda item: (item["start"], item["end"]))
    out: list[str] = []
    position = 0
    for item in local:
        if item["start"] < position:
            item = {**item, "start": position}
        if item["start"] >= item["end"]:
            continue
        out.append(_esc(text[position : item["start"]]))
        fragment = _esc(text[item["start"] : item["end"]])
        if item.get("kind") == "revision":
            out.append(f'<span class="ann rev" style="--c:{item.get("color", "#d97706")}" title="{_esc(item.get("label", ""))}">{fragment}</span>')
        else:
            ref = f'<sup class="ref">{_esc(str(item["ref"]))}</sup>' if item.get("ref") else ""
            out.append(f'<span class="ann" style="--c:{item.get("color", "#2563eb")}">{fragment}{ref}</span>')
        position = item["end"]
    out.append(_esc(text[position:]))
    return "".join(out)


def _runs_html(runs: list[dict], block_start: int, decorations: list[dict] | None) -> str:
    if decorations:
        return _decorate("".join(run["text"] for run in runs), block_start, decorations)
    out = []
    for run in runs:
        fragment = _esc(run["text"])
        if run.get("code"):
            fragment = f"<code>{fragment}</code>"
        if run.get("bold"):
            fragment = f"<strong>{fragment}</strong>"
        if run.get("italic"):
            fragment = f"<em>{fragment}</em>"
        if run.get("underline"):
            fragment = f"<u>{fragment}</u>"
        if run.get("strike"):
            fragment = f"<del>{fragment}</del>"
        out.append(fragment)
    return "".join(out)


def render_html(blocks: list[dict], decorations: list[dict] | None = None) -> str:
    """Paper HTML for the print export: block markup plus optional highlights.

    When decorations are supplied the block is rendered from its flat text so the
    character offsets keep working; inline styling is then dropped for that block
    (only blocks carrying comments are affected).
    """
    by_block: dict[int, list[dict]] = {}
    parts: list[str] = []
    for block in blocks:
        kind = block["type"]
        overlay = [item for item in (decorations or []) if item["start"] < block["end"] and item["end"] > block["start"]]
        if kind in ("p", "h1", "h2", "h3", "quote"):
            inner = _runs_html(block["runs"], block["start"], overlay) if not overlay else _decorate(
                block["text"], block["start"], overlay
            )
            if kind == "quote":
                parts.append(f"<blockquote>{inner}</blockquote>")
            elif kind == "p":
                parts.append(f'<p class="body">{inner.replace(chr(10), "<br>")}</p>')
            else:
                parts.append(f"<{kind}>{inner}</{kind}>")
        elif kind in ("ul", "ol"):
            rows = []
            for item in block["items"]:
                base = block["start"] + block["text"].find(item["text"]) if item["text"] else block["start"]
                inner = _runs_html(item["runs"], base, overlay) if not overlay else _decorate(item["text"], base, overlay)
                rows.append(f"<li>{inner}</li>")
            parts.append(f"<{kind}>{''.join(rows)}</{kind}>")
        elif kind == "table":
            rows_html = []
            for row in block["rows"]:
                tag = "th" if row.get("header") else "td"
                cells = "".join(f"<{tag}>{_runs_html(cell['runs'], block['start'], None)}</{tag}>" for cell in row["cells"])
                rows_html.append(f"<tr>{cells}</tr>")
            parts.append(f"<table class=\"doc-table\">{''.join(rows_html)}</table>")
        elif kind == "hr":
            parts.append("<hr>")
    return "".join(parts)
