# personio-autofill

Fill in your missing [Personio](https://www.personio.com/) attendance in one go.

It reads your timesheet for a date range, works out which working days have no
attendance yet (skipping weekends, holidays, vacations and other absences), shows
you the plan as a calendar, and only after you confirm it tracks your usual
working hours on those days.

It logs in **as you**, through your normal company SSO in a real browser window.
No API keys, no admin credentials, no passwords stored by the script.

![flow](https://img.shields.io/badge/flow-login%20→%20plan%20→%20confirm%20→%20apply%20→%20verify-blue)

## Why

Personio's public API needs company-wide credentials created by an admin, which
can read and write data for every employee. That is far more access than "fill
in my own attendance" needs. This tool instead reuses your own web session, so
it can only do what you can already do in the Personio web app.

## How it works

1. **Login** – opens your Chrome (or Edge) with a dedicated profile, separate
   from your everyday one. You log in with SSO/MFA
   as usual. The script detects the session, grabs the cookies and closes the
   browser. The profile is kept, so next runs usually skip the login until the
   SSO session expires.
2. **Read** – fetches your timesheet month by month from the same internal
   endpoint the web app uses. For each day it gets: existing attendance,
   holidays from your company holiday calendar, absences and non-working days.
3. **Plan** – classifies every day and prints a monthly calendar, a list of
   absences and holidays, the days that will be tracked and a summary. A CSV
   with the day-by-day detail is written too.
4. **Confirm** – asks `Apply and track N days in Personio? [y/n]`. Default is
   no. Nothing is written unless you answer `y`.
5. **Apply** – re-reads the timesheet (in case something changed meanwhile),
   then saves each day with the same GraphQL call the web app uses.
6. **Verify** – reads the timesheet again and reports what was created, what
   failed and any alerts Personio raised.

### Day rules

| Day | Result |
|-|-|
| Already has attendance | Left untouched, always |
| Holiday in your Personio calendar | Skipped |
| Absence (vacation, sick leave, half day…) not rejected/cancelled | Skipped |
| Non-working day (weekend, part-time off day) | Skipped |
| Not editable in Personio (e.g. locked period) | Skipped |
| Listed in `--skip` | Skipped |
| Workday not finished yet (today before `--end`, future days) | Skipped |
| Anything else | **Tracked** with `--start`–`--end` |

A break is stored as a gap between two work periods (e.g. 09:00–14:00 and
15:00–18:00), the same way it shows up when you track it by hand.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs the Python dependencies on the
  fly from the script header, no virtualenv or `pip install` needed)
- Google Chrome or Microsoft Edge installed (the script uses whichever it
  finds, in that order). If you have neither, install Playwright's Chromium once:

```bash
uvx playwright install chromium
```

Safari and Firefox are not supported: Playwright cannot drive the system Safari,
and Firefox would need its own patched build download anyway.

On macOS, install uv with `brew install uv` (or
`curl -LsSf https://astral.sh/uv/install.sh | sh`).

## Usage

```bash
git clone https://github.com/ichasco/personio-autofill.git
cd personio-autofill

./personio_autofill.py --host https://yourcompany.app.personio.com \
  --from 2026-01-01 --start 09:00 --end 18:00 --break 14:00-15:00
```

`--host` is the URL you see in the browser when you use Personio. You can also
set it once with `export PERSONIO_HOST=https://yourcompany.app.personio.com`.

Try a single day first and check it in the web app before running a whole year:

```bash
./personio_autofill.py --from 2026-10-02 --to 2026-10-02 --start 09:00 --end 18:00
```

### Options

| Option | Default | Description |
|-|-|-|
| `--host` | `$PERSONIO_HOST` | Your Personio URL |
| `--from` | – (required) | First day, `YYYY-MM-DD` |
| `--to` | today | Last day, `YYYY-MM-DD` |
| `--start` / `--end` | `09:00` / `17:00` | Workday hours |
| `--break` | none | Break inside the workday, e.g. `14:00-15:00` |
| `--skip` | none | Extra days not to track: `2026-08-03:2026-08-21,2026-12-24` |
| `--tz` | `Europe/Madrid` | Your timezone |
| `--comment` | none | Comment added to every created period |
| `--plan-only` | off | Show the plan and exit without asking |
| `--csv` | `attendance_plan.csv` | Day-by-day plan output |
| `--profile` | `~/.cache/personio-autofill/profile` | Browser profile that keeps the session |
| `--discover FILE` | – | Record the web app's API calls (see below) |
| `--self-test` | – | Run the offline self-checks |

## When it breaks

This tool talks to **internal, undocumented** Personio endpoints. Personio can
change them at any time. Typical symptoms: `GET timesheet ... failed: 404`, or
every day failing in the apply step.

To see what the web app calls now:

```bash
./personio_autofill.py --host https://yourcompany.app.personio.com --discover discover.json
```

In the browser window open your attendance page, switch month, open your
absences, track or edit one test day by hand and close the browser. Every call
is saved to `discover.json` (cookies are excluded). Look for:

- the `GET .../timesheet/...` call, to update `Personio.timesheet`
- the `POST /graphql?op=TM_TrackAttendancePeriodsOnDay_...` call, to update
  `TRACK_OP` and `TRACK_HASH` at the top of the script

`discover.json` and the CSV contain your personal attendance data and are
ignored by git. Delete them when you are done.

## Security and privacy

- No credentials are stored by the script. The session lives only in the
  browser profile under `~/.cache/personio-autofill/` (delete it to log out).
- Everything runs as your own user, with your own Personio permissions.
- It only writes attendance for the logged-in person, and never modifies a day
  that already has attendance.
- Check your company policy before automating attendance with your session.

## Development

```bash
./personio_autofill.py --self-test
```

## License

MIT
