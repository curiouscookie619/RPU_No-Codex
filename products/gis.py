from __future__ import annotations

import re
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
    s = _clean_text(s)
    return s.lower()


def _to_int(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\d+", text.replace(",", ""))
    return int(m.group()) if m else None


def _to_number(text: Any) -> Optional[int]:
    if text is None:
        return None
    s = _clean_text(str(text))
    if s in {"-", "—", ""}:
        return None
    s = s.replace(",", "").replace("₹", "").strip()
    try:
        return int(float(s))
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
    if "death benefit" in h or h.strip() == "death benefit" or "death" in h:
        return "death"
    return ""


class GISHandler(ProductHandler):
    product_id = "GIS"
    product_display_name = "Guaranteed Income STAR"

    # --- REQUIRED by ProductHandler (abstract) ---
    def detect(self, parsed_pdf) -> float:
        """
        Return confidence score 0..1 that this PDF is this product.
        """
        t = (parsed_pdf.text or "").lower()
        if "guaranteed income star" in t:
            return 0.95
        if "edelweiss" in t and "guaranteed income" in t and "star" in t:
            return 0.75
        return 0.0

    def extract(self, parsed_pdf):
        """
        Return extracted schema object (whatever your base expects).
        This relies on your existing ParsedPDF providing:
          - text
          - tables (list of tables; each is list of rows; each row list of cells)
          - bi_generation_date (already parsed elsewhere)
        """
        kv: Dict[str, str] = {}

        # Page-1 key-value tables are present within parsed_pdf.tables as 2-column rows.
        for tb in (parsed_pdf.tables or []):
            for row in tb:
                if not row or len(row) < 2:
                    continue
                k = _norm_key(row[0])
                v = _clean_text(row[1])
                if k:
                    kv[k] = v

        product_name = kv.get("name of the product") or self.product_display_name
        uin = kv.get("unique identification no.") or kv.get("uin")

        proposer = kv.get("name of the prospect/policyholder")
        mode = (kv.get("mode of payment of premium") or "Annual").title()

        pt = _to_int(kv.get("policy term (in years)") or kv.get("policy term") or "") or 0
        ppt = _to_int(
            kv.get("premium payment term (in years)") or kv.get("premium payment term") or ""
        ) or 0

        sum_assured = _to_number(
            kv.get("sum assured on death (at inception of the policy) rs.")
            or kv.get("sum assured on death (at inception of the policy) rs")
            or kv.get("sum assured on death (at inception of the policy)")
            or ""
        )

        schedule_rows = self._extract_schedule(parsed_pdf)

        # Create the schema object using the base helper (your repo uses .schema())
        return self.schema(
            product_name=product_name,
            product_uin=uin,
            bi_generation_date=parsed_pdf.bi_generation_date,
            proposer_name_transient=proposer,
            life_assured_age=_to_int(kv.get("age (years)") or ""),
            life_assured_gender=(kv.get("gender of the life assured") or "").title() or None,
            mode=mode,
            policy_term_years=pt,
            ppt_years=ppt,
            annualized_premium_excl_tax=_to_number(kv.get("instalment premium (excluding taxes) (in rupees)") or ""),
            income_start_point_text=kv.get("income start point"),
            income_duration_years=_to_int(kv.get("'income duration' (in years)") or kv.get("income duration (in years)") or ""),
            income_payout_frequency=(kv.get("income benefit pay-out frequency") or "Yearly").title(),
            income_payout_type=(kv.get("income benefit pay-out type") or "").title() or None,
            sum_assured_on_death=sum_assured,
            schedule_rows=schedule_rows,
        )

    def _extract_schedule(self, parsed_pdf) -> List[Dict[str, Any]]:
        rows_out: List[Dict[str, Any]] = []
        headers: Optional[List[str]] = None
        header_keys: Optional[List[str]] = None

        for tb in (parsed_pdf.tables or []):
            if not tb or len(tb) < 2:
                continue

            # Find row containing "Policy Year" within first few rows
            header_row_idx = None
            for i in range(min(6, len(tb))):
                txt = " ".join((c or "") for c in (tb[i] or [])).lower()
                if "policy" in txt and "year" in txt:
                    header_row_idx = i
                    break

            if header_row_idx is not None:
                base = tb[header_row_idx] or []
                merged = [(c or "").strip() for c in base]

                # Merge next 2 rows to handle multi-row headers
                for j in range(header_row_idx + 1, min(header_row_idx + 3, len(tb))):
                    r2 = tb[j] or []
                    for col in range(min(len(merged), len(r2))):
                        nxt = (r2[col] or "").strip()
                        if nxt:
                            merged[col] = f"{merged[col]} {nxt}".strip()

                headers = merged
                header_keys = [_header_key(h) for h in headers]
                data_rows = tb[min(len(tb), header_row_idx + 3):]
            else:
                # continuation table pages
                if headers is None or header_keys is None:
                    continue
                data_rows = tb

            for r in data_rows:
                if not r:
                    continue

                # Row must have policy_year numeric somewhere; else skip
                row_obj: Dict[str, Any] = {}
                for idx, cell in enumerate(r):
                    if idx >= len(header_keys):
                        continue
                    key = header_keys[idx]
                    if not key:
                        continue
                    if key == "policy_year":
                        row_obj[key] = _to_int(str(cell))
                    else:
                        row_obj[key] = _to_number(cell)

                if row_obj.get("policy_year"):
                    rows_out.append(row_obj)

        return rows_out

    def calculate(self, extracted, ptd):
        rcd, rpu_date, grace_days = derive_rcd_and_rpu_dates(
            bi_date=extracted.bi_generation_date,
            ptd=ptd,
            mode=extracted.mode,
        )

        # Fully paid income (sum of income column)
        total_income = sum((r.get("income") or 0) for r in extracted.schedule_rows)

        maturity = extracted.schedule_rows[-1].get("maturity") if extracted.schedule_rows else None

        fully_paid = {
            "total_income_annual": int(total_income),
            "maturity": int(maturity) if maturity else None,
            "death_inception": extracted.sum_assured_on_death,
        }

        # RPU factor based on months paid / total months in PPT
        months_paid = max(0, (ptd.year - rcd.year) * 12 + (ptd.month - rcd.month))
        total_months = int(extracted.ppt_years) * 12 if extracted.ppt_years else 0
        rpu_factor = round(months_paid / total_months, 4) if total_months else 0.0

        reduced_paid = {
            "rpu_factor": rpu_factor,
            "total_income": int(total_income * rpu_factor),
            "maturity": int((maturity or 0) * rpu_factor) if maturity else None,
            "death_scaled": int((extracted.sum_assured_on_death or 0) * rpu_factor)
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
