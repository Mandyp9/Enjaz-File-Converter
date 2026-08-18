# How `remittance_app` Works — A to Z

This is a complete walkthrough of the system: what runs, in what order, and
why each piece exists. Read top to bottom for the full picture, or jump to
a section using the list below.

1. [The big picture](#1-the-big-picture)
2. [scheduler.py — getting files off the portal](#2-schedulerpy--getting-files-off-the-portal)
3. [downloader.py — how one file check actually works](#3-downloaderpy--how-one-file-check-actually-works)
4. [network_check.py + alerts.py — LAN/portal monitoring](#4-network_checkpy--alertspy--lanportal-monitoring)
5. [app.py — watching input/ and driving conversion](#5-apppy--watching-input-and-driving-conversion)
6. [converter.py — TXT → CSV, field by field](#6-converterpy--txt--csv-field-by-field)
7. [exchange_rate.py — the USD rate used for one column](#7-exchange_ratepy--the-usd-rate-used-for-one-column)
8. [whatsapp_sender.py — actually sending the files](#8-whatsapp_senderpy--actually-sending-the-files)
9. [Data files you'll see appear](#9-data-files-youll-see-appear)
10. [Two processes, one machine — how they talk](#10-two-processes-one-machine--how-they-talk)
11. [config.py — the knobs you can turn](#11-configpy--the-knobs-you-can-turn)

---

## 1. The big picture

Two Python scripts run **at the same time**, in two terminal windows:

```
Terminal 1:  python scheduler.py     ← downloads TXT files from the portal
Terminal 2:  python app.py           ← converts + sends them via WhatsApp
```

They never call each other directly. They hand work off through the
filesystem:

```
Bank Albilad portal
        │  (scheduler.py logs in, downloads)
        ▼
     input/  ─────────────────────┐
        │  (app.py watches this)  │
        ▼                         │
  convert TXT → CSV               │ scheduler.py/downloader.py can also
        │                         │ queue an operational alert here:
        ▼                         │   "LAN down", "portal down",
  data_archive/  (permanent)      │   "no file found"
        │                         ▼
        ▼                   alerts.py (file-based queue)
     output/                      │
        │                         │ app.py picks these up too
        ▼                         ▼
   WhatsApp group  ◄──────────────┘
   ("Enjaz File")
```

Why split into two processes instead of one? Because downloading (Selenium
+ portal login) and sending (Selenium + WhatsApp Web) are two independent,
occasionally-slow browser automations. If one hangs or needs a retry, it
shouldn't block the other.

---

## 2. `scheduler.py` — getting files off the portal

`scheduler.py` runs one infinite loop (`run_scheduler()`), ticking every
`SCHEDULER_TICK_SECONDS` (30s). Each tick it does three things:

**a) Runs the 15-minute network check, if due.**
Independent of any download window — see [section 4](#4-network_checkpy--alertspy--lanportal-monitoring).

**b) For each time in `config.DOWNLOAD_SCHEDULE`**, it asks: *"are we
currently inside this slot's 5-minute window
(`DOWNLOAD_WINDOW_MINUTES`)?"* If yes, and this slot hasn't already
produced a file, it calls `downloader.check_and_download()`.

**c) Tracks three sets of slots** so it doesn't spam or misreport:
- `completed_slots` — already got a file this slot, stop retrying.
- `ever_checked_slots` — the portal was actually reached at least once
  this slot (as opposed to being blocked by a LAN problem the whole time).
- `alerted_no_file_slots` — already sent the "no file found" WhatsApp
  alert for this slot, don't send it again every tick.

When a window closes with nothing downloaded, it only sends a **"no file
found"** alert if the portal was actually reachable at some point during
that window. If the whole window was blocked by a LAN outage, that's a
different, more useful alert (see section 4) — reporting both would be
misleading, since "no file" implies the portal *was* checked and was
empty.

At startup, any window that had already closed *before the process even
started* is silently skipped for that day — restarting scheduler.py at
11am shouldn't claim the 01:46 slot "found no file" when it never had a
chance to check.

---

## 3. `downloader.py` — how one file check actually works

`check_and_download()` is the entry point scheduler.py calls. Every single
time it's called:

1. **Fresh LAN check first.** `_check_lan_status_and_alert()` re-checks
   LAN/IP status right now (not a cached value — see section 4 for why
   that matters) and returns `None` immediately if LAN is down, so the
   caller knows "we genuinely don't know if a file exists" rather than
   mistakenly reporting "no file found."

2. **Figure out the starting sequence number.** Reads
   `download_state.json` for today's last-downloaded sequence and starts
   from `last + 1`. If there's no state for today yet, it doesn't assume
   the day starts at `00001` — it **probes** for the actual starting
   sequence using exponential search + binary search
   (`_probe_for_first_existing`), since the portal doesn't always reset
   to 1 each day.

3. **Log in** (`_login`), then loop: call `_download_file()` for each
   sequence number, going up, until one comes back `"not_found"` — that's
   today's last file.

4. **`_download_file()` itself** checks existence via a direct HTTP
   request (using the browser's own login cookies) rather than scanning
   rendered page text. Why: this portal is built on SharePoint/ASP.NET
   WebParts, and those platforms bake boilerplate error-message strings
   into *every* page's JavaScript resource bundles — scanning page text
   for phrases like "the file or folder" could false-positive on a file
   that actually exists. A real HTTP 404 is unambiguous. If the HTTP path
   ever fails outright (not a 404, just broken), it falls back to the
   older browser-navigation approach so behavior degrades gracefully.
   Any ambiguous or unexpected response gets dumped to
   `debug_logs/downloader/` as JSON for later inspection.

5. **Saves progress** to `download_state.json` after each successful
   download, so a restart mid-day resumes from the right sequence number
   instead of re-downloading everything.

---

## 4. `network_check.py` + `alerts.py` — LAN/portal monitoring

Two independent checks run on two different clocks, on purpose:

**Every 15 minutes** (`config.NETWORK_CHECK_INTERVAL_SECONDS`), regardless
of download windows: `downloader.run_periodic_network_check()` checks LAN
connectivity, LAN IP (against `config.EXPECTED_LAN_IP`, if set), and
portal reachability. This is what catches a problem during the long gaps
*between* scheduled download times — LAN could drop at 3am when nothing's
scheduled, and you'd still get alerted.

**Every time a download is actually attempted**, a completely fresh LAN
check runs again right then (`_check_lan_status_and_alert()`), instead of
trusting the 15-minute check's result. This distinction matters: if the
15-minute check happened to run while LAN was down, that stale "down"
reading could otherwise sit there for up to 15 minutes — long enough to
outlast an entire 5-minute download window — even if LAN reconnected
moments later. A fresh check every attempt closes that gap.

**Alerts** (`alerts.py`) are a small JSON-file queue, because
`scheduler.py`/`downloader.py` run in a separate process from `app.py` and
don't have their own WhatsApp session:

- `queue_alert(key, message)` adds a message, but only once per
  `ALERT_COOLDOWN_MINUTES` (30) per `key` — an ongoing problem reminds you
  periodically instead of spamming every 30 seconds.
- `clear_cooldown(key)` is called the moment an issue resolves, so if it
  happens again soon after, you're alerted right away instead of waiting
  out the old cooldown.
- Reads/writes use atomic temp-file-then-rename with retries, since two
  processes touch the same JSON files.
- `app.py` calls `dequeue_alerts()` every watch cycle and actually sends
  whatever's queued, since it's the process with the live WhatsApp
  session.

---

## 5. `app.py` — watching input/ and driving conversion

`watch_and_process()` loops every `WATCH_INTERVAL_SECONDS` (3s):

1. **List `input/*.txt`.** For each file:
   - **Duplicate check** (`is_duplicate`) — two independent checks: same
     filename already in the archive, or identical content (MD5 hash)
     already processed under any name. Duplicates get moved into the
     archive with a `duplicate_` prefix instead of being reprocessed.
   - **Convert + archive** (`convert_and_archive`) — calls
     `converter.convert_txt_to_csv()`, then copies the CSV and moves the
     original TXT into `data_archive/` (permanent history, filenames
     prefixed with today's date), and drops working copies into
     `output/` for sending.
   - Only marked "processed" (hash saved) **after** successful conversion
     + archiving — so a failure leaves the file in `input/` for retry
     next cycle instead of silently losing it.

2. **Send pending alerts** — picks up anything queued by
   scheduler.py/downloader.py and sends it as a WhatsApp text message.

3. **Send pending files** (`send_pending_files`) — for every file waiting
   in `output/`, matches TXT and CSV by filename stem and sends the TXT
   **first**, then the CSV, as two separate messages. Each file is
   deleted from `output/` only after a confirmed successful send; a
   failure leaves it there for the next cycle to retry.

4. **Close the browser when idle** — if nothing's pending in `output/`
   and no alerts are queued, the WhatsApp browser session closes rather
   than sitting open indefinitely.

Startup also rebuilds the duplicate-hash set by scanning
`data_archive/txt/` directly (`build_hashes_from_archive`), so duplicate
detection survives even if `processed_files.json` is missing or deleted.

---

## 6. `converter.py` — TXT → CSV, field by field

One call: `convert_txt_to_csv(txt_path, csv_path, bank_mapping)`. Four
steps, in order:

**Step 1 — Read.** `read_data_lines()` keeps only lines starting with
`'D'` (data records); `'H'` (header) and `'F'` (footer) lines are ignored.

**Step 2 — Parse.** `parse_line()` slices each fixed-width line into
named fields using `config.FIELD_POSITIONS` (1-based start position +
length per field).

**Step 3 — Transform.** `transform_record()` applies every business rule
to turn one parsed record into one CSV row. In priority order:

- **SSF override (highest priority):** if `BeneficiaryAddress` contains
  "SSF" anywhere, `PaymentMode` is forced to `B` and the bank code is
  forced to `125` ("SSF Bank"), overriding every other rule below.
- **Extract a bank name from the address**, if `beneficiary_bank` wasn't
  already given directly (looks for a phrase containing "BANK").
- **Extract an account number from the address**, if it wasn't already
  given directly and the address is purely digits.
- **Payment mode (`B`/`W`/`C`)** — primarily driven by the `RemitterDOB`
  field, which is repurposed as a flag: `"CASH"` → `C`, `"ACCOUNT"` → `B`.
  If that flag isn't set, falls back to: a bank name present → `B`; the
  account number is shaped like a mobile number → `W` (wallet); numeric
  but not phone-shaped → `B`; otherwise → `C`.
- **Wallet detection (`looks_like_wallet_number`)** isn't just "all
  digits" — bank accounts are numeric too. It checks the number's actual
  *shape* against your editable `wallet_formats.json` prefix/length
  rules first, then falls back to the `phonenumbers` library for any
  valid mobile number not covered by an explicit rule.
- **Bank code lookup** (`lookup_bank`) — tries an exact name match first
  (ignoring case and LTD/LIMITED-style differences), then a substring
  match against `bank_mapping.json`'s full names, then a configured
  alias match. If nothing matches for a `B` payout, falls back to a
  static "eSewa Bank Account" / code `99` rather than leaving it blank.
- **`USDValueAmount`** — the transaction amount divided by the exchange
  rate (from `exchange_rate.py`), rounded to 2 decimals. This is the
  *only* place the live rate is used — `TransactionExchangeRate` in the
  output CSV is always hardcoded `"1"`.
- **`csv_safe_numeric`** — prefixes a leading apostrophe onto numeric
  strings that Excel would otherwise mangle (leading zeros getting
  dropped, 12+ digit numbers turning into scientific notation).

**Step 4 — Write.** `write_csv()` writes every row using the fixed column
order in `config.CSV_HEADERS`.

**One rate per file, not per row:** the exchange rate is fetched **once**
at the top of `convert_txt_to_csv()` and passed into every row's
transform — so a single CSV can never end up with two rows computed
against two different rates just because the rate happened to refresh
mid-file.

---

## 7. `exchange_rate.py` — the USD rate used for one column

The rate only actually changes twice a day in reality, so this module
avoids hitting the live SOAP service on every single file conversion:

- **Refresh slots** (`config.EXRATE_REFRESH_TIMES`, default `10:00` and
  `14:00`) define when a fresh call is actually needed. Any request for
  the rate that falls inside a slot that's already been fetched just
  returns the cached value — no network call.
- **Persisted across restarts** — every fetch (success or fallback) is
  appended as a row to `exchange_rate_history.xlsx`
  (`Timestamp, Slot, Rate, Source, Note`). On startup, the module scans
  backward for the most recent row whose `Source` was an actual
  successful `"soap"` call (deliberately ignoring fallback rows — a
  fallback value must never get "locked in" as if it were validated) and
  reuses it if it matches the current slot, avoiding a redundant call
  right after a restart.
- **Fallback chain on failure:** SOAP call fails → use the last rate
  that worked this run (or restored from history) → if nothing has ever
  worked, use `config.EXRATE_DEFAULT_FALLBACK_RATE` ("1"). A failed
  refresh does **not** lock in that slot, so the very next call (even
  within the same slot) retries the real service instead of silently
  sticking with a bad fallback for hours.
- **JSON call log** (`exchange_rate_log.json`) — a lightweight, always-
  written record of every attempt (success or failure) with the reason
  on failure, kept independently of the xlsx so you always have
  something to check even if the xlsx write has a problem. Capped at the
  most recent 500 entries.
- **Never crashes the caller** — the whole thing is wrapped in a
  catch-all, so even an unexpected bug or an incomplete `config.py`
  degrades to a fallback rate (and still gets logged) instead of
  crashing the file conversion that needed it.

You can test this module in isolation, without a real file to convert,
using `python test_exchange_rate.py`.

---

## 8. `whatsapp_sender.py` — actually sending the files

`WhatsAppSession` wraps a Selenium-controlled Chrome window pointed at
WhatsApp Web, using a persistent Chrome profile (`chrome_profile/`) so you
only scan the QR code once, ever.

- **`start()`** opens Chrome, navigates to WhatsApp Web, and waits (up to
  `INITIAL_LOAD_WAIT_SECONDS`) for either the QR code (first time) or the
  chat list (already logged in) to appear, then opens the configured
  group chat.
- **`send(file_path)`** attaches and sends a file (TXT or CSV) as a
  document. Delivery is confirmed via WhatsApp's own `data-id` message
  tagging on the sent bubble — not just "the click succeeded" — so a
  message that silently failed to actually go out doesn't get treated as
  sent.
- **`send_text(message)`** sends a plain text alert the same way, used
  for the operational alerts from `alerts.py`.
- The browser instance is **reused** across sends rather than restarted
  on every failure, and a blind Escape-key press that used to trigger a
  "Discard selection?" prompt (interrupting a send mid-flight) has been
  removed.
- **`close()`** shuts the browser down; `app.py` calls this once nothing
  is pending, so the browser doesn't sit open indefinitely between file
  batches.

---

## 9. Data files you'll see appear

All created automatically — you never need to create these by hand.

| File | Created by | Purpose |
|---|---|---|
| `input/*.txt` | scheduler.py | Freshly downloaded, awaiting conversion |
| `output/*.csv`, `output/*.txt` | app.py | Converted, awaiting WhatsApp send |
| `data_archive/txt/`, `data_archive/csv/` | app.py | Permanent history, date-prefixed filenames |
| `processed_files.json` | app.py | MD5 hashes of everything ever processed (duplicate detection) |
| `download_state.json` | downloader.py | Last downloaded sequence number per day |
| `alerts_queue.json`, `alerts_state.json` | alerts.py | Pending WhatsApp alerts + per-issue cooldown timestamps |
| `exchange_rate_history.xlsx` | exchange_rate.py | Every rate fetch (success/fallback), also the persisted cache |
| `exchange_rate_log.json` | exchange_rate.py | Lightweight success/failure log of every SOAP call attempt |
| `debug_logs/downloader/*.json` | downloader.py | Raw HTTP diagnostics for any ambiguous file-existence check |
| `chrome_profile/` | whatsapp_sender.py | Saved WhatsApp Web login (avoids re-scanning the QR code) |

---

## 10. Two processes, one machine — how they talk

Since `scheduler.py`/`downloader.py` and `app.py` are separate OS
processes, they can't share Python variables. Everything they need to
hand off goes through the filesystem instead:

- `input/` — scheduler.py writes, app.py reads.
- `alerts_queue.json` / `alerts_state.json` — either side can write
  (queue_alert/clear_cooldown), app.py reads and drains
  (dequeue_alerts). All reads/writes are atomic (temp file + rename)
  with retries, specifically because both processes touch these files
  concurrently — this is what stops one process's write from corrupting
  or silently dropping data written by the other.

Nothing else needs cross-process coordination — the rest
(`download_state.json`, `processed_files.json`,
`exchange_rate_history.xlsx`, etc.) is only ever touched by one of the
two processes.

---

## 11. `config.py` — the knobs you can turn

The full behavior above is driven by values in `config.py`. The ones most
worth knowing about:

| Setting | Default | Controls |
|---|---|---|
| `DOWNLOAD_SCHEDULE` | list of `"HH:MM"` | When scheduler.py expects a new file |
| `DOWNLOAD_WINDOW_MINUTES` | 5 | How long it keeps retrying each slot |
| `SCHEDULER_TICK_SECONDS` | 30 | How often scheduler.py's main loop wakes up |
| `WATCH_INTERVAL_SECONDS` | 3 | How often app.py checks input/ and output/ |
| `NETWORK_CHECK_INTERVAL_SECONDS` | 15 min | Periodic LAN/portal check cadence (download attempts also always check fresh — see §4) |
| `EXPECTED_LAN_IP` | "" (unset) | If set, alerts when the LAN IP doesn't match |
| `ALERT_COOLDOWN_MINUTES` | 30 | Minimum gap between repeat alerts for the same issue |
| `EXRATE_REFRESH_TIMES` | `["10:00","14:00"]` | When exchange_rate.py is allowed to call the live SOAP service |
| `wallet_formats.json` | — | Wallet-ID prefix/length rules (edit without touching code) |
| `bank_mapping.json` | — | Bank name → code + aliases |

If you're ever unsure why something behaved a certain way, the log output
from either process (stdout) explains its reasoning at each decision
point — most functions above log exactly which branch they took and why.
