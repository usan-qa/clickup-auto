#!/usr/bin/env python3
"""
Prompt UI (frontend + local server)
===================================
A simple web page where you paste prompts (one per line) and click
"Start Prompting". Each prompt is sent to your Niural Ask Emma in its OWN new
tab, using the same logged-in Chrome automation profile as the other scripts.

HOW TO RUN
----------
    python3 app.py

Then open this in your normal browser:
    http://localhost:8000

Paste your prompts (one per line), click "Start Prompting". A Chrome window
(the automation profile) opens and runs each prompt in a new tab. The tabs stay
open so you can review the responses.

If it says you're not logged in, log in to Niural in that Chrome window, then
click "Start Prompting" again.
"""

import re
import sys
import json
import time
import threading
import webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from playwright.sync_api import sync_playwright

# ==============================================================================
# CONFIG
# ==============================================================================
NIURAL_URL = "https://www.qa.niural.com/49733946-fc18-49ef-8789-96c05b47e9bd/niural-ai"
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
AUTOMATION_PROFILE_DIR = Path(__file__).parent / "chrome_automation"
GAP_BETWEEN_TABS = 4          # seconds to wait after each tab (tab mode)
RESPONSE_MAX_WAIT = 120       # max seconds to wait for one Emma response (follow-up mode)
PORT = 8000

NIURAL_INPUT_SELECTOR = None
NIURAL_BOX_SELECTOR = (
    "textarea[placeholder*='Ask' i], input[placeholder*='Ask' i], "
    "[placeholder*='Ask' i], div[contenteditable='true'], "
    "[contenteditable='true'], [role='textbox'], textarea"
)
# ==============================================================================


# ---- Browser automation (kept alive across requests) -------------------------
_state = {"pw": None, "ctx": None}


def get_context():
    """Start Playwright + the automation Chrome once, then reuse it."""
    if _state["ctx"] is not None:
        return _state["ctx"]
    AUTOMATION_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(AUTOMATION_PROFILE_DIR),
        executable_path=CHROME_PATH,
        headless=False,
        viewport=None,
        args=["--no-first-run", "--no-default-browser-check"],
    )
    _state["pw"] = pw
    _state["ctx"] = ctx
    return ctx


def looks_like_login(page) -> bool:
    url = (page.url or "").lower()
    if "/login" in url or "/auth" in url:
        return True
    try:
        head = (page.evaluate("document.body.innerText") or "").lower()[:500]
    except Exception:
        head = ""
    return any(s in head for s in [
        "log in to niural", "forgot password", "sign in using sso",
        "unable to sign you in", "request failed with status code", "back to login",
    ])


def find_niural_input(page, timeout=45000):
    sel = NIURAL_INPUT_SELECTOR or NIURAL_BOX_SELECTOR
    try:
        page.wait_for_selector(sel, state="visible", timeout=timeout)
    except Exception:
        return None
    return page.locator(sel).first


def _box_still_has_text(box) -> bool:
    try:
        val = box.input_value()
    except Exception:
        try:
            val = box.inner_text()
        except Exception:
            val = ""
    return bool((val or "").strip())


def _click_send_button(page) -> bool:
    for sel in ["button[type='submit']", "button[aria-label*='send' i]",
                "button:has(svg)"]:
        try:
            btn = page.locator(sel).last
            if btn.count() > 0 and btn.is_enabled():
                btn.click()
                return True
        except Exception:
            continue
    return False


def is_logged_in(ctx, url) -> bool:
    """Open the Niural link in the first tab and check whether we're signed in."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        return False
    time.sleep(3)
    return not looks_like_login(page)


def submit_in_page(page, prompt):
    """Find the box in this page, type the prompt and submit it. Returns outcome."""
    box = find_niural_input(page, timeout=45000)
    if box is None:
        return "could not find the 'Ask me anything' box"
    try:
        box.click()
        page.keyboard.type(prompt, delay=8)
        time.sleep(0.4)
        page.keyboard.press("Enter")
        time.sleep(0.8)
        if _box_still_has_text(box):
            if _click_send_button(page):
                return "submitted (send button)"
            return "typed but not confirmed -- check the tab"
        return "submitted (Enter)"
    except Exception as e:
        return f"failed: {e}"


def wait_for_response(page, max_wait=RESPONSE_MAX_WAIT):
    """
    Wait until Emma's answer finishes: first wait for the page text to start
    growing (response streaming in), then wait until it stops growing (done).
    """
    interval = 1.5
    waited = 0.0

    def text_len():
        try:
            return len(page.evaluate("document.body.innerText") or "")
        except Exception:
            return -1

    baseline = text_len()

    # Phase 1: wait for the response to START (text grows past baseline).
    while waited < 25:
        time.sleep(interval)
        waited += interval
        if text_len() > baseline + 5:
            break

    # Phase 2: wait for it to STOP (text stable for a few checks in a row).
    last, stable = -1, 0
    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        cur = text_len()
        if cur == last:
            stable += 1
            if stable >= 3:      # ~4.5s with no change -> done
                return True
        else:
            stable, last = 0, cur
    return False


def open_prompt_in_tab(ctx, prompt, index, total, url):
    """Open a NEW tab, load the Niural link, type this prompt and submit it."""
    tab = ctx.new_page()
    print(f"[{index}/{total}] new tab -> {prompt}", flush=True)
    try:
        tab.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return f"could not load Niural: {e}"
    return submit_in_page(tab, prompt)


def run_tab_mode(ctx, prompts, url):
    """One new tab per prompt."""
    results = []
    for i, pr in enumerate(prompts, 1):
        outcome = open_prompt_in_tab(ctx, pr, i, len(prompts), url)
        results.append({"prompt": pr, "outcome": outcome})
        if i < len(prompts):
            time.sleep(GAP_BETWEEN_TABS)
    return results


def run_followup_mode(ctx, prompts, url):
    """One tab; send each prompt, wait for the response, then send the next."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return [{"prompt": prompts[0] if prompts else "", "outcome": f"could not load Niural: {e}"}]
    time.sleep(3)

    results = []
    for i, pr in enumerate(prompts, 1):
        print(f"[{i}/{len(prompts)}] follow-up -> {pr}", flush=True)
        outcome = submit_in_page(page, pr)
        results.append({"prompt": pr, "outcome": outcome})
        if outcome.startswith("submitted") and i < len(prompts):
            print("   waiting for Emma's response to finish...", flush=True)
            wait_for_response(page)
            time.sleep(1)
    return results


def run_prompts(prompts, url, mode):
    """Ensure logged in, then run in the chosen mode. Returns a result dict."""
    ctx = get_context()
    if not is_logged_in(ctx, url):
        return {
            "status": "login_required",
            "message": ("You're not logged in to Niural (or the link is wrong). "
                        "Log in to Niural in the Chrome window that just opened, "
                        "then click Start Prompting again."),
        }
    if mode == "followup":
        results = run_followup_mode(ctx, prompts, url)
    else:
        results = run_tab_mode(ctx, prompts, url)
    return {"status": "ok", "count": len(results), "results": results, "mode": mode}


# ---- QA: ClickUp task -> Niural ----------------------------------------------
def extract_prompts_from_text(text):
    """Pull every 'For prompt: ... >' segment out of a blob of text."""
    matches = re.findall(r"[Ff]or\s+prompt\s*:\s*(.+?)\s*(?:>|$)", text or "")
    seen, out = set(), []
    for m in matches:
        s = m.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def clickup_needs_login(page) -> bool:
    url = (page.url or "").lower()
    if "clickup.com/login" in url or "/login" in url:
        return True
    try:
        head = (page.evaluate("document.body.innerText") or "").lower()[:400]
    except Exception:
        head = ""
    return "welcome back" in head or "log in to clickup" in head


def run_clickup(clickup_url, niural_url, mode):
    """Read the prompt(s) from a ClickUp task, then run them on Niural."""
    ctx = get_context()

    # 1) Open the ClickUp task and read its text.
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(clickup_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return {"status": "error", "message": f"Could not open the ClickUp link: {e}"}
    time.sleep(5)

    if clickup_needs_login(page):
        return {"status": "login_required",
                "message": ("You're not logged in to ClickUp. Log in to ClickUp in "
                            "the Chrome window that just opened, then click Start "
                            "Prompting again.")}

    try:
        text = page.evaluate("document.body.innerText") or ""
    except Exception:
        text = ""
    prompts = extract_prompts_from_text(text)
    if not prompts:
        return {"status": "error",
                "message": ("Couldn't find a 'For prompt: ... >' in that ClickUp task. "
                            "Make sure it's the task whose title has the prompt.")}

    # 2) Make sure Niural is logged in, then run the prompt(s) on it.
    if not is_logged_in(ctx, niural_url):
        return {"status": "login_required",
                "message": ("Read the prompt from ClickUp, but you're not logged in to "
                            "Niural (or the link is wrong). Log in to Niural in the "
                            "Chrome window, then click Start Prompting again.")}

    if mode == "followup":
        results = run_followup_mode(ctx, prompts, niural_url)
    else:
        results = run_tab_mode(ctx, prompts, niural_url)
    return {"status": "ok", "count": len(results), "results": results,
            "prompts": prompts, "mode": mode}


def parse_prompts(text):
    """One prompt per non-blank line; strips whitespace and surrounding quotes."""
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
            s = s[1:-1].strip()
        if s:
            out.append(s)
    return out


# ---- Web page ----------------------------------------------------------------
PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Niural Prompt Runner</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 760px; margin: 40px auto; padding: 0 20px; line-height: 1.5; }
  h1 { font-size: 1.5rem; margin-bottom: .25rem; text-align: center; }
  .switch-wrap { text-align: center; }
  p.sub { color: #666; margin-top: 0; }
  label { display: block; font-weight: 600; margin: 18px 0 6px; }
  input[type=text] { width: 100%; padding: 11px 12px; font-size: 14px;
             border: 1px solid #bbb; border-radius: 10px;
             font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  textarea { width: 100%; min-height: 240px; padding: 12px; font-size: 15px;
             border: 1px solid #bbb; border-radius: 10px; resize: vertical;
             font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .modes { display: flex; flex-direction: column; gap: 8px; margin-top: 6px; }
  .modes label { font-weight: 400; margin: 0; display: flex; align-items: center;
                 gap: 8px; cursor: pointer; }
  .switch { display: inline-flex; border: 1px solid #6c47ff; border-radius: 999px;
            overflow: hidden; margin-bottom: 20px; }
  .switch button { background: transparent; color: #6c47ff; border: 0;
            padding: 9px 20px; font-size: 14px; font-weight: 600; border-radius: 0;
            cursor: pointer; }
  .switch button.active { background: #6c47ff; color: #fff; }
  section.panel[hidden] { display: none; }
  .row { display: flex; gap: 12px; align-items: center; margin-top: 14px; }
  button { background: #6c47ff; color: #fff; border: 0; padding: 12px 22px;
           font-size: 15px; font-weight: 600; border-radius: 10px; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  #status { margin-top: 18px; white-space: pre-wrap; }
  .card { border: 1px solid #ddd3; border-radius: 10px; padding: 10px 14px;
          margin-top: 8px; }
  .ok { color: #1a7f37; } .warn { color: #b26a00; } .err { color: #c0392b; }
  code { background: #8881; padding: 1px 5px; border-radius: 5px; }
</style>
</head>
<body>
  <h1>Niural Prompt Runner</h1>

  <div class="switch-wrap">
    <div class="switch">
      <button id="tab-dev" class="active">Emma</button>
      <button id="tab-qa">ClickUp</button>
    </div>
  </div>

  <!-- ================= DEV + QA: paste prompts directly ================= -->
  <section class="panel" id="panel-dev">
    <p class="sub">Paste the employee's Ask Emma link, then the prompts — <b>one per line</b>.</p>

    <label for="url">Niural Ask Emma link (per employee)</label>
    <input type="text" id="url" placeholder="https://www.qa.niural.com/<id>/niural-ai"
           value="__DEFAULT_URL__">

    <label for="prompts">Prompts (one per line)</label>
    <textarea id="prompts" placeholder="Show timesheet hours by employee for 2027
How many timesheets are pending for March 2027?
Show regular vs. overtime hours for 2027"></textarea>

    <label>Mode</label>
    <div class="modes">
      <label><input type="radio" name="mode" value="tab" checked>
        Each prompt in a new tab</label>
      <label><input type="radio" name="mode" value="followup">
        Follow-up — same tab, wait for each response before the next</label>
    </div>

    <div class="row">
      <button id="start">Start Prompting</button>
      <span id="count"></span>
    </div>
  </section>

  <!-- ================= QA: read prompt from a ClickUp task ================= -->
  <section class="panel" id="panel-qa" hidden>
    <p class="sub">Give a <b>ClickUp task link</b> and a Niural link. It reads the
      <code>For prompt: … &gt;</code> from that task and runs it on Niural.</p>

    <label for="cuUrl">ClickUp task link</label>
    <input type="text" id="cuUrl" placeholder="https://app.clickup.com/t/XXXXXXX">

    <label for="qaNiuralUrl">Niural Ask Emma link</label>
    <input type="text" id="qaNiuralUrl" placeholder="https://www.qa.niural.com/<id>/niural-ai"
           value="__DEFAULT_URL__">

    <label>Mode</label>
    <div class="modes">
      <label><input type="radio" name="qamode" value="tab" checked>
        Each prompt in a new tab</label>
      <label><input type="radio" name="qamode" value="followup">
        Follow-up — same tab, wait for each response before the next</label>
    </div>

    <div class="row">
      <button id="startQa">Start Prompting</button>
    </div>
  </section>

  <div id="status"></div>

<script>
  const status = document.getElementById('status');

  // ---- Top switch: Dev + QA  /  QA ----
  const tabDev = document.getElementById('tab-dev');
  const tabQa = document.getElementById('tab-qa');
  const panelDev = document.getElementById('panel-dev');
  const panelQa = document.getElementById('panel-qa');
  function show(which) {
    const dev = which === 'dev';
    tabDev.classList.toggle('active', dev);
    tabQa.classList.toggle('active', !dev);
    panelDev.hidden = !dev;
    panelQa.hidden = dev;
    status.innerHTML = '';
  }
  tabDev.addEventListener('click', () => show('dev'));
  tabQa.addEventListener('click', () => show('qa'));

  // ---- Shared: render the run results ----
  function renderResult(data, doneWord) {
    if (data.status === 'login_required') {
      status.innerHTML = '<div class="card warn">' + data.message + '</div>';
    } else if (data.status === 'ok') {
      let html = '<div class="card ok">' + doneWord + ' ' + data.count + ' prompt(s).</div>';
      data.results.forEach((r, i) => {
        const cls = r.outcome.startsWith('submitted') ? 'ok'
                  : (r.outcome.startsWith('failed') || r.outcome.startsWith('could not')) ? 'err' : 'warn';
        html += '<div class="card"><b>' + (i+1) + '.</b> <code>' + r.prompt +
                '</code><br><span class="' + cls + '">' + r.outcome + '</span></div>';
      });
      status.innerHTML = html;
    } else {
      status.innerHTML = '<div class="card err">' + (data.message || 'Something went wrong.') + '</div>';
    }
  }

  async function post(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    return res.json();
  }

  // ---- Dev + QA panel ----
  const ta = document.getElementById('prompts');
  const urlInput = document.getElementById('url');
  const btn = document.getElementById('start');
  const count = document.getElementById('count');
  function lines() {
    return ta.value.split('\\n').map(s => s.trim()).filter(Boolean).length;
  }
  ta.addEventListener('input', () => { count.textContent = lines() + ' prompt(s)'; });
  count.textContent = lines() + ' prompt(s)';

  btn.addEventListener('click', async () => {
    const text = ta.value;
    const url = urlInput.value.trim();
    const mode = document.querySelector('input[name=mode]:checked').value;
    if (!url) { status.textContent = 'Please paste the Niural Ask Emma link.'; return; }
    if (!text.trim()) { status.textContent = 'Please enter at least one prompt.'; return; }
    btn.disabled = true;
    status.innerHTML = mode === 'followup'
      ? 'Working… sending prompts one by one in a single tab, waiting for each response.'
      : 'Working… a Chrome window will open and run each prompt in its own tab.';
    try {
      renderResult(await post('/start', { prompts: text, url: url, mode: mode }), 'Done — ran');
    } catch (e) {
      status.innerHTML = '<div class="card err">Error talking to the local server: ' + e + '</div>';
    } finally {
      btn.disabled = false;
    }
  });

  // ---- QA panel (ClickUp -> Niural) ----
  const btnQa = document.getElementById('startQa');
  const cuUrl = document.getElementById('cuUrl');
  const qaNiuralUrl = document.getElementById('qaNiuralUrl');
  btnQa.addEventListener('click', async () => {
    const clickup_url = cuUrl.value.trim();
    const niural_url = qaNiuralUrl.value.trim();
    const mode = document.querySelector('input[name=qamode]:checked').value;
    if (!clickup_url) { status.textContent = 'Please paste the ClickUp task link.'; return; }
    if (!niural_url) { status.textContent = 'Please paste the Niural Ask Emma link.'; return; }
    btnQa.disabled = true;
    status.innerHTML = 'Working… reading the prompt from ClickUp, then running it on Niural.';
    try {
      renderResult(
        await post('/start_clickup', { clickup_url, niural_url, mode }),
        'Done — read from ClickUp and ran');
    } catch (e) {
      status.innerHTML = '<div class="card err">Error talking to the local server: ' + e + '</div>';
    } finally {
      btnQa.disabled = false;
    }
  });
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = PAGE_HTML.replace("__DEFAULT_URL__", NIURAL_URL)
            self._send(200, page, "text/html; charset=utf-8")
        elif self.path == "/favicon.ico":
            self._send(204, b"")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path not in ("/start", "/start_clickup"):
            self._send(404, "not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
            mode = "followup" if payload.get("mode") == "followup" else "tab"

            if self.path == "/start":
                prompts = parse_prompts(payload.get("prompts", ""))
                url = (payload.get("url") or "").strip() or NIURAL_URL
                if not url.lower().startswith("http"):
                    self._send(200, json.dumps(
                        {"status": "error",
                         "message": "Please paste a valid Niural link (starting with http)."}))
                    return
                if not prompts:
                    self._send(200, json.dumps(
                        {"status": "error", "message": "No prompts found."}))
                    return
                result = run_prompts(prompts, url, mode)
                self._send(200, json.dumps(result))
                return

            # /start_clickup
            clickup_url = (payload.get("clickup_url") or "").strip()
            niural_url = (payload.get("niural_url") or "").strip() or NIURAL_URL
            if not clickup_url.lower().startswith("http"):
                self._send(200, json.dumps(
                    {"status": "error",
                     "message": "Please paste a valid ClickUp task link (starting with http)."}))
                return
            if not niural_url.lower().startswith("http"):
                self._send(200, json.dumps(
                    {"status": "error",
                     "message": "Please paste a valid Niural link (starting with http)."}))
                return
            result = run_clickup(clickup_url, niural_url, mode)
            self._send(200, json.dumps(result))
        except Exception as e:
            self._send(200, json.dumps({"status": "error", "message": str(e)}))

    def log_message(self, *args):
        pass  # keep the terminal quiet


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    # Single-threaded server: Playwright's sync API must stay on one thread.
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print("=" * 60)
    print("Prompt UI is running.")
    print(f"Opening it in your browser:  {url}")
    print(f"(If it doesn't open, paste this into your browser: {url} )")
    print("Press Ctrl+C here to stop.")
    print("=" * 60, flush=True)
    # Open the page in the real default browser (not VS Code's built-in one).
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        if _state["ctx"]:
            try:
                _state["ctx"].close()
            except Exception:
                pass
        if _state["pw"]:
            try:
                _state["pw"].stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()
