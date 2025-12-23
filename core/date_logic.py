from __future__ import annotations

from datetime import date, timedelta
from dateutil.relativedelta import relativedelta


MODE_TO_MONTHS = {
    "ANNUAL": 12,
    "YEARLY": 12,
    "HALF-YEARLY": 6,
    "HALF YEARLY": 6,
    "SEMI-ANNUAL": 6,
    "SEMI ANNUAL": 6,
    "QUARTERLY": 3,
    "MONTHLY": 1,
}


def normalize_mode(mode_raw: str) -> str:
    m = (mode_raw or "").strip().upper()
    # common normalization
    if "ANNU" in m or "YEAR" in m:
        return "Annual"
    if "HALF" in m or "SEMI" in m:
        return "Half-yearly"
    if "QUART" in m:
        return "Quarterly"
    if "MONTH" in m:
        return "Monthly"
    return mode_raw.strip().title() if mode_raw else "Annual"


def mode_months(mode_norm: str) -> int:
    key = (mode_norm or "").strip().upper()
    return MODE_TO_MONTHS.get(key, 12)


def derive_rcd(bi_date: date, ptd: date, mode_norm: str) -> date:
    """Derive RCD as the smallest candidate date >= BI date where candidates are PTD - k*mode_months."""
    months = mode_months(mode_norm)
    candidate = ptd
    # step backward while still >= bi_date; stop when stepping back would go < bi_date
    while (candidate - relativedelta(months=months)) >= bi_date:
        candidate = candidate - relativedelta(months=months)
    # ensure constraint candidate >= bi_date
    if candidate < bi_date:
        candidate = candidate + relativedelta(months=months)
    return candidate


def count_paid_months(rcd: date, ptd: date, mode_norm: str) -> int:
    """Months between RCD and PTD, aligned to mode. Returns months paid (multiple of mode_months)."""
    months_step = mode_months(mode_norm)
    # count intervals by stepping from rcd to ptd
    cur = rcd
    intervals = 0
    while cur < ptd:
        cur = cur + relativedelta(months=months_step)
        intervals += 1
        if intervals > 500:  # safety
            break
    # If we overshot due to day adjustments, clamp
    if cur != ptd:
        # tolerate small day drift by comparing month/year
        if (cur.year, cur.month, cur.day) != (ptd.year, ptd.month, ptd.day):
            # if not aligned, approximate using month diff
            diff = (ptd.year - rcd.year) * 12 + (ptd.month - rcd.month)
            intervals = max(0, round(diff / months_step))
    return intervals * months_step


def add_grace(ptd: date, grace_days: int) -> date:
    return ptd + timedelta(days=grace_days)
