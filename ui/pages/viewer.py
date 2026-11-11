"""Contract Viewer: current sections, threaded comments and clarification Q&A."""
from __future__ import annotations

import streamlit as st

from core import contracts as contract_service
from core import discussions as discussion_service
from core.constants import CLARIFICATION_CLOSED, ROLE_ADMIN
from core.workflow import FROZEN_STATUSES
from ui.state import (
    actor_label,
    can_discuss,
    get_context,
    render_reviewer_manager,
    run_action,
    status_badge,
)


def _fmt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else str(value)


def _comment_tree(
    comments: list[dict],
    contract_id: str,
    section_id: str | None,
    actor,
    stakeholders: list[dict],
    can_add: bool,
) -> None:
    top_level = [c for c in comments if not c["parent_comment_id"]]
    replies = [c for c in comments if c["parent_comment_id"]]
    if not top_level:
        st.caption("No comments yet.")

    def render_one(comment: dict, depth: int = 0) -> None:
        prefix = "↳ " * depth
        st.caption(f"{prefix}**{actor_label(comment['author_id'], stakeholders)}** · {_fmt(comment['created_at'])}")
        st.text(comment["message"] if not depth else "  " * depth + comment["message"])
        if can_add:
            reply_key = f"reply_{comment['comment_id']}"
            value = st.text_input("Reply", key=reply_key, label_visibility="collapsed", placeholder="Write a reply...")
            if st.button("Send reply", key=f"reply_btn_{comment['comment_id']}"):
                if value.strip():
                    run_action(
                        discussion_service.add_comment,
                        contract_id,
                        section_id,
                        value,
                        actor,
                        parent_comment_id=comment["comment_id"],
                        message="Reply posted",
                    )
                else:
                    st.warning("Reply cannot be empty.")
            if actor.role == ROLE_ADMIN or comment["author_id"] == actor.id:
                columns = st.columns([1, 1, 4])
                if columns[0].button("Delete", key=f"del_{comment['comment_id']}"):
                    run_action(discussion_service.delete_comment, comment["comment_id"], actor, message="Comment deleted")
                with columns[1]:
                    with st.popover("Edit"):
                        edited = st.text_area("Edit comment", value=comment["message"], key=f"edit_{comment['comment_id']}")
                        if st.button("Save", key=f"save_{comment['comment_id']}"):
                            run_action(
                                discussion_service.update_comment,
                                comment["comment_id"],
                                edited,
                                actor,
                                message="Comment updated",
                            )
        st.divider()

    for comment in top_level:
        render_one(comment)
        for reply in replies:
            if reply["parent_comment_id"] == comment["comment_id"]:
                render_one(reply, depth=1)


def _clarification_block(
    clarifications: list[dict],
    contract_id: str,
    section_id: str | None,
    actor,
    stakeholders: list[dict],
    can_resolve: bool,
) -> None:
    if not clarifications:
        st.caption("No clarifications yet.")
    for row in clarifications:
        st.caption(
            f"**{actor_label(row['created_by'], stakeholders)}** · {_fmt(row['created_at'])} · Status `{row['status']}`"
        )
        st.text(f"Q: {row['question']}")
        if row["answer"]:
            st.text(f"A: {row['answer']}  ({actor_label(row['answered_by'], stakeholders)})")
        if can_resolve and row["status"] != CLARIFICATION_CLOSED:
            columns = st.columns([3, 1, 1])
            answer = columns[0].text_input(
                "Write an answer",
                key=f"answer_{row['clarification_id']}",
                label_visibility="collapsed",
                placeholder="Write an answer...",
            )
            if columns[1].button("Answer", key=f"answer_btn_{row['clarification_id']}"):
                if answer.strip():
                    run_action(
                        discussion_service.answer_clarification,
                        row["clarification_id"],
                        answer,
                        actor,
                        message="Clarification answered",
                    )
                else:
                    st.warning("Answer cannot be empty.")
            if columns[2].button("Close", key=f"close_btn_{row['clarification_id']}"):
                run_action(
                    discussion_service.close_clarification,
                    row["clarification_id"],
                    actor,
                    message="Clarification closed",
                )
        st.divider()


def _discussion_block(
    contract_id: str,
    section_id: str | None,
    label: str,
    actor,
    stakeholders: list[dict],
    comments: list[dict],
    clarifications: list[dict],
    can_add: bool,
    can_resolve: bool,
) -> None:
    section_comments = [c for c in comments if c["section_id"] == section_id]
    section_clarifications = [c for c in clarifications if c["section_id"] == section_id]
    with st.expander(
        f"{label} · {len(section_comments)} comments · {len(section_clarifications)} clarifications", expanded=False
    ):
        tab_comments, tab_clarifications = st.tabs(["💬 Comments / Replies", "❓ Clarifications Q&A"])
        with tab_comments:
            _comment_tree(section_comments, contract_id, section_id, actor, stakeholders, can_add)
            if can_add:
                message = st.text_input(
                    "Post comment", key=f"comment_{section_id or 'contract'}", placeholder="Write a comment..."
                )
                if st.button("Post comment", key=f"comment_btn_{section_id or 'contract'}"):
                    if message.strip():
                        run_action(
                            discussion_service.add_comment,
                            contract_id,
                            section_id,
                            message,
                            actor,
                            message="Comment posted",
                        )
                    else:
                        st.warning("Comment cannot be empty.")
        with tab_clarifications:
            _clarification_block(
                section_clarifications, contract_id, section_id, actor, stakeholders, can_resolve
            )
            if can_add:
                question = st.text_input(
                    "Raise clarification", key=f"question_{section_id or 'contract'}", placeholder="e.g. What is the liability cap?"
                )
                if st.button("Submit question", key=f"question_btn_{section_id or 'contract'}"):
                    if question.strip():
                        run_action(
                            discussion_service.add_clarification,
                            contract_id,
                            section_id,
                            question,
                            actor,
                            message="Clarification submitted",
                        )
                    else:
                        st.warning("Question cannot be empty.")


def render() -> None:
    st.title("📄 Contract Viewer")
    st.caption("Current version by section · Comments / Replies / Clarifications")
    contract_id, actor, contract, stakeholders = get_context()
    st.markdown(status_badge(contract["status"]), unsafe_allow_html=True)
    render_reviewer_manager(contract_id, actor, stakeholders, contract)
    can_add = can_discuss(contract)
    can_resolve = contract["status"] not in FROZEN_STATUSES
    if not can_add:
        st.warning("Comments and clarifications are closed once the contract is approved or locked (open during DRAFT / IN_REVIEW / PENDING_APPROVAL).")
    else:
        st.caption("Reviewers can comment and raise clarifications here. Switch identity in the sidebar to post as someone else.")

    sections = contract_service.list_sections(contract_id)
    comments = contract_service.list_comments(contract_id)
    clarifications = contract_service.list_clarifications(contract_id)

    for index, section in enumerate(sections, start=1):
        with st.container(border=True):
            st.subheader(f"{index}. {section['title']}")
            st.text(section["content"] if section["content"] else "(deleted)")
            st.caption(f"Section ID: {section['section_id']}")
        _discussion_block(
            contract_id,
            section["section_id"],
            f"Section {index} discussion",
            actor,
            stakeholders,
            comments,
            clarifications,
            can_add,
            can_resolve,
        )

    st.divider()
    _discussion_block(
        contract_id,
        None,
        "Contract-level discussion",
        actor,
        stakeholders,
        comments,
        clarifications,
        can_add,
        can_resolve,
    )
