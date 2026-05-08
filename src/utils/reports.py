"""
SubmissionVeritas — PDF Report Builder (Agent 3)

Runs on Modal.com after AI Triage completes.
Picks up document_diffs where ai_status='completed' (is_material=True),
builds a professional PDF report, uploads to Supabase Storage,
and updates document_diffs.pdf_report_path + ai_status='pdf_ready'.

PDF Structure:
  Page 1: Cover Page (Record name, doc name, date, delta type)
  Page 2: AI Executive Summary
  Page 3: Technical Reference (Old text RED, New text GREEN)
  Page 4+: Full new document text for self-verification

CRITICAL DESIGN RULES:
- Uses datetime.now() not os.popen('date') — runs on Linux container
- Handles missing text gracefully (no crashes on empty strings)
- Cleans up temp files even on failure
- Uploads with upsert to prevent duplicate storage errors
"""

import os
import html
import logging
from datetime import datetime, timezone
from pathlib import Path
from supabase import create_client, Client

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black, white
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    HRFlowable, Table, TableStyle
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [PDF-Builder] %(levelname)s - %(message)s"
)
logger = logging.getLogger("PDFBuilder")

STORAGE_BUCKET = "record-documents"


# ── Supabase ──────────────────────────────────────────────────────────────────
def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    return create_client(url, key)


# ── PDF Builder ───────────────────────────────────────────────────────────────
class DeltaReportBuilder:
    BRAND_DARK = HexColor("#1A237E")   # Navy Blue
    BRAND_MID  = HexColor("#1565C0")   # Medium Blue
    RED_DARK   = HexColor("#B71C1C")
    RED_LIGHT  = HexColor("#FFEBEE")
    GREEN_DARK = HexColor("#1B5E20")
    GREEN_LIGHT= HexColor("#E8F5E9")
    GREY       = HexColor("#546E7A")

    def __init__(self, output_path: str, record_title: str, doc_title: str, delta_type: str):
        self.output_path = output_path
        self.record_title = record_title
        self.doc_title = doc_title
        self.delta_type = delta_type
        self.doc = SimpleDocTemplate(
            output_path,
            pagesize=LETTER,
            rightMargin=0.75 * inch,
            leftMargin=0.75 * inch,
            topMargin=0.75 * inch,
            bottomMargin=0.75 * inch,
        )
        self.elements = []
        self._build_styles()

    def _build_styles(self):
        base = getSampleStyleSheet()
        self.s = {}

        self.s["cover_title"] = ParagraphStyle(
            "CoverTitle", parent=base["Heading1"],
            fontSize=26, textColor=self.BRAND_DARK,
            alignment=1, spaceAfter=12, spaceBefore=0,
        )
        self.s["cover_sub"] = ParagraphStyle(
            "CoverSub", parent=base["Normal"],
            fontSize=13, textColor=self.GREY,
            alignment=1, spaceAfter=8,
        )
        self.s["section_heading"] = ParagraphStyle(
            "SectionHeading", parent=base["Heading2"],
            fontSize=14, textColor=white,
            spaceBefore=0, spaceAfter=0,
            leftIndent=6,
        )
        self.s["body"] = ParagraphStyle(
            "Body", parent=base["Normal"],
            fontSize=11, leading=16,
            spaceAfter=8, textColor=black,
        )
        self.s["ref_old"] = ParagraphStyle(
            "RefOld", parent=base["Normal"],
            fontSize=10, leading=14, leftIndent=12,
            textColor=self.RED_DARK, backColor=self.RED_LIGHT,
            spaceAfter=6,
        )
        self.s["ref_new"] = ParagraphStyle(
            "RefNew", parent=base["Normal"],
            fontSize=10, leading=14, leftIndent=12,
            textColor=self.GREEN_DARK, backColor=self.GREEN_LIGHT,
            spaceAfter=6,
        )
        self.s["disclaimer"] = ParagraphStyle(
            "Disclaimer", parent=base["Normal"],
            fontSize=9, leading=12, textColor=self.GREY,
            spaceAfter=4,
        )
        self.s["mono"] = ParagraphStyle(
            "Mono", parent=base["Normal"],
            fontSize=9, leading=13, leftIndent=12,
            fontName="Courier", textColor=HexColor("#263238"),
            spaceAfter=4,
        )

    def _section_banner(self, title: str):
        """A blue banner row acting as a section header."""
        tbl = Table([[Paragraph(title, self.s["section_heading"])]], colWidths=[7 * inch])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.BRAND_MID),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        self.elements.append(tbl)
        self.elements.append(Spacer(1, 8))

    def _add_cover_page(self):
        self.elements.append(Spacer(1, 1.5 * inch))
        self.elements.append(Paragraph("⚠ TENDER AMENDMENT ALERT", self.s["cover_title"]))
        self.elements.append(HRFlowable(width="100%", thickness=2, color=self.BRAND_DARK))
        self.elements.append(Spacer(1, 0.3 * inch))

        type_label = "NEW FILE ADDED" if self.delta_type == "new_file" else "EXISTING FILE MODIFIED"
        meta = [
            ["Delta Type:", type_label],
            ["Record:", self.record_title or "N/A"],
            ["Affected Document:", self.doc_title or "N/A"],
            ["Report Generated:", datetime.now(timezone.utc).strftime("%B %d, %Y — %H:%M UTC")],
            ["Source:", "SubmissionVeritas — Automated Delta Detection"],
        ]
        tbl = Table(meta, colWidths=[2 * inch, 5 * inch])
        tbl.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 11),
            ("TEXTCOLOR", (0, 0), (0, -1), self.BRAND_DARK),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        self.elements.append(Spacer(1, 0.2 * inch))
        self.elements.append(tbl)
        self.elements.append(PageBreak())

    def _add_reference_page(self, ref_old: str, ref_new: str):
        self._section_banner("TECHNICAL VERIFICATION — EXACT CHANGES")
        self.elements.append(Spacer(1, 6))

        if ref_old:
            self.elements.append(Paragraph("<b>❌ REMOVED / OLD TEXT:</b>", self.s["body"]))
            # Split long text across paragraphs safely and ESCAPE HTML
            for chunk in (ref_old[:10000]).split("\n"):
                if chunk.strip():
                    safe_chunk = html.escape(chunk.strip())
                    self.elements.append(Paragraph(safe_chunk, self.s["ref_old"]))
            self.elements.append(Spacer(1, 0.2 * inch))

        if ref_new:
            self.elements.append(Paragraph("<b>✅ ADDED / NEW TEXT:</b>", self.s["body"]))
            for chunk in (ref_new[:10000]).split("\n"):
                if chunk.strip():
                    safe_chunk = html.escape(chunk.strip())
                    self.elements.append(Paragraph(safe_chunk, self.s["ref_new"]))

        if not ref_old and not ref_new:
            self.elements.append(
                Paragraph("No specific verbatim references extracted.", self.s["body"])
            )
        self.elements.append(PageBreak())

    def _add_full_document_page(self, full_text: str):
        self._section_banner("FULL UPDATED DOCUMENT — FOR SELF-VERIFICATION")
        self.elements.append(Spacer(1, 6))
        self.elements.append(
            Paragraph(
                "The complete text of the new document version is included below "
                "so you can independently verify the AI analysis above.",
                self.s["disclaimer"],
            )
        )
        self.elements.append(Spacer(1, 8))
        if full_text:
            # Increased limit to 100,000 chars for deep audit / complete document
            for chunk in (full_text[:100000]).split("\n"):
                if chunk.strip():
                    safe_chunk = html.escape(chunk.strip())
                    self.elements.append(Paragraph(safe_chunk, self.s["mono"]))
        else:
            self.elements.append(Paragraph("Full document text not available.", self.s["body"]))

    def build(self, ref_old: str, ref_new: str, full_new_text: str):
        self._add_cover_page()
        self._add_reference_page(ref_old, ref_new)
        self._add_full_document_page(full_new_text)
        self.doc.build(self.elements)
        logger.info(f"[PDF-Builder] PDF written to {self.output_path}")


# ── Main Processor ────────────────────────────────────────────────────────────
def process_pending_pdfs():
    """
    Polls document_diffs where ai_status='completed' (meaning Gemini said is_material=True).
    Builds a PDF and uploads to Supabase Storage.
    """
    db = get_supabase()

    res = (
        db.table("document_diffs")
        .select("id, record_id, document_id, ai_reference_old, ai_reference_new, delta_type, change_type")
        .eq("ai_status", "completed")
        .execute()
    )

    if not res.data:
        logger.info("[PDF-Builder] No completed diffs waiting for PDF generation.")
        return

    logger.info(f"[PDF-Builder] Building {len(res.data)} PDF reports...")

    for diff in res.data:
        diff_id = diff["id"]
        record_id = diff["record_id"]
        doc_id = diff["document_id"]
        delta_type = diff.get("delta_type") or diff.get("change_type") or "modified"

        local_file = f"/tmp/delta_{diff_id}.pdf"

        try:
            # ── Fetch record title ────────────────────────────────────────────
            record_res = db.table("records").select("title").eq("id", record_id).single().execute()
            record_title = record_res.data.get("title", "Unknown Record")

            # ── Fetch document title + full text ──────────────────────────────
            doc_res = (
                db.table("documents")
                .select("title, current_parsed_text")
                .eq("id", doc_id)
                .single()
                .execute()
            )
            doc_title = doc_res.data.get("title", "Unknown Document")
            full_new_text = doc_res.data.get("current_parsed_text") or ""

            # ── Build PDF ─────────────────────────────────────────────────────
            builder = DeltaReportBuilder(
                output_path=local_file,
                record_title=record_title,
                doc_title=doc_title,
                delta_type=delta_type,
            )
            builder.build(
                ref_old=diff.get("ai_reference_old") or "",
                ref_new=diff.get("ai_reference_new") or "",
                full_new_text=full_new_text,
            )

            # ── Upload to Supabase Storage ────────────────────────────────────
            storage_path = f"delta_reports/{record_id}/{diff_id}.pdf"
            with open(local_file, "rb") as f:
                db.storage.from_(STORAGE_BUCKET).upload(
                    path=storage_path,
                    file=f,
                    file_options={"content-type": "application/pdf", "upsert": "true"},
                )

            supabase_url = os.environ.get("SUPABASE_URL", "")
            public_url = f"{supabase_url}/storage/v1/object/public/{STORAGE_BUCKET}/{storage_path}"

            # ── Update document_diffs ─────────────────────────────────────────
            db.table("document_diffs").update({
                "pdf_report_path": public_url,
                "ai_status": "pdf_ready",
            }).eq("id", diff_id).execute()

            logger.info(f"[PDF-Builder] ✅ PDF ready for diff {diff_id} | {public_url}")

        except Exception as e:
            logger.error(f"[PDF-Builder] ❌ Failed for diff {diff_id}: {e}")
            # Don't update ai_status — it stays 'completed' so it retries next run

        finally:
            if os.path.exists(local_file):
                os.remove(local_file)


if __name__ == "__main__":
    process_pending_pdfs()
