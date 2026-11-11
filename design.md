Contract Negotiation & Approval Platform MVP
Project Goal
Build an MVP SaaS module that supports:

Contract Negotiation
Multi-party Review
Comment & Clarification
Change Tracking (similar to MS Word Track Changes)
Multi-party Approval
Final PDF Generation
eSignature Integration
Audit Trail
Matching functionality already exists and is out of scope.

MVP Scope
The system starts after parties have been matched.

Workflow:

Contract Created
    ↓
Review
    ↓
Comment / Clarification
    ↓
Change Request
    ↓
Stakeholder Approval
    ↓
Contract Locked
    ↓
Generate Final PDF
    ↓
eSignature
    ↓
Executed Contract
Tech Stack
Frontend
Streamlit

Backend
Python

Database
DuckDB

Vector Database
LanceDB

PDF Generation
HTML + WeasyPrint

or

HTML + Playwright

eSignature
Dropbox Sign (preferred for MVP)

Future:

DocuSign
PandaDoc
User Roles
Stakeholder
Can:

Read Contract
Comment
Clarify
Propose Change
Cannot:

Finalize Contract
Approver
Can:

Approve
Reject
Admin
Can:

Manage Stakeholders
Lock Contracts
Trigger PDF Generation
Contract Structure
Contracts are not stored as Word documents.

Contracts are stored as Sections.

Example:

{
  "contract_id": "CTR001",
  "sections": [
    {
      "id": "payment",
      "title": "Payment Terms",
      "content": "Payment within 30 days."
    }
  ]
}
Database Schema
contracts
CREATE TABLE contracts (
    contract_id VARCHAR,
    title VARCHAR,
    status VARCHAR,
    version INTEGER,
    created_at TIMESTAMP
);
Status
DRAFT
IN_REVIEW
PENDING_APPROVAL
APPROVED
LOCKED
SIGNING
EXECUTED
sections
CREATE TABLE sections (
    section_id VARCHAR,
    contract_id VARCHAR,
    title VARCHAR,
    content TEXT,
    created_at TIMESTAMP
);
Document Operations
The system supports 3 document operations.

1. Comment
Discussion only.

No content change.

Example:

Can we shorten payment period?
Database:

comments
CREATE TABLE comments (
    comment_id VARCHAR,
    contract_id VARCHAR,
    section_id VARCHAR,
    author_id VARCHAR,
    message TEXT,
    created_at TIMESTAMP
);
2. Clarification
Question and answer workflow.

Example:

What is the liability cap?
Database:

CREATE TABLE clarifications (
    clarification_id VARCHAR,
    contract_id VARCHAR,
    section_id VARCHAR,
    question TEXT,
    answer TEXT,
    status VARCHAR,
    created_by VARCHAR,
    created_at TIMESTAMP
);
Status:

OPEN
ANSWERED
CLOSED
3. Modification
Equivalent to Word Track Changes.

Supported Operations:

INSERT
UPDATE
DELETE
INSERT
Before:

Payment within 30 days.
After:

Payment within 30 days.

Late payment incurs 2% interest.
UPDATE
Before:

Payment within 30 days.
After:

Payment within 45 days.
DELETE
Before:

Late payment incurs 2% interest.
After:

(deleted)
Change Tracking
Every modification creates a Change Request.

Stakeholders propose changes via Change Requests (tracked changes). Admin/Approver may also
apply direct edits while the contract is in DRAFT or IN_REVIEW — these are recorded as a
MERGED change, bump the version and leave an in-document revision mark, so the audit trail
and PDF export stay consistent with proposed changes.

Workflow:

Current Content
      ↓
Proposed Change
      ↓
Review
      ↓
Accept / Reject
      ↓
Merge
Database:

CREATE TABLE change_requests (
    change_id VARCHAR,
    contract_id VARCHAR,
    section_id VARCHAR,

    operation VARCHAR,

    before_content TEXT,
    after_content TEXT,

    requested_by VARCHAR,

    status VARCHAR,

    created_at TIMESTAMP
);
Status:

PENDING
APPROVED
REJECTED
MERGED
Operation:

INSERT
UPDATE
DELETE
Word Track Changes View
UI should display diffs.

Example:

- Payment within 30 days.
+ Payment within 45 days.

In-Document Revision Marks
Merged changes are highlighted inline in the document body, not only in a side panel.
Each revised span carries the author and timestamp, e.g. "Revised by Admin on 2026-11-11",
so reviewers immediately see who changed what and when.

Document Rendering Model
Section content is stored as plain text using a Markdown-lite subset:

# H1 / ## H2 / ### H3 headings
- bullet lists and 1. numbered lists
> block quotes
| a | b | tables (first row is the header)
--- horizontal rules
Inline: **bold**, *italic*, ++underline++, ~~strike~~, `code`

A plain-text projection (markup stripped) is used for anchoring comments and diffs, so
adding formatting never shifts existing annotation anchors. Legacy plain-text sections
project byte-for-byte to their original content (backward compatible).
Stakeholder Approval
Database:

CREATE TABLE approvals (
    approval_id VARCHAR,
    contract_id VARCHAR,

    stakeholder_id VARCHAR,

    status VARCHAR,

    approved_at TIMESTAMP
);
Status:

PENDING
APPROVED
REJECTED
Approval Rules
Contract can proceed only if:

No Open Clarifications
AND
No Pending Changes
AND
All Stakeholders Approved
Then:

Contract Status = APPROVED
Contract Locking
Once approved:

Contract Status → LOCKED
Rules:

No further modifications allowed
No new comments
No new change requests
Only PDF generation is allowed

PDF Generation
When contract is locked:

Generate Contract Version
Example:

contract_v18.pdf
Store:

CREATE TABLE generated_pdfs (
    pdf_id VARCHAR,
    contract_id VARCHAR,
    version INTEGER,

    file_path VARCHAR,

    sha256_hash VARCHAR,

    generated_at TIMESTAMP
);
eSignature Workflow
After PDF Generation:

LOCKED
    ↓
PDF Generated
    ↓
Send For Signature
    ↓
Party A Signed
    ↓
Party B Signed
    ↓
Executed
Database:

CREATE TABLE signatures (
    signature_id VARCHAR,
    contract_id VARCHAR,

    signer_email VARCHAR,

    signed_at TIMESTAMP,

    provider VARCHAR
);
Provider:

DROPBOX_SIGN
DOCUSIGN
PANDADOC
Audit Trail
Every operation must create an audit record.

Database:

CREATE TABLE audit_events (
    event_id VARCHAR,

    contract_id VARCHAR,

    actor VARCHAR,

    event_type VARCHAR,

    target_id VARCHAR,

    before_value TEXT,

    after_value TEXT,

    created_at TIMESTAMP
);
Audit Event Types
CONTRACT_CREATED

COMMENT_CREATED

COMMENT_UPDATED

COMMENT_DELETED

CLARIFICATION_CREATED

CLARIFICATION_ANSWERED

CLARIFICATION_CLOSED

CHANGE_REQUEST_CREATED

CHANGE_REQUEST_APPROVED

CHANGE_REQUEST_REJECTED

CHANGE_REQUEST_MERGED

APPROVAL_GRANTED

APPROVAL_REVOKED

CONTRACT_APPROVED

CONTRACT_LOCKED

PDF_GENERATED

SIGNATURE_REQUESTED

SIGNATURE_COMPLETED

CONTRACT_EXECUTED

SECTION_EDITED
SECTION_ADDED
SECTION_REMOVED
SECTION_MOVED
SECTION_RENAMED
Activity Timeline
UI should include a Timeline page.

Example:

2026-11-01 09:01

John updated Payment Clause

2026-11-01 09:20

Mary commented

2026-11-01 10:00

Legal approved

2026-11-01 11:00

Finance approved

2026-11-01 11:30

Contract locked

2026-11-01 11:35

PDF generated

2026-11-01 12:00

Sent for signature
Timeline reads from:

audit_events
LanceDB Usage
LanceDB is not used for transactional data.

LanceDB only stores embeddings for:

Contract Sections
Clauses
Historical Changes
Comments
Future capabilities:

Similar Clause Search
Clause Recommendation
Risk Detection
AI Review Assistant
Contract RAG
Not required for MVP.

Streamlit Pages
Dashboard
Displays:

Contract List
Status
Version
Stakeholders
Contract Viewer
Displays:

Sections
Current Version
Comments
Supports:

Comment
Clarification
Replies
Change Requests
Supports:

Insert
Update
Delete
Diff View

Document Review
The primary review surface. Renders the contract as a print-ready document (Markdown-lite)
rather than a form, and hosts inline collaboration:

Displays:

Printable document with headings, lists, tables, quotes
Inline annotations anchored to selected text (each stakeholder gets a distinct color)
Threaded replies inside the annotation popover
Tracked changes with insert/delete marks and author
Merged revisions highlighted in the body ("Revised by X on ...")
Line numbers in the document gutter

Supports (Edit mode):

Paragraph styles (Body / Title / Heading 1-2 / Quote)
Bold, Italic, Underline, Strikethrough
Bullet and numbered lists
Insert table (grid picker)
Horizontal rule
Clear formatting, undo / redo
Save changes (only changed sections submitted; optimistic lock guards concurrent edits)
Add / rename / move / delete section

Print / PDF export of the annotated document.

Approvals
Displays:

Stakeholder
Status
Approved Time
Timeline
Displays audit history.

PDF & Signature
Displays:

Generated PDFs
Signature Status
Executed Contracts
Out of Scope (MVP)
Do NOT implement:

Real-time collaboration
Google Docs style editing
Rich text collaborative editor
Workflow engine
Complex permission matrix
AI Contract Review
Clause Risk Scoring
Contract Templates Marketplace
Multi-tenant billing
MVP Success Criteria
A successful MVP allows:

Create Contract
Add Stakeholders
Comment
Clarify
Submit Change Requests
Review Changes
Approve Contract
Lock Contract
Generate PDF
Send for eSignature
Complete Signing
View Full Audit Trail