"""
SubmissionVeritas — Delta Detection Engine (Shared Across All Agents)

Logic:
  - First run  → No existing docs → Save as baseline (is_delta=False, v1)
  - Next runs  → Hash matches old version  → Skip (no change)
  - Next runs  → Hash is NEW for same title → Save as Delta (v2, v3...)
  - Next runs  → Completely new title seen  → Save as baseline for that file
                 BUT if record already has ANY docs → it IS an delta document

This module is imported by agent1_portal_alpha.py, agent_source_beta.py, agent1_4_portal_gamma.py
"""

import logging
import hashlib
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("DeltaEngine")


def get_supabase_client():
    """Build Supabase client using SERVICE_ROLE key (required for reliable writes)."""
    import supabase as sb_lib
    url = os.getenv("SUPABASE_URL", "").strip()
    # Always prefer SERVICE_ROLE for agents — bypasses RLS, reliable writes
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.getenv("SUPABASE_ANON_KEY", "").strip()
    )
    if not url or not key:
        logger.error("[DeltaEngine] SUPABASE_URL or key is missing!")
        return None
    return sb_lib.create_client(url, key)


def save_document_with_delta_detection(
    supabase_client,
    record_id: str,
    title: str,
    content_text: str,
    content_hash: str,
    file_path: str = "",
    download_url: str = "",
    record_already_has_docs: Optional[bool] = None,
    local_file_path: str = None,
) -> dict:
    """
    The single, unified, bulletproof document save function.

    Rules:
        1. Hash already in DB for this record+title?  → True duplicate, SKIP.
        2. Title exists in DB but with different hash? → AMENDMENT (new version).
        3. Title never seen before, but record has other docs? → NEW FILE AMENDMENT.
        4. Title never seen, record has NO docs at all? → BASELINE (first ever run).

    Returns:
        {
            "status": "no_change" | "new_baseline" | "delta_version" | "delta_new_file",
            "doc_id": str,        # ID of the newly inserted document (or existing on skip)
            "is_delta": bool,
            "version": int,
        }
    """
    if not supabase_client:
        logger.error("[DeltaEngine] No Supabase client. Skipping save.")
        return {"status": "error", "doc_id": None, "is_delta": False, "version": 0}

    clean_text = (content_text or "").replace("\u0000", "").strip()
    title = (title or "Unknown Document").strip()

    # ── Guard: content_hash must exist ──────────────────────────────────────────
    if not content_hash:
        content_hash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()

    try:
        # ── STEP 1: True duplicate check (same record + same title + same hash) ──
        exact = (
            supabase_client.table("documents")
            .select("id, version_number")
            .eq("record_id", record_id)
            .eq("content_hash", content_hash)
            .execute()
        )
        if exact.data:
            doc_id = exact.data[0]["id"]
            logger.info(
                "[DeltaEngine] NO CHANGE — '%s' hash identical. Skipping.", title
            )
            return {
                "status": "no_change",
                "doc_id": doc_id,
                "is_delta": False,
                "version": exact.data[0]["version_number"],
            }

        # ── STEP 2: Previous version check (same title, different hash) ──────────
        # We look for the HIGHEST version number for this title, even if is_latest is somehow false.
        prev = (
            supabase_client.table("documents")
            .select("id, version_number, content_hash")
            .eq("record_id", record_id)
            .eq("title", title)
            .order("version_number", desc=True)
            .limit(1)
            .execute()
        )

        is_delta = False
        prev_doc_id = None
        new_version = 1
        change_type = "new_baseline"

        if prev.data:
            # Title existed before but content changed → AMENDMENT VERSION
            is_delta = True
            prev_doc_id = prev.data[0]["id"]
            new_version = prev.data[0]["version_number"] + 1
            change_type = "modified"
            logger.info(
                "[DeltaEngine] AMENDMENT VERSION — '%s' changed! v%s → v%s",
                title, prev.data[0]["version_number"], new_version,
            )
            # Mark old version as no longer latest
            supabase_client.table("documents").update({"is_latest": False}).eq(
                "id", prev_doc_id
            ).execute()

        else:
            # Title never seen for this record
            # Check if record has ANY documents at all
            if record_already_has_docs is None:
                existing_any = (
                    supabase_client.table("documents")
                    .select("id")
                    .eq("record_id", record_id)
                    .limit(1)
                    .execute()
                )
                record_already_has_docs = bool(existing_any.data)

            if record_already_has_docs:
                # Record has other docs but this title is brand new → NEW FILE AMENDMENT
                is_delta = True
                change_type = "new_file"
                logger.info(
                    "[DeltaEngine] AMENDMENT NEW FILE — '%s' is a new document added to existing record.",
                    title,
                )
            else:
                # Record is completely empty → First ever run → BASELINE
                is_delta = False
                change_type = "new_baseline"
                logger.info(
                    "[DeltaEngine] BASELINE — '%s' saved for first time (record is blank).",
                    title,
                )

        # ── Optional: Upload actual file to Supabase Storage ──
        if local_file_path and os.path.exists(local_file_path):
            try:
                storage_path = f"{record_id}/v{new_version}_{os.path.basename(local_file_path)}"
                with open(local_file_path, "rb") as f:
                    supabase_client.storage.from_("record-documents").upload(
                        path=storage_path,
                        file=f,
                        file_options={"content-type": "application/octet-stream"}
                    )
                # Replace download_url with the Supabase Storage public URL
                public_url = supabase_client.storage.from_("record-documents").get_public_url(storage_path)
                download_url = public_url
                logger.info("[DeltaEngine] ✅ Uploaded file to Supabase Storage: %s", storage_path)
            except Exception as up_err:
                # If error is 'Duplicate', ignore or handle
                if "Duplicate" in str(up_err) or "already exists" in str(up_err):
                    public_url = supabase_client.storage.from_("record-documents").get_public_url(storage_path)
                    download_url = public_url
                    logger.info("[DeltaEngine] ℹ️ File already existed in storage: %s", storage_path)
                else:
                    logger.error("[DeltaEngine] ❌ Failed to upload to Supabase Storage: %s", up_err)

        # ── STEP 3: Insert the new document version ──────────────────────────────
        new_doc = {
            "record_id": record_id,
            "title": title,
            "current_parsed_text": clean_text,
            "content_hash": content_hash,
            "current_hash": content_hash,
            "file_path": file_path,
            "download_url": download_url,
            "is_delta": is_delta,
            "is_latest": True,
            "version_number": new_version,
            "previous_version_id": prev_doc_id,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        insert_result = supabase_client.table("documents").insert(new_doc).execute()
        if not insert_result.data:
            logger.error(
                "[DeltaEngine] Insert returned no data for '%s'. Possible DB error.", title
            )
            return {"status": "error", "doc_id": None, "is_delta": False, "version": 0}

        new_doc_id = insert_result.data[0]["id"]
        logger.info(
            "[DeltaEngine] ✅ Saved '%s' | v%s | delta=%s | change=%s",
            title, new_version, is_delta, change_type,
        )

        return {
            "status": change_type,
            "doc_id": new_doc_id,
            "is_delta": is_delta,
            "version": new_version,
            "prev_doc_id": prev_doc_id,
        }

    except Exception as e:
        logger.error("[DeltaEngine] Exception saving '%s': %s", title, e)
        return {"status": "error", "doc_id": None, "is_delta": False, "version": 0}


def check_record_has_docs(supabase_client, record_id: str) -> bool:
    """Quick check: does this record already have any docs in DB?"""
    try:
        res = (
            supabase_client.table("documents")
            .select("id")
            .eq("record_id", record_id)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception as e:
        logger.warning("[DeltaEngine] check_record_has_docs failed: %s", e)
        return False
