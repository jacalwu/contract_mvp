"""eSignature: Dropbox Sign adapter plus a local mock provider for credential-free demos."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

import requests

import config
from core.audit import log_event
from core.constants import (
    EXECUTED,
    E_CONTRACT_EXECUTED,
    E_SIGNATURE_COMPLETED,
    E_SIGNATURE_REQUESTED,
    LOCKED,
    PROVIDER_DROPBOX_SIGN,
    PROVIDER_MOCK,
    ROLE_ADMIN,
    SIGNATURE_PENDING,
    SIGNATURE_SIGNED,
    SIGNING,
    Actor,
    DomainError,
    new_id,
    now,
)
from core.contracts import list_signatures, list_stakeholders
from core.pdf import pdf_absolute_path
from core.workflow import get_contract, require_role, require_status
from db.database import fetch_all, fetch_one, sql, tx

_DROPBOX_SIGN_API = "https://api.hellosign.com/v3"


class SignatureProvider(Protocol):
    name: str

    def send(self, contract: dict, pdf_path: Path, signers: list[dict]) -> str: ...

    def fetch_status(self, request_id: str) -> dict: ...


class MockProvider:
    """Local simulation: signing is completed explicitly in the UI."""

    name = PROVIDER_MOCK

    def send(self, contract: dict, pdf_path: Path, signers: list[dict]) -> str:
        return new_id("MOCKREQ")

    def fetch_status(self, request_id: str) -> dict:
        return {"signatures": []}


class DropboxSignProvider:
    """Real Dropbox Sign (HelloSign v3) signature_request/send + status polling."""

    name = PROVIDER_DROPBOX_SIGN

    def __init__(self, api_key: str, test_mode: bool = True) -> None:
        if not api_key:
            raise DomainError("DROPBOX_SIGN_API_KEY is missing; set it in .env or switch to mock mode.")
        self.api_key = api_key
        self.test_mode = test_mode

    def send(self, contract: dict, pdf_path: Path, signers: list[dict]) -> str:
        data: dict[str, str] = {
            "title": contract["title"],
            "subject": f"Signature request: {contract['title']}",
            "message": "Please review and sign the attached final contract.",
            "test_mode": "1" if self.test_mode else "0",
        }
        for index, signer in enumerate(signers):
            data[f"signers[{index}][name]"] = signer["name"]
            data[f"signers[{index}][email_address]"] = signer["email"]
        try:
            with pdf_path.open("rb") as handle:
                response = requests.post(
                    f"{_DROPBOX_SIGN_API}/signature_request/send",
                    auth=(self.api_key, ""),
                    data=data,
                    files={"file": (pdf_path.name, handle, "application/pdf")},
                    timeout=60,
                )
            if response.status_code >= 400:
                raise DomainError(f"Dropbox Sign returned {response.status_code}: {response.text[:300]}")
            payload = response.json().get("signature_request", {})
            request_id = payload.get("signature_request_id")
            if not request_id:
                raise DomainError("Dropbox Sign did not return a signature_request_id.")
            return str(request_id)
        except requests.RequestException as exc:
            raise DomainError(f"Dropbox Sign request failed: {exc}") from exc

    def fetch_status(self, request_id: str) -> dict:
        try:
            response = requests.get(
                f"{_DROPBOX_SIGN_API}/signature_request/{request_id}",
                auth=(self.api_key, ""),
                timeout=30,
            )
            if response.status_code >= 400:
                raise DomainError(f"Dropbox Sign status query failed ({response.status_code}): {response.text[:300]}")
            payload = response.json().get("signature_request", {})
            return {
                "completed": bool(payload.get("is_complete")),
                "signatures": [
                    {
                        "email": str(item.get("signer_email_address", "")).lower(),
                        "signed": item.get("status_code") == "signed",
                    }
                    for item in payload.get("signatures", [])
                ],
            }
        except requests.RequestException as exc:
            raise DomainError(f"Dropbox Sign request failed: {exc}") from exc


def resolve_provider_name() -> str:
    configured = config.SIGNATURE_PROVIDER
    if configured == PROVIDER_DROPBOX_SIGN.lower():
        if not config.DROPBOX_SIGN_API_KEY:
            raise DomainError("DROPBOX_SIGN mode was requested but DROPBOX_SIGN_API_KEY is not configured.")
        return PROVIDER_DROPBOX_SIGN
    if configured == "mock":
        return PROVIDER_MOCK
    return PROVIDER_DROPBOX_SIGN if config.DROPBOX_SIGN_API_KEY else PROVIDER_MOCK


def get_provider(name: str | None = None) -> SignatureProvider:
    provider_name = name or resolve_provider_name()
    if provider_name == PROVIDER_DROPBOX_SIGN:
        return DropboxSignProvider(config.DROPBOX_SIGN_API_KEY, config.DROPBOX_SIGN_TEST_MODE)
    return MockProvider()


def send_for_signature(contract_id: str, pdf_id: str, actor: Actor) -> dict:
    require_role(actor, {ROLE_ADMIN}, "Send for signature")
    contract = get_contract(contract_id)
    require_status(contract, {LOCKED}, "Send for signature")
    pdf = fetch_one(
        "SELECT * FROM generated_pdfs WHERE pdf_id = ? AND contract_id = ?",
        [pdf_id, contract_id],
    )
    if pdf is None:
        raise DomainError("The selected PDF does not exist.")
    pdf_path = pdf_absolute_path(pdf["file_path"])
    if not pdf_path.exists():
        raise DomainError(f"PDF file is missing: {pdf['file_path']}")
    if list_signatures(contract_id):
        raise DomainError("A signature request was already sent for this contract.")

    stakeholders = list_stakeholders(contract_id)
    signers, seen = [], set()
    for party in stakeholders:
        email = party["email"].lower()
        if email in seen:
            continue
        seen.add(email)
        signers.append({"name": party["name"], "email": email})
    if not signers:
        raise DomainError("The contract has no signers.")

    provider = get_provider()
    request_id = provider.send(contract, pdf_path, signers)
    with tx() as con:
        for signer in signers:
            sql(
                con,
                """INSERT INTO signatures
                   (signature_id, contract_id, signer_email, signer_name, signed_at,
                    provider, request_id, status, created_at)
                   VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                [
                    new_id("SIG"),
                    contract_id,
                    signer["email"],
                    signer["name"],
                    provider.name,
                    request_id,
                    SIGNATURE_PENDING,
                    now(),
                ],
            )
        sql(con, "UPDATE contracts SET status = ? WHERE contract_id = ?", [SIGNING, contract_id])
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_SIGNATURE_REQUESTED,
            target_id=request_id,
            before_value={"status": contract["status"]},
            after_value={
                "status": SIGNING,
                "provider": provider.name,
                "pdf": pdf["file_path"],
                "signers": signers,
            },
        )
    return {"request_id": request_id, "provider": provider.name, "signers": signers}


def _complete_if_all_signed(con, contract_id: str, actor: Actor) -> bool:
    remaining = fetch_all(
        "SELECT 1 FROM signatures WHERE contract_id = ? AND status <> ?",
        [contract_id, SIGNATURE_SIGNED],
    )
    if remaining:
        return False
    contract = get_contract(contract_id)
    if contract["status"] != EXECUTED:
        sql(con, "UPDATE contracts SET status = ? WHERE contract_id = ?", [EXECUTED, contract_id])
        log_event(
            con,
            contract_id=contract_id,
            actor=actor,
            event_type=E_CONTRACT_EXECUTED,
            target_id=contract_id,
            before_value={"status": contract["status"]},
            after_value={"status": EXECUTED},
        )
    return True


def mark_signed(signature_id: str, actor: Actor) -> bool:
    """Mock provider only: simulate one party completing their signature."""
    row = fetch_one("SELECT * FROM signatures WHERE signature_id = ?", [signature_id])
    if row is None:
        raise DomainError("Signature record not found.")
    if row["provider"] != PROVIDER_MOCK:
        raise DomainError("Real Dropbox Sign signatures must be synced via status refresh.")
    contract = get_contract(row["contract_id"])
    require_status(contract, {SIGNING}, "Mark signature")
    if row["status"] == SIGNATURE_SIGNED:
        raise DomainError("This signer already signed.")
    with tx() as con:
        sql(
            con,
            "UPDATE signatures SET status = ?, signed_at = ? WHERE signature_id = ?",
            [SIGNATURE_SIGNED, now(), signature_id],
        )
        log_event(
            con,
            contract_id=row["contract_id"],
            actor=actor,
            event_type=E_SIGNATURE_COMPLETED,
            target_id=signature_id,
            before_value={"status": row["status"]},
            after_value={"status": SIGNATURE_SIGNED, "signer_email": row["signer_email"]},
        )
        return _complete_if_all_signed(con, row["contract_id"], actor)


def refresh_signature_status(contract_id: str, actor: Actor) -> dict:
    """Poll Dropbox Sign and sync signed state back into the local audit-backed records."""
    require_role(actor, {ROLE_ADMIN}, "Refresh signature status")
    rows = list_signatures(contract_id)
    if not rows:
        raise DomainError("No signature request has been sent.")
    if rows[0]["provider"] != PROVIDER_DROPBOX_SIGN:
        raise DomainError("Mock mode needs no refresh; simulate each signature directly on the page.")
    request_id = rows[0]["request_id"]
    status = get_provider(PROVIDER_DROPBOX_SIGN).fetch_status(request_id)
    signed_emails = {item["email"] for item in status.get("signatures", []) if item["signed"]}
    newly_signed = 0
    with tx() as con:
        for row in rows:
            if row["signer_email"] in signed_emails and row["status"] != SIGNATURE_SIGNED:
                sql(
                    con,
                    "UPDATE signatures SET status = ?, signed_at = ? WHERE signature_id = ?",
                    [SIGNATURE_SIGNED, now(), row["signature_id"]],
                )
                log_event(
                    con,
                    contract_id=contract_id,
                    actor=actor,
                    event_type=E_SIGNATURE_COMPLETED,
                    target_id=row["signature_id"],
                    before_value={"status": row["status"]},
                    after_value={"status": SIGNATURE_SIGNED, "signer_email": row["signer_email"]},
                )
                newly_signed += 1
        _complete_if_all_signed(con, contract_id, actor)
    return {"newly_signed": newly_signed, "request_id": request_id}
