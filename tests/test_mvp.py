"""End-to-end MVP lifecycle test: create -> review -> discuss -> track changes ->
approve -> lock -> PDF -> eSignature -> executed, plus guard/boundary assertions.

Run with:  python -m pytest tests/test_mvp.py -q
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

_TMP = Path(tempfile.mkdtemp(prefix="contract_mvp_test_"))
os.environ["MVP_DB_PATH"] = str(_TMP / "contracts.duckdb")
os.environ["MVP_DATA_DIR"] = str(_TMP)
os.environ["MVP_PDF_DIR"] = str(_TMP / "pdfs")
os.environ["MVP_SIGNATURE_PROVIDER"] = "mock"
os.environ["DROPBOX_SIGN_API_KEY"] = ""
os.environ["MVP_PDF_ENGINE"] = "auto"

import pytest  # noqa: E402

from core import approvals, changes, contracts, discussions, editing, export, pdf, review, richtext, signing  # noqa: E402
from core.audit import list_events  # noqa: E402
from core.constants import (  # noqa: E402
    APPROVED,
    DRAFT,
    E_CHANGE_MERGED,
    E_CONTRACT_CREATED,
    E_CONTRACT_EXECUTED,
    E_CONTRACT_LOCKED,
    E_PDF_GENERATED,
    E_SIGNATURE_REQUESTED,
    EXECUTED,
    IN_REVIEW,
    LOCKED,
    OP_DELETE,
    OP_INSERT,
    OP_UPDATE,
    PENDING_APPROVAL,
    PROVIDER_MOCK,
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_STAKEHOLDER,
    SIGNATURE_SIGNED,
    SIGNING,
    Actor,
    DomainError,
)
from core.workflow import approval_gate, get_contract  # noqa: E402
from db.database import fetch_all  # noqa: E402

ADMIN = Actor("admin", "Admin", ROLE_ADMIN)

SECTIONS = [
    {"title": "Scope of Work", "content": "Vendor shall deliver the analytics platform described in Appendix A."},
    {"title": "Payment Terms", "content": "Payment within 30 days."},
    {"title": "Liability Cap", "content": "Total liability shall not exceed fees paid in the last 12 months."},
]
STAKEHOLDERS = [
    {"name": "Alice Chen", "email": "alice@party-a.com", "role": ROLE_APPROVER},
    {"name": "Bob Lee", "email": "bob@party-b.com", "role": ROLE_STAKEHOLDER},
    {"name": "Carol Wong", "email": "carol@legal.com", "role": ROLE_APPROVER},
]


def _actors(contract_id: str) -> list[Actor]:
    return [
        Actor(row["stakeholder_id"], row["name"], row["role"])
        for row in contracts.list_stakeholders(contract_id)
    ]


def _actor_by_role(contract_id: str, role: str) -> Actor:
    return next(actor for actor in _actors(contract_id) if actor.role == role)


def _approve_all(contract_id: str) -> None:
    for row in approvals.list_approvals(contract_id):
        approvals.decide_approval(row["approval_id"], True, ADMIN)


def _make_locked_contract(title: str) -> str:
    contract_id = contracts.create_contract(title, SECTIONS, STAKEHOLDERS, ADMIN)
    contracts.submit_for_review(contract_id, ADMIN)
    approvals.submit_for_approval(contract_id, ADMIN)
    _approve_all(contract_id)
    contracts.lock_contract(contract_id, ADMIN)
    return contract_id


def test_full_lifecycle():
    contract_id = contracts.create_contract("Master Service Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    contract = get_contract(contract_id)
    assert contract["status"] == DRAFT and contract["version"] == 1
    assert len(contracts.list_sections(contract_id)) == 3
    assert len(approvals.list_approvals(contract_id)) == 3
    alice = _actor_by_role(contract_id, ROLE_APPROVER)
    bob = _actor_by_role(contract_id, ROLE_STAKEHOLDER)

    contracts.submit_for_review(contract_id, ADMIN)
    assert get_contract(contract_id)["status"] == IN_REVIEW

    # 1. Comment + threaded reply
    section_id = contracts.list_sections(contract_id)[1]["section_id"]
    comment_id = discussions.add_comment(contract_id, section_id, "Can we shorten the payment period?", bob)
    discussions.add_comment(contract_id, section_id, "I suggest 45 days instead.", alice, parent_comment_id=comment_id)
    assert len(contracts.list_comments(contract_id)) == 2
    discussions.update_comment(comment_id, "Can we shorten the payment period? (updated)", bob)
    discussions.delete_comment(comment_id, bob)
    assert len(contracts.list_comments(contract_id)) == 1  # reply survives as top-level

    # 2. Clarification Q&A
    clarification_id = discussions.add_clarification(contract_id, section_id, "What is the liability cap?", bob)
    assert approval_gate(contract_id)["open_clarifications"]
    discussions.answer_clarification(clarification_id, "Capped at 12 months of fees.", alice)
    discussions.close_clarification(clarification_id, bob)
    assert not approval_gate(contract_id)["open_clarifications"]

    # 3. Track changes: UPDATE / INSERT / DELETE, each reviewed then merged
    section = contracts.get_section(section_id)
    before = section["content"]
    after = changes.build_after_content(OP_UPDATE, before, "Payment within 45 days.")
    change_id = changes.propose_change(contract_id, section_id, OP_UPDATE, before, after, bob)
    assert len(approval_gate(contract_id)["unmerged_changes"]) == 1
    changes.review_change(change_id, True, alice)
    version = changes.merge_change(change_id, alice)
    assert version == 2
    assert contracts.get_section(section_id)["content"] == "Payment within 45 days."
    assert get_contract(contract_id)["version"] == 2

    before = contracts.get_section(section_id)["content"]
    after = changes.build_after_content(OP_INSERT, before, "Late payment incurs 2% interest.")
    change_id = changes.propose_change(contract_id, section_id, OP_INSERT, before, after, bob)
    changes.review_change(change_id, True, alice)
    assert changes.merge_change(change_id, alice) == 3

    before = contracts.get_section(section_id)["content"]
    after = changes.build_after_content(OP_DELETE, before, "Late payment incurs 2% interest.")
    change_id = changes.propose_change(contract_id, section_id, OP_DELETE, before, after, bob)
    changes.review_change(change_id, True, alice)
    assert changes.merge_change(change_id, alice) == 4
    assert "2% interest" not in contracts.get_section(section_id)["content"]
    assert not approval_gate(contract_id)["unmerged_changes"]

    # 4. Multi-party approval -> APPROVED -> LOCKED
    approvals.submit_for_approval(contract_id, ADMIN)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL
    rows = approvals.list_approvals(contract_id)
    approvals.decide_approval(rows[0]["approval_id"], True, alice)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL
    approvals.decide_approval(rows[1]["approval_id"], True, ADMIN)
    approvals.decide_approval(rows[2]["approval_id"], True, ADMIN)
    assert get_contract(contract_id)["status"] == APPROVED
    assert approval_gate(contract_id)["ready"]

    with pytest.raises(DomainError):
        discussions.add_comment(contract_id, section_id, "too late", bob)  # APPROVED is frozen
    contracts.lock_contract(contract_id, ADMIN)
    assert get_contract(contract_id)["status"] == LOCKED

    # 5. Final PDF
    result = pdf.generate_pdf(contract_id, ADMIN)
    assert result["path"].exists() and result["path"].read_bytes()[:5].startswith(b"%PDF")
    assert result["filename"] == "contract_v4.pdf"
    assert hashlib.sha256(result["path"].read_bytes()).hexdigest() == result["sha256"]
    assert result["html_path"].exists()
    assert len(contracts.list_pdfs(contract_id)) == 1

    # 6. Locked contract rejects every mutation
    with pytest.raises(DomainError):
        discussions.add_comment(contract_id, section_id, "hack", ADMIN)
    with pytest.raises(DomainError):
        discussions.add_clarification(contract_id, section_id, "hack", ADMIN)
    with pytest.raises(DomainError):
        changes.propose_change(contract_id, section_id, OP_UPDATE, "x", "y", ADMIN)
    with pytest.raises(DomainError):
        contracts.add_stakeholder(contract_id, "New", "new@x.com", ROLE_STAKEHOLDER, ADMIN)

    # 7. eSignature (mock provider) -> EXECUTED
    sent = signing.send_for_signature(contract_id, result["pdf_id"], ADMIN)
    assert sent["provider"] == PROVIDER_MOCK and len(sent["signers"]) == 3
    assert get_contract(contract_id)["status"] == SIGNING
    signatures = contracts.list_signatures(contract_id)
    assert len(signatures) == 3
    assert not signing.mark_signed(signatures[0]["signature_id"], alice)
    signing.mark_signed(signatures[1]["signature_id"], ADMIN)
    assert signing.mark_signed(signatures[2]["signature_id"], ADMIN)
    assert get_contract(contract_id)["status"] == EXECUTED
    assert all(row["status"] == SIGNATURE_SIGNED and row["signed_at"] for row in contracts.list_signatures(contract_id))

    # 8. Audit trail covers the whole workflow
    events = list_events(contract_id)
    types = {row["event_type"] for row in events}
    expected = {
        E_CONTRACT_CREATED, "COMMENT_CREATED", "COMMENT_UPDATED", "COMMENT_DELETED",
        "CLARIFICATION_CREATED", "CLARIFICATION_ANSWERED", "CLARIFICATION_CLOSED",
        "CHANGE_REQUEST_CREATED", "CHANGE_REQUEST_APPROVED", E_CHANGE_MERGED,
        "APPROVAL_GRANTED", "CONTRACT_APPROVED", E_CONTRACT_LOCKED,
        E_PDF_GENERATED, E_SIGNATURE_REQUESTED, "SIGNATURE_COMPLETED", E_CONTRACT_EXECUTED,
    }
    assert expected <= types, f"missing audit events: {expected - types}"
    assert all(row["actor"] and row["created_at"] for row in events)


def test_approval_rules_block_early_approval():
    contract_id = contracts.create_contract("Gate Check Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    contracts.submit_for_review(contract_id, ADMIN)
    section_id = contracts.list_sections(contract_id)[0]["section_id"]
    clarification_id = discussions.add_clarification(contract_id, section_id, "Unresolved question?", ADMIN)
    approvals.submit_for_approval(contract_id, ADMIN)
    _approve_all(contract_id)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL  # OPEN clarification blocks APPROVED
    assert not approval_gate(contract_id)["ready"]

    row = approvals.list_approvals(contract_id)[0]
    approvals.revoke_approval(row["approval_id"], ADMIN)
    approvals.decide_approval(row["approval_id"], True, ADMIN)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL

    discussions.answer_clarification(clarification_id, "Answered.", ADMIN)
    discussions.close_clarification(clarification_id, ADMIN)
    assert get_contract(contract_id)["status"] == APPROVED  # gate re-evaluated on close


def test_validation_and_role_guards():
    with pytest.raises(DomainError):
        contracts.create_contract("  ", SECTIONS, STAKEHOLDERS, ADMIN)
    with pytest.raises(DomainError):
        contracts.create_contract("No sections", [], STAKEHOLDERS, ADMIN)
    with pytest.raises(DomainError):
        contracts.create_contract("Bad email", SECTIONS, [{"name": "X", "email": "nope", "role": ROLE_APPROVER}], ADMIN)
    with pytest.raises(DomainError):
        contracts.create_contract(
            "No approver", SECTIONS, [{"name": "X", "email": "x@x.com", "role": ROLE_STAKEHOLDER}], ADMIN
        )
    with pytest.raises(DomainError):
        contracts.create_contract("Wrong role", SECTIONS, STAKEHOLDERS, Actor("s", "Bob", ROLE_STAKEHOLDER))

    contract_id = contracts.create_contract("Guard Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    contracts.submit_for_review(contract_id, ADMIN)
    section = contracts.list_sections(contract_id)[0]
    section_id, before = section["section_id"], section["content"]
    bob = _actor_by_role(contract_id, ROLE_STAKEHOLDER)

    with pytest.raises(DomainError):
        changes.propose_change(contract_id, section_id, "PATCH", before, "x", bob)
    with pytest.raises(DomainError):
        changes.propose_change(contract_id, section_id, OP_UPDATE, "stale text", "x", bob)
    with pytest.raises(DomainError):
        changes.propose_change(contract_id, section_id, OP_UPDATE, before, before, bob)

    change_id = changes.propose_change(contract_id, section_id, OP_UPDATE, before, "Revised scope text.", bob)
    with pytest.raises(DomainError):
        changes.review_change(change_id, True, bob)  # stakeholder cannot review
    changes.review_change(change_id, True, ADMIN)
    with pytest.raises(DomainError):
        changes.merge_change(change_id, bob)
    changes.merge_change(change_id, ADMIN)
    with pytest.raises(DomainError):
        changes.merge_change(change_id, ADMIN)  # cannot merge twice


def test_pdf_engines_and_hash():
    contract_id = _make_locked_contract("Engine Check Agreement")
    minimal = pdf.generate_pdf(contract_id, ADMIN, engine="minimal")
    assert minimal["engine"] == "minimal"
    assert minimal["path"].read_bytes()[:5].startswith(b"%PDF")
    auto = pdf.generate_pdf(contract_id, ADMIN, engine="auto")
    assert auto["engine"] in ("weasyprint", "playwright", "headless", "minimal")
    assert auto["filename"] != minimal["filename"]  # no overwrite
    assert len(contracts.list_pdfs(contract_id)) == 2
    stored = fetch_all("SELECT sha256_hash FROM generated_pdfs WHERE contract_id = ?", [contract_id])
    assert {row["sha256_hash"] for row in stored} == {minimal["sha256"], auto["sha256"]}


def test_add_reviewer_during_approval_can_comment_and_blocks_gate():
    """Extra reviewers can join mid-approval, comment immediately, and join the approval gate."""
    contract_id = contracts.create_contract("Reviewer Roster Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    contracts.submit_for_review(contract_id, ADMIN)
    approvals.submit_for_approval(contract_id, ADMIN)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL

    contracts.add_stakeholder(contract_id, "Dana Reviewer", "dana@legal.com", ROLE_APPROVER, ADMIN)
    assert len(contracts.list_stakeholders(contract_id)) == 4
    assert len(approvals.list_approvals(contract_id)) == 4
    dana = next(actor for actor in _actors(contract_id) if actor.name == "Dana Reviewer")
    assert dana.role == ROLE_APPROVER

    section_id = contracts.list_sections(contract_id)[0]["section_id"]
    comment_id = discussions.add_comment(contract_id, section_id, "Dana's review comment", dana)
    assert any(row["comment_id"] == comment_id for row in contracts.list_comments(contract_id))
    clarification_id = discussions.add_clarification(contract_id, section_id, "Is the cap negotiable?", dana)

    _approve_all(contract_id)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL  # OPEN clarification still blocks
    discussions.answer_clarification(clarification_id, "No, the cap is fixed.", ADMIN)
    discussions.close_clarification(clarification_id, ADMIN)
    assert get_contract(contract_id)["status"] == APPROVED
    assert approval_gate(contract_id)["ready"]

    # approved / locked contracts freeze both the roster and the discussion
    with pytest.raises(DomainError):
        contracts.add_stakeholder(contract_id, "Late Reviewer", "late@legal.com", ROLE_STAKEHOLDER, ADMIN)
    with pytest.raises(DomainError):
        discussions.add_comment(contract_id, section_id, "too late", dana)
    contracts.lock_contract(contract_id, ADMIN)
    with pytest.raises(DomainError):
        contracts.add_stakeholder(contract_id, "Later Reviewer", "later@legal.com", ROLE_STAKEHOLDER, ADMIN)

    types = {row["event_type"] for row in list_events(contract_id)}
    assert {"STAKEHOLDER_ADDED", "COMMENT_CREATED", "CLARIFICATION_CREATED"} <= types


def test_removing_pending_reviewer_re_evaluates_approval():
    """Changing the roster while PENDING_APPROVAL must re-run the three approval rules."""
    contract_id = contracts.create_contract("Roster Gate Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    contracts.submit_for_review(contract_id, ADMIN)
    approvals.submit_for_approval(contract_id, ADMIN)
    contracts.add_stakeholder(contract_id, "Dana Reviewer", "dana@legal.com", ROLE_STAKEHOLDER, ADMIN)

    for row in approvals.list_approvals(contract_id):
        if row["stakeholder_name"] != "Dana Reviewer":
            approvals.decide_approval(row["approval_id"], True, ADMIN)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL  # Dana's PENDING approval blocks

    dana_id = next(
        row["stakeholder_id"]
        for row in contracts.list_stakeholders(contract_id)
        if row["name"] == "Dana Reviewer"
    )
    contracts.remove_stakeholder(contract_id, dana_id, ADMIN)
    assert get_contract(contract_id)["status"] == APPROVED
    assert len(approvals.list_approvals(contract_id)) == 3


def test_inline_annotations_are_anchored_coloured_and_exported():
    """Inline review: selection offsets, per-reviewer colours, replies, resolve and exports."""
    contract_id = contracts.create_contract("Annotated Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    reviewers = contracts.list_stakeholders(contract_id)
    colours = [row["color"] for row in reviewers]
    assert len(set(colours)) == len(colours), "each reviewer needs a distinct annotation colour"

    section = contracts.list_sections(contract_id)[1]
    content = section["content"]
    start, end = 0, len("Payment within 30 days.")
    assert content[start:end] == "Payment within 30 days."

    comment_id = discussions.add_comment(
        contract_id,
        section["section_id"],
        "Should this be 45 days?",
        _actors(contract_id)[0],
        anchor_start=start,
        anchor_end=end,
        quoted_text=content[start:end],
        color=colours[1],
    )
    stored = next(row for row in contracts.list_comments(contract_id) if row["comment_id"] == comment_id)
    assert (stored["anchor_start"], stored["anchor_end"]) == (start, end)
    assert stored["quoted_text"] == "Payment within 30 days."
    assert stored["color"] == colours[1]

    reply_id = discussions.add_comment(
        contract_id, section["section_id"], "Legal prefers 45.", _actors(contract_id)[1], parent_comment_id=comment_id
    )
    reply = next(row for row in contracts.list_comments(contract_id) if row["comment_id"] == reply_id)
    assert (reply["anchor_start"], reply["anchor_end"]) == (start, end), "replies inherit the parent anchor"

    # stale or out-of-range anchors must be rejected instead of highlighting the wrong words
    with pytest.raises(DomainError):
        discussions.add_comment(
            contract_id, section["section_id"], "stale", ADMIN, anchor_start=start, anchor_end=end, quoted_text="Payment within 60 days."
        )
    with pytest.raises(DomainError):
        discussions.add_comment(contract_id, section["section_id"], "oob", ADMIN, anchor_start=0, anchor_end=len(content) + 5)
    with pytest.raises(DomainError):
        discussions.add_comment(contract_id, section["section_id"], "bad colour", ADMIN, color="#123456")

    discussions.set_resolved(comment_id, True, ADMIN)
    resolved = next(row for row in contracts.list_comments(contract_id) if row["comment_id"] == comment_id)
    assert resolved["resolved_at"] is not None
    discussions.set_resolved(comment_id, False, ADMIN)
    assert next(row for row in contracts.list_comments(contract_id) if row["comment_id"] == comment_id)["resolved_at"] is None

    payload = review.build_payload(contract_id, ADMIN)
    annotation = next(item for item in payload["annotations"] if item["id"] == comment_id)
    assert (annotation["start"], annotation["end"]) == (start, end)
    assert annotation["color"] == colours[1]
    assert len(annotation["replies"]) == 1
    assert payload["can_annotate"] and payload["can_propose"]

    markdown = export.build_markdown(contract_id, ADMIN)
    assert "[^1]" in markdown and "Should this be 45 days?" in markdown
    printable = export.build_print_html(contract_id, ADMIN)
    assert 'class="ann"' in printable and "Comments" in printable
    assert "Should this be 45 days?" in printable


def test_tracked_change_marks_author_and_time_and_appears_in_exports():
    """Merged changes leave a Word-like mark naming the editor and the moment."""
    contract_id = contracts.create_contract("Tracked Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    section = contracts.list_sections(contract_id)[1]
    before = section["content"]
    after = "Payment within 45 days of invoice."
    change_id = changes.propose_change(contract_id, section["section_id"], OP_UPDATE, before, after, ADMIN)

    payload = review.build_payload(contract_id, ADMIN)
    change = next(item for item in payload["changes"] if item["id"] == change_id)
    assert change["status"] == "PENDING"
    assert any(op["op"] == "ins" for op in change["diff"])
    assert any(op["op"] == "del" for op in change["diff"])
    assert payload["revisions"] == {}, "nothing is merged yet"

    changes.review_change(change_id, True, ADMIN)
    changes.merge_change(change_id, ADMIN)

    payload = review.build_payload(contract_id, ADMIN)
    marks = payload["revisions"][section["section_id"]]
    assert marks, "merged change should be located in the document"
    assert marks[0]["author"] == ADMIN.name
    assert "Revised by" in marks[0]["label"]
    assert payload["contract"]["version"] == 2

    markdown = export.build_markdown(contract_id, ADMIN)
    assert "## Change history" in markdown
    assert "45 days of invoice" in markdown  # the body carries the merged text
    assert "| Payment Terms | UPDATE | Admin |" in markdown
    printable = export.build_print_html(contract_id, ADMIN)
    # the merged text is now wrapped in a revision mark, so compare the text only
    assert "45 days of invoice" in re.sub(r"<[^>]+>", "", printable)
    assert "Revised by Admin" in printable


RICH_BODY = """### Payment schedule

Payment within **45 days** of invoice, per the table below.

| Milestone | Share |
| --- | --- |
| Kickoff | 30% |
| Go-live | 70% |

- every invoice must reference the PO
- late payment accrues 2% monthly
"""


def test_richtext_keeps_plain_projection_stable():
    """Markup must never leak into the text that anchors are measured against."""
    assert richtext.plain("Payment within 30 days.") == "Payment within 30 days."
    plain = richtext.plain(RICH_BODY)
    assert "**" not in plain and "|" not in plain
    assert plain.splitlines()[0] == "Payment schedule"
    assert [block["type"] for block in richtext.parse(RICH_BODY)] == ["h3", "p", "table", "ul"]

    # a range inside bold text edits the words, not the markers
    index = plain.find("45 days")
    replaced = richtext.replace_plain_range(RICH_BODY, index, index + len("45 days"), "60 days")
    assert "**60 days**" in replaced
    assert replaced.count("**") == 2

    # escaping lets a clause talk about * or | literally
    assert richtext.plain(r"Cost \*5\* and \| pipe") == "Cost *5* and | pipe"


def test_editor_saves_rich_section_and_marks_who_changed_it():
    contract_id = contracts.create_contract("Rich Editor", SECTIONS, STAKEHOLDERS, ADMIN)
    section = contracts.list_sections(contract_id)[1]
    before = section["content"]

    result = editing.apply_edit(contract_id, section["section_id"], before, RICH_BODY, ADMIN, "editor")
    assert result["version"] == 2
    assert contracts.get_contract(contract_id)["version"] == 2

    payload = review.build_payload(contract_id, ADMIN)
    saved = next(item for item in payload["sections"] if item["id"] == section["section_id"])
    assert saved["markdown"] == RICH_BODY
    assert [block["type"] for block in saved["blocks"]] == ["h3", "p", "table", "ul"]
    assert saved["blocks"][0]["runs"][0]["text"] == "Payment schedule"

    marks = payload["revisions"][section["section_id"]]
    assert marks and marks[0]["author"] == ADMIN.name

    # the printable copy renders the structure, not raw markup
    printable = export.build_print_html(contract_id, ADMIN)
    assert "<table class=\"doc-table\">" in printable and "<li>" in printable
    assert "**" not in printable
    assert "| Kickoff |" in export.build_markdown(contract_id, ADMIN)

    # an editor holding a stale copy cannot overwrite someone else's save
    with pytest.raises(DomainError):
        editing.apply_edit(contract_id, section["section_id"], before, "clobbered", ADMIN)

    # reviewers keep the tracked-change path instead of writing directly
    bob = _actor_by_role(contract_id, ROLE_STAKEHOLDER)
    with pytest.raises(DomainError):
        editing.apply_edit(contract_id, section["section_id"], RICH_BODY, "bob was here", bob)
    tracked = editing.apply_range(
        contract_id, section["section_id"], 0, len("Payment schedule"), "Payment timetable", bob
    )
    assert tracked["mode"] == "tracked"
    pending = changes.list_changes(contract_id)[0]
    assert pending["status"] == "PENDING"
    # the clause is untouched until an approver merges the tracked change
    assert "### Payment timetable" in pending["after_content"]
    assert "### Payment schedule" in contracts.get_section(section["section_id"])["content"]


def test_section_structure_operations_are_audited_and_guarded():
    contract_id = contracts.create_contract("Structure", SECTIONS, STAKEHOLDERS, ADMIN)
    original = contracts.list_sections(contract_id)
    added = editing.add_section(contract_id, original[0]["section_id"], "Audit Rights", "Records for 3 years.", ADMIN)
    order = [row["section_id"] for row in contracts.list_sections(contract_id)]
    assert order.index(added) == order.index(original[0]["section_id"]) + 1

    editing.rename_section(added, "Audit", ADMIN)
    assert contracts.get_section(added)["title"] == "Audit"
    editing.move_section(added, -1, ADMIN)
    assert [row["section_id"] for row in contracts.list_sections(contract_id)].index(added) == 0

    # a section carrying conversation cannot vanish
    discussions.add_comment(
        contract_id, added, "who audits?", _actors(contract_id)[0]
    )
    with pytest.raises(DomainError):
        editing.delete_section(added, ADMIN)

    empty_section = original[2]["section_id"]
    editing.delete_section(empty_section, ADMIN)
    assert empty_section not in [row["section_id"] for row in contracts.list_sections(contract_id)]

    events = [event["event_type"] for event in list_events(contract_id)]
    assert "SECTION_ADDED" in events and "SECTION_RENAMED" in events
    assert "SECTION_MOVED" in events and "SECTION_REMOVED" in events
    assert "SECTION_EDITED" not in events, "structure changes are not text edits"
