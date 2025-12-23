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
            should_extract_tables = (
                idx in (0, 1)  # page 1 & 2
                or ("policy year" in txt)
            )
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
