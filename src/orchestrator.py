"""
SubmissionVeritas — Modal Manager (Master Orchestrator)

SCHEDULE: Mon-Fri at 21:00 UTC (9:00 PM UTC)
Deployment: modal deploy modal_manager.py

SEQUENCE:
  Phase 1: Scrape records (parallel)
  Phase 2: AI Triage - analyze deltas (sequential, waits)
  Phase 3: PDF Generation (sequential, waits)
  Phase 4: Notification Dispatch + Baseline Promotion (sequential, waits)
"""

import modal
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s - %(message)s")

app = modal.App("bidveritas-pipeline")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "tesseract-ocr", "poppler-utils",
        "xvfb", "libgbm1", "libnss3", "libasound2", "libxcomposite1",
        "libxdamage1", "libxrandr2", "libatk1.0-0", "libgtk-3-0",
        "libxshmfence1", "chromium"
    )
    .pip_install(
        "playwright", "pymupdf", "supabase", "python-dotenv",
        "python-docx", "openpyxl", "pandas", "pytesseract",
        "pdf2image", "pillow", "playwright-stealth", "nodriver",
        "anticaptchaofficial", "pyvirtualdisplay",
        "google-cloud-aiplatform", "google-auth", "reportlab", "resend",
    )
    .run_commands("playwright install chromium", "playwright install-deps chromium")
    .add_local_file("src/agents/gamma.py", remote_path="/root/gamma.py")
    .add_local_file("src/agents/beta.py", remote_path="/root/beta.py")
    .add_local_file("src/agents/alpha.py", remote_path="/root/alpha.py")
    .add_local_file("src/core/delta.py", remote_path="/root/delta.py")
    .add_local_file("src/core/triage.py", remote_path="/root/triage.py")
    .add_local_file("src/utils/reports.py", remote_path="/root/reports.py")
    .add_local_file("src/utils/notifications.py", remote_path="/root/notifications.py")
    .add_local_file("src/core/parser.py", remote_path="/root/parser.py")
)

state_volume = modal.Volume.from_name("recordtrack-state", create_if_missing=True)

# ── SCRAPER FUNCTIONS ─────────────────────────────────────────────
@app.function(image=image, volumes={"/data": state_volume},
              secrets=[modal.Secret.from_name("my-recordtrack-secrets")],
              timeout=900, max_containers=25)
def process_single_portal_alpha_task(record):
    import os, subprocess, glob

    # Kill any stale browser processes
    os.system("pkill -9 -f chromium || true")
    os.system("pkill -9 -f chrome || true")

    # ── Find the playwright-installed chromium (modern, JS-capable) ──
    import glob, subprocess
    chromium_path = None

    # Try glob patterns first
    patterns = [
        "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/home/**/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            chromium_path = matches[0]
            break

    # Try which command as fallback
    if not chromium_path:
        try:
            result = subprocess.run(["find", "/root", "-name", "chrome", "-type", "f"],
                                    capture_output=True, text=True, timeout=10)
            for line in result.stdout.splitlines():
                if "playwright" in line and line.endswith("/chrome"):
                    chromium_path = line.strip()
                    break
        except Exception:
            pass

    if chromium_path:
        print(f"[SETUP] ✅ Found playwright chromium: {chromium_path}")
    else:
        print("[SETUP] ⚠️  playwright chromium NOT found — using system /bin/chromium")
        # Print system chromium version for debugging
        os.system("/bin/chromium --version 2>/dev/null || echo 'system chromium not found'")

    from pyvirtualdisplay import Display
    disp = Display(visible=0, size=(1920, 1080))
    disp.start()
    try:
        import asyncio, alpha
        # Pass chromium_path into the agent so it can use it
        if chromium_path:
            alpha.CHROMIUM_PATH_OVERRIDE = chromium_path
        else:
            alpha.CHROMIUM_PATH_OVERRIDE = None
        return asyncio.run(alpha.run_single_portal_alpha(record))
    finally:
        disp.stop()



@app.function(image=image, volumes={"/data": state_volume},
              secrets=[modal.Secret.from_name("my-recordtrack-secrets")],
              timeout=900, max_containers=25)
def process_single_portal_beta_task(record):
    import asyncio, beta
    asyncio.run(beta.orchestrate_single_portal_beta(record, "/data"))

@app.function(image=image, volumes={"/data": state_volume},
              secrets=[modal.Secret.from_name("my-recordtrack-secrets")],
              timeout=900, max_containers=50)
def process_single_portal_gamma_task(record):
    import asyncio, gamma
    asyncio.run(gamma.orchestrate_single_portal_gamma(record, "/data"))

# ── AMENDMENT PIPELINE ────────────────────────────────────────────
@app.function(image=image, secrets=[modal.Secret.from_name("my-recordtrack-secrets")], timeout=1800)
def run_delta_triage():
    import triage
    triage.process_queue()
    return "triage_done"

@app.function(image=image, secrets=[modal.Secret.from_name("my-recordtrack-secrets")], timeout=900)
def run_pdf_generation():
    import reports
    reports.process_pending_pdfs()
    return "pdf_done"

@app.function(image=image, secrets=[modal.Secret.from_name("my-recordtrack-secrets")], timeout=900)
def run_notification_dispatch():
    import notification_dispatcher
    notification_dispatcher.run_dispatch_cycle()
    return "dispatch_done"

# ── CLOUD-SIDE MASTER ORCHESTRATOR ──
@app.function(image=image, volumes={"/data": state_volume},
              secrets=[modal.Secret.from_name("my-recordtrack-secrets")],
              timeout=3600,
              schedule=modal.Cron("0 21 * * 1-5"))
def orchestrate_full_cloud_run():
    """Runs the entire pipeline ON MODAL to avoid local network/DNS issues."""
    from supabase import create_client
    import os
    
    logger = logging.getLogger("CLOUD-MANAGER")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return "ERROR: Missing Supabase credentials in Modal Secret"
    
    db = create_client(url, key)
    
    # PHASE 1: SCRAPING
    logger.info("PHASE 1: SCRAPING")
    # SecurePortalAlpha
    pb = db.table("records").select("id, url, notice_id").eq("portal_type", "portal_alpha").execute().data or []
    if pb:
        # We call the other Modal functions directly from here
        list(process_single_portal_alpha_task.map(pb))
    
    # SecurePortalGamma
    sam = db.table("records").select("id, url, notice_id").eq("portal_type", "federal").execute().data or []
    if sam:
        list(process_single_portal_gamma_task.map(sam))

    # SecurePortalBeta
    cal = db.table("records").select("id, url, notice_id").eq("source", "cal_eprocure").execute().data or []
    if cal:
        list(process_single_portal_beta_task.map(cal))

    # PHASE 2: AI TRIAGE
    logger.info("PHASE 2: AI TRIAGE")
    run_delta_triage.local() # .local() runs it in the same cloud container context

    # PHASE 3: PDF GENERATION
    logger.info("PHASE 3: PDF BUILD")
    run_pdf_generation.local()

    # PHASE 4: DISPATCH
    logger.info("PHASE 4: DISPATCH")
    run_notification_dispatch.local()

    return "FULL CYCLE SUCCESSFUL"

# ── MASTER ORCHESTRATOR ───────────────────────────────────────────
@app.local_entrypoint()
def run_full_cycle():
    """Trigger the cloud orchestrator."""
    print("🚀 Triggering FULL CLOUD CYCLE on Modal.com...")
    result = orchestrate_full_cloud_run.remote()
    print(f"🏁 Cloud Result: {result}")

@app.local_entrypoint()
def main():
    run_full_cycle()
