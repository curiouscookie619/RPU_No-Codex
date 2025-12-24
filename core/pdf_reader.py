from __future__ import annotations

import re
from datetime import date
from typing import List, Optional

import pdfplumber
from pypdf import PdfReader

from core.models import ParsedPDF


def read_pdf(file_bytes: bytes) -> ParsedPDF:
    """Read PDF into text-by-page and tables-by-page.

    Performance note:
    - Full-table extraction on every page can be slow.
    - For these BIs, we almost always need page 1 (summary tables) and
      the schedule pages (Policy Year tables), which are commonly split
      across page 2 & 3.

    Heuristic:
    - Always extract tables for pages 1–3 (0,1,2).
    - Also extract tables for any page that contains 'policy year' in its text.
    - Also extract tables for a page immediately following a 'policy year' page
      (continuation pages often omit the header).
    """

    text_by_page: List[str] = []
    tables_by_page: List[List[List[List[Optional[str]]]]] = []

    from io import BytesIO

    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        # First pass: extract text for all pages
        for p in pdf.pages:
            text_by_page.append(p.extract_text() or "")

        # Second pass: extract tables selectively
        #
        # We need schedule tables from 'Policy Year' pages, and those tables can
        # continue across multiple pages without repeating the 'Policy Year' header.
        # So once we detect a schedule page, we keep extracting tables for all
        # subsequent pages (until the document ends).
        schedule_started = False
        for idx, p in enumerate(pdf.pages):
            txt = (text_by_page[idx] or "").lower()
            has_policy_year = ("policy year" in txt)

            # Always extract tables for pages 1–2 (0,1) because they contain the
            # core summary blocks (premium summary, GST rates, etc.).
            #
            # For the schedule, start extracting when we detect 'policy year' and
            # continue for all later pages (continuation pages often omit the header).
            should_extract_tables = (idx in (0, 1)) or schedule_started or has_policy_year

            if should_extract_tables:
                try:
                    tables = p.extract_tables() or []
                except Exception:
                    tables = []
            else:
                tables = []

            tables_by_page.append(tables)

            if has_policy_year:
                schedule_started = True
    # Fallback: if almost no text, try pypdf extraction
    if sum(len(t.strip()) for t in text_by_page) < 50:
        reader = PdfReader(BytesIO(file_bytes))
        text_by_page = [(page.extract_text() or "") for page in reader.pages]

    return ParsedPDF(
        text_by_page=text_by_page,
        tables_by_page=tables_by_page,
        page_count=len(text_by_page),
    )


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def extract_bi_generation_date(page_text: str) -> Optional[date]:
    """Extract BI/Quote generation date from page-1 text.

    Supports patterns like:
      - "BI (Quote) Date : 31/03/2023"
      - "Date of Quote: 31-03-2023"
      - top-right date with month-name: "31 Mar 2023"
    """

    t = (page_text or "").replace("\n", " ")

    # 1) Labelled numeric dates
    labelled_patterns = [
        r"(?:BI\s*\(Quote\)\s*Date|Quote\s*Date|Quotation\s*Date|Date\s*of\s*Quote|BI\s*Date|BI\s*Generation\s*Date)\s*[:\-]?\s*([0-3]?\d)[/\-\.]([01]?\d)[/\-\.]((?:19|20)\d{2})",
    ]
    for p in labelled_patterns:
        m = re.search(p, t, flags=re.IGNORECASE)
        if m:
            dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return date(yy, mm, dd)
            except Exception:
                return None

    # 2) Unlabelled month-name date (common on top-right of BI): e.g. "31 Mar 2023"
    m2 = re.search(
        r"\b([0-3]?\d)\s+([A-Za-z]{3,9})\s+((?:19|20)\d{2})\b",
        t,
        flags=re.IGNORECASE,
    )
    if m2:
        dd = int(m2.group(1))
        mon_raw = m2.group(2).strip().lower()
        yy = int(m2.group(3))
        mm = _MONTHS.get(mon_raw)
        if mm:
            try:
                return date(yy, mm, dd)
            except Exception:
                return None

    return None