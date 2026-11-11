"""Comments (with threaded replies) and the clarification Q&A workflow."""
from __future__ import annotations

from core.audit import log_event
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
    E_COMMENT_UPDATED,
    ROLE_ADMIN,
    Actor,
    new_id,
    now,
)
from core.workflow import evaluate_contract_approval, get_contract, require_discussion_open
from db.database import fetch_one, sql, tx


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


def add_comment(
    contract_id: str,
    section_id: str | None,
    message: str,
    actor: Actor,
    parent_comment_id: str | None = None,
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

    comment_id = new_id("CMT")
    with tx() as con:
        sql(
            con,
            """INSERT INTO comments
               (comment_id, contract_id, section_id, author_id, message, parent_comment_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [comment_id, contract_id, section_id or None, actor.id, message, parent_comment_id or None, now()],
        )
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_COMMENT_CREATED,
            target_id=comment_id,
            after_value={"section_id": section_id, "message": message, "reply_to": parent_comment_id},
        )
    return comment_id


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
