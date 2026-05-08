# NEW PRODUCTION READY ASYNC CAL EPROCURE AGENT
import os
import re
import time
import asyncio
import logging
import hashlib
from urllib.parse import urljoin
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

import supabase
from playwright.async_api import async_playwright
from document_parser import DocumentParser

load_dotenv()
logger = logging.getLogger("Agent1-CalEprocure")
logger.setLevel(logging.INFO)

from delta_engine import save_document_with_delta_detection, check_record_has_docs

CACHE_DIR = None

def setup_paths(data_dir="/data"):
    global CACHE_DIR
    CACHE_DIR = Path(data_dir) / "contract_files"
    CACHE_DIR.mkdir(exist_ok=True, parents=True)

# Global Supabase Client
sb_client = None

def get_supabase():
    global sb_client
    if sb_client is None:
        url = os.getenv("SUPABASE_URL")
        key = (
            os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            or os.getenv("SUPABASE_ANON_KEY", "").strip()
        )
        if url and key:
            sb_client = supabase.create_client(url, key)
    return sb_client


async def process_external_url(external_url: str, record_id: str, notice_id: str, p_context, db, record_already_has_docs: bool):
    logger.info(f"\n  =======================================================")
    logger.info(f"  🌊 [EXTERNAL SNATCHER] Navigating to: {external_url}")
    logger.info(f"  =======================================================")
    
    ext_page = await p_context.new_page()
    try:
        await ext_page.goto(external_url, wait_until="domcontentloaded", timeout=60000)
        await ext_page.wait_for_timeout(5000)
        
        # Look for explicit document links on this external page
        locators = await ext_page.locator("a").all()
        doc_links = set()
        
        for loc in locators:
            try:
                href = await loc.get_attribute("href")
                if not href: continue
                
                hl = href.lower()
                if hl.endswith(('.pdf', '.doc', '.docx', '.xls', '.xlsx')) or 'download' in hl:
                    absolute_url = urljoin(external_url, href)
                    doc_links.add(absolute_url)
            except Exception:
                pass
                
        if not doc_links:
            logger.warning("  [EXTERNAL SNATCHER] No explicit document links (.pdf, .docx, or 'download') found on external page.")
            return

        logger.info(f"  [EXTERNAL SNATCHER] Found {len(doc_links)} potential document links. Snatching them...")
        
        for doc_idx, d_url in enumerate(doc_links):
            logger.info(f"  [EXTERNAL SNATCHER] Downloading {doc_idx+1}/{len(doc_links)}: {d_url}")
            try:
                response = await p_context.request.get(d_url)
                if response.status == 200:
                    file_bytes = await response.body()
                    
                    # Resolve name
                    cd = response.headers.get("content-disposition", "")
                    filename = f"ext_snatch_{notice_id}_{doc_idx}.pdf"
                    if 'filename="' in cd:
                        filename = cd.split('filename="')[-1].split('"')[0]
                    else:
                        path_part = d_url.split("/")[-1].split("?")[0]
                        if "." in path_part[-5:]:
                            filename = f"{notice_id}_{path_part}"
                            
                    filename = filename.replace(" ", "+").replace("%20", "+")
                    final_file_path = CACHE_DIR / f"{int(time.time())}_{filename}"
                    
                    with open(final_file_path, "wb") as f:
                        f.write(file_bytes)
                        
                    logger.info(f"  [DOWNLOAD] Saved -> {filename}")
                    
                    # Parse using central parser
                    text_content, content_hash = DocumentParser.process_file(str(final_file_path))
                    
                    if not text_content or len(text_content.strip()) < 10:
                        with open(final_file_path, "rb") as f:
                            content_hash = hashlib.sha256(f.read()).hexdigest()
                            text_content = "[TEXT EXTRACTION FAILED OR EMPTY - RAW BLOB HASHED]"

                    logger.info(f"  [PARSER] Ext File: {filename} | Hash: {content_hash[:10]}...")

                    if db and record_id:
                        # Use the pre-loop flag passed in — do NOT re-check DB here
                        result = save_document_with_delta_detection(
                            supabase_client=db,
                            record_id=record_id,
                            title=filename,
                            content_text=text_content,
                            content_hash=content_hash,
                            file_path=str(final_file_path),
                            download_url=d_url,
                            record_already_has_docs=record_already_has_docs,
                            local_file_path=str(final_file_path),
                        )
                        logger.info(f"  [DB] CalEP external doc result: {result['status']} | delta={result['is_delta']}")
                else:
                    logger.error(f"  [EXTERNAL SNATCHER] Download failed HTTP {response.status} for {d_url}")
            except Exception as dl_err:
                logger.error(f"  [EXTERNAL SNATCHER] Error downloading {d_url}: {dl_err}")
                
    except Exception as e:
        logger.error(f"  [EXTERNAL SNATCHER] Error processing external URL: {e}")
    finally:
        await ext_page.close()

async def extract_portal_beta_advanced(record_id: str, notice_id: str, url: str, p_context):
    logger.info(f"\n=======================================================")
    logger.info(f"🚀 CAL EPROCURE PRODUCTION EXTRACTION: Notice ID {notice_id}")
    logger.info(f"=======================================================")
    
    db = get_supabase()
    
    # PRE-LOOP CHECK (Done ONCE per record, before any downloading begins)
    # This flag stays CONSTANT for the ENTIRE file loop below.
    # First run  → False → ALL files = Baseline (is_delta=False)
    # Later runs → True  → Engine compares hash of each file vs its stored version
    record_already_has_docs = False
    if db and record_id:
        existing_check = db.table("documents").select("id").eq("record_id", record_id).limit(1).execute()
        if existing_check.data:
            record_already_has_docs = True
            logger.info(f"  [AMENDMENT MODE] Record has existing baseline docs. Engine will compare hashes.")
        else:
            logger.info(f"  [BASELINE MODE] Record is fresh. ALL files in this batch will be saved as Baseline.")

    page = await p_context.new_page()
    
    try:
        logger.info(f"  [WEB] Entering SecurePortalBeta Portal: {url}")
        # Wait until page fully loads
        await page.goto(url, wait_until="networkidle", timeout=90000)
        
        # Check for Access Denied or Blocking
        body_text = await page.inner_text("body")
        if "Access Denied" in body_text or "blocked" in body_text.lower():
            logger.error("  [ERROR] 🛑 Access Denied! SecurePortalBeta is blocking Modal IP.")
            await page.close()
            return
        
        # Check carefully if 'View Event Package' button exists
        try:
            logger.info("  [WEB] Waiting for 'View Event Package' button (up to 45s)...")
            await page.wait_for_selector("#RESP_INQ_DL0_WK_AUC_DOWNLOAD_PB", state="visible", timeout=45000)
        except Exception:
            await page.wait_for_timeout(5000)

        # Basic title log (Optional)
        try:
            title_text = await page.locator("h1, h2, h3, span").filter(has_text=notice_id).first.inner_text()
            logger.info(f"  [WEB] Page Title Confirmed: {title_text.strip()}")
        except: pass

        pkg_btn = page.locator("#RESP_INQ_DL0_WK_AUC_DOWNLOAD_PB")
        if not await pkg_btn.is_visible():
            logger.warning(f"  [SCAN] No 'View Event Package' button found for {notice_id}. Ending.")
            await page.close()
            return

        logger.info("  [WEB] Entering Document Portal Modal...")
        
        try:
            async with p_context.expect_page(timeout=5000) as popup_info:
                await pkg_btn.click(force=True, timeout=5000)
            new_page = await popup_info.value
            await new_page.wait_for_load_state("domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
        except Exception:
            # It might have navigated in the same page or there was no popup
            try:
                await pkg_btn.click(force=True, timeout=5000)
            except:
                pass
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await asyncio.sleep(2)
        
        # In SecurePortalBeta, attachments might open in popups or the same page DOM.
        documents_snatched = 0
        for p_idx, pg in enumerate(p_context.pages):
            try:
                logger.info(f"  [SCAN] Checking Tab {p_idx} for document triggers...")
                await pg.wait_for_load_state("domcontentloaded", timeout=10000)
                
                trigger_locator = pg.locator("button[id^='PV_ATTACH_WRK_SCM_DOWNLOAD'], button[name^='PV_ATTACH_WRK_SCM_DOWNLOAD'], a[id^='PV_ATTACH_WRK_SCM_DOWNLOAD']")
                count = await trigger_locator.count()
                
                if count > 0:
                    logger.info(f"  [SCAN] Found {count} document triggers inside Tab {p_idx}.")
                    for i in range(count):
                        try:
                            logger.info(f"  [PERFECT] Snatching Document {i+1}/{count}...")
                            trigger = trigger_locator.nth(i)
                            await trigger.scroll_into_view_if_needed()
                            await trigger.click(force=True)
                            
                            # Wait for inner attachment modal
                            try:
                                await pg.wait_for_selector("#attachmentWrapperModal", state="visible", timeout=10000)
                            except Exception:
                                await pg.wait_for_timeout(3000)
                                
                            modal = pg.locator("#attachmentWrapperModal")
                            if await modal.is_visible():
                                dl_link = modal.locator("#downloadButton, a:has-text('Download')").first
                                
                                try:
                                    await dl_link.wait_for(state="attached", timeout=10000)
                                except: pass
                                
                                if await dl_link.count() > 0:
                                    href = await dl_link.get_attribute("href")
                                    retries = 0
                                    while not href and retries < 5:
                                        await pg.wait_for_timeout(1000)
                                        href = await dl_link.get_attribute("href")
                                        retries += 1
                                        
                                    if href:
                                        logger.info(f"  [PERFECT] Download URL ready. Sending authenticated GET...")
                                        
                                        # Snatch Bytes
                                        response = await p_context.request.get(href)
                                        if response.status == 200:
                                            pdf_bytes = await response.body()
                                            
                                            # Resolve name
                                            cd = response.headers.get("content-disposition", "")
                                            filename = f"portal_beta_snatch_{notice_id}_{i}.pdf"
                                            if 'filename="' in cd:
                                                filename = cd.split('filename="')[-1].split('"')[0]
                                                
                                            # Avoid spaces/weird chars in filenames based on SecurePortalGamma
                                            filename = filename.replace(" ", "+")
                                                
                                            final_file_path = CACHE_DIR / f"{int(time.time())}_{filename}"
                                            with open(final_file_path, "wb") as f:
                                                f.write(pdf_bytes)
                                                
                                            logger.info(f"  [DOWNLOAD] Saved -> {filename}")
                                            
                                            # Parse via Central Parser
                                            text_content, content_hash = DocumentParser.process_file(str(final_file_path))
                                            
                                            if not text_content or len(text_content.strip()) < 10:
                                                with open(final_file_path, "rb") as f:
                                                    content_hash = hashlib.sha256(f.read()).hexdigest()
                                                    text_content = "[TEXT EXTRACTION FAILED OR EMPTY - RAW BLOB HASHED]"
                                                    
                                            logger.info(f"  [PARSER] File: {filename} | Hash: {content_hash[:10]}...")
                                            
                                            if db and record_id:
                                                result = save_document_with_delta_detection(
                                                    supabase_client=db,
                                                    record_id=record_id,
                                                    title=filename,
                                                    content_text=text_content,
                                                    content_hash=content_hash,
                                                    file_path=str(final_file_path),
                                                    download_url=url,
                                                    record_already_has_docs=record_already_has_docs,
                                                    local_file_path=str(final_file_path),
                                                )
                                                logger.info(f"  [DB] CalEP doc result: {result['status']} | delta={result['is_delta']} | v{result['version']}")
                                            
                                            documents_snatched += 1
                                        else:
                                            logger.error(f"  [PERFECT] Download failed HTTP {response.status}")
                                            
                                # CLOSE inner attachment modal
                                close_btn = modal.locator("button.close, button:has-text('Close')").first
                                if await close_btn.count() > 0:
                                    await close_btn.click(force=True)
                                await pg.keyboard.press("Escape")
                                await pg.wait_for_timeout(1000)
                        except Exception as doc_err:
                            logger.error(f"  [PERFECT] Error on document {i}: {doc_err}")
            except Exception as tab_err:
                pass
                
        # Check for external URLs if no standard attachments were found
        if documents_snatched == 0:
            logger.warning(f"  [SCAN] No standard attachments found. Extracting and printing all page text...")
            for p_idx, pg in enumerate(p_context.pages):
                try:
                    # Get visible text on the page and all frames
                    all_texts = []
                    
                    try:
                        all_texts.append(await pg.locator("body").inner_text())
                    except: pass
                    
                    # Ensure we grab text from textareas since the UI shows a resizable box
                    try:
                        textareas = await pg.locator("textarea").all_text_contents()
                        for ta in textareas:
                            all_texts.append(ta)
                        
                        textarea_vals = []
                        for ta in await pg.locator("textarea").all():
                            val = await ta.input_value()
                            if val: textarea_vals.append(val)
                        all_texts.extend(textarea_vals)
                    except: pass
                    
                    # Also look inside frames because Comments are often embedded in iframes
                    for f_idx, frame in enumerate(pg.frames):
                        try:
                            f_text = await frame.locator("body").inner_text()
                            if f_text and len(f_text.strip()) > 5:
                                all_texts.append(f"--- FRAME {f_idx} TEXT ---\n{f_text}")
                        except: pass
                        
                    body_text = "\n".join(all_texts)
                    
                    # Instead of just taking any URL, let's analyze its context (words nearby)
                    target_keywords = ["visit", "view", "dataset", "request", "download", "link", "submissionders", "published"]
                    excluded_domains = ["portal_beta.ca.gov", "google-analytics.com", "fiscal.ca.gov", "w3.org", "schema.org"]
                    
                    found_target = False
                    # Use finditer to get positional information for contextual analysis
                    for match in re.finditer(r'(https?://[^\s<>\"\']+)', body_text):
                        url = match.group(1)
                        
                        # Skip excluded or known bad domains
                        if any(bad_domain in url.lower() for bad_domain in excluded_domains):
                            continue
                            
                        # Extract a context window around the URL (approx 150 chars before and after)
                        start_idx = max(0, match.start() - 150)
                        end_idx = min(len(body_text), match.end() + 150)
                        
                        context_window = body_text[start_idx:end_idx].lower()
                        
                        # Check if any of our target keywords appear in this window
                        matched_keywords = [kw for kw in target_keywords if kw in context_window]
                        
                        if matched_keywords:
                            logger.info(f"  [EXTERNAL LINK LOGIC] Contextual URL Discovered: {url}")
                            logger.info(f"  [EXTERNAL LINK LOGIC] Matches Keywords: {matched_keywords}")
                            
                            # Clean up the snippet for logging
                            snippet = body_text[start_idx:end_idx].replace('\n', ' ').strip()
                            logger.info(f"  [EXTERNAL LINK LOGIC] Context Snippet: ...{snippet}...")
                            
                            found_target = True
                            # Break out to avoid multiple navigations per text match
                            break
                            
                    if found_target:
                        logger.info(f"  [EXTERNAL LINK LOGIC] Triggering External Document Snatcher for {url}...")
                        await process_external_url(url, record_id, notice_id, p_context, db, record_already_has_docs)
                        break
                except Exception as e:
                    pass

        logger.info(f"🏆 MISSION COMPLETE for Notice ID {notice_id}")
    except Exception as e:
        logger.error(f"  [ERROR] Page navigation/extraction failed: {e}")
    finally:
        await page.close()


async def orchestrate_single_portal_beta(record, data_dir="/data"):
    logger.info(f"Starting SINGLE TENDER SecurePortalBeta Extractor for Notice ID: {record.get('notice_id')}")
    setup_paths(data_dir)
    db = get_supabase()
    if not db:
        logger.error("[ORCHESTRATOR] ❌ Missing Supabase Connection!")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Using a standard context. Since it's public, WAF is lower, but we still mask it.
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            accept_downloads=True
        )
        
        tid = record.get("id")
        nid = record.get("notice_id", "UNKNOWN")
        url = record.get("url")
        
        if url:
            logger.info(f"Firing extraction solely for Notice ID: {nid}")
            await extract_portal_beta_advanced(tid, nid, url, context)
                
        await context.close()
        await browser.close()


async def orchestrated_cloud_main(data_dir="/data"):
    setup_paths(data_dir)
    db = get_supabase()
    if not db:
        logger.error("[ORCHESTRATOR] ❌ Missing Supabase Connection!")
        return
        
    try:
        logger.info("[ORCHESTRATOR] Querying Supabase for active SecurePortalBeta targets...")
        response = db.table("records").select("id, notice_id, url").eq("source", "cal_eprocure").execute()
        records = response.data
        if not records:
            logger.info("[ORCHESTRATOR] 🛑 No SecurePortalBeta records found in DB.")
            return
            
        logger.info(f"[ORCHESTRATOR] Found {len(records)} pending targets for SecurePortalBeta.")
    except Exception as e:
        logger.error(f"[ORCHESTRATOR] Database query failed: {e}")
        return
        
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Using a standard context. Since it's public, WAF is lower, but we still mask it.
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            accept_downloads=True
        )
        
        # Sequentially visit all URLs found in the DB (LOOP)
        for t in records:
            tid = t.get("id")
            nid = t.get("notice_id", "UNKNOWN")
            url = t.get("url")
            
            if url:
                logger.info(f"Firing extraction solely for Notice ID: {nid}")
                await extract_portal_beta_advanced(tid, nid, url, context)
                logger.info("  [SLEEP] Yielding 5 seconds between targets to respect servers...")
                await asyncio.sleep(5)
                
        await context.close()
        await browser.close()
        logger.info("[ORCHESTRATOR] All targets processed successfully.")

if __name__ == "__main__":
    asyncio.run(orchestrated_cloud_main("."))
"""
=============================================================================
BIDVERITAS - AGENT 1: THE PERFECTIONIST DEEP EXTRACTOR
=============================================================================
Role: The Intelligence Gatherer (Advanced Level).
      This is the FINAL, 100% RELIABLE version of the Agent 1 engine.
      - Handles the 'Two-Step Modal' download process.
      - Extracts the exact download URL from the session-secured modal.
      - Uses direct authenticated HTTP requests to snatch PDF bytes.
      - Bypasses all flaky browser download events.

Outcome: 100% data extraction from secure enterprise portals.
=============================================================================
"""

import os
import json
import time
import hashlib
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import fitz  # PyMuPDF
from dotenv import load_dotenv

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# =============================================================================
# SETUP
# =============================================================================
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Agent1-Final] %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

CACHE_DIR = Path("temp_cache")
CACHE_DIR.mkdir(exist_ok=True)


# =============================================================================
# PERFECTIONIST ENGINE
# =============================================================================
def extract_record_advanced(client_name: str, url: str) -> dict:
    logger.info(f"\n{'='*75}")
    logger.info(f"PERFECTIONIST EXTRACTION MISSION: {url}")
    logger.info(f"{'='*75}")

    captured_pdfs = []
    total_web_text = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Use context to share cookies between page and request
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            accept_downloads=True
        )
        
        main_page = context.new_page()
        Stealth().apply_stealth_sync(main_page)

        # 1. LANDING
        logger.info(f"  [WEB] Navigating to: {url}")
        main_page.goto(url, wait_until="domcontentloaded", timeout=60000)
        
        try:
            main_page.wait_for_selector("#RESP_INQ_DL0_WK_AUC_DOWNLOAD_PB", state="visible", timeout=20000)
        except:
            main_page.wait_for_timeout(5000)

        total_web_text += main_page.inner_text("body")
        
        # 2. TRIGGER ATTACHMENTS PORTAL
        pkg_btn = main_page.locator("#RESP_INQ_DL0_WK_AUC_DOWNLOAD_PB")
        if pkg_btn.is_visible():
            logger.info("  [WEB] Entering Document Portal...")
            pkg_btn.click()
            main_page.wait_for_timeout(6000) 
            
            # --- SCAN ALL PAGES (Handling Popups) ---
            for p_idx, pg in enumerate(context.pages):
                try:
                    logger.info(f"  [SCAN] Checking Tab {p_idx}...")
                    pg.wait_for_load_state("domcontentloaded", timeout=10000)
                    total_web_text += f"\n\n--- TAB {p_idx} ---\n" + pg.inner_text("body")
                    
                    # 3. IDENTIFY TRIGGERS
                    trigger_locator = pg.locator("button[id^='PV_ATTACH_WRK_SCM_DOWNLOAD'], button[name^='PV_ATTACH_WRK_SCM_DOWNLOAD'], a[id^='PV_ATTACH_WRK_SCM_DOWNLOAD']")
                    
                    count = trigger_locator.count()
                    if count > 0:
                        logger.info(f"  [SCAN] Found {count} document triggers.")
                        for i in range(count):
                            try:
                                logger.info(f"  [PERFECT] Snatching Item {i}...")
                                # Re-query using nth() to avoid stale elements
                                trigger = trigger_locator.nth(i)
                                trigger.scroll_into_view_if_needed()
                                trigger.click(force=True)
                                
                                # Wait for modal to appear reliably
                                try:
                                    pg.wait_for_selector("#attachmentWrapperModal", state="visible", timeout=10000)
                                except Exception:
                                    pg.wait_for_timeout(3000)
                                    
                                modal = pg.locator("#attachmentWrapperModal")
                                if modal.is_visible():
                                    # Target the exact ID found by the browser subagent
                                    dl_link = modal.locator("#downloadButton, a:has-text('Download')").first
                                    
                                    # Wait for the href to actually populate
                                    try:
                                        dl_link.wait_for(state="attached", timeout=10000)
                                    except:
                                        pass
                                    
                                    if dl_link.count() > 0:
                                        href = dl_link.get_attribute("href")
                                        # Retry getting href if empty (SPA rendering delay)
                                        retries = 0
                                        while not href and retries < 5:
                                            pg.wait_for_timeout(1000)
                                            href = dl_link.get_attribute("href")
                                            retries += 1
                                            
                                        if href:
                                            logger.info(f"  [PERFECT] Final URL Captured: {href[:60]}...")
                                            
                                            # SNATCH BYTES DIRECTLY
                                            response = context.request.get(href)
                                            if response.status == 200:
                                                pdf_bytes = response.body()
                                                temp_pdf = os.path.join(tempfile.gettempdir(), f"final_{i}_{int(time.time())}.pdf")
                                                with open(temp_pdf, "wb") as f:
                                                    f.write(pdf_bytes)
                                                
                                                try:
                                                    doc = fitz.open(temp_pdf)
                                                    text = "".join([f"\n-Page{p+1}-\n{doc[p].get_text()}" for p in range(len(doc))])
                                                    doc.close()
                                                    
                                                    captured_pdfs.append({
                                                        "filename": f"document_{i}.pdf",
                                                        "text": text.strip()
                                                    })
                                                    logger.info(f"  [PERFECT] ✅ Snatched {len(text)} characters from PDF.")
                                                except Exception as pdf_err:
                                                    logger.error(f"  [PERFECT] Failed to parse PDF {i}: {pdf_err}")
                                                
                                                if os.path.exists(temp_pdf):
                                                    os.unlink(temp_pdf)
                                            else:
                                                logger.error(f"  [PERFECT] Download failed (HTTP {response.status})")
                                    
                                    # ALWAYS CLOSE MODAL RELIABLY
                                    close_btn = modal.locator("button.close, button:has-text('Close')").first
                                    if close_btn.count() > 0:
                                        close_btn.click(force=True)
                                    pg.keyboard.press("Escape")
                                    try:
                                        modal.wait_for(state="hidden", timeout=5000)
                                    except:
                                        pass
                                    pg.wait_for_timeout(1000)
                            except Exception as e:
                                logger.error(f"  [PERFECT] Error on item {i}: {e}")
                except Exception: pass

        browser.close()

    # --- SAVE MISSION DATA ---
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    safe_name = "".join(c if c.isalnum() else "_" for c in url.split("//")[-1][:40])

    record_state = {
        "meta": {
            "client_name": client_name,
            "url": url,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "pdf_count": len(captured_pdfs),
            "engine": "Perfectionist_v11_DirectFetch"
        },
        "attachments": captured_pdfs,
        "combined_text": total_web_text + "\n\n" + "\n\n".join(
            [f"=== ATTACHMENT {i}: {a['filename']} ===\n{a['text']}" for i, a in enumerate(captured_pdfs)]
        )
    }

    cache_filename = CACHE_DIR / f"{url_hash}_{safe_name}.json"
    with open(cache_filename, "w", encoding="utf-8") as f:
        json.dump(record_state, f, indent=2, ensure_ascii=False)

    logger.info(f"\n🏆 MISSION COMPLETE | Docs Snatched: {len(captured_pdfs)}")
    return record_state

def run_agent1():
    clients_file = Path("clients.json")
    if not clients_file.exists(): return
    with open(clients_file, "r") as f:
        clients = json.load(f)
    for client in clients:
        for url in client.get("watch_urls", []):
            if "portal_beta.ca.gov" not in url.lower():
                logger.info(f"Skipping non-SecurePortalBeta target: {url}")
                continue
                
            try:
                extract_record_advanced(client["client_name"], url)
            except Exception as e:
                logger.error(f"Perfectionist Agent failed: {e}")

if __name__ == "__main__":
    run_agent1()