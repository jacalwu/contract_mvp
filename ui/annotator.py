"""Streamlit wrapper around the vanilla-JS document annotator component."""
from __future__ import annotations

import streamlit.components.v1 as components

import config

_component = components.declare_component(
    "doc_annotator", path=str(config.COMPONENT_DIR / "annotator")
)


def render_annotator(payload: dict, key: str) -> dict | None:
    """Render the document and return the last emitted action (if any)."""
    return _component(payload=payload, key=key, default=None)
