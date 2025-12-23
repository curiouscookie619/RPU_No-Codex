from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Dict, Optional

import streamlit as st

from core.db import init_db, get_conn
from core.event_logger import log_event
from core.pdf_reader import read_pdf
from products.registry import detect_product
from core.output_pdf import render_one_pager


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@st.cache_data(show_spinner=False, ttl=3600, max_entries=128)
def _cached_read_pdf(file_bytes: bytes):
    # cache by content (Streamlit cache uses argument hashing)
    return read_pdf(file_bytes)


def make_case_id(
    product_id: str,
    product_uin: Optional[str],
    bi_date: date,
    ptd: date,
    rcd: date,
    mode: str,
    pt: int,
    ppt: int,
    annualized_premium: Optional[float],
    proposer_name_transient: Optional[str],
) -> str:
    raw = "|".join(
        [
            product_id,
            product_uin or "",
            str(bi_date),
            str(ptd),
            str(rcd),
            mode,
            str(pt),
            str(ppt),
            str(annualized_premium or ""),
            (proposer_name_transient or "").strip().lower(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def save_case(
    case_id: str,
    session_id: str,
    product_id: str,
    product_confidence: float,
    bi_date: date,
    ptd: date,
    rcd: date,
    rpu_date: date,
    mode: str,
    file_hash: str,
    extracted_json: Dict[str, Any],
    outputs_json: Dict[str, Any],
) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases(case_id, session_id, product_id, product_confidence, bi_date, ptd, rcd, rpu_date,
                             mode, file_hash, extracted, outputs)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
            ON CONFLICT (case_id) DO UPDATE SET
                session_id=EXCLUDED.session_id,
                product_id=EXCLUDED.product_id,
                product_confidence=EXCLUDED.product_confidence,
                bi_date=EXCLUDED.bi_date,
                ptd=EXCLUDED.ptd,
                rcd=EXCLUDED.rcd,
                rpu_date=EXCLUDED.rpu_date,
                mode=EXCLUDED.mode,
                file_hash=EXCLUDED.file_hash,
                extracted=EXCLUDED.extracted,
                outputs=EXCLUDED.outputs,
                updated_at=NOW()
            """,
            (
                case_id,
                session_id,
                product_id,
                product_confidence,
                bi_date,
                ptd,
                rcd,
                rpu_date,
                mode,
                file_hash,
                json.dumps(extracted_json),
                json.dumps(outputs_json),
            ),
        )
        conn.commit()


def _fmt_money(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.0f}"
    except Exception:
        return str(v)


def _render_income_segments_bullets(segments: list[dict], title: str):
    st.markdown(f"**{title}**")
    if not segments:
        st.write("- (No income rows detected)")
        return

    for seg in segments:
        inc = seg.get("income")
        yrs = seg.get("years")
        s = seg.get("start_year")
        e = seg.get("end_year")
        st.write(f"- ₹{_fmt_money(inc)} per year for **{yrs}** years (Policy Year {s}–{e})")


def main():
    st.set_page_config(page_title="RPU Calculator", layout="centered")

    init_db()

    if "session_id" not in st.session_state:
        st.session_state["session_id"] = hashlib.sha256(str(st.session_state).encode("utf-8")).hexdigest()
        log_event("session_start", st.session_state["session_id"], {"version": "m1"})

    session_id = st.session_state["session_id"]

    st.title("Reduced Paid-Up Calculator (Internal)")
    st.caption("Upload a Benefit Illustration PDF and enter PTD (Next Premium Due Date). No PDFs are stored.")

    # Use a form to avoid rerun/button non-responsiveness
    with st.form("main_form"):
        debug = st.checkbox("Debug mode (show what was extracted)", value=True)
        uploaded = st.file_uploader("Upload BI PDF", type=["pdf"])
        ptd = st.date_input("PTD (Next Premium Due Date)", value=None, format="DD/MM/YYYY")
        submitted = st.form_submit_button("Generate")

    if not submitted:
        return

    if uploaded is None or ptd is None:
        st.error("Please upload a PDF and enter PTD.")
        return

    file_bytes = uploaded.getvalue()
    file_hash = _sha256_bytes(file_bytes)

    log_event("pdf_uploaded", session_id, {"file_hash": file_hash, "size_bytes": len(file_bytes)})

    try:
        # Cached parsing for speed (especially repeated attempts)
        parsed = _cached_read_pdf(file_bytes)
        log_event("pdf_parsed", session_id, {"pages": parsed.page_count})

        handler, conf, dbg = detect_product(parsed)
        log_event("product_detected", session_id, {"product_id": handler.product_id, "confidence": conf, "dbg": dbg})

        extracted = handler.extract(parsed)
        outputs = handler.calculate(extracted, ptd)

        extracted_dump = extracted.model_dump()
        outputs_dump = outputs.model_dump()

        # Build case_id (we store only hashes, not proposer name)
        case_id = make_case_id(
            product_id=handler.product_id,
            product_uin=extracted.product_uin,
            bi_date=extracted.bi_generation_date,
            ptd=ptd,
            rcd=outputs.rcd,
            mode=extracted.mode,
            pt=extracted.policy_term_years,
            ppt=extracted.ppt_years,
            annualized_premium=extracted.annualized_premium_excl_tax,
            proposer_name_transient=extracted.proposer_name_transient,
        )

        save_case(
            case_id=case_id,
            session_id=session_id,
            product_id=handler.product_id,
            product_confidence=conf,
            bi_date=extracted.bi_generation_date,
            ptd=ptd,
            rcd=outputs.rcd,
            rpu_date=outputs.rpu_date,
            mode=extracted.mode,
            file_hash=file_hash,
            extracted_json=extracted_dump,
            outputs_json=outputs_dump,
        )

        log_event("output_generated", session_id, {"case_id": case_id, "product_id": handler.product_id})

        # ---------- DEBUG ----------
        if debug:
            st.subheader("DEBUG: Extracted object (raw)")
            st.json(extracted_dump)

            st.subheader("DEBUG: Schedule preview")
            schedule_rows = extracted_dump.get("schedule_rows") or []
            st.write(f"Schedule rows found = {len(schedule_rows)}")
            if schedule_rows:
                st.dataframe(schedule_rows[:20], use_container_width=True)

        # ---------- OUTPUT SUMMARY ----------
        st.divider()
        st.subheader("Key Dates Summary")
        st.json(
            {
                "BI (Quote) Date": str(extracted.bi_generation_date),
                "RCD (Derived)": str(outputs.rcd),
                "PTD (Input)": str(ptd),
                "Assumed RPU Date (PTD + Grace)": str(outputs.rpu_date),
                "Grace Period Days": outputs.grace_period_days,
            }
        )

        st.divider()
        st.subheader("Fully Paid vs Reduced Paid-Up (Summary)")

        fully = outputs.fully_paid or {}
        rpu = outputs.reduced_paid_up or {}

        # Premium
        st.markdown("**Premium (from BI Premium Summary)**")
        st.write(f"- Instalment Premium without GST: ₹{_fmt_money(fully.get('instalment_premium_without_gst'))}")

        st.divider()

        # Fully Paid (stacked - responsive)
        st.markdown("### Fully Paid (as per BI)")
        _render_income_segments_bullets(fully.get("income_segments") or [], "Income pay-outs")
        st.write(f"- Total Income (sum): ₹{_fmt_money(fully.get('total_income'))}")
        st.write(f"- Maturity / Lump Sum: ₹{_fmt_money(fully.get('maturity'))}")
        st.write(f"- Death Benefit (schedule last year): ₹{_fmt_money(fully.get('death_last_year') or fully.get('death_inception'))}")

        st.divider()

        st.markdown("### Reduced Paid-Up (Assuming non-payment after PTD + grace)")
        st.write(f"- RPU factor: **{rpu.get('rpu_factor')}**")
        segs_scaled = rpu.get("income_segments_scaled") or []
        st.markdown("**Income pay-outs (scaled)**")
        if not segs_scaled:
            st.write("- (No income rows detected)")
        else:
            for seg in segs_scaled:
                inc = seg.get("income")
                inc_scaled = seg.get("income_scaled")
                yrs = seg.get("years")
                s = seg.get("start_year")
                e = seg.get("end_year")
                st.write(f"- ₹{_fmt_money(inc_scaled)} per year for **{yrs}** years (Policy Year {s}–{e}) [orig: ₹{_fmt_money(inc)}]")

        st.write(f"- Total Income (scaled): ₹{_fmt_money(rpu.get('total_income'))}")
        st.write(f"- Maturity (scaled): ₹{_fmt_money(rpu.get('maturity'))}")
        st.write(f"- Death Benefit (scaled): ₹{_fmt_money(rpu.get('death_scaled'))}")

        # Notes
        if outputs.notes:
            st.divider()
            st.subheader("Notes")
            for n in outputs.notes:
                st.write(f"- {n}")

        # ---------- PDF download ----------
        st.divider()
        st.subheader("Download one-pager (neutral)")
        pdf_bytes = render_one_pager(
            customer_name=(extracted.proposer_name_transient or "Customer"),
            product_name=extracted.product_name,
            summary={
                "Mode": extracted.mode,
                "PT": extracted.policy_term_years,
                "PPT": extracted.ppt_years,
                "BI Date": str(extracted.bi_generation_date),
                "RCD": str(outputs.rcd),
                "PTD": str(ptd),
                "Assumed RPU Date (PTD + Grace)": str(outputs.rpu_date),
            },
            fully_paid=fully,
            rpu=rpu,
            notes=outputs.notes or [],
        )
        st.download_button(
            "Download PDF",
            data=pdf_bytes,
            file_name=f"rpu_summary_{handler.product_id}.pdf",
            mime="application/pdf",
        )

    except Exception as e:
        st.error(f"Failed: {e}")
        log_event("error", session_id, {"error": str(e)})


if __name__ == "__main__":
    main()
