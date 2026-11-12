"""Contracts, sections, stakeholders, review submission and locking."""
from __future__ import annotations

import re
from typing import Any

import config
from core.audit import log_event
from core.constants import (
    APPROVAL_PENDING,
    APPROVED,
    DRAFT,
    DomainError,
    E_CONTRACT_CREATED,
    E_CONTRACT_LOCKED,
    E_REVIEW_STARTED,
    E_STAKEHOLDER_ADDED,
    E_STAKEHOLDER_COLOR_SET,
    E_STAKEHOLDER_REMOVED,
    IN_REVIEW,
    ROLE_ADMIN,
    ROLE_STAKEHOLDER,
    SIDE_BUYER,
    SIDE_VENDOR,
    SIDES,
    Actor,
    new_id,
    now,
)
from core.workflow import (
    approval_gate,
    evaluate_contract_approval,
    get_contract,
    require_not_frozen,
    require_role,
    require_status,
)
from db.database import fetch_all, fetch_one, sql, tx

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DomainError(f"{field} cannot be empty.")
    return text


def create_contract(
    title: str,
    sections: list[dict[str, str]],
    stakeholders: list[dict[str, str]],
    actor: Actor,
) -> str:
    require_role(actor, {ROLE_ADMIN}, "Create contract")
    title = _clean_text(title, "Contract title")
    if not sections:
        raise DomainError("A contract needs at least one section.")
    if not stakeholders:
        raise DomainError("A contract needs at least one stakeholder.")

    clean_sections: list[tuple[str, str]] = []
    for index, item in enumerate(sections, start=1):
        clean_sections.append(
            (_clean_text(item.get("title"), f"Section {index} title"), _clean_text(item.get("content"), f"Section {index} content"))
        )

    clean_stakeholders: list[tuple[str, str, str, str]] = []
    seen_emails: set[str] = set()
    for item in stakeholders:
        name = _clean_text(item.get("name"), "Stakeholder name")
        email = _clean_text(item.get("email"), "Stakeholder email").lower()
        side = str(item.get("side") or SIDE_BUYER).strip().upper()
        if side not in SIDES:
            raise DomainError(f"Unknown side: {side}. Use {SIDE_BUYER} or {SIDE_VENDOR}.")
        if not _EMAIL_RE.match(email):
            raise DomainError(f"Invalid stakeholder email: {email}")
        if email in seen_emails:
            raise DomainError(f"Duplicate stakeholder email: {email}")
        seen_emails.add(email)
        clean_stakeholders.append((name, email, side, ROLE_STAKEHOLDER))

    buyer_count = sum(1 for _, _, side, _ in clean_stakeholders if side == SIDE_BUYER)
    vendor_count = sum(1 for _, _, side, _ in clean_stakeholders if side == SIDE_VENDOR)
    if buyer_count < 1 or vendor_count < 1:
        raise DomainError("A contract needs at least one Buyer-side and one Vendor-side participant.")

    contract_id = new_id("CTR")
    timestamp = now()
    with tx() as con:
        sql(
            con,
            "INSERT INTO contracts (contract_id, title, status, version, created_at) VALUES (?, ?, ?, ?, ?)",
            [contract_id, title, DRAFT, 1, timestamp],
        )
        for position, (section_title, content) in enumerate(clean_sections):
            sql(
                con,
                """INSERT INTO sections (section_id, contract_id, title, content, position, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [new_id("SEC"), contract_id, section_title, content, position, timestamp],
            )
        for index, (name, email, side, role) in enumerate(clean_stakeholders):
            stakeholder_id = new_id("STK")
            sql(
                con,
                """INSERT INTO stakeholders (stakeholder_id, contract_id, name, email, role, side, color, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    stakeholder_id,
                    contract_id,
                    name,
                    email,
                    role,
                    side,
                    config.REVIEW_PALETTE[index % len(config.REVIEW_PALETTE)],
                    timestamp,
                ],
            )
            sql(
                con,
                """INSERT INTO approvals (approval_id, contract_id, stakeholder_id, status, approved_at, created_at)
                   VALUES (?, ?, ?, ?, NULL, ?)""",
                [new_id("APR"), contract_id, stakeholder_id, APPROVAL_PENDING, timestamp],
            )
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_CONTRACT_CREATED,
            target_id=contract_id,
            after_value={
                "title": title,
                "status": DRAFT,
                "sections": len(clean_sections),
                "stakeholders": len(clean_stakeholders),
            },
            at=timestamp,
        )
    return contract_id


def list_contracts() -> list[dict]:
    return fetch_all(
        """SELECT c.*,
                  (SELECT count(*) FROM stakeholders s WHERE s.contract_id = c.contract_id) AS stakeholder_count,
                  (SELECT count(*) FROM change_requests cr
                    WHERE cr.contract_id = c.contract_id AND cr.status IN ('PENDING', 'APPROVED')) AS open_changes,
                  (SELECT count(*) FROM clarifications cl
                    WHERE cl.contract_id = c.contract_id AND cl.status = 'OPEN') AS open_clarifications,
                  (SELECT count(*) FROM comments cm WHERE cm.contract_id = c.contract_id) AS comment_count
           FROM contracts c ORDER BY c.created_at DESC"""
    )


def get_section(section_id: str) -> dict:
    section = fetch_one("SELECT * FROM sections WHERE section_id = ?", [section_id])
    if section is None:
        raise DomainError(f"Section not found: {section_id}")
    return section


def list_sections(contract_id: str) -> list[dict]:
    return fetch_all(
        "SELECT * FROM sections WHERE contract_id = ? ORDER BY position, section_id",
        [contract_id],
    )


def list_stakeholders(contract_id: str) -> list[dict]:
    return fetch_all(
        "SELECT * FROM stakeholders WHERE contract_id = ? ORDER BY created_at, email",
        [contract_id],
    )


def list_comments(contract_id: str) -> list[dict]:
    return fetch_all("SELECT * FROM comments WHERE contract_id = ? ORDER BY created_at", [contract_id])


def list_clarifications(contract_id: str) -> list[dict]:
    return fetch_all(
        "SELECT * FROM clarifications WHERE contract_id = ? ORDER BY created_at",
        [contract_id],
    )


def list_pdfs(contract_id: str) -> list[dict]:
    return fetch_all(
        "SELECT * FROM generated_pdfs WHERE contract_id = ? ORDER BY generated_at DESC",
        [contract_id],
    )


def list_signatures(contract_id: str) -> list[dict]:
    return fetch_all(
        "SELECT * FROM signatures WHERE contract_id = ? ORDER BY created_at, signature_id",
        [contract_id],
    )


def add_stakeholder(
    contract_id: str, name: str, email: str, side: str, actor: Actor, color: str | None = None
) -> str:
    require_role(actor, {ROLE_ADMIN}, "Manage stakeholders")
    contract = get_contract(contract_id)
    # Reviewers may join while the contract is still being negotiated (including
    # PENDING_APPROVAL). Once approved / locked, the participant list is frozen.
    require_not_frozen(contract, "Add stakeholder")
    name = _clean_text(name, "Stakeholder name")
    email = _clean_text(email, "Stakeholder email").lower()
    side = str(side or "").strip().upper()
    if side not in SIDES:
        raise DomainError(f"Unknown side: {side}. Use {SIDE_BUYER} or {SIDE_VENDOR}.")
    if not _EMAIL_RE.match(email):
        raise DomainError(f"Invalid stakeholder email: {email}")
    if fetch_one("SELECT 1 FROM stakeholders WHERE contract_id = ? AND lower(email) = ?", [contract_id, email]):
        raise DomainError(f"Stakeholder email already exists: {email}")

    stakeholder_id = new_id("STK")
    used = {row["color"] for row in fetch_all("SELECT color FROM stakeholders WHERE contract_id = ?", [contract_id]) if row["color"]}
    palette = [color for color in config.REVIEW_PALETTE if color not in used] or list(config.REVIEW_PALETTE)
    color = color or palette[0]
    if color not in config.REVIEW_PALETTE:
        raise DomainError("Unknown annotation colour.")
    with tx() as con:
        sql(
            con,
            """INSERT INTO stakeholders (stakeholder_id, contract_id, name, email, role, side, color, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [stakeholder_id, contract_id, name, email, ROLE_STAKEHOLDER, side, color, now()],
        )
        sql(
            con,
            """INSERT INTO approvals (approval_id, contract_id, stakeholder_id, status, approved_at, created_at)
               VALUES (?, ?, ?, ?, NULL, ?)""",
            [new_id("APR"), contract_id, stakeholder_id, APPROVAL_PENDING, now()],
        )
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_STAKEHOLDER_ADDED,
            target_id=stakeholder_id,
            after_value={"name": name, "email": email, "side": side, "color": color, "approval": APPROVAL_PENDING},
        )
        # A new reviewer always starts with a PENDING approval, which keeps the
        # approval gate honest; re-evaluate in case the roster changed mid-approval.
        evaluate_contract_approval(con, contract_id, actor)
    return stakeholder_id


def set_stakeholder_color(contract_id: str, stakeholder_id: str, color: str, actor: Actor) -> None:
    """Admins can re-assign a reviewer's annotation colour at any time."""
    require_role(actor, {ROLE_ADMIN}, "Manage stakeholders")
    if color not in config.REVIEW_PALETTE:
        raise DomainError("Unknown annotation colour.")
    row = fetch_one(
        "SELECT * FROM stakeholders WHERE contract_id = ? AND stakeholder_id = ?",
        [contract_id, stakeholder_id],
    )
    if row is None:
        raise DomainError("Stakeholder not found.")
    if row["color"] == color:
        return
    with tx() as con:
        sql(con, "UPDATE stakeholders SET color = ? WHERE stakeholder_id = ?", [color, stakeholder_id])
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_STAKEHOLDER_COLOR_SET,
            target_id=stakeholder_id,
            before_value={"color": row["color"]},
            after_value={"color": color},
        )


def remove_stakeholder(contract_id: str, stakeholder_id: str, actor: Actor) -> None:
    require_role(actor, {ROLE_ADMIN}, "Manage stakeholders")
    contract = get_contract(contract_id)
    require_not_frozen(contract, "Remove stakeholder")
    stakeholder = fetch_one(
        "SELECT * FROM stakeholders WHERE contract_id = ? AND stakeholder_id = ?",
        [contract_id, stakeholder_id],
    )
    if stakeholder is None:
        raise DomainError("Stakeholder not found.")
    remaining = fetch_all(
        "SELECT stakeholder_id, side FROM stakeholders WHERE contract_id = ?", [contract_id]
    )
    others = [row for row in remaining if row["stakeholder_id"] != stakeholder_id]
    if len(others) < 1:
        raise DomainError("A contract must keep at least one participant.")
    other_sides = {row["side"] for row in others}
    if SIDE_BUYER not in other_sides or SIDE_VENDOR not in other_sides:
        raise DomainError(
            "A contract must keep at least one Buyer-side and one Vendor-side participant; "
            "this removal would leave only one side."
        )
    approvals = fetch_all(
        "SELECT * FROM approvals WHERE contract_id = ? AND stakeholder_id = ?",
        [contract_id, stakeholder_id],
    )
    if any(row["status"] != APPROVAL_PENDING for row in approvals):
        raise DomainError("This participant already participated in approval and cannot be removed.")

    with tx() as con:
        sql(con, "DELETE FROM approvals WHERE stakeholder_id = ?", [stakeholder_id])
        sql(con, "DELETE FROM stakeholders WHERE stakeholder_id = ?", [stakeholder_id])
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_STAKEHOLDER_REMOVED,
            target_id=stakeholder_id,
            before_value={key: stakeholder[key] for key in ("name", "email", "role")},
        )
        evaluate_contract_approval(con, contract_id, actor)


def submit_for_review(contract_id: str, actor: Actor) -> None:
    require_role(actor, {ROLE_ADMIN}, "Submit for review")
    contract = get_contract(contract_id)
    require_status(contract, {DRAFT}, "Submit for review")
    with tx() as con:
        from core.workflow import _set_status

        _set_status(con, contract, IN_REVIEW, actor, E_REVIEW_STARTED, target_id=contract_id)


def lock_contract(contract_id: str, actor: Actor) -> None:
    require_role(actor, {ROLE_ADMIN}, "Lock contract")
    contract = get_contract(contract_id)
    require_status(contract, {APPROVED}, "Lock contract")
    gate = approval_gate(contract_id)
    if not gate["ready"]:
        raise DomainError("Approval rules are not satisfied; the contract cannot be locked.")
    with tx() as con:
        from core.workflow import _set_status

        _set_status(con, contract, "LOCKED", actor, E_CONTRACT_LOCKED, target_id=contract_id)
