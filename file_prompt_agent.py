#!/usr/bin/env python3
"""
File Prompt Agent
=================
Reads prompts from a .txt file (one prompt per line) and sends them to your
Niural "Ask Emma" box -- ONE prompt after another, in the SAME tab (a running
conversation). It waits for each response before sending the next.

Put your prompt files in the ./promptfolder folder, e.g.:
    promptfolder/prompt1.txt
    promptfolder/prompt2.txt
Each line in a file is one prompt. Blank lines are ignored.

--------------------------------------------------------------------------------
HOW TO RUN
--------------------------------------------------------------------------------
Pick a file interactively (it lists what's in promptfolder):
    python3 file_prompt_agent.py

Or name the file directly (either of these works):
    python3 file_prompt_agent.py prompt1.txt
    python3 file_prompt_agent.py promptfolder/prompt2.txt

It uses the SAME Chrome automation profile as prompt_agent.py, so if you already
logged in to Niural there, you stay logged in here too. Nothing closes until you
press Enter in the terminal.
"""

import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
from niural_auth import auto_login

# Show output live instead of buffering it until exit.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# ==============================================================================
# CONFIG
# ==============================================================================
NIURAL_URL = "https://www.qa.niural.com/7fc11251-ecb3-470e-8d62-c7a2b77b63c6/niural-ai"

CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Same automation profile as prompt_agent.py -> shared login.
AUTOMATION_PROFILE_DIR = Path(__file__).parent / "chrome_automation"

# Folder holding your prompt .txt files.
PROMPT_FOLDER = Path(__file__).parent / "promptfolder"

# How long to wait (seconds) for Emma to answer before sending the next prompt.
RESPONSE_WAIT_SECONDS = 20

# Leave None for auto-detection of the "Ask me anything..." box.
NIURAL_INPUT_SELECTOR = None
NIURAL_BOX_SELECTOR = (
    "textarea[placeholder*='Ask' i], input[placeholder*='Ask' i], "
    "[placeholder*='Ask' i], div[contenteditable='true'], "
    "[contenteditable='true'], [role='textbox'], textarea"
)
# ==============================================================================


def choose_prompt_file():
    """Return the Path of the prompt file to use (from arg or a terminal menu)."""
    PROMPT_FOLDER.mkdir(parents=True, exist_ok=True)

    # 1) If a file was given on the command line, use it.
    if len(sys.argv) > 1:
        raw = sys.argv[1]
        for cand in (Path(raw), PROMPT_FOLDER / raw, Path.cwd() / raw):
            if cand.is_file():
                return cand
        print(f"File not found: {raw}")
        sys.exit(1)

    # 2) Otherwise list the .txt files in promptfolder and let the user pick.
    files = sorted(PROMPT_FOLDER.glob("*.txt"))
    if not files:
        print(f"No .txt files found in {PROMPT_FOLDER}.")
        print("Add a file (one prompt per line) and run again.")
        sys.exit(1)

    print("\nPrompt files in promptfolder:")
    for i, f in enumerate(files, 1):
        n = sum(1 for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip())
        print(f"  {i}. {f.name}   ({n} prompt(s))")

    choice = input("\nPick a file by number (or type a path): ").strip()
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(files):
            return files[idx]
        print("That number isn't in the list.")
        sys.exit(1)
    # Treat the input as a path/name.
    for cand in (Path(choice), PROMPT_FOLDER / choice, Path.cwd() / choice):
        if cand.is_file():
            return cand
    print(f"File not found: {choice}")
    sys.exit(1)


def read_prompts(path: Path):
    """One prompt per non-blank line."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip()]


def prepare_profile():
    """Dedicated automation profile (shared with prompt_agent.py). Persists login."""
    AUTOMATION_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return str(AUTOMATION_PROFILE_DIR)


def find_niural_input(page, timeout=45000):
    """Wait for the 'Ask me anything...' box to render, then return it."""
    sel = NIURAL_INPUT_SELECTOR or NIURAL_BOX_SELECTOR
    try:
        page.wait_for_selector(sel, state="visible", timeout=timeout)
    except Exception:
        return None
    return page.locator(sel).first


def looks_like_login(page) -> bool:
    """True if we're NOT cleanly signed in: a login page OR a sign-in error."""
    url = (page.url or "").lower()
    if "/login" in url or "/auth" in url:
        return True
    try:
        head = (page.evaluate("document.body.innerText") or "").lower()[:500]
    except Exception:
        head = ""
    signals = [
        "log in to niural", "forgot password", "sign in using sso",
        # Niural sign-in error screen (e.g. after an account switch / stale session)
        "unable to sign you in", "request failed with status code", "back to login",
    ]
    return any(s in head for s in signals)


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


def submit_prompt(page, prompt) -> bool:
    """Type one prompt into the box and submit it (Enter, with button fallback)."""
    box = find_niural_input(page)
    if box is None:
        print("   !! Could not find the 'Ask me anything' box.", flush=True)
        return False
    try:
        box.click()
        page.keyboard.type(prompt, delay=8)
        time.sleep(0.4)
        page.keyboard.press("Enter")
        time.sleep(0.8)
        if _box_still_has_text(box):
            if _click_send_button(page):
                print("   submitted (clicked send button).", flush=True)
                return True
            print("   typed, but couldn't confirm submit -- check the tab.",
                  flush=True)
            return False
        print("   submitted (hit Enter).", flush=True)
        return True
    except Exception as e:
        print(f"   !! failed to type/submit: {e}", flush=True)
        return False


def main():
    prompt_file = choose_prompt_file()
    prompts = read_prompts(prompt_file)
    if not prompts:
        print(f"{prompt_file.name} has no prompts (all lines blank).")
        return

    print(f"\nUsing file: {prompt_file}")
    print(f"Found {len(prompts)} prompt(s):")
    for i, pr in enumerate(prompts, 1):
        print(f"  {i}. {pr}")

    automation_dir = prepare_profile()

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=automation_dir,
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

        # Open Niural once; this single tab is reused for every prompt.
        print("\nOpening Niural...", flush=True)
        page.goto(NIURAL_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)

        if looks_like_login(page):
            print("\n" + "=" * 70)
            print("NIURAL LOGIN NEEDED (in the Chrome window that just opened):")
            print("  1) If you see an error, click 'Back to login'.")
            print("  2) Enter your email + password and click 'Log In'.")
            print("  3) If it asks 'Select your role', click your role (e.g. Org owner).")
            print("  4) Open 'Ask Emma' so you can see the 'Ask me anything' box.")
            print("Then come back here and press Enter (saved for next time).")
            print("=" * 70, flush=True)
            input("Press Enter once Ask Emma is open and ready... ")
            page.goto(NIURAL_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)

        # Send each prompt in the SAME tab, one after another.
        for i, pr in enumerate(prompts, 1):
            print("\n" + "-" * 70)
            print(f"[{i}/{len(prompts)}] Sending to Niural (same tab):")
            print(f"   {pr}", flush=True)
            submit_prompt(page, pr)
            if i < len(prompts):
                print(f"   waiting {RESPONSE_WAIT_SECONDS}s for Emma to respond...",
                      flush=True)
                time.sleep(RESPONSE_WAIT_SECONDS)

        print("\n" + "=" * 70)
        print(f"Done -- sent {len(prompts)} prompt(s) in one tab.")
        print("The browser stays OPEN so you can review the conversation.")
        print("=" * 70)
        input("\nPress Enter here to close the browser and exit... ")
        ctx.close()


if __name__ == "__main__":
    main()
