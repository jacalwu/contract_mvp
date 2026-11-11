"""Builds the payload the document-review component renders.

The component never reads the database: the page assembles a plain JSON document
(sections, inline annotations, tracked changes, revision marks) and passes it in.
Keeping this in the service layer means the same snapshot can also drive the
Markdown / printable export.
"""
from __future__ import annotations

import difflib

import config
from core import contracts as contract_service
from core import discussions as discussion_service
from core import changes as change_service
from core.constants import (
    CHANGE_APPROVED,
    CHANGE_MERGED,
    CHANGE_PENDING,
    DISCUSSION_STATUSES,
    EDITABLE_STATUSES,
    FROZEN_STATUSES,
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_LABELS,
    STATUS_LABELS,
    Actor,
)
from db.database import fetch_all, fetch_one


def _fmt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else (str(value) if value else "")


def _people(contract_id: str, actor: Actor) -> dict[str, dict]:
    """author_id -> display name, role and colour."""
    people: dict[str, dict] = {
        "admin": {
            "name": config.ADMIN_NAME,
            "role": ROLE_ADMIN,
            "role_label": ROLE_LABELS[ROLE_ADMIN],
            "color": config.ADMIN_COLOR,
        }
    }
    for index, party in enumerate(contract_service.list_stakeholders(contract_id)):
        people[party["stakeholder_id"]] = {
            "name": party["name"],
            "role": party["role"],
            "role_label": ROLE_LABELS.get(party["role"], party["role"]),
            "color": party["color"] or config.REVIEW_PALETTE[index % len(config.REVIEW_PALETTE)],
        }
    if actor.id not in people:
        people[actor.id] = {"name": actor.name, "role": actor.role, "role_label": actor.role, "color": config.ADMIN_COLOR}
    return people


def diff_ops(before: str, after: str) -> list[dict]:
    """Character level diff as structured operations, never HTML."""
    matcher = difflib.SequenceMatcher(None, before or "", after or "")
    ops: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            ops.append({"op": "eq", "text": (before or "")[i1:i2]})
        elif tag == "delete":
            ops.append({"op": "del", "text": (before or "")[i1:i2]})
        elif tag == "insert":
            ops.append({"op": "ins", "text": (after or "")[j1:j2]})
        else:
            ops.append({"op": "del", "text": (before or "")[i1:i2]})
            ops.append({"op": "ins", "text": (after or "")[j1:j2]})
    return [op for op in ops if op["text"]]


def _revision_marks(sections: list[dict], merged: list[dict], people: dict[str, dict]) -> dict[str, list[dict]]:
    """Locate the text each merged change introduced, so the document can mark it
    "changed by X on date" like Word's track-changes margin."""
    marks: dict[str, list[dict]] = {}
    content_by_section = {section["section_id"]: section["content"] or "" for section in sections}
    for change in merged:
        content = content_by_section.get(change["section_id"])
        if content is None:
            continue
        person = people.get(change["requested_by"], {"name": change["requested_by"], "color": config.ADMIN_COLOR})
        inserted = [op["text"] for op in diff_ops(change["before_content"], change["after_content"]) if op["op"] == "ins"]
        cursor = 0
        for fragment in inserted:
            probe = fragment.strip()
            if len(probe) < 3:
                cursor += len(fragment)
                continue
            found = content.find(probe, cursor)
            if found < 0:
                continue
            marks.setdefault(change["section_id"], []).append(
                {
                    "start": found,
                    "end": found + len(probe),
                    "author": person["name"],
                    "color": person.get("color", config.ADMIN_COLOR),
                    "when": _fmt(change.get("merged_at") or change["created_at"]),
                    "kind": change["operation"],
                    "label": f"Revised by {person['name']} on {_fmt(change.get('merged_at') or change['created_at'])}",
                }
            )
            cursor = found + len(probe)
    return marks


def build_payload(contract_id: str, actor: Actor) -> dict:
    contract = contract_service.get_contract(contract_id)
    stakeholders = contract_service.list_stakeholders(contract_id)
    people = _people(contract_id, actor)
    sections = contract_service.list_sections(contract_id)
    threads = discussion_service.list_threads(contract_id)
    change_rows = change_service.list_changes(contract_id)

    annotations = []
    for thread in threads:
        author = people.get(thread["author_id"], {"name": thread["author_id"], "color": config.ADMIN_COLOR})
        threads_payload = []
        for reply in thread["replies"]:
            reply_author = people.get(reply["author_id"], {"name": reply["author_id"], "color": config.ADMIN_COLOR})
            threads_payload.append(
                {
                    "id": reply["comment_id"],
                    "author": reply_author["name"],
                    "color": reply_author.get("color", config.ADMIN_COLOR),
                    "text": reply["message"],
                    "when": _fmt(reply["created_at"]),
                    "own": reply["author_id"] == actor.id or actor.role == ROLE_ADMIN,
                }
            )
        annotations.append(
            {
                "id": thread["comment_id"],
                "section_id": thread["section_id"],
                "start": thread["anchor_start"],
                "end": thread["anchor_end"],
                "quote": thread["quoted_text"] or "",
                "author": author["name"],
                "author_role": ROLE_LABELS.get(author.get("role"), author.get("role", "")),
                "color": thread["color"] or author.get("color", config.ADMIN_COLOR),
                "text": thread["message"],
                "when": _fmt(thread["created_at"]),
                "resolved": bool(thread["resolved_at"]),
                "own": thread["author_id"] == actor.id or actor.role == ROLE_ADMIN,
                "replies": threads_payload,
            }
        )

    merged = [row for row in change_rows if row["status"] == CHANGE_MERGED]
    revision_marks = _revision_marks(sections, merged, people)

    change_payload = []
    for row in change_rows:
        author = people.get(row["requested_by"], {"name": row["requested_by"], "color": config.ADMIN_COLOR})
        change_payload.append(
            {
                "id": row["change_id"],
                "section_id": row["section_id"],
                "section_title": row.get("section_title") or "",
                "operation": row["operation"],
                "status": row["status"],
                "author": author["name"],
                "color": author.get("color", config.ADMIN_COLOR),
                "when": _fmt(row["created_at"]),
                "reviewed_by": people.get(row["reviewed_by"], {}).get("name", "") if row["reviewed_by"] else "",
                "merged_at": _fmt(row["merged_at"]),
                "before": row["before_content"],
                "after": row["after_content"],
                "diff": diff_ops(row["before_content"], row["after_content"]),
            }
        )

    can_annotate = contract["status"] in DISCUSSION_STATUSES
    can_propose = contract["status"] in EDITABLE_STATUSES
    can_review_changes = contract["status"] not in FROZEN_STATUSES and actor.role in (ROLE_APPROVER, ROLE_ADMIN)
    return {
        "contract": {
            "id": contract_id,
            "title": contract["title"],
            "status": contract["status"],
            "status_label": STATUS_LABELS.get(contract["status"], contract["status"]),
            "version": contract["version"],
        },
        "sections": [
            {"id": section["section_id"], "title": section["title"], "content": section["content"] or ""}
            for section in sections
        ],
        "annotations": annotations,
        "changes": change_payload,
        "revisions": revision_marks,
        "people": [
            {
                "id": key,
                "name": value["name"],
                "role": value.get("role_label", value.get("role", "")),
                "color": value["color"],
            }
            for key, value in people.items()
        ],
        "actor": {
            "id": actor.id,
            "name": actor.name,
            "role": ROLE_LABELS.get(actor.role, actor.role),
            "color": people.get(actor.id, {}).get("color", config.ADMIN_COLOR),
        },
        "palette": [{"color": color, "label": config.PALETTE_LABELS.get(color, color)} for color in config.REVIEW_PALETTE],
        "can_annotate": can_annotate,
        "can_propose": can_propose,
        "can_review_changes": can_review_changes,
        "stakeholders": len(stakeholders),
    }


def pending_changes_by_section(contract_id: str) -> dict[str, int]:
    rows = fetch_all(
        "SELECT section_id, status, count(*) AS total FROM change_requests WHERE contract_id = ? GROUP BY section_id, status",
        [contract_id],
    )
    result: dict[str, int] = {}
    for row in rows:
        if row["status"] in (CHANGE_PENDING, CHANGE_APPROVED):
            result[row["section_id"]] = result.get(row["section_id"], 0) + row["total"]
    return result


def section_content(section_id: str) -> str:
    row = fetch_one("SELECT content FROM sections WHERE section_id = ?", [section_id])
    return (row or {}).get("content", "") or ""
