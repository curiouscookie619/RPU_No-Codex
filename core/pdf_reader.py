from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple
import re
from datetime import date, datetime

import pdfplumber
from pypdf import PdfReader

from .models import ParsedPDF


DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\b")


def _parse_date_token(token: str) -> Optional[date]:
    m = DATE_RE.search(token)
    if not m:
        return None
    d = int(m.group(1))
    mon = m.group(2).title()
    y = int(m.group(3))
    try:
        return datetime.strptime(f"{d} {mon} {y}", "%d %b %Y").date()
    except Exception:
        return None


def read_pdf(file_bytes: bytes) -> ParsedPDF:
    text_by_page: List[str] = []
    tables_by_page: List[List[List[List[Optional[str]]]]] = []

    from io import BytesIO

    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        for p in pdf.pages:
            txt = p.extract_text() or ""
            text_by_page.append(txt)
            try:
                tables = p.extract_tables() or []
            except Exception:
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


def extract_bi_generation_date(text_page1: str) -> Optional[date]:
    # Heuristic: pick the first date token found on page 1.
    # (In the GIS BIs, the quote/BI date is printed at top and appears early in extracted text.)
    for m in DATE_RE.finditer(text_page1):
        dt = _parse_date_token(m.group(0))
        if dt:
            return dt
    return None
