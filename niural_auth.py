#!/usr/bin/env python3
"""
Niural auth helpers -- shared by the prompt scripts.

Main job: when a Niural session has expired (the "Unable to sign you in" error
or a login page), log back in automatically using the credentials in
niural_config.py, then land on Ask Emma. If anything goes wrong, the caller
falls back to a manual login pause.
"""

import re
import time


def load_credentials():
    """Return (email, password, role, org). password is None if not set yet."""
    try:
        import niural_config as cfg
    except Exception:
        return (None, None, None, None)
    email = getattr(cfg, "NIURAL_EMAIL", "") or ""
    password = getattr(cfg, "NIURAL_PASSWORD", "") or ""
    role = getattr(cfg, "NIURAL_ROLE", "") or ""
    org = getattr(cfg, "NIURAL_ORG", "") or ""
    # Treat the placeholder as "not set".
    if not password or "PUT-YOUR" in password:
        password = None
    return (email or None, password or None, role or None, org or None)


def looks_like_login(page) -> bool:
    """True if NOT cleanly signed in: a login page OR a sign-in error screen."""
    url = (page.url or "").lower()
    if "/login" in url or "/auth" in url:
        return True
    try:
        head = (page.evaluate("document.body.innerText") or "").lower()[:600]
    except Exception:
        head = ""
    signals = [
        "log in to niural", "forgot password", "sign in using sso",
        "unable to sign you in", "request failed with status code",
        "back to login",
    ]
    return any(s in head for s in signals)


def _click_if_present(page, selectors, timeout=2500):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def auto_login(page, niural_url) -> bool:
    """
    Try to log back in automatically. Returns True if we end up signed in
    (Ask Emma reachable), False if we couldn't (caller should pause for manual).
    """
    email, password, role, org = load_credentials()
    if not email or not password:
        print("   (auto re-login skipped: set NIURAL_PASSWORD in niural_config.py)",
              flush=True)
        return False

    print("   session expired -> auto re-logging in...", flush=True)
    try:
        # 1) If we're on the error screen, go back to the login form.
        _click_if_present(page, [
            "button:has-text('Back to login')",
            "text=Back to login",
        ])
        time.sleep(1.5)

        # 2) Fill email + password.
        email_box = page.locator(
            "input[type='email'], input[name='email'], "
            "input[placeholder*='@'], input[placeholder*='example']"
        ).first
        email_box.wait_for(state="visible", timeout=20000)
        email_box.fill(email)

        pw_box = page.locator("input[type='password']").first
        pw_box.wait_for(state="visible", timeout=10000)
        pw_box.fill(password)

        # 3) Click "Log In".
        clicked = _click_if_present(page, [
            "button:has-text('Log In')",
            "button:has-text('Log in')",
            "button[type='submit']",
        ])
        if not clicked:
            try:
                page.get_by_role("button", name=re.compile("log ?in", re.I)).first.click()
            except Exception:
                pass
        time.sleep(4)

        # 4) Handle the "Select your role" screen, if it appears.
        if role:
            try:
                # Click the card that shows the role name.
                page.get_by_text(role, exact=False).first.click(timeout=8000)
                time.sleep(3)
            except Exception:
                pass  # maybe no role screen for this account

        # 5) Land on Ask Emma.
        page.goto(niural_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

        ok = not looks_like_login(page)
        print("   auto re-login " + ("succeeded." if ok else "did not stick."),
              flush=True)
        return ok
    except Exception as e:
        print(f"   auto re-login error: {e}", flush=True)
        return False
