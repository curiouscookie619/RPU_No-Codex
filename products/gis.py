from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

from products.base import ProductHandler
from core.date_logic import derive_rcd_and_rpu_dates


# -------------------------
# Helpers
# -------------------------

def _clean_text(s: str) -> str:
    return " ".join((s or "").replace("\n", " ").split()).strip()


def _norm_key(s: str) -> str:
    s = _clean_text(s)
    s = s.replace(" :", ":")
    if s.endswith(":"):
        s = s[:-1]
    return s.lower()


def _to_int(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\d+", text.replace(",", ""))
    return int(m.group()) if m else None


def _to_number(text: str) -> Optional[int]:
    if not text:
        return None
    text = text.replace(",", "").replace("₹", "").strip()
    if text in {"-", "—", ""}:
        return None
    try:
        return int(float(text))
    except Exception:
        return None


def _header_key(h: str) -> str:
    h = h.lower()
    if "policy year" in h:
        return "policy_year"
    if "income" in h or "survival" in h:
        return "income"
    if "maturity" in h or "lump" in h:
        return "maturity"
    if "death" in h:
        return "death"
    return ""


# -------------------------
# Handler
# -------------------------

class GISHandler(ProductHandler):
    product_id = "GIS"
    product_name = "Guaranteed Income STAR"

    def matches(self, parsed) -> bool:
        return "guaranteed income star" in parsed.text.lower()

    # -------------------------
    # Extraction
    # -------------------------

    def extract(self, parsed):
        kv: Dict[str, str] = {}

        # -------- PAGE 1: key-value tables --------
        for tb in parsed.tables:
            for row in tb:
                if not row or len(row) < 2:
                    continue
                k = _norm_key(row[0])
                v = _clean_text(row[1])
                if k:
                    kv[k] = v

        product_name = kv.get("name of the product") or self.product_name
        uin = kv.get("unique identification no.") or kv.get("uin")

        proposer = kv.get("name of the prospect/policyholder")
        mode = kv.get("mode of payment of premium", "Annual").title()

        pt = _to_int(kv.get("policy term (in years)") or kv.get("policy term")) or 0
        ppt = _to_int(
            kv.get("premium payment term (in years)") or kv.get("premium payment term")
        ) or 0

        sum_assured = _to_number(
            kv.get("sum assured on death (at inception of the policy)")
        )

        bi_date = parsed.bi_generation_date

        # -------- PAGE 2+: schedule table --------
        schedule_rows = self._extract_schedule(parsed)

        return self.schema(
            product_name=product_name,
            product_uin=uin,
            bi_generation_date=bi_date,
            proposer_name_transient=proposer,
            mode=mode,
            policy_term_years=pt,
            ppt_years=ppt,
            sum_assured_on_death=sum_assured,
            schedule_rows=schedule_rows,
        )

    # -------------------------
    # Schedule extraction
    # -------------------------

    def _extract_schedule(self, parsed) -> List[Dict[str, Any]]:
        rows_out: List[Dict[str, Any]] = []
        headers: Optional[List[str]] = None
        header_keys: Optional[List[str]] = None

        for tb in parsed.tables:
            if not tb or len(tb) < 2:
                continue

            # find header row (can be row 0–5)
            header_row_idx = None
            for i in range(min(6, len(tb))):
                txt = " ".join((c or "") for c in tb[i]).lower()
                if "policy" in txt and "year" in txt:
                    header_row_idx = i
                    break

            if header_row_idx is not None:
                base = tb[header_row_idx]
                merged = [(c or "").strip() for c in base]

                # merge next two rows (multi-row headers)
                for j in range(header_row_idx + 1, min(header_row_idx + 3, len(tb))):
                    for col in range(len(merged)):
                        nxt = (tb[j][col] or "").strip()
                        if nxt:
                            merged[col] = f"{merged[col]} {nxt}".strip()

                headers = merged
                header_keys = [_header_key(h) for h in headers]
                data_rows = tb[header_row_idx + 3 :]
            else:
                if headers is None:
                    continue
                data_rows = tb

            for r in data_rows:
                if not r or not r[0]:
                    continue

                row_obj: Dict[str, Any] = {}
                for idx, cell in enumerate(r):
                    if idx >= len(header_keys):
                        continue
                    key = header_keys[idx]
                    if not key:
                        continue
                    row_obj[key] = _to_number(cell)

                if "policy_year" in row_obj:
                    rows_out.append(row_obj)

        return rows_out

    # -------------------------
    # Calculation
    # -------------------------

    def calculate(self, extracted, ptd: date):
        rcd, rpu_date, grace_days = derive_rcd_and_rpu_dates(
            bi_date=extracted.bi_generation_date,
            ptd=ptd,
            mode=extracted.mode,
        )

        total_income = sum(
            r.get("income", 0) or 0 for r in extracted.schedule_rows
        )

        maturity = (
            extracted.schedule_rows[-1].get("maturity")
            if extracted.schedule_rows
            else None
        )

        fully_paid = {
            "total_income_annual": total_income,
            "maturity": maturity,
            "death_inception": extracted.sum_assured_on_death,
        }

        months_paid = max(0, (ptd.year - rcd.year) * 12 + (ptd.month - rcd.month))
        total_months = extracted.ppt_years * 12
        rpu_factor = round(months_paid / total_months, 4) if total_months else 0

        reduced_paid = {
            "rpu_factor": rpu_factor,
            "total_income": int(total_income * rpu_factor),
            "maturity": int((maturity or 0) * rpu_factor) if maturity else None,
            "death_scaled": int(
                (extracted.sum_assured_on_death or 0) * rpu_factor
            )
            if extracted.sum_assured_on_death
            else None,
        }

        return self.output_schema(
            rcd=rcd,
            ptd=ptd,
            rpu_date=rpu_date,
            grace_period_days=grace_days,
            fully_paid=fully_paid,
            reduced_paid_up=reduced_paid,
            notes=[
                "Illustrative values assuming non-payment of the next premium and policy becoming paid-up after grace period."
            ],
        )
