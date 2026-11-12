"""Status machine guards and the three approval rules from design.md."""
from __future__ import annotations

from typing import Iterable

from core.constants import (
    APPROVAL_APPROVED,
    CLARIFICATION_OPEN,
    CONTRACT_STATUSES,
    PENDING_APPROVAL,
    ROLE_ADMIN,
    ROLE_STAKEHOLDER,
    ROLES,
    SIDE_BUYER,
    SIDE_VENDOR,
    UNMERGED_CHANGE_STATUSES,
    Actor,
    DomainError,
    DISCUSSION_STATUSES,
    EDITABLE_STATUSES,
    E_CONTRACT_APPROVED,
    FROZEN_STATUSES,
)
from core.audit import log_event
from db.database import fetch_all, fetch_one, sql


def get_contract(contract_id: str) -> dict:
    contract = fetch_one("SELECT * FROM contracts WHERE contract_id = ?", [contract_id])
    if contract is None:
        raise DomainError(f"Contract not found: {contract_id}")
    return contract


def require_editable(contract: dict, action: str) -> None:
    if contract["status"] not in EDITABLE_STATUSES:
        raise DomainError(
            f"Contract status is {contract['status']}. Action not allowed: {action}. Only DRAFT / IN_REVIEW are editable; "
            "comments, clarifications and changes are frozen after locking."
        )


def require_discussion_open(contract: dict, action: str) -> None:
    """Comments and clarifications stay open through PENDING_APPROVAL so newly
    added reviewers can actually take part in the negotiation."""
    if contract["status"] not in DISCUSSION_STATUSES:
        raise DomainError(
            f"Contract status is {contract['status']}. Action not allowed: {action}. "
            "Comments and clarifications are closed once the contract is approved or locked."
        )


def require_not_frozen(contract: dict, action: str) -> None:
    """Resolving existing clarifications and merging approved changes is allowed
    while PENDING_APPROVAL, so an approval blocker can actually be cleared."""
    if contract["status"] in FROZEN_STATUSES:
        raise DomainError(f"Contract status is {contract['status']}. Action not allowed: {action}. The body is immutable once locked.")


def require_status(contract: dict, allowed: Iterable[str], action: str) -> None:
    allowed = tuple(allowed)
    if contract["status"] not in allowed:
        raise DomainError(f"Contract status is {contract['status']}. Action: {action}. Required status: {' / '.join(allowed)}.")


def require_role(actor: Actor, allowed: Iterable[str], action: str) -> None:
    allowed = tuple(allowed)
    if actor.role not in allowed or actor.role not in ROLES:
        raise DomainError(f"Permission denied: {actor.name} ({actor.role}) cannot {action}.")


def can_review_change_for(actor: Actor, proposer_side: str | None) -> bool:
    """Cross-side rule for track-changes.

    A tracked change is reviewed and merged by the *opposite* party. The Admin is
    the neutral operator and may act on either side; a change proposed by the Admin
    (no side) may be reviewed by any participant.
    """
    if actor.role == ROLE_ADMIN:
        return True
    if actor.role != ROLE_STAKEHOLDER or actor.side is None:
        return False
    if proposer_side is None:
        return True
    return actor.side != proposer_side


def _set_status(con, contract: dict, new_status: str, actor: Actor, event_type: str, target_id=None) -> None:
    if new_status not in CONTRACT_STATUSES:
        raise DomainError(f"Invalid status: {new_status}")
    sql(con, "UPDATE contracts SET status = ? WHERE contract_id = ?", [new_status, contract["contract_id"]])
    log_event(
        con,
        contract_id=contract["contract_id"],
        actor=actor,
        event_type=event_type,
        target_id=target_id,
        before_value={"status": contract["status"]},
        after_value={"status": new_status},
    )


def bump_version(con, contract: dict) -> int:
    version = int(contract["version"]) + 1
    sql(con, "UPDATE contracts SET version = ? WHERE contract_id = ?", [version, contract["contract_id"]])
    return version


def approval_gate(contract_id: str) -> dict:
    """Approval rules: no OPEN clarifications, no unmerged changes, all stakeholders approved."""
    open_clarifications = fetch_all(
        "SELECT * FROM clarifications WHERE contract_id = ? AND status = ? ORDER BY created_at",
        [contract_id, CLARIFICATION_OPEN],
    )
    placeholders = ", ".join("?" for _ in UNMERGED_CHANGE_STATUSES)
    unmerged_changes = fetch_all(
        f"SELECT * FROM change_requests WHERE contract_id = ? AND status IN ({placeholders}) ORDER BY created_at",
        [contract_id, *UNMERGED_CHANGE_STATUSES],
    )
    approvals = fetch_all(
        """SELECT a.*, s.name AS stakeholder_name, s.email, s.role, s.side
           FROM approvals a JOIN stakeholders s ON s.stakeholder_id = a.stakeholder_id
           WHERE a.contract_id = ? ORDER BY a.created_at, s.email""",
        [contract_id],
    )
    buyer_approvals = [row for row in approvals if row["side"] == SIDE_BUYER]
    vendor_approvals = [row for row in approvals if row["side"] == SIDE_VENDOR]
    buyer_ok = bool(buyer_approvals) and any(row["status"] == APPROVAL_APPROVED for row in buyer_approvals)
    vendor_ok = bool(vendor_approvals) and any(row["status"] == APPROVAL_APPROVED for row in vendor_approvals)
    both_sides_ok = buyer_ok and vendor_ok
    conditions = [
        {
            "key": "clarifications",
            "label": "No open clarifications",
            "ok": not open_clarifications,
            "detail": f"{len(open_clarifications)} open",
        },
        {
            "key": "changes",
            "label": "No unmerged change requests",
            "ok": not unmerged_changes,
            "detail": f"{len(unmerged_changes)} pending / approved",
        },
        {
            "key": "approvals",
            "label": "Each side has an approval (≥1 Buyer & ≥1 Vendor)",
            "ok": both_sides_ok,
            "detail": (
                f"Buyer {sum(1 for r in buyer_approvals if r['status'] == APPROVAL_APPROVED)}/"
                f"{len(buyer_approvals)} · Vendor "
                f"{sum(1 for r in vendor_approvals if r['status'] == APPROVAL_APPROVED)}/{len(vendor_approvals)}"
            ),
        },
    ]
    return {
        "approvals": approvals,
        "open_clarifications": open_clarifications,
        "unmerged_changes": unmerged_changes,
        "buyer_approvals": buyer_approvals,
        "vendor_approvals": vendor_approvals,
        "both_sides_ok": both_sides_ok,
        "ready": all(condition["ok"] for condition in conditions),
        "conditions": conditions,
    }


def evaluate_contract_approval(con, contract_id: str, actor: Actor) -> bool:
    """Called after each approval decision. Transitions PENDING_APPROVAL -> APPROVED when all rules hold."""
    contract = get_contract(contract_id)
    if contract["status"] != PENDING_APPROVAL:
        return False
    if not approval_gate(contract_id)["ready"]:
        return False
    _set_status(con, contract, "APPROVED", actor, E_CONTRACT_APPROVED, target_id=contract_id)
    return True
