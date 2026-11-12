"""Track-changes style modification requests: INSERT / UPDATE / DELETE, review and merge.

Nothing is edited directly: a change request stores before/after text and is merged into
the section only after an Approver or Admin accepts it.
"""
from __future__ import annotations

import difflib
import html

from core.audit import log_event
from core.constants import (
    CHANGE_APPROVED,
    CHANGE_MERGED,
    CHANGE_PENDING,
    CHANGE_REJECTED,
    E_CHANGE_APPROVED,
    E_CHANGE_CREATED,
    E_CHANGE_MERGED,
    E_CHANGE_REJECTED,
    OPERATIONS,
    OP_DELETE,
    OP_INSERT,
    OP_UPDATE,
    Actor,
    DomainError,
    new_id,
    now,
)
from core.workflow import (
    bump_version,
    can_review_change_for,
    evaluate_contract_approval,
    get_contract,
    require_editable,
    require_not_frozen,
)
from db.database import fetch_all, fetch_one, sql, tx


def build_after_content(operation: str, before: str, user_input: str) -> str:
    """Translate a UI intent into the proposed after_content text."""
    text = str(user_input or "").strip()
    if operation not in OPERATIONS:
        raise DomainError(f"Unknown change operation: {operation}")
    if operation == OP_INSERT:
        if not text:
            raise DomainError("Inserted text cannot be empty.")
        return f"{before}\n\n{text}" if before.strip() else text
    if operation == OP_UPDATE:
        if not text:
            raise DomainError("Updated clause text cannot be empty.")
        return text
    # DELETE
    if not text:
        raise DomainError("Enter the exact text fragment to delete.")
    if text not in before:
        raise DomainError("The fragment to delete is not present in the current clause text.")
    return before.replace(text, "", 1).strip()


def diff_html(before: str, after: str) -> str:
    """Word-like inline diff. All user text is HTML-escaped before rendering."""
    matcher = difflib.SequenceMatcher(None, before or "", after or "")
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old = html.escape(before[i1:i2])
        new = html.escape(after[j1:j2])
        if tag == "equal":
            parts.append(old)
        elif tag == "delete":
            parts.append(f'<span class="diff-del">{old or "(deleted)"}</span>')
        elif tag == "insert":
            parts.append(f'<span class="diff-ins">{new}</span>')
        else:
            parts.append(f'<span class="diff-del">{old}</span><span class="diff-ins">{new}</span>')
    body = "".join(parts).replace("\n", "<br>")
    return f'<div class="diff-body">{body or "<em>(empty)</em>"}</div>'


def diff_lines(before: str, after: str) -> list[tuple[str, str]]:
    """Line-level -/+ view for the change list."""
    rows: list[tuple[str, str]] = []
    for line in difflib.ndiff((before or "").splitlines(), (after or "").splitlines()):
        if line.startswith("? "):
            continue
        rows.append((line[:2].strip() or " ", line[2:]))
    return rows


def propose_change(
    contract_id: str,
    section_id: str,
    operation: str,
    before_content: str,
    after_content: str,
    actor: Actor,
) -> str:
    contract = get_contract(contract_id)
    require_editable(contract, "Submit change request")
    section = fetch_one(
        "SELECT * FROM sections WHERE section_id = ? AND contract_id = ?",
        [section_id, contract_id],
    )
    if section is None:
        raise DomainError("The selected section does not belong to this contract.")
    if before_content != section["content"]:
        raise DomainError("The clause text changed; refresh and resubmit your request.")
    if operation not in OPERATIONS:
        raise DomainError(f"Unknown change operation: {operation}")
    if after_content == before_content:
        raise DomainError("Before and after text are identical; nothing to submit.")
    if operation == OP_INSERT and (before_content not in after_content or len(after_content) <= len(before_content)):
        raise DomainError("INSERT may only append content to the original text.")
    if operation == OP_DELETE and len(after_content) >= len(before_content):
        raise DomainError("DELETE must shorten the clause text.")

    change_id = new_id("CHG")
    with tx() as con:
        sql(
            con,
            """INSERT INTO change_requests
               (change_id, contract_id, section_id, operation, before_content, after_content,
                requested_by, status, created_at, reviewed_by, reviewed_at, merged_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)""",
            [
                change_id,
                contract_id,
                section_id,
                operation,
                before_content,
                after_content,
                actor.id,
                CHANGE_PENDING,
                now(),
            ],
        )
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_CHANGE_CREATED,
            target_id=change_id,
            after_value={"section_id": section_id, "operation": operation, "status": CHANGE_PENDING},
            before_value=before_content,
        )
    return change_id


def list_changes(contract_id: str) -> list[dict]:
    return fetch_all(
        """SELECT cr.*, s.title AS section_title
           FROM change_requests cr LEFT JOIN sections s ON s.section_id = cr.section_id
           WHERE cr.contract_id = ? ORDER BY cr.created_at DESC, cr.change_id DESC""",
        [contract_id],
    )


def _proposer_side(change_row: dict) -> str | None:
    proposer = fetch_one(
        "SELECT side FROM stakeholders WHERE stakeholder_id = ?", [change_row["requested_by"]]
    )
    return proposer["side"] if proposer else None


def review_change(change_id: str, accept: bool, actor: Actor) -> None:
    row = fetch_one("SELECT * FROM change_requests WHERE change_id = ?", [change_id])
    if row is None:
        raise DomainError("Change request not found.")
    require_not_frozen(get_contract(row["contract_id"]), "Review change")
    if row["status"] != CHANGE_PENDING:
        raise DomainError(f"Change request status is {row['status']}; only PENDING requests can be reviewed.")
    if not can_review_change_for(actor, _proposer_side(row)):
        raise DomainError("Only the opposite party (the other side) can review and accept this change.")
    new_status = CHANGE_APPROVED if accept else CHANGE_REJECTED
    event = E_CHANGE_APPROVED if accept else E_CHANGE_REJECTED
    with tx() as con:
        sql(
            con,
            "UPDATE change_requests SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE change_id = ?",
            [new_status, actor.id, now(), change_id],
        )
        log_event(
            con,
            contract_id=row["contract_id"],
            actor=actor,
            event_type=event,
            target_id=change_id,
            before_value={"status": row["status"]},
            after_value={"status": new_status, "operation": row["operation"]},
        )


def merge_change(change_id: str, actor: Actor) -> int:
    """Apply an approved change to the section and bump the contract version."""
    row = fetch_one("SELECT * FROM change_requests WHERE change_id = ?", [change_id])
    if row is None:
        raise DomainError("Change request not found.")
    contract = get_contract(row["contract_id"])
    require_not_frozen(contract, "Merge change")
    if not can_review_change_for(actor, _proposer_side(row)):
        raise DomainError("Only the opposite party (the other side) can merge this change.")
    if row["status"] != CHANGE_APPROVED:
        raise DomainError(f"Only APPROVED changes can be merged; current status is {row['status']}.")
    section = fetch_one("SELECT * FROM sections WHERE section_id = ?", [row["section_id"]])
    if section is None:
        raise DomainError("The original section no longer exists; cannot merge.")
    if section["content"] != row["before_content"]:
        raise DomainError("The clause text was modified by another change; resubmit based on the latest text.")

    with tx() as con:
        sql(con, "UPDATE sections SET content = ? WHERE section_id = ?", [row["after_content"], row["section_id"]])
        sql(
            con,
            "UPDATE change_requests SET status = ?, merged_at = ? WHERE change_id = ?",
            [CHANGE_MERGED, now(), change_id],
        )
        version = bump_version(con, contract)
        log_event(
            con,
            contract_id=row["contract_id"],
            actor=actor,
            event_type=E_CHANGE_MERGED,
            target_id=change_id,
            before_value={"content": row["before_content"], "version": contract["version"]},
            after_value={"content": row["after_content"], "version": version},
        )
        # clearing the last unmerged change can unblock an in-flight approval
        evaluate_contract_approval(con, row["contract_id"], actor)
    return version
