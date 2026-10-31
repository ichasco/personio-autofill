#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "rich", "playwright"]
# ///
"""
Fill in missing Personio attendance for a date range using YOUR web session (SSO).

Flow:
  1. Opens a browser; you log in with your normal SSO (only the first time or
     when the session expires: the browser profile is kept).
  2. Reads your Personio timesheet: attendance, holidays from your calendar,
     absences and non-working days.
  3. Shows the PLAN as a monthly calendar + summary (writes nothing).
  4. Asks for confirmation. Only if you answer "y" it creates the attendance.
  5. Reads Personio again and verifies everything was created.

Skips: non-working days, holidays, absences (unless rejected/cancelled),
--skip dates and days that already have attendance (never touched).

Uses internal (undocumented) endpoints of the Personio web app: they may change.
If it stops working, use --discover to see what the web app calls now.

Requirements: uv, and Google Chrome or Microsoft Edge installed. Without them,
run once: uvx playwright install chromium

Example:
    ./personio_autofill.py --host https://yourcompany.app.personio.com \
        --from 2026-01-01 --start 09:00 --end 18:00 --break 14:00-15:00
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

console = Console()

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

TRACK, DONE, ABSENCE, HOLIDAY, OFF, FUTURE, OUTSIDE = (
    "track", "done", "absence", "holiday", "off", "future", "outside")
STYLE = {
    TRACK:   ("bold black on green",  "Will be tracked"),
    DONE:    ("bold white on blue",   "Already tracked (untouched)"),
    ABSENCE: ("bold black on yellow", "Vacation / absence"),
    HOLIDAY: ("bold white on red",    "Holiday"),
    OFF:     ("grey50",               "Non-working day"),
    FUTURE:  ("grey35",               "Pending / future"),
    OUTSIDE: ("grey23",               "Out of range"),
}

IGNORED_STATUS = {"rejected", "declined", "cancelled", "canceled", "withdrawn"}

# Persisted GraphQL query the web app uses to save a day's attendance. If Personio
# changes it, --discover shows the new operationName and hash.
TRACK_OP = "TM_TrackAttendancePeriodsOnDay_v2026071301"
TRACK_HASH = "52d040f7dfa51ff8a9a3cd149cd2da58c5d15ed404f2be69e1d081bcab879c83"


def launch_browser(p, profile_dir: str):
    """Use the installed Chrome or Edge; fall back to Playwright's own Chromium."""
    for channel in ("chrome", "msedge", None):
        try:
            return p.chromium.launch_persistent_context(profile_dir, headless=False,
                                                        channel=channel)
        except Exception as e:
            last = e
    sys.exit("No usable browser found. Install Google Chrome, or run "
             f"`uvx playwright install chromium`.\n{last}")


def month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


# --------------------------------------------------------------------------- #
# HTTP client (Personio web session)
# --------------------------------------------------------------------------- #
class Personio:
    def __init__(self, host: str, profile_dir: str, tz: str):
        self.host = host.rstrip("/")
        self.tz = tz
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json"})
        for c in self._browser_login(profile_dir):
            self.s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])

    def _browser_login(self, profile_dir: str) -> list[dict]:
        from playwright.sync_api import sync_playwright

        ctx_url = f"{self.host}/api/v1/navigation/context"
        with sync_playwright() as p:
            ctx = launch_browser(p, profile_dir)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(self.host)
            console.print("Log in in the browser window (SSO). "
                          "It will close by itself once the session is detected...")
            deadline = time.time() + 600
            while True:
                try:
                    r = ctx.request.get(ctx_url, max_redirects=0,
                                        fail_on_status_code=False)
                    if r.ok and "json" in r.headers.get("content-type", ""):
                        self.person_id = str(r.json()["data"]["user"]["id"])
                        break
                except Exception as e:  # window closed, etc.
                    sys.exit(f"Login cancelled: {e}")
                if time.time() > deadline:
                    ctx.close()
                    sys.exit("Timed out waiting for login (10 min).")
                page.wait_for_timeout(2000)
            cookies = ctx.cookies()
            ctx.close()
        return cookies

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        for attempt in range(6):
            r = self.s.request(method, url, timeout=30, allow_redirects=False, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("Retry-After", "")
                wait = int(ra) if ra.isdigit() else 2 ** attempt
                console.print(f"  [dim]{r.status_code}, retrying in {wait}s...[/]")
                time.sleep(wait)
                continue
            if r.status_code in (301, 302, 401, 419):
                sys.exit(f"Session expired or invalid ({r.status_code}). Run the script "
                         "again to log in.")
            return r
        return r

    def timesheet(self, d0: date, d1: date) -> dict[date, dict]:
        """Timecards per day (attendance, holidays, absences), fetched month by month."""
        cards, d = {}, d0
        while d <= d1:
            e = min(month_end(d), d1)
            r = self._req("GET", f"{self.host}/svc/attendance-bff/v1/timesheet/{self.person_id}",
                          params={"start_date": d.isoformat(), "end_date": e.isoformat(),
                                  "timezone": self.tz})
            if not r.ok:
                sys.exit(f"GET timesheet {d}→{e} failed: {r.status_code} {r.text[:300]}")
            for c in r.json()["timecards"]:
                cards[date.fromisoformat(c["date"])] = c
            d = e + timedelta(days=1)
        return cards

    def track_day(self, d: date, periods: list[dict]) -> tuple[bool, str]:
        body = {"query": TRACK_HASH, "operationName": TRACK_OP,
                "variables": {"input": {
                    "autoFix": False, "origin": "ATTENDANCE_ORIGIN_WEB",
                    "assumedAttributionDate": gql_date(d),
                    "personId": {"id": self.person_id},
                    "attendancePeriods": [dict(p, personId={"id": self.person_id})
                                          for p in periods]}}}
        r = self._req("POST", f"{self.host}/graphql", params={"op": TRACK_OP}, json=body)
        try:
            j = r.json()
        except ValueError:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        if not r.ok or j.get("errors"):
            return False, f"HTTP {r.status_code}: {str(j.get('errors') or j)[:200]}"
        saved = ((j.get("data") or {}).get("trackAttendancePeriodsOnDay") or {}).get("saved") or {}
        if not saved.get("attendancePeriodsList"):
            return False, f"Not saved: {str(j)[:200]}"
        alerts = saved.get("alertsList") or []
        return True, (f"alerts: {str(alerts)[:150]}" if alerts else "")


# --------------------------------------------------------------------------- #
# Day classification
# --------------------------------------------------------------------------- #
def gql_date(d: date) -> dict:
    return {"year": d.year, "month": d.month, "day": d.day}


def is_tracked(card: dict) -> bool:
    return any(p.get("type") == "work" for p in card.get("periods") or [])


def day_status(card: dict) -> tuple[str, str] | None:
    """Holiday/absence/non-working day according to Personio; None for a normal day."""
    items = (card.get("time_off") or {}).get("items") or []
    for it in items:
        if it.get("type") == "holiday":
            return HOLIDAY, it.get("name") or "Holiday"
    for it in items:
        status = str(it.get("status") or "").lower()
        if it.get("type") == "absence" and status not in IGNORED_STATUS:
            label = it.get("name") or "Absence"
            if it.get("half_day_off") not in (None, "none", "unspecified"):
                label += " (half day)"
            if status and status != "approved":
                label += f" [{status}]"
            return ABSENCE, label
    if card.get("is_off_day"):
        return OFF, ""
    if card.get("state") != "trackable":
        return ABSENCE, f"Not editable ({card.get('state')})"
    return None


def classify(args, cards: dict[date, dict]):
    """Returns {date: (category, detail)} for every day in the range."""
    now_local = datetime.now(ZoneInfo(args.tz)).replace(tzinfo=None)
    out: dict[date, tuple[str, str]] = {}
    d = args.date_from
    while d <= args.date_to:
        card = cards.get(d)
        st = day_status(card) if card else None
        if card is None:
            out[d] = (OUTSIDE, "no data in Personio")
        elif is_tracked(card):  # first: an already tracked day is never touched
            out[d] = (DONE, "already tracked")
        elif st:
            out[d] = st
        elif d in args.skip:
            out[d] = (ABSENCE, "Manual (--skip)")
        elif datetime.combine(d, args.end) > now_local:
            out[d] = (FUTURE, "workday not finished yet")
        else:
            out[d] = (TRACK, f"{args.start:%H:%M}-{args.end:%H:%M}")
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def month_table(year: int, month: int, days: dict[date, tuple[str, str]]) -> Table:
    t = Table(title=f"[bold]{MONTHS[month]} {year}[/]", box=box.SIMPLE_HEAD,
              show_edge=False, pad_edge=False, padding=(0, 0))
    for h in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]:
        t.add_column(h, justify="center", width=4,
                     style="grey50" if h in ("Sa", "Su") else None)
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for d in week:
            if d.month != month:
                row.append(Text(""))
                continue
            cat = days.get(d, (OUTSIDE, ""))[0]
            row.append(Text(f" {d.day:>2} ", style=STYLE[cat][0]))
        t.add_row(*row)
    c = Counter(cat for d, (cat, _) in days.items()
                if d.year == year and d.month == month)
    t.caption = (f"[green]{c[TRACK]} track[/]  [blue]{c[DONE]} done[/]  "
                 f"[yellow]{c[ABSENCE]} abs.[/]")
    return t


def ranges(dates: list[date]):
    """Groups consecutive working dates (bridging weekends)."""
    dates = sorted(dates)
    if not dates:
        return []
    out, start, prev = [], dates[0], dates[0]
    for d in dates[1:]:
        gap = (d - prev).days
        bridge = all((prev + timedelta(i)).weekday() >= 5 for i in range(1, gap))
        if gap == 1 or bridge:
            prev = d
            continue
        out.append((start, prev))
        start = prev = d
    out.append((start, prev))
    return out


def fmt_range(a: date, b: date) -> str:
    if a == b:
        return f"{WEEKDAYS[a.weekday()]} {a:%d/%m}"
    return f"{a:%d/%m} → {b:%d/%m}"


def work_hours(args) -> float:
    def mins(a, b):
        return (datetime.combine(date.min, b) - datetime.combine(date.min, a)).seconds / 60
    m = mins(args.start, args.end) - (mins(*args.break_) if args.break_ else 0)
    return m / 60


def show_plan(args, days, person_id):
    c = Counter(cat for cat, _ in days.values())
    console.rule(f"[bold]ATTENDANCE PLAN  {args.date_from:%d/%m/%Y} → "
                 f"{args.date_to:%d/%m/%Y}[/]")
    console.print(f"Person {person_id} · Workday "
                  f"{args.start:%H:%M}-{args.end:%H:%M}"
                  + (f" · Break {args.break_[0]:%H:%M}-{args.break_[1]:%H:%M}"
                     if args.break_ else "") + "\n")

    months, d = [], args.date_from.replace(day=1)
    while d <= args.date_to:
        months.append(month_table(d.year, d.month, days))
        d = month_end(d) + timedelta(days=1)
    console.print(Columns(months, padding=(1, 3), equal=True))

    legend = Text()
    for cat in (TRACK, DONE, ABSENCE, HOLIDAY, OFF, FUTURE):
        legend.append("  ██ ", style=STYLE[cat][0].replace("on ", "").split()[-1])
        legend.append(STYLE[cat][1] + "   ")
    console.print(legend, "\n")

    by_label = defaultdict(list)
    for dd, (cat, det) in days.items():
        if cat == ABSENCE:
            by_label[det].append(dd)
    at = Table(title="Vacations and absences", box=box.ROUNDED, title_justify="left")
    at.add_column("Type", style="yellow")
    at.add_column("Ranges")
    at.add_column("Days", justify="right")
    for lbl, ds in sorted(by_label.items(), key=lambda x: min(x[1])):
        at.add_row(lbl, ", ".join(fmt_range(a, b) for a, b in ranges(ds)), str(len(ds)))
    if not by_label:
        at.add_row("—", "No absences", "0")

    ht = Table(title="Holidays", box=box.ROUNDED, title_justify="left")
    ht.add_column("Date", style="red")
    ht.add_column("Holiday")
    for dd in sorted(x for x, (cat, _) in days.items() if cat == HOLIDAY):
        ht.add_row(f"{WEEKDAYS[dd.weekday()]} {dd:%d/%m}", days[dd][1])

    console.print(Columns([at, ht], padding=(0, 4)))

    ft = Table(title="Days to be tracked", box=box.ROUNDED, title_justify="left")
    ft.add_column("Month", style="bold")
    ft.add_column("Ranges", style="green")
    ft.add_column("Days", justify="right")
    by_month = defaultdict(list)
    for dd, (cat, _) in days.items():
        if cat == TRACK:
            by_month[(dd.year, dd.month)].append(dd)
    for (y, m), ds in sorted(by_month.items()):
        ft.add_row(f"{MONTHS[m]}", ", ".join(fmt_range(a, b) for a, b in ranges(ds)),
                   str(len(ds)))
    console.print(ft)

    console.print(Panel(
        f"[bold green]{c[TRACK]}[/] days to track  ({c[TRACK] * work_hours(args):.0f} h)   "
        f"[bold blue]{c[DONE]}[/] already tracked   "
        f"[bold yellow]{c[ABSENCE]}[/] absence   "
        f"[bold red]{c[HOLIDAY]}[/] holidays",
        title="Summary", expand=False))


def write_csv(path, days):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "weekday", "status", "detail"])
        for d in sorted(days):
            cat, det = days[d]
            w.writerow([d, WEEKDAYS[d.weekday()], cat, det])


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def periods_for(d: date, args) -> list[dict]:
    """Work periods for the day; the break is left as a gap between two periods."""
    def work(a, b):
        return {"id": {"id": str(uuid.uuid4())}, "type": "ATTENDANCE_PERIOD_TYPE_WORK",
                "startLocalTime": {"date": gql_date(d),
                                   "time": {"hours": a.hour, "minutes": a.minute}},
                "endLocalTime": {"date": gql_date(d),
                                 "time": {"hours": b.hour, "minutes": b.minute}},
                "projectId": None, "comment": args.comment,
                "origin": "ATTENDANCE_ORIGIN_WEB", "legacyBreakMinutes": 0,
                "startGeolocation": None, "endGeolocation": None, "duration": None}

    if not args.break_:
        return [work(args.start, args.end)]
    return [work(args.start, args.break_[0]), work(args.break_[1], args.end)]


def apply(api: Personio, args, to_do: list[date]):
    # Re-read right before writing in case something changed since the plan
    fresh = api.timesheet(args.date_from, args.date_to)
    skipped_now = [d for d in to_do if is_tracked(fresh.get(d, {}))]
    to_do = [d for d in to_do if d not in skipped_now]
    if skipped_now:
        console.print(f"[blue]{len(skipped_now)} days were tracked between the plan and "
                      f"now; leaving them as they are.[/]")

    ok, errors, warnings = [], [], []
    with console.status("") as status:
        for i, d in enumerate(to_do, 1):
            status.update(f"Tracking {d:%d/%m/%Y}  ({i}/{len(to_do)})")
            success, msg = api.track_day(d, periods_for(d, args))
            if success:
                ok.append(d)
                if msg:
                    warnings.append((d, msg))
            else:
                errors.append((d, msg))
            time.sleep(0.2)

    after = api.timesheet(args.date_from, args.date_to)
    missing = [d for d in ok if not is_tracked(after.get(d, {}))]

    console.rule("[bold]RESULT[/]")
    console.print(f"[green]Created and verified: {len(ok) - len(missing)}[/]")
    if missing:
        console.print(f"[yellow]Created but missing when re-read: "
                      f"{', '.join(f'{d:%d/%m}' for d in missing)}[/]")
    for title, rows in (("Errors", errors), ("Personio alerts", warnings)):
        if rows:
            et = Table(title=title, box=box.ROUNDED, title_justify="left")
            et.add_column("Date")
            et.add_column("Detail")
            for d, msg in rows:
                et.add_row(f"{d:%d/%m}", msg)
            console.print(et)


# --------------------------------------------------------------------------- #
# Endpoint discovery
# --------------------------------------------------------------------------- #
def discover(host: str, profile_dir: str, out: str):
    """Records the web app's XHR/fetch calls while you browse, to find the real endpoints."""
    from playwright.sync_api import sync_playwright

    calls = []

    def on_response(r):
        req = r.request
        if req.resource_type not in ("xhr", "fetch") or not r.url.startswith(host):
            return
        try:
            body = r.text()[:200000] if "json" in r.headers.get("content-type", "") else ""
        except Exception:
            body = ""
        headers = {k: v for k, v in req.headers.items() if k.lower() != "cookie"}
        calls.append({"method": req.method, "url": r.url, "status": r.status,
                      "request_headers": headers,
                      "request_body": req.post_data, "response_body": body})
        # Saved on every call so nothing is lost if the process dies on close.
        Path(out).write_text(json.dumps(calls, indent=2, ensure_ascii=False))
        console.print(f"[dim]{req.method} {r.status} {r.url[len(host):][:120]}[/]")

    with sync_playwright() as p:
        ctx = launch_browser(p, profile_dir)
        ctx.on("response", on_response)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(host)
        console.print("[bold]Discovery mode.[/] Open your attendance page, switch "
                      "month, open Absences, and track/edit ONE test day by hand. "
                      "Then close the browser.")
        try:
            ctx.wait_for_event("close", timeout=0)
        except Exception:
            pass
    console.print(f"{len(calls)} calls saved to {out}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def hhmm(s: str):
    return datetime.strptime(s, "%H:%M").time()


def parse_dates(spec: str) -> set[date]:
    """'2026-08-03:2026-08-21,2026-12-24' -> set of dates."""
    out = set()
    for part in filter(None, spec.split(",")):
        a, _, b = part.partition(":")
        cur, last = date.fromisoformat(a), date.fromisoformat(b or a)
        while cur <= last:
            out.add(cur)
            cur += timedelta(days=1)
    return out


def self_test():
    assert parse_dates("2026-08-03:2026-08-05,2026-12-24") == {
        date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 12, 24)}

    def card(day, off=False, periods=(), items=None, state="trackable"):
        return {"date": day, "is_off_day": off, "periods": list(periods), "state": state,
                "time_off": {"items": items} if items else None}
    cards = {date.fromisoformat(c["date"]): c for c in [
        card("2026-10-01", periods=[{"type": "work"}]),
        card("2026-10-02"),
        card("2026-10-03", off=True),
        card("2026-10-12", off=True, items=[{"type": "holiday", "name": "National Day"}]),
        card("2026-10-19", off=True, items=[{"type": "absence", "name": "Vacation",
                                             "status": "approved", "half_day_off": "none"}]),
        card("2026-10-20", items=[{"type": "absence", "name": "X", "status": "rejected"}]),
        card("2026-10-21"),
        card("2026-10-22", state="locked"),
    ]}
    a = argparse.Namespace(start=hhmm("09:00"), end=hhmm("18:00"), comment=None,
                           break_=(hhmm("14:00"), hhmm("15:00")), tz="Europe/Madrid",
                           date_from=date(2026, 10, 1), date_to=date(2026, 10, 22),
                           skip={date(2026, 10, 21)})
    days = classify(a, cards)
    assert days[date(2026, 10, 1)][0] == DONE
    assert days[date(2026, 10, 2)][0] == TRACK
    assert days[date(2026, 10, 3)][0] == OFF
    assert days[date(2026, 10, 12)] == (HOLIDAY, "National Day")
    assert days[date(2026, 10, 19)] == (ABSENCE, "Vacation")
    assert days[date(2026, 10, 20)][0] == TRACK  # rejected absence does not count
    assert days[date(2026, 10, 21)] == (ABSENCE, "Manual (--skip)")
    assert days[date(2026, 10, 22)][0] == ABSENCE  # not editable
    assert days[date(2026, 10, 5)][0] == OUTSIDE

    ps = periods_for(date(2026, 3, 2), a)
    assert [(p["startLocalTime"]["time"], p["endLocalTime"]["time"]) for p in ps] == [
        ({"hours": 9, "minutes": 0}, {"hours": 14, "minutes": 0}),
        ({"hours": 15, "minutes": 0}, {"hours": 18, "minutes": 0})]
    assert ps[0]["startLocalTime"]["date"] == {"year": 2026, "month": 3, "day": 2}
    assert work_hours(a) == 8
    print("self-test OK")


def main():
    if "--self-test" in sys.argv:
        return self_test()

    p = argparse.ArgumentParser(description="Fill in missing Personio attendance (SSO session)")
    p.add_argument("--host", default=os.environ.get("PERSONIO_HOST"),
                   help="Your Personio URL, e.g. https://yourcompany.app.personio.com "
                        "(or PERSONIO_HOST)")
    p.add_argument("--from", dest="date_from", type=date.fromisoformat,
                   help="First day, YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", type=date.fromisoformat, default=date.today(),
                   help="Last day, YYYY-MM-DD (default: today)")
    p.add_argument("--start", type=hhmm, default=hhmm("09:00"), help="Default 09:00")
    p.add_argument("--end", type=hhmm, default=hhmm("17:00"), help="Default 17:00")
    p.add_argument("--break", dest="break_", help="Optional break, e.g. 14:00-14:30")
    p.add_argument("--skip", default="",
                   help="Days not to track: 2026-08-03:2026-08-21,2026-12-24")
    p.add_argument("--tz", default="Europe/Madrid")
    p.add_argument("--comment", help="Comment added to every created period")
    p.add_argument("--profile", default=str(Path.home() / ".cache/personio-autofill/profile"),
                   help="Browser profile directory (keeps the SSO session)")
    p.add_argument("--plan-only", action="store_true",
                   help="Only show the plan and exit, without asking")
    p.add_argument("--csv", default="attendance_plan.csv")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--discover", metavar="FILE",
                   help="Record the web app's calls while you browse, then exit")
    args = p.parse_args()

    if not args.host:
        p.error("Missing --host or PERSONIO_HOST")
    args.host = args.host.rstrip("/")
    if args.discover:
        return discover(args.host, args.profile, args.discover)
    if not args.date_from:
        p.error("Missing --from")
    args.skip = parse_dates(args.skip)
    if args.break_:
        a, b = args.break_.split("-")
        args.break_ = (hhmm(a), hhmm(b))
        if not args.start < args.break_[0] < args.break_[1] < args.end:
            p.error("The break must be inside the workday")
    elif not args.start < args.end:
        p.error("--start must be before --end")

    api = Personio(args.host, args.profile, args.tz)
    with console.status("Reading Personio..."):
        cards = api.timesheet(args.date_from, args.date_to)

    days = classify(args, cards)
    show_plan(args, days, api.person_id)
    write_csv(args.csv, days)
    console.print(f"[dim]Day-by-day detail in {args.csv}[/]\n")

    to_do = sorted(d for d, (cat, _) in days.items() if cat == TRACK)
    if not to_do:
        console.print("[bold]No days left to track.[/]")
        return
    if args.plan_only:
        console.print("[bold]Plan mode: nothing was written to Personio.[/]")
        return
    if not Confirm.ask(
            f"[bold]Apply and track {len(to_do)} days in Personio?[/]", default=False):
        console.print("Cancelled. Nothing was written.")
        return
    apply(api, args, to_do)


if __name__ == "__main__":
    main()
