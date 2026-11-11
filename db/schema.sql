-- Contract Negotiation & Approval Platform MVP schema (DuckDB)
-- Table/column names follow design.md. Extra columns are marked with "+".

CREATE TABLE IF NOT EXISTS contracts (
    contract_id VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS sections (
    section_id VARCHAR PRIMARY KEY,
    contract_id VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    content TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,  -- + stable display order
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS stakeholders (
    stakeholder_id VARCHAR PRIMARY KEY,
    contract_id VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    email VARCHAR NOT NULL,
    role VARCHAR NOT NULL,               -- STAKEHOLDER | APPROVER
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    comment_id VARCHAR PRIMARY KEY,
    contract_id VARCHAR NOT NULL,
    section_id VARCHAR,
    author_id VARCHAR NOT NULL,
    message TEXT NOT NULL,
    parent_comment_id VARCHAR,           -- + threaded replies
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS clarifications (
    clarification_id VARCHAR PRIMARY KEY,
    contract_id VARCHAR NOT NULL,
    section_id VARCHAR,
    question TEXT NOT NULL,
    answer TEXT,
    status VARCHAR NOT NULL,             -- OPEN | ANSWERED | CLOSED
    created_by VARCHAR NOT NULL,
    answered_by VARCHAR,                 -- +
    answered_at TIMESTAMP,               -- +
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS change_requests (
    change_id VARCHAR PRIMARY KEY,
    contract_id VARCHAR NOT NULL,
    section_id VARCHAR NOT NULL,
    operation VARCHAR NOT NULL,          -- INSERT | UPDATE | DELETE
    before_content TEXT NOT NULL,
    after_content TEXT NOT NULL,
    requested_by VARCHAR NOT NULL,
    status VARCHAR NOT NULL,             -- PENDING | APPROVED | REJECTED | MERGED
    created_at TIMESTAMP NOT NULL,
    reviewed_by VARCHAR,                 -- +
    reviewed_at TIMESTAMP,               -- +
    merged_at TIMESTAMP                  -- +
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id VARCHAR PRIMARY KEY,
    contract_id VARCHAR NOT NULL,
    stakeholder_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL,             -- PENDING | APPROVED | REJECTED
    decided_by VARCHAR,                  -- +
    approved_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL        -- +
);

CREATE TABLE IF NOT EXISTS generated_pdfs (
    pdf_id VARCHAR PRIMARY KEY,
    contract_id VARCHAR NOT NULL,
    version INTEGER NOT NULL,
    file_path VARCHAR NOT NULL,
    sha256_hash VARCHAR NOT NULL,
    engine VARCHAR,                      -- +
    generated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS signatures (
    signature_id VARCHAR PRIMARY KEY,
    contract_id VARCHAR NOT NULL,
    signer_email VARCHAR NOT NULL,
    signer_name VARCHAR,                  -- +
    signed_at TIMESTAMP,
    provider VARCHAR NOT NULL,            -- DROPBOX_SIGN | MOCK
    request_id VARCHAR,                   -- + provider request reference
    status VARCHAR NOT NULL,              -- + PENDING | SIGNED | DECLINED
    created_at TIMESTAMP NOT NULL         -- +
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id VARCHAR PRIMARY KEY,
    contract_id VARCHAR NOT NULL,
    actor VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    target_id VARCHAR,
    before_value TEXT,
    after_value TEXT,
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sections_contract ON sections(contract_id);
CREATE INDEX IF NOT EXISTS idx_comments_contract ON comments(contract_id);
CREATE INDEX IF NOT EXISTS idx_changes_contract ON change_requests(contract_id);
CREATE INDEX IF NOT EXISTS idx_audit_contract ON audit_events(contract_id);
