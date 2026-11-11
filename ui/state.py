"""Shared Streamlit session context: current contract, current identity, action helpers."""
from __future__ import annotations

import html
from typing import Callable

import streamlit as st

import config
from core import contracts as contract_service
from core.constants import (
    DISCUSSION_STATUSES,
    EDITABLE_STATUSES,
    FROZEN_STATUSES,
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_LABELS,
    ROLE_STAKEHOLDER,
    STATUS_LABELS,
    Actor,
    DomainError,
)
from core.contracts import list_contracts, list_stakeholders
from core.workflow import get_contract

ADMIN_ACTOR = Actor("admin", config.ADMIN_NAME, ROLE_ADMIN)

_STATUS_COLORS = {
    "DRAFT": ("#f3f4f6", "#4b5563"),
    "IN_REVIEW": ("#dbeafe", "#1d4ed8"),
    "PENDING_APPROVAL": ("#fef3c7", "#92400e"),
    "APPROVED": ("#dcfce7", "#166534"),
    "LOCKED": ("#e0e7ff", "#3730a3"),
    "SIGNING": ("#fce7f3", "#9d174d"),
    "EXECUTED": ("#d1fae5", "#065f46"),
}


def inject_styles() -> None:
    st.markdown(
        """<style>
        .diff-body { background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px;
          padding:10px 12px; line-height:1.8; font-size:14px; color:#111827;
          white-space:pre-wrap; word-break:break-word; }
        .diff-del { background:#fee2e2; color:#991b1b; text-decoration:line-through;
          border-radius:3px; padding:0 2px; }
        .diff-ins { background:#dcfce7; color:#166534; border-radius:3px;
          padding:0 2px; font-weight:600; }
        .mvp-card { background:#ffffff; border:1px solid #e5e7eb; border-radius:10px;
          padding:12px 14px; margin-bottom:10px; color:#1f2937; }
        .mvp-muted { color:#6b7280; font-size:12px; }
        .badge { display:inline-block; padding:2px 9px; border-radius:999px;
          font-size:12px; font-weight:600; }
        .timeline-item { border-left:2px solid #c7d2fe; padding:2px 0 12px 14px; margin-left:6px; }
        .timeline-time { color:#6b7280; font-size:12px; }
        </style>""",
        unsafe_allow_html=True,
    )


def status_badge(status: str) -> str:
    background, color = _STATUS_COLORS.get(status, ("#f3f4f6", "#4b5563"))
    label = STATUS_LABELS.get(status, status)
    return (
        f'<span class="badge" style="background:{background};color:{color}">'
        f"{html.escape(str(label))} · {html.escape(str(status))}</span>"
    )


def actor_label(actor_id: str | None, stakeholders: list[dict] | None = None) -> str:
    if not actor_id:
        return "—"
    if actor_id == ADMIN_ACTOR.id:
        return f"{ADMIN_ACTOR.name} (Admin)"
    for party in stakeholders or []:
        if party["stakeholder_id"] == actor_id:
            return f"{party['name']} ({ROLE_LABELS.get(party['role'], party['role'])})"
    return str(actor_id)


def can_admin(actor: Actor) -> bool:
    return actor.role == ROLE_ADMIN


def can_review(actor: Actor) -> bool:
    return actor.role in (ROLE_ADMIN, ROLE_APPROVER)


def is_editable(contract: dict) -> bool:
    return contract["status"] in EDITABLE_STATUSES


def can_discuss(contract: dict) -> bool:
    """Comments / clarifications stay open through PENDING_APPROVAL."""
    return contract["status"] in DISCUSSION_STATUSES


def _identity_options(contract_id: str) -> list[tuple[str, str]]:
    options = [(ADMIN_ACTOR.id, f"{ADMIN_ACTOR.name} · {ROLE_LABELS[ROLE_ADMIN]}")]
    for party in list_stakeholders(contract_id):
        label = f"{party['name']} · {ROLE_LABELS.get(party['role'], party['role'])}"
        options.append((party["stakeholder_id"], label))
    return options


def render_sidebar() -> None:
    with st.sidebar:
        st.title("Contract MVP")
        st.caption("Contract negotiation & approval platform")
        st.divider()
        rows = list_contracts()
        by_id = {row["contract_id"]: row for row in rows}
        if not rows:
            st.session_state["contract_id"] = None
            st.info("No contracts yet. Create one on the Dashboard.")
        else:
            ids = list(by_id)
            current_id = st.session_state.get("contract_id")
            index = ids.index(current_id) if current_id in by_id else 0
            selected = st.selectbox(
                "Current contract",
                ids,
                index=index,
                format_func=lambda cid: f"{by_id[cid]['title'][:22]} · {cid[-6:]}",
            )
            st.session_state["contract_id"] = selected
            contract = by_id[selected]
            st.markdown(status_badge(contract["status"]), unsafe_allow_html=True)
            st.caption(f"Version v{contract['version']} · {contract['stakeholder_count']} stakeholders")

            options = _identity_options(selected)
            keys = [key for key, _ in options]
            saved = st.session_state.get("actor_id")
            actor_index = keys.index(saved) if saved in keys else 0
            st.session_state["actor_id"] = st.selectbox(
                "Current identity (switch roles to test permissions)",
                keys,
                index=actor_index,
                format_func=lambda key: dict(options)[key],
            )
        st.divider()
        st.caption(
            "Flow: Draft → Review → Comments / Clarifications → Change Requests → Approval → Lock → PDF → eSignature → Executed"
        )
        st.caption("Every action is audited; no edits are allowed after locking.")


def current_actor(contract_id: str) -> Actor:
    actor_id = st.session_state.get("actor_id", ADMIN_ACTOR.id)
    if actor_id == ADMIN_ACTOR.id:
        return ADMIN_ACTOR
    for party in list_stakeholders(contract_id):
        if party["stakeholder_id"] == actor_id:
            return Actor(party["stakeholder_id"], party["name"], party["role"])
    return ADMIN_ACTOR


def get_context() -> tuple[str, Actor, dict, list[dict]]:
    """Returns (contract_id, actor, contract, stakeholders); stops the page if no contract."""
    contract_id = st.session_state.get("contract_id")
    if not contract_id:
        st.info("Create or select a contract on the Dashboard first.")
        st.stop()
    try:
        contract = get_contract(contract_id)
    except DomainError as exc:
        st.warning(str(exc))
        st.stop()
    return contract_id, current_actor(contract_id), contract, list_stakeholders(contract_id)


def run_action(action: Callable, *args, message: str = "Done", **kwargs) -> None:
    """Execute a service call, surface DomainError cleanly, then refresh the page."""
    try:
        action(*args, **kwargs)
    except DomainError as exc:
        st.error(str(exc))
    except Exception as exc:  # unexpected failure must never be silent
        st.error(f"Action failed: {type(exc).__name__}: {exc}")
    else:
        st.success(message)
        st.rerun()


def render_reviewer_manager(
    contract_id: str, actor: Actor, stakeholders: list[dict], contract: dict
) -> None:
    """Shared Admin panel for adding/removing reviewers on any open contract.

    Rendered on both the Contract Viewer (where commenting happens) and Approvals.
    """
    keep_open = bool(st.session_state.pop("reviewers_panel_open", False))
    with st.expander(f"👥 Reviewers & stakeholders ({len(stakeholders)})", expanded=keep_open):
        st.caption(
            "Everyone listed here can switch to their identity in the sidebar to read the contract, "
            "post comments and raise clarifications. Approvers can also review change requests "
            "and approve the contract."
        )
        for party in stakeholders:
            capability = (
                "Approver - approves, reviews changes, comments"
                if party["role"] == ROLE_APPROVER
                else "Stakeholder - comments & clarifies"
            )
            st.markdown(
                f"- **{html.escape(str(party['name']))}** · {html.escape(str(party['email']))} "
                f"· `{party['role']}` — {capability}"
            )

        if contract["status"] in FROZEN_STATUSES:
            st.info("This contract is approved or locked; reviewers can no longer be added or removed.")
            return
        if not can_admin(actor):
            st.info("Switch to the Admin identity in the sidebar to add or remove reviewers.")
            return

        st.caption(
            "New reviewers take effect immediately: they appear in the sidebar identity list and get "
            "a PENDING approval, so the contract cannot become APPROVED until they approve too."
        )
        columns = st.columns([2, 3, 2, 1])
        name = columns[0].text_input("Name", key="reviewer_name", placeholder="Dana Reviewer")
        email = columns[1].text_input("Email", key="reviewer_email", placeholder="dana@company.com")
        role = columns[2].selectbox(
            "Role",
            [ROLE_APPROVER, ROLE_STAKEHOLDER],
            format_func=lambda key: ROLE_LABELS[key],
            key="reviewer_role",
            help="Approvers can approve and review change requests; Stakeholders can comment and clarify. Both can comment.",
        )
        if columns[3].button("Add reviewer", key="reviewer_add", width="stretch"):
            st.session_state["reviewers_panel_open"] = True
            run_action(
                contract_service.add_stakeholder,
                contract_id,
                name,
                email,
                role,
                actor,
                message="Reviewer added. They can comment now and must approve before the contract is APPROVED.",
            )
        if stakeholders:
            remove_id = st.selectbox(
                "Remove reviewer",
                [row["stakeholder_id"] for row in stakeholders],
                format_func=lambda sid: next(
                    (f"{row['name']} · {row['email']}" for row in stakeholders if row["stakeholder_id"] == sid),
                    sid,
                ),
                key="reviewer_remove_id",
            )
            if st.button("Remove selected reviewer", key="reviewer_remove"):
                st.session_state["reviewers_panel_open"] = True
                run_action(
                    contract_service.remove_stakeholder,
                    contract_id,
                    remove_id,
                    actor,
                    message="Reviewer removed",
                )
