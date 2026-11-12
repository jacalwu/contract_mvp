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
    ROLE_LABELS,
    ROLE_STAKEHOLDER,
    SIDE_BUYER,
    SIDE_LABELS,
    SIDE_VENDOR,
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
            side = SIDE_LABELS.get(party.get("side"), party.get("side", ""))
            return f"{party['name']} ({side})"
    return str(actor_id)


def can_admin(actor: Actor) -> bool:
    return actor.role == ROLE_ADMIN


def is_participant(actor: Actor) -> bool:
    return actor.role == ROLE_STAKEHOLDER and actor.side is not None


def can_review(actor: Actor) -> bool:
    """A participant or the Admin can act on reviews/approvals in general; the
    actual per-change / per-approval rule is enforced by the service layer."""
    return actor.role in (ROLE_ADMIN, ROLE_STAKEHOLDER)


def is_editable(contract: dict) -> bool:
    return contract["status"] in EDITABLE_STATUSES


def can_discuss(contract: dict) -> bool:
    """Comments / clarifications stay open through PENDING_APPROVAL."""
    return contract["status"] in DISCUSSION_STATUSES


def _identity_options(contract_id: str) -> list[tuple[str, str]]:
    options = [(ADMIN_ACTOR.id, f"{ADMIN_ACTOR.name} · Admin")]
    for party in list_stakeholders(contract_id):
        side = SIDE_LABELS.get(party.get("side"), party.get("side", ""))
        options.append((party["stakeholder_id"], f"{party['name']} · {side}"))
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
            return Actor(party["stakeholder_id"], party["name"], party["role"], party["side"])
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
    contract_id: str, actor: Actor, stakeholders: list[dict], contract: dict, key_prefix: str = "review"
) -> None:
    """Shared Admin panel for adding/removing reviewers on any open contract.

    Rendered on the Contract Viewer, the Approvals tab and the Document Review page.
    Because Streamlit executes every tab body in one run, callers pass a distinct
    ``key_prefix`` so the widget keys never collide.
    """
    keep_open = bool(st.session_state.pop("reviewers_panel_open", False))
    with st.expander(f"👥 Reviewers & stakeholders ({len(stakeholders)})", expanded=keep_open):
        st.caption(
            "Everyone listed here can switch to their identity in the sidebar to read the contract, "
            "post comments, raise clarifications and propose clause changes. A change proposed by one "
            "side can only be accepted and merged by the opposite side, and the contract is APPROVED "
            "once each side has at least one approval."
        )
        for party in stakeholders:
            side_label = SIDE_LABELS.get(party.get("side"), party.get("side", ""))
            capability = (
                "Buyer-side participant — proposes & reviews changes, comments, approves their side"
                if party.get("side") == SIDE_BUYER
                else "Vendor-side participant — proposes & reviews changes, comments, approves their side"
            )
            swatch = (
                f"<span style='display:inline-block;width:10px;height:10px;border-radius:50%;"
                f"background:{html.escape(str(party['color'] or '#9ca3af'))};margin-right:5px'></span>"
            )
            st.markdown(
                f"- {swatch}**{html.escape(str(party['name']))}** · {html.escape(str(party['email']))} "
                f"· `{side_label}` — {capability}"
            )

        if contract["status"] in FROZEN_STATUSES:
            st.info("This contract is approved or locked; participants can no longer be added or removed.")
            return
        if not can_admin(actor):
            st.info("Switch to the Admin identity in the sidebar to add or remove participants.")
            return

        st.caption(
            "New participants take effect immediately: they appear in the sidebar identity list and get "
            "a PENDING approval, so the contract cannot become APPROVED until both sides have an approval."
        )
        columns = st.columns([2, 3, 2, 1])
        name = columns[0].text_input("Name", key=f"{key_prefix}_reviewer_name", placeholder="Dana Participant")
        email = columns[1].text_input("Email", key=f"{key_prefix}_reviewer_email", placeholder="dana@company.com")
        side = columns[2].selectbox(
            "Side",
            [SIDE_BUYER, SIDE_VENDOR],
            format_func=lambda key: SIDE_LABELS[key],
            key=f"{key_prefix}_reviewer_side",
            help="Buyer-side or Vendor-side. Each side proposes changes that only the other side can accept.",
        )
        if columns[3].button("Add participant", key=f"{key_prefix}_reviewer_add", width="stretch"):
            st.session_state["reviewers_panel_open"] = True
            run_action(
                contract_service.add_stakeholder,
                contract_id,
                name,
                email,
                side,
                actor,
                message="Participant added. They can comment now; the contract needs an approval from each side to be APPROVED.",
            )
        if stakeholders:
            remove_id = st.selectbox(
                "Remove reviewer",
                [row["stakeholder_id"] for row in stakeholders],
                format_func=lambda sid: next(
                    (f"{row['name']} · {row['email']}" for row in stakeholders if row["stakeholder_id"] == sid),
                    sid,
                ),
                key=f"{key_prefix}_reviewer_remove_id",
            )
            if st.button("Remove selected reviewer", key=f"{key_prefix}_reviewer_remove"):
                st.session_state["reviewers_panel_open"] = True
                run_action(
                    contract_service.remove_stakeholder,
                    contract_id,
                    remove_id,
                    actor,
                    message="Reviewer removed",
                )

            st.divider()
            st.caption(
                "Annotation colour: each reviewer writes in their own colour, which is how the document "
                "tells comments apart."
            )
            color_columns = st.columns([2, 2, 1])
            color_target = color_columns[0].selectbox(
                "Reviewer",
                [row["stakeholder_id"] for row in stakeholders],
                format_func=lambda sid: next(
                    (row["name"] for row in stakeholders if row["stakeholder_id"] == sid), sid
                ),
                key=f"{key_prefix}_reviewer_color_target",
            )
            current_color = next(
                (row["color"] for row in stakeholders if row["stakeholder_id"] == color_target),
                config.REVIEW_PALETTE[0],
            )
            palette = list(config.REVIEW_PALETTE)
            new_color = color_columns[1].selectbox(
                "Colour",
                palette,
                index=palette.index(current_color) if current_color in palette else 0,
                format_func=lambda value: config.PALETTE_LABELS.get(value, value),
                key=f"{key_prefix}_reviewer_color_value",
            )
            if color_columns[2].button("Apply", key=f"{key_prefix}_reviewer_color_apply", width="stretch"):
                st.session_state["reviewers_panel_open"] = True
                run_action(
                    contract_service.set_stakeholder_color,
                    contract_id,
                    color_target,
                    new_color,
                    actor,
                    message="Annotation colour updated",
                )
