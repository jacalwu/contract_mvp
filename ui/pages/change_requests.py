"""Change Requests: INSERT / UPDATE / DELETE proposals, Word-like diff, review and merge."""
from __future__ import annotations

import streamlit as st

from core import changes as change_service
from core import contracts as contract_service
from core.constants import (
    CHANGE_APPROVED,
    CHANGE_MERGED,
    CHANGE_PENDING,
    CHANGE_REJECTED,
    OPERATIONS,
    OPERATION_LABELS,
    DomainError,
)
from core.workflow import FROZEN_STATUSES
from ui.state import actor_label, can_review, get_context, is_editable, run_action, status_badge

_CHANGE_STYLES = {
    CHANGE_PENDING: ("#fef3c7", "#92400e"),
    CHANGE_APPROVED: ("#dbeafe", "#1d4ed8"),
    CHANGE_REJECTED: ("#fee2e2", "#991b1b"),
    CHANGE_MERGED: ("#dcfce7", "#166534"),
}


def _fmt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else str(value)


def _proposal_form(contract_id: str, sections: list[dict], actor) -> None:
    with st.container(border=True):
        st.subheader("Submit change request")
        st.caption("All edits are submitted as Change Requests and merged by an Approver; clause text is never edited directly. Press Ctrl+Enter (or click outside) to apply text box input.")
        if not sections:
            st.warning("This contract has no sections.")
            return
        section_id = st.selectbox(
            "Target section",
            [section["section_id"] for section in sections],
            format_func=lambda sid: next((f"{i}. {s['title']}" for i, s in enumerate(sections, 1) if s["section_id"] == sid), sid),
            key="cr_section",
        )
        section = next(item for item in sections if item["section_id"] == section_id)
        before = section["content"]
        operation = st.radio("Operation", list(OPERATIONS), format_func=lambda op: f"{op} · {OPERATION_LABELS[op]}", horizontal=True)
        st.caption("Current text")
        st.text(before)

        # distinct widget keys so switching operation always shows the right default text
        if operation == "INSERT":
            user_input = st.text_area(
                "Text to insert", key=f"cr_input_{operation}", placeholder="Late payment incurs 2% interest."
            )
        elif operation == "UPDATE":
            user_input = st.text_area(
                "Full revised clause text", value=before, height=120, key=f"cr_input_{operation}"
            )
        else:
            user_input = st.text_area(
                "Exact original fragment to delete", value=before, height=120, key=f"cr_input_{operation}"
            )

        after = None
        try:
            after = change_service.build_after_content(operation, before, user_input)
            st.caption("Preview (Track Changes)")
            st.markdown(change_service.diff_html(before, after), unsafe_allow_html=True)
        except DomainError as exc:
            st.warning(str(exc))

        if st.button("Submit change request", type="primary", disabled=after is None):
            run_action(
                change_service.propose_change,
                contract_id,
                section_id,
                operation,
                before,
                after,
                actor,
                message="Change request submitted; awaiting review",
            )


def _change_card(
    row: dict,
    actor,
    stakeholders: list[dict],
    contract_frozen: bool,
) -> None:
    background, color = _CHANGE_STYLES.get(row["status"], ("#f3f4f6", "#4b5563"))
    with st.container(border=True):
        columns = st.columns([2, 2, 3, 3])
        columns[0].markdown(
            f'<span class="badge" style="background:#e0e7ff;color:#3730a3">{row["operation"]} · {OPERATION_LABELS.get(row["operation"], "")}</span>',
            unsafe_allow_html=True,
        )
        columns[1].markdown(
            f'<span class="badge" style="background:{background};color:{color}">{row["status"]}</span>',
            unsafe_allow_html=True,
        )
        columns[2].caption(f"Requested by: {actor_label(row['requested_by'], stakeholders)}")
        columns[3].caption(f"Section: {row['section_title'] or '—'} · {_fmt(row['created_at'])}")

        st.markdown(change_service.diff_html(row["before_content"], row["after_content"]), unsafe_allow_html=True)
        with st.expander("Line-by-line -/+ view"):
            for sign, line in change_service.diff_lines(row["before_content"], row["after_content"]):
                st.text(f"{sign} {line}")

        if contract_frozen or not can_review(actor):
            return
        buttons = st.columns([1, 1, 1, 5])
        if row["status"] == CHANGE_PENDING:
            if buttons[0].button("Accept", key=f"accept_{row['change_id']}"):
                run_action(change_service.review_change, row["change_id"], True, actor, message="Change accepted")
            if buttons[1].button("Reject", key=f"reject_{row['change_id']}"):
                run_action(change_service.review_change, row["change_id"], False, actor, message="Change rejected")
        elif row["status"] == CHANGE_APPROVED:
            if buttons[2].button("Merge into text → version +1", type="primary", key=f"merge_{row['change_id']}"):
                run_action(change_service.merge_change, row["change_id"], actor, message="Change merged; contract version updated")
        elif row["status"] == CHANGE_MERGED:
            st.caption(f"Merged at: {_fmt(row['merged_at'])}")


def render() -> None:
    st.title("✏️ Change Requests")
    st.caption("Track Changes · INSERT / UPDATE / DELETE · Diff review · Merge")
    contract_id, actor, contract, stakeholders = get_context()
    st.markdown(status_badge(contract["status"]), unsafe_allow_html=True)
    can_add = is_editable(contract)
    frozen = contract["status"] in FROZEN_STATUSES

    if can_add:
        _proposal_form(contract_id, contract_service.list_sections(contract_id), actor)
    else:
        st.warning("This status does not allow new change requests (only DRAFT / IN_REVIEW). Approved changes can still be merged during PENDING_APPROVAL.")

    st.divider()
    st.subheader("Change requests")
    rows = change_service.list_changes(contract_id)
    if not rows:
        st.info("No change requests yet.")
        return
    if not can_review(actor):
        st.caption("You are viewing as a Stakeholder (read-only). Switch to an Approver / Admin identity to review and merge.")
    for row in rows:
        _change_card(row, actor, stakeholders, frozen)
