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
- **CloudWatch error logs per realm** (optional) — reads the realms'
  shared ECS log group (`marketplace-prod-logs` by default) once a minute,
  splits the matching lines by site hostname, and adds **Log Errors** and
  **Log Inbox** columns to each location's row. Log Errors is the number
  of PHP error lines in the last 5 minutes against its threshold, and it
  alerts (banner, beep, spoken name) through the same thresholds as the
  error rate. Double-click the Log Inbox cell for every line, the request
  URL, the ECS task and the raw log line
- **CloudTrail change feed** (optional, display only) — a CloudTrail tab
  listing every ECS deploy, scale, create/delete and task stop on the
  Marketplace, SSR and Search API prod clusters, plus the SSR and Search
  API PR environments in the dev account, over the last week: when, which service, what changed and who
  did it (Jenkins, a GitHub Actions workflow, a person). Deploys are also
  drawn as dotted markers on the charts, so a spike can be lined up with
  the deploy before it
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

### CloudWatch logs (AWS login)

CloudWatch tracking reads logs with your own AWS SSO login. Click **AWS
Login** in the top bar (or answer Yes when Start Monitoring says the login
has expired): your browser opens to approve it, and the logs rows pick up
straight away. It runs `aws sso login --profile <profile>` for you, so it
needs the AWS CLI v2 installed.

The profile needs read-only `logs:FilterLogEvents` and
`logs:DescribeLogGroups` on the log group, and for the CloudTrail tab
`cloudtrail:LookupEvents` and `sts:GetCallerIdentity`. `xforce` (Marketplace prod,
us-east-1) is the default; change it on the Settings tab.

## Using it

### Top bar

- **Test Connection** — validates the API key and (if an account ID is set)
  runs a real NRQL query against it. The light turns yellow while testing,
  green on success, red on failure (with the reason shown next to it). If
  CloudWatch tracking is on, the AWS login and log group are checked too
  and the result is appended to the message; a CloudWatch failure doesn't
  turn the light red or stop New Relic monitoring from starting.
- **Start Monitoring / Stop Monitoring** — tests the connection first, then
  begins polling every location and group on the configured interval. While
  waiting for the first results, the dashboard shows "Loading..." rows and a
  progress bar.
- **Refresh Now** — polls immediately instead of waiting for the next
  scheduled interval. If monitoring isn't running yet, this starts it.
- **Silence All** — acknowledges every currently active alert at once.
- **AWS Login** — logs in to AWS for CloudWatch and CloudTrail (`aws sso
  login` for the profiles on the Settings tab, once per SSO session). Prod
  (`xforce`) and the dev account with the PR environments use different
  SSO sessions, so expect two browser approvals. Your browser opens to approve it; once
  done, the connection is rechecked and the logs are read immediately.

### Dashboard tab

The table has one row per location (plus groups) with:

- **Status**: the worse of New Relic and CloudWatch (`OK` / `Anomaly` /
  `Error` / `Warning`). Hover it for both explanations.
- **Error Rate / Threshold**: New Relic.
- **Log Errors**: CloudWatch error lines in the count window against
  their threshold, e.g. `12 / 100`. `⚠ error` means the logs couldn't be
  read (usually an expired AWS login); it doesn't turn the row red.
- **Last Updated** (12-hour clock, e.g. `1:05:03 PM`; hidden when the
  window is narrow).
- **Error Inbox (New Relic)** and **Log Inbox (CloudWatch)**: a summary
  of the latest record, cut to fit the column.

Rows highlight red on anomaly, and also on a chart-metric breach (see
below) even if the error rate itself is fine.

**Reading the full exception without leaving the dashboard:**

- **Hover** an inbox cell for the latest record in full.
- **Click** a row to open the **Details** panel under the table: the full
  latest New Relic error and CloudWatch log line (including the raw line
  with file paths), as selectable text you can copy. **Open Error Inbox**
  / **Open Log Inbox** jump to the full lists; **Hide** or Esc closes it.

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

With CloudWatch tracking on, **Log Errors** is the number of matching
error lines in the count window (default 5 minutes), and its threshold
works like the error rate's (automatic from this session's history, with
a fallback until enough samples exist). A spike raises a **LOG ERROR
SPIKE** banner and the spoken alert. The Log Inbox cell turns green on
new lines; double-click it for the **Log Inbox**. In the Mini View a
location's logs only get their own item while they're alerting.

How lines are matched: all the trader sites log to one ECS log group as
nginx output. PHP messages come through as `[error] ... PHP message:
[...] marketplace.<LEVEL>: ...`. nginx tags every PHP message `[error]`,
NOTICEs included, so the filter keys on the Monolog level instead
(`marketplace.ERROR` and above, plus `PHP Fatal`). Each line is assigned
to a realm by its `host: "www.<site>.com"`.

### CloudTrail tab

A CloudTrail feed of ECS changes on the watched clusters, newest first:
time, app (Marketplace / SSR / Search API), service, what changed, and who
did it. Select a row for the details, including the full CloudTrail event.

- **Deploy**: a new task definition revision (Marketplace via Jenkins) or
  a forced redeploy (SSR and Search API via GitHub Actions). Shown in bold
  and as dotted markers on the dashboard charts; hover a marker for which
  deploy it was.
- **Scale**: Auto Scaling changing the task count. These happen hundreds
  of times a week, so they're hidden unless **Show Auto Scaling** is on.
- **Create / Delete / Stop task / Failed**: service created or deleted, a
  task stopped by hand, or any of these calls failing (in red).

PR environments are listed as **SSR PRs** (services `marketplace-ssr-prN`
in the shared `marketplace-ssr-pr` cluster) and **Search API PRs** (a
cluster per PR, `mp-sapi-pr-env-N`, so its start and teardown show too).
They're in the dev account (profile `MarketplaceDevs-toldev-729320268350`)
and aren't marked on the prod charts. Red "FAILED ... ServiceNotFound" rows
there are usually a workflow redeploying a PR whose environment was
already removed.

Nothing here raises an alert. Hover the ⓘ next to the status line for how
fresh it is: after Start, prod changes appear in about 25 seconds and the
PR environments take about 2–3 minutes to fill in. After that it checks
every 2 minutes. CloudTrail itself can take up to about 15 minutes to
deliver an event, so a deploy may appear a little after it happened.

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

Each location also has a **CloudWatch log host**: the site's hostname in
the logs. Left blank, it's `www.<name>.com` (`RVTrader (ECS)` becomes
`www.rvtrader.com`). TruckTrader is set to `www.commercialtrucktrader.com`.

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
Settings are split into three sub-tabs (General, CloudWatch Logs,
CloudTrail); **Save Settings** below them saves all three.

- **CloudWatch Logs** (applies on the next Start):
  - **Track CloudWatch error logs per realm**: on/off.
  - **AWS profile / region / log group**: where the logs are read from
    (defaults `xforce`, `us-east-1`, `marketplace-prod-logs`).
  - **Filter pattern**: a CloudWatch filter pattern selecting the lines
    to count. The default matches Monolog ERROR and above, plus PHP fatals.
  - **Ignore lines containing**: `;`-separated text. Lines containing
    any of it are dropped before counting, for known noise that's already
    ticketed.
  - **Count window**: how many seconds of logs each count covers (300).
  - **Poll interval**: how often CloudWatch is read (60 seconds).
  - **Min error lines before alerting**: a spike smaller than this never
    alerts, so a quiet site going from 0 to 2 lines stays silent (10).
  - **Fallback threshold**: the line-count threshold used until a realm
    has enough history for its automatic one (100).
- **CloudTrail** (applies on the next Start):
  - **Show ECS deploys/changes**: on/off.
  - **History to show**: hours of changes to load (168, a week; up to 90
    days).
  - **Poll interval**: how often CloudTrail is checked (120 seconds).
  - **Clusters to watch**: one per line, `label | cluster | service prefix
    | profile | region | charts`. Leave the service prefix empty for every
    service in the cluster; profile and region default to `xforce` /
    `us-east-1`. End the cluster with `*` to cover every cluster with that
    prefix (e.g. `mp-sapi-pr-env-*`). `charts` is `yes`/`no`: whether its
    deploys are marked on the dashboard charts.

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

## Questions

If you have any questions, feel free to contact Czyrus Tuazon at
[czyrus.tuazon@traderinteractive.com](mailto:czyrus.tuazon@traderinteractive.com).
