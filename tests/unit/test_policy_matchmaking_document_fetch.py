"""_fetch_and_extract (policy_matchmaking_service.py) - regression coverage for a
real production crash: an AES-encrypted PDF made pypdf raise DependencyError, which
the old except clause (httpx.HTTPError | UnsupportedDocumentTypeError only) didn't
catch, crashing the whole Flow 1 background job before a Policy row was ever
created - no row, no recorded error, no Flow 2 callback. Two fixes: cryptography is
now a real dependency (pypdf can decrypt AES PDFs), and the extraction except is
broadened to match its own "best-effort" contract for whatever's left over."""

import io

import httpx
import pytest
from pypdf import PdfWriter

from app.services import policy_matchmaking_service
from app.services.policy_matchmaking_service import _fetch_and_extract

pytestmark = pytest.mark.asyncio

DOCUMENT_URL = "https://example.com/policy.pdf"


def _blank_pdf_bytes(*, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if encrypted:
        writer.encrypt(user_password="", owner_password="secret-owner-pw", algorithm="AES-256")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _mock_httpx_client(monkeypatch, *, status_code: int = 200, content: bytes = b""):
    """Patches the module's httpx.AsyncClient so _fetch_and_extract's real
    `async with httpx.AsyncClient(...) as client: await client.get(...)` hits a
    MockTransport instead of the network - no extra test dependency needed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content)

    real_client_cls = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(policy_matchmaking_service.httpx, "AsyncClient", patched_client)


async def test_no_document_url_returns_nothing():
    assert await _fetch_and_extract(None, None, None) == (None, None)


async def test_fetch_failure_loses_both(monkeypatch):
    _mock_httpx_client(monkeypatch, status_code=404)
    text, data = await _fetch_and_extract("policy.pdf", "application/pdf", DOCUMENT_URL)
    assert (text, data) == (None, None)


async def test_extraction_failure_keeps_the_downloaded_bytes(monkeypatch):
    """The regression case: the document downloads fine, but extract_text() raises
    something other than UnsupportedDocumentTypeError (an unparseable/corrupt PDF).
    Must degrade to (None, data), never propagate and crash the caller."""
    _mock_httpx_client(monkeypatch, content=b"not a real pdf")
    text, data = await _fetch_and_extract("policy.pdf", "application/pdf", DOCUMENT_URL)
    assert text is None
    assert data == b"not a real pdf"  # file bytes preserved despite the extraction failure


async def test_unsupported_file_type_also_keeps_the_bytes(monkeypatch):
    _mock_httpx_client(monkeypatch, content=b"plain text file")
    text, data = await _fetch_and_extract("policy.txt", "text/plain", DOCUMENT_URL)
    assert text is None
    assert data == b"plain text file"


async def test_successful_extraction_is_unaffected(monkeypatch):
    pdf_bytes = _blank_pdf_bytes()
    _mock_httpx_client(monkeypatch, content=pdf_bytes)
    text, data = await _fetch_and_extract("policy.pdf", "application/pdf", DOCUMENT_URL)
    assert text == ""  # blank page, no text - but extraction succeeded, not None
    assert data == pdf_bytes


async def test_aes_encrypted_pdf_extracts_successfully(monkeypatch):
    """The exact real-world case that crashed production: an AES-encrypted PDF.
    Now succeeds outright (cryptography installed) rather than needing the
    fallback path at all."""
    pdf_bytes = _blank_pdf_bytes(encrypted=True)
    _mock_httpx_client(monkeypatch, content=pdf_bytes)
    text, data = await _fetch_and_extract("policy.pdf", "application/pdf", DOCUMENT_URL)
    assert text == ""
    assert data == pdf_bytes
