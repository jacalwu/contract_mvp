"""Approvals: three gating rules, multi-party decisions and stakeholder management."""
from __future__ import annotations

import streamlit as st

from core import approvals as approval_service
from core.constants import (
    APPROVAL_APPROVED,
    DRAFT,
    IN_REVIEW,
    PENDING_APPROVAL,
    ROLE_LABELS,
)
from ui.state import (
    actor_label,
    can_review,
    get_context,
    render_reviewer_manager,
    run_action,
    status_badge,
)


def _fmt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else (str(value) if value else "—")


def _gate_block(contract_id: str) -> None:
    gate = approval_service.approval_summary(contract_id)
    st.subheader("Approval rules")
    for condition in gate["conditions"]:
        icon = "✅" if condition["ok"] else "⛔"
        st.markdown(f"{icon} **{condition['label']}** — `{condition['detail']}`")
    if gate["ready"]:
        st.success("All three rules are satisfied; a contract in PENDING_APPROVAL automatically becomes APPROVED.")
    else:
        st.info("A contract only reaches APPROVED when there are no OPEN clarifications, no unmerged changes, and all stakeholders have approved.")
    return gate


def render() -> None:
    st.title("✅ Approvals")
    st.caption("Multi-party approval · Status · Decision time · Stakeholder management")
    contract_id, actor, contract, stakeholders = get_context()
    st.markdown(status_badge(contract["status"]), unsafe_allow_html=True)

    if contract["status"] in (DRAFT, IN_REVIEW):
        st.info("This contract has not entered approval yet: submit for review / approval on the **Dashboard**.")
    gate = _gate_block(contract_id)

    st.divider()
    st.subheader("Stakeholder approval status")
    rows = gate["approvals"]
    if not rows:
        st.warning("No approval records yet.")
    else:
        table = [
            {
                "Stakeholders": row["stakeholder_name"],
                "Email": row["email"],
                "Role": ROLE_LABELS.get(row["role"], row["role"]),
                "Approval status": row["status"],
                "Decided by": actor_label(row["decided_by"], stakeholders),
                "Approved at": _fmt(row["approved_at"]),
            }
            for row in rows
        ]
        st.dataframe(table, hide_index=True, use_container_width=True)

    can_decide = can_review(actor) and contract["status"] == PENDING_APPROVAL
    for row in rows:
        with st.container(border=True):
            columns = st.columns([3, 2, 1, 1, 1])
            columns[0].write(f"**{row['stakeholder_name']}** · {row['email']} · `{row['role']}`")
            columns[1].write(f"`{row['status']}` · {_fmt(row['approved_at'])}")
            if not can_decide:
                continue
            if columns[2].button("Approve", key=f"grant_{row['approval_id']}", disabled=row["status"] == APPROVAL_APPROVED):
                run_action(
                    approval_service.decide_approval,
                    row["approval_id"],
                    True,
                    actor,
                    message="Approved; rules re-evaluated",
                )
            if columns[3].button("Reject", key=f"reject_{row['approval_id']}"):
                run_action(
                    approval_service.decide_approval,
                    row["approval_id"],
                    False,
                    actor,
                    message="Approval rejected for this stakeholder",
                )
            if columns[4].button(
                "Revoke", key=f"revoke_{row['approval_id']}", disabled=row["status"] != APPROVAL_APPROVED
            ):
                run_action(
                    approval_service.revoke_approval,
                    row["approval_id"],
                    actor,
                    message="Approval revoked",
                )
    if not can_decide and contract["status"] == PENDING_APPROVAL:
        st.caption("The current identity cannot approve, or the contract is not in PENDING_APPROVAL. Switch to an Approver / Admin in the sidebar.")

    st.divider()
    render_reviewer_manager(contract_id, actor, stakeholders, contract)
