"""Contract Audit History — groups Contract Viewer, Change Requests and Approvals
under a single navigation entry with three tabs."""
from __future__ import annotations

import streamlit as st

from ui.pages import approvals, change_requests, viewer


def render() -> None:
    st.title("🧾 Contract Audit History")
    st.caption("Contract Viewer · Change Requests · Approvals")

    tab_viewer, tab_changes, tab_approvals = st.tabs(
        ["📄 Contract Viewer", "✏️ Change Requests", "✅ Approvals"]
    )

    with tab_viewer:
        viewer.render()
    with tab_changes:
        change_requests.render()
    with tab_approvals:
        approvals.render()
