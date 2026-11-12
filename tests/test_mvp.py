"""End-to-end MVP lifecycle test: create -> review -> discuss -> track changes ->
cross-side approval -> lock -> PDF -> eSignature -> executed, plus guard/boundary
assertions for the buyer/vendor split.

Two sides negotiate one contract: every participant belongs to the Buyer or the
Vendor. A clause change proposed by one side can only be accepted and merged by
the opposite side, and the contract reaches APPROVED once each side has at least
one approval.

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
    APPROVAL_PENDING,
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
    ROLE_STAKEHOLDER,
    SIDE_BUYER,
    SIDE_VENDOR,
    SIGNATURE_SIGNED,
    SIGNING,
    Actor,
    DomainError,
)
from core.workflow import approval_gate, can_review_change_for, get_contract  # noqa: E402
from db.database import fetch_all  # noqa: E402

ADMIN = Actor("admin", "Admin", ROLE_ADMIN)

SECTIONS = [
    {"title": "Scope of Work", "content": "Vendor shall deliver the analytics platform described in Appendix A."},
    {"title": "Payment Terms", "content": "Payment within 30 days."},
    {"title": "Liability Cap", "content": "Total liability shall not exceed fees paid in the last 12 months."},
]
# Two Buyer-side and two Vendor-side participants per contract.
STAKEHOLDERS = [
    {"name": "Alice Chen", "email": "alice@buyer.com", "side": SIDE_BUYER},
    {"name": "Bob Tan", "email": "bob@buyer.com", "side": SIDE_BUYER},
    {"name": "Carol Wong", "email": "carol@vendor.com", "side": SIDE_VENDOR},
    {"name": "Dave Lim", "email": "dave@vendor.com", "side": SIDE_VENDOR},
]


def _actors(contract_id: str) -> list[Actor]:
    return [
        Actor(row["stakeholder_id"], row["name"], row["role"], row["side"])
        for row in contracts.list_stakeholders(contract_id)
    ]


def _actor_by_side(contract_id: str, side: str) -> Actor:
    return next(actor for actor in _actors(contract_id) if actor.side == side)


def _actor_by_email(contract_id: str, email: str) -> Actor:
    row = next(r for r in contracts.list_stakeholders(contract_id) if r["email"] == email)
    return Actor(row["stakeholder_id"], row["name"], row["role"], row["side"])


def _approve_all(contract_id: str) -> None:
    """Approve until the gate is met (>=1 Buyer and >=1 Vendor); Admin signs for both sides."""
    for row in approvals.list_approvals(contract_id):
        if get_contract(contract_id)["status"] == APPROVED:
            break
        if row["status"] == APPROVAL_PENDING:
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
    assert len(approvals.list_approvals(contract_id)) == 4

    alice = _actor_by_side(contract_id, SIDE_BUYER)   # buyer proposer
    carol = _actor_by_side(contract_id, SIDE_VENDOR)  # vendor reviewer/merger
    bob = _actor_by_email(contract_id, "bob@buyer.com")
    dave = _actor_by_email(contract_id, "dave@vendor.com")

    contracts.submit_for_review(contract_id, ADMIN)
    assert get_contract(contract_id)["status"] == IN_REVIEW

    # 1. Comment + threaded reply (any participant may comment)
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

    # 3. Track changes: a Buyer proposes, the Vendor accepts & merges (cross-side).
    section = contracts.get_section(section_id)
    before = section["content"]
    after = changes.build_after_content(OP_UPDATE, before, "Payment within 45 days.")
    change_id = changes.propose_change(contract_id, section_id, OP_UPDATE, before, after, alice)
    assert len(approval_gate(contract_id)["unmerged_changes"]) == 1
    # the proposer (Buyer) cannot review their own change
    with pytest.raises(DomainError):
        changes.review_change(change_id, True, alice)
    changes.review_change(change_id, True, carol)
    version = changes.merge_change(change_id, carol)
    assert version == 2
    assert contracts.get_section(section_id)["content"] == "Payment within 45 days."
    assert get_contract(contract_id)["version"] == 2

    before = contracts.get_section(section_id)["content"]
    after = changes.build_after_content(OP_INSERT, before, "Late payment incurs 2% interest.")
    change_id = changes.propose_change(contract_id, section_id, OP_INSERT, before, after, bob)
    with pytest.raises(DomainError):  # another Buyer still cannot review a Buyer change
        changes.review_change(change_id, True, bob)
    changes.review_change(change_id, True, dave)
    assert changes.merge_change(change_id, dave) == 3

    before = contracts.get_section(section_id)["content"]
    after = changes.build_after_content(OP_DELETE, before, "Late payment incurs 2% interest.")
    change_id = changes.propose_change(contract_id, section_id, OP_DELETE, before, after, alice)
    changes.review_change(change_id, True, carol)
    assert changes.merge_change(change_id, carol) == 4
    assert "2% interest" not in contracts.get_section(section_id)["content"]
    assert not approval_gate(contract_id)["unmerged_changes"]

    # 4. Cross-side sign-off -> APPROVED -> LOCKED
    approvals.submit_for_approval(contract_id, ADMIN)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL
    rows = approvals.list_approvals(contract_id)
    # a Buyer signs off for the Buyer side, a Vendor for the Vendor side
    alice_approval = next(row for row in rows if row["stakeholder_id"] == alice.id)
    carol_approval = next(row for row in rows if row["stakeholder_id"] == carol.id)
    approvals.decide_approval(alice_approval["approval_id"], True, alice)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL  # only one side approved
    approvals.decide_approval(carol_approval["approval_id"], True, carol)
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
        contracts.add_stakeholder(contract_id, "New", "new@x.com", SIDE_BUYER, ADMIN)

    # 7. eSignature (mock provider) -> EXECUTED
    sent = signing.send_for_signature(contract_id, result["pdf_id"], ADMIN)
    assert sent["provider"] == PROVIDER_MOCK and len(sent["signers"]) == 4
    assert get_contract(contract_id)["status"] == SIGNING
    signatures = contracts.list_signatures(contract_id)
    assert len(signatures) == 4
    assert not signing.mark_signed(signatures[0]["signature_id"], alice)
    signing.mark_signed(signatures[1]["signature_id"], ADMIN)
    signing.mark_signed(signatures[2]["signature_id"], ADMIN)
    assert signing.mark_signed(signatures[3]["signature_id"], ADMIN)
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
        contracts.create_contract("Bad email", SECTIONS, [{"name": "X", "email": "nope", "side": SIDE_BUYER}], ADMIN)
    with pytest.raises(DomainError):
        contracts.create_contract(
            "No vendor", SECTIONS, [{"name": "X", "email": "x@x.com", "side": SIDE_BUYER}], ADMIN
        )
    with pytest.raises(DomainError):
        contracts.create_contract(
            "Wrong side", SECTIONS, [{"name": "X", "email": "x@x.com", "side": "PARTY"}, {"name": "Y", "email": "y@y.com", "side": SIDE_BUYER}], ADMIN
        )
    with pytest.raises(DomainError):
        contracts.create_contract("Bad actor", SECTIONS, STAKEHOLDERS, Actor("s", "Bob", ROLE_STAKEHOLDER))

    contract_id = contracts.create_contract("Guard Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    contracts.submit_for_review(contract_id, ADMIN)
    section = contracts.list_sections(contract_id)[0]
    section_id, before = section["section_id"], section["content"]
    buyer = _actor_by_side(contract_id, SIDE_BUYER)
    vendor = _actor_by_side(contract_id, SIDE_VENDOR)

    with pytest.raises(DomainError):
        changes.propose_change(contract_id, section_id, "PATCH", before, "x", buyer)
    with pytest.raises(DomainError):
        changes.propose_change(contract_id, section_id, OP_UPDATE, "stale text", "x", buyer)
    with pytest.raises(DomainError):
        changes.propose_change(contract_id, section_id, OP_UPDATE, before, before, buyer)

    change_id = changes.propose_change(contract_id, section_id, OP_UPDATE, before, "Revised scope text.", buyer)
    # cross-side: the proposer's own side cannot review; the opposite side can
    assert not can_review_change_for(buyer, SIDE_BUYER)
    assert can_review_change_for(vendor, SIDE_BUYER)
    assert can_review_change_for(ADMIN, SIDE_BUYER)  # operator override
    with pytest.raises(DomainError):
        changes.review_change(change_id, True, buyer)
    changes.review_change(change_id, True, vendor)
    with pytest.raises(DomainError):
        changes.merge_change(change_id, buyer)
    changes.merge_change(change_id, vendor)
    with pytest.raises(DomainError):
        changes.merge_change(change_id, vendor)  # cannot merge twice


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


def test_add_participant_during_approval_can_comment_and_blocks_gate():
    """Extra participants can join mid-approval, comment immediately, and join the approval gate."""
    contract_id = contracts.create_contract("Roster Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    contracts.submit_for_review(contract_id, ADMIN)
    approvals.submit_for_approval(contract_id, ADMIN)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL

    contracts.add_stakeholder(contract_id, "Dana Reviewer", "dana@legal.com", SIDE_VENDOR, ADMIN)
    assert len(contracts.list_stakeholders(contract_id)) == 5
    assert len(approvals.list_approvals(contract_id)) == 5
    dana = next(actor for actor in _actors(contract_id) if actor.name == "Dana Reviewer")
    assert dana.side == SIDE_VENDOR

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
        contracts.add_stakeholder(contract_id, "Late Reviewer", "late@legal.com", SIDE_BUYER, ADMIN)
    with pytest.raises(DomainError):
        discussions.add_comment(contract_id, section_id, "too late", dana)
    contracts.lock_contract(contract_id, ADMIN)
    with pytest.raises(DomainError):
        contracts.add_stakeholder(contract_id, "Later Reviewer", "later@legal.com", SIDE_BUYER, ADMIN)

    types = {row["event_type"] for row in list_events(contract_id)}
    assert {"STAKEHOLDER_ADDED", "COMMENT_CREATED", "CLARIFICATION_CREATED"} <= types


def test_removing_pending_participant_re_evaluates_approval():
    """Changing the roster while PENDING_APPROVAL re-runs the gate without corrupting it."""
    contract_id = contracts.create_contract("Roster Gate Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    contracts.submit_for_review(contract_id, ADMIN)
    approvals.submit_for_approval(contract_id, ADMIN)
    contracts.add_stakeholder(contract_id, "Dana Reviewer", "dana@legal.com", SIDE_VENDOR, ADMIN)
    assert len(approvals.list_approvals(contract_id)) == 5

    # Removing a still-pending participant is allowed and leaves the gate unmet.
    dana_id = next(
        row["stakeholder_id"]
        for row in contracts.list_stakeholders(contract_id)
        if row["name"] == "Dana Reviewer"
    )
    contracts.remove_stakeholder(contract_id, dana_id, ADMIN)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL
    assert len(approvals.list_approvals(contract_id)) == 4

    # A buyer sign-off alone does not satisfy the gate: a Vendor is still required.
    alice = _actor_by_email(contract_id, "alice@buyer.com")
    alice_row = next(r for r in approvals.list_approvals(contract_id) if r["stakeholder_id"] == alice.id)
    approvals.decide_approval(alice_row["approval_id"], True, alice)
    assert get_contract(contract_id)["status"] == PENDING_APPROVAL

    # Someone who already recorded a sign-off cannot be removed from the roster.
    with pytest.raises(DomainError):
        contracts.remove_stakeholder(contract_id, alice.id, ADMIN)

    # Drop one pending Vendor, leaving exactly one Vendor on that side.
    dave = _actor_by_email(contract_id, "dave@vendor.com")
    contracts.remove_stakeholder(contract_id, dave.id, ADMIN)
    # Removing the last participant of a side is forbidden.
    last_vendor = next(
        row["stakeholder_id"]
        for row in contracts.list_stakeholders(contract_id)
        if row["side"] == SIDE_VENDOR
    )
    with pytest.raises(DomainError):
        contracts.remove_stakeholder(contract_id, last_vendor, ADMIN)

    # One Vendor sign-off now satisfies the gate -> APPROVED.
    carol = _actor_by_email(contract_id, "carol@vendor.com")
    carol_row = next(r for r in approvals.list_approvals(contract_id) if r["stakeholder_id"] == carol.id)
    approvals.decide_approval(carol_row["approval_id"], True, carol)
    assert get_contract(contract_id)["status"] == APPROVED
    assert approval_gate(contract_id)["ready"]


def test_inline_annotations_are_anchored_coloured_and_exported():
    """Inline review: selection offsets, per-reviewer colours, replies, resolve and exports."""
    contract_id = contracts.create_contract("Annotated Agreement", SECTIONS, STAKEHOLDERS, ADMIN)
    reviewers = contracts.list_stakeholders(contract_id)
    colours = [row["color"] for row in reviewers]
    assert len(set(colours)) == len(colours), "each participant needs a distinct annotation colour"

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
    # Admin proposes (no side) and reviews/merges with the operator override
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


def test_editor_saves_as_tracked_change_and_cross_side_reviews():
    """The rich editor no longer writes directly: it submits a tracked change that
    only the opposite party can accept and merge."""
    contract_id = contracts.create_contract("Rich Editor", SECTIONS, STAKEHOLDERS, ADMIN)
    contracts.submit_for_review(contract_id, ADMIN)
    section = contracts.list_sections(contract_id)[1]
    before = section["content"]

    buyer = _actor_by_side(contract_id, SIDE_BUYER)
    vendor = _actor_by_side(contract_id, SIDE_VENDOR)

    # The editor's inline save goes through propose_change (tracked), never a direct write.
    cut = before.index("30")
    tracked = editing.apply_range(contract_id, section["section_id"], cut, cut + 2, "45 business", buyer)
    assert tracked["mode"] == "tracked"
    pending = changes.list_changes(contract_id)[0]
    assert pending["status"] == "PENDING"
    # the clause is untouched until the opposite side merges the tracked change
    assert "45 business" in pending["after_content"]
    assert "30 days" in contracts.get_section(section["section_id"])["content"]

    # the proposer's own side cannot review; the opposite side can
    with pytest.raises(DomainError):
        changes.review_change(pending["change_id"], True, buyer)
    changes.review_change(pending["change_id"], True, vendor)
    changes.merge_change(pending["change_id"], vendor)
    assert "45 business" in contracts.get_section(section["section_id"])["content"]

    payload = review.build_payload(contract_id, ADMIN)
    assert any(m for m in payload["revisions"].values())


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
    discussions.add_comment(contract_id, added, "who audits?", _actors(contract_id)[0])
    with pytest.raises(DomainError):
        editing.delete_section(added, ADMIN)

    empty_section = original[2]["section_id"]
    editing.delete_section(empty_section, ADMIN)
    assert empty_section not in [row["section_id"] for row in contracts.list_sections(contract_id)]

    events = [event["event_type"] for event in list_events(contract_id)]
    assert "SECTION_ADDED" in events and "SECTION_RENAMED" in events
    assert "SECTION_MOVED" in events and "SECTION_REMOVED" in events
    assert "SECTION_EDITED" not in events, "structure changes are not text edits"
