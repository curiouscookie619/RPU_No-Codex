from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from core.models import ParsedPDF, ExtractedFields, ComputedOutputs
from core.pdf_reader import extract_bi_generation_date
from core.date_logic import derive_rcd_and_rpu_dates
from .base import ProductHandler


# -------------------------
# Helpers
# -------------------------

def _clean_text(s: Any) -> str:
    return " ".join(str(s or "").replace("\n", " ").split()).strip()


def _norm_key(s: Any) -> str:
    s = _clean_text(s)
    s = s.replace(" :", ":")
    if s.endswith(":"):
        s = s[:-1]
    s = _clean_text(s)
    return s.lower()


def _to_int(text: Any) -> Optional[int]:
    s = _clean_text(text)
    if not s:
        return None
    m = re.search(r"\d+", s.replace(",", ""))
    return int(m.group()) if m else None


def _to_number(text: Any) -> Optional[float]:
    s = _clean_text(text)
    if not s or s in {"-", "—"}:
        return None
    s = s.replace(",", "").replace("₹", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _header_key(h: str) -> str:
    h = _clean_text(h).lower()
    if "policy year" in h:
        return "policy_year"
    if "income" in h or "survival" in h or "loyalty addition" in h:
        return "income"
    if "maturity" in h or "lump sum" in h or "lumpsum" in h:
        return "maturity"
    if "death" in h:
        return "death"
    return ""


def _flatten_tables(parsed: ParsedPDF) -> List[List[List[Optional[str]]]]:
    """
    ParsedPDF.tables_by_page is:
      List[ page -> List[ table -> List[ row -> List[cell] ] ] ]
    We flatten page order while keeping tables as-is.
    """
    out: List[List[List[Optional[str]]]] = []
    for page_tables in (parsed.tables_by_page or []):
        for tb in (page_tables or []):
            if tb:
                out.append(tb)
    return out


def _join_text(parsed: ParsedPDF) -> str:
    return "\n".join(parsed.text_by_page or [])


# -------------------------
# Handler
# -------------------------

class GISHandler(ProductHandler):
    product_id = "GIS"

    def detect(self, parsed: ParsedPDF) -> Tuple[float, Dict[str, Any]]:
        text = _join_text(parsed).lower()
        if "guaranteed income star" in text:
            return 0.95, {"match": "contains 'guaranteed income star'"}
        # fallback fuzzy match
        if "guaranteed income" in text and "star" in text:
            return 0.70, {"match": "contains 'guaranteed income' and 'star'"}
        return 0.0, {"match": "no"}

    def extract(self, parsed: ParsedPDF) -> ExtractedFields:
        # BI date: use your existing heuristic (page 1)
        bi_date = extract_bi_generation_date((parsed.text_by_page or [""])[0]) or date.today()

        # Build a key-value dictionary from all 2-col rows in all tables
        kv: Dict[str, str] = {}
        all_tables = _flatten_tables(parsed)

        for tb in all_tables:
            for row in (tb or []):
                if not row or len(row) < 2:
                    continue
                k = _norm_key(row[0])
                v = _clean_text(row[1])
                if k:
                    kv[k] = v

        product_name = kv.get("name of the product") or "Guaranteed Income STAR"
        uin = kv.get("unique identification no.") or kv.get("uin")

        proposer = kv.get("name of the prospect/policyholder")

        mode = (kv.get("mode of payment of premium") or "Annual").title()

        pt = _to_int(kv.get("policy term (in years)") or kv.get("policy term")) or 0
        ppt = _to_int(kv.get("premium payment term (in years)") or kv.get("premium payment term")) or 0

        # Sum Assured on death key varies; handle common variants
        sum_assured = _to_number(
            kv.get("sum assured on death (at inception of the policy) rs.")
            or kv.get("sum assured on death (at inception of the policy) rs")
            or kv.get("sum assured on death (at inception of the policy)")
            or ""
        )

        # schedule rows
        schedule_rows = self._extract_schedule(parsed)

        # Optional fields: try best effort (not critical yet)
        age = _to_int(kv.get("age (years)") or kv.get("age") or "")
        gender = (kv.get("gender of the life assured") or kv.get("gender") or "").title() or None
        annual_prem = _to_number(kv.get("instalment premium (excluding taxes) (in rupees)") or "")

        income_duration = _to_int(kv.get("income duration (in years)") or kv.get("'income duration' (in years)") or "")

        payout_freq = (kv.get("income benefit pay-out frequency") or "Yearly").title() or None
        payout_type = (kv.get("income benefit pay-out type") or "").title() or None

        return ExtractedFields(
            product_name=product_name,
            product_uin=uin,
            bi_generation_date=bi_date,
            proposer_name_transient=proposer,
            life_assured_age=age,
            life_assured_gender=gender,
            mode=mode,
            policy_term_years=pt,
            ppt_years=ppt,
            annualized_premium_excl_tax=annual_prem,
            income_start_point_text=kv.get("income start point"),
            income_duration_years=income_duration,
            income_payout_frequency=payout_freq,
            income_payout_type=payout_type,
            sum_assured_on_death=sum_assured,
            schedule_rows=schedule_rows,
        )

    def _extract_schedule(self, parsed: ParsedPDF) -> List[Dict[str, Any]]:
        """
        Extract year-wise schedule rows.
        Handles multi-row headers by merging up to next 2 rows after the header row.
        """
        rows_out: List[Dict[str, Any]] = []
        headers: Optional[List[str]] = None
        header_keys: Optional[List[str]] = None

        all_tables = _flatten_tables(parsed)

        for tb in all_tables:
            if not tb or len(tb) < 2:
                continue

            # Find header row containing "Policy Year" within first few rows
            header_row_idx = None
            for i in range(min(6, len(tb))):
                row = tb[i] or []
                txt = " ".join((_clean_text(c) for c in row)).lower()
                if "policy" in txt and "year" in txt:
                    header_row_idx = i
                    break

            if header_row_idx is not None:
                base = tb[header_row_idx] or []
                merged = [(_clean_text(c) if c is not None else "") for c in base]

                # merge next 2 rows for multi-row header tables
                for j in range(header_row_idx + 1, min(header_row_idx + 3, len(tb))):
                    r2 = tb[j] or []
                    for col in range(min(len(merged), len(r2))):
                        nxt = _clean_text(r2[col])
                        if nxt:
                            merged[col] = f"{merged[col]} {nxt}".strip()

                headers = merged
                header_keys = [_header_key(h) for h in headers]
                data_rows = tb[min(len(tb), header_row_idx + 3):]
            else:
                # continuation: reuse previous header
                if headers is None or header_keys is None:
                    continue
                data_rows = tb

            for r in (data_rows or []):
                if not r:
                    continue

                row_obj: Dict[str, Any] = {}
                for idx, cell in enumerate(r):
                    if idx >= len(header_keys):
                        continue
                    key = header_keys[idx]
                    if not key:
                        continue
                    if key == "policy_year":
                        row_obj[key] = _to_int(cell)
                    else:
                        row_obj[key] = _to_number(cell)

                if row_obj.get("policy_year"):
                    rows_out.append(row_obj)

        return rows_out

    def calculate(self, extracted: ExtractedFields, ptd: date) -> ComputedOutputs:
        rcd, rpu_date, grace_days = derive_rcd_and_rpu_dates(
            bi_date=extracted.bi_generation_date,
            ptd=ptd,
            mode=extracted.mode,
        )

        # Fully paid (based on extracted schedule)
        total_income = sum((r.get("income") or 0) for r in extracted.schedule_rows)
        maturity = extracted.schedule_rows[-1].get("maturity") if extracted.schedule_rows else None

        fully_paid = {
            "total_income_annual": float(total_income),
            "maturity": float(maturity) if maturity is not None else None,
            "death_inception": extracted.sum_assured_on_death,
        }

        # Paid months between RCD and PTD
        months_paid = max(0, (ptd.year - rcd.year) * 12 + (ptd.month - rcd.month))
        months_payable_total = int(extracted.ppt_years) * 12 if extracted.ppt_years else 0
        rpu_factor = round(months_paid / months_payable_total, 6) if months_payable_total else 0.0

        reduced_paid = {
            "rpu_factor": rpu_factor,
            "total_income": float(total_income) * rpu_factor,
            "maturity": (float(maturity) * rpu_factor) if maturity is not None else None,
            "death_scaled": (float(extracted.sum_assured_on_death) * rpu_factor)
            if extracted.sum_assured_on_death is not None
            else None,
        }

        return ComputedOutputs(
            rcd=rcd,
            ptd=ptd,
            rpu_date=rpu_date,
            grace_period_days=grace_days,
            months_paid=months_paid,
            months_payable_total=months_payable_total,
            rpu_factor=rpu_factor,
            fully_paid=fully_paid,
            reduced_paid_up=reduced_paid,
            notes=[
                "Illustrative values assuming non-payment of the next premium and policy becoming paid-up after grace period."
            ],
        )
