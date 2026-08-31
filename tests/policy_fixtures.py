"""Builds a minimal, genuinely-valid .docx file in memory for F2 policy-upload tests -
python-docx's Document API produces real OOXML, unlike a hand-rolled byte string, so
app.services.document_extraction's real parser can round-trip it."""

import io

import docx

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def make_test_docx_bytes(text: str = "This is a test policy document about ERP road pricing.") -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
