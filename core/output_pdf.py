from __future__ import annotations

from io import BytesIO
from typing import Any, Dict, List, Optional
from datetime import date

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def _fmt_money(v: Any) -> str:
    if v is None:
        return "-"
    try:
        n = float(v)
        return f"{n:,.0f}"
    except Exception:
        return str(v)


def render_one_pager(
    customer_name: str,
    product_name: str,
    summary: Dict[str, Any],
    fully_paid: Dict[str, Any],
    rpu: Dict[str, Any],
    notes: List[str],
) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    y = h - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, "Policy Benefits Summary (Neutral)")
    y -= 22

    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Customer (Proposer): {customer_name}")
    y -= 14
    c.drawString(40, y, f"Product: {product_name}")
    y -= 14

    for k in ["Mode", "PT", "PPT", "BI Date", "RCD", "PTD", "Assumed RPU Date (PTD + Grace)"]:
        if k in summary:
            c.drawString(40, y, f"{k}: {summary[k]}")
            y -= 14

    y -= 6
    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Fully Paid Benefits (as per BI)")
    y -= 16
    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Total Income Pay-outs (sum): {_fmt_money(fully_paid.get('total_income'))}")
    y -= 14
    c.drawString(40, y, f"Maturity / Lump Sum (at maturity): {_fmt_money(fully_paid.get('maturity'))}")
    y -= 14
    c.drawString(40, y, f"Death Benefit (at inception / schedule): {_fmt_money(fully_paid.get('death_inception'))}")
    y -= 18

    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Reduced Paid-Up Benefits (illustrative, if next premium is not paid)")
    y -= 16
    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"RPU Factor: {rpu.get('rpu_factor')}")
    y -= 14
    c.drawString(40, y, f"Reduced Income Pay-outs (sum): {_fmt_money(rpu.get('total_income'))}")
    y -= 14
    c.drawString(40, y, f"Reduced Maturity / Lump Sum: {_fmt_money(rpu.get('maturity'))}")
    y -= 14
    c.drawString(40, y, f"Reduced Death Benefit (scaled): {_fmt_money(rpu.get('death_scaled'))}")
    y -= 18

    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Notes / Assumptions")
    y -= 16
    c.setFont("Helvetica", 9)
    for note in notes[:10]:
        c.drawString(50, y, f"- {note}")
        y -= 12
        if y < 60:
            c.showPage()
            y = h - 40
            c.setFont("Helvetica", 9)

    c.showPage()
    c.save()
    return buf.getvalue()
