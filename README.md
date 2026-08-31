# New Relic Anomaly Monitor

A Windows desktop app (Python + Tkinter) that continuously polls New Relic
for error-rate anomalies across individual services ("locations") and
combined groups of them, shows live APM overview charts, watches New
Relic's Errors Inbox for per-error detail, and alerts you audibly and
visually — by name — until you acknowledge each alert.

It talks to New Relic through the NerdGraph (GraphQL) API using NRQL
queries, so it works with whatever your account can already query: APM
apps, services tagged by a custom attribute, container/ECS deployments,
etc.

## Features

- **Connection test** with a red / yellow / green status light
- **Locations** — each one is a name + an NRQL query that returns a single
  numeric error-rate value; not tied to any one data model, so a "location"
  can be an APM app, a store/site, a container, anything NRQL can filter on
- **Combined groups** — average multiple locations into one monitored
  series, or supply your own custom NRQL for the group; a default
  "All Locations Combined" group is included
- **Thresholds** — an automatic default per series (`mean + std-dev
  multiplier × std-dev` of that series' own history so far this session,
  with a flat fallback until enough samples exist), or pin a fixed custom
  threshold per location/group
- **Anomaly alerts** — a red banner names the offending location/group,
  beeps repeatedly, and speaks the location's name out loud via offline
  text-to-speech, repeating until you click **OK** on that specific alert or
  **Silence All**
- **Manual "Refresh Now"** — force an immediate poll instead of waiting out
  the polling interval (also starts monitoring if it isn't running yet)
- **Live APM overview charts** — Web Transaction Time, Apdex, Throughput,
  and Errors %, one line per location, over the last 60 minutes:
  - click a legend name **or** a location's row in the dashboard table to
    isolate that service's line and see its rolling average plotted as a
    dashed reference line (with the value labeled directly on the chart)
  - right-click anywhere on the chart area **or** the table to reset and
    show every service again
  - services breaching a threshold pulse red and grow/shrink in the
    legend, and highlight red on the dashboard row
- **Errors Inbox integration** (optional) — polls individual error
  occurrences per location over the last hour by default (not just the
  aggregate rate), de-duplicated by event ID, for this session only. The
  dashboard shows a live count and the latest error's summary; the first
  poll of a session just establishes the baseline (whatever's already
  sitting in the lookback window) without highlighting anything, so a
  location with a pre-existing backlog doesn't look like something just
  happened. From then on, a location with a genuinely new error since the
  last poll gets its Error Inbox cell highlighted dark green until you
  double-click it (or the row) to open the full detail list (timestamp —
  shown in 12-hour clock format, error class, message, endpoint, HTTP
  method/status, and which container it came from), which also clears the
  highlight
- **Session logs** — every run writes a timestamped, human-readable `.log`
  and a numeric-readings `.csv`, viewable live in the Logs tab
- **One-click installer** (`install.bat` / `install.ps1`) that sets up the
  virtual environment, dependencies, `.env`, app icon, and Desktop shortcut
  — safe to re-run, and works the same way on a brand-new install
- **Desktop shortcut** with a custom neon HUD-style icon, launching with no
  console window

## Setup

### One-step install (recommended, including brand-new installs)

Double-click **install.bat**, or run:

```
powershell -ExecutionPolicy Bypass -File install.ps1
```

This creates the virtual environment, installs all dependencies, creates
`.env` from the template (if it doesn't already exist), generates the
app icon, and creates/refreshes the **Desktop shortcut**. It's safe to
re-run any time — it won't overwrite an existing `.env` or venv, and
re-running just refreshes the icon and shortcut. Use this same script on a
new machine or a fresh copy of this folder.

After it finishes, open `.env` and fill in:

- `NEW_RELIC_API_KEY` — a User API key (starts with `NRAK-`), created at
  https://one.newrelic.com/api-keys
- `NEW_RELIC_ACCOUNT_ID` — the numeric account ID shown on the same page
- `NEW_RELIC_REGION` — `US` or `EU`, depending on your account's data center

Then launch via the Desktop shortcut, or:

```
.venv\Scripts\python.exe main.py
```

### Manual install

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Then fill in `.env` as above and run `python main.py`.

## Using it

### Top bar

- **Test Connection** — validates the API key and (if an account ID is set)
  runs a real NRQL query against it. The light turns yellow while testing,
  green on success, red on failure (with the reason shown next to it).
- **Start Monitoring / Stop Monitoring** — tests the connection first, then
  begins polling every location and group on the configured interval. While
  waiting for the first results, the dashboard shows "Loading..." rows and a
  progress bar.
- **Refresh Now** — polls immediately instead of waiting for the next
  scheduled interval. If monitoring isn't running yet, this starts it.
- **Silence All** — acknowledges every currently active alert at once.

### Dashboard tab

The table lists every location and group with its current error rate,
threshold, status (`OK` / `ANOMALY` / `ERROR`), last-updated time (12-hour
clock, e.g. `1:05:03 PM`), and an Error Inbox summary column. Rows
highlight red on anomaly, and also on a chart-metric breach (see below)
even if the error-rate itself is fine.

- **Click a location's row** to isolate that service everywhere in the
  charts below (same effect as clicking its legend name) — the row stays
  selected so it's obvious which one you're looking at.
- **Right-click anywhere in the table** to reset the charts back to
  showing every service.
- A location's **Error Inbox cell turns dark green** the moment a new
  error shows up for it since the last poll. It stays green — even across
  further polls — until you **double-click** it (or anywhere else on that
  row) to open the detail list, which also clears the highlight. The
  session's very first poll never triggers this — it only establishes the
  starting baseline, so an existing backlog within the lookback window
  doesn't get flagged as if it just happened.

Below the table are four live charts (Web Transaction Time, Apdex,
Throughput, Errors %) covering the last 60 minutes for every enabled
location:

- Click a name in the legend (or a row in the table above) to isolate that
  line — a dashed reference line and a labeled value show its average over
  the displayed window.
- Right-click anywhere on the charts (or the table) to go back to showing
  everyone.
- A pulsing, growing/shrinking red legend entry means that service is
  currently breaching a threshold for that specific metric; its dashboard
  row also turns solid red.

Double-click a row's Error Inbox cell (or the row itself, if it has
records) to open the full list of individual errors caught for that
location this session, with a detail panel per error.

### Locations & Groups tab

Add, edit, or remove **Locations** (name + NRQL query, optionally a custom
threshold) and **Groups** (a set of member locations to average, or a
custom NRQL, optionally a custom threshold). See the tip text in the
Location dialog for the expected NRQL shape — it must return one numeric
column, e.g.:

```sql
SELECT percentage(count(*), WHERE error IS true) AS 'errorRate'
FROM Transaction WHERE appName = 'Checkout Service' SINCE 5 minutes ago
```

**Important for charts and Errors Inbox:** those two features key off the
actual New Relic `appName` your location is filtering on. Include
`appName = 'Your App Name'` (single-quoted) somewhere in the location's
NRQL and the app will pick it up automatically; otherwise it falls back to
using the location's display name, which may not match anything and will
leave that location's chart lines and error inbox empty.

### Settings tab

- **Poll interval** — how often (seconds) to query New Relic.
- **Auto threshold: std-dev multiplier / min samples / fallback %** — tune
  how the automatic per-series threshold is derived.
- **Alert repeat interval** — how often (seconds) an unacknowledged alert
  re-beeps/re-announces.
- **Audible beep on anomaly** / **Speak location name on anomaly** —
  toggle sound and text-to-speech independently.
- **Watch Errors Inbox per container** — toggles the per-error polling
  described above (session log only; doesn't affect the error-rate
  threshold logic).
- **Errors Inbox lookback window** — how far back (seconds) each poll looks
  for error occurrences. Defaults to `3600` (1 hour).
- **Errors Inbox container attribute** — the NRQL attribute name that
  identifies which container/instance an error came from (default
  `containerId`) — useful for ECS/container deployments running multiple
  instances of the same app.
- Region and Account ID are shown read-only here as a reminder; change them
  in `.env` and restart to apply.

### Logs tab

Shows a live tail of the current session's `.log` file. Each run also
writes a `_readings.csv` with every polled value, threshold, and status —
both files live under `logs/`, named with the session's start timestamp.

## Desktop shortcut & icon

The Desktop shortcut **"New Relic Anomaly Monitor"** launches
`.venv\Scripts\pythonw.exe main.py` from the project folder with no console
window, using the icon at `assets/app_icon.ico`. `install.ps1` is what
creates both — run it again any time to recreate them, including on a
brand-new installation of this project.

To only regenerate the icon file itself:

```
.venv\Scripts\python.exe assets\generate_icon.py
```

## Notes

- Changes to `.env` require restarting the app.
- Location/group/threshold/settings configuration is saved to
  `config/app_config.json` and persists between runs.
- The auto-threshold baseline (mean/std-dev) and the Errors Inbox de-dup set
  are both built from readings collected during the current running
  session, not prior sessions.
- Chart breach thresholds for Web Transaction Time (>1s), Apdex (<0.7), and
  Throughput (<1 rpm) are fixed defaults meant as reasonable "something's
  wrong" cutoffs; the Errors % chart reuses each location's own existing
  error-rate threshold instead of a separate fixed one.
