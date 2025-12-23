from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from core.models import ParsedPDF, ExtractedFields, ComputedOutputs
from core.pdf_reader import extract_bi_generation_date
from core.date_logic import derive_rcd_and_rpu_dates
from products.base import ProductHandler


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
    return _clean_text(s).lower()


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
    out: List[List[List[Optional[str]]]] = []
    for page_tables in (parsed.tables_by_page or []):
        for tb in (page_tables or []):
            if tb:
                out.append(tb)
    return out


def _join_text(parsed: ParsedPDF) -> str:
    return "\n".join(parsed.text_by_page or [])


def _find_value_in_tables(
    tables_by_page: List[List[List[List[Optional[str]]]]],
    row_contains: str,
) -> Optional[float]:
    """
    For multi-column tables (like Premium Summary), find a row that contains row_contains
    and return the LAST numeric cell in that row.
    """
    needle = row_contains.lower()
    for page_tables in (tables_by_page or []):
        for tb in (page_tables or []):
            for row in (tb or []):
                if not row:
                    continue
                row_text = " ".join(_clean_text(c).lower() for c in row if c is not None)
                if needle in row_text:
                    # pick last numeric cell
                    for c in reversed(row):
                        n = _to_number(c)
                        if n is not None:
                            return n
    return None


def _income_segments(schedule_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Compress consecutive policy years with the same income into segments:
      [{"income": 497850, "start_year": 2, "end_year": 12, "years": 11}, ...]
    """
    segs: List[Dict[str, Any]] = []
    prev_income: Optional[float] = None
    seg_start: Optional[int] = None
    seg_end: Optional[int] = None

    def push(income, start, end):
        if income is None or income == 0 or start is None or end is None:
            return
        segs.append(
            {"income": income, "start_year": start, "end_year": end, "years": (end - start + 1)}
        )

    for r in (schedule_rows or []):
        py = r.get("policy_year")
        inc = r.get("income")

        if py is None:
            continue

        if inc is None or inc == 0:
            push(prev_income, seg_start, seg_end)
            prev_income, seg_start, seg_end = None, None, None
            continue

        if prev_income is None:
            prev_income, seg_start, seg_end = inc, py, py
        elif inc == prev_income and py == (seg_end + 1):
            seg_end = py
        else:
            push(prev_income, seg_start, seg_end)
            prev_income, seg_start, seg_end = inc, py, py

    push(prev_income, seg_start, seg_end)
    return segs


def _last_non_null(schedule_rows: List[Dict[str, Any]], key: str) -> Optional[float]:
    for r in reversed(schedule_rows or []):
        v = r.get(key)
        if v is not None:
            try:
                return float(v)
            except Exception:
                return None
    return None


# -------------------------
# Handler
# -------------------------

class GISHandler(ProductHandler):
    product_id = "GIS"

    def detect(self, parsed: ParsedPDF) -> Tuple[float, Dict[str, Any]]:
        t = _join_text(parsed).lower()
        if "guaranteed income star" in t:
            return 0.95, {"match": "contains 'guaranteed income star'"}
        if "guaranteed income" in t and "star" in t:
            return 0.70, {"match": "contains 'guaranteed income' and 'star'"}
        return 0.0, {"match": "no"}

    def extract(self, parsed: ParsedPDF) -> ExtractedFields:
        # BI date from page 1 text
        page1 = (parsed.text_by_page or [""])[0]
        bi_date = extract_bi_generation_date(page1) or date.today()

        # Build kv from 2-column style rows across all tables
        kv: Dict[str, str] = {}
        for tb in _flatten_tables(parsed):
            for row in (tb or []):
                if not row or len(row) < 2:
                    continue
                k = _norm_key(row[0])
                v = _clean_text(row[1])
                if k:
                    kv[k] = v

        product_name = kv.get("name of the product") or "Edelweiss Tokio Life- Guaranteed Income STAR"
        uin = kv.get("unique identification no.") or kv.get("uin")
        proposer = kv.get("name of the prospect/policyholder")

        mode = (kv.get("mode of payment of premium") or "Annual").title()

        pt = _to_int(kv.get("policy term (in years)") or kv.get("policy term")) or 0
        ppt = _to_int(kv.get("premium payment term (in years)") or kv.get("premium payment term")) or 0

        age = _to_int(kv.get("age (years)") or kv.get("age") or "")
        gender = (kv.get("gender of the life assured") or kv.get("gender") or "").title() or None

        # Premium Summary table on page 2: "Instalment Premium without GST"
        instalment_premium_wo_gst = _find_value_in_tables(
            parsed.tables_by_page,
            "Instalment Premium without GST",
        )

        # Sum Assured on death in kv sometimes absent; keep best-effort
        sum_assured = _to_number(
            kv.get("sum assured on death (at inception of the policy) rs.")
            or kv.get("sum assured on death (at inception of the policy) rs")
            or kv.get("sum assured on death (at inception of the policy)")
            or ""
        )

        schedule_rows = self._extract_schedule(parsed)

        income_duration = _to_int(
            kv.get("income duration (in years)")
            or kv.get("'income duration' (in years)")
            or ""
        )

        payout_freq = (kv.get("income benefit pay-out frequency") or "Annual").title() or None
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
            annualized_premium_excl_tax=instalment_premium_wo_gst,
            income_start_point_text=kv.get("income start point"),
            income_duration_years=income_duration,
            income_payout_frequency=payout_freq,
            income_payout_type=payout_type,
            sum_assured_on_death=sum_assured,
            schedule_rows=schedule_rows,
        )

    def _extract_schedule(self, parsed: ParsedPDF) -> List[Dict[str, Any]]:
        rows_out: List[Dict[str, Any]] = []
        headers: Optional[List[str]] = None
        header_keys: Optional[List[str]] = None

        all_tables = _flatten_tables(parsed)

        for tb in all_tables:
            if not tb or len(tb) < 2:
                continue

            # find header row containing "Policy Year" within first 6 rows
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

                # merge next 2 rows to handle multi-row headers
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

        segments = _income_segments(extracted.schedule_rows)
        total_income = sum(seg["income"] * seg["years"] for seg in segments)

        maturity = _last_non_null(extracted.schedule_rows, "maturity")
        last_death = _last_non_null(extracted.schedule_rows, "death")

        # months paid vs total
        months_paid = max(0, (ptd.year - rcd.year) * 12 + (ptd.month - rcd.month))
        months_payable_total = int(extracted.ppt_years) * 12 if extracted.ppt_years else 0
        rpu_factor = round(months_paid / months_payable_total, 6) if months_payable_total else 0.0

        fully_paid = {
            "instalment_premium_without_gst": extracted.annualized_premium_excl_tax,
            "income_segments": segments,
            "total_income": float(total_income),
            "maturity": float(maturity) if maturity is not None else None,
            # keep legacy key used by PDF renderer:
            "death_inception": float(last_death) if last_death is not None else None,
            # also provide explicit label:
            "death_last_year": float(last_death) if last_death is not None else None,
        }

        reduced_paid_up = {
            "rpu_factor": rpu_factor,
            "income_segments_scaled": [
                {**seg, "income_scaled": float(seg["income"]) * rpu_factor} for seg in segments
            ],
            "total_income": float(total_income) * rpu_factor,
            "maturity": (float(maturity) * rpu_factor) if maturity is not None else None,
            "death_scaled": (float(last_death) * rpu_factor) if last_death is not None else None,
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
            reduced_paid_up=reduced_paid_up,
            notes=[
                "Illustrative values assuming non-payment of the next premium and policy becoming paid-up after grace period."
            ],
        )
