"""Streamlit entry point.

Run:  python -m streamlit run app.py
"""
from __future__ import annotations

import streamlit as st

from ui.pages import (
    approvals,
    change_requests,
    dashboard,
    document_review,
    pdf_signature,
    timeline,
    viewer,
)
from ui.state import inject_styles, render_sidebar

st.set_page_config(
    page_title="Contract Negotiation & Approval MVP",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

inject_styles()
render_sidebar()

pages = [
    st.Page(dashboard.render, title="Dashboard", icon="📊", default=True, url_path="dashboard"),
    st.Page(document_review.render, title="Document Review", icon="🖊️", url_path="review"),
    st.Page(viewer.render, title="Contract Viewer", icon="📄", url_path="viewer"),
    st.Page(change_requests.render, title="Change Requests", icon="✏️", url_path="changes"),
    st.Page(approvals.render, title="Approvals", icon="✅", url_path="approvals"),
    st.Page(timeline.render, title="Timeline", icon="🕒", url_path="timeline"),
    st.Page(pdf_signature.render, title="PDF & Signature", icon="📑", url_path="pdf-signature"),
]

st.navigation(pages).run()
