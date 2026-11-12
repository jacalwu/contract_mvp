"""Document editing: the word-processor side of the review surface.

Clause text is never written directly. Whole-section rewrites and inline range
edits are submitted as tracked changes (change requests) and only merged into the
document after the *opposite* party accepts them. Structural operations
(add / delete / move / rename a section) are document-level edits kept to the
Admin so participants negotiate clause text through the track-changes flow.
"""
from __future__ import annotations

from core.audit import log_event
from core.constants import (
    E_SECTION_ADDED,
    E_SECTION_MOVED,
    E_SECTION_REMOVED,
    E_SECTION_RENAMED,
    OP_DELETE,
    OP_UPDATE,
    ROLE_ADMIN,
    Actor,
    DomainError,
    new_id,
    now,
)
from core.workflow import bump_version, get_contract, require_editable, require_role
from db.database import fetch_one, sql, tx

_EDIT_ROLES = {ROLE_ADMIN}


def _guards(contract_id: str, actor: Actor, action: str) -> dict:
    contract = get_contract(contract_id)
    require_editable(contract, action)
    require_role(actor, _EDIT_ROLES, action)
    return contract


def _section(section_id: str, contract_id: str) -> dict:
    row = fetch_one(
        "SELECT * FROM sections WHERE section_id = ? AND contract_id = ?", [section_id, contract_id]
    )
    if row is None:
        raise DomainError("The section does not belong to this contract.")
    return row


def _renumber(con, section_ids: list[str]) -> None:
    for index, section_id in enumerate(section_ids):
        sql(con, "UPDATE sections SET position = ? WHERE section_id = ?", [index, section_id])


def _ordered_ids(con, contract_id: str) -> list[str]:
    rows = con.execute(
        "SELECT section_id FROM sections WHERE contract_id = ? ORDER BY position, section_id", [contract_id]
    ).fetchall()
    return [row[0] for row in rows]


def apply_range(
    contract_id: str,
    section_id: str,
    start: int,
    end: int,
    replacement: str,
    actor: Actor,
) -> dict:
    """Replace a selected range — the "suggest edit" path from the document.

    This always produces a tracked change: "suggest" means propose, and the
    approval gate must see it. Whole-section rewrites from the editor take the
    same tracked-change path via :func:`core.changes.propose_change`.
    """
    from core import changes as change_service
    from core import richtext

    contract = get_contract(contract_id)
    require_editable(contract, "Edit the document")
    section = _section(section_id, contract_id)
    markdown = section["content"] or ""
    body = richtext.plain(markdown)
    if start < 0 or end > len(body) or start >= end:
        raise DomainError("The selection is outside this clause; select it again.")
    after = richtext.replace_plain_range(markdown, start, end, replacement)
    if after.strip() == markdown.strip():
        raise DomainError("That edit did not change the text.")

    operation = OP_DELETE if not (replacement or "").strip() else OP_UPDATE
    change_id = change_service.propose_change(contract_id, section_id, operation, markdown, after, actor)
    return {"mode": "tracked", "change_id": change_id}


def add_section(
    contract_id: str,
    after_section_id: str | None,
    title: str,
    content: str,
    actor: Actor,
) -> str:
    _guards(contract_id, actor, "Add a section")
    title = (title or "").strip()
    if not title:
        raise DomainError("A section needs a title.")
    section_id = new_id("SEC")
    with tx() as con:
        sql(
            con,
            """INSERT INTO sections (section_id, contract_id, title, content, position, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [section_id, contract_id, title, content or "", 0, now()],
        )
        order = _ordered_ids(con, contract_id)
        order.remove(section_id)
        if after_section_id and after_section_id in order:
            order.insert(order.index(after_section_id) + 1, section_id)
        else:
            order.append(section_id)
        _renumber(con, order)
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_SECTION_ADDED,
            target_id=section_id,
            after_value={"title": title, "after": after_section_id, "position": order.index(section_id)},
        )
    return section_id


def delete_section(section_id: str, actor: Actor) -> None:
    section = fetch_one("SELECT * FROM sections WHERE section_id = ?", [section_id])
    if section is None:
        raise DomainError("Section not found.")
    contract_id = section["contract_id"]
    contract = _guards(contract_id, actor, "Delete a section")
    total = fetch_one("SELECT count(*) AS total FROM sections WHERE contract_id = ?", [contract_id])
    if (total or {}).get("total", 0) <= 1:
        raise DomainError("A contract needs at least one section.")
    comments = fetch_one("SELECT count(*) AS total FROM comments WHERE section_id = ?", [section_id])
    if (comments or {}).get("total", 0):
        raise DomainError("This section still carries annotations; resolve or delete them before removing it.")
    pending = fetch_one(
        "SELECT count(*) AS total FROM change_requests WHERE section_id = ? AND status IN ('PENDING', 'APPROVED')",
        [section_id],
    )
    if (pending or {}).get("total", 0):
        raise DomainError("This section has unmerged change requests; process them before removing it.")
    with tx() as con:
        sql(con, "DELETE FROM sections WHERE section_id = ?", [section_id])
        order = _ordered_ids(con, contract_id)
        _renumber(con, order)
        bump_version(con, contract)
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_SECTION_REMOVED,
            target_id=section_id,
            before_value={"title": section["title"], "content": section["content"]},
        )


def move_section(section_id: str, direction: int, actor: Actor) -> None:
    section = fetch_one("SELECT * FROM sections WHERE section_id = ?", [section_id])
    if section is None:
        raise DomainError("Section not found.")
    contract_id = section["contract_id"]
    contract = _guards(contract_id, actor, "Reorder sections")
    with tx() as con:
        order = _ordered_ids(con, contract_id)
        index = order.index(section_id)
        target = index + (1 if direction > 0 else -1)
        if target < 0 or target >= len(order):
            return
        order[index], order[target] = order[target], order[index]
        _renumber(con, order)
        bump_version(con, contract)
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_SECTION_MOVED,
            target_id=section_id,
            before_value={"position": index},
            after_value={"position": target},
        )


def rename_section(section_id: str, title: str, actor: Actor) -> None:
    section = fetch_one("SELECT * FROM sections WHERE section_id = ?", [section_id])
    if section is None:
        raise DomainError("Section not found.")
    contract_id = section["contract_id"]
    contract = _guards(contract_id, actor, "Rename a section")
    title = (title or "").strip()
    if not title:
        raise DomainError("A section needs a title.")
    if title == section["title"]:
        return
    with tx() as con:
        sql(con, "UPDATE sections SET title = ? WHERE section_id = ?", [title, section_id])
        bump_version(con, contract)
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_SECTION_RENAMED,
            target_id=section_id,
            before_value={"title": section["title"]},
            after_value={"title": title},
        )
