from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from dateutil.relativedelta import relativedelta

from core.models import ParsedPDF, ExtractedFields, ComputedOutputs
from core.pdf_reader import extract_bi_generation_date
from core.date_logic import normalize_mode, derive_rcd, count_paid_months, add_grace


def _to_number(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    st = str(s).strip()
    if st in {"", "-", "—"}:
        return None
    # remove currency and commas
    st = re.sub(r"[^0-9.]", "", st)
    if st == "":
        return None
    try:
        return float(st)
    except Exception:
        return None


def _find_int_in_text(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else None


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower())


def _header_key(h: str) -> Optional[str]:
    hh = _norm_header(h)
    if "policy year" in hh:
        return "policy_year"
    if "age" in hh and "birthday" in hh:
        return "age"
    if "annualized" in hh or "single/" in hh:
        return "annualized_premium"
    if "income benefit" in hh or "survival benefit" in hh or "loyalty addition" in hh:
        return "income_payout_annual"
    if "maturity" in hh or "lump sum" in hh:
        return "maturity"
    if "death benefit" in hh:
        return "death_benefit"
    if "min guaranteed surrender value" in hh or "guaranteed surrender value" in hh or "(gsv)" in hh:
        return "gsv"
    if "special surrender" in hh or "(ssv)" in hh:
        return "ssv"
    if "surrender value payable" in hh:
        return "svp"
    return None


class GISHandler:
    product_id = "GIS"

    # From SL (brochure): Grace Period (monthly 15 days, others 30 days)
    def grace_period_days(self, mode_norm: str) -> int:
        return 15 if mode_norm.lower().startswith("month") else 30

    # From SL (brochure): Income Benefit Pay-out Multiplier Factor
    INCOME_MULTIPLIER = {
        "Yearly": 1.00,
        "Half-yearly": 0.49,
        "Quarterly": 0.2425,
        "Monthly": 0.08,
    }
    FREQ_TO_MONTHS = {"Yearly": 12, "Half-yearly": 6, "Quarterly": 3, "Monthly": 1}
    FREQ_TO_INSTALLMENTS_PER_YEAR = {"Yearly": 1, "Half-yearly": 2, "Quarterly": 4, "Monthly": 12}

    def detect(self, parsed: ParsedPDF) -> Tuple[float, Dict[str, Any]]:
        hay = "\n".join(parsed.text_by_page[:2]).lower()
        conf = 1.0 if "guaranteed income star" in hay else 0.0
        return conf, {"matched": "guaranteed income star" if conf else None}

    def extract(self, parsed: ParsedPDF) -> ExtractedFields:
        page1_text = parsed.text_by_page[0] if parsed.text_by_page else ""
        bi_date = extract_bi_generation_date(page1_text)
        if not bi_date:
            raise ValueError("Could not detect BI generation date on page 1.")

        # Parse key-value tables from page 1
        kv: Dict[str, str] = {}
        for tb in (parsed.tables_by_page[0] if parsed.tables_by_page else []):
            for row in tb:
                if not row or len(row) < 2:
                    continue
                k = (row[0] or "").strip()
                v = (row[1] or "").strip() if len(row) > 1 else ""
                if k:
                    kv[k] = v

        proposer_name = kv.get("Name of the Prospect/Policyholder") or kv.get("Name of the Prospect / Policyholder")
        la_age = _find_int_in_text(kv.get("Age (years)") or kv.get("Age (years) of the Prospect/Policyholder") or "")
        mode = normalize_mode(kv.get("Mode of Payment of Premium") or "")
        pt = _find_int_in_text(kv.get("Policy Term (in years)") or "") or 0
        ppt = _find_int_in_text(kv.get("Premium Payment Term (in years)") or "") or 0
        uin = kv.get("Unique Identification No.") or kv.get("Unique Identification No") or None
        product_name = kv.get("Name of the Product") or "Guaranteed Income STAR"

        # premium excluding taxes is on page 2 summary, but sometimes also appears as annualized premium in schedule table
        premium_excl = None
        if len(parsed.tables_by_page) > 1:
            for tb in parsed.tables_by_page[1]:
                for row in tb:
                    if not row or len(row) < 2:
                        continue
                    k = (row[0] or "").strip().lower()
                    if "excluding taxes" in k:
                        premium_excl = _to_number(row[1])
                        break
                if premium_excl is not None:
                    break

        # plan option metadata from page 1 kv table
        income_start = kv.get("Income Start Point")
        income_duration = _find_int_in_text(kv.get("'Income duration' (in years)") or kv.get("Income duration (in years)") or "")
        income_freq = (kv.get("Income Benefit Pay-out Frequency") or "Yearly").strip().title()
        # normalize
        if "Half" in income_freq:
            income_freq = "Half-yearly"
        if "Quarter" in income_freq:
            income_freq = "Quarterly"
        if "Month" in income_freq:
            income_freq = "Monthly"
        if "Year" in income_freq:
            income_freq = "Yearly"
        income_type = (kv.get("Income Benefit Pay-out type") or "").strip().title() or None

        sa_death = _to_number(kv.get("Basic Sum Assured on Death") or "")

        schedule_rows = self._extract_schedule(parsed)

        return ExtractedFields(
            product_name=product_name,
            product_uin=uin,
            bi_generation_date=bi_date,
            proposer_name_transient=proposer_name,
            life_assured_age=la_age,
            life_assured_gender=(kv.get("Gender of the Life Assured") or "").strip().title() or None,
            mode=mode,
            policy_term_years=pt,
            ppt_years=ppt,
            annualized_premium_excl_tax=premium_excl,
            income_start_point_text=income_start,
            income_duration_years=income_duration,
            income_payout_frequency=income_freq,
            income_payout_type=income_type,
            sum_assured_on_death=sa_death,
            schedule_rows=schedule_rows,
        )

    def _extract_schedule(self, parsed: ParsedPDF) -> List[Dict[str, Any]]:
        headers: Optional[List[str]] = None
        header_keys: Optional[List[Optional[str]]] = None
        out: List[Dict[str, Any]] = []

        for page_idx in range(1, parsed.page_count):
            for tb in parsed.tables_by_page[page_idx]:
                if not tb or len(tb) < 2:
                    continue
                first = tb[0]
                # detect header row by presence of 'Policy Year'
                if any((cell or "").strip().lower() == "policy year" or "policy year" in (cell or "").lower() for cell in first):
                    headers = [cell or "" for cell in first]
                    header_keys = [_header_key(h) for h in headers]
                    data_rows = tb[1:]
                else:
                    if headers is None:
                        continue
                    data_rows = tb

                for row in data_rows:
                    if not row:
                        continue
                    # normalize row length
                    if headers and len(row) < len(headers):
                        row = row + [None] * (len(headers) - len(row))
                    # must have policy_year integer
                    py = None
                    for idx, key in enumerate(header_keys or []):
                        if key == "policy_year":
                            py = _find_int_in_text(row[idx] if idx < len(row) else None)
                            break
                    # continuation tables sometimes shift; fallback: second column often policy year
                    if py is None and len(row) >= 2:
                        py = _find_int_in_text(row[1])
                    if py is None:
                        continue

                    rec: Dict[str, Any] = {"policy_year": py}
                    # age
                    age = _find_int_in_text(row[0]) if len(row) > 0 else None
                    if age is not None:
                        rec["age"] = age
                    # map numeric fields
                    for idx, key in enumerate(header_keys or []):
                        if key in {"annualized_premium", "income_payout_annual", "maturity", "death_benefit", "gsv", "ssv", "svp"}:
                            rec[key] = _to_number(row[idx] if idx < len(row) else None)
                    out.append(rec)

        # sort unique by policy_year, keeping first occurrence
        dedup = {}
        for r in out:
            py = r["policy_year"]
            if py not in dedup:
                dedup[py] = r
        return [dedup[k] for k in sorted(dedup.keys())]

    def calculate(self, extracted: ExtractedFields, ptd: date) -> ComputedOutputs:
        mode = extracted.mode
        rcd = derive_rcd(extracted.bi_generation_date, ptd, mode)
        grace_days = self.grace_period_days(mode)
        rpu_date = add_grace(ptd, grace_days)

        months_paid = count_paid_months(rcd, ptd, mode)
        months_payable_total = extracted.ppt_years * 12
        rpu_factor = 0.0 if months_payable_total <= 0 else max(0.0, min(1.0, months_paid / months_payable_total))

        # Fully paid summary
        fp_income_total_annual = sum((r.get("income_payout_annual") or 0.0) for r in extracted.schedule_rows)
        fp_maturity = 0.0
        for r in extracted.schedule_rows:
            if r.get("maturity"):
                fp_maturity = float(r["maturity"])
        fp = {
            "total_income_annual": fp_income_total_annual,
            "maturity": fp_maturity,
            "death_inception": extracted.sum_assured_on_death,
        }

        # Build instalment-level income schedule for SL formula
        income_instalments = self._generate_income_instalments(extracted, rcd)
        total_income_due = sum(x["amount"] for x in income_instalments)
        already_paid = [x for x in income_instalments if (x["due_date"] > rcd and x["due_date"] < rpu_date)]
        already_paid_total = sum(x["amount"] for x in already_paid)
        remaining_instalments = max(1, len(income_instalments) - len(already_paid))

        # SL formula for Reduced Paid-up Income Benefit Pay-out
        rpu_income_total = (total_income_due * rpu_factor) - (already_paid_total * (1 - rpu_factor) / remaining_instalments)

        # Scale maturity and death
        rpu_maturity = fp_maturity * rpu_factor
        death_scaled = (extracted.sum_assured_on_death or 0.0) * rpu_factor

        rpu = {
            "rpu_factor": round(rpu_factor, 6),
            "total_income": rpu_income_total,
            "total_income_due": total_income_due,
            "income_already_paid": already_paid_total,
            "remaining_income_instalments": remaining_instalments,
            "maturity": rpu_maturity,
            "death_scaled": death_scaled,
        }

        notes = [
            "Illustrative values assuming non-payment of the next premium and policy becoming paid-up after expiry of the grace period.",
            "Proposer name is used for personalization; names are not stored.",
            "Income instalment due exactly on RCD is treated as not paid (premium-first convention).",
        ]

        return ComputedOutputs(
            rcd=rcd,
            ptd=ptd,
            rpu_date=rpu_date,
            grace_period_days=grace_days,
            months_paid=months_paid,
            months_payable_total=months_payable_total,
            rpu_factor=rpu_factor,
            fully_paid=fp,
            reduced_paid_up=rpu,
            notes=notes,
        )

    def _generate_income_instalments(self, extracted: ExtractedFields, rcd: date) -> List[Dict[str, Any]]:
        """Generate income instalment schedule using BI annual income values and SL multiplier factors."""
        freq = (extracted.income_payout_frequency or "Yearly").title()
        if freq not in self.INCOME_MULTIPLIER:
            freq = "Yearly"
        mult = self.INCOME_MULTIPLIER[freq]
        step_months = self.FREQ_TO_MONTHS[freq]
        inst_per_year = self.FREQ_TO_INSTALLMENTS_PER_YEAR[freq]

        # Identify which policy years have income > 0 from BI schedule
        income_year_rows = [(r["policy_year"], float(r.get("income_payout_annual") or 0.0)) for r in extracted.schedule_rows]
        income_year_rows = [(py, amt) for py, amt in income_year_rows if amt and amt > 0]

        instalments: List[Dict[str, Any]] = []
        for policy_year, annual_income in income_year_rows:
            # annual income is converted to instalment amount using SL multiplier
            instalment_amount = annual_income * mult
            year_start = rcd + relativedelta(years=policy_year - 1)

            # per SL example: in the start policy year, payouts start at end of 1/3/6/12 months
            # We'll place instalments at month offsets within that policy year: step_months, 2*step_months, ... 12
            for k in range(1, inst_per_year + 1):
                due = year_start + relativedelta(months=step_months * k)
                instalments.append({"policy_year": policy_year, "due_date": due, "amount": instalment_amount})

        return instalments
