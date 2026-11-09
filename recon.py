"""Recon for Eldorado CC tee sheet — logs in, captures pages + XHR traffic. Never books.

Usage: python3 recon.py OUT_DIR [YYYY-MM-DD]
"""
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "https://www.goibsvision.com/WebRes/Club/EldoradoCC"

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
target_date = sys.argv[2] if len(sys.argv) > 2 else None

traffic = []


def on_request(req):
    if req.resource_type in ("xhr", "fetch", "document"):
        body = req.post_data or ""
        if "Password" in body:
            body = "<redacted login body>"
        traffic.append({"method": req.method, "url": req.url, "type": req.resource_type, "post": body[:2000]})


def snap(page, name):
    (out / f"{name}.html").write_text(page.content())
    page.screenshot(path=str(out / f"{name}.png"), full_page=True)
    print(f"saved {name}: {page.url}")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.on("request", on_request)

    page.goto(f"{BASE}/Login?ReturnUrl=%2FWebRes%2FClub%2FEldoradoCC%2FBrowse")
    page.fill("#txtLoginName", os.environ["ELDORADO_LOGIN"])
    page.fill("#txtPassword", os.environ["ELDORADO_PASSWORD"])
    page.click("input[type=submit][value='Secure Login']")
    page.wait_for_load_state("networkidle")
    snap(page, "01_after_login")

    if "Browse" not in page.url:
        page.goto(f"{BASE}/Browse")
        page.wait_for_load_state("networkidle")
    snap(page, "02_browse")

    if target_date:
        # Kendo date pickers usually wrap an <input data-role=datepicker>; set via the widget API.
        set_ok = page.evaluate(
            """(d) => {
                const el = document.querySelector('input[data-role=datepicker]');
                if (!el || !window.jQuery) return false;
                const w = jQuery(el).data('kendoDatePicker');
                const [y, m, day] = d.split('-').map(Number);
                w.value(new Date(y, m - 1, day));
                w.trigger('change');
                return true;
            }""",
            target_date,
        )
        print("datepicker set:", set_ok)
        players_ok = page.evaluate(
            """() => {
                const el = document.querySelector('input[data-role=dropdownlist], select[data-role=dropdownlist]');
                const w = el && jQuery(el).data('kendoDropDownList');
                if (!w) return false;
                w.value('4');
                w.trigger('change');
                return w.value();
            }"""
        )
        print("players set:", players_ok)
        btn = page.locator("text=View Tee Sheet").first
        if btn.count():
            btn.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)
            snap(page, "03_tee_sheet")
            page.click("button.slot-hour-btn.filter-id-2")  # Late AM
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            snap(page, "04_late_am")
            rows = page.evaluate(
                """() => [...document.querySelectorAll('tr')].map(tr => ({
                    text: tr.innerText.replace(/\\s+/g, ' ').trim(),
                    bookable: !!tr.querySelector('form[action$="BookingStart"] input[type=submit]'),
                    players: tr.querySelector('input[name=NumberOfPlayers]')?.value,
                })).filter(r => /\\d:\\d\\d [AP]M/.test(r.text))"""
            )
            (out / "late_am_rows.json").write_text(json.dumps(rows, indent=2))
            for r in rows:
                print(("BOOK " if r["bookable"] else "     ") + r["text"][:110])

    (out / "traffic.json").write_text(json.dumps(traffic, indent=2))
    print(f"{len(traffic)} requests logged")
    browser.close()
