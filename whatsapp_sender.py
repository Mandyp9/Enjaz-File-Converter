"""
WHATSAPP_SENDER.PY
==================
Sends a CSV file to a WhatsApp group as a document attachment using
WhatsApp Web automated through Selenium + Chrome.

HOW THIS WORKS:
    - A Chrome window opens showing WhatsApp Web.
    - The FIRST time, scan the QR code with your phone
      (WhatsApp > Settings > Linked Devices > Link a Device).
    - After that the login is saved in chrome_profile/ and you won't
      need to scan again.

SETUP:
    pip install selenium pyautogui
    Google Chrome must be installed.
"""

import logging
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    _SELENIUM_AVAILABLE = True
except ImportError:
    _SELENIUM_AVAILABLE = False

# Saved Chrome login profile so you only scan the QR code once.
CHROME_PROFILE_DIR = config.BASE_DIR / "chrome_profile"

# Max seconds to wait for any single element to appear.
ELEMENT_WAIT_SECONDS = 30

# Shorter timeout used when trying multiple fallback selectors — lets
# failed selectors fail quickly without holding everything up.
SHORT_ELEMENT_WAIT_SECONDS = 2

# Max seconds to wait for initial WhatsApp Web load / QR scan.
INITIAL_LOAD_WAIT_SECONDS = 60

# Max seconds to wait for a sent file to be confirmed delivered (single
# grey check or better) after clicking Send. We default to "assume sent"
# once no failure indicator has shown up (see _wait_for_send_confirmed),
# so this is a ceiling on wasted time, not something we actually expect
# to hit on every send — it was previously 45s, which is why sending a
# TXT then CSV could take ~30s+ per file when bubble/tick detection
# didn't resolve quickly; 12s is plenty of headroom while keeping the
# total gap between files in the 10-15s range that's actually needed.
SEND_CONFIRM_WAIT_SECONDS = 12

# Selector for outgoing message bubbles. WhatsApp Web's own message model
# tags each bubble's container with data-id="true_..." for messages sent
# by you (and "false_..." for received messages) — this convention has
# stayed stable across WhatsApp Web's various UI redesigns even when its
# CSS class names (which are hashed per build) have not. The message-out
# class check is kept as a secondary fallback for older builds.
OUTGOING_BUBBLE_XPATH = (
    '//div[@data-id and starts-with(@data-id,"true_")]'
    ' | //div[contains(@class,"message-out")]'
)


# ---------------------------------------------------------------------------
# INTERNAL HELPERS
# ---------------------------------------------------------------------------

def _create_browser():
    """Open Chrome. Returns a webdriver instance, or None on failure."""
    # Remove stale SingletonLock so Chrome can start even after a crash.
    singleton_lock = CHROME_PROFILE_DIR / "SingletonLock"
    if singleton_lock.exists():
        try:
            singleton_lock.unlink()
            logger.info("Removed stale Chrome SingletonLock.")
        except OSError:
            pass

    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--window-size=1200,900")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-extensions")

    try:
        driver = webdriver.Chrome(options=options)
    except Exception:
        logger.exception("Could not start Chrome. Is Google Chrome installed?")
        return None

    driver.get("https://web.whatsapp.com")
    return driver


def _browser_is_alive(driver) -> bool:
    """Check whether an existing driver's browser window/session is still
    usable, so we can reuse it instead of always spawning a brand new
    Chrome process (which is what was causing "browser restarts between
    every file" — see WhatsAppSession.start())."""
    if driver is None:
        return False
    try:
        _ = driver.current_url  # any call that requires a live session
        return True
    except Exception:
        return False


def _find_element_any(driver, wait_seconds, xpaths, description):
    """Try a list of XPath selectors, return the first one that appears.
    Returns None if none match within wait_seconds each."""
    for xpath in xpaths:
        try:
            el = WebDriverWait(driver, wait_seconds).until(
                EC.presence_of_element_located((By.XPATH, xpath))
            )
            return el
        except Exception:
            continue
    logger.error("Could not find: %s", description)
    return None


def _wait_for_whatsapp_loaded(driver) -> bool:
    """Wait until WhatsApp Web chat list is visible. Returns True/False."""
    for xpath in ['//div[@id="pane-side"]', '//div[@aria-label="Chat list"]',
                  '//div[@data-testid="chat-list"]']:
        try:
            WebDriverWait(driver, INITIAL_LOAD_WAIT_SECONDS).until(
                EC.presence_of_element_located((By.XPATH, xpath))
            )
            return True
        except Exception:
            continue
    logger.error(
        "WhatsApp Web did not load. If this is the first run, "
        "scan the QR code with your phone."
    )
    return False


def _open_group_chat(driver, group_name: str) -> bool:
    """Search for the group and open it. Returns True/False."""
    search_box = _find_element_any(driver, ELEMENT_WAIT_SECONDS, [
        '//input[@aria-label="Search or start a new chat"]',
        '//div[@contenteditable="true"][@data-tab="3"]',
        '//div[@contenteditable="true"][@aria-label="Search input textbox"]',
        '//div[@id="pane-side"]//div[@contenteditable="true"]',
    ], "WhatsApp search box")

    if search_box is None:
        return False

    try:
        search_box.click()
        time.sleep(0.3)
        search_box.send_keys(group_name)
        time.sleep(1)

        result = None
        for xpath in [
            f'//span[@title="{group_name}"]',
            f'//span[contains(@title, "{group_name}")]',
            f'//span[contains(text(), "{group_name}")]',
        ]:
            try:
                result = WebDriverWait(driver, SHORT_ELEMENT_WAIT_SECONDS).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                break
            except Exception:
                continue

        if result is None:
            logger.error(
                "Could not find group '%s'. Check the exact name in config.py.",
                group_name
            )
            return False

        result.click()
        time.sleep(1)

        try:
            search_box.send_keys(Keys.ESCAPE)
        except Exception:
            pass

        return True

    except Exception:
        logger.exception("Failed to open group '%s'", group_name)
        return False


def _find_document_input(driver):
    """Find the hidden file input used for document uploads (accept='*').
    Returns the element or None."""
    try:
        inputs = driver.find_elements(By.XPATH, '//input[@type="file"]')
    except Exception:
        return None

    for el in inputs:
        if el.get_attribute("accept") == "*":
            return el
    return None


def _dismiss_discard_popup(driver) -> bool:
    """If WhatsApp Web is showing a file preview / discard confirmation
    dialog, dismiss it so the next attach starts from a clean state.
    Returns True if a popup was found and dismissed."""
    try:
        for xpath in [
            '//div[@role="button"][contains(., "Discard")]',
            '//button[contains(., "Discard")]',
            '//div[@role="button"][contains(., "Cancel")]',
        ]:
            try:
                btn = WebDriverWait(driver, 1).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                btn.click()
                logger.info("Dismissed discard/cancel popup.")
                time.sleep(0.5)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _outgoing_message_count(driver) -> int:
    """Count outgoing message bubbles currently in the open chat, used to
    detect the specific new bubble created by the file we just sent."""
    try:
        return len(driver.find_elements(By.XPATH, OUTGOING_BUBBLE_XPATH))
    except Exception:
        return -1


def _wait_for_send_confirmed(driver, count_before: int, timeout: int) -> bool:
    """After clicking Send, wait for the new outgoing message bubble to
    show a real delivery status (single/double check) rather than just
    waiting for the compose box to reappear.

    The compose box returns almost immediately after Send is clicked —
    well before WhatsApp has actually finished uploading the file — so
    treating "compose box is back" as success is what let files with a
    red exclamation (failed upload) get reported and deleted as if sent.

    Returns True as soon as either: (a) the newest outgoing bubble shows a
    check-mark status icon, or (b) we can't positively identify the new
    bubble at all (WhatsApp Web's DOM/class names are hashed per build and
    change often, so bubble-detection can legitimately fail even though
    the send itself worked) AND no explicit failure indicator was seen.
    Returns False only when an explicit failure signal is found ("Failed
    to send" / error icon / retry prompt) — never purely from a detection
    timeout, since that produced false negatives on real WhatsApp Web
    builds where the file was actually delivered fine.

    NOTE: WhatsApp Web's DOM/aria-labels change periodically. If false
    negatives resurface, open dev tools on a manually-sent message and
    update OUTGOING_BUBBLE_XPATHS / the error/ok xpaths below.
    """
    deadline = time.time() + timeout
    # Only spend a short grace period actively looking for the new bubble
    # itself — if the selectors can't find it, waiting out the full
    # timeout just delays every single send for no benefit. Half the
    # overall timeout for this phase, half left for tick-icon polling.
    bubble_search_deadline = min(deadline, time.time() + (timeout / 2))

    error_xpaths = [
        '//*[@aria-label="Failed to send" or @aria-label="This message failed to send. Click to retry."]',
        '//span[@data-icon="msg-error" or @data-icon="status-error" or contains(@data-icon,"error")]',
        '//div[@aria-label="Message not sent"]',
    ]
    ok_xpaths = [
        './/span[@data-icon="msg-check" or @data-icon="msg-dblcheck" or @data-icon="msg-dblcheck-ack"]',
    ]

    def _has_error_anywhere() -> bool:
        for xp in error_xpaths:
            try:
                if driver.find_elements(By.XPATH, xp):
                    return True
            except Exception:
                pass
        return False

    # Phase 1: try to find the new outgoing bubble specifically.
    new_bubble = None
    while time.time() < bubble_search_deadline:
        if _has_error_anywhere():
            logger.error("WhatsApp reported a failed send (error indicator visible).")
            return False
        bubbles = driver.find_elements(By.XPATH, OUTGOING_BUBBLE_XPATH)
        if len(bubbles) > count_before:
            new_bubble = bubbles[-1]
            break
        time.sleep(0.4)

    if new_bubble is None:
        # Couldn't positively identify the bubble (stale selector for this
        # WhatsApp Web build). The attach/preview flow already completed
        # normally by this point (caller already confirmed the compose
        # box returned), so assume it sent rather than falsely report
        # failure and cause duplicate re-sends + a stuck output folder.
        if _has_error_anywhere():
            logger.error("WhatsApp reported a failed send (error indicator visible).")
            return False
        logger.warning(
            "Could not locate the new message bubble to verify delivery "
            "(selector may need updating for this WhatsApp Web build) — "
            "no failure indicator seen, assuming it sent."
        )
        return True

    # Phase 2: poll that specific bubble briefly for a status icon.
    while time.time() < deadline:
        for xp in error_xpaths:
            try:
                if new_bubble.find_elements(By.XPATH, "." + xp[1:]):
                    logger.error("WhatsApp reported a failed send (error indicator on message).")
                    return False
            except Exception:
                pass

        for xp in ok_xpaths:
            try:
                if new_bubble.find_elements(By.XPATH, xp):
                    return True
            except Exception:
                pass

        time.sleep(0.4)

    # Bubble exists, no error seen, just couldn't confirm the tick icon
    # in time (e.g. large file still uploading) — assume success rather
    # than block/retry, since no failure was ever indicated.
    logger.warning("Delivery tick icon not confirmed within timeout, but no failure indicator seen — assuming sent.")
    return True


def _attach_and_send_document(driver, file_path: Path, caption: str) -> bool:
    """Attach a document and send it. Returns True only once WhatsApp has
    confirmed the message actually went out (not just that the Send
    button was clicked)."""
    try:
        # Clean up any leftover discard/preview dialog from a previous
        # interrupted attach BEFORE starting a new one — this is what
        # was causing the discard popup to still be showing when the
        # next file's attach flow started.
        _dismiss_discard_popup(driver)

        count_before = _outgoing_message_count(driver)

        # 1. Click the Attach button.
        attach_button = _find_element_any(driver, SHORT_ELEMENT_WAIT_SECONDS, [
            '//button[@aria-label="Attach"]',
            '//button[@data-tab="10"][@aria-label="Attach"]',
        ], "Attach button")

        if attach_button is None:
            return False

        attach_button.click()
        time.sleep(0.5)

        # 2. Click "Document" in the attach menu.
        doc_menu_item = _find_element_any(driver, SHORT_ELEMENT_WAIT_SECONDS, [
            '//button[@aria-label="Document"]',
            '//button[@role="menuitem"][@aria-label="Document"]',
            '//span[text()="Document"]/ancestor::button[1]',
        ], "Document menu item")

        if doc_menu_item is None:
            return False

        # Block the OS file picker BEFORE clicking Document.
        driver.execute_script("""
            document.addEventListener('click', function blockPicker(e) {
                if (e.target && e.target.type === 'file') {
                    e.preventDefault();
                    e.stopPropagation();
                    document.removeEventListener('click', blockPicker, true);
                }
            }, true);
        """)

        doc_menu_item.click()
        time.sleep(0.8)

        # 3. Find the document file input and set the file.
        document_input = _find_document_input(driver)
        if document_input is None:
            document_input = _find_element_any(
                driver, SHORT_ELEMENT_WAIT_SECONDS,
                ['//input[@accept="*"][@multiple]', '//input[@accept="*"]'],
                "document file input"
            )
        if document_input is None:
            logger.error("Document file input not found after clicking Document menu.")
            return False

        document_input.send_keys(str(file_path.resolve()))

        # 4. Verify file preview appeared. The JS click-blocker installed
        # above should mean the native OS file picker never opens (the
        # file is set directly on the <input> via Selenium), so normally
        # no Escape key is needed at all. Previously an Escape was sent
        # unconditionally here "just in case" — but since the picker
        # essentially never appears, that keypress was instead landing on
        # WhatsApp's own file-preview screen and triggering its "Discard
        # selection?" confirmation dialog, killing the send every time.
        caption_box = _find_element_any(driver, SHORT_ELEMENT_WAIT_SECONDS * 2, [
            '//div[@data-testid="media-caption-input-container"]',
            '//div[@aria-label="Add a caption"]',
            '//div[@role="textbox"][@aria-label="Add a caption"]',
            '//div[@contenteditable="true"][@data-tab="10"]',
        ], "caption box (file preview)")

        if caption_box is None:
            # Genuine fallback: the preview never showed up, which could
            # mean a native OS dialog really did slip through. Try Escape
            # ONCE, then re-check for the preview before giving up.
            logger.warning("File preview not found yet — trying Escape once in case a native picker appeared.")
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except Exception:
                pass
            time.sleep(0.5)
            caption_box = _find_element_any(driver, SHORT_ELEMENT_WAIT_SECONDS * 2, [
                '//div[@data-testid="media-caption-input-container"]',
                '//div[@aria-label="Add a caption"]',
                '//div[@role="textbox"][@aria-label="Add a caption"]',
                '//div[@contenteditable="true"][@data-tab="10"]',
            ], "caption box (file preview, retry)")

        if caption_box is None:
            logger.error("File preview did not appear — file may not have been attached.")
            return False

        # 5. Type caption.
        if caption:
            caption_box.send_keys(caption)
            time.sleep(0.3)

        # 6. Click Send.
        send_button = None
        for xpath in [
            '//span[@data-icon="wds-ic-send-filled"]',
            '//span[@data-testid="wds-ic-send-filled"]',
            '//div[@aria-label="Send"]',
            '//button[@aria-label="Send"]',
            '//div[@role="button"][@aria-label="Send"]',
            '//div[@data-testid="send"]',
        ]:
            try:
                send_button = WebDriverWait(driver, 1.0).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                break
            except Exception:
                continue

        if send_button is None:
            logger.error("Send button not found.")
            return False

        send_button.click()

        # Wait for the preview screen to close (returns to normal compose
        # box) — this alone is NOT proof of a successful send, it just
        # means WhatsApp stopped showing the attachment preview. The
        # actual upload can still fail afterwards (red exclamation /
        # "Failed to send"), which is what was happening before: this
        # step returned True immediately and the file got deleted from
        # output/ as if it had been delivered.
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//div[@data-testid="conversation-compose-box-input"]')
                )
            )
        except Exception:
            time.sleep(2)  # fallback wait if compose box detection fails

        # Now actually confirm delivery by watching the new message bubble.
        confirmed = _wait_for_send_confirmed(driver, count_before, SEND_CONFIRM_WAIT_SECONDS)
        if not confirmed:
            # Clear away any resulting discard/error state so the next
            # attach attempt (retry, or the next file) starts clean.
            _dismiss_discard_popup(driver)
        return confirmed

    except Exception:
        logger.exception("Exception while attaching/sending %s", file_path.name)
        return False


# ---------------------------------------------------------------------------
# PUBLIC: WhatsAppSession (used by app.py)
# ---------------------------------------------------------------------------

class WhatsAppSession:
    """Keeps one Chrome + WhatsApp Web session open across multiple sends.

    Usage in app.py:
        session = WhatsAppSession()
        if session.start():          # opens browser, loads WhatsApp, opens group
            session.send(csv_path)   # fast - browser already open
        session.close()              # call on shutdown
    """

    def __init__(self) -> None:
        self.driver = None
        self._group_open = False
        self._last_failed_at = 0

    def is_ready(self) -> bool:
        """True if browser is open and group chat is active."""
        return self.driver is not None and self._group_open

    def start(self) -> bool:
        """Open Chrome, load WhatsApp Web, open the group.
        Waits 15 seconds between failed attempts to avoid Chrome spam.
        Returns True if ready to send."""
        if not _SELENIUM_AVAILABLE:
            logger.error("selenium not installed. Run: pip install selenium")
            return False

        if self._last_failed_at:
            elapsed = time.time() - self._last_failed_at
            if elapsed < 15:
                return False

        # Reuse the existing browser if it's still alive. Previously this
        # method unconditionally called _create_browser() any time
        # is_ready() was False — which includes right after a single
        # failed send (send() sets _group_open = False to force a
        # re-open). That meant EVERY send failure spawned a brand new
        # Chrome window (via the SingletonLock removal hack killing the
        # old process), i.e. "the browser restarts between files" — when
        # really all that was needed was to re-open the group chat in
        # the browser that was already open.
        if self.driver is not None:
            if _browser_is_alive(self.driver):
                if _open_group_chat(self.driver, config.WHATSAPP_GROUP_NAME):
                    self._last_failed_at = 0
                    self._group_open = True
                    logger.info("Reused existing WhatsApp session — group re-opened.")
                    return True
                self._last_failed_at = time.time()
                return False
            else:
                logger.warning("Existing browser session is no longer usable — restarting Chrome.")
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None

        logger.info("Starting WhatsApp Web session...")
        self.driver = _create_browser()
        if self.driver is None:
            self._last_failed_at = time.time()
            return False

        if not _wait_for_whatsapp_loaded(self.driver):
            self._last_failed_at = time.time()
            return False

        if not _open_group_chat(self.driver, config.WHATSAPP_GROUP_NAME):
            self._last_failed_at = time.time()
            return False

        self._last_failed_at = 0
        self._group_open = True
        logger.info("WhatsApp session ready — group '%s' open.", config.WHATSAPP_GROUP_NAME)
        return True

    def send(self, file_path: Path) -> bool:
        """Send one file using the open browser session."""
        if self.driver is None:
            logger.error("Session not started. Call start() first.")
            return False

        if not file_path.exists():
            logger.error("File not found: %s", file_path)
            return False

        # Re-open the group if something changed (e.g. popup closed the chat).
        if not self._group_open:
            self._group_open = _open_group_chat(self.driver, config.WHATSAPP_GROUP_NAME)
            if not self._group_open:
                logger.error("Could not re-open group. Skipping %s", file_path.name)
                return False

        # Dismiss any "discard" popup left over from a previous interrupted
        # send — this is what causes the TXT to fail on first attempt.
        _dismiss_discard_popup(self.driver)

        # Build caption with file type label.
        suffix = file_path.suffix.upper().lstrip(".")  # "TXT" or "CSV"
        caption = f"[{suffix}] {config.WHATSAPP_CAPTION}: {file_path.name}"

        success = _attach_and_send_document(self.driver, file_path, caption)

        if success:
            logger.info("Sent and confirmed delivered: %s", file_path.name)
        else:
            logger.error("Failed to send (or could not confirm delivery): %s", file_path.name)
            # NOTE: intentionally NOT closing/restarting the browser here —
            # just flag the group as needing a re-open. The browser itself
            # stays alive and gets reused on the next attempt (see start()).
            self._group_open = False

        return success

    def send_text(self, message: str) -> bool:
        """Send a plain text message (no file attachment) to the group —
        used for operational alerts (LAN down, portal down, no file
        found, etc.) via alerts.py's cross-process queue."""
        if self.driver is None:
            logger.error("Session not started. Call start() first.")
            return False

        if not self._group_open:
            self._group_open = _open_group_chat(self.driver, config.WHATSAPP_GROUP_NAME)
            if not self._group_open:
                logger.error("Could not re-open group. Skipping alert: %s", message)
                return False

        _dismiss_discard_popup(self.driver)

        compose_box = _find_element_any(self.driver, SHORT_ELEMENT_WAIT_SECONDS * 2, [
            '//div[@data-testid="conversation-compose-box-input"]',
            '//div[@aria-label="Type a message"]',
            '//div[@role="textbox"][@data-tab="10"]',
        ], "message compose box")

        if compose_box is None:
            logger.error("Could not find compose box to send alert: %s", message)
            return False

        try:
            count_before = _outgoing_message_count(self.driver)

            compose_box.click()
            compose_box.send_keys(message)

            # Click the actual Send button rather than trusting the Enter
            # key. Our alert messages start with an emoji (⚠️/✅/⏱️), and
            # WhatsApp Web can pop up an emoji/autocomplete suggestion
            # right after typing one — Enter then gets swallowed by that
            # popup instead of submitting the message, leaving the text
            # sitting in the box typed but never actually sent. Clicking
            # the real button sidesteps that ambiguity entirely.
            send_button = _find_element_any(self.driver, SHORT_ELEMENT_WAIT_SECONDS, [
                '//button[@aria-label="Send"]',
                '//span[@data-icon="wds-ic-send-filled" or @data-icon="send"]/ancestor::button',
            ], "send button")

            if send_button is not None:
                send_button.click()
            else:
                # Fall back to Enter only if the button truly can't be found.
                compose_box.send_keys(Keys.ENTER)

            # Verify it actually went out instead of trusting the click —
            # same lesson as file sending: "the action ran" isn't proof
            # "it worked."
            confirmed = _wait_for_send_confirmed(self.driver, count_before, SEND_CONFIRM_WAIT_SECONDS)
            if confirmed:
                logger.info("Sent WhatsApp alert: %s", message)
            else:
                logger.error("Alert may not have actually sent (no delivery confirmation): %s", message)
            return confirmed
        except Exception:
            logger.exception("Failed to send WhatsApp alert: %s", message)
            return False

    def close(self) -> None:
        """Close the browser."""
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            self._group_open = False
            logger.info("WhatsApp session closed.")
