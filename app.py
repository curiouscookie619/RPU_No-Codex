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


def _make_case_id(
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
    # Proposer name can be included as entropy but is not persisted; only the hash is stored.
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
              product_confidence=EXCLUDED.product_confidence,
              extracted=EXCLUDED.extracted,
              outputs=EXCLUDED.outputs
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
                json.dumps(extracted_json, default=str),
                json.dumps(outputs_json, default=str),
            ),
        )
        conn.commit()


def save_feedback(session_id: str, case_id: str, rating: Optional[int], comment: Optional[str]) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO feedback(session_id, case_id, rating, comment)
            VALUES (%s,%s,%s,%s)
            """,
            (session_id, case_id, rating, comment),
        )
        conn.commit()


def main():
    st.set_page_config(page_title="RPU Calculator (Internal)", layout="centered")
    init_db()

    if "session_id" not in st.session_state:
        st.session_state["session_id"] = hashlib.sha256(st.session_state.__repr__().encode("utf-8")).hexdigest()[:16]
        log_event("session_start", st.session_state["session_id"], {"app": "rpu_streamlit", "version": "m1"})

    session_id = st.session_state["session_id"]

    st.title("Reduced Paid-Up Calculator (Internal)")
    st.caption("Upload a Benefit Illustration PDF and enter PTD (Next Premium Due Date). No PDFs are stored.")

    device = st.selectbox("Device", ["Desktop", "Mobile", "Tablet"], index=0)
    st.session_state["device"] = device

    uploaded = st.file_uploader("Upload BI PDF", type=["pdf"])
    ptd = st.date_input("PTD (Next Premium Due Date)", value=None)

    if uploaded is not None:
        file_bytes = uploaded.getvalue()
        file_hash = _sha256_bytes(file_bytes)
        log_event(
            "pdf_uploaded",
            session_id,
            {"file_size_kb": round(len(file_bytes) / 1024, 2), "file_hash": file_hash, "device": device},
        )

    if st.button("Generate", type="primary", disabled=(uploaded is None or ptd is None)):
        try:
            file_bytes = uploaded.getvalue()
            parsed = read_pdf(file_bytes)

            log_event("pdf_parsed", session_id, {"pages": parsed.page_count}, case_id=None)

            handler, conf, dbg = detect_product(parsed)
            log_event("product_detected", session_id, {"product_id": handler.product_id, "confidence": conf, "dbg": dbg})

            extracted = handler.extract(parsed)
            # ---- DEBUG MODE (temporary) ----
            debug = st.checkbox("Debug mode (show what was extracted)", value=True)

            if debug:
            st.subheader("DEBUG: Extracted fields")
            st.json(extracted)

            schedule = extracted.get("schedule", []) or extracted.get("benefit_schedule", []) or []
            st.subheader(f"DEBUG: Schedule rows found = {len(schedule)}")

            if schedule:
        st.dataframe(schedule[:20])  # show first 20 rows
    else:
        st.warning("No schedule table rows were extracted from the PDF.")
# ---- END DEBUG MODE ----
log_event(
                "fields_extracted",
                session_id,
                {
                    "product_name": extracted.product_name,
                    "uin": extracted.product_uin,
                    "mode": extracted.mode,
                    "pt": extracted.policy_term_years,
                    "ppt": extracted.ppt_years,
                    "schedule_rows": len(extracted.schedule_rows),
                },
            )

            outputs = handler.calculate(extracted, ptd)
            log_event(
                "calculation_success",
                session_id,
                {"rcd": outputs.rcd, "rpu_date": outputs.rpu_date, "rpu_factor": outputs.rpu_factor},
            )

            # case id (hash only)
            case_id = _make_case_id(
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

            # Prepare JSON for DB (strip transient name)
            extracted_json = extracted.model_dump()
            extracted_json["proposer_name_transient"] = None

            outputs_json = outputs.model_dump()

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
                extracted_json=extracted_json,
                outputs_json=outputs_json,
            )

            st.success("Generated successfully.")
            st.subheader("Detected Product")
            st.write(f"{extracted.product_name} ({handler.product_id})")

            st.subheader("Key Dates")
            st.write(
                {
                    "BI (Quote) Date": extracted.bi_generation_date,
                    "RCD (Derived)": outputs.rcd,
                    "PTD (Input)": outputs.ptd,
                    "Assumed RPU Date (PTD + Grace)": outputs.rpu_date,
                    "Grace Period Days": outputs.grace_period_days,
                }
            )

            st.subheader("Fully Paid vs Reduced Paid-Up (Summary)")
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**Fully Paid**")
                st.write(outputs.fully_paid)
            with col2:
                st.markdown("**Reduced Paid-Up**")
                st.write(outputs.reduced_paid_up)

            st.subheader("Download Output PDF")
            customer_name = extracted.proposer_name_transient or "Customer"
            summary = {
                "Mode": extracted.mode,
                "PT": extracted.policy_term_years,
                "PPT": extracted.ppt_years,
                "BI Date": extracted.bi_generation_date,
                "RCD": outputs.rcd,
                "PTD": outputs.ptd,
                "Assumed RPU Date (PTD + Grace)": outputs.rpu_date,
            }
            pdf_bytes = render_one_pager(
                customer_name=customer_name,
                product_name=extracted.product_name,
                summary=summary,
                fully_paid={
                    "total_income": outputs.fully_paid.get("total_income_annual"),
                    "maturity": outputs.fully_paid.get("maturity"),
                    "death_inception": outputs.fully_paid.get("death_inception"),
                },
                rpu={
                    "rpu_factor": outputs.reduced_paid_up.get("rpu_factor"),
                    "total_income": outputs.reduced_paid_up.get("total_income"),
                    "maturity": outputs.reduced_paid_up.get("maturity"),
                    "death_scaled": outputs.reduced_paid_up.get("death_scaled"),
                },
                notes=outputs.notes,
            )
            log_event("output_pdf_generated", session_id, {"bytes": len(pdf_bytes)}, case_id=case_id)

            st.download_button(
                "Download PDF",
                data=pdf_bytes,
                file_name=f"RPU_Output_{case_id[:8]}.pdf",
                mime="application/pdf",
            )

            st.subheader("Feedback (Optional)")
            rating = st.select_slider("Rating", options=[1, 2, 3, 4, 5], value=4)
            comment = st.text_area("Comment", placeholder="Optional feedback…")
            if st.button("Submit Feedback"):
                save_feedback(session_id=session_id, case_id=case_id, rating=rating, comment=comment)
                log_event("feedback_submitted", session_id, {"rating": rating, "has_comment": bool(comment)}, case_id=case_id)
                st.success("Feedback submitted.")

        except Exception as e:
            log_event("calculation_failed", session_id, {"error": str(e)}, case_id=None)
            st.error(f"Failed: {e}")


if __name__ == "__main__":
    main()
