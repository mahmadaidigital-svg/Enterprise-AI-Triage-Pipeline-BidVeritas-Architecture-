"""
SubmissionVeritas — Delta AI Triage Agent (Agent 2)

Runs on Modal.com as a scheduled worker every 15 minutes.
Picks up 'waiting' items from delta_queue, sends the diff to
Gemini 3.1 Pro (Vertex AI), validates the response, and updates
document_diffs with ai_summary, references, and is_material flag.

CRITICAL DESIGN RULES:
- Processes queue items one-at-a-time per container (max_containers=5)
- Row-level locking via status='processing' prevents double-processing
- Retries up to 3 times with exponential backoff on AI failure
- Non-material changes are marked and SKIPPED (no PDF, no notification)
- All DB reads use SERVICE_ROLE key to bypass RLS
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import vertexai
from vertexai.generative_models import GenerativeModel
from supabase import create_client, Client

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AI-Triage] %(levelname)s - %(message)s"
)
logger = logging.getLogger("AITriage")

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT_ID = "propace-trade-ai"
LOCATION = "global"
# Path inside the Modal container (service-account.json is added via add_local_file)
SERVICE_ACCOUNT_PATH = "/root/service-account.json"
# ── MODEL TIERS (EXACT MATCH) ─────────────────────────────────────────────
# STAGE 1: Gemini 3 Flash (Exact ID)
MODEL_TRIAGE = "gemini-3-flash-preview"
# STAGE 2: Gemini 2.5 Pro (Exact Stable ID)
MODEL_ANALYSIS = "gemini-2.5-pro"

MAX_RETRY_ATTEMPTS = 3
BATCH_SIZE = 50  # Process up to 50 items per run (queue handles overflow)


# ── Supabase Client ───────────────────────────────────────────────────────────
def get_supabase() -> Optional[Client]:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        logger.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
        return None
    return create_client(url, key)


# ── Vertex AI Init ────────────────────────────────────────────────────────────
def init_vertex_ai():
    if not os.path.exists(SERVICE_ACCOUNT_PATH):
        raise FileNotFoundError(
            "[AI-Triage] service-account.json not found at {}".format(SERVICE_ACCOUNT_PATH)
        )
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SERVICE_ACCOUNT_PATH
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    logger.info("[AI-Triage] Vertex AI initialized with service account.")


# ── AI Analysis ───────────────────────────────────────────────────────────────
def analyze_with_gemini(
    delta_type: str,
    diff_summary: str,
    old_text: str,
    new_text: str,
) -> Dict[str, Any]:
    """
    HYBRID TWO-STAGE ANALYSIS (Safe Version):
    1. Call Flash to check materiality (Fast & Cheap)
    2. Call Pro for Advanced Technical Summary ONLY if material.
    """
    
    # ── HELPER: SMART CONTEXT EXTRACTION ──
    def get_smart_context(full_text: str, diff_snippet: str, window=4000) -> str:
        if not full_text or not diff_snippet: return full_text[:window]
        first_line = diff_snippet.split("\n")[0].replace("+", "").replace("-", "").strip()
        if not first_line or len(first_line) < 10: return full_text[:window]
        idx = full_text.find(first_line)
        if idx == -1: return full_text[:window]
        start = max(0, idx - (window // 2))
        end = min(len(full_text), start + window)
        ctx = full_text[start:end]
        return "... [CONTEXT WINDOW] ...\n" + ctx + "\n... [END CONTEXT] ..."

    # ── STAGE 1: TRIAGE (Gemini 3 Flash) ──
    logger.info("[AI-Hybrid] STAGE 1 (Triage) via " + MODEL_TRIAGE)
    triage_model = GenerativeModel(MODEL_TRIAGE)
    
    # Small sample for triage
    triage_sample = new_text[:8000] if delta_type == "new_file" else get_smart_context(new_text, diff_summary, window=5000)
    
    triage_prompt = """Is this procurement update 'MATERIAL'?
REJECT (is_material: false) ONLY if it is trivial formatting, typos, or minor scuffing (e.g. 'this' vs 'that', punctuation changes).
ACCEPT (is_material: true) for EVERYTHING ELSE, especially Addendums, Scope, Prices, or New Files.
Respond ONLY JSON: {{"is_material": bool, "reason": "short string"}}""".format(delta_type, triage_sample)

    try:
        t_res = triage_model.generate_content(triage_prompt)
        import re
        raw_t = re.sub(r'```json|```', '', t_res.text).strip()
        t_parsed = json.loads(raw_t)
        
        if not t_parsed.get("is_material", False):
            logger.info("[AI-Hybrid] Marked NON-MATERIAL by Flash. Saving cost.")
            return {
                "is_material": False,
                "reason": t_parsed.get("reason", "Flash deemed non-material"),
                "summary": "", "reference_old": "", "reference_new": ""
            }
    except Exception as e:
        logger.warning("[AI-Hybrid] Stage 1 (Flash) failed: {}. Falling back to Pro.".format(e))

    # ── STAGE 2: DEEP ANALYSIS (Gemini 2.5 Pro) ──
    logger.info("[AI-Hybrid] STAGE 2 (Deep Analysis) via " + MODEL_ANALYSIS)
    analysis_model = GenerativeModel(MODEL_ANALYSIS)
    
    if delta_type == "new_file":
        new_sample = new_text[:100000] if new_text else "N/A"
        old_sample = "N/A"
        diff_sample = "N/A (New File Addition)"
    else:
        old_sample = get_smart_context(old_text, diff_summary) if old_text else "N/A"
        new_sample = get_smart_context(new_text, diff_summary) if new_text else "N/A"
        diff_sample = diff_summary[:5000]

    analysis_prompt = """You are a senior US enterprise procurement analyst.
Write an ADVANCED TECHNICAL SUMMARY for this MATERIAL delta.

AMENDMENT TYPE: {}
RAW DIFF: {}
NEW DOCUMENT CONTEXT: {}

Focus on: Scope, Requirements, and Pricing impact. 
Respond ONLY with JSON:
{{
  "is_material": true,
  "reason": "Technical justification",
  "summary": "3-5 paragraphs of advanced analysis.",
  "reference_old": "verbatim old text",
  "reference_new": "verbatim new text"
}}""".format(delta_type, diff_sample, new_sample)

    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            res = analysis_model.generate_content(analysis_prompt)
            import re
            raw = re.sub(r'```json|```', '', res.text).strip()
            # Newline handling (Safe version - no backslash in f-string)
            raw = re.sub(r'(?<=[:\s])"([\s\S]*?)"(?=[,\s}])', lambda m: m.group(0).replace('\n', '\\n'), raw)
            return json.loads(raw)
        except Exception as e:
            logger.warning("[AI-Hybrid] Stage 2 attempt {} failed: {}".format(attempt, e))
            time.sleep(2**attempt)
            
    return {"is_material": False, "reason": "Failed all attempts", "summary": "", "reference_old": "", "reference_new": ""}


# ── Main Queue Processor ───────────────────────────────────────────────────────
def process_queue():
    """
    Main entry point. Called by Modal.
    Fetches BATCH_SIZE 'waiting' items from delta_queue,
    processes each through Gemini, updates document_diffs.
    """
    db = get_supabase()
    if not db:
        return

    # Fetch waiting queue items + join to document_diffs in one query
    # We use two separate queries because Supabase Python SDK doesn't support
    # deep nested joins with .select("*, diff_id(*)")
    queue_res = (
        db.table("delta_queue")
        .select("id, diff_id, attempts")
        .eq("status", "waiting")
        .order("created_at")
        .limit(BATCH_SIZE)
        .execute()
    )

    if not queue_res.data:
        logger.info("[AI-Triage] No pending deltas in queue. Sleeping.")
        return

    logger.info("[AI-Triage] Found {} waiting items. Initializing Vertex AI...".format(len(queue_res.data)))
    init_vertex_ai()

    for item in queue_res.data:
        queue_id = item["id"]
        diff_id = item["diff_id"]
        attempts = item.get("attempts", 0)

        # ── STEP 1: Lock row to prevent double-processing ─────────────────────
        db.table("delta_queue").update({
            "status": "processing",
            "attempts": attempts + 1
        }).eq("id", queue_id).eq("status", "waiting").execute()

        logger.info("[AI-Triage] Processing queue item {} | diff_id={}".format(queue_id, diff_id))

        try:
            # ── STEP 2: Fetch the diff record ─────────────────────────────────
            diff_res = (
                db.table("document_diffs")
                .select("id, record_id, document_id, diff_summary, change_type")
                .eq("id", diff_id)
                .single()
                .execute()
            )
            diff_row = diff_res.data
            doc_id = diff_row["document_id"]
            raw_diff = diff_row["diff_summary"]
            # change_type from document_diffs: 'modified' or 'new_file'
            delta_type = diff_row.get("change_type", "modified")

            # ── STEP 3: Fetch new document text and old text ──────────────────
            doc_res = (
                db.table("documents")
                .select("current_parsed_text, previous_version_id, is_latest")
                .eq("id", doc_id)
                .single()
                .execute()
            )
            doc_row = doc_res.data
            new_text = doc_row.get("current_parsed_text") or ""
            prev_id = doc_row.get("previous_version_id")

            old_text = ""
            if prev_id:
                old_res = (
                    db.table("documents")
                    .select("current_parsed_text")
                    .eq("id", prev_id)
                    .single()
                    .execute()
                )
                old_text = old_res.data.get("current_parsed_text") or ""

            # ── STEP 4: Call Gemini ───────────────────────────────────────────
            analysis = analyze_with_gemini(delta_type, raw_diff, old_text, new_text)

            # ── STEP 5: Update document_diffs with results ────────────────────
            new_status = "completed" if analysis["is_material"] else "non_material"
            db.table("document_diffs").update({
                "ai_summary": analysis.get("summary"),
                "ai_reference_old": analysis.get("reference_old"),
                "ai_reference_new": analysis.get("reference_new"),
                "is_material": analysis.get("is_material", False),
                "delta_type": delta_type,
                "ai_status": new_status,
            }).eq("id", diff_id).execute()

            # ── STEP 6: Mark queue item done ──────────────────────────────────
            db.table("delta_queue").update({
                "status": "done",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", queue_id).execute()

            logger.info(
                "[AI-Triage] ✅ Done | diff={} | material={} | reason={}".format(
                    diff_id, analysis.get('is_material'), analysis.get('reason', '')[:80]
                )
            )

        except Exception as e:
            logger.error("[AI-Triage] ❌ Failed queue item {}: {}".format(queue_id, e))
            db.table("delta_queue").update({
                "status": "failed",
                "error_log": str(e)[:1000],
            }).eq("id", queue_id).execute()
            # Do NOT re-raise — continue processing next item


if __name__ == "__main__":
    process_queue()
