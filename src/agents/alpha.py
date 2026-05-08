"""
Agent 1 SecurePortalAlpha

Current scope:
- Read SecurePortalAlpha URLs from clients.json
- Open each URL in browser
- Click Begin button with robust fallback logic
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import supabase
from document_parser import DocumentParser
from delta_engine import save_document_with_delta_detection, check_record_has_docs

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

load_dotenv()

logger = logging.getLogger("Agent1-SecurePortalAlpha")
logger.setLevel(logging.INFO)

CLIENTS_FILE = "clients.json"
DOCUMENT_AUDIT_FILE = "portal_alpha_documents_audit.json"
DOCUMENT_CONTENT_FILE = "portal_alpha_documents_content.json"
WAF_DIAGNOSTIC_FILE = "portal_alpha_waf_diagnostics.jsonl"
DOWNLOAD_DIR = Path(os.getenv("AGENT1_DOWNLOAD_DIR", "contract_files"))
EXTRACT_DIR = Path(os.getenv("AGENT1_EXTRACT_DIR", "contract_files"))
ZIP_WAIT_TIMEOUT = int(os.getenv("AGENT1_ZIP_WAIT_TIMEOUT", "240"))
ANTICAPTCHA_KEY = os.getenv("ANTICAPTCHA_API_KEY", "").strip()
PB_EMAIL = os.getenv("PB_EMAIL", "support@forexfundai.com")
PB_PASSWORD = os.getenv("PB_PASSWORD", "").strip()
AGENT1_HEADLESS = os.getenv("AGENT1_HEADLESS", "False").lower() in {
    "1",
    "true",
    "yes",
    "y",
    "on",
}
AGENT1_KEEP_BROWSER_OPEN = os.getenv("AGENT1_KEEP_BROWSER_OPEN", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
    "on",
}

# Set by modal_manager.py at runtime to use the playwright-installed chromium
# which supports modern JS. Falls back to system chromium if None.
CHROMIUM_PATH_OVERRIDE = None

sb_client = None
def get_supabase():
    global sb_client
    if sb_client is None:
        url = os.getenv("SUPABASE_URL", "").strip()
        # Prefer SERVICE_ROLE for reliable writes; fallback to ANON
        key = (
            os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            or os.getenv("SUPABASE_ANON_KEY", "").strip()
        )
        if url and key:
            sb_client = supabase.create_client(url, key)
    return sb_client


def _normalized_pb_email(raw_email):
    email = (raw_email or "").strip()
    if not email:
        return "support@forexfundai.com"
    if "@" not in email and email.lower() == "supportforexfundai.com":
        return "support@forexfundai.com"
    return email


def load_clients():
    if not os.path.exists(CLIENTS_FILE):
        return []
    with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_portal_alpha_urls(clients):
    urls = []
    for client in clients:
        for url in client.get("watch_urls", []):
            if isinstance(url, str) and "portal_alpha.com" in url:
                urls.append(url.strip())
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(urls))


def _normalize_eval_result(value):
    if isinstance(value, dict):
        if set(value.keys()) == {"type", "value"}:
            return _normalize_eval_result(value.get("value"))
        return {k: _normalize_eval_result(v) for k, v in value.items()}

    if isinstance(value, list):
        if all(
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], str)
            for item in value
        ):
            normalized = {}
            for key, raw_val in value:
                normalized[key] = _normalize_eval_result(raw_val)
            return normalized
        return [_normalize_eval_result(item) for item in value]

    return value


async def _safe_evaluate(page, script, timeout=8):
    try:
        raw = await asyncio.wait_for(page.evaluate(script), timeout=timeout)
        return _normalize_eval_result(raw)
    except asyncio.TimeoutError:
        logger.warning("JS evaluate timed out after %ss.", timeout)
        return None
    except Exception as e:
        logger.warning("JS evaluate failed: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# PLAYWRIGHT ADAPTER — Makes Playwright page/browser look like nodriver
# This allows ALL existing functions to work without modification.
# ═══════════════════════════════════════════════════════════════════════════════

# Native Playwright helpers for detection and interaction




async def _detect_captcha_stage(page):
    """Detect whether AWS WAF challenge is already active after Begin."""
    stage_script = """
    (() => {
        const text = (document.body?.innerText || '').toLowerCase();
        const hasBeginButton = !!document.querySelector('#amzn-captcha-verify-button, button.amzn-captcha-verify-button, .amzn-captcha-state-container button, button[id*="captcha"][id*="verify"]');
        const hasCaptchaWidget =
            !!document.querySelector('.amzn-captcha-state-container, [id^="amzn-captcha-"], [class*="amzn-captcha"]') ||
            text.includes('complete the security check before continuing') ||
            text.includes("let's confirm you are human") ||
            text.includes('human verification');

        return {
            hasBeginButton,
            hasCaptchaWidget,
            title: document.title || '',
            href: location.href || ''
        };
    })()
    """
    return await _safe_evaluate(page, stage_script, timeout=4)


async def click_begin_if_present(page, max_attempts=8, delay_seconds=4):
    """Retry-click Amazon WAF Begin and generic Begin buttons."""
    begin_selectors = [
        "#amzn-captcha-verify-button",
        "button.amzn-captcha-verify-button",
        ".amzn-captcha-state-container button",
        "button[id*='captcha'][id*='verify']",
    ]

    begin_js_fallback = """
    (() => {
        try {
            const selectors = [
                '#amzn-captcha-verify-button',
                'button.amzn-captcha-verify-button',
                '.amzn-captcha-state-container button',
                "button[id*='captcha'][id*='verify']"
            ];
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el) {
                    el.click();
                    return { clicked: true, strategy: selector };
                }
            }
            const nodes = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')).slice(0, 120);
            const beginBtn = nodes.find(el => {
                const t = ((el.textContent || el.value || '') + '').trim().toLowerCase();
                return t === 'begin' || t.startsWith('begin ');
            });
            if (beginBtn) {
                beginBtn.click();
                return { clicked: true, strategy: 'text:begin' };
            }
            return { clicked: false };
        } catch (e) {
            return { clicked: false, error: String(e) };
        }
    })();
    """

    deep_begin_click_script = """
    (() => {
        try {
            const getRootNodes = (root) => {
                const roots = [root];
                const allEls = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
                for (const el of allEls) {
                    if (el.shadowRoot) roots.push(el.shadowRoot);
                }
                return roots;
            };

            const clickBeginInsideRoot = (root, strategyPrefix) => {
                const selectors = [
                    '#amzn-captcha-verify-button',
                    'button.amzn-captcha-verify-button',
                    '.amzn-captcha-state-container button',
                    "button[id*='captcha'][id*='verify']"
                ];

                for (const selector of selectors) {
                    const el = root.querySelector ? root.querySelector(selector) : null;
                    if (el) {
                        el.scrollIntoView({ block: 'center', inline: 'center' });
                        el.click();
                        return { clicked: true, strategy: `${strategyPrefix}:${selector}` };
                    }
                }

                const candidates = root.querySelectorAll
                    ? Array.from(root.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'))
                    : [];

                const beginBtn = candidates.find(el => {
                    const t = ((el.textContent || el.value || '') + '').trim().toLowerCase();
                    return t === 'begin' || t.startsWith('begin ');
                });

                if (beginBtn) {
                    beginBtn.scrollIntoView({ block: 'center', inline: 'center' });
                    beginBtn.click();
                    return { clicked: true, strategy: `${strategyPrefix}:text:begin` };
                }

                return null;
            };

            const roots = getRootNodes(document);
            for (const root of roots) {
                const res = clickBeginInsideRoot(root, 'doc');
                if (res) return res;
            }

            const iframes = Array.from(document.querySelectorAll('iframe'));
            for (const frame of iframes) {
                try {
                    const doc = frame.contentDocument;
                    if (!doc) continue;
                    const frameRoots = getRootNodes(doc);
                    for (const root of frameRoots) {
                        const res = clickBeginInsideRoot(root, 'iframe');
                        if (res) return res;
                    }
                } catch (e) {
                    // Ignore cross-origin iframe.
                }
            }

            return { clicked: false };
        } catch (e) {
            return { clicked: false, error: String(e) };
        }
    })();
    """

    for attempt in range(1, max_attempts + 1):
        stage = await _detect_captcha_stage(page)
        if isinstance(stage, dict):
            has_begin = bool(stage.get("hasBeginButton"))
            has_widget = bool(stage.get("hasCaptchaWidget"))
            if has_widget and not has_begin:
                logger.info(
                    "Begin already passed (captcha stage active) on attempt %s/%s.",
                    attempt,
                    max_attempts,
                )
                return True

        for selector in begin_selectors:
            try:
                btn = await asyncio.wait_for(page.select(selector), timeout=2)
            except Exception:
                btn = None

            if not btn:
                continue

            try:
                await btn.mouse_click()
            except Exception:
                try:
                    await btn.click()
                except Exception:
                    continue

            logger.info("Begin clicked via selector %s (attempt %s/%s).", selector, attempt, max_attempts)
            return True

        try:
            text_btns = await asyncio.wait_for(page.find_elements_by_text("Begin"), timeout=3)
            if text_btns:
                for btn in reversed(text_btns):
                    try:
                        await btn.mouse_click()
                        logger.info("Begin clicked via text lookup (attempt %s/%s).", attempt, max_attempts)
                        return True
                    except Exception:
                        continue
        except Exception:
            pass

        result = await _safe_evaluate(page, begin_js_fallback, timeout=3)
        if isinstance(result, dict) and result.get("clicked"):
            logger.info(
                "Begin clicked via JS fallback %s (attempt %s/%s).",
                result.get("strategy", "unknown"),
                attempt,
                max_attempts,
            )
            return True

        if attempt >= 2:
            deep_result = await _safe_evaluate(page, deep_begin_click_script, timeout=5)
            if isinstance(deep_result, dict) and deep_result.get("clicked"):
                logger.info(
                    "Begin clicked via deep DOM fallback %s (attempt %s/%s).",
                    deep_result.get("strategy", "unknown"),
                    attempt,
                    max_attempts,
                )
                return True

        stage = await _detect_captcha_stage(page)
        if isinstance(stage, dict):
            has_begin = bool(stage.get("hasBeginButton"))
            has_widget = bool(stage.get("hasCaptchaWidget"))
            if has_widget and not has_begin:
                logger.info(
                    "Begin transitioned to captcha stage after retries (attempt %s/%s).",
                    attempt,
                    max_attempts,
                )
                return True

        logger.info("Begin button not ready yet (attempt %s/%s).", attempt, max_attempts)
        await asyncio.sleep(delay_seconds)

    logger.warning("Could not auto-click Begin after %s attempts.", max_attempts)
    return False


def _short_text(value, limit=220):
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


async def extract_captcha_payload(page):
    """
    Confirm CAPTCHA state and extract AWS WAF payload fields needed for solving.
    This only logs data for validation; it does not send to Anti-Captcha yet.
    """
    detect_script = """
    (() => {
        const types = new Set();
        const iframes = Array.from(document.querySelectorAll('iframe'));
        for (const i of iframes) {
            const src = (i.src || '').toLowerCase();
            if (src.includes('recaptcha')) types.add('ReCaptcha');
            if (src.includes('turnstile')) types.add('Cloudflare Turnstile');
            if (src.includes('hcaptcha')) types.add('hCaptcha');
            if (src.includes('aws-waf') || src.includes('captcha.awswaf.com')) types.add('AWS WAF');
        }

        if (document.querySelector('#amzn-captcha-verify-button, .amzn-captcha-state-container, [id^="amzn-captcha-"], [class*="amzn-captcha"]')) {
            types.add('AWS WAF');
        }

        const href = location.href || '';
        const title = document.title || '';
        const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
        if (
            href.toLowerCase().includes('captcha.awswaf.com') ||
            href.toLowerCase().includes('human-verification') ||
            title.toLowerCase().includes('human verification') ||
            text.includes("let's confirm you are human") ||
            text.includes('human verification') ||
            bodyText.includes("let's confirm you are human") ||
            bodyText.includes('complete the security check before continuing')
        ) {
            types.add('AWS WAF');
        }

        return {
            providers: Array.from(types),
            url: href,
            title,
            iframeCount: iframes.length,
            scriptCount: document.querySelectorAll('script[src]').length
        };
    })()
    """

    extract_script = """
    (() => {
        try {
            const g = window.gokuProps || window.goku || window.gokuPropsV2 || null;
            const pick = (keys) => {
                if (!g || typeof g !== 'object') return '';
                for (const k of keys) {
                    if (g[k]) return g[k];
                }
                return '';
            };

            const iframeSrcs = Array.from(document.querySelectorAll('iframe')).map(f => f.src || '').slice(0, 30);
            const scriptSrcs = Array.from(document.querySelectorAll('script[src]')).map(s => s.src || '').slice(0, 80);

            let websiteKey = pick(['key', 'websiteKey', 'sitekey', 'siteKey']);
            let iv = pick(['iv']);
            let context = pick(['context']);

            for (const src of [...iframeSrcs, ...scriptSrcs]) {
                try {
                    const u = new URL(src, location.href);
                    if (!websiteKey) websiteKey = u.searchParams.get('key') || u.searchParams.get('sitekey') || u.searchParams.get('siteKey') || '';
                    if (!iv) iv = u.searchParams.get('iv') || '';
                    if (!context) context = u.searchParams.get('context') || '';
                } catch (e) {}
            }

            const captchaScript = scriptSrcs.find(src => src.includes('captcha.awswaf.com') && src.includes('captcha.js')) || '';
            const challengeScript = scriptSrcs.find(src => src.includes('token.awswaf.com') && src.includes('challenge.js')) || '';
            const jsapiScript = scriptSrcs.find(src => src.includes('jsapi.js')) || '';

            return {
                websiteKey,
                iv,
                context,
                captchaScript,
                challengeScript,
                jsapiScript,
                gokuExists: !!g,
                iframeSrcs,
                scriptSrcs
            };
        } catch (e) {
            return { error: String(e) };
        }
    })()
    """

    detect_data = await _safe_evaluate(page, detect_script, timeout=10)
    if not isinstance(detect_data, dict):
        logger.warning("CAPTCHA confirm step failed: detection script returned no data.")
        return None

    providers = detect_data.get("providers") or []
    logger.info(
        "CAPTCHA confirm -> providers=%s | title=%s | url=%s | iframes=%s | scripts=%s",
        providers,
        _short_text(detect_data.get("title", ""), 120),
        _short_text(detect_data.get("url", ""), 180),
        detect_data.get("iframeCount", 0),
        detect_data.get("scriptCount", 0),
    )

    if "AWS WAF" not in providers:
        logger.warning("AWS WAF markers not confirmed after Begin click.")
        return {"detected": detect_data}

    payload = await _safe_evaluate(page, extract_script, timeout=15)
    if not isinstance(payload, dict):
        logger.warning("CAPTCHA payload extraction failed: no payload object returned.")
        return {"detected": detect_data}

    if payload.get("error"):
        logger.warning("CAPTCHA payload extraction JS error: %s", payload.get("error"))
        return {"detected": detect_data, "payload": payload}

    logger.info(
        "CAPTCHA payload flags -> key=%s iv=%s context=%s goku=%s",
        bool(payload.get("websiteKey")),
        bool(payload.get("iv")),
        bool(payload.get("context")),
        payload.get("gokuExists", False),
    )

    logger.info("CAPTCHA payload websiteKey: %s", _short_text(payload.get("websiteKey", ""), 320))
    logger.info("CAPTCHA payload iv: %s", _short_text(payload.get("iv", ""), 320))
    logger.info("CAPTCHA payload context: %s", _short_text(payload.get("context", ""), 320))
    logger.info("CAPTCHA payload captchaScript: %s", _short_text(payload.get("captchaScript", ""), 320))
    logger.info("CAPTCHA payload challengeScript: %s", _short_text(payload.get("challengeScript", ""), 320))
    logger.info("CAPTCHA payload jsapiScript: %s", _short_text(payload.get("jsapiScript", ""), 320))

    iframe_srcs = payload.get("iframeSrcs") if isinstance(payload.get("iframeSrcs"), list) else []
    script_srcs = payload.get("scriptSrcs") if isinstance(payload.get("scriptSrcs"), list) else []
    logger.info("CAPTCHA payload iframeSrc sample: %s", [_short_text(x, 220) for x in iframe_srcs[:3]])
    logger.info("CAPTCHA payload scriptSrc sample: %s", [_short_text(x, 220) for x in script_srcs[:3]])

    return {"detected": detect_data, "payload": payload}


async def _attempt_aws_challenge_submit_bridge(page, token, flow_label="primary"):
    """Try common AWS in-page submit callbacks after token injection."""
    bridge_script = f"""
    (async () => {{
        const out = {{
            called: false,
            path: '',
            methods: [],
            saveReferrer: false,
            submitEventDispatched: false,
            error: ''
        }};
        try {{
            const cs = window.ChallengeScript;
            if (cs && typeof cs === 'object') {{
                out.methods = Object.keys(cs).filter(k => typeof cs[k] === 'function').slice(0, 20);
            }}

            if (window.AwsWafIntegration && typeof window.AwsWafIntegration.saveReferrer === 'function') {{
                try {{
                    window.AwsWafIntegration.saveReferrer();
                    out.saveReferrer = true;
                }} catch (e) {{
                    out.saveReferrerError = String(e);
                }}
            }}

            const candidates = [];
            if (cs && typeof cs.submitCaptcha === 'function') candidates.push(['ChallengeScript.submitCaptcha', cs.submitCaptcha.bind(cs)]);
            if (cs && typeof cs.submitToken === 'function') candidates.push(['ChallengeScript.submitToken', cs.submitToken.bind(cs)]);
            if (cs && typeof cs.submit === 'function') candidates.push(['ChallengeScript.submit', cs.submit.bind(cs)]);
            if (cs && typeof cs.verify === 'function') candidates.push(['ChallengeScript.verify', cs.verify.bind(cs)]);

            for (const [name, fn] of candidates) {{
                try {{
                    await fn({json.dumps(token)});
                    out.called = true;
                    out.path = name;
                    break;
                }} catch (e) {{
                    out.lastError = String(e);
                }}
            }}

            if (!out.called && {json.dumps(flow_label)} !== "secondary-download-all") {{
                const form = document.querySelector('form');
                if (form) {{
                    try {{
                        form.dispatchEvent(new Event('submit', {{ bubbles: true, cancelable: true }}));
                        out.submitEventDispatched = true;
                    }} catch (e) {{
                        out.submitEventError = String(e);
                    }}
                }}
            }}
            
            if (!out.called && {json.dumps(flow_label)} === "secondary-download-all") {{
                const form = document.querySelector('form[action*="fileHandlingNEW.cfm"]');
                if (form) {{
                    setTimeout(() => form.submit(), 150);
                    out.submitEventDispatched = true;
                }}
            }}
        }} catch (e) {{
            out.error = String(e);
        }}
        return out;
    }})()
    """
    result = await _safe_evaluate(page, bridge_script, timeout=25)
    logger.info("[STEP] Callback bridge result: %s", result)
    return result


async def _probe_target_fetch(page, target_url):
    probe_script = f"""
    (() => fetch({json.dumps(target_url)}, {{
        method: 'GET',
        credentials: 'include',
        redirect: 'manual'
    }}).then(r => ({{status: r.status, ok: r.ok, type: r.type, url: r.url}})).catch(e => ({{error: String(e)}})))()
    """
    return await _safe_evaluate(page, probe_script, timeout=20)


async def _get_verification_state(page):
    state_script = """
    (() => {
        const text = (document.body?.innerText || '').toLowerCase();
        const hasVerification =
            text.includes("let's confirm you are human") ||
            text.includes('human verification') ||
            !!document.querySelector('#amzn-captcha-verify-button, .amzn-captcha-state-container, [id^="amzn-captcha-"], [class*="amzn-captcha"]');

        return {
            hasVerification,
            title: document.title || '',
            href: location.href || ''
        };
    })()
    """
    return await _safe_evaluate(page, state_script, timeout=8)


def _append_waf_diagnostic(record):
    try:
        payload = dict(record or {})

        def _to_json_safe(value):
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            if isinstance(value, dict):
                return {str(k): _to_json_safe(v) for k, v in value.items()}
            if isinstance(value, (list, tuple, set)):
                return [_to_json_safe(v) for v in value]
            return str(value)

        payload = _to_json_safe(payload)
        payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        with open(WAF_DIAGNOSTIC_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("[DIAG] Could not append WAF diagnostic record: %s", e)


async def _collect_waf_diagnostics(page, token_value=""):
    token_prefix = str(token_value or "")[:24]
    diag_script = rf"""
    (() => {{
        const normalize = (t) => (t || '').replace(/\s+/g, ' ').trim();
        const text = normalize(document.body?.innerText || '');
        const lower = text.toLowerCase();
        const hasVerification =
            lower.includes("let's confirm you are human") ||
            lower.includes('human verification') ||
            !!document.querySelector('#amzn-captcha-verify-button, .amzn-captcha-state-container, [id^="amzn-captcha-"], [class*="amzn-captcha"]');

        const hasBeginButton = !!document.querySelector('#amzn-captcha-verify-button, button.amzn-captcha-verify-button, .amzn-captcha-state-container button');
        const cookieStr = document.cookie || '';
        const cookieNames = cookieStr
            ? cookieStr.split(';').map(x => (x.split('=')[0] || '').trim()).filter(Boolean).slice(0, 40)
            : [];

        const indicators = [
            'verification',
            'human',
            'captcha',
            'token',
            'expired',
            'retry',
            'again',
            'denied',
            'blocked',
            'access'
        ];
        const wafCookiePairs = cookieStr
            ? cookieStr
                .split(';')
                .map(x => x.trim())
                .filter(Boolean)
                .filter(x => x.toLowerCase().startsWith('aws-waf-token=') || x.toLowerCase().startsWith('amazon-waf-token='))
                .map(x => {{
                    const parts = x.split('=');
                    const name = (parts[0] || '').trim();
                    const val = parts.slice(1).join('=');
                    return {{
                        name,
                        valueLen: val.length,
                        valueHint: val ? `${{val.slice(0, 10)}}...${{val.slice(-6)}}` : ''
                    }};
                }})
                .slice(0, 6)
            : [];

        const hintLines = text
            .split(/[.\n]/)
            .map(s => normalize(s))
            .filter(Boolean)
            .filter(line => indicators.some(k => line.toLowerCase().includes(k)))
            .slice(0, 8);

        const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src || '').slice(0, 20);

        return {{
            href: location.href || '',
            title: document.title || '',
            readyState: document.readyState || '',
            hasVerification,
            hasBeginButton,
            hasChallengeScript: scripts.some(s => s.includes('challenge.js')),
            hasCaptchaScript: scripts.some(s => s.includes('captcha.js')),
            cookieLen: cookieStr.length,
            cookieNames,
            wafCookiePairs,
            hasAwsCookieName: cookieNames.includes('aws-waf-token') || cookieNames.includes('amazon-waf-token'),
            hasTokenPrefix: {json.dumps(token_prefix)} ? cookieStr.includes({json.dumps(token_prefix)}) : false,
            bodyHintLines: hintLines,
            textSample: text.slice(0, 400)
        }};
    }})()
    """
    diag = await _safe_evaluate(page, diag_script, timeout=10)
    if isinstance(diag, dict):
        return diag
    return {
        "error": "diagnostics_evaluate_failed",
        "details": _short_text(diag, 500),
    }


async def solve_aws_waf_with_anticaptcha(
    page,
    target_url,
    extract_result,
    flow_label="primary",
    consume_url=None,
    reopen_on_retry=True,
    anticaptcha_task_timeout=240,
    anticaptcha_rotate_on_timeout=False,
    anticaptcha_infinite_retry=False,
    refresh_payload_each_task=False,
):
    """Send extracted AWS WAF payload to Anti-Captcha, inject token, and verify acceptance."""

    payload = (extract_result or {}).get("payload") or {}
    detected = (extract_result or {}).get("detected") or {}

    website_key = payload.get("websiteKey") or ""
    iv = payload.get("iv") or ""
    context = payload.get("context") or ""
    captcha_script = payload.get("captchaScript") or ""
    challenge_script = payload.get("challengeScript") or ""
    jsapi_script = payload.get("jsapiScript") or ""
    website_url = (detected.get("url") or target_url or "").strip()
    consume_target = (consume_url or website_url or target_url or "").strip()
    flow_tag = (flow_label or "waf").upper()

    logger.info("[%s] Preparing Anti-Captcha request for AWS WAF.", flow_tag)
    logger.info(
        "[%s] Required fields present -> key=%s iv=%s context=%s | api_key=%s | website_url=%s | consume_url=%s",
        flow_tag,
        bool(website_key),
        bool(iv),
        bool(context),
        bool(ANTICAPTCHA_KEY),
        _short_text(website_url, 180),
        _short_text(consume_target, 180),
    )

    if not ANTICAPTCHA_KEY:
        logger.error("[%s] ANTICAPTCHA_API_KEY missing in environment.", flow_tag)
        return False
    if not (website_key and iv and context and website_url and consume_target):
        logger.error("[%s] Incomplete AWS payload. Cannot send to Anti-Captcha.", flow_tag)
        return False

    solver = amazonProxyless()
    solver.set_verbose(1)
    solver.set_key(ANTICAPTCHA_KEY)
    solver.set_website_url(website_url)
    solver.set_website_key(website_key)
    solver.set_iv(iv)
    solver.set_context(context)
    if captcha_script:
        solver.set_captcha_script(captcha_script)
    if challenge_script:
        solver.set_challenge_script(challenge_script)
    if jsapi_script:
        solver.set_jsapi_script(jsapi_script)

    task_round = 0
    token = ""
    while True:
        task_round += 1

        if refresh_payload_each_task and task_round > 1:
            if reopen_on_retry:
                import json
                logger.info("[%s] Reloading target URL to generate completely fresh AWS WAF payload...", flow_tag)
                await _safe_evaluate(page, f"window.location.href = {json.dumps(target_url)}", timeout=8)
                await asyncio.sleep(12)  # Wait for page and WAF scripts to fully load

            refreshed = await extract_captcha_payload(page)
            refreshed_payload = (refreshed or {}).get("payload") or {}
            refreshed_detected = (refreshed or {}).get("detected") or {}
            if isinstance(refreshed_payload, dict) and refreshed_payload.get("websiteKey") and refreshed_payload.get("iv") and refreshed_payload.get("context"):
                payload = refreshed_payload
                detected = refreshed_detected
                website_key = payload.get("websiteKey") or website_key
                iv = payload.get("iv") or iv
                context = payload.get("context") or context
                captcha_script = payload.get("captchaScript") or captcha_script
                challenge_script = payload.get("challengeScript") or challenge_script
                jsapi_script = payload.get("jsapiScript") or jsapi_script
                website_url = (detected.get("url") or website_url or target_url or "").strip()
                consume_target = (consume_url or website_url or target_url or "").strip()
                logger.info(
                    "[%s] Refreshed payload for Anti-Captcha round %s. website_url=%s consume_url=%s",
                    flow_tag,
                    task_round,
                    _short_text(website_url, 180),
                    _short_text(consume_target, 180),
                )
            else:
                logger.warning("[%s] Could not refresh payload for round %s. Reusing previous payload.", flow_tag, task_round)

        solver = amazonProxyless()
        solver.set_verbose(1)
        solver.set_key(ANTICAPTCHA_KEY)
        solver.set_website_url(website_url)
        solver.set_website_key(website_key)
        solver.set_iv(iv)
        solver.set_context(context)
        if captcha_script:
            solver.set_captcha_script(captcha_script)
        if challenge_script:
            solver.set_challenge_script(challenge_script)
        if jsapi_script:
            solver.set_jsapi_script(jsapi_script)

        logger.info(
            "[%s] Sending solve request to Anti-Captcha... round=%s timeout=%ss",
            flow_tag,
            task_round,
            anticaptcha_task_timeout,
        )

        task_payload = {
            "type": "AmazonTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": website_key,
            "waf_type": solver.waf_type,
            "iv": iv,
            "context": context,
            "captchaScript": captcha_script,
            "challengeScript": challenge_script,
            "jsapiScript": jsapi_script,
        }

        created = await asyncio.to_thread(
            solver.create_task,
            {
                "clientKey": solver.client_key,
                "task": task_payload,
                "softId": solver.soft_id,
            },
        )
        if created != 1:
            logger.error(
                "[%s] Anti-Captcha createTask failed on round %s. error_code=%s err=%s",
                flow_tag,
                task_round,
                getattr(solver, "error_code", "unknown_error"),
                _short_text(getattr(solver, "err_string", ""), 220),
            )
            _append_waf_diagnostic(
                {
                    "kind": "anticaptcha-create-failed",
                    "flow": flow_label,
                    "round": task_round,
                    "target_url": target_url,
                    "consume_url": consume_target,
                    "website_url": website_url,
                    "error_code": getattr(solver, "error_code", "unknown_error"),
                    "error_string": getattr(solver, "err_string", ""),
                }
            )
            if anticaptcha_infinite_retry:
                logger.info("[%s] Retrying Anti-Captcha task creation in 5 seconds...", flow_tag)
                await asyncio.sleep(5)
                continue
            return False

        logger.info("[%s] Anti-Captcha task created. round=%s task_id=%s", flow_tag, task_round, solver.task_id)

        solve_started = asyncio.get_event_loop().time()
        task_result = await asyncio.to_thread(solver.wait_for_result, int(anticaptcha_task_timeout), 0)
        elapsed = asyncio.get_event_loop().time() - solve_started

        if task_result == 0:
            err_string = getattr(solver, "err_string", "")
            err_code = getattr(solver, "error_code", "")
            logger.warning(
                "[%s] Anti-Captcha task did not complete in round %s after %.1fs. error_code=%s err=%s",
                flow_tag,
                task_round,
                elapsed,
                err_code or "none",
                _short_text(err_string, 220),
            )
            _append_waf_diagnostic(
                {
                    "kind": "anticaptcha-timeout-or-error",
                    "flow": flow_label,
                    "round": task_round,
                    "target_url": target_url,
                    "consume_url": consume_target,
                    "website_url": website_url,
                    "task_id": solver.task_id,
                    "elapsed": elapsed,
                    "error_code": err_code,
                    "error_string": err_string,
                }
            )

            if anticaptcha_rotate_on_timeout or anticaptcha_infinite_retry:
                await asyncio.sleep(1)
                continue
            return False

        token = (task_result.get("solution") or {}).get("token") if isinstance(task_result, dict) else ""
        logger.info("[%s] Anti-Captcha response time: %.1fs", flow_tag, elapsed)
        if token:
            break

        logger.warning("[%s] Anti-Captcha returned no token on round %s. Retrying new task.", flow_tag, task_round)
        _append_waf_diagnostic(
            {
                "kind": "anticaptcha-empty-token",
                "flow": flow_label,
                "round": task_round,
                "target_url": target_url,
                "consume_url": consume_target,
                "website_url": website_url,
                "task_id": solver.task_id,
                "task_result": task_result,
            }
        )
        if not anticaptcha_infinite_retry and not anticaptcha_rotate_on_timeout:
            return False
        await asyncio.sleep(1)

    token_str = str(token)
    token_hint = f"{token_str[:18]}...{token_str[-10:]}" if len(token_str) > 32 else token_str
    logger.info("[%s] Token received from Anti-Captcha. token_length=%s token_hint=%s", flow_tag, len(token_str), token_hint)

    # [FIX] Post-Solve Cookie Purge: Clean the slate IMMEDIATELY before injecting the exact correct token
    # Doing this prior to solve allows WAF scripts to regenerate bad tokens during the 25s Anti-Captcha wait time
    logger.info("[%s] Purging existing WAF cookies prior to injection to prevent duplication.", flow_tag)
    
    js_purge_script = """
    (() => {
        try {
            const targetHost = location.hostname;
            document.cookie = `aws-waf-token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/; domain=${targetHost};`;
            document.cookie = `aws-waf-token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/; domain=.${targetHost};`;
            document.cookie = `amazon-waf-token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/; domain=${targetHost};`;
            document.cookie = `amazon-waf-token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/; domain=.${targetHost};`;
            document.cookie = `aws-waf-token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;`;
            document.cookie = `amazon-waf-token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;`;
            return { purged: true, remains: document.cookie };
        } catch (e) {
            return { purged: false, error: String(e) };
        }
    })()
    """
    js_purge_result = await _safe_evaluate(page, js_purge_script, timeout=4)
    logger.info("[%s] JS purge executed: %s", flow_tag, js_purge_result)

    try:
        # Clear cookies via Playwright context
        await page.context.clear_cookies()
        logger.info("[%s] Cleared Playwright context cookies.", flow_tag)
    except Exception as e:
        logger.warning("[%s] Failed to clear context cookies: %s", flow_tag, e)

    set_results = []
    try:
        # Set token cookies via Playwright context
        pw_cookies = []
        for name in ["aws-waf-token", "amazon-waf-token"]:
            # Playwright context.add_cookies expects a list of cookie objects
            pw_cookies.append({
                "name": name,
                "value": str(token),
                "url": consume_target,
                "path": "/",
                "secure": True,
                "sameSite": "None"
            })
        
        await page.context.add_cookies(pw_cookies)
        set_results = [{"name": c["name"], "ok": True} for c in pw_cookies]
        logger.info("[%s] Token cookies set via Playwright context.", flow_tag)
    except Exception as e:
        logger.error("[%s] Failed to set token cookies via Playwright: %s", flow_tag, e)
        set_results = [{"error": str(e)}]
    logger.info("[%s] Token cookie set results: %s", flow_tag, set_results)

    js_cookie_script = """
    (() => {
        return { href: location.href || '', title: document.title || '', cookieLen: (document.cookie || '').length, targetDomain: location.hostname || '' };
    })()
    """
    js_cookie_result = await _safe_evaluate(page, js_cookie_script, timeout=10)
    logger.info("[%s] Base environment info after CDP cookie injection: %s", flow_tag, js_cookie_result)

    post_inject_diag = await _collect_waf_diagnostics(page, token_str)
    logger.info("[%s] Post-injection diagnostics: %s", flow_tag, post_inject_diag)
    _append_waf_diagnostic(
        {
            "kind": "post-injection",
            "flow": flow_label,
            "target_url": target_url,
            "consume_url": consume_target,
            "website_url": website_url,
            "token_length": len(token_str),
            "token_hint": token_hint,
            "cookie_set_results": set_results,
            "js_cookie_result": js_cookie_result,
            "diagnostics": post_inject_diag,
        }
    )

    final_reason = "unknown"
    last_state = None
    last_bridge_result = None
    last_probe_consume = None
    last_probe_target = None
    last_diag = None

    if flow_label == "secondary-download-all":
        logger.info("[%s] Flow is secondary POST block. Token is injected. Returning early.", flow_tag)
        return True

    for attempt in range(1, 4):
        logger.info("[%s] Token consume attempt %s/3 started. consume_url=%s", flow_tag, attempt, _short_text(consume_target, 180))

        bridge_result = await _attempt_aws_challenge_submit_bridge(page, token_str, flow_label)
        last_bridge_result = bridge_result
        if isinstance(bridge_result, dict) and bridge_result.get("called"):
            logger.info("[%s] Callback executed via %s", flow_tag, bridge_result.get("path", "unknown"))
        else:
            logger.warning("[%s] No direct callback accepted token on attempt %s", flow_tag, attempt)

        probe = await _probe_target_fetch(page, consume_target)
        last_probe_consume = probe
        logger.info("[%s] Post-token consume probe (attempt %s): %s", flow_tag, attempt, probe)

        probe_target = None
        if consume_target != target_url:
            probe_target = await _probe_target_fetch(page, target_url)
            logger.info("[%s] Post-token target probe (attempt %s): %s", flow_tag, attempt, probe_target)
        last_probe_target = probe_target

        state = await _get_verification_state(page)
        last_state = state
        logger.info("[%s] Verification state (attempt %s): %s", flow_tag, attempt, state)

        diag = await _collect_waf_diagnostics(page, token_str)
        last_diag = diag
        logger.info("[%s] Attempt %s diagnostics: %s", flow_tag, attempt, diag)
        _append_waf_diagnostic(
            {
                "kind": "consume-attempt",
                "flow": flow_label,
                "attempt": attempt,
                "target_url": target_url,
                "consume_url": consume_target,
                "state": state,
                "bridge": bridge_result,
                "probe_consume": probe,
                "probe_target": probe_target,
                "diagnostics": diag,
            }
        )

        locked = True
        if isinstance(state, dict):
            locked = bool(state.get("hasVerification"))

        if not locked:
            logger.info("[%s] SecurePortalAlpha accepted token on attempt %s.", flow_tag, attempt)
            return True

        final_reason = "verification_still_visible"
        if isinstance(probe, dict):
            if probe.get("error"):
                final_reason = f"probe_error:{probe.get('error')}"
            elif isinstance(probe.get("status"), int):
                final_reason = f"probe_status:{probe.get('status')}"

        if attempt < 3 and reopen_on_retry:
            import json
            logger.warning("[%s] Token not accepted yet. Reopening consume URL and retrying. reopen_url=%s", flow_tag, _short_text(consume_target, 180))
            await _safe_evaluate(page, f"window.location.href = {json.dumps(consume_target)}", timeout=8)
            await asyncio.sleep(5)

    logger.error(
        "[%s] SecurePortalAlpha did not accept token after retries. reason=%s | consume_url=%s | target_url=%s | last_state=%s | last_bridge=%s | last_probe_consume=%s | last_probe_target=%s | last_diag=%s",
        flow_tag,
        final_reason,
        _short_text(consume_target, 180),
        _short_text(target_url, 180),
        last_state,
        last_bridge_result,
        last_probe_consume,
        last_probe_target,
        last_diag,
    )
    _append_waf_diagnostic(
        {
            "kind": "final-failure",
            "flow": flow_label,
            "reason": final_reason,
            "target_url": target_url,
            "consume_url": consume_target,
            "website_url": website_url,
            "last_state": last_state,
            "last_bridge": last_bridge_result,
            "last_probe_consume": last_probe_consume,
            "last_probe_target": last_probe_target,
            "last_diag": last_diag,
        }
    )
    return False


async def _wait_for_green_signal(page, target_url, timeout=120):
    """Wait until verification is cleared and portal looks ready for next actions."""
    logger.info("[STEP] Waiting for green signal from SecurePortalAlpha (verification cleared + portal ready)...")

    state_script = """
    (() => {
        const text = (document.body?.innerText || '').toLowerCase();
        const href = location.href || '';
        const title = document.title || '';
        const hasVerification =
            text.includes("let's confirm you are human") ||
            text.includes('human verification') ||
            text.includes('complete the security check before continuing') ||
            !!document.querySelector('#amzn-captcha-verify-button, .amzn-captcha-state-container, [id^="amzn-captcha-"], [class*="amzn-captcha"]');
        const hasLoginBtn = !!Array.from(document.querySelectorAll('a, button')).find(el => {
            const t = (el.textContent || '').trim().toLowerCase();
            return t === 'log in' || t === 'login';
        });
        return {
            href,
            title,
            readyState: document.readyState || '',
            hasVerification,
            hasLoginBtn
        };
    })()
    """

    for sec in range(timeout):
        state = await _safe_evaluate(page, state_script, timeout=8)
        if isinstance(state, dict):
            ready = state.get("readyState") in ("complete", "interactive")
            no_verification = not bool(state.get("hasVerification"))
            on_portal = "vendors.portal_alpha.com" in (state.get("href") or "").lower()
            has_login_btn = bool(state.get("hasLoginBtn"))

            if sec % 5 == 0:
                logger.info(
                    "[STEP] Green signal probe t=%ss -> ready=%s readyState=%s no_verification=%s on_portal=%s has_login_btn=%s title=%s",
                    sec,
                    ready,
                    state.get("readyState"),
                    no_verification,
                    on_portal,
                    has_login_btn,
                    _short_text(state.get("title", ""), 120),
                )

            if ready and no_verification and on_portal:
                logger.info("[STEP] Green signal received from SecurePortalAlpha.")
                return True

        await asyncio.sleep(1)

    logger.error("[ISSUE] Green signal wait timed out after %ss.", timeout)
    return False


async def _click_login_button(page):
    click_script = """
    (() => {
        const bySelector = [
            'a[href*="identity/public/oauth/login"]',
            'a[href*="oauth/login"]',
            'button[data-testid*="login"]'
        ];
        for (const sel of bySelector) {
            const el = document.querySelector(sel);
            if (el) {
                el.click();
                return { clicked: true, strategy: `selector:${sel}` };
            }
        }

        const candidates = Array.from(document.querySelectorAll('a, button')).slice(0, 250);
        const loginEl = candidates.find(el => {
            const t = (el.textContent || '').trim().toLowerCase();
            return t === 'log in' || t === 'login';
        });
        if (loginEl) {
            loginEl.click();
            return { clicked: true, strategy: 'text:login' };
        }
        return { clicked: false };
    })()
    """
    result = await _safe_evaluate(page, click_script, timeout=8)
    logger.info("[STEP] Login button click result: %s", result)
    return isinstance(result, dict) and bool(result.get("clicked"))


async def _wait_for_identity_page(page, timeout=40):
    logger.info("[STEP] Waiting for identity login page...")
    check_script = """
    (() => ({
        href: location.href || '',
        title: document.title || '',
        hasEmail: !!document.querySelector('#email, input[type="email"], input[name="email"]'),
        hasPassword: !!document.querySelector('#password, input[type="password"], input[name="password"]')
    }))()
    """
    for sec in range(timeout):
        state = await _safe_evaluate(page, check_script, timeout=6)
        if isinstance(state, dict):
            href = (state.get("href") or "").lower()
            if "identity.portal_alpha.com" in href or (state.get("hasEmail") and state.get("hasPassword")):
                logger.info("[STEP] Identity page detected: %s", _short_text(state.get("href", ""), 180))
                return True
        await asyncio.sleep(1)
    logger.error("[ISSUE] Identity page did not load within timeout.")
    return False


async def _submit_identity_credentials(page):
    email = _normalized_pb_email(PB_EMAIL)
    password = (PB_PASSWORD or "").strip()
    if not email or not password:
        logger.error("[ISSUE] Missing PB_EMAIL or PB_PASSWORD for login automation.")
        return False

    submit_script = f"""
    (() => {{
        const email = {json.dumps(email)};
        const password = {json.dumps(password)};

        const emailEl = document.querySelector('#email, input[type="email"], input[name="email"]');
        const passEl = document.querySelector('#password, input[type="password"], input[name="password"]');
        if (!emailEl || !passEl) {{
            return {{ ok: false, reason: 'inputs_missing', href: location.href }};
        }}

        // Force clear and fill
        emailEl.value = '';
        emailEl.focus();
        emailEl.value = email;
        emailEl.dispatchEvent(new Event('input', {{ bubbles: true }}));
        emailEl.dispatchEvent(new Event('change', {{ bubbles: true }}));

        passEl.value = '';
        passEl.focus();
        passEl.value = password;
        passEl.dispatchEvent(new Event('input', {{ bubbles: true }}));
        passEl.dispatchEvent(new Event('change', {{ bubbles: true }}));

        const submit = document.querySelector('#submitBtn, button[type="submit"], input[type="submit"]');
        if (submit) {{
            // Use native click first
            submit.click();
            
            // Fallback: if still on page after 1s, try form submit
            setTimeout(() => {{
                const form = document.querySelector('form');
                if (form && location.href.includes('identity.portal_alpha.com')) {{
                    form.submit();
                }}
            }}, 1000);

            return {{ ok: true, method: 'click-and-form-fallback', href: location.href || '' }};
        }}

        passEl.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }}));
        return {{ ok: true, method: 'enter-key', href: location.href || '' }};
    }})()
    """
    result = await _safe_evaluate(page, submit_script, timeout=10)
    logger.info("[STEP] Identity credential submit result: %s", result)
    # Wait for the actual navigation to trigger
    await asyncio.sleep(3) 
    return isinstance(result, dict) and bool(result.get("ok"))


async def _wait_for_redirect_back_to_portal(page, target_url, timeout=120):
    logger.info("[STEP] Waiting for redirect back to SecurePortalAlpha portal...")
    for sec in range(timeout):
        state = await _get_portal_documents_state(page)
        if isinstance(state, dict):
            if sec % 5 == 0:
                logger.info(
                    "[STEP] Redirect probe t=%ss -> on_portal=%s identity=%s docs_tab=%s downloads=%s verification=%s href=%s",
                    sec,
                    state.get("onPortalDomain"),
                    state.get("onIdentityDomain"),
                    state.get("hasDocumentsTab"),
                    state.get("downloadButtonsCount"),
                    state.get("hasVerification"),
                    _short_text(state.get("href", ""), 150),
                )

            if (
                state.get("onPortalDomain")
                and not state.get("onIdentityDomain")
                and not state.get("hasVerification")
            ):
                if state.get("hasDocumentsTab") or state.get("hasSubmissionHeading") or state.get("downloadButtonsCount", 0) > 0:
                    logger.info("[STEP] Redirected back to SecurePortalAlpha portal successfully.")
                    return True

        await asyncio.sleep(1)

    # Last chance: force navigation to target URL and re-check one more time.
    logger.warning("[STEP] Redirect timeout reached. Forcing target URL re-open for final state check.")
    await _safe_evaluate(page, f"window.location.href = {json.dumps(target_url)}", timeout=8)
    await asyncio.sleep(5)
    state = await _get_portal_documents_state(page)
    if isinstance(state, dict) and state.get("onPortalDomain") and not state.get("hasVerification"):
        logger.warning("[STEP] Final check succeeded after forced portal reopen.")
        return True

    logger.error("[ISSUE] Redirect back to portal did not complete within timeout.")
    return False


def _sha256_text(value):
    return hashlib.sha256((value or "").encode("utf-8", errors="ignore")).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_dir(path_obj):
    path_obj.mkdir(parents=True, exist_ok=True)
    return path_obj


def _find_file_case_insensitive(root_dir, file_name):
    root = Path(root_dir)
    target = (file_name or "").strip().lower()
    if not target:
        return None

    exact = root / file_name
    if exact.exists() and exact.is_file():
        return exact

    for candidate in root.rglob("*"):
        if candidate.is_file() and candidate.name.lower() == target:
            return candidate
    return None


async def _configure_download_behavior(page, download_dir):
    _ensure_dir(download_dir)
    try:
        # For Playwright, we use the CDP session of the active page
        client = await page.context.new_cdp_session(page)
        await client.send("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": str(download_dir.absolute())
        })
        logger.info("[STEP] Playwright CDP download behavior configured. download_dir=%s", download_dir)
        return True
    except Exception as e:
        logger.warning("[ISSUE] Could not set download behavior via Playwright CDP: %s", e)
        return False


def _snapshot_download_files(download_dir):
    if not download_dir.exists():
        return set()
    return {p.name for p in download_dir.iterdir() if p.is_file()}


async def _click_download_all_button(page):
    click_script = r"""
    (() => {
        const normalize = (t) => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();

        const selectors = [
            '.docs-table-footer .soft-blue-xs-btn',
            'button.soft-blue-xs-btn',
            'button[type="button"]'
        ];

        for (const sel of selectors) {
            const nodes = Array.from(document.querySelectorAll(sel));
            const btn = nodes.find(el => normalize(el.textContent || el.value || '') === 'download all');
            if (btn) {
                btn.scrollIntoView({ block: 'center', inline: 'center' });
                btn.click();
                return { clicked: true, strategy: `selector:${sel}`, text: (btn.textContent || '').trim() };
            }
        }

        const all = Array.from(document.querySelectorAll('button, a')).slice(0, 1200);
        const textBtn = all.find(el => normalize(el.textContent || el.value || '') === 'download all');
        if (textBtn) {
            textBtn.scrollIntoView({ block: 'center', inline: 'center' });
            textBtn.click();
            return { clicked: true, strategy: 'text:download all', text: (textBtn.textContent || '').trim() };
        }

        return { clicked: false };
    })()
    """
    result = await _safe_evaluate(page, click_script, timeout=10)
    logger.info("[STEP] Download All click result: %s", result)
    return result


async def _wait_for_new_zip(page, download_dir, baseline_files, timeout=240):
    logger.info("[STEP] Monitoring download folder for ZIP. timeout=%ss dir=%s", timeout, download_dir)
    start = time.time()
    observed_start = False

    while time.time() - start < timeout:
        # CONTINUOUS VERIFICATION CHECK:
        # If a CAPTCHA appears while we are waiting for the ZIP, we must exit and signal a solve is needed.
        try:
            stage = await _detect_captcha_stage(page)
            state = await _get_verification_state(page)
            
            verification_found = False
            if isinstance(stage, dict) and (stage.get("hasBeginButton") or stage.get("hasCaptchaWidget")):
                verification_found = True
            if isinstance(state, dict) and state.get("hasVerification"):
                verification_found = True
            
            if verification_found:
                logger.warning("[STEP] Secondary verification detected DURING download monitor. Signalling solve-and-retry.")
                return "VERIFICATION_REQUIRED"
        except Exception as e:
            logger.warning("[STEP] Minor error during background verification check: %s", e)

        current_files = _snapshot_download_files(download_dir)
        new_files = current_files - baseline_files

        partials = [n for n in new_files if n.lower().endswith((".crdownload", ".part", ".tmp"))]
        zips = [n for n in new_files if n.lower().endswith(".zip")]

        elapsed = int(time.time() - start)
        if elapsed % 5 == 0:
            logger.info(
                "[STEP] Download monitor t=%ss -> new_files=%s partials=%s zips=%s",
                elapsed,
                len(new_files),
                len(partials),
                len(zips),
            )

        if partials and not observed_start:
            observed_start = True
            logger.info("[STEP] Download appears to have started. partial_files=%s", partials[:5])

        if zips:
            zip_paths = [download_dir / name for name in zips]
            latest = max(zip_paths, key=lambda p: p.stat().st_mtime)

            # Wait for ZIP size stability to avoid reading while browser is still writing.
            size1 = latest.stat().st_size
            await asyncio.sleep(2)
            if not latest.exists():
                continue
            size2 = latest.stat().st_size
            if size1 == size2 and size2 > 0:
                logger.info("[SUCCESS] ZIP download completed: %s (%s bytes)", latest, size2)
                return latest

        await asyncio.sleep(1)

    logger.error("[ISSUE] ZIP download not detected within timeout=%ss", timeout)
    return None


def _extract_zip(zip_path, extract_root):
    _ensure_dir(extract_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = extract_root / f"{Path(zip_path).stem}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(out_dir)

    logger.info("[SUCCESS] ZIP extracted to: %s", out_dir)
    return out_dir


def _save_document_content_json(documents_meta, extracted_dir, record_id, record_is_baseline, url):
    try:
        from document_parser import DocumentParser
    except Exception as e:
        logger.error("[ISSUE] Could not import DocumentParser from document_parser.py: %s", e)
        return None

    supabase_client = get_supabase()
    if not supabase_client:
        logger.error("[ISSUE] Supabase client not initialized. Cannot save to DB.")
        return None

    # Pre-check: does this record already have ANY docs? (used by delta engine)
    record_already_has_docs = check_record_has_docs(supabase_client, record_id)
    documents_inserted = 0
    for item in documents_meta:
        title = str(item.get("title") or "").strip()
        file_name = str(item.get("file_name") or "").strip()
        size = str(item.get("size") or "").strip()
        
        file_path = _find_file_case_insensitive(extracted_dir, file_name)
        extracted_content = ""
        file_hash = ""
        
        if file_path and file_path.exists():
            try:
                extracted_content, file_hash = DocumentParser.process_file(str(file_path))
                extracted_content = (extracted_content or "").replace('\u0000', '')

                result = save_document_with_delta_detection(
                    supabase_client=supabase_client,
                    record_id=record_id,
                    title=title,
                    content_text=extracted_content,
                    content_hash=file_hash,
                    file_path=file_name,
                    download_url=url,
                    record_already_has_docs=record_already_has_docs,
                    local_file_path=str(file_path),
                )
                if result["status"] != "no_change" and result["status"] != "error":
                    documents_inserted += 1
                    # NOTE: Do NOT flip record_already_has_docs here.
                    # It must stay as the pre-loop value for the ENTIRE batch.
                    # First run = False for all files = all Baseline.
                    # Subsequent runs = True for all files = engine compares properly.
            except Exception as e:
                logger.error("[ISSUE] Failed to parse/insert document %s: %s", file_name, e)
        else:
            # TRY FUZZY MATCH IF EXACT FAIL
            logger.info("[STEP] Exact file match failed for '%s'. Trying fuzzy match...", file_name)
            candidate_files = list(Path(extracted_dir).rglob("*"))
            fuzzy_match = None
            import re
            def clean_name(n):
                base = Path(n).stem.lower()
                base = re.sub(r'[^a-z0-9 ]+', ' ', base)
                return ' '.join(base.split())
            target_clean = clean_name(title)
            if not target_clean:
                target_clean = clean_name(file_name)
            for f in candidate_files:
                if not f.is_file(): continue
                cand_clean = clean_name(f.name)
                cand_words = set(cand_clean.split())
                target_words = set(target_clean.split())
                common = cand_words.intersection(target_words)
                cand_text = f.name.lower()
                target_text = file_name.lower()
                no_ext_target = Path(target_text).stem
                if len(common) >= min(3, len(target_words), len(cand_words)) or \
                   target_clean in cand_clean or cand_clean in target_clean or \
                   cand_text in target_text or target_text in cand_text or \
                   no_ext_target in cand_text:
                    fuzzy_match = f
                    break
            if fuzzy_match:
                logger.info("[SUCCESS] Fuzzy match found: %s", fuzzy_match.name)
                try:
                    extracted_content, file_hash = DocumentParser.process_file(str(fuzzy_match))
                    extracted_content = (extracted_content or "").replace('\u0000', '')
                    result = save_document_with_delta_detection(
                        supabase_client=supabase_client,
                        record_id=record_id,
                        title=title,
                        content_text=extracted_content,
                        content_hash=file_hash,
                        file_path=fuzzy_match.name,
                        download_url=url,
                        record_already_has_docs=record_already_has_docs,
                        local_file_path=str(fuzzy_match),
                    )
                    if result["status"] != "no_change" and result["status"] != "error":
                        documents_inserted += 1
                        # NOTE: Do NOT flip record_already_has_docs here either.
                except Exception as e:
                    logger.error("[ISSUE] Failed fuzzy processing: %s", e)
            else:
                logger.warning("[ISSUE] Could not find file for document: %s (Expected: %s)", title, file_name)

    logger.info("[SUCCESS] Inserted %s new documents for record %s", documents_inserted, record_id)
    
    # Cleanup: Delete the local extracted directory and the ZIP file after processing
    try:
        shutil.rmtree(extracted_dir, ignore_errors=True)
        # Try to find and delete the original ZIP as well
        for zip_file in DOWNLOAD_DIR.glob("*.zip"):
            if record_id in zip_file.name or (zip_file.stat().st_mtime > (time.time() - 3600)):
                try:
                    os.remove(zip_file)
                    logger.info("[CLEANUP] Deleted ZIP file: %s", zip_file.name)
                except:
                    pass
        logger.info("[CLEANUP] Deleted local extraction directory: %s", extracted_dir)
    except Exception as cleanup_err:
        logger.warning("[WARNING] Cleanup failed for %s: %s", extracted_dir, cleanup_err)

    return True


async def _handle_download_all_with_second_waf(context, page, target_url, documents_meta, record_id, record_is_baseline):
    logger.info("[STEP] Starting Download-All workflow with detailed monitoring.")

    await _configure_download_behavior(page, DOWNLOAD_DIR)
    baseline_files = _snapshot_download_files(DOWNLOAD_DIR)

    for attempt in range(1, 4):
        logger.info("[STEP] Download-All attempt %s/3.", attempt)

        pre_attempt_state = await _get_verification_state(page)
        pre_attempt_href = ""
        if isinstance(pre_attempt_state, dict):
            pre_attempt_href = str(pre_attempt_state.get("href") or "")
        
        # In Playwright, page.url is a property
        current_browser_url = page.url
        
        if pre_attempt_href and "vendors.portal_alpha.com" not in pre_attempt_href.lower():
            logger.warning(
                "[STEP] Pre-attempt context is not portal (%s). Returning to target URL before click flow.",
                _short_text(pre_attempt_href, 180),
            )
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            # Wait securely for portal and documents tab to become available
            for _ in range(15):
                st = await _get_portal_documents_state(page)
                if isinstance(st, dict) and st.get("hasDocumentsTab"):
                    break
                await asyncio.sleep(2)
            await asyncio.sleep(2)

        # Retry clicking documents tab a few times if Download All is not immediately ready
        click_result = {"clicked": False}
        zip_result = None
        for doc_tab_retry in range(3):
            doc_click = await _click_documents_tab(page)
            if isinstance(doc_click, dict) and doc_click.get("clicked"):
                logger.info("[STEP] Documents tab clicked successfully.")
            await asyncio.sleep(3)
            
            # --- START NATIVE DOWNLOAD ---
            try:
                async with page.expect_download(timeout=30000) as download_info:
                    click_result = await _click_download_all_button(page)
                
                if isinstance(click_result, dict) and click_result.get("clicked"):
                    download = await download_info.value
                    zip_path = DOWNLOAD_DIR / download.suggested_filename
                    await download.save_as(str(zip_path))
                    logger.info("[STEP] Playwright native download successful: %s", zip_path)
                    zip_result = zip_path
                    break
            except Exception as e:
                logger.warning("[STEP] Download expect timed out or failed: %s", e)
                if isinstance(click_result, dict) and click_result.get("clicked"):
                    logger.info("[STEP] Click was registered, breaking to check for CAPTCHA gates.")
                    break
            
            logger.warning("[STEP] Download All not found yet. Retrying tab click...")

        if not (isinstance(click_result, dict) and click_result.get("clicked")):
            logger.warning("[ISSUE] Could not click Download All on attempt %s.", attempt)
            await asyncio.sleep(2)
            continue

        logger.info("[STEP] Download All triggered. Monitoring for verification gate...")
        await asyncio.sleep(3)

        stage = await _detect_captcha_stage(page)
        state = await _get_verification_state(page)
        current_href = (state or {}).get("href") or ""

        stage_title = (stage or {}).get("title") or ""
        stage_href = (stage or {}).get("href") or ""

        verification_needed = False
        if isinstance(stage, dict) and (stage.get("hasBeginButton") or stage.get("hasCaptchaWidget")):
            verification_needed = True
        if isinstance(state, dict) and state.get("hasVerification"):
            verification_needed = True
        if "captcha.awswaf.com" in current_href.lower() or "human-verification" in current_href.lower():
            verification_needed = True
        if "human verification" in stage_title.lower():
            verification_needed = True
        if "filehandlingnew.cfm" in stage_href.lower() or "files-prod" in stage_href.lower():
            verification_needed = True

        logger.info(
            "[STEP] Verification gate status -> needed=%s stage=%s state=%s",
            verification_needed,
            stage,
            state,
        )

        if verification_needed:
            logger.info("[STEP] Secondary verification detected after Download All. Running Begin -> Solve pipeline.")

            consume_url = current_href or stage_href or target_url
            logger.info("[STEP] Secondary verification context URL: %s", _short_text(consume_url, 220))

            solved = False
            for solve_round in range(1, 2):
                logger.info("[STEP] Secondary WAF Solve Inner Loop Round %s/1", solve_round)
                
                begin_clicked = await click_begin_if_present(page, max_attempts=15, delay_seconds=3)
                logger.info("[STEP] Secondary Begin click result: %s", begin_clicked)
                
                await asyncio.sleep(12)
                extract_result = await extract_captcha_payload(page)
                logger.info("[STEP] Secondary CAPTCHA extraction completed. has_payload=%s", bool((extract_result or {}).get("payload")))

                if not extract_result or not isinstance(extract_result.get("payload"), dict):
                    logger.error("[ISSUE] Secondary CAPTCHA payload missing. Cannot continue solve.")
                    continue

                round_solved = await solve_aws_waf_with_anticaptcha(
                    page,
                    target_url,
                    extract_result,
                    flow_label="secondary-download-all",
                    consume_url=consume_url,
                    reopen_on_retry=False,
                    anticaptcha_task_timeout=60,
                    anticaptcha_rotate_on_timeout=True,
                    anticaptcha_infinite_retry=True,
                    refresh_payload_each_task=True,
                )
                logger.info("[STEP] Inner loop Anti-Captcha solve result (round %s): %s", solve_round, round_solved)
                
                if round_solved:
                    solved = True
                    break
                else:
                    logger.warning("[STEP] The WAF token was rejected. Doing full token reset.")

            if not solved:
                logger.error("[ISSUE] Secondary Anti-Captcha solve completely failed.")
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(3)
                continue

            if solved:
                logger.info("[STEP] Secondary CAPTCHA solved. Retrying the target flow from portal.")
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                for _ in range(15):
                    st = await _get_portal_documents_state(page)
                    if isinstance(st, dict) and st.get("hasDocumentsTab"):
                        break
                    await asyncio.sleep(2)
                await asyncio.sleep(2)
                continue

        if not zip_result:
            zip_result = await _wait_for_new_zip(page, DOWNLOAD_DIR, baseline_files, timeout=ZIP_WAIT_TIMEOUT)
        
        if zip_result == "VERIFICATION_REQUIRED":
            logger.info("[STEP] Late verification gate detected. Forcing solve-and-retry flow.")
            state = await _get_verification_state(page)
            consume_url = (state or {}).get("href") or page.url or target_url
            
            solved = False
            for solve_round in range(1, 2):
                logger.info("[STEP] Late WAF Solve Inner Loop Round %s/1", solve_round)
                await click_begin_if_present(page, max_attempts=15, delay_seconds=3)
                await asyncio.sleep(12)
                extract_result = await extract_captcha_payload(page)
                if not extract_result or not isinstance(extract_result.get("payload"), dict):
                    continue
                round_solved = await solve_aws_waf_with_anticaptcha(
                    page, target_url, extract_result,
                    flow_label="late-monitor-solve",
                    consume_url=consume_url,
                    reopen_on_retry=False,
                    anticaptcha_task_timeout=60,
                )
                if round_solved:
                    solved = True
                    break
            
            if solved:
                logger.info("[STEP] Late CAPTCHA solved. Returning to portal to retry click.")
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(5)
                continue 
            else:
                logger.error("[ISSUE] Late CAPTCHA solve failed.")
                continue

        if zip_result and isinstance(zip_result, Path):
            extracted_dir = _extract_zip(zip_result, EXTRACT_DIR)
            _save_document_content_json(documents_meta, extracted_dir, record_id, record_is_baseline, target_url)
            return True

        logger.warning("[ISSUE] ZIP not found on attempt %s. Retrying full Download-All flow.", attempt)

    logger.error("[ISSUE] Download-All workflow failed after retries.")
    return False


async def _get_portal_documents_state(page):
    state_script = r"""
    (() => {
        const href = (location.href || '').toLowerCase();
        const text = (document.body?.innerText || '').toLowerCase();
        const nodes = Array.from(document.querySelectorAll('a, button, [role="tab"]')).slice(0, 600);
        const normalize = (t) => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();

        const hasDocumentsTab = nodes.some(el => normalize(el.textContent) === 'documents');
        const hasLoginBtn = nodes.some(el => {
            const t = normalize(el.textContent);
            return t === 'log in' || t === 'login' || t === 'sign in';
        });

        const buttons = Array.from(document.querySelectorAll('button, a')).slice(0, 1200);
        let downloadButtonsCount = 0;
        let viewButtonsCount = 0;
        for (const el of buttons) {
            const t = normalize(el.textContent || el.value || '');
            if (t === 'download' || t.startsWith('download ')) downloadButtonsCount += 1;
            if (t === 'view' || t.startsWith('view ')) viewButtonsCount += 1;
        }

        const docRows = Array.from(document.querySelectorAll('tr')).filter(tr => {
            const tdCount = tr.querySelectorAll('td').length;
            const rowText = normalize(tr.innerText);
            return tdCount >= 2 && (rowText.includes('download') || rowText.includes('attachment') || rowText.includes('.pdf') || rowText.includes('.doc') || rowText.includes('.xls'));
        }).length;

        const hasVerification =
            text.includes("let's confirm you are human") ||
            text.includes('human verification') ||
            text.includes('complete the security check before continuing') ||
            !!document.querySelector('#amzn-captcha-verify-button, .amzn-captcha-state-container, [id^="amzn-captcha-"], [class*="amzn-captcha"]');

        // Detect blank page: app rendered if there's visible text beyond just script tags
        const bodyText = (document.body?.innerText || '').trim();
        const bodyHTML = (document.body?.innerHTML || '');
        // A blank/unrendered SPA page only has <script> tags and no visible text
        const isBlankPage = bodyText.length < 50 && bodyHTML.includes('<script');

        return {
            href: location.href || '',
            title: document.title || '',
            onPortalDomain: href.includes('vendors.portal_alpha.com'),
            onIdentityDomain: href.includes('identity.portal_alpha.com'),
            hasDocumentsTab,
            hasLoginBtn,
            hasVerification,
            hasSubmissionHeading: !!document.querySelector('h1, h2, .submission-title'),
            downloadButtonsCount,
            viewButtonsCount,
            documentRowsCount: docRows,
            readyState: document.readyState || '',
            isBlankPage,
            bodyTextLength: bodyText.length
        };
    })()
    """
    return await _safe_evaluate(page, state_script, timeout=8)


def _is_strong_portal_state(state):
    if not isinstance(state, dict):
        return False
    return (
        bool(state.get("onPortalDomain"))
        and not bool(state.get("hasVerification"))
        and bool(state.get("hasDocumentsTab"))
        and int(state.get("downloadButtonsCount") or 0) > 0
    )


async def _get_auth_ui_signals(page):
    script = r"""
    (() => {
        const normalize = (t) => (t || '').replace(/\s+/g, ' ').trim().toLowerCase();
        const items = Array.from(document.querySelectorAll('a, button, [role="button"], [role="menuitem"]')).slice(0, 800);
        const texts = items.map(el => normalize(el.textContent || el.value || ''));
        const hasLogout = texts.some(t => t === 'log out' || t === 'logout' || t.includes('sign out'));
        const hasMyAccount = texts.some(t => t === 'my account' || t.includes('vendor profile home'));
        const hasUserDropdown = !!document.querySelector('.user-dropdown, [class*="user-dropdown"], [data-testid*="user"], [aria-label*="user"]');
        const hasIdentityInputs = !!document.querySelector('#email, input[type="email"], #password, input[type="password"]');

        return {
            hasLogout,
            hasMyAccount,
            hasUserDropdown,
            hasIdentityInputs,
        };
    })()
    """
    return await _safe_evaluate(page, script, timeout=8)


def _auth_confidence_score(portal_state, ui_signals):
    score = 0
    signals = []

    if isinstance(portal_state, dict):
        if portal_state.get("onPortalDomain"):
            score += 2
            signals.append("onPortalDomain")

        # ── BLANK PAGE CHECK: SPA failed to render → NOT authenticated ──
        if portal_state.get("isBlankPage"):
            score -= 10
            signals.append(f"blankPage(bodyLen={portal_state.get('bodyTextLength', 0)})")
            logger.warning("[AUTH] Blank page detected — SPA not rendered. bodyTextLength=%s",
                           portal_state.get('bodyTextLength', 0))

        # Check title for verification signals
        title = portal_state.get("title", "").lower()
        if "human verification" in title or "confirm you are human" in title:
            score -= 10
            signals.append("verificationDetectedViaTitle")

        if not portal_state.get("hasVerification"):
            score += 2
            signals.append("noVerification")
        if portal_state.get("hasDocumentsTab"):
            score += 1
            signals.append("hasDocumentsTab")
        if portal_state.get("hasSubmissionHeading"):
            score += 1
            signals.append("hasSubmissionHeading")
        if int(portal_state.get("downloadButtonsCount") or 0) > 0:
            score += 1
            signals.append("hasDownloadButtons")
        if portal_state.get("hasLoginBtn"):
            score -= 4
            signals.append("hasLoginBtn")

    if isinstance(ui_signals, dict):
        if ui_signals.get("hasLogout"):
            score += 2
            signals.append("hasLogout")
        if ui_signals.get("hasMyAccount"):
            score += 1
            signals.append("hasMyAccount")
        if ui_signals.get("hasUserDropdown"):
            score += 1
            signals.append("hasUserDropdown")
        if ui_signals.get("hasIdentityInputs"):
            score -= 3
            signals.append("hasIdentityInputs")

    return score, signals


async def _validate_portal_authentication(
    page,
    stage_label="auth-check",
    max_probes=3,
    probe_delay=2,
    min_score=5,
):
    """Bounded authentication validation with confidence scoring and anti-stuck fail-open."""
    stable_docs_hits = 0
    last_state = {}
    last_ui = {}
    last_score = 0
    last_signals = []

    for probe in range(1, max_probes + 1):
        portal_state = await _get_portal_documents_state(page)
        ui_signals = await _get_auth_ui_signals(page)

        last_state = portal_state if isinstance(portal_state, dict) else {}
        last_ui = ui_signals if isinstance(ui_signals, dict) else {}

        score, active_signals = _auth_confidence_score(last_state, last_ui)
        last_score = score
        last_signals = active_signals

        strong_portal = _is_strong_portal_state(last_state)
        near_threshold = score >= (min_score - 1)

        if strong_portal and near_threshold:
            stable_docs_hits += 1
        else:
            stable_docs_hits = 0

        login_button_visible = bool(last_state.get("hasLoginBtn"))
        logged_in_ui = bool(last_ui.get("hasLogout")) or bool(last_ui.get("hasMyAccount"))

        hard_block = (
            bool(last_ui.get("hasIdentityInputs"))
            or bool(last_state.get("hasVerification"))
            or (login_button_visible and not logged_in_ui)
        )
        authenticated = False
        reason = ""

        if score >= min_score and not hard_block:
            authenticated = True
            reason = "confidence-threshold"
        elif stable_docs_hits >= 2 and not hard_block:
            authenticated = True
            reason = "near-threshold-stable-docs"

        logger.info(
            "[STEP] Auth probe %s/%s (%s) -> score=%s authenticated=%s stable_docs_hits=%s signals=%s",
            probe,
            max_probes,
            stage_label,
            score,
            authenticated,
            stable_docs_hits,
            active_signals,
        )

        if authenticated:
            return {
                "authenticated": True,
                "score": score,
                "reason": reason,
                "signals": active_signals,
                "portal_state": last_state,
                "ui_signals": last_ui,
            }

        if probe < max_probes:
            await asyncio.sleep(probe_delay)

    return {
        "authenticated": False,
        "score": last_score,
        "reason": "confidence-failed",
        "signals": last_signals,
        "portal_state": last_state,
        "ui_signals": last_ui,
    }


async def _click_documents_tab(page):
    click_script = r"""
    (() => {
        const bySelector = [
            'a[href*="#submissionDocs"]',
            'a[href*="submissionDocs"]',
            'button[aria-controls*="doc"]'
        ];
        for (const sel of bySelector) {
            const el = document.querySelector(sel);
            if (el) {
                el.click();
                return { clicked: true, strategy: `selector:${sel}` };
            }
        }

        const candidates = Array.from(document.querySelectorAll('a, button, [role="tab"]')).slice(0, 400);
        const tab = candidates.find(el => ((el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase()) === 'documents');
        if (tab) {
            tab.click();
            return { clicked: true, strategy: 'text:documents' };
        }
        return { clicked: false };
    })()
    """
    result = await _safe_evaluate(page, click_script, timeout=10)
    logger.info("[STEP] Documents tab click result: %s", result)
    return result


async def extract_documents_audit(page, target_url):
    """Capture complete document-page audit data, log it, and save to JSON with hashes."""
    logger.info("[AUDIT] Starting documents-page audit extraction...")

    await _click_documents_tab(page)
    await asyncio.sleep(3)

    extract_script = r"""
    (() => {
        const normalize = (t) => (t || '').replace(/\s+/g, ' ').trim();
        const normalizeLower = (t) => normalize(t).toLowerCase();

        const tabs = Array.from(document.querySelectorAll('a, button, [role="tab"]')).slice(0, 400)
            .map(el => normalize(el.textContent))
            .filter(Boolean);

        const headings = Array.from(document.querySelectorAll('h1, h2, h3')).slice(0, 40)
            .map(el => normalize(el.textContent))
            .filter(Boolean);

        const scripts = Array.from(document.querySelectorAll('script')).slice(0, 400).map((s, idx) => ({
            index: idx,
            src: s.src || '',
            type: s.type || '',
            inlineLength: s.src ? 0 : (s.textContent || '').length,
            inlineSample: s.src ? '' : (s.textContent || '').slice(0, 500)
        }));

        const hiddenInputs = Array.from(document.querySelectorAll('input[type="hidden"]')).slice(0, 300).map(i => ({
            name: i.name || '',
            id: i.id || '',
            valueSample: (i.value || '').slice(0, 180)
        }));

        const buttons = Array.from(document.querySelectorAll('button, a')).slice(0, 1500).map((el, idx) => {
            const text = normalize(el.textContent || el.value || '');
            const lower = text.toLowerCase();
            return {
                index: idx,
                text,
                tag: (el.tagName || '').toLowerCase(),
                id: el.id || '',
                className: el.className || '',
                href: el.href || '',
                type: el.type || '',
                onclick: el.getAttribute('onclick') || '',
                dataAction: el.getAttribute('data-action') || '',
                dataTestId: el.getAttribute('data-testid') || '',
                isDownload: lower === 'download' || lower.startsWith('download '),
                isView: lower === 'view' || lower.startsWith('view ')
            };
        });

        const rows = Array.from(document.querySelectorAll('tr')).slice(0, 1200).map((tr, idx) => {
            const cells = Array.from(tr.querySelectorAll('td')).map(td => normalize(td.innerText));
            const actionButtons = Array.from(tr.querySelectorAll('button, a')).map(el => ({
                text: normalize(el.textContent || el.value || ''),
                href: el.href || '',
                onclick: el.getAttribute('onclick') || '',
                className: el.className || ''
            }));
            return {
                index: idx,
                cells,
                rowText: normalize(tr.innerText),
                actionButtons
            };
        }).filter(r => r.cells.length > 0 || r.actionButtons.length > 0);

        return {
            page: {
                title: document.title || '',
                href: location.href || '',
                readyState: document.readyState || '',
                bodyTextSample: normalize((document.body?.innerText || '').slice(0, 9000)),
                bodyHtmlSample: (document.body?.innerHTML || '').slice(0, 30000)
            },
            tabs,
            headings,
            scripts,
            hiddenInputs,
            buttons,
            rows
        };
    })()
    """

    raw = await _safe_evaluate(page, extract_script, timeout=35)
    if not isinstance(raw, dict):
        logger.error("[AUDIT] Failed to extract documents audit payload from page JS.")
        return None

    rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
    buttons = raw.get("buttons") if isinstance(raw.get("buttons"), list) else []
    scripts = raw.get("scripts") if isinstance(raw.get("scripts"), list) else []

    documents = []
    for row in rows:
        cells = row.get("cells") if isinstance(row.get("cells"), list) else []
        action_buttons = row.get("actionButtons") if isinstance(row.get("actionButtons"), list) else []
        action_texts = [str(btn.get("text") or "").strip() for btn in action_buttons if isinstance(btn, dict)]
        action_texts = [x for x in action_texts if x]
        lower_actions = [x.lower() for x in action_texts]

        has_download_or_view = any(("download" in a) or ("view" in a) for a in lower_actions)
        if not has_download_or_view and len(cells) < 2:
            continue

        title = str(cells[0]).strip() if len(cells) > 0 else ""
        file_name = str(cells[1]).strip() if len(cells) > 1 else ""
        size = str(cells[2]).strip() if len(cells) > 2 else ""
        row_text = str(row.get("rowText") or "").strip()

        doc_fingerprint = "|".join([title, file_name, size, row_text, ",".join(action_texts)])
        documents.append(
            {
                "title": title,
                "file_name": file_name,
                "size": size,
                "actions": action_texts,
                "row_text": row_text,
                "hash_sha256": _sha256_text(doc_fingerprint),
            }
        )

    # Keep only requested document metadata in audit JSON.
    documents_minimal = [
        {
            "title": doc.get("title", ""),
            "file_name": doc.get("file_name", ""),
            "size": doc.get("size", ""),
            "hash_sha256": doc.get("hash_sha256", ""),
        }
        for doc in documents
    ]

    audit = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_url": target_url,
        "summary": {
            "documents_count": len(documents_minimal),
            "all_buttons_count": len(buttons),
            "scripts_count": len(scripts),
            "rows_count": len(rows),
        },
        "documents": documents_minimal,
    }

    with open(DOCUMENT_AUDIT_FILE, "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=2, ensure_ascii=False)

    logger.info(
        "[AUDIT] Saved %s with documents=%s scripts=%s",
        DOCUMENT_AUDIT_FILE,
        len(documents_minimal),
        len(scripts),
    )

    for idx, doc in enumerate(documents_minimal[:25], start=1):
        logger.info(
            "[DOC-%s] title=%s | file=%s | size=%s | hash=%s",
            idx,
            _short_text(doc.get("title", ""), 120),
            _short_text(doc.get("file_name", ""), 140),
            _short_text(doc.get("size", ""), 40),
            _short_text(doc.get("hash_sha256", ""), 16),
        )

    return audit


async def perform_portal_login_after_unblock(page, target_url):
    """After token acceptance, do resilient login flow and stop once portal is reachable."""
    logger.info("[STEP] Starting post-unblock login workflow.")

    green = await _wait_for_green_signal(page, target_url, timeout=120)
    if not green:
        return False

    # Always run strict auth validation first. Do not enter documents stage until this login workflow confirms auth.
    start_state = await _get_portal_documents_state(page)
    logger.info("[STEP] Initial portal state: %s", start_state)
    start_auth = await _validate_portal_authentication(
        page,
        stage_label="post-unblock-start",
        max_probes=2,
        probe_delay=2,
        min_score=5,
    )
    if start_auth.get("authenticated"):
        logger.info("[STEP] Existing authenticated session confirmed by login workflow.")
        return True

    logger.info("[STEP] Authentication not confirmed yet. Executing explicit identity login steps.")

    for attempt in range(1, 4):
        logger.info("[STEP] Portal login attempt %s/3.", attempt)

        clicked = await _click_login_button(page)
        if not clicked:
            logger.warning("[ISSUE] Login button click failed on attempt %s.", attempt)
            auth_result = await _validate_portal_authentication(
                page,
                stage_label="post-unblock-click-fallback",
                max_probes=2,
                probe_delay=2,
                min_score=5,
            )
            if auth_result.get("authenticated"):
                logger.info("[STEP] Auth confirmed after failed click; login workflow complete.")
                return True
            await asyncio.sleep(2)
            continue

        identity_loaded = await _wait_for_identity_page(page, timeout=45)
        if not identity_loaded:
            auth_result = await _validate_portal_authentication(
                page,
                stage_label="post-unblock-identity-fallback",
                max_probes=2,
                probe_delay=2,
                min_score=5,
            )
            if auth_result.get("authenticated"):
                logger.info("[STEP] Identity page skipped and portal auth is valid; proceeding.")
                return True
            logger.warning("[ISSUE] Identity page not loaded on attempt %s.", attempt)
            await asyncio.sleep(2)
            continue

        submitted = await _submit_identity_credentials(page)
        if not submitted:
            logger.warning("[ISSUE] Could not submit identity credentials on attempt %s.", attempt)
            await asyncio.sleep(2)
            continue

        redirected = await _wait_for_redirect_back_to_portal(page, target_url, timeout=120)
        if redirected:
            auth_result = await _validate_portal_authentication(
                page,
                stage_label="post-unblock-redirect",
                max_probes=2,
                probe_delay=2,
                min_score=5,
            )
            if auth_result.get("authenticated"):
                logger.info("[SUCCESS] Post-unblock login workflow completed.")
                return True
            logger.warning("[ISSUE] Redirect completed but auth confidence stayed low; continuing retry.")

        auth_result = await _validate_portal_authentication(
            page,
            stage_label="post-unblock-redirect-fallback",
            max_probes=2,
            probe_delay=2,
            min_score=5,
        )
        if auth_result.get("authenticated"):
            logger.warning("[STEP] Redirect watcher timed out but portal auth is valid. Proceeding.")
            return True

        logger.warning("[ISSUE] Redirect failed after submit on attempt %s. Retrying...", attempt)
        await asyncio.sleep(2)

    logger.error("[ISSUE] Post-unblock login workflow failed after retries.")
    return False


async def _execute_documents_pipeline(context, page, target_url, record_id, record_is_baseline):
    logger.info("[STEP] Starting documents pipeline (audit -> download all -> zip extract -> content json).")

    pre_state = await _get_portal_documents_state(page)
    auth_result = await _validate_portal_authentication(
        page,
        stage_label="documents-pipeline-entry",
        max_probes=2,
        probe_delay=2,
        min_score=5,
    )
    logger.info(
        "[STEP] Documents pipeline entry -> authenticated=%s score=%s strong_portal=%s",
        bool(auth_result.get("authenticated")),
        auth_result.get("score"),
        _is_strong_portal_state(pre_state),
    )
    if not auth_result.get("authenticated"):
        logger.error("[ISSUE] Authentication not confirmed at documents pipeline entry. Skipping documents stage.")
        return False

    audit = await extract_documents_audit(page, target_url)
    if not isinstance(audit, dict):
        logger.error("[ISSUE] Documents audit could not be created.")
        return False

    documents = audit.get("documents") if isinstance(audit.get("documents"), list) else []
    logger.info("[STEP] Documents discovered for processing: %s", len(documents))
    if not documents:
        logger.error("[ISSUE] No documents found in audit. Download pipeline skipped.")
        return False

    download_ok = await _handle_download_all_with_second_waf(context, page, target_url, documents, record_id, record_is_baseline)
    logger.info("[RESULT] Documents download/content pipeline finished. success=%s", download_ok)
    return download_ok


async def _execute_addenda_emails_pipeline(context, page, target_url, record_id, record_is_baseline):
    logger.info("[STEP] Starting Addenda/Emails pipeline (Extracting Email History).")
    
    js_click_tab = '''
    (() => {
        const tab = document.querySelector('li.submissionAddendaAndEmails button');
        if (tab) {
            tab.click();
            return true;
        }
        return false;
    })()
    '''
    clicked = await _safe_evaluate(page, js_click_tab)
    if not clicked:
        logger.warning("[ISSUE] Could not find Addenda/Emails tab. Skipping addenda.")
        return False
        
    await asyncio.sleep(4)
    
    js_expand_rows = '''
    (() => {
        const expandBtns = document.querySelectorAll('.ant-collapse-header, button[aria-expanded="false"], .fa-chevron-down, .fa-angle-down');
        let count = 0;
        expandBtns.forEach(btn => {
            try { btn.click(); count++; } catch(e){}
        });
        return count;
    })();
    '''
    expanded_count = await _safe_evaluate(page, js_expand_rows)
    logger.info("[STEP] Expanded %s addenda rows.", expanded_count)
    await asyncio.sleep(2)

    logger.info("[STEP] Gather Addenda/Email History text.")
    # Advanced logic to scrape entire structured history text from the tab
    js_extract_emails = r'''
    (() => {
        try {
            let textFound = "";
            
            // Strategy 1: Look for specific active tab panes first (Bootstrap/Antd)
            let tabPanes = Array.from(document.querySelectorAll('.ant-tabs-tabpane-active, .tab-pane.active, div[role="tabpanel"][aria-hidden="false"]'));
            
            // Strategy 2: If none, look for addenda specific wrapper
            if (tabPanes.length === 0) {
                tabPanes = Array.from(document.querySelectorAll('.submissionAddendaAndEmails-body, .ant-table-wrapper, #addenda-summary, ag-grid-angular'));
            }
            
            // Strategy 3: Last resort, try to grab everything in the main view wrapper to not miss anything
            if (tabPanes.length === 0) {
                tabPanes = Array.from(document.querySelectorAll('.project-main-content, .layout-content, main, body'));
                // Filter out standard navbars/footers if possible, but better to get everything than nothing
            }

            tabPanes.forEach(pane => {
                if (pane && pane.innerText) {
                    textFound += pane.innerText + "\n\n";
                }
            });
            
            // Minimal cleanup to return something useful
            if (textFound) {
                textFound = textFound.replace(/\n{3,}/g, '\n\n').trim();
            }

            return textFound || "NO_TEXT_FOUND_IN_TAB_ABSOLUTE_FALLBACK";
        } catch (e) {
            return "JS_ERROR: " + e.message;
        }
    })()
    '''
    email_text = await _safe_evaluate(page, js_extract_emails)
    
    if email_text and len(str(email_text).strip()) > 30:
        cleaned_text = str(email_text).strip().replace('\u0000', '')
        email_hash = hashlib.sha256(cleaned_text.encode('utf-8', errors='ignore')).hexdigest()
        logger.info("[STEP] Extracted Email History Text (%s chars, hash: %s).", len(cleaned_text), email_hash)
        
        supabase_client = get_supabase()
        if supabase_client:
            try:
                result = save_document_with_delta_detection(
                    supabase_client=supabase_client,
                    record_id=record_id,
                    title="Addenda & Email History Notifications",
                    content_text=cleaned_text,
                    content_hash=email_hash,
                    file_path="addenda_email_history_extracted.txt",
                    download_url=target_url,
                    record_already_has_docs=not record_is_baseline,
                )
                logger.info("[DB] Email History save result: %s", result["status"])
            except Exception as e:
                logger.error("[ISSUE] Failed saving email history as document: %s", e)
                
            try:
                supabase_client.table("records").update({"email_history_text": cleaned_text, "email_history_hash": email_hash}).eq("id", record_id).execute()
                logger.info("[DB UPDATE] Updated newly requested `email_history_text` column on records table.")
            except Exception as e:
                logger.warning("[WARNING] Could not update `records.email_history_text` (Does the column exist?): %s", e)
    else:
        logger.info("[STEP] No substantial Addenda/Email History text found.")
        
    return True


async def _process_single_record(context, page, record, supabase_client):
    url = record.get("url")
    record_id = record.get("id")
    if not url or not record_id:
        return

    # ZERO-FAILURE AMENDMENT CHECK: Check if record has existing documents
    docs_run = supabase_client.table("documents").select("id").eq("record_id", record_id).limit(1).execute()
    record_is_baseline = len(docs_run.data) == 0

    # =========================================================
    # COOKIE INJECTION: Load saved session from Supabase
    # This bypasses AWS WAF blank page by reusing a real browser session
    # =========================================================
    try:
        session_res = supabase_client.table("portal_sessions") \
            .select("cookies") \
            .eq("portal", "portal_alpha") \
            .limit(1) \
            .execute()
        if session_res.data and session_res.data[0].get("cookies"):
            raw_payload = session_res.data[0]["cookies"]
            payload = raw_payload if isinstance(raw_payload, dict) else __import__("json").loads(raw_payload)
            
            cookies = payload.get("cookies", [])
            ls_data = payload.get("localStorage", {})
            ss_data = payload.get("sessionStorage", {})

            if cookies:
                logger.info("[SESSION] Found full session data. Injecting Cookies...")
                pw_cookies = []
                for c in cookies:
                    domain = c.get("domain") or ".vendors.portal_alpha.com"
                    if not domain.startswith("."): domain = "." + domain
                    pw_cookies.append({
                        "name": c.get("name"),
                        "value": c.get("value"),
                        "domain": domain,
                        "path": c.get("path") or "/",
                        "secure": True,
                        "sameSite": "None"
                    })
                await context.add_cookies(pw_cookies)
                
                # Injection of Storage happens after navigation
                record["_localStorage"] = ls_data
                record["_sessionStorage"] = ss_data
                logger.info("[SESSION] ✅ Cookies injected via Playwright context.")
    except Exception as e:
        logger.warning("[SESSION] Could not inject full session: %s", e)

    screenshots = []
    async def capture_diagnostic(label):
        pass

    logger.info("Opening URL: %s | Record ID: %s | Is Baseline: %s", url, record_id, record_is_baseline)
    
    # Navigation
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    
    # Inject Storage if available
    ls = record.get("_localStorage")
    ss = record.get("_sessionStorage")
    if ls or ss:
        injection_script = f"""
        (() => {{
            const ls = {json.dumps(ls or {{}})};
            const ss = {json.dumps(ss or {{}})};
            for (let k in ls) localStorage.setItem(k, ls[k]);
            for (let k in ss) sessionStorage.setItem(k, ss[k]);
        }})();
        """
        await page.evaluate(injection_script)
        logger.info("[SESSION] LocalStorage and SessionStorage injected on-page.")

    logger.info("[STEP] Waiting up to 60s for page to render (checking body text)...")
    for _wait_i in range(12):  # 12 x 5s = 60s max
        try:
            body_text = await page.inner_text("body")
            if body_text and len(body_text.strip()) > 100:
                logger.info("[STEP] Page rendered OK — bodyTextLength=%s", len(body_text))
                break
        except Exception as _we:
            logger.warning("[STEP] Body check error: %s", _we)
        await asyncio.sleep(5)
    else:
        logger.warning("[ISSUE] Page still blank after 60s. Continuing anyway — auth scoring will catch it.")

    await capture_diagnostic("initial_load")
    # Check if we have session data in the record object (loaded above)
    has_session_data = bool(record.get("_localStorage") or record.get("_sessionStorage"))
    
    # Injected Storage now that we have a page
    if has_session_data:
        try:
            ls = record.get("_localStorage", {})
            ss = record.get("_sessionStorage", {})
            if ls or ss:
                # Use a more robust string-based injection
                ls_json = __import__("json").dumps(ls)
                ss_json = __import__("json").dumps(ss)
                injection_js = f"""
                (() => {{
                    const lsData = {ls_json};
                    const ssData = {ss_json};
                    for (const [k, v] of Object.entries(lsData)) {{
                        localStorage.setItem(k, typeof v === 'string' ? v : JSON.stringify(v));
                    }}
                    for (const [k, v] of Object.entries(ssData)) {{
                        sessionStorage.setItem(k, typeof v === 'string' ? v : JSON.stringify(v));
                    }}
                }})();
                """

                await page.evaluate(injection_js)
                logger.info("[SESSION] ✅ LocalStorage and SessionStorage injected. Refreshing page...")
                
                # Stealth tweaks: spoof some navigator properties before reload
                stealth_js = """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {} };
                """
                await page.evaluate(stealth_js)
                
                await page.reload()
                # Wait for SPA to re-render after reload (poll instead of fixed sleep)
                logger.info("[SESSION] Waiting for page to re-render after storage injection...")
                for _ri in range(12):  # up to 60s
                    try:
                        bl = await page.evaluate("(document.body?.innerText || '').trim().length")
                        if isinstance(bl, int) and bl > 100:
                            logger.info("[SESSION] Page re-rendered after storage inject. bodyLen=%s", bl)
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(5)
                else:
                    logger.warning("[SESSION] Page still blank after storage injection reload. Auth scoring will handle it.")
                await capture_diagnostic("after_storage_injection")


        except Exception as e:
            logger.warning("[SESSION] Storage injection failed: %s", e)

    login_done = False

    login_workflow_executed = False

    # If session data was found, check immediately if we're already authenticated
    if has_session_data:
        await asyncio.sleep(3)
        await capture_diagnostic("pre_auth_check")
        quick_check = await _validate_portal_authentication(
            page, stage_label="cookie-inject-check", max_probes=2, probe_delay=2, min_score=4
        )
        if quick_check.get("authenticated"):
            logger.info("[COOKIE] ✅ Cookie injection worked! Authenticated immediately.")
            login_done = True
            login_workflow_executed = True
            await capture_diagnostic("authenticated_view")

    # --- Start a background task for periodic screenshots during the main wait ---
    async def periodic_ss():
        # Capture aggressively as requested: every 2 seconds for up to 20 minutes (600 items)
        # This gives the user a frame-by-frame view of the cloud execution.
        for i in range(600): 
            try:
                await asyncio.sleep(2)
                await capture_diagnostic(f"v_{i:03d}") # padded index for sorting
            except asyncio.CancelledError:
                break
            except Exception:
                pass
    
    # ss_task = asyncio.create_task(periodic_ss())
    class MockTask:
        def cancel(self): pass
    ss_task = MockTask()

    if not login_done:

        logger.info("Proceeding to Begin/CAPTCHA unblock phase (login_done=False)...")
        clicked = await click_begin_if_present(page, max_attempts=15, delay_seconds=4)
        logger.info("Begin click result for URL: %s", clicked)

        if clicked:
            # Wait for Begin button to definitely disappear before proceeding
            logger.info("Waiting for Begin button to clear from DOM...")
            for i in range(10):
                st = await _detect_captcha_stage(page)
                if isinstance(st, dict) and not st.get("hasBeginButton") and st.get("hasCaptchaWidget"):
                    logger.info("Begin button cleared, CAPTCHA widget active.")
                    break
                await asyncio.sleep(2)

            logger.info("Begin phase finished. Waiting 25s for CAPTCHA challenge payload to stabilize...")
            await asyncio.sleep(25)
            
            extract_result = await extract_captcha_payload(page)
            logger.info("CAPTCHA data extraction completed. has_data=%s", bool(extract_result))

            if extract_result and isinstance(extract_result.get("payload"), dict):
                logger.info("[STEP] Starting Anti-Captcha solve + inject pipeline...")
                solved = await solve_aws_waf_with_anticaptcha(page, url, extract_result, anticaptcha_infinite_retry=True, anticaptcha_rotate_on_timeout=True, reopen_on_retry=True, refresh_payload_each_task=True)
                logger.info("[RESULT] Anti-Captcha pipeline finished. solved=%s", solved)
                if solved:
                    login_workflow_executed = True
                    login_done = await perform_portal_login_after_unblock(page, url)
                    logger.info("[RESULT] Post-unblock login workflow finished. success=%s", login_done)
            else:
                logger.warning("[STEP] Anti-Captcha pipeline skipped because payload data is missing.")
        else:
            logger.warning("Begin click not confirmed. Checking if CAPTCHA stage is already active...")
            extract_result = await extract_captcha_payload(page)
            logger.info("CAPTCHA data extraction after begin-miss completed. has_data=%s", bool(extract_result))
            if extract_result and isinstance(extract_result.get("payload"), dict):
                logger.info("[STEP] Starting Anti-Captcha solve + inject pipeline after begin-miss fallback...")
                solved = await solve_aws_waf_with_anticaptcha(page, url, extract_result, anticaptcha_infinite_retry=True, anticaptcha_rotate_on_timeout=True, reopen_on_retry=True, refresh_payload_each_task=True)
                logger.info("[RESULT] Anti-Captcha pipeline (begin-miss fallback) finished. solved=%s", solved)
                if solved:
                    login_workflow_executed = True
                    login_done = await perform_portal_login_after_unblock(page, url)
                    logger.info("[RESULT] Post-unblock login workflow finished. success=%s", login_done)
            else:
                logger.warning("Begin did not click and CAPTCHA payload was not found for fallback flow.")

    if not login_done:
        logger.info("[STEP] Login not complete yet. Forcing explicit login workflow before documents stage.")
        login_workflow_executed = True
        login_done = await perform_portal_login_after_unblock(page, url)
        logger.info("[RESULT] Forced login workflow finished. success=%s", login_done)

    if login_done:
        if not login_workflow_executed:
            logger.error("[ISSUE] Internal guard: login_done=true without running login workflow. Skipping documents stage.")
            return

        gate_auth = await _validate_portal_authentication(
            page,
            stage_label="run-pre-docs-gate",
            max_probes=2,
            probe_delay=2,
            min_score=5,
        )
        if gate_auth.get("authenticated"):
            logger.info("[STEP] Strict login gate passed. Proceeding to Documents tab.")
            await _execute_documents_pipeline(context, page, url, record_id, record_is_baseline)
            await _execute_addenda_emails_pipeline(context, page, url, record_id, record_is_baseline)
        else:
            logger.error("[ISSUE] Login workflow ran but strict auth gate failed. Skipping documents stage.")
    else:
        logger.error("[ISSUE] Login flow did not complete for URL. Skipping documents pipeline for: %s", url)
    # --- FINISH TELEMETRY ---
    try:
        ss_task.cancel()
    except: pass
    
    return {"screenshots": screenshots}


async def run_single_portal_alpha(record_data):
    """
    PURE PLAYWRIGHT IMPLEMENTATION.
    Uses Playwright's modern Chromium + playwright-stealth to bypass bot detection.
    PlaywrightBrowserAdapter / PlaywrightPageAdapter make all existing agent logic
    (CAPTCHA, auth, document pipeline) work unchanged with zero modifications.
    """
    logger.info("[PW] Starting SINGLE TENDER SecurePortalAlpha [PLAYWRIGHT ENGINE]")
    supabase_client = get_supabase()
    if not supabase_client:
        logger.error("[PW] Supabase client not initialized")
        return None

    from playwright.async_api import async_playwright

    pw = None
    pw_browser = None

    try:
        # ── Step 1: Launch Playwright Chromium ─────────────────────────────────
        logger.info("[PW] Starting Playwright...")
        pw = await async_playwright().start()

        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1920,1080",
            # Disable SRI (Subresource Integrity) validation which blocks JS in headless
            "--disable-features=SubresourceIntegrity",
            # Disable automation detection flags
            "--disable-blink-features=AutomationControlled",
        ]

        chromium_exe = CHROMIUM_PATH_OVERRIDE
        launch_kwargs = dict(headless=True, args=launch_args)
        if chromium_exe:
            launch_kwargs["executable_path"] = chromium_exe
            logger.info(f"[PW] Using chromium override: {chromium_exe}")
        else:
            logger.info("[PW] Using Playwright's built-in Chromium")

        pw_browser = await pw.chromium.launch(**launch_kwargs)

        # ── Step 2: Create stealth browser context ──────────────────────────────
        ctx = await pw_browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/133.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Los_Angeles",
            permissions=["geolocation"],
            java_script_enabled=True,
            ignore_https_errors=True,
            accept_downloads=True,
        )

        # Apply playwright-stealth if available (hides automation fingerprints)
        try:
            _stealth_fn = Stealth().apply_stealth_async
            logger.info("[PW] playwright-stealth loaded ✅")
        except ImportError:
            _stealth_fn = None
            logger.warning("[PW] playwright-stealth not available — running without stealth")

        # Override JS automation properties at context level
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {}, app: {}, csi: () => {}, loadTimes: () => {} };
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
        """)

        # ── Step 4: Create page + apply stealth ────────────────────────────────
        pw_page = await ctx.new_page()
        await Stealth().apply_stealth_async(pw_page)
        logger.info("[PW] Browser + context + page created. Stealth applied.")

        # ── Step 5: Run the existing record processing logic ────────────────────
        try:
            result = await _process_single_record(ctx, pw_page, record_data, supabase_client)
            logger.info("[PW] _process_single_record completed.")
            return result
        except Exception as e:
            logger.error(f"[PW] Fatal error in run_single_portal_alpha inner: {e}", exc_info=True)
            return None
        finally:
            await pw_browser.close()
    except Exception as e:
        logger.error(f"[PW] Fatal error in run_single_portal_alpha outer: {e}", exc_info=True)
        return None
    finally:
        if pw:
            await pw.stop()


async def run_agent1_portal_alpha():
    logger.info("Starting Agent 1 SecurePortalAlpha [PURE PLAYWRIGHT STEALTH]")

    supabase_client = get_supabase()
    if not supabase_client:
        logger.error("Supabase client not initialized")
        return

    # Fetch pending SecurePortalAlpha targets
    records_res = supabase_client.table("records") \
        .select("id, url, notice_id, status") \
        .eq("portal_type", "portal_alpha") \
        .execute()
    
    records = records_res.data
    if not records:
        logger.error("No SecurePortalAlpha specific records found in DB.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        try:
            logger.info("Found %s SecurePortalAlpha target(s) in database", len(records))
            context = await browser.new_context(viewport={"width": 1920, "height": 1080})
            
            for record in records:
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)
                try:
                    await _process_single_record(context, page, record, supabase_client)
                finally:
                    await page.close()

            if AGENT1_KEEP_BROWSER_OPEN:
                logger.info("Keeping browser open. Press Ctrl+C to exit.")
                while True: await asyncio.sleep(60)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run_agent1_portal_alpha())