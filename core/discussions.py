"""Comments (with threaded replies) and the clarification Q&A workflow."""
from __future__ import annotations

import config
from core.audit import log_event
from core import richtext
from core.constants import (
    CLARIFICATION_ANSWERED,
    CLARIFICATION_CLOSED,
    CLARIFICATION_OPEN,
    DomainError,
    E_CLARIFICATION_ANSWERED,
    E_CLARIFICATION_CLOSED,
    E_CLARIFICATION_CREATED,
    E_COMMENT_CREATED,
    E_COMMENT_DELETED,
    E_COMMENT_REOPENED,
    E_COMMENT_RESOLVED,
    E_COMMENT_UPDATED,
    ROLE_ADMIN,
    Actor,
    new_id,
    now,
)
from core.workflow import evaluate_contract_approval, get_contract, require_discussion_open
from db.database import fetch_all, fetch_one, sql, tx


def _text(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DomainError(f"{field} cannot be empty.")
    return text


def _validate_section(contract_id: str, section_id: str | None) -> None:
    if section_id is None or section_id == "":
        return
    exists = fetch_one(
        "SELECT 1 FROM sections WHERE section_id = ? AND contract_id = ?",
        [section_id, contract_id],
    )
    if not exists:
        raise DomainError("The selected section does not belong to this contract.")


def _validate_anchor(
    contract_id: str,
    section_id: str | None,
    anchor_start: int | None,
    anchor_end: int | None,
    quoted_text: str | None,
) -> tuple[int | None, int | None, str | None]:
    """Inline annotations address a character range inside one section's text.

    The offsets are stored relative to the plain section text, and the quoted
    fragment is kept so a shifted document can be detected instead of silently
    highlighting the wrong words.
    """
    if anchor_start is None and anchor_end is None:
        return None, None, None
    if section_id is None:
        raise DomainError("Inline annotations must be attached to a section.")
    section = fetch_one(
        "SELECT content FROM sections WHERE section_id = ? AND contract_id = ?",
        [section_id, contract_id],
    )
    if section is None:
        raise DomainError("The selected section does not belong to this contract.")
    if anchor_start is None or anchor_end is None:
        raise DomainError("Both annotation offsets are required.")
    try:
        start, end = int(anchor_start), int(anchor_end)
    except (TypeError, ValueError):
        raise DomainError("Annotation offsets must be integers.") from None
    content = section["content"] or ""
    body = richtext.plain(content)
    if start < 0 or end <= start or end > len(body):
        raise DomainError("The selected range is outside the section text; re-select and try again.")
    actual = body[start:end]
    if quoted_text is not None and str(quoted_text) != actual:
        raise DomainError("The document changed while you were annotating; reload and re-select the text.")
    return start, end, actual


def add_comment(
    contract_id: str,
    section_id: str | None,
    message: str,
    actor: Actor,
    parent_comment_id: str | None = None,
    anchor_start: int | None = None,
    anchor_end: int | None = None,
    quoted_text: str | None = None,
    color: str | None = None,
) -> str:
    contract = get_contract(contract_id)
    require_discussion_open(contract, "Post comment")
    message = _text(message, "Comment text")
    _validate_section(contract_id, section_id)
    if parent_comment_id:
        parent = fetch_one(
            "SELECT * FROM comments WHERE comment_id = ? AND contract_id = ?",
            [parent_comment_id, contract_id],
        )
        if parent is None:
            raise DomainError("The comment being replied to does not exist.")
        section_id = parent["section_id"]
        # a reply belongs to the same highlighted range as its parent thread
        anchor_start = anchor_start if anchor_start is not None else parent["anchor_start"]
        anchor_end = anchor_end if anchor_end is not None else parent["anchor_end"]
        quoted_text = quoted_text if quoted_text is not None else parent["quoted_text"]
        color = color or parent["color"]
    if color is not None and not _is_color(color):
        raise DomainError("Unknown annotation colour.")
    anchor_start, anchor_end, quoted_text = _validate_anchor(
        contract_id, section_id, anchor_start, anchor_end, quoted_text
    )

    comment_id = new_id("CMT")
    with tx() as con:
        sql(
            con,
            """INSERT INTO comments
               (comment_id, contract_id, section_id, author_id, message, parent_comment_id,
                anchor_start, anchor_end, quoted_text, color, resolved_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            [
                comment_id,
                contract_id,
                section_id or None,
                actor.id,
                message,
                parent_comment_id or None,
                anchor_start,
                anchor_end,
                quoted_text,
                color,
                now(),
            ],
        )
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_COMMENT_CREATED,
            target_id=comment_id,
            after_value={
                "section_id": section_id,
                "message": message,
                "reply_to": parent_comment_id,
                "anchor": [anchor_start, anchor_end] if anchor_start is not None else None,
                "quoted_text": quoted_text,
                "color": color,
            },
        )
    return comment_id


def set_resolved(comment_id: str, resolved: bool, actor: Actor) -> None:
    """Resolve or reopen an annotation thread without removing its history."""
    comment = _load_comment(comment_id)
    require_discussion_open(get_contract(comment["contract_id"]), "Resolve annotation")
    if comment["parent_comment_id"]:
        raise DomainError("Only a top-level annotation can be resolved.")
    if bool(comment["resolved_at"]) == bool(resolved):
        return
    timestamp = now() if resolved else None
    with tx() as con:
        sql(con, "UPDATE comments SET resolved_at = ? WHERE comment_id = ?", [timestamp, comment_id])
        log_event(
            con,
            contract_id=comment["contract_id"],
            actor=actor,
            event_type=E_COMMENT_RESOLVED if resolved else E_COMMENT_REOPENED,
            target_id=comment_id,
            before_value={"resolved": bool(comment["resolved_at"])},
            after_value={"resolved": bool(resolved)},
        )


def list_threads(contract_id: str, include_resolved: bool = True) -> list[dict]:
    """Top-level annotations with their replies nested, ready for the document view."""
    rows = fetch_all(
        "SELECT * FROM comments WHERE contract_id = ? ORDER BY created_at, comment_id",
        [contract_id],
    )
    roots = [row for row in rows if not row["parent_comment_id"]]
    replies: dict[str, list[dict]] = {}
    for row in rows:
        if row["parent_comment_id"]:
            replies.setdefault(row["parent_comment_id"], []).append(row)
    threads = []
    for root in roots:
        if not include_resolved and root["resolved_at"]:
            continue
        item = dict(root)
        item["replies"] = replies.get(root["comment_id"], [])
        threads.append(item)
    return threads


def _is_color(color: str) -> bool:
    return color in config.REVIEW_PALETTE or color == config.ADMIN_COLOR


def _load_comment(comment_id: str) -> dict:
    comment = fetch_one("SELECT * FROM comments WHERE comment_id = ?", [comment_id])
    if comment is None:
        raise DomainError("Comment not found.")
    return comment


def _can_modify(comment: dict, actor: Actor) -> None:
    if actor.role != ROLE_ADMIN and comment["author_id"] != actor.id:
        raise DomainError("Only the author (or an Admin) can edit or delete a comment.")


def update_comment(comment_id: str, message: str, actor: Actor) -> None:
    comment = _load_comment(comment_id)
    require_discussion_open(get_contract(comment["contract_id"]), "Edit comment")
    _can_modify(comment, actor)
    message = _text(message, "Comment text")
    with tx() as con:
        sql(con, "UPDATE comments SET message = ? WHERE comment_id = ?", [message, comment_id])
        log_event(
            con,
            contract_id=comment["contract_id"],
            actor=actor,
            event_type=E_COMMENT_UPDATED,
            target_id=comment_id,
            before_value=comment["message"],
            after_value=message,
        )


def delete_comment(comment_id: str, actor: Actor) -> None:
    comment = _load_comment(comment_id)
    require_discussion_open(get_contract(comment["contract_id"]), "Delete comment")
    _can_modify(comment, actor)
    with tx() as con:
        # keep replies visible by promoting them to top-level
        sql(
            con,
            "UPDATE comments SET parent_comment_id = NULL WHERE parent_comment_id = ?",
            [comment_id],
        )
        sql(con, "DELETE FROM comments WHERE comment_id = ?", [comment_id])
        log_event(
            con,
            contract_id=comment["contract_id"],
            actor=actor,
            event_type=E_COMMENT_DELETED,
            target_id=comment_id,
            before_value={"author_id": comment["author_id"], "message": comment["message"]},
        )


def add_clarification(contract_id: str, section_id: str | None, question: str, actor: Actor) -> str:
    contract = get_contract(contract_id)
    require_discussion_open(contract, "Raise clarification")
    question = _text(question, "Clarification question")
    _validate_section(contract_id, section_id)
    clarification_id = new_id("CLF")
    with tx() as con:
        sql(
            con,
            """INSERT INTO clarifications
               (clarification_id, contract_id, section_id, question, answer, status,
                created_by, answered_by, answered_at, created_at)
               VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, NULL, ?)""",
            [clarification_id, contract_id, section_id or None, question, CLARIFICATION_OPEN, actor.id, now()],
        )
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_CLARIFICATION_CREATED,
            target_id=clarification_id,
            after_value={"section_id": section_id, "question": question, "status": CLARIFICATION_OPEN},
        )
    return clarification_id


def answer_clarification(clarification_id: str, answer: str, actor: Actor) -> None:
    row = fetch_one("SELECT * FROM clarifications WHERE clarification_id = ?", [clarification_id])
    if row is None:
        raise DomainError("Clarification not found.")
    require_discussion_open(get_contract(row["contract_id"]), "Answer clarification")
    if row["status"] == CLARIFICATION_CLOSED:
        raise DomainError("This clarification is closed and cannot be answered again.")
    answer = _text(answer, "Clarification answer")
    with tx() as con:
        sql(
            con,
            """UPDATE clarifications SET answer = ?, status = ?, answered_by = ?, answered_at = ?
               WHERE clarification_id = ?""",
            [answer, CLARIFICATION_ANSWERED, actor.id, now(), clarification_id],
        )
        log_event(
            con,
            contract_id=row["contract_id"],
            actor=actor,
            event_type=E_CLARIFICATION_ANSWERED,
            target_id=clarification_id,
            before_value={"status": row["status"], "answer": row["answer"]},
            after_value={"status": CLARIFICATION_ANSWERED, "answer": answer},
        )


def close_clarification(clarification_id: str, actor: Actor) -> None:
    row = fetch_one("SELECT * FROM clarifications WHERE clarification_id = ?", [clarification_id])
    if row is None:
        raise DomainError("Clarification not found.")
    require_discussion_open(get_contract(row["contract_id"]), "Close clarification")
    if row["status"] == CLARIFICATION_CLOSED:
        raise DomainError("This clarification is already closed.")
    with tx() as con:
        sql(
            con,
            "UPDATE clarifications SET status = ? WHERE clarification_id = ?",
            [CLARIFICATION_CLOSED, clarification_id],
        )
        log_event(
            con,
            contract_id=row["contract_id"],
            actor=actor,
            event_type=E_CLARIFICATION_CLOSED,
            target_id=clarification_id,
            before_value={"status": row["status"]},
            after_value={"status": CLARIFICATION_CLOSED, "answer": row["answer"]},
        )
        # closing the last OPEN clarification can unblock an in-flight approval
        evaluate_contract_approval(con, row["contract_id"], actor)
