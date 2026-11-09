"""Eldorado CC tee time booker.

Logs in to GolfIBS WebRes, waits for the tee sheet to open, then books up to
NUM_SLOTS tee times inside the time window, packed as close together as
possible (ties broken by closeness of the first slot to TARGET_TIME).

Usage:
    book_teetime.py [--date YYYY-MM-DD] [--wait-until HH:MM:SS] [--live]

Without --live it is a dry run: each chosen slot is opened (Book), verified on
the Enter Details page, and its lock released — Finish Booking is never clicked.

Credentials come from ELDORADO_LOGIN / ELDORADO_PASSWORD.
"""
import argparse
import datetime as dt
import itertools
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://www.goibsvision.com/WebRes/Club/EldoradoCC"

# ── Booking preferences ───────────────────────────────────────────────────────
DAYS_AHEAD = 7                  # tee sheet opens 7 days out at 6:00 AM
WINDOW_START = dt.time(10, 15)  # earliest acceptable tee time
WINDOW_END = dt.time(11, 15)    # latest acceptable tee time
TARGET_TIME = dt.time(10, 30)   # tie-breaker: first slot closest to this
NUM_SLOTS = 3                   # tee times to book
PLAYERS = 4                     # players per tee time
PERIOD_TAB = 2                  # 1=Early AM, 2=Late AM, 3=Afternoon
POLL_SECONDS = 600              # keep polling for open slots this long after start
SLOT_COOLDOWN = 45              # after a failed Book, skip that slot this long (another member may hold its lock)

SHOT_DIR = Path(__file__).resolve().parent / "logs" / "shots"


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S.%f}"[:-3] + f"] {msg}", flush=True)


def minutes(t):
    return t.hour * 60 + t.minute


def choose_slots(available, booked, n):
    """Pick n more slots from `available` so booked+picked spans the least time.

    Ties go to the set whose earliest slot is closest to TARGET_TIME.
    Returns [] if nothing is available.
    """
    k = min(n, len(available))
    if k == 0:
        return []
    best, best_key = None, None
    for combo in itertools.combinations(sorted(available, key=minutes), k):
        all_times = [minutes(t) for t in list(booked) + list(combo)]
        key = (max(all_times) - min(all_times), abs(min(all_times) - minutes(TARGET_TIME)))
        if best_key is None or key < best_key:
            best, best_key = list(combo), key
    return best


def snap(page, name):
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{name}.png"
    page.screenshot(path=str(path), full_page=True)
    log(f"screenshot: {path}")


class BadCredentials(RuntimeError):
    pass


class SessionLost(RuntimeError):
    pass


def logged_in(page):
    return page.locator("a:has-text('Log Off')").count() > 0


def login(page):
    page.goto(f"{BASE}/Login?ReturnUrl=%2FWebRes%2FClub%2FEldoradoCC%2FBrowse")
    page.fill("#txtLoginName", os.environ["ELDORADO_LOGIN"])
    page.fill("#txtPassword", os.environ["ELDORADO_PASSWORD"])
    page.click("input[type=submit][value='Secure Login']")
    page.wait_for_load_state("networkidle")
    if "/Browse" not in page.url:
        snap(page, "login_failed")
        if "password provided is incorrect" in page.content():
            raise BadCredentials("site rejected login name/password")
        raise RuntimeError(f"login failed (landed on {page.url})")
    log("logged in")


def login_with_retry(page, deadline):
    """Retry transient login failures until `deadline` (monotonic). Bad credentials fail at once
    — repeating them risks locking the account."""
    attempt = 0
    while True:
        attempt += 1
        try:
            login(page)
            return
        except BadCredentials:
            raise
        except Exception as e:
            wait = min(5 * attempt, 15)
            if time.monotonic() + wait > deadline:
                raise RuntimeError(f"login still failing after {attempt} attempts: {e}") from e
            log(f"login attempt {attempt} failed ({e}); retrying in {wait}s")
            time.sleep(wait)


def reservations_on(page, date):
    """Tee times already reserved on `date`, read from My Reservations (source of truth in live mode)."""
    page.goto(f"{BASE}/Profile/Reservations")
    page.wait_for_load_state("networkidle")
    body = " ".join(page.locator("body").inner_text().split())
    pattern = rf"\b{date.month}/{date.day}/{date.year} (\d{{1,2}}:\d\d [AP]M)"
    times = {dt.datetime.strptime(m, "%I:%M %p").time() for m in re.findall(pattern, body)}
    page.goto(f"{BASE}/Browse")
    page.wait_for_load_state("networkidle")
    return sorted(times, key=minutes)


def load_sheet(page, date):
    """Show the tee sheet for `date` and return {time: Book-button locator} for open slots in the window."""
    if "/Browse" not in page.url:
        page.goto(f"{BASE}/Browse")
        page.wait_for_load_state("networkidle")
    # The tee sheet is viewable logged out, but Book then demands a login — catch that here.
    if not logged_in(page):
        raise SessionLost("not logged in on Browse page")
    ok = page.evaluate(
        """([y, m, d, players]) => {
            const dp = jQuery('input[data-role=datepicker]').data('kendoDatePicker');
            const dd = jQuery('[data-role=dropdownlist]').data('kendoDropDownList');
            if (!dp || !dd) return false;
            dp.value(new Date(y, m - 1, d)); dp.trigger('change');
            dd.value(String(players)); dd.trigger('change');
            const v = dp.value();
            return !!v && v.getFullYear() === y && v.getMonth() === m - 1 && v.getDate() === d;
        }""",
        [date.year, date.month, date.day, PLAYERS],
    )
    if not ok:
        # Date is past the picker's max (set at page load) — reload so the max moves forward.
        page.goto(f"{BASE}/Browse")
        page.wait_for_load_state("networkidle")
        return {}
    # The grid fills from an AJAX call after the click; wait for that response, not network idle.
    with page.expect_response(lambda r: "GetVisualViewGridResult" in r.url, timeout=30000):
        page.locator("text=View Tee Sheet").first.click()
    tab = page.locator(f"button.slot-hour-btn.filter-id-{PERIOD_TAB}")
    try:
        tab.wait_for(state="visible", timeout=10000)
    except Exception:
        return {}
    tab.click()
    page.wait_for_timeout(500)

    open_slots = {}
    buttons = page.locator("form[action$='BookingStart'] input[type=submit]")
    for i in range(buttons.count()):
        btn = buttons.nth(i)
        cell = btn.locator("xpath=ancestor::tr[1]/td[1]").inner_text().strip()
        try:
            t = dt.datetime.strptime(cell, "%I:%M %p").time()
        except ValueError:
            continue
        if WINDOW_START <= t <= WINDOW_END:
            open_slots[t] = btn
    return open_slots


def release_lock(page):
    page.evaluate("() => fetch('/WebRes/Club/EldoradoCC/ClearLocks', {method: 'POST'})")
    page.goto(f"{BASE}/Browse")
    page.wait_for_load_state("networkidle")


def book_slot(page, date, t, btn, live):
    """Click Book for one slot, verify the details page, then finish (live) or release (dry run)."""
    label = f"{date:%m/%d/%Y} {t:%I:%M %p}".lstrip("0")
    btn.click()
    page.wait_for_load_state("networkidle")
    if "/BookingCheckUser" in page.url or "/Login" in page.url:
        raise SessionLost(f"Book redirected to login ({page.url})")
    if "/ReservationDetails" not in page.url:
        snap(page, f"book_click_{t:%H%M}")
        if "Unable to Book" in page.content():
            log(f"{label}: Unable to Book — taken or locked by someone else's booking in progress")
        else:
            log(f"{label}: Book did not open details page ({page.url}) — slot likely taken")
        page.goto(f"{BASE}/Browse")
        page.wait_for_load_state("networkidle")
        return False

    body = " ".join(page.locator("body").inner_text().split())
    want_time = t.strftime("%I:%M %p").lstrip("0")
    want_date = f"{date.month}/{date.day}/{date.year}"
    if want_time not in body or want_date not in body:
        snap(page, f"details_mismatch_{t:%H%M}")
        log(f"{label}: details page does not show {want_date} {want_time} — releasing")
        release_lock(page)
        return False

    if not live:
        log(f"{label}: DRY RUN — details verified, releasing lock (Finish Booking not clicked)")
        release_lock(page)
        return True

    # JS click so an informational popup over the button can't swallow it.
    page.evaluate("() => document.getElementById('book').click()")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    snap(page, f"finished_{t:%H%M}")
    if "/BookingConfirmation/" not in page.url:
        log(f"{label}: Finish Booking did not reach confirmation page ({page.url})")
        if "/ReservationDetails" in page.url:
            release_lock(page)
        return False
    code = page.url.split("/BookingConfirmation/")[1].split("?")[0]
    log(f"{label}: BOOKED — confirmation {code}")
    page.goto(f"{BASE}/Browse")
    page.wait_for_load_state("networkidle")
    return True


def wait_until(target):
    while True:
        remaining = (target - dt.datetime.now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.25 if remaining < 5 else 5))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="tee date YYYY-MM-DD (default: today + %d days)" % DAYS_AHEAD)
    ap.add_argument("--wait-until", help="HH:MM:SS to start polling (e.g. 06:00:00)")
    ap.add_argument("--live", action="store_true", help="actually click Finish Booking")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--window", help="override window, e.g. 10:15-11:15")
    ap.add_argument("--target", help="override target time, e.g. 10:30")
    ap.add_argument("--tab", type=int, help="override tee sheet tab: 1=Early AM, 2=Late AM, 3=Afternoon")
    ap.add_argument("--poll-seconds", type=int, default=POLL_SECONDS, help="give up after this long")
    args = ap.parse_args()

    global WINDOW_START, WINDOW_END, TARGET_TIME, PERIOD_TAB
    if args.window:
        WINDOW_START, WINDOW_END = (dt.time.fromisoformat(x) for x in args.window.split("-"))
    if args.target:
        TARGET_TIME = dt.time.fromisoformat(args.target)
    if args.tab:
        PERIOD_TAB = args.tab

    date = dt.date.fromisoformat(args.date) if args.date else dt.date.today() + dt.timedelta(days=DAYS_AHEAD)
    mode = "LIVE" if args.live else "DRY RUN"
    log(f"{mode}: {NUM_SLOTS} x {PLAYERS} players on {date:%a %m/%d/%Y}, "
        f"window {WINDOW_START:%H:%M}-{WINDOW_END:%H:%M}, target {TARGET_TIME:%H:%M}")

    booked = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        try:
            start = None
            if args.wait_until:
                h, m, s = map(int, args.wait_until.split(":"))
                start = dt.datetime.combine(dt.date.today(), dt.time(h, m, s))
            secs_to_start = max(0.0, (start - dt.datetime.now()).total_seconds()) if start else 0.0
            deadline = time.monotonic() + secs_to_start + args.poll_seconds

            login_with_retry(page, deadline)
            if args.live:
                booked = [t for t in reservations_on(page, date) if WINDOW_START <= t <= WINDOW_END]
                if booked:
                    log("already reserved in window: " + ", ".join(f"{t:%H:%M}" for t in booked))
            if start:
                log(f"waiting until {start:%H:%M:%S}")
                wait_until(start)

            polls = errors = 0
            cooldown = {}  # slot time -> monotonic time it may be retried
            while len(booked) < NUM_SLOTS and time.monotonic() < deadline:
                try:
                    open_slots = load_sheet(page, date)
                    polls += 1
                    now = time.monotonic()
                    for t in list(booked) + [t for t, until in cooldown.items() if until > now]:
                        open_slots.pop(t, None)
                    picks = choose_slots(open_slots.keys(), booked, NUM_SLOTS - len(booked))
                    if not picks:
                        if polls == 1 or polls % 30 == 0:
                            log(f"no open slots in window yet (poll {polls})")
                        if polls == 1:
                            snap(page, "first_poll_empty")  # shows whether times were taken or not yet posted
                        time.sleep(1)
                        continue
                    log("open: " + ", ".join(f"{t:%H:%M}" for t in sorted(open_slots, key=minutes))
                        + " -> picking " + ", ".join(f"{t:%H:%M}" for t in picks))
                    # Book only the first pick, then re-read the sheet so later picks adapt to what's left.
                    t = picks[0]
                    if book_slot(page, date, t, open_slots[t], args.live):
                        booked.append(t)
                    else:
                        cooldown[t] = time.monotonic() + SLOT_COOLDOWN
                        log(f"skipping {t:%H:%M} for {SLOT_COOLDOWN}s")
                except BadCredentials:
                    raise
                except SessionLost as e:
                    log(f"session lost ({e}) — logging back in")
                    login_with_retry(page, deadline)
                except Exception as e:
                    errors += 1
                    log(f"error during poll/booking (#{errors}): {e} — recovering")
                    if errors <= 3 or errors % 10 == 0:
                        try:
                            snap(page, f"poll_error_{errors}")
                        except Exception:
                            pass
                    time.sleep(2)
                    try:
                        page.goto(f"{BASE}/Browse")
                        page.wait_for_load_state("networkidle")
                        if not logged_in(page):
                            login_with_retry(page, deadline)
                        if args.live:
                            # A crash right after Finish Booking could hide a successful booking.
                            booked = [t for t in reservations_on(page, date) if WINDOW_START <= t <= WINDOW_END]
                            log("reservations now held in window: "
                                + (", ".join(f"{t:%H:%M}" for t in booked) or "none"))
                        else:
                            page.goto(f"{BASE}/Browse")
                            page.wait_for_load_state("networkidle")
                    except BadCredentials:
                        raise
                    except Exception as e2:
                        log(f"recovery failed ({e2}); will retry")
        except Exception as e:
            log(f"ERROR: {e}")
            try:
                snap(page, "error")
            except Exception:
                pass
            raise
        finally:
            browser.close()

    summary = ", ".join(f"{t:%I:%M %p}".lstrip("0") for t in sorted(booked, key=minutes)) or "none"
    log(f"RESULT ({mode}) {date:%a %m/%d/%Y}: {len(booked)}/{NUM_SLOTS} slots -> {summary}")
    return 0 if booked else 1


if __name__ == "__main__":
    sys.exit(main())
