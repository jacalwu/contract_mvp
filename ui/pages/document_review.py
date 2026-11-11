"""Document Review: read the contract as a document and annotate it inline.

This is the review surface: reviewers select words, comment in their own colour,
reply in the margin and see tracked changes marked with author and time. The
table-style pages stay available for quick scanning and approvals.
"""
from __future__ import annotations

import streamlit as st

import config
from core import changes as change_service
from core import discussions as discussion_service
from core import editing as editing_service
from core import export as export_service
from core.constants import DomainError
from core.review import build_payload
from ui.annotator import render_annotator
from ui.state import get_context, render_reviewer_manager, status_badge


def _fmt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else str(value)


def _handle_event(event: dict, contract_id: str, actor) -> None:
    action = event.get("action")
    try:
        if action == "comment":
            discussion_service.add_comment(
                contract_id,
                event.get("section_id"),
                event.get("message", ""),
                actor,
                anchor_start=event.get("start"),
                anchor_end=event.get("end"),
                quoted_text=event.get("quote"),
                color=event.get("color"),
            )
        elif action == "reply":
            discussion_service.add_comment(
                contract_id,
                None,
                event.get("message", ""),
                actor,
                parent_comment_id=event.get("comment_id"),
                color=event.get("color"),
            )
        elif action == "resolve":
            discussion_service.set_resolved(event.get("comment_id"), bool(event.get("resolved")), actor)
        elif action == "delete_comment":
            discussion_service.delete_comment(event.get("comment_id"), actor)
        elif action == "apply_range":
            result = editing_service.apply_range(
                contract_id,
                event.get("section_id"),
                int(event.get("start") or 0),
                int(event.get("end") or 0),
                event.get("replacement", ""),
                actor,
            )
            st.session_state["review_flash"] = (
                "Edit submitted as a tracked change — an approver can accept and merge it."
                if result["mode"] == "tracked"
                else f"Clause updated (v{result.get('version', '')})."
            )
        elif action == "save_edits":
            _save_edits(event, contract_id, actor)
        elif action == "add_section":
            editing_service.add_section(
                contract_id, event.get("after_section_id"), "New section", "", actor
            )
            st.session_state["review_flash"] = "Section added — give it a title and text."
        elif action == "delete_section":
            editing_service.delete_section(event.get("section_id"), actor)
            st.session_state["review_flash"] = "Section removed."
        elif action == "move_section":
            editing_service.move_section(
                event.get("section_id"), int(event.get("direction") or 1), actor
            )
        elif action == "rename_section":
            editing_service.rename_section(event.get("section_id"), event.get("title", ""), actor)
        elif action == "review_change":
            change_service.review_change(event.get("change_id"), bool(event.get("accept")), actor)
        elif action == "merge_change":
            change_service.merge_change(event.get("change_id"), actor)
        else:
            st.warning(f"Unknown review action: {action}")
            return
    except DomainError as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # never swallow an unexpected failure
        st.error(f"Action failed: {type(exc).__name__}: {exc}")
        return
    st.rerun()


def _save_edits(event: dict, contract_id: str, actor) -> None:
    """Persist the editor buffer: direct save for document owners, otherwise a
    tracked change per edited section."""
    edits = event.get("edits") or []
    saved = 0
    tracked = 0
    for item in edits:
        section_id = item.get("section_id")
        before = item.get("before", "")
        after = item.get("after", "")
        try:
            if event.get("direct"):
                editing_service.apply_edit(contract_id, section_id, before, after, actor, "rich editor")
                saved += 1
            else:
                change_service.propose_change(
                    contract_id, section_id, "UPDATE", before, after, actor
                )
                tracked += 1
        except DomainError as exc:
            st.error(f"{exc} (section {section_id})")
    if saved:
        st.session_state["review_flash"] = f"Saved {saved} section(s) directly — version bumped."
    if tracked:
        st.session_state["review_flash"] = (
            f"{tracked} section(s) submitted as tracked changes for approval."
        )
    if not saved and not tracked:
        st.session_state["review_flash"] = "No changes to save."


def render() -> None:
    st.title("🖊️ Document Review")
    st.caption(
        "Annotate the contract itself: select any sentence, comment in your reviewer colour, "
        "reply in the margin, and see tracked changes with author and timestamp."
    )
    contract_id, actor, contract, stakeholders = get_context()
    st.markdown(status_badge(contract["status"]), unsafe_allow_html=True)
    render_reviewer_manager(contract_id, actor, stakeholders, contract)

    flash = st.session_state.pop("review_flash", None)
    if flash:
        st.success(flash)

    payload = build_payload(contract_id, actor)
    actor_color = payload["actor"]["color"]

    metrics = st.columns(4)
    open_threads = [a for a in payload["annotations"] if not a["resolved"]]
    pending_changes = [c for c in payload["changes"] if c["status"] in ("PENDING", "APPROVED")]
    metrics[0].metric("Annotations", len(payload["annotations"]))
    metrics[1].metric("Open threads", len(open_threads))
    metrics[2].metric("Tracked changes", len(pending_changes))
    metrics[3].metric("Version", f"v{payload['contract']['version']}")

    st.markdown(
        f"<div style='display:flex;align-items:center;gap:8px;margin:4px 0 10px'>"
        f"<span style='width:14px;height:14px;border-radius:50%;background:{actor_color};display:inline-block'></span>"
        f"<span style='font-size:13px;color:#374151'>You are <strong>{actor.name}</strong> · {payload['actor']['role']}"
        f" — your annotations use this colour by default</span></div>",
        unsafe_allow_html=True,
    )

    if not payload["can_annotate"]:
        st.warning(
            "This contract is approved or locked, so the document is read-only. "
            "Annotations and tracked changes are still visible."
        )
    elif not payload["can_propose"]:
        st.caption(
            "Comments stay open while the contract is pending approval. New tracked changes need the contract "
            "back in DRAFT / IN_REVIEW."
        )

    event = render_annotator(payload, key=f"annotator_{contract_id}")
    if isinstance(event, dict) and event.get("action"):
        nonce = event.get("nonce")
        if nonce and st.session_state.get("annotator_nonce") != nonce:
            st.session_state["annotator_nonce"] = nonce
            _handle_event(event, contract_id, actor)

    st.divider()
    st.subheader("Printable copies")
    st.caption(
        "Both exports carry the reviewer colours, comment footnotes and the change history. "
        "Open the HTML in a browser and press Ctrl/Cmd+P for a print-ready PDF."
    )
    markdown = export_service.build_markdown(contract_id, actor)
    printable = export_service.build_print_html(contract_id, actor)
    downloads = st.columns(3)
    downloads[0].download_button(
        "Download Markdown",
        data=markdown.encode("utf-8"),
        file_name=f"{contract_id}_review.md",
        mime="text/markdown",
        width="stretch",
    )
    downloads[1].download_button(
        "Download printable HTML",
        data=printable.encode("utf-8"),
        file_name=f"{contract_id}_review.html",
        mime="text/html",
        width="stretch",
    )
    with downloads[2]:
        st.caption(
            "The signed, versioned PDF is produced on the **PDF & Signature** page once the contract is locked."
        )
