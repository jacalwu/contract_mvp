"""Activity Timeline: immutable audit trail read from audit_events."""
from __future__ import annotations

import html
import json

import streamlit as st

from core.audit import list_events
from core.constants import EVENT_LABELS
from ui.state import get_context, status_badge


def _fmt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else str(value)


def _value_block(label: str, value: str | None) -> None:
    if value is None:
        return
    st.caption(label)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        st.text(value)
    else:
        st.json(parsed, expanded=False)


def render() -> None:
    st.title("🕒 Timeline")
    st.caption("Every action produces an audit record · from audit_events")
    contract_id, actor, contract, stakeholders = get_context()
    st.markdown(status_badge(contract["status"]), unsafe_allow_html=True)
    events = list_events(contract_id)
    st.metric("Total audit events", len(events))
    if not events:
        st.info("No audit events yet.")
        return

    columns = st.columns(2)
    event_types = sorted({row["event_type"] for row in events})
    selected_types = columns[0].multiselect(
        "Event type",
        event_types,
        default=event_types,
        format_func=lambda key: f"{EVENT_LABELS.get(key, key)} · {key}",
    )
    actors = ["All"] + sorted({row["actor"] for row in events})
    selected_actor = columns[1].selectbox("Actor", actors)

    filtered = [
        row
        for row in events
        if row["event_type"] in selected_types and (selected_actor == "All" or row["actor"] == selected_actor)
    ]
    st.caption(f"Showing {len(filtered)} / {len(events)} events")
    st.divider()

    for row in filtered:
        label = html.escape(EVENT_LABELS.get(row["event_type"], row["event_type"]))
        safe_actor = html.escape(str(row["actor"]))
        safe_event = html.escape(str(row["event_type"]))
        st.markdown(
            f'<div class="timeline-item"><span class="timeline-time">{html.escape(_fmt(row["created_at"]))}</span> '
            f'&nbsp; <strong>{safe_actor}</strong> · <strong>{label}</strong> '
            f'<code>{safe_event}</code></div>',
            unsafe_allow_html=True,
        )
        details = st.columns([3, 1])
        details[0].caption(f"Target: {row['target_id'] or '—'}")
        with details[1]:
            with st.popover("Change details"):
                _value_block("Before", row["before_value"])
                _value_block("After", row["after_value"])
