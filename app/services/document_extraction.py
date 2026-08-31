"""Extracts plain text from an uploaded policy document (PDF or Word)."""

import io

import docx
from pypdf import PdfReader

PDF_CONTENT_TYPES = frozenset({"application/pdf"})
DOCX_CONTENT_TYPES = frozenset(
    {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
)


class UnsupportedDocumentTypeError(ValueError):
    """Not a PDF or Word (.docx) document."""


def extract_text(filename: str, content_type: str | None, data: bytes) -> str:
    lowered_name = filename.lower()
    is_pdf = content_type in PDF_CONTENT_TYPES or lowered_name.endswith(".pdf")
    is_docx = content_type in DOCX_CONTENT_TYPES or lowered_name.endswith(".docx")

    if is_pdf:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if is_docx:
        document = docx.Document(io.BytesIO(data))
        return "\n".join(paragraph.text for paragraph in document.paragraphs).strip()

    raise UnsupportedDocumentTypeError(
        f"Unsupported file type for {filename!r} (content_type={content_type!r}) - "
        "only PDF and Word (.docx) documents are accepted."
    )
