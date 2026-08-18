"""
DOWNLOADER.PY
=============
Logs into https://remittance.bankalbilad.com using Selenium (Chrome browser),
then directly constructs file URLs (no folder browsing needed) and
downloads new TXT files into the input/ folder so app.py picks them up
automatically.

Uses a separate Chrome profile from WhatsApp so both can run simultaneously.

FILE URL PATTERN:
    https://remittance.bankalbilad.com/Esewa/13994464.YYYYMMDD.NNNNN.TXT

We track the last downloaded sequence number per date in
download_state.json, and after logging in we just try
last_seq+1, last_seq+2, ... directly via URL until we hit a 404 /
"file not found" page.
"""

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import config
import network_check
import alerts

logger = logging.getLogger(__name__)

# Where we dump raw diagnostics (status code, headers, body snippet) any
# time file-existence detection is ambiguous or a text-marker firing isn't
# corroborated by the HTTP status code. Lets us actually see what the
# portal returned instead of guessing after the fact.
DEBUG_LOG_DIR = config.BASE_DIR / "debug_logs" / "downloader"

# Separate Chrome profile so it doesn't conflict with WhatsApp's browser.
DOWNLOAD_CHROME_PROFILE = config.BASE_DIR / "chrome_profile_downloader"

LOGIN_URL = "https://remittance.bankalbilad.com/_forms/default.aspx"
FILE_URL_TEMPLATE = "https://remittance.bankalbilad.com/Esewa/{username}.{date_str}.{seq:05d}.TXT"

USERNAME_FIELD_ID = "ctl00_PlaceHolderMain_signInControl_UserName"
PASSWORD_FIELD_ID = "ctl00_PlaceHolderMain_signInControl_password"

WAIT = 20   # seconds to wait for page elements

DOWNLOAD_STATE_FILE = config.BASE_DIR / "download_state.json"


# ---------------------------------------------------------------------------
# BROWSER SETUP
# ---------------------------------------------------------------------------

def _create_browser():
    """Open a Chrome browser configured to auto-download files to input/."""
    lock = DOWNLOAD_CHROME_PROFILE / "SingletonLock"
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass

    prefs = {
        "download.default_directory": str(config.INPUT_DIR.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": False,
        "safebrowsing.disable_download_protection": True,
        # Force .txt to download instead of opening in browser preview.
        "plugins.always_open_pdf_externally": True,
    }

    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={DOWNLOAD_CHROME_PROFILE}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--window-size=1200,800")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-extensions")
    options.add_experimental_option("prefs", prefs)

    try:
        driver = webdriver.Chrome(options=options)
        return driver
    except Exception:
        logger.exception("Could not start download browser.")
        return None


# ---------------------------------------------------------------------------
# LOGIN
# ---------------------------------------------------------------------------

def _login(driver) -> bool:
    """Navigate to the login page and sign in. Returns True on success."""
    logger.info("Navigating to Bank Albilad portal...")
    driver.get(LOGIN_URL)

    try:
        username_field = WebDriverWait(driver, WAIT).until(
            EC.presence_of_element_located((By.ID, USERNAME_FIELD_ID))
        )
        username_field.clear()
        username_field.send_keys(config.DOWNLOAD_USERNAME)

        password_field = driver.find_element(By.ID, PASSWORD_FIELD_ID)
        password_field.clear()
        password_field.send_keys(config.DOWNLOAD_PASSWORD)

        login_button = WebDriverWait(driver, WAIT).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "a.ms-SPButton, input[type='submit'], "
                "a[id*='login'], input[id*='login'], "
                "a[id*='signIn'], input[id*='signIn']"
            ))
        )
        login_button.click()
        time.sleep(2)

        if "signInControl$UserName" in driver.page_source:
            driver.execute_script(
                "document.querySelector('[id*=\"login\"]').click();"
            )
            time.sleep(2)

        if "signInControl$UserName" in driver.page_source:
            logger.error(
                "Login failed. Check DOWNLOAD_USERNAME / DOWNLOAD_PASSWORD "
                "in config.py."
            )
            alerts.queue_alert(
                "portal_login_failed",
                "⚠️ Bank Albilad portal login failed — please check "
                "DOWNLOAD_USERNAME / DOWNLOAD_PASSWORD in config.py. File "
                "downloads are blocked until this is fixed."
            )
            return False

        logger.info("Logged in to Bank Albilad portal.")
        alerts.clear_cooldown("portal_login_failed")
        alerts.clear_cooldown("portal_down")
        return True

    except Exception:
        logger.exception("Error during login.")
        alerts.queue_alert(
            "portal_down",
            "⚠️ The Bank Albilad portal did not load / respond during login. "
            "It may be down or unreachable — file downloads are paused until "
            "it's back."
        )
        return False


# ---------------------------------------------------------------------------
# RAW HTTP SESSION (built from the logged-in Selenium browser's cookies)
# ---------------------------------------------------------------------------
#
# WHY: file-existence used to be decided by scanning the browser's rendered
# page_source for phrases like "the file or folder" / "cannot be found".
# This portal is a SharePoint/ASP.NET WebParts site (the ctl00_PlaceHolderMain_
# field IDs give it away), and those platforms ship huge client-side resource
# bundles that contain hundreds of hardcoded error-message strings baked into
# EVERY page's JavaScript, whether or not an error actually happened. A file
# that genuinely exists can still have one of those boilerplate phrases
# sitting inertly in a <script> tag, which was almost certainly why 00002
# got reported as "does not exist yet" despite being visible in the portal.
#
# FIX: use the browser's own auth cookies to make a direct HTTP request and
# decide existence from the actual status code, not from text sniffing.

def _build_session_from_driver(driver) -> requests.Session:
    """Create a requests.Session carrying the logged-in browser's cookies,
    so we can check file existence via real HTTP status codes instead of
    scanning rendered page text."""
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(
            cookie.get("name"), cookie.get("value"),
            domain=cookie.get("domain"), path=cookie.get("path", "/"),
        )
    session.headers.update({
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
    })
    return session


def _log_diagnostics(filename: str, reason: str, status_code=None,
                      headers=None, body_snippet: str = "") -> None:
    """Save what the portal actually returned for a file check so an
    ambiguous or surprising result can be inspected afterward instead of
    re-guessed. One JSON file per event, timestamped."""
    try:
        DEBUG_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        record = {
            "timestamp": datetime.now().isoformat(),
            "filename": filename,
            "reason": reason,
            "status_code": status_code,
            "headers": dict(headers) if headers is not None else None,
            "body_snippet": body_snippet[:2000],
        }
        out_path = DEBUG_LOG_DIR / f"{ts}_{filename}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
    except Exception:
        logger.exception("Could not write downloader diagnostics.")


# ---------------------------------------------------------------------------
# FILE EXISTENCE / NOT-FOUND DETECTION
# ---------------------------------------------------------------------------

def _page_indicates_missing_file(driver) -> bool:
    """Return True if the current page looks like a 'file not found' /
    error page rather than an actual file or download trigger."""
    page_source_lower = driver.page_source.lower()

    not_found_markers = [
        "file not found",
        "404",
        "the resource you are looking for",
        "the file or folder",
        "cannot be found",
        "page not found",
        "http 400",
        "http 404",
        "object reference not set",
    ]

    for marker in not_found_markers:
        if marker in page_source_lower:
            return True

    # If we got redirected back to login, treat as "can't verify" — not a
    # missing-file case, but caller should stop and re-login.
    return False


def _page_indicates_login_required(driver) -> bool:
    """Return True if we got bounced back to the login page (session
    expired)."""
    return "signInControl$UserName" in driver.page_source


# ---------------------------------------------------------------------------
# DOWNLOAD ONE FILE BY DIRECT URL
# ---------------------------------------------------------------------------

def _download_file(driver, date_str: str, seq: int, session: requests.Session = None) -> str:
    """Check whether a file exists and download it if so.

    Primary path: a direct HTTP request using the browser's own auth
    cookies (`session`). The HTTP status code is the authoritative signal
    for existence — 404 means "not found", 200 with real content means
    "downloaded". This avoids scanning rendered page text, which produced
    false "not found" results on this SharePoint-based portal (its stock
    resource-script bundles contain boilerplate error-message strings on
    EVERY page, not just error pages).

    Falls back to the old browser-navigation approach only if no session
    was supplied or the HTTP request itself fails (e.g. network hiccup),
    so behavior degrades gracefully rather than breaking outright.

    Returns:
        "downloaded"   - file existed and was saved to input/
        "not_found"    - file doesn't exist yet at this sequence (normal,
                          means we've reached the end of today's files)
        "login_needed" - session expired, caller should stop and re-login
        "failed"       - file seemed to exist but didn't download properly
    """
    filename = f"{config.DOWNLOAD_USERNAME}.{date_str}.{seq:05d}.TXT"
    url = FILE_URL_TEMPLATE.format(
        username=config.DOWNLOAD_USERNAME, date_str=date_str, seq=seq
    )
    dest = config.INPUT_DIR / filename

    # Remove any leftover partial/.crdownload file from a previous attempt.
    for leftover in config.INPUT_DIR.glob(f"{filename}*"):
        try:
            leftover.unlink()
        except OSError:
            pass

    logger.info("Checking %s ...", filename)

    if session is not None:
        result = _download_file_via_http(session, url, filename, dest)
        if result is not None:
            return result
        logger.warning(
            "HTTP check for %s failed unexpectedly — falling back to "
            "browser-based check.", filename
        )

    return _download_file_via_browser(driver, url, filename, dest)


def _download_file_via_http(session: requests.Session, url: str, filename: str, dest: Path):
    """Existence/download check using a direct HTTP request with the
    browser's auth cookies. Returns one of the _download_file result
    strings, or None if the request itself failed (caller should fall
    back to the browser-based check)."""
    try:
        response = session.get(url, timeout=30, allow_redirects=True)
    except requests.RequestException:
        logger.exception("HTTP request failed while checking %s.", filename)
        return None

    # A bounce back to the login page shows up as a redirect to the forms
    # login URL, or login markup appearing in the final response body.
    if "signInControl$UserName" in response.text or "_forms/default.aspx" in response.url:
        logger.warning("Session expired (HTTP) while checking %s.", filename)
        return "login_needed"

    if response.status_code == 404:
        logger.info("%s does not exist yet (HTTP 404 — end of today's files).", filename)
        return "not_found"

    if response.status_code != 200:
        logger.warning(
            "Unexpected HTTP %d while checking %s.", response.status_code, filename
        )
        _log_diagnostics(
            filename, f"unexpected_status_{response.status_code}",
            status_code=response.status_code, headers=response.headers,
            body_snippet=response.text,
        )
        return "failed"

    content_type = response.headers.get("Content-Type", "").lower()
    body = response.content

    # A 200 that's actually an HTML error/redirect page (rather than the
    # TXT file) is the ambiguous case that used to be decided by fragile
    # text-marker sniffing. Treat it as "not found" ONLY if it also looks
    # like an HTML document AND has one of the specific not-found markers,
    # and log full diagnostics either way so this can be verified, not
    # assumed.
    looks_like_html = "text/html" in content_type or body.strip()[:15].lower().startswith(b"<!doctype") \
        or body.strip()[:5].lower().startswith(b"<html")

    if looks_like_html:
        text_lower = body.decode("utf-8", errors="ignore").lower()
        if _text_has_missing_file_marker(text_lower):
            logger.info("%s does not exist yet (HTML not-found page).", filename)
            _log_diagnostics(filename, "html_not_found_marker",
                              status_code=response.status_code,
                              headers=response.headers,
                              body_snippet=text_lower)
            return "not_found"

        # 200 + HTML but no recognizable not-found marker: this is exactly
        # the ambiguous case that previously caused false negatives if a
        # marker happened to appear elsewhere in boilerplate script. Log it
        # and treat as failed rather than silently guessing either way.
        logger.warning(
            "%s returned HTML with no clear existence signal — treating as "
            "unconfirmed, not not-found. Diagnostics saved.", filename
        )
        _log_diagnostics(filename, "ambiguous_html_response",
                          status_code=response.status_code,
                          headers=response.headers,
                          body_snippet=text_lower)
        return "failed"

    # Not HTML, not a 404 — this is the actual file content.
    if len(body) == 0:
        logger.warning("%s returned 200 with empty body.", filename)
        _log_diagnostics(filename, "empty_body_200",
                          status_code=response.status_code,
                          headers=response.headers)
        return "failed"

    try:
        dest.write_bytes(body)
    except OSError:
        logger.exception("Could not write %s to disk.", filename)
        return "failed"

    logger.info("Downloaded (via HTTP): %s", filename)
    return "downloaded"


def _text_has_missing_file_marker(text_lower: str) -> bool:
    """Stricter not-found detection for use on a confirmed HTML error page
    (already gated by content-type/doctype in the caller) — same marker
    list as before, but only ever consulted after we already know we're
    looking at an HTML document, not arbitrary page_source that might
    contain unrelated boilerplate script."""
    not_found_markers = [
        "file not found", "the resource you are looking for",
        "the file or folder", "cannot be found", "page not found",
        "http 400", "http 404", "object reference not set",
    ]
    return any(marker in text_lower for marker in not_found_markers)


def _download_file_via_browser(driver, url: str, filename: str, dest: Path) -> str:
    """Old approach: navigate the Selenium browser directly to the file URL
    and watch for either a download landing in input/ or not-found page
    text. Kept as a fallback for when the HTTP session path isn't usable."""
    driver.get(url)
    time.sleep(1.5)  # let the browser start the download or show an error page

    if _page_indicates_login_required(driver):
        logger.warning("Session expired while checking %s.", filename)
        return "login_needed"

    if _page_indicates_missing_file(driver):
        logger.info("%s does not exist yet (end of today's files).", filename)
        _log_diagnostics(filename, "browser_not_found_marker",
                          body_snippet=driver.page_source)
        return "not_found"

    # Wait for the actual download to complete (file appears in input/).
    for _ in range(20):
        if dest.exists() and dest.stat().st_size > 0:
            # Make sure Chrome isn't still writing it (.crdownload check).
            if not any(config.INPUT_DIR.glob(f"{filename}.crdownload")):
                logger.info("Downloaded: %s", filename)
                return "downloaded"
        time.sleep(1)

    # If the file never appeared but the page also didn't show a clear
    # "not found" message, it may have rendered the TXT content directly
    # in the browser instead of downloading (some servers do this for
    # text/plain). In that case, save the page's raw text as the file.
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        if body_text and len(body_text.strip()) > 0 and not _page_indicates_missing_file(driver):
            dest.write_text(body_text, encoding="utf-8")
            logger.info("Saved %s from displayed page text.", filename)
            return "downloaded"
    except Exception:
        pass

    logger.warning("Could not confirm download for %s.", filename)
    _log_diagnostics(filename, "browser_unconfirmed", body_snippet=driver.page_source)
    return "failed"


# ---------------------------------------------------------------------------
# STATE TRACKING (last sequence number downloaded per date)
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if not DOWNLOAD_STATE_FILE.exists():
        return {}
    try:
        with DOWNLOAD_STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with DOWNLOAD_STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        logger.warning("Could not save download_state.json.")


# How far we're willing to probe forward when we don't yet know where
# today's file numbering actually starts. Doubling steps (1, 2, 4, 8...)
# mean this only takes a handful of requests even if today's files start
# in the hundreds or low thousands.
PROBE_SAFETY_CAP = 20000


def _probe_for_first_existing(driver, date_str: str, confirmed_missing_seq: int,
                               session: requests.Session = None):
    """Find the smallest existing sequence number for date_str, without
    assuming file numbering starts at 00001.

    `confirmed_missing_seq` must already be known NOT to exist (the
    caller just checked it). We expand outward from there in doubling
    steps until we hit a sequence that DOES exist, then binary-search
    back to find the smallest one that exists.

    As a side effect, every sequence actually checked that exists gets
    downloaded too (checking = downloading here), so nothing is wasted.

    Returns:
        (seq, downloaded_seqs) where seq is the smallest existing
        sequence found (or None if nothing exists for this date within
        the safety cap), and downloaded_seqs is the set of sequence
        numbers already saved to input/ during the probe.
        Returns ("login_needed", downloaded_seqs) if the session expired
        mid-probe — caller should re-login and retry.
    """
    downloaded_seqs = set()
    lo = confirmed_missing_seq
    hi = None
    step = 1

    while lo + step <= PROBE_SAFETY_CAP:
        candidate = lo + step
        result = _download_file(driver, date_str, candidate, session=session)
        if result == "downloaded":
            downloaded_seqs.add(candidate)
            hi = candidate
            break
        if result == "login_needed":
            return "login_needed", downloaded_seqs
        # "not_found" or "failed" — keep expanding outward.
        lo = candidate
        step *= 2

    if hi is None:
        return None, downloaded_seqs

    # Binary search the gap between lo (confirmed missing) and hi
    # (confirmed existing) for the smallest existing sequence.
    while hi - lo > 1:
        mid = (lo + hi) // 2
        result = _download_file(driver, date_str, mid, session=session)
        if result == "login_needed":
            return "login_needed", downloaded_seqs
        if result == "downloaded":
            downloaded_seqs.add(mid)
            hi = mid
        else:
            lo = mid

    return hi, downloaded_seqs


# ---------------------------------------------------------------------------
# NETWORK MONITORING
# ---------------------------------------------------------------------------
# Portal reachability runs on its own timer (config.NETWORK_CHECK_INTERVAL_
# SECONDS, default 15 min) via run_periodic_network_check() — call that from
# scheduler.py's main loop. It runs independently of download windows, so a
# problem is caught even during long gaps between scheduled slots.
#
# LAN/IP status is different: check_and_download() does a FRESH check every
# time it's called (every scheduler tick, e.g. 30s, while inside a download
# window) rather than trusting the periodic check's cached result. Relying
# on the 15-minute cadence alone meant that if LAN dropped shortly before a
# download window and came back up mid-window, the stale "down" reading
# could block the ENTIRE window — the periodic check might not run again
# until well after the window had already closed, so the file simply never
# got downloaded until the process was restarted (which forces one
# immediate check). A LAN check itself is cheap, so doing it fresh on every
# download attempt costs nothing and closes that gap.

_lan_ok = True  # optimistic default until the first check runs


def _check_lan_status_and_alert() -> bool:
    """Do a FRESH LAN/IP check right now (never uses a cached result),
    queue/clear the relevant WhatsApp alerts, update the shared _lan_ok
    flag (for logging/visibility elsewhere), and return whether LAN is
    currently usable. Cheap enough to call before every real download
    attempt, not just on the periodic timer."""
    global _lan_ok

    status = network_check.get_network_status()

    if not status["psutil_available"]:
        _lan_ok = True  # can't check — don't block on it
        return True

    if not status["lan_connected"]:
        _lan_ok = False
        if status["wifi_connected"]:
            logger.warning("LAN is disconnected — only Wi-Fi is active.")
            alerts.queue_alert(
                "lan_disconnected",
                "⚠️ LAN (Ethernet) connection is down — only Wi-Fi is active "
                "right now. The Bank Albilad portal requires the LAN "
                "connection, so file downloads won't work until it's "
                "reconnected."
            )
        else:
            logger.warning("No network connection detected (LAN or Wi-Fi).")
            alerts.queue_alert(
                "lan_disconnected",
                "⚠️ No network connection detected (LAN or Wi-Fi). File "
                "downloads from the Bank Albilad portal will not work until "
                "connectivity is restored."
            )
        return False

    alerts.clear_cooldown("lan_disconnected")

    if status["ip_matches_expected"] is False:
        _lan_ok = False
        logger.warning("LAN IP is %s, expected %s.", status["lan_ip"], config.EXPECTED_LAN_IP)
        alerts.queue_alert(
            "lan_ip_mismatch",
            f"⚠️ LAN IP is {status['lan_ip']}, but {config.EXPECTED_LAN_IP} "
            f"was expected. The Bank Albilad portal may not open correctly "
            f"from this connection."
        )
        return False

    if status["ip_matches_expected"] is True:
        alerts.clear_cooldown("lan_ip_mismatch")

    _lan_ok = True
    return True


def run_periodic_network_check() -> None:
    """Check LAN connectivity/IP (fresh) and portal reachability, queue
    WhatsApp alerts for any problem found. Call this on a fixed interval
    from scheduler.py's idle loop — it's what catches problems during
    long gaps between download windows. (Download attempts themselves
    call _check_lan_status_and_alert() directly for a fresh reading
    rather than waiting for this timer — see module docstring above.)
    """
    if not _check_lan_status_and_alert():
        return

    # Also sweep portal reachability here so "site is down" is caught
    # even between download windows, not just when check_and_download()
    # happens to run.
    if network_check.is_portal_reachable():
        alerts.clear_cooldown("portal_down")
    else:
        logger.warning("Bank Albilad portal is not reachable.")
        alerts.queue_alert(
            "portal_down",
            "⚠️ The Bank Albilad remittance portal is not responding. It "
            "may be down or under maintenance — file downloads are paused "
            "until it's back."
        )


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------------------------

def check_and_download():
    """Log in and download any new TXT files for today by trying
    sequence numbers directly via URL.

    Starts from (last_downloaded_sequence + 1) and keeps trying higher
    sequence numbers until a "not found" result is returned.

    Returns:
        None if blocked before even attempting a portal check (LAN down
        or LAN IP mismatch, checked fresh right now) — i.e. we genuinely
        don't know whether a file exists, so callers shouldn't report
        "no file found".
        Otherwise, the number of new files downloaded (0 = portal was
        actually checked and nothing new was there).
    """
    if not _check_lan_status_and_alert():
        logger.info("Skipping portal check — LAN is down right now.")
        return None

    today_str = date.today().strftime("%Y%m%d")
    state = _load_state()
    have_state_for_today = today_str in state
    start_seq = state.get(today_str, 0) + 1

    logger.info(
        "Checking for new files — date=%s, starting from sequence %05d",
        today_str, start_seq
    )

    driver = _create_browser()
    if driver is None:
        return None

    downloaded = 0
    interrupted = False

    try:
        if not _login(driver):
            return None

        session = _build_session_from_driver(driver)

        seq = start_seq
        first_check = True

        while True:
            result = _download_file(driver, today_str, seq, session=session)

            if result == "downloaded":
                state[today_str] = seq
                _save_state(state)
                downloaded += 1
                seq += 1
                first_check = False
                continue

            if result == "not_found":
                # Only worth an expanded search on the very FIRST check of
                # the day, when we don't actually know where today's file
                # numbering starts (it may not start at 00001 — that's
                # what was causing every run to give up immediately after
                # checking just 00001). Once we've found at least one
                # file today, "not_found" at last+1 is the normal,
                # expected "nothing new yet" case and needs no probing —
                # doing this on every routine 30s poll would hammer the
                # portal with extra requests for no reason.
                if first_check and not have_state_for_today:
                    logger.info(
                        "No file at sequence %05d — probing further in case "
                        "today's numbering doesn't start at 00001...", seq
                    )
                    probe_result, probed = _probe_for_first_existing(
                        driver, today_str, seq, session=session
                    )

                    if probe_result == "login_needed":
                        if not _login(driver):
                            interrupted = True
                            break
                        session = _build_session_from_driver(driver)
                        probe_result, probed = _probe_for_first_existing(
                            driver, today_str, seq, session=session
                        )

                    if probe_result is None:
                        logger.info("No files exist yet for %s.", today_str)
                        break

                    logger.info("Found today's files starting at sequence %05d.", probe_result)
                    # Let the normal sequential path below do the actual
                    # state-update/counting for every file from here on —
                    # simpler than trying to reconcile which sequences the
                    # probe already touched, and re-checking a couple of
                    # already-confirmed sequences is harmless.
                    seq = probe_result
                    first_check = False
                    continue

                break  # reached the end of today's available files

            if result == "login_needed":
                logger.info("Re-logging in...")
                if not _login(driver):
                    interrupted = True
                    break
                session = _build_session_from_driver(driver)
                continue  # retry the same seq after re-login

            if result == "failed":
                logger.warning("Stopping after a failed download attempt for sequence %05d.", seq)
                interrupted = True
                break

        if downloaded == 0:
            logger.info("No new files found for %s.", today_str)
        else:
            logger.info("Downloaded %d new file(s) for %s.", downloaded, today_str)

    except Exception:
        logger.exception("Unexpected error in check_and_download.")

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if downloaded == 0 and interrupted:
        # We never got a clean "checked, nothing there" result — a
        # failure interrupted the check, so we genuinely don't know.
        return None

    return downloaded
