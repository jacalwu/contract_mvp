"""Dashboard: contract list, creation, demo data and workflow transitions."""
from __future__ import annotations

import streamlit as st

from core import approvals as approval_service
from core import contracts as contract_service
from core.constants import (
    APPROVED,
    DRAFT,
    IN_REVIEW,
    LOCKED,
    PENDING_APPROVAL,
    STATUS_LABELS,
    DomainError,
)
from ui.state import ADMIN_ACTOR, can_admin, can_review, current_actor, get_context, run_action, status_badge

DEFAULT_SECTIONS = """Scope of Work | Vendor shall deliver the analytics platform described in Appendix A.
Payment Terms | Payment within 30 days.
Liability Cap | Total liability shall not exceed fees paid in the preceding 12 months."""

DEFAULT_STAKEHOLDERS = """Alice Chen, alice@party-a.com, APPROVER
Bob Lee, bob@party-b.com, STAKEHOLDER
Carol Wong, carol@legal.com, APPROVER"""


def _parse_sections(raw: str) -> list[dict[str, str]]:
    items = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        title, content = line.split("|", 1)
        items.append({"title": title.strip(), "content": content.strip()})
    return items


def _parse_stakeholders(raw: str) -> list[dict[str, str]]:
    items = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            raise DomainError(f"Invalid stakeholder line: {line} (expected: name, email, role)")
        items.append(
            {
                "name": parts[0],
                "email": parts[1],
                "role": (parts[2] if len(parts) > 2 else "APPROVER").upper(),
            }
        )
    return items


def _create_block(actor) -> None:
    with st.expander("➕ New contract (Admin)", expanded=False):
        if not can_admin(actor):
            st.info("Only an Admin can create contracts. Switch to the Admin identity in the sidebar.")
            return
        title = st.text_input("Contract title", value="Master Service Agreement", key="new_contract_title")
        st.caption("Sections: one per line, `Title | Content`")
        sections_raw = st.text_area("Sections", value=DEFAULT_SECTIONS, height=110, key="new_sections")
        st.caption("Stakeholders: one per line, `name, email, role(APPROVER/STAKEHOLDER)`; at least one APPROVER")
        stakeholders_raw = st.text_area(
            "Stakeholders", value=DEFAULT_STAKEHOLDERS, height=90, key="new_stakeholders"
        )
        columns = st.columns(2)
        with columns[0]:
            if st.button("Create contract", type="primary", width="stretch"):
                try:
                    contract_id = contract_service.create_contract(
                        title, _parse_sections(sections_raw), _parse_stakeholders(stakeholders_raw), actor
                    )
                except DomainError as exc:
                    st.error(str(exc))
                else:
                    st.session_state["contract_id"] = contract_id
                    st.success(f"Contract created: {contract_id}")
                    st.rerun()
        with columns[1]:
            if st.button("Load demo contract", width="stretch"):
                contract_id = contract_service.create_contract(
                    "Master Service Agreement",
                    _parse_sections(DEFAULT_SECTIONS),
                    _parse_stakeholders(DEFAULT_STAKEHOLDERS),
                    actor,
                )
                st.session_state["contract_id"] = contract_id
                st.success(f"Demo contract created: {contract_id}")
                st.rerun()


def _flow_block(contract_id: str, contract: dict, actor) -> None:
    st.subheader("Workflow")
    status = contract["status"]
    columns = st.columns(3)
    with columns[0]:
        if status == DRAFT:
            if can_admin(actor):
                if st.button("Submit for review → IN_REVIEW", width="stretch"):
                    run_action(contract_service.submit_for_review, contract_id, actor, message="Moved to IN_REVIEW")
            else:
                st.caption("Waiting for an Admin to submit for review")
    with columns[1]:
        if status == IN_REVIEW:
            if can_review(actor):
                if st.button("Submit for approval → PENDING_APPROVAL", width="stretch"):
                    run_action(approval_service.submit_for_approval, contract_id, actor, message="Submitted for multi-party approval")
            else:
                st.caption("Waiting for an Approver to submit for approval")
    with columns[2]:
        if status == APPROVED:
            if can_admin(actor):
                if st.button("Lock contract → LOCKED", type="primary", width="stretch"):
                    run_action(contract_service.lock_contract, contract_id, actor, message="Contract locked; the body is now immutable")
            else:
                st.caption("Waiting for an Admin to lock the contract")

    hints = {
        PENDING_APPROVAL: "Approval in progress: finish all decisions on **Approvals**. Once all three rules are satisfied the contract automatically becomes APPROVED.",
        LOCKED: "Contract locked: generate the final PDF and send for signature on **PDF & Signature**.",
        "SIGNING": "Signing in progress: complete all signatures on **PDF & Signature**.",
        "EXECUTED": "The contract is executed. View the full audit trail on **Timeline**.",
    }
    if status in hints:
        st.info(hints[status])


def render() -> None:
    st.title("📊 Dashboard")
    st.caption("Contracts · Status · Version · Stakeholders")
    rows = contract_service.list_contracts()
    if not rows:
        create_actor = ADMIN_ACTOR
    else:
        valid_ids = [row["contract_id"] for row in rows]
        selected = st.session_state.get("contract_id")
        create_actor = current_actor(selected if selected in valid_ids else valid_ids[0])
    _create_block(create_actor)

    if rows:
        display = [
            {
                "Contract ID": row["contract_id"],
                "Title": row["title"],
                "Status": row["status"],
                "Version": f"v{row['version']}",
                "Stakeholders": row["stakeholder_count"],
                "Open changes": row["open_changes"],
                "Open clarifications": row["open_clarifications"],
                "Comments": row["comment_count"],
                "Created": row["created_at"],
            }
            for row in rows
        ]
        st.dataframe(display, hide_index=True, width="stretch")
        st.caption("Select a contract and an identity in the sidebar.")

    contract_id, actor, contract, stakeholders = get_context()
    st.divider()
    st.subheader(f"Current contract: {contract['title']}")
    st.markdown(status_badge(contract["status"]), unsafe_allow_html=True)
    info = st.columns(4)
    info[0].metric("Version", f"v{contract['version']}")
    info[1].metric("Status", STATUS_LABELS.get(contract["status"], contract["status"]))
    info[2].metric("Stakeholders", len(stakeholders))
    info[3].metric("Contract ID", contract_id[-8:])
    with st.expander("Stakeholder list", expanded=False):
        for party in stakeholders:
            st.write(f"- **{party['name']}** · {party['email']} · `{party['role']}`")
        st.caption(
            "Need more reviewers? Use **Reviewers & stakeholders** on the Contract Viewer "
            "or Approvals page (Admin identity). New reviewers can comment immediately."
        )
    _flow_block(contract_id, contract, actor)
