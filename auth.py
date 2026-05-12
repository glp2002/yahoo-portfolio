"""Yahoo Finance authentication via Playwright with a persistent session."""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright


def _log(msg: str) -> None:
    print(f"[YP {time.strftime('%H:%M:%S')}] auth: {msg}", flush=True)


SESSION_DIR = Path(__file__).parent / ".yahoo_session"
PORTFOLIOS_URL = "https://finance.yahoo.com/portfolios"


def is_auth_url(url: str) -> bool:
    """True if the URL is on Yahoo's login or consent flow."""
    lower = (url or "").lower()
    return any(s in lower for s in (
        "login.yahoo.com",
        "guce.yahoo.com",
        "consent.yahoo.com",
    ))


def interactive_login(timeout_seconds: int = 600) -> bool:
    """Open a visible browser and wait for the user to close it."""
    _log("interactive_login: launching visible browser")
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,
            viewport={"width": 1100, "height": 800},
        )
        _log("browser launched, opening page")
        page = context.new_page()
        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            page.goto(PORTFOLIOS_URL, wait_until="domcontentloaded", timeout=30000)
            _log(f"login page landed at: {page.url}")
        except Exception as e:
            _log(f"initial goto error (ignored): {type(e).__name__}: {e}")

        _log("waiting for user to close the browser window...")
        deadline = time.time() + timeout_seconds
        closed_by_user = False
        while time.time() < deadline:
            try:
                page.evaluate("1")
            except Exception as e:
                _log(f"liveness check failed -> browser closed ({type(e).__name__})")
                closed_by_user = True
                break
            time.sleep(1)

        _log(f"interactive_login returning closed_by_user={closed_by_user}")
        try:
            context.close()
        except Exception:
            pass
        return closed_by_user
