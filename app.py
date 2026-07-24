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
GAP_BETWEEN_TABS = 4          # seconds to wait after each tab
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


def open_prompt_in_tab(ctx, prompt, index, total, url):
    """Open a NEW tab, load the Niural link, type this prompt and submit it."""
    tab = ctx.new_page()
    print(f"[{index}/{total}] new tab -> {prompt}", flush=True)
    try:
        tab.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return f"could not load Niural: {e}"

    box = find_niural_input(tab, timeout=45000)
    if box is None:
        return "could not find the 'Ask me anything' box"

    try:
        box.click()
        tab.keyboard.type(prompt, delay=8)
        time.sleep(0.4)
        tab.keyboard.press("Enter")
        time.sleep(0.8)
        if _box_still_has_text(box):
            if _click_send_button(tab):
                return "submitted (send button)"
            return "typed but not confirmed -- check the tab"
        return "submitted (Enter)"
    except Exception as e:
        return f"failed: {e}"


def run_prompts(prompts, url):
    """Ensure logged in, then open one tab per prompt. Returns a result dict."""
    ctx = get_context()
    if not is_logged_in(ctx, url):
        return {
            "status": "login_required",
            "message": ("You're not logged in to Niural (or the link is wrong). "
                        "Log in to Niural in the Chrome window that just opened, "
                        "then click Start Prompting again."),
        }
    results = []
    for i, pr in enumerate(prompts, 1):
        outcome = open_prompt_in_tab(ctx, pr, i, len(prompts), url)
        results.append({"prompt": pr, "outcome": outcome})
        if i < len(prompts):
            time.sleep(GAP_BETWEEN_TABS)
    return {"status": "ok", "count": len(results), "results": results}


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
  h1 { font-size: 1.5rem; margin-bottom: .25rem; }
  p.sub { color: #666; margin-top: 0; }
  label { display: block; font-weight: 600; margin: 18px 0 6px; }
  input[type=text] { width: 100%; padding: 11px 12px; font-size: 14px;
             border: 1px solid #bbb; border-radius: 10px;
             font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  textarea { width: 100%; min-height: 240px; padding: 12px; font-size: 15px;
             border: 1px solid #bbb; border-radius: 10px; resize: vertical;
             font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
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
  <p class="sub">Paste the employee's Ask Emma link, then the prompts — <b>one per line</b>. Each opens in its own tab.</p>

  <label for="url">Niural Ask Emma link (per employee)</label>
  <input type="text" id="url" placeholder="https://www.qa.niural.com/<id>/niural-ai"
         value="__DEFAULT_URL__">

  <label for="prompts">Prompts (one per line)</label>
  <textarea id="prompts" placeholder="Show timesheet hours by employee for 2027
How many timesheets are pending for March 2027?
Show regular vs. overtime hours for 2027"></textarea>

  <div class="row">
    <button id="start">Start Prompting</button>
    <span id="count"></span>
  </div>

  <div id="status"></div>

<script>
  const ta = document.getElementById('prompts');
  const urlInput = document.getElementById('url');
  const btn = document.getElementById('start');
  const status = document.getElementById('status');
  const count = document.getElementById('count');

  function lines() {
    return ta.value.split('\\n').map(s => s.trim()).filter(Boolean).length;
  }
  ta.addEventListener('input', () => { count.textContent = lines() + ' prompt(s)'; });
  count.textContent = lines() + ' prompt(s)';

  btn.addEventListener('click', async () => {
    const text = ta.value;
    const url = urlInput.value.trim();
    if (!url) { status.textContent = 'Please paste the Niural Ask Emma link.'; return; }
    if (!text.trim()) { status.textContent = 'Please enter at least one prompt.'; return; }
    btn.disabled = true;
    status.innerHTML = 'Working… a Chrome window will open and run each prompt. ' +
                       'This can take a bit for many prompts.';
    try {
      const res = await fetch('/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompts: text, url: url })
      });
      const data = await res.json();
      if (data.status === 'login_required') {
        status.innerHTML = '<div class="card warn">' + data.message + '</div>';
      } else if (data.status === 'ok') {
        let html = '<div class="card ok">Done — opened ' + data.count + ' tab(s).</div>';
        data.results.forEach((r, i) => {
          const cls = r.outcome.startsWith('submitted') ? 'ok'
                    : r.outcome.startsWith('failed') || r.outcome.startsWith('could not') ? 'err' : 'warn';
          html += '<div class="card"><b>' + (i+1) + '.</b> <code>' + r.prompt +
                  '</code><br><span class="' + cls + '">' + r.outcome + '</span></div>';
        });
        status.innerHTML = html;
      } else {
        status.innerHTML = '<div class="card err">' + (data.message || 'Something went wrong.') + '</div>';
      }
    } catch (e) {
      status.innerHTML = '<div class="card err">Error talking to the local server: ' + e + '</div>';
    } finally {
      btn.disabled = false;
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
        if self.path != "/start":
            self._send(404, "not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
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
            result = run_prompts(prompts, url)
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
