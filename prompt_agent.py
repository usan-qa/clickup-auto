#!/usr/bin/env python3
"""
Prompt Agent
============
Reads your ClickUp notification/inbox task titles, extracts the prompt from each
(the text after "For prompt:" up to the next ">"), then types that prompt into
your Niural "Ask Emma" box and hits Enter.

It drives CHROME. Because Chrome 136+ refuses automation on your everyday
profile, the script uses a dedicated automation profile (./chrome_automation)
that Chrome DOES allow it to control. You log in to ClickUp and Niural once in
that window; the login is saved and reused on every future run.

--------------------------------------------------------------------------------
HOW TO RUN
--------------------------------------------------------------------------------
Just run it (you do NOT need to quit your normal Chrome):
       python3 prompt_agent.py

First run: a Chrome window opens with a ClickUp tab and a Niural tab. If either
shows a login page, log in once and press Enter -- it is saved for all future
runs. Later runs skip login and go straight to reading your prompts.

To reset the login later, delete the ./chrome_automation folder.
"""

import re
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

# Make output show up live in the terminal instead of being buffered until exit.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# ==============================================================================
# CONFIG
# ==============================================================================
CLICKUP_URL = "https://app.clickup.com/90131112803/inbox?tab=primary"
NIURAL_URL  = "https://www.qa.niural.com/7fc11251-ecb3-470e-8d62-c7a2b77b63c6/niural-ai"

# Path to your Chrome executable (macOS default).
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Your REAL Chrome profile directory. macOS default:
REAL_CHROME_PROFILE_DIR = Path.home() / "Library/Application Support/Google/Chrome"

# Chrome 136+ refuses automation on the DEFAULT profile folder, so we use a local
# (non-default) automation profile and drive that instead. It persists between
# runs (your one-time login is remembered). Delete it to reset the login.
AUTOMATION_PROFILE_DIR = Path(__file__).parent / "chrome_automation"

# Advanced overrides. Leave as None for auto-detection.
CLICKUP_ROW_SELECTOR = None
NIURAL_INPUT_SELECTOR = None

# Where to dump debug info if reading the inbox fails.
DEBUG_DIR = Path(__file__).parent / "debug"
# ==============================================================================


def prepare_profile():
    """
    Ensure the dedicated automation profile folder exists. Chrome allows
    automation on this (non-default) profile. It persists between runs, so the
    one-time login you do is remembered. Returns the folder to drive.
    """
    AUTOMATION_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return str(AUTOMATION_PROFILE_DIR)


def looks_like_login(page) -> bool:
    """Heuristic: is the current page a ClickUp/Niural login screen?"""
    url = (page.url or "").lower()
    if "/login" in url or "/auth" in url:
        return True
    try:
        head = (page.evaluate("document.body.innerText") or "").lower()[:500]
    except Exception:
        head = ""
    signals = ["welcome back", "log in to niural", "forgot password",
               "continue with sso", "sign in using sso"]
    return any(s in head for s in signals)


def ensure_logged_in(ctx, page):
    """
    Open ClickUp (and Niural in a 2nd tab). If either shows a login page, pause
    so the user logs in once; the automation profile then keeps them logged in.
    """
    print("\nChecking your logins...", flush=True)
    page.goto(CLICKUP_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)
    niural_tab = ctx.new_page()
    try:
        niural_tab.goto(NIURAL_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
    except Exception:
        pass

    cu_login = looks_like_login(page)
    ni_login = looks_like_login(niural_tab)

    if cu_login or ni_login:
        print("\n" + "=" * 70)
        print("ONE-TIME LOGIN NEEDED")
        need = []
        if cu_login:
            need.append("ClickUp")
        if ni_login:
            need.append("Niural")
        print(f"Please log in to: {', '.join(need)} in the Chrome window that")
        print("just opened (two tabs: ClickUp and Niural).")
        print("Once BOTH show your account, come back and press Enter.")
        print("This is saved -- you will not be asked again.")
        print("=" * 70, flush=True)
        input("Press Enter after logging in... ")
    else:
        print("  Already logged in to both. ", flush=True)

    try:
        niural_tab.close()
    except Exception:
        pass


def extract_prompts_from_text(text: str):
    """
    Find EVERY 'For prompt: ... >' segment in a big blob of text and return the
    inner prompts. Example match:
        'QA> For prompt:Show me the visualization ... 2026> Asking follow up...'
      -> 'Show me the visualization ... 2026'
    Works on the whole page text at once, so it does not depend on fragile CSS.
    """
    matches = re.findall(r"[Ff]or\s+prompt\s*:\s*(.+?)\s*(?:>|$)", text)
    prompts = []
    for m in matches:
        pr = m.strip()
        if pr:
            prompts.append(pr)
    return prompts


# Finds the tallest scrollable element on the page (the notifications panel).
_FIND_SCROLLER_JS = """
() => {
  const cands = Array.from(document.querySelectorAll('*')).filter(e => {
    const st = getComputedStyle(e);
    const oy = st.overflowY;
    return (oy === 'auto' || oy === 'scroll') && (e.scrollHeight - e.clientHeight) > 40;
  });
  cands.sort((a,b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
  return cands[0] || document.scrollingElement || document.body;
}
"""

_SCROLL_TO_TOP_JS = """
() => {
  %s
  const el = findScroller();
  el.scrollTop = 0;
  window.scrollTo(0, 0);
}
""" % ("const findScroller = " + _FIND_SCROLLER_JS + ";")

_SCROLL_DOWN_JS = """
() => {
  %s
  const el = findScroller();
  const before = el.scrollTop;
  const step = Math.max(200, el.clientHeight * 0.85);
  el.scrollTop = el.scrollTop + step;
  window.scrollBy(0, step);
  const maxTop = el.scrollHeight - el.clientHeight;
  return { top: el.scrollTop, max: maxTop,
           moved: el.scrollTop !== before,
           atBottom: el.scrollTop >= maxTop - 5 };
}
""" % ("const findScroller = " + _FIND_SCROLLER_JS + ";")


def read_clickup_prompts(page):
    """Load ClickUp, scroll to load rows, and extract all prompts from the text."""
    print("\nNavigating to ClickUp inbox...", flush=True)
    page.goto(CLICKUP_URL, wait_until="domcontentloaded", timeout=60000)
    print(f"  Landed on: {page.url}", flush=True)

    # Give the React app time to render. Don't wait for full network idle --
    # ClickUp keeps sockets open, so that never settles; just wait a bit.
    print("  Waiting for the inbox to render...", flush=True)
    time.sleep(6)

    # Scroll the inbox from top to bottom, collecting every prompt on the way.
    # ClickUp virtualizes the list, so we scroll the actual scrollable panel
    # (not just the window) and keep reading until we reach the bottom.
    collected = ""
    seen_prompts = []
    seen_set = set()
    print("  Scanning for prompts (scrolling through the whole inbox)...", flush=True)

    # Start at the very top of the list.
    try:
        page.evaluate(_SCROLL_TO_TOP_JS)
    except Exception:
        pass
    time.sleep(1)

    stuck = 0
    for pass_num in range(40):  # generous cap; we stop early when we hit bottom
        try:
            text = page.evaluate("document.body.innerText") or ""
        except Exception:
            text = ""
        collected = text
        before = len(seen_prompts)
        for pr in extract_prompts_from_text(text):
            if pr not in seen_set:
                seen_set.add(pr)
                seen_prompts.append(pr)
        print(f"    pass {pass_num + 1}: {len(seen_prompts)} prompt(s) so far "
              f"(page text {len(text)} chars)", flush=True)

        # Scroll the tallest scrollable container down by ~one screen.
        try:
            info = page.evaluate(_SCROLL_DOWN_JS)
        except Exception:
            info = {"moved": False, "atBottom": True}

        # Stop when we've reached the bottom AND found nothing new for a bit.
        no_new = len(seen_prompts) == before
        if info and info.get("atBottom") and no_new:
            stuck += 1
            if stuck >= 2:
                print("    Reached the bottom of the inbox.", flush=True)
                break
        else:
            stuck = 0
        time.sleep(1.0)

    # Also honor an explicit row selector if the user set one.
    if CLICKUP_ROW_SELECTOR and not seen_prompts:
        rows = page.locator(CLICKUP_ROW_SELECTOR)
        for i in range(rows.count()):
            try:
                t = rows.nth(i).inner_text()
            except Exception:
                continue
            for pr in extract_prompts_from_text(t):
                if pr not in seen_set:
                    seen_set.add(pr)
                    seen_prompts.append(pr)

    if not seen_prompts:
        _dump_debug(page, collected)

    return seen_prompts


def _dump_debug(page, text):
    """Save a screenshot + the page text so we can see what ClickUp actually showed."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / "clickup.png"), full_page=True)
        (DEBUG_DIR / "clickup_text.txt").write_text(text or "", encoding="utf-8")
        has_marker = "for prompt" in (text or "").lower()
        print("\n--- DEBUG ---")
        print(f"  Page text length: {len(text or '')} chars")
        print(f"  Contains 'for prompt': {has_marker}")
        print(f"  Saved screenshot -> {DEBUG_DIR / 'clickup.png'}")
        print(f"  Saved page text  -> {DEBUG_DIR / 'clickup_text.txt'}")
        if not has_marker:
            print("  The text 'For prompt' was NOT on the page. Likely causes:")
            print("    - The inbox tab/URL is different from where the tasks are.")
            print("    - The notifications had not finished loading.")
        print("--- END DEBUG ---")
    except Exception as e:
        print(f"  (could not write debug info: {e})")


# All the shapes the "Ask me anything..." box might take.
NIURAL_BOX_SELECTOR = (
    "textarea[placeholder*='Ask' i], input[placeholder*='Ask' i], "
    "[placeholder*='Ask' i], div[contenteditable='true'], "
    "[contenteditable='true'], [role='textbox'], textarea"
)


def find_niural_input(page, timeout=30000):
    """Wait for the Niural input box to actually render, then return it."""
    sel = NIURAL_INPUT_SELECTOR or NIURAL_BOX_SELECTOR
    try:
        page.wait_for_selector(sel, state="visible", timeout=timeout)
    except Exception:
        return None
    return page.locator(sel).first


def open_prompt_in_tab(ctx, prompt, index, total):
    """
    Open a NEW Chrome tab, load Niural in it, type this prompt and submit it.
    Returns the tab (Page). The tab is left OPEN for you to review.
    """
    tab = ctx.new_page()
    print(f"\n[{index}/{total}] New tab -> Niural")
    print(f"   prompt: {prompt}", flush=True)
    try:
        tab.goto(NIURAL_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"   !! could not load Niural in this tab: {e}", flush=True)
        return tab

    # Wait until the "Ask me anything..." box has actually rendered.
    box = find_niural_input(tab, timeout=45000)
    if box is None:
        print("   !! Could not find the 'Ask me anything' box (page too slow?).",
              flush=True)
        return tab

    try:
        box.click()
        # Type via the keyboard so the app's React state registers the input.
        tab.keyboard.type(prompt, delay=8)
        time.sleep(0.4)
        tab.keyboard.press("Enter")
        time.sleep(0.8)

        # Fallback: if the text is still sitting in the box, click the send
        # (up-arrow) button instead.
        if _box_still_has_text(box):
            if _click_send_button(tab):
                print("   submitted (clicked send button).", flush=True)
            else:
                print("   typed, but couldn't confirm submit -- check this tab.",
                      flush=True)
        else:
            print("   submitted (hit Enter).", flush=True)
    except Exception as e:
        print(f"   !! failed to type/submit: {e}", flush=True)
    return tab


def _box_still_has_text(box) -> bool:
    """True if the input box still contains text (i.e. it wasn't submitted)."""
    try:
        val = box.input_value()
    except Exception:
        try:
            val = box.inner_text()
        except Exception:
            val = ""
    return bool((val or "").strip())


def _click_send_button(page) -> bool:
    """Try to click Niural's send/submit button next to the input."""
    for sel in [
        "button[type='submit']",
        "button[aria-label*='send' i]",
        "button:has(svg)",
    ]:
        try:
            btn = page.locator(sel).last
            if btn.count() > 0 and btn.is_enabled():
                btn.click()
                return True
        except Exception:
            continue
    return False


def main():
    # Uses a DEDICATED automation profile, separate from your everyday Chrome,
    # so you do NOT need to quit your normal Chrome.
    # Just don't run this script twice at the same time.
    automation_dir = prepare_profile()

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=automation_dir,  # dedicated automation profile
                executable_path=CHROME_PATH,
                headless=False,
                viewport=None,
                args=["--no-first-run", "--no-default-browser-check"],
            )
        except Exception as e:
            print("Failed to launch Chrome.")
            print(f"  {e}")
            print("If it mentions the profile is in use, close any Chrome window")
            print("this script opened previously, then try again.")
            sys.exit(1)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Make sure both sites are logged in (pauses once if not). Persists after.
        ensure_logged_in(ctx, page)

        prompts = read_clickup_prompts(page)

        if not prompts:
            print("\nNo prompts found. See the DEBUG output above (a screenshot")
            print("and the page text were saved to the ./debug folder).")
            ctx.close()
            return

        print(f"\nFound {len(prompts)} prompt(s):")
        for i, pr in enumerate(prompts, 1):
            print(f"  {i}. {pr}")

        # One tab per ticket: open a fresh Niural tab for each prompt and submit.
        tabs = []
        for i, pr in enumerate(prompts, 1):
            tab = open_prompt_in_tab(ctx, pr, i, len(prompts))
            tabs.append(tab)
            time.sleep(1)  # brief gap so tabs open cleanly

        print("\n" + "=" * 70)
        print(f"Opened {len(tabs)} tab(s) -- one per ticket -- and submitted each")
        print("prompt to Niural. All tabs (plus the ClickUp tab) are staying OPEN")
        print("so you can review every response.")
        print("=" * 70)
        print("\nNOTHING will be closed until you say so here.")
        input("When you're done reviewing, press Enter here to close all tabs "
              "and exit... ")
        ctx.close()


if __name__ == "__main__":
    main()
