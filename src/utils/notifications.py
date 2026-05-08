"""
SubmissionVeritas — Notification Dispatcher + Baseline Promoter (Agent 4)

Runs on Modal.com after PDF generation.
For each document_diffs row where ai_status='pdf_ready':
  1. Query all clients tracking that record
  2. For each unnotified client: send personalized email
  3. Log to notification_logs (unique constraint prevents duplicates)
  4. After all clients notified: promote new doc version to baseline
     (archive old version, so next scrape compares against new version)

CRITICAL DESIGN RULES:
- Uses existing `clients` + `client_records` tables (already in DB)
  NOT the new client_tracked_records (which has no data yet)
- Anti-spam: UNIQUE(client_id, diff_id) in notification_logs
- Baseline promotion: sets old doc is_archived=True, is_latest=False
- Email is a PLACEHOLDER — production will use Resend/SendGrid
- Continues to next diff even if one client email fails
"""

import os
import logging
from datetime import datetime, timezone
from supabase import create_client, Client
import resend  # Real email provider integration

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Dispatcher] %(levelname)s - %(message)s"
)
logger = logging.getLogger("Dispatcher")


# ── Supabase ──────────────────────────────────────────────────────────────────
def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    return create_client(url, key)


# ── Email Sender (PLACEHOLDER) ────────────────────────────────────────────────
def send_email(
    to_email: str,
    to_name: str,
    record_title: str,
    doc_title: str,
    delta_type: str,
    ai_summary: str,
    pdf_url: str,
) -> bool:
    """
    Sends a real email using Resend.
    Requires RESEND_API_KEY in Modal Secrets.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logger.error("[Dispatcher] RESEND_API_KEY not found! Skipping email.")
        return False

    resend.api_key = api_key
    
    subject = f"⚠ Delta Alert: {record_title}"
    type_label = "New Document Added" if delta_type == "new_file" else "Existing Document Modified"
    
    # Pre-format summary for HTML (f-strings cannot contain backslashes inside curly braces)
    safe_summary = (ai_summary or "").replace("\n", "<br>")

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            .container {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #333; line-height: 1.6; }}
            .header {{ background-color: #1A237E; color: #ffffff; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }}
            .content {{ padding: 30px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 8px 8px; }}
            .badge {{ background-color: #E8EAF6; color: #1A237E; padding: 4px 12px; border-radius: 16px; font-size: 12px; font-weight: bold; text-transform: uppercase; }}
            .summary-box {{ background-color: #f8f9fa; border-left: 4px solid #1A237E; padding: 20px; margin: 25px 0; border-radius: 4px; }}
            .button {{ background-color: #1A237E; color: #ffffff !important; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block; margin-top: 10px; }}
            .footer {{ font-size: 12px; color: #777; text-align: center; margin-top: 25px; }}
            .meta-table {{ width: 100%; margin-top: 20px; border-collapse: collapse; }}
            .meta-table td {{ padding: 10px 0; border-bottom: 1px solid #f0f0f0; font-size: 14px; }}
            .label {{ color: #777; font-weight: bold; width: 130px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 style="margin:0; font-size: 24px;">SubmissionVeritas</h1>
                <p style="margin:10px 0 0 0; opacity: 0.8;">Verified Delta Alert</p>
            </div>
            <div class="content">
                <p style="font-size: 16px;">Dear <strong>{to_name}</strong>,</p>
                <p>A <strong>Material Delta</strong> has been detected for a record in your watchlist. Our AI has analyzed the changes to help you make an informed decision.</p>
                
                <table class="meta-table">
                    <tr><td class="label">TENDER</td><td>{record_title}</td></tr>
                    <tr><td class="label">DOCUMENT</td><td>{doc_title}</td></tr>
                    <tr><td class="label">CHANGE TYPE</td><td><span class="badge">{type_label}</span></td></tr>
                </table>

                <div class="summary-box">
                    <h4 style="margin: 0 0 10px 0; color: #1A237E; font-size: 16px;">AI ANALYSIS SUMMARY</h4>
                    <p style="margin: 0; color: #444;">{safe_summary}</p>
                </div>

                <div style="text-align: center; margin-top: 30px;">
                    <p style="margin-bottom: 20px; font-size: 14px; color: #666;">For exact line-by-line changes and technical verification, please review the full report:</p>
                    <a href="{pdf_url}" class="button">View Technical Evidence Report (PDF)</a>
                </div>
            </div>
            <div class="footer">
                <p>&copy; 2026 SubmissionVeritas. All rights reserved.</p>
                <p>This is an automated alert based on verified document changes. Please confirm all details on the official procurement portal before submissionding.</p>
            </div>
        </div>
    </body>
    </html>
    """

    try:
        resend.Emails.send({
            "from": "SubmissionVeritas <alerts@submissionveritas.com>", 
            "to": to_email,
            "subject": subject,
            "html": html_body
        })
        logger.info(f"[Dispatcher] 📧 Email SENT successfully to {to_email}")
        return True
    except Exception as e:
        logger.error(f"[Dispatcher] ❌ Resend API Error: {e}")
        return False


# ── Baseline Promoter ─────────────────────────────────────────────────────────
def promote_baseline(db: Client, diff_id: str, doc_id: str):
    """
    Archives the old document version.
    The current doc (doc_id) is already is_latest=True.
    We just archive its previous_version_id.
    """
    try:
        doc_res = (
            db.table("documents")
            .select("previous_version_id")
            .eq("id", doc_id)
            .single()
            .execute()
        )
        prev_id = doc_res.data.get("previous_version_id")

        if prev_id:
            db.table("documents").update({
                "is_latest": False,
                "is_archived": True,
            }).eq("id", prev_id).execute()
            logger.info(f"[Dispatcher] 🔄 Archived old version {prev_id}")

        db.table("document_diffs").update({
            "baseline_promoted": True,
        }).eq("id", diff_id).execute()

        logger.info(f"[Dispatcher] ✅ Baseline promoted for diff {diff_id}")

    except Exception as e:
        logger.error(f"[Dispatcher] ❌ Baseline promotion failed for {diff_id}: {e}")


# ── Main Dispatch Cycle ───────────────────────────────────────────────────────
def run_dispatch_cycle():
    """
    Main entry point. Called by Modal.
    Finds all pdf_ready + material + un-notified diffs and fans out to all clients.
    """
    db = get_supabase()

    # ── Find all pending material deltas ──────────────────────────────────
    diffs_res = (
        db.table("document_diffs")
        .select("id, record_id, document_id, ai_summary, pdf_report_path, delta_type, change_type")
        .eq("ai_status", "pdf_ready")
        .eq("is_material", True)
        .eq("notifications_sent", False)
        .execute()
    )

    if not diffs_res.data:
        logger.info("[Dispatcher] No pending notifications. All caught up.")
        return

    logger.info(f"[Dispatcher] Processing {len(diffs_res.data)} pending deltas...")

    for diff in diffs_res.data:
        diff_id = diff["id"]
        record_id = diff["record_id"]
        doc_id = diff["document_id"]
        ai_summary = diff.get("ai_summary") or "No summary available."
        pdf_url = diff.get("pdf_report_path") or ""
        delta_type = diff.get("delta_type") or diff.get("change_type") or "modified"

        # ── Fetch record info ─────────────────────────────────────────────────
        try:
            record_res = db.table("records").select("title").eq("id", record_id).single().execute()
            record_title = record_res.data.get("title", "Unknown Record")
        except Exception:
            record_title = "Unknown Record"

        # ── Fetch document title ──────────────────────────────────────────────
        try:
            doc_res = db.table("documents").select("title").eq("id", doc_id).single().execute()
            doc_title = doc_res.data.get("title", "Unknown Document")
        except Exception:
            doc_title = "Unknown Document"

        # ── Find clients who track this record ────────────────────────────────
        # Using existing client_records (junction) + clients table
        ct_res = (
            db.table("client_records")
            .select("client_id")
            .eq("record_id", record_id)
            .execute()
        )

        client_ids = [row["client_id"] for row in (ct_res.data or [])]

        if not client_ids:
            logger.warning(f"[Dispatcher] No clients tracking record {record_id}. Skipping fan-out.")
            db.table("document_diffs").update({"notifications_sent": True}).eq("id", diff_id).execute()
            promote_baseline(db, diff_id, doc_id)
            continue

        # ── Fetch client details ──────────────────────────────────────────────
        clients_res = (
            db.table("clients")
            .select("id, client_name, email")
            .in_("id", client_ids)
            .execute()
        )

        sent_count = 0
        skip_count = 0

        for client in (clients_res.data or []):
            client_id = client["id"]
            client_name = client.get("client_name") or "Valued Client"
            client_email = client.get("email") or ""

            if not client_email:
                logger.warning(f"[Dispatcher] Client {client_id} has no email. Skipping.")
                continue

            # ── Anti-spam: check notification_logs ────────────────────────────
            log_check = (
                db.table("notification_logs")
                .select("id")
                .eq("client_id", client_id)
                .eq("diff_id", diff_id)
                .execute()
            )
            if log_check.data:
                skip_count += 1
                logger.debug(f"[Dispatcher] ⏩ Client {client_name} already notified. Skipping.")
                continue

            # ── Send personalized email ───────────────────────────────────────
            success = send_email(
                to_email=client_email,
                to_name=client_name,
                record_title=record_title,
                doc_title=doc_title,
                delta_type=delta_type,
                ai_summary=ai_summary,
                pdf_url=pdf_url,
            )

            if success:
                # Log it — UNIQUE(client_id, diff_id) prevents double logging
                try:
                    db.table("notification_logs").insert({
                        "client_id": client_id,
                        "diff_id": diff_id,
                        "channel": "email",
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                    }).execute()
                    sent_count += 1
                except Exception as log_err:
                    logger.error(f"[Dispatcher] Failed to log notification: {log_err}")

        # ── Mark diff as fully notified ───────────────────────────────────────
        db.table("document_diffs").update({
            "notifications_sent": True,
        }).eq("id", diff_id).execute()

        logger.info(
            f"[Dispatcher] 🎊 Diff {diff_id} done | "
            f"sent={sent_count} | skipped={skip_count} | {record_title}"
        )

        # ── Promote new version to baseline ───────────────────────────────────
        promote_baseline(db, diff_id, doc_id)


if __name__ == "__main__":
    run_dispatch_cycle()
