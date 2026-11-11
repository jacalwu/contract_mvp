"""Multi-party approval and the automatic transition to APPROVED."""
from __future__ import annotations

from core.audit import log_event
from core.constants import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    DomainError,
    E_APPROVAL_GRANTED,
    E_APPROVAL_REVOKED,
    E_APPROVAL_STARTED,
    IN_REVIEW,
    PENDING_APPROVAL,
    ROLE_ADMIN,
    ROLE_APPROVER,
    Actor,
    now,
)
from core.workflow import (
    _set_status,
    approval_gate,
    evaluate_contract_approval,
    get_contract,
    require_role,
    require_status,
)
from db.database import fetch_one, sql, tx


def list_approvals(contract_id: str) -> list[dict]:
    return approval_gate(contract_id)["approvals"]


def submit_for_approval(contract_id: str, actor: Actor) -> None:
    require_role(actor, {ROLE_ADMIN, ROLE_APPROVER}, "Submit for approval")
    contract = get_contract(contract_id)
    require_status(contract, {IN_REVIEW}, "Submit for approval")
    with tx() as con:
        _set_status(con, contract, PENDING_APPROVAL, actor, E_APPROVAL_STARTED, target_id=contract_id)
        evaluate_contract_approval(con, contract_id, actor)


def decide_approval(approval_id: str, approved: bool, actor: Actor) -> bool:
    """Grant or reject one stakeholder approval. Returns True if the contract became APPROVED."""
    require_role(actor, {ROLE_APPROVER, ROLE_ADMIN}, "Approve contract")
    row = fetch_one("SELECT * FROM approvals WHERE approval_id = ?", [approval_id])
    if row is None:
        raise DomainError("Approval record not found.")
    contract = get_contract(row["contract_id"])
    require_status(contract, {PENDING_APPROVAL}, "Record approval decision")
    if approved and row["status"] == APPROVAL_APPROVED:
        raise DomainError("This stakeholder already approved.")

    new_status = APPROVAL_APPROVED if approved else APPROVAL_REJECTED
    with tx() as con:
        sql(
            con,
            "UPDATE approvals SET status = ?, decided_by = ?, approved_at = ? WHERE approval_id = ?",
            [new_status, actor.id, now() if approved else None, approval_id],
        )
        log_event(
            con,
            contract_id=row["contract_id"],
            actor=actor,
            event_type=E_APPROVAL_GRANTED if approved else E_APPROVAL_REVOKED,
            target_id=approval_id,
            before_value={"status": row["status"]},
            after_value={"status": new_status, "stakeholder_id": row["stakeholder_id"]},
        )
        return evaluate_contract_approval(con, row["contract_id"], actor)


def revoke_approval(approval_id: str, actor: Actor) -> None:
    require_role(actor, {ROLE_APPROVER, ROLE_ADMIN}, "Revoke approval")
    row = fetch_one("SELECT * FROM approvals WHERE approval_id = ?", [approval_id])
    if row is None:
        raise DomainError("Approval record not found.")
    if row["status"] != APPROVAL_APPROVED:
        raise DomainError("Only approved records can be revoked.")
    contract = get_contract(row["contract_id"])
    require_status(contract, {PENDING_APPROVAL}, "Revoke approval")
    with tx() as con:
        sql(
            con,
            "UPDATE approvals SET status = ?, decided_by = NULL, approved_at = NULL WHERE approval_id = ?",
            [APPROVAL_PENDING, approval_id],
        )
        log_event(
            con,
            contract_id=row["contract_id"],
            actor=actor,
            event_type=E_APPROVAL_REVOKED,
            target_id=approval_id,
            before_value={"status": row["status"]},
            after_value={"status": APPROVAL_PENDING},
        )


def approval_summary(contract_id: str) -> dict:
    return approval_gate(contract_id)
