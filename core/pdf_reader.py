from __future__ import annotations

import re
from datetime import date
from typing import List, Optional

import pdfplumber
from pypdf import PdfReader

from core.models import ParsedPDF


def read_pdf(file_bytes: bytes) -> ParsedPDF:
    text_by_page: List[str] = []
    tables_by_page: List[List[List[List[Optional[str]]]]] = []

    from io import BytesIO

    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        # First pass: extract text for all pages
        for p in pdf.pages:
            txt = p.extract_text() or ""
            text_by_page.append(txt)

        # Second pass: extract tables selectively
        for idx, p in enumerate(pdf.pages):
            txt = (text_by_page[idx] or "").lower()
            should_extract_tables = (idx in (0, 1) or ("policy year" in txt))
            if should_extract_tables:
                try:
                    tables = p.extract_tables() or []
                except Exception:
                    tables = []
            else:
                tables = []
            tables_by_page.append(tables)

    # Fallback: if almost no text, try pypdf extraction
    if sum(len(t.strip()) for t in text_by_page) < 50:
        reader = PdfReader(file_bytes)
        text_by_page = [(page.extract_text() or "") for page in reader.pages]

    return ParsedPDF(
        text_by_page=text_by_page,
        tables_by_page=tables_by_page,
        page_count=len(text_by_page),
    )


def extract_bi_generation_date(page_text: str) -> Optional[date]:
    """
    Extract BI/Quote generation date from page-1 text.

    Supports common BI patterns like:
      - "BI (Quote) Date : 31/03/2023"
      - "Date of Quote: 31-03-2023"
      - "Quotation Date 31.03.2023"
    Returns a datetime.date or None if not found.
    """
    t = (page_text or "").replace("\n", " ")

    patterns = [
        r"(?:BI\s*\(Quote\)\s*Date|Quote\s*Date|Quotation\s*Date|Date\s*of\s*Quote)\s*[:\-]?\s*([0-3]?\d)[/\-\.]([01]?\d)[/\-\.]((?:19|20)\d{2})",
        r"(?:BI\s*Date|BI\s*Generation\s*Date)\s*[:\-]?\s*([0-3]?\d)[/\-\.]([01]?\d)[/\-\.]((?:19|20)\d{2})",
    ]

    for p in patterns:
        m = re.search(p, t, flags=re.IGNORECASE)
        if m:
            dd = int(m.group(1))
            mm = int(m.group(2))
            yy = int(m.group(3))
            try:
                return date(yy, mm, dd)
            except Exception:
                return None

    return None
from __future__ import annotations

import re
from datetime import date
from typing import List, Optional

import pdfplumber
from pypdf import PdfReader

from core.models import ParsedPDF


def read_pdf(file_bytes: bytes) -> ParsedPDF:
    text_by_page: List[str] = []
    tables_by_page: List[List[List[List[Optional[str]]]]] = []

    from io import BytesIO

    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        # First pass: extract text for all pages
        for p in pdf.pages:
            txt = p.extract_text() or ""
            text_by_page.append(txt)

        # Second pass: extract tables selectively
        for idx, p in enumerate(pdf.pages):
            txt = (text_by_page[idx] or "").lower()
            should_extract_tables = (idx in (0, 1) or ("policy year" in txt))
            if should_extract_tables:
                try:
                    tables = p.extract_tables() or []
                except Exception:
                    tables = []
            else:
                tables = []
            tables_by_page.append(tables)

    # Fallback: if almost no text, try pypdf extraction
    if sum(len(t.strip()) for t in text_by_page) < 50:
        reader = PdfReader(file_bytes)
        text_by_page = [(page.extract_text() or "") for page in reader.pages]

    return ParsedPDF(
        text_by_page=text_by_page,
        tables_by_page=tables_by_page,
        page_count=len(text_by_page),
    )


def extract_bi_generation_date(page_text: str) -> Optional[date]:
    """
    Extract BI/Quote generation date from page-1 text.

    Supports common BI patterns like:
      - "BI (Quote) Date : 31/03/2023"
      - "Date of Quote: 31-03-2023"
      - "Quotation Date 31.03.2023"
    Returns a datetime.date or None if not found.
    """
    t = (page_text or "").replace("\n", " ")

    patterns = [
        r"(?:BI\s*\(Quote\)\s*Date|Quote\s*Date|Quotation\s*Date|Date\s*of\s*Quote)\s*[:\-]?\s*([0-3]?\d)[/\-\.]([01]?\d)[/\-\.]((?:19|20)\d{2})",
        r"(?:BI\s*Date|BI\s*Generation\s*Date)\s*[:\-]?\s*([0-3]?\d)[/\-\.]([01]?\d)[/\-\.]((?:19|20)\d{2})",
    ]

    for p in patterns:
        m = re.search(p, t, flags=re.IGNORECASE)
        if m:
            dd = int(m.group(1))
            mm = int(m.group(2))
            yy = int(m.group(3))
            try:
                return date(yy, mm, dd)
            except Exception:
                return None

    return None
