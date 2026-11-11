"""Final PDF generation, sha256 verification, eSignature and executed contracts."""
from __future__ import annotations

import streamlit as st

from core import contracts as contract_service
from core import pdf as pdf_service
from core import signing as signing_service
from core.constants import (
    EXECUTED,
    LOCKED,
    PROVIDER_DROPBOX_SIGN,
    PROVIDER_MOCK,
    SIGNATURE_PENDING,
    SIGNING,
    DomainError,
)
from ui.state import can_admin, get_context, run_action, status_badge


def _fmt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else str(value)


def _provider_block() -> str:
    try:
        provider = signing_service.resolve_provider_name()
    except DomainError as exc:
        st.error(str(exc))
        return PROVIDER_MOCK
    if provider == PROVIDER_DROPBOX_SIGN:
        st.info("Signature provider: **Dropbox Sign** (live API). Use **Refresh Dropbox Sign status** to sync results after sending.")
    else:
        st.info("Signature provider: **Local MOCK** (no API key required). Set DROPBOX_SIGN_API_KEY in `.env` to switch to the live service automatically.")
    return provider


def _pdf_block(contract_id: str, contract: dict, actor) -> None:
    st.subheader("Final PDF")
    rows = contract_service.list_pdfs(contract_id)
    if can_admin(actor) and contract["status"] == LOCKED:
        columns = st.columns([2, 2, 2])
        engine = columns[0].selectbox(
            "Render engine",
            ["auto", "weasyprint", "playwright", "headless", "minimal"],
            help="auto tries WeasyPrint → Playwright → Chrome/Edge headless → built-in minimal PDF engine",
        )
        if columns[1].button("Generate final PDF", type="primary", use_container_width=True):
            try:
                result = pdf_service.generate_pdf(contract_id, actor, engine=engine)
            except DomainError as exc:
                st.error(str(exc))
            else:
                st.session_state["last_pdf"] = {
                    key: str(value) for key, value in result.items() if key not in ("html_path",)
                }
                st.success(f"Generated {result['filename']} (engine: {result['engine']})")
                st.rerun()
        columns[2].caption(f"Current version v{contract['version']}; filename contract_v{contract['version']}.pdf")
        last = st.session_state.get("last_pdf")
        if last:
            st.caption(
                f"Last generated: {last.get('filename')} · SHA256 `{last.get('sha256')}` · {last.get('size')} bytes"
            )
    elif not can_admin(actor):
        st.caption("Only an Admin can generate PDFs.")

    if not rows:
        st.info("No PDF yet. An Admin can generate the final copy once the contract is locked.")
        return
    for row in rows:
        with st.container(border=True):
            columns = st.columns([3, 2, 2, 2])
            columns[0].write(f"**{row['file_path'].split('/')[-1]}** · v{row['version']}")
            columns[1].caption(f"Engine: {row['engine']} · {_fmt(row['generated_at'])}")
            columns[2].caption(f"SHA256: `{row['sha256_hash'][:16]}…`")
            path = pdf_service.pdf_absolute_path(row["file_path"])
            if path.exists():
                columns[3].download_button(
                    "Download PDF",
                    data=path.read_bytes(),
                    file_name=path.name,
                    mime="application/pdf",
                    key=f"download_{row['pdf_id']}",
                    use_container_width=True,
                )
            else:
                columns[3].caption("File missing")
            st.code(f"sha256: {row['sha256_hash']}", language="text")


def _signature_block(contract_id: str, contract: dict, actor, provider: str) -> None:
    st.divider()
    st.subheader("eSignature")
    pdfs = contract_service.list_pdfs(contract_id)
    signatures = contract_service.list_signatures(contract_id)

    if contract["status"] == LOCKED and not signatures and can_admin(actor) and pdfs:
        columns = st.columns([3, 2])
        pdf_id = columns[0].selectbox(
            "PDF to sign",
            [row["pdf_id"] for row in pdfs],
            format_func=lambda pid: next(
                (f"{row['file_path'].split('/')[-1]} · {_fmt(row['generated_at'])}" for row in pdfs if row["pdf_id"] == pid),
                pid,
            ),
        )
        if columns[1].button("Send for signature → SIGNING", type="primary", use_container_width=True):
            run_action(
                signing_service.send_for_signature,
                contract_id,
                pdf_id,
                actor,
                message="Signature request sent",
            )
    elif not signatures:
        st.caption("An Admin can send for signature once the contract is locked and a PDF exists.")

    if signatures:
        table = [
            {
                "Signer": row["signer_name"],
                "Email": row["signer_email"],
                "Status": row["status"],
                "Signed at": _fmt(row["signed_at"]),
                "Provider": row["provider"],
                "Request ID": row["request_id"],
            }
            for row in signatures
        ]
        st.dataframe(table, hide_index=True, use_container_width=True)
        for row in signatures:
            if row["status"] != SIGNATURE_PENDING:
                continue
            columns = st.columns([4, 2, 2])
            columns[0].caption(f"Pending: {row['signer_name']} · {row['signer_email']}")
            if row["provider"] == PROVIDER_MOCK and contract["status"] == SIGNING:
                if columns[1].button("Simulate signature", key=f"sign_{row['signature_id']}"):
                    run_action(
                        signing_service.mark_signed,
                        row["signature_id"],
                        actor,
                        message="Signature recorded; the contract becomes EXECUTED once everyone signs",
                    )
        if signatures[-1]["provider"] == PROVIDER_DROPBOX_SIGN and contract["status"] == SIGNING and can_admin(actor):
            if st.button("Refresh Dropbox Sign status"):
                try:
                    result = signing_service.refresh_signature_status(contract_id, actor)
                except DomainError as exc:
                    st.error(str(exc))
                else:
                    st.success(f"Sync complete; {result['newly_signed']} new signature(s)")
                    st.rerun()

    if contract["status"] == EXECUTED:
        st.success("🎉 All parties have signed; the contract is EXECUTED. View the full audit trail on **Timeline**.")
    elif contract["status"] == SIGNING:
        st.info("Signing in progress: the contract becomes EXECUTED once all signatures are complete.")


def render() -> None:
    st.title("📑 PDF & Signature")
    st.caption("Final PDF · SHA256 verification · eSignature · Executed contracts")
    contract_id, actor, contract, stakeholders = get_context()
    st.markdown(status_badge(contract["status"]), unsafe_allow_html=True)
    if contract["status"] not in (LOCKED, SIGNING, EXECUTED):
        st.warning("Complete approval and lock the contract before generating the final PDF or sending for signature.")

    provider = _provider_block()
    _pdf_block(contract_id, contract, actor)
    _signature_block(contract_id, contract, actor, provider)
