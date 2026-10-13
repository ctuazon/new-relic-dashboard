"""Tkinter desktop UI: connection light, live dashboard, location/group config, settings, logs."""
import math
import queue
import re
import threading
import time
import tkinter as tk
import warnings
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import matplotlib
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.colors import same_color
from matplotlib.lines import Line2D

from . import config, theme
from .alerts import AlertManager
from .aws import AwsLoginError, login_profiles, sso_login
from .logger import SessionLogger
from .monitor import MonitorEngine, logs_key
from .newrelic_client import NewRelicClient, OVERVIEW_METRICS
from .pr_logs_client import PrLogsClient

matplotlib.rcParams["font.family"] = [theme.FONT_FAMILY, "DejaVu Sans"]

POLL_UI_MS = 300
CHART_LOOKBACK_MINUTES = 60
CHART_DPI = 96  # so chart point sizes match Tk's (10.5pt = 14px at 100% scaling)
CHART_FONT_PT = 10.5
CHART_COLUMNS = 2  # the four overview charts sit in a fixed 2x2 grid
CHART_MIN_ROW_HEIGHT = 130  # logical px per chart row; a shorter viewport scrolls instead of squashing
DIM_ALPHA = 0.15
METRIC_SHORT_LABELS = {"response_time": "Resp (s)", "apdex": "Apdex", "throughput": "rpm", "errors": "Err %"}
METRIC_VALUE_FORMATS = {"response_time": "{:.3f}", "apdex": "{:.2f}", "throughput": "{:,.0f}", "errors": "{:.2f}"}
TREE_MAX_ROWS = 12
TREE_NARROW_WIDTH = 1050  # logical px
# Dashboard columns. A location's New Relic series ("loc:<name>") and its CloudWatch logs series
# ("cw:<name>") share one row, keyed by the location's iid.
DASHBOARD_COLUMNS = ("name", "kind", "value", "threshold", "logs", "updated", "errors", "logerrs")
LOG_COLUMNS = ("logs", "logerrs")
STATUS_LABELS = {"ok": "OK", "anomaly": "Anomaly", "error": "Error", "breach": "Warning", "pending": "Loading…"}
CONN_TITLES = {"idle": "Not tested", "testing": "Checking…", "connected": "Connected", "failed": "Disconnected"}

# Industry-standard-ish defaults for the 3 metrics the app doesn't already threshold.
# "errors" deliberately has no fixed default here: it reuses each location's own existing
# error-rate threshold (custom or auto-computed), per the ask to reuse what's already there.
CHART_THRESHOLDS = {
    "response_time": {"direction": "above", "default": 1.0},   # seconds — common "slow" cutoff
    "apdex": {"direction": "below", "default": 0.7},           # Apdex Fair/Poor boundary
    "throughput": {"direction": "below", "default": 1.0},      # rpm — near-zero traffic/outage
}
# CloudTrail change kinds drawn as markers on the charts (Auto Scaling resizes are left off).
CHART_MARKER_KINDS = ("deploy", "create", "delete", "failed")
CHANGE_KIND_LABELS = {"deploy": "Deploy", "scale": "Scale", "create": "Create", "delete": "Delete",
                      "stop": "Stop task", "update": "Update", "failed": "Failed"}
CLOUDTRAIL_TIMING_NOTE = (
    "How fresh this is:\n"
    "• After Start Monitoring, the first load reads the whole history window (a week by default). "
    "Prod changes appear after about 25 seconds; the PR environments are busier and take about "
    "2–3 minutes to fill in.\n"
    "• After that it checks every 2 minutes (Settings → CloudTrail → Poll interval), "
    "which takes a few seconds.\n"
    "• CloudTrail itself can take up to about 15 minutes to report a change, so a deploy may "
    "show up here a little after it happened.")
# The error rate (New Relic) and log error count (CloudWatch) are tracked separately and never
# mixed; only Status combines them. These spell that out wherever the two sit side by side.
NR_ONLY_NOTE = ("Error Rate comes only from New Relic: the % of APM transactions that errored. "
                "CloudWatch log lines are not part of it.")
CW_ONLY_NOTE = ("Log Errors comes only from CloudWatch: a count of error log lines, with its own "
                "threshold. It is not part of the New Relic error rate.")
STATUS_SOURCES_NOTE = "Status shows whichever of the two is worse; their numbers are never combined."
HEADING_TOOLTIPS = {
    "#0": "Worse of New Relic and CloudWatch. " + STATUS_SOURCES_NOTE,
    "value": NR_ONLY_NOTE,
    "threshold": "Threshold for the New Relic error rate (custom or automatic). "
                 "Log Errors (CW) has its own threshold, shown in that column.",
    "logs": CW_ONLY_NOTE + " Shown as count / threshold.",
    "errors": "Latest New Relic APM error for this location.",
    "logerrs": "Latest CloudWatch error log line for this location.",
}
LEGEND_PULSE_INTERVAL_MS = 150
LEGEND_PULSE_AMPLITUDE = 3.5
LEGEND_PULSE_PERIOD_S = 1.0
# A new-records highlight blips green, holds, then fades back into the row; one over its threshold
# stays red until opened.
NEW_ERROR_HOLD_S = 10.0
NEW_ERROR_FADE_S = 1.0
NEW_ERROR_FADE_FRAME_MS = 50


def format_12h_time(epoch_seconds: float) -> str:
    """Formats an epoch timestamp as h:mm:ss AM/PM (no leading zero on the hour)."""
    return time.strftime("%I:%M:%S %p", time.localtime(epoch_seconds)).lstrip("0")


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def format_reading(kind: str, value) -> str:
    """Error rates are percentages; a CloudWatch logs series is a count of error lines."""
    if value is None:
        return "-"
    return f"{value:.0f} lines" if kind == "logs" else f"{value:.2f}%"


class LocationDialog(tk.Toplevel):
    def __init__(self, parent, location=None):
        super().__init__(parent)
        self.title("Location")
        self.result = None
        self.configure(padx=14, pady=14)
        self.resizable(False, False)

        loc = location or config.Location(name="", nrql="")

        ttk.Label(self, text="Name:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.name_var = tk.StringVar(value=loc.name)
        ttk.Entry(self, textvariable=self.name_var, width=42).grid(row=0, column=1, padx=8, pady=6)

        ttk.Label(self, text="NRQL query:").grid(row=1, column=0, sticky="nw", padx=8, pady=6)
        self.nrql_text = tk.Text(self, width=52, height=4)
        self.nrql_text.insert("1.0", loc.nrql)
        self.nrql_text.grid(row=1, column=1, padx=8, pady=6)

        self.enabled_var = tk.BooleanVar(value=loc.enabled)
        ttk.Checkbutton(self, text="Enabled", variable=self.enabled_var).grid(row=2, column=1, sticky="w", padx=8)

        self.custom_var = tk.BooleanVar(value=loc.use_custom_threshold)
        ttk.Checkbutton(self, text="Use custom threshold (%) instead of auto",
                        variable=self.custom_var).grid(row=3, column=1, sticky="w", padx=8)
        self.threshold_var = tk.StringVar(value=str(loc.custom_threshold))
        ttk.Entry(self, textvariable=self.threshold_var, width=10).grid(row=4, column=1, sticky="w", padx=8)

        ttk.Label(self, text="CloudWatch log host:").grid(row=5, column=0, sticky="w", padx=8, pady=6)
        self.log_host_var = tk.StringVar(value=loc.cloudwatch_host if loc.name else "")
        ttk.Entry(self, textvariable=self.log_host_var, width=42).grid(row=5, column=1, padx=8, pady=6, sticky="w")

        ttk.Label(
            self,
            text=("Tip: the query must return one numeric column, e.g.\n"
                  "SELECT percentage(count(*), WHERE error IS true) FROM Transaction\n"
                  "WHERE appName = 'X' SINCE 5 minutes ago\n"
                  "Log host is the site's hostname in the CloudWatch logs; leave blank to use\n"
                  "www.<name>.com (e.g. 'RVTrader (ECS)' -> www.rvtrader.com)."),
            foreground="gray", justify="left",
        ).grid(row=6, column=0, columnspan=2, padx=8, pady=6, sticky="w")

        btns = ttk.Frame(self)
        btns.grid(row=7, column=0, columnspan=2, pady=10)
        ttk.Button(btns, text="Save", command=self._save).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        self.grab_set()

    def _save(self):
        name = self.name_var.get().strip()
        nrql = self.nrql_text.get("1.0", "end").strip()
        if not name or not nrql:
            messagebox.showerror("Missing data", "Name and NRQL query are required.")
            return
        try:
            threshold = float(self.threshold_var.get())
        except ValueError:
            threshold = 5.0
        self.result = config.Location(
            name=name, nrql=nrql, enabled=self.enabled_var.get(),
            use_custom_threshold=self.custom_var.get(), custom_threshold=threshold,
        )
        log_host = self.log_host_var.get().strip().lower()
        # Only store an override; a host matching the name-derived default stays derived.
        if log_host and log_host != self.result.cloudwatch_host:
            self.result.log_host = log_host
        self.destroy()


class GroupDialog(tk.Toplevel):
    def __init__(self, parent, all_location_names, group=None):
        super().__init__(parent)
        self.title("Combined Group")
        self.result = None
        self.configure(padx=14, pady=14)
        self.resizable(False, False)
        grp = group or config.Group(name="", members=[])

        ttk.Label(self, text="Name:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.name_var = tk.StringVar(value=grp.name)
        ttk.Entry(self, textvariable=self.name_var, width=42).grid(row=0, column=1, padx=8, pady=6)

        ttk.Label(self, text="Member locations:").grid(row=1, column=0, sticky="nw", padx=8, pady=6)
        height = min(8, max(3, len(all_location_names)))
        self.listbox = tk.Listbox(self, selectmode="multiple", height=height, exportselection=False)
        for i, n in enumerate(all_location_names):
            self.listbox.insert("end", n)
            if n in grp.members:
                self.listbox.selection_set(i)
        self.listbox.grid(row=1, column=1, padx=8, pady=6, sticky="w")
        ttk.Label(self, text="(none selected = combine all locations)",
                  foreground="gray").grid(row=2, column=1, sticky="w")

        ttk.Label(self, text="Or custom NRQL\n(overrides member averaging):").grid(
            row=3, column=0, sticky="nw", padx=8, pady=6)
        self.nrql_text = tk.Text(self, width=52, height=3)
        self.nrql_text.insert("1.0", grp.custom_nrql or "")
        self.nrql_text.grid(row=3, column=1, padx=8, pady=6)

        self.custom_var = tk.BooleanVar(value=grp.use_custom_threshold)
        ttk.Checkbutton(self, text="Use custom threshold (%) instead of auto",
                        variable=self.custom_var).grid(row=4, column=1, sticky="w", padx=8)
        self.threshold_var = tk.StringVar(value=str(grp.custom_threshold))
        ttk.Entry(self, textvariable=self.threshold_var, width=10).grid(row=5, column=1, sticky="w", padx=8)

        btns = ttk.Frame(self)
        btns.grid(row=6, column=0, columnspan=2, pady=10)
        ttk.Button(btns, text="Save", command=self._save).pack(side="left", padx=6)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        self.grab_set()

    def _save(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Missing data", "Name is required.")
            return
        members = [self.listbox.get(i) for i in self.listbox.curselection()]
        custom_nrql = self.nrql_text.get("1.0", "end").strip() or None
        try:
            threshold = float(self.threshold_var.get())
        except ValueError:
            threshold = 5.0
        self.result = config.Group(
            name=name, members=members, custom_nrql=custom_nrql,
            use_custom_threshold=self.custom_var.get(), custom_threshold=threshold,
        )
        self.destroy()


class ErrorInboxDialog(tk.Toplevel):
    """Lists every error occurrence found for a service this session, with a detail panel
    (New Relic's TransactionError events don't include a response body attribute, so this
    shows everything that actually is captured: timestamp, endpoint, method, status, container)."""

    def __init__(self, parent, state):
        super().__init__(parent)
        self.title(f"Error Inbox (New Relic) — {state.name}")
        scale = parent.ui_scale
        self.geometry(f"{int(1200 * scale)}x{int(600 * scale)}")
        self.records = state.error_records
        self._source_header("New Relic", "TransactionError events from New Relic APM")

        # List on the left, full exception details in a side panel: long messages wrap there
        # instead of being clipped inside grid cells.
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=12, pady=12)

        list_frame = ttk.Frame(paned)
        columns = ("time", "class", "endpoint", "status", "container")
        self.list_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=12)
        headings = {
            "time": "Time", "class": "Error Class", "endpoint": "Endpoint",
            "status": "HTTP Status", "container": "Container",
        }
        widths = {"time": 190, "class": 200, "endpoint": 200, "status": 110, "container": 130}
        for col in columns:
            anchor = "e" if col == "status" else "w"
            self.list_tree.heading(col, text=headings[col], anchor=anchor)
            self.list_tree.column(col, width=int(widths[col] * scale), anchor=anchor)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.list_tree.yview)
        self.list_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.list_tree.pack(side="left", fill="both", expand=True)
        paned.add(list_frame, weight=3)

        detail_frame = ttk.LabelFrame(paned, text="Details", padding=12)
        self.detail_text = tk.Text(detail_frame, width=40, wrap="word", state="disabled",
                                   borderwidth=0, highlightthickness=0, font="TkDefaultFont",
                                   padx=4, pady=4, spacing1=2)
        self.detail_text.pack(fill="both", expand=True)
        paned.add(detail_frame, weight=2)

        for i, rec in enumerate(self.records):
            self.list_tree.insert("", "end", iid=str(i), values=(
                self._fmt_time(rec.timestamp), rec.error_class, rec.endpoint,
                rec.status_code if rec.status_code is not None else "-", rec.container,
            ))

        self.list_tree.bind("<<TreeviewSelect>>", self._show_detail)
        if self.records:
            self.list_tree.selection_set("0")
            self._show_detail()

    def _source_header(self, source, detail):
        """Names the data source at the top of the window, so the two inboxes aren't confused."""
        header = ttk.Frame(self)
        header.pack(fill="x", padx=12, pady=(12, 0))
        ttk.Label(header, text=f"Source: {source}", style="Strong.TLabel").pack(side="left")
        ttk.Label(header, text=f"  ·  {detail}", style="Muted.TLabel").pack(side="left")

    @staticmethod
    def _fmt_time(timestamp):
        if not timestamp:
            return "-"
        date_str = time.strftime("%Y-%m-%d", time.localtime(timestamp))
        return f"{date_str} {format_12h_time(timestamp)}"

    def _show_detail(self, event=None):
        sel = self.list_tree.selection()
        if not sel:
            return
        rec = self.records[int(sel[0])]
        fields = (
            ("Time", self._fmt_time(rec.timestamp)),
            ("Error Class", rec.error_class),
            ("Message", rec.error_message),
            ("Endpoint", rec.endpoint),
            ("HTTP Method", rec.method or "-"),
            ("HTTP Status", rec.status_code if rec.status_code is not None else "-"),
            ("Container", rec.container),
        )
        self.detail_text.config(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.tag_configure("field", font="SunValleyBodyStrongFont")
        self.detail_text.tag_configure("value", spacing3=12)
        for label, value in fields:
            self.detail_text.insert("end", f"{label}\n", "field")
            self.detail_text.insert("end", f"{value}\n", "value")
        self.detail_text.config(state="disabled")


class LogInboxDialog(ErrorInboxDialog):
    """The CloudWatch counterpart of the Error Inbox: every matching log line seen for a realm
    this session, with the parsed fields and the full raw line in the detail panel."""

    def __init__(self, parent, state):
        tk.Toplevel.__init__(self, parent)
        self.title(f"Log Inbox (CloudWatch) — {state.name}")
        scale = parent.ui_scale
        self.geometry(f"{int(1200 * scale)}x{int(600 * scale)}")
        self.records = state.log_records
        self._source_header("CloudWatch", "Error lines from CloudWatch Logs, log group "
                                          f"{parent.cfg.settings.cloudwatch_log_group}")

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=12, pady=12)

        list_frame = ttk.Frame(paned)
        columns = ("time", "level", "message", "url", "task")
        self.list_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=12)
        headings = {"time": "Time", "level": "Level", "message": "Message", "url": "URL", "task": "Task"}
        widths = {"time": 190, "level": 90, "message": 340, "url": 220, "task": 110}
        for col in columns:
            self.list_tree.heading(col, text=headings[col], anchor="w")
            self.list_tree.column(col, width=int(widths[col] * scale), anchor="w")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.list_tree.yview)
        self.list_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.list_tree.pack(side="left", fill="both", expand=True)
        paned.add(list_frame, weight=3)

        detail_frame = ttk.LabelFrame(paned, text="Details", padding=12)
        self.detail_text = tk.Text(detail_frame, width=40, wrap="word", state="disabled",
                                   borderwidth=0, highlightthickness=0, font="TkDefaultFont",
                                   padx=4, pady=4, spacing1=2)
        self.detail_text.pack(fill="both", expand=True)
        paned.add(detail_frame, weight=2)

        for i, rec in enumerate(self.records):
            self.list_tree.insert("", "end", iid=str(i), values=(
                self._fmt_time(rec.timestamp), rec.level, truncate(rec.message, 200),
                rec.url or "-", rec.task_id[:12],
            ))

        self.list_tree.bind("<<TreeviewSelect>>", self._show_detail)
        if self.records:
            self.list_tree.selection_set("0")
            self._show_detail()

    def _show_detail(self, event=None):
        sel = self.list_tree.selection()
        if not sel:
            return
        rec = self.records[int(sel[0])]
        fields = (
            ("Time", self._fmt_time(rec.timestamp)),
            ("Level", rec.level),
            ("Message", rec.message),
            ("Request", f"{rec.method} {rec.url}".strip() or "-"),
            ("Host", rec.host or "-"),
            ("ECS Task", rec.task_id or "-"),
            ("Raw log line", rec.raw),
        )
        self.detail_text.config(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.tag_configure("field", font="SunValleyBodyStrongFont")
        self.detail_text.tag_configure("value", spacing3=12)
        for label, value in fields:
            self.detail_text.insert("end", f"{label}\n", "field")
            self.detail_text.insert("end", f"{value}\n", "value")
        self.detail_text.config(state="disabled")


PR_LOG_RANGES = (("Last 15 minutes", 15), ("Last hour", 60), ("Last 6 hours", 360),
                 ("Last 24 hours", 1440), ("Last 7 days", 10080))
PR_LOG_FOLLOW_MS = 5000
PR_LOG_MAX_LINES = 20000  # follow mode drops the oldest lines past this
PR_LOG_ERROR_WORDS = ("error", "fatal", "exception", "critical", "panic", "uncaught")
PR_LOG_WARN_WORDS = ("warn", "deprecat")
PR_LOG_REGEX_DELAY_MS = 250  # regex filtering re-runs this long after the last keystroke
# A line's own leading timestamp (e.g. PM2's "2026-10-13T17:48:01: "), which only repeats the
# CloudWatch time already shown in the first column.
PR_LOG_LEADING_TIME = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
                                 r"(?:Z|[+-]\d{2}:?\d{2})?\]?:?\s+")
ALL_CONTAINERS = "All containers"


def log_level(message: str) -> str:
    """'error', 'warn' or '' for a log line, from the words in it."""
    lowered = message.lower()
    if any(w in lowered for w in PR_LOG_ERROR_WORDS):
        return "error"
    if any(w in lowered for w in PR_LOG_WARN_WORDS):
        return "warn"
    return ""


class PrLogsDialog(tk.Toplevel):
    """A PR environment's container logs from CloudWatch: the whole window for a time range, or
    followed live (the `tail -f` of the containers) with new lines appended every few seconds."""

    def __init__(self, parent, change, target):
        super().__init__(parent)
        self.app = parent
        self.title(f"PR Logs (CloudWatch) — {change.target} PR #{change.pr_number}")
        scale = parent.ui_scale
        self.geometry(f"{int(1200 * scale)}x{int(680 * scale)}")
        self.minsize(int(1160 * scale), int(400 * scale))  # narrower clips the toolbar
        pal = parent.palette
        self.configure(bg=pal["bg"])
        self.client = PrLogsClient(target.profile, target.region)
        self.cluster, self.service = change.cluster, change.service
        self.containers = []  # ContainerLog per container, once looked up
        self._result = None  # (status, payload) handed from the worker thread to the Tk thread
        self._busy = False
        self._refresh_pending = False  # a refresh asked for mid-load runs once that load is done
        self._follow_job = None
        self._regex_job = None
        self._last_ts = None  # newest line loaded; follow mode fetches only what's after it
        self._lines = []  # (LogLine, display text, level) for everything loaded; filters redraw from it
        self._load_note = ""  # where the lines came from and when, shown after the line count

        def px(n):
            return int(round(n * scale))

        gap, label_gap = px(20), px(6)  # between control groups / between a label and its input

        # Tier 1: what's being shown, on a tinted band. The profile is detail, so it's in a tooltip.
        header = ttk.Frame(self, style="Band.TFrame", padding=(px(14), px(10)))
        header.pack(fill="x")
        service_label = ttk.Label(header, text=self.service, style="BandStrong.TLabel")
        service_label.pack(side="left")
        where_label = ttk.Label(header, text=f"   cluster {self.cluster}   ·   CloudWatch container logs",
                                style="Band.TLabel")
        where_label.pack(side="left")
        info = ttk.Label(header, text="ⓘ", style="BandMuted.TLabel", cursor="question_arrow")
        info.pack(side="right")
        source_help = (f"Service: {self.service}\nCluster: {self.cluster}\nAWS profile: {target.profile}\n"
                       f"Region: {target.region}\n\nRead from the containers' awslogs streams in CloudWatch.")
        for widget in (service_label, where_label, info):
            parent._add_tooltip(widget, source_help)

        # Tier 2: what to load and how to narrow it.
        controls = ttk.Frame(self, padding=(px(12), px(12), px(12), px(8)))
        controls.pack(fill="x")
        ttk.Label(controls, text="Container").pack(side="left", padx=(0, label_gap))
        self.container_var = tk.StringVar(value=ALL_CONTAINERS)
        self.container_combo = ttk.Combobox(controls, textvariable=self.container_var, state="readonly",
                                            width=20, values=[ALL_CONTAINERS])
        self.container_combo.pack(side="left")
        self.container_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(controls, text="Range").pack(side="left", padx=(gap, label_gap))
        self.range_var = tk.StringVar(value=PR_LOG_RANGES[1][0])
        range_combo = ttk.Combobox(controls, textvariable=self.range_var, state="readonly", width=14,
                                   values=[label for label, _m in PR_LOG_RANGES])
        range_combo.pack(side="left")
        range_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        ttk.Label(controls, text="Filter").pack(side="left", padx=(gap, label_gap))
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(controls, textvariable=self.filter_var, width=24)
        self.filter_entry.pack(side="left")
        self.filter_entry.bind("<Return>", lambda e: self._apply_filter(now=True))
        self.filter_entry.bind("<Escape>", lambda e: self._clear_filter())
        self.filter_var.trace_add("write", lambda *_: self._on_filter_typed())
        # The clear button sits inside the entry's right edge, and only while there's text to clear.
        self.clear_btn = tk.Label(self.filter_entry, text="✕", cursor="hand2", padx=px(4),
                                  bg=pal["surface"], fg=pal["muted"], font="TkSmallCaptionFont")
        self.clear_btn.bind("<Button-1>", lambda e: self._clear_filter())
        parent._add_tooltip(self.clear_btn, "Clear the filter (Esc)")
        self.regex_var = tk.BooleanVar(value=False)
        regex_toggle = ttk.Checkbutton(controls, text=".*", style="Toggle.TButton", variable=self.regex_var,
                                       command=self._toggle_regex)
        regex_toggle.pack(side="left", padx=(label_gap, 0))
        parent._add_tooltip(self.filter_entry, "With .* off: a CloudWatch filter pattern, applied when you "
                                               "press Enter. Plain words match lines containing all of them "
                                               "(case-sensitive), e.g. ERROR; ?ERROR ?Fatal matches either.\n\n"
                                               "With .* on: a regular expression, case-insensitive, applied "
                                               "as you type, e.g. timeout|ECONNRESET.")
        parent._add_tooltip(regex_toggle, "Regex: filter with a regular expression as you type, instead of "
                                          "a CloudWatch filter pattern")

        ttk.Label(controls, text="Show").pack(side="left", padx=(gap, label_gap))
        self.errors_only_var = tk.BooleanVar(value=False)
        self.warnings_var = tk.BooleanVar(value=False)
        errors_chip = ttk.Checkbutton(controls, text="Errors", style="Toggle.TButton",
                                      variable=self.errors_only_var, command=self._render)
        errors_chip.pack(side="left")
        warnings_chip = ttk.Checkbutton(controls, text="Warnings", style="Toggle.TButton",
                                        variable=self.warnings_var, command=self._render)
        warnings_chip.pack(side="left", padx=(label_gap, 0))
        parent._add_tooltip(errors_chip, "Only error lines (plus warnings, if that's on too). "
                                         "Neither on shows every line.")
        parent._add_tooltip(warnings_chip, "Only warning lines (plus errors, if that's on too). "
                                           "Neither on shows every line.")
        ttk.Button(controls, text="Refresh", command=self.refresh).pack(side="right")

        # Tier 3: the log, with its view options and status in a footer along the bottom.
        footer = ttk.Frame(self, padding=(px(12), px(6), px(12), px(10)))
        footer.pack(side="bottom", fill="x")
        self.status = ttk.Label(footer, style="Muted.TLabel", text="Looking up the containers' log locations…")
        self.status.pack(side="left", fill="x", expand=True)
        self.follow_var = tk.BooleanVar(value=False)
        follow = ttk.Checkbutton(footer, text="Follow", style="Switch.TCheckbutton", variable=self.follow_var,
                                 command=self._toggle_follow)
        follow.pack(side="right")
        parent._add_tooltip(follow, f"Check for new lines every {PR_LOG_FOLLOW_MS // 1000} s and keep the "
                                    f"newest in view")
        self.wrap_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(footer, text="Wrap lines", style="Switch.TCheckbutton", variable=self.wrap_var,
                        command=self._apply_wrap).pack(side="right", padx=(0, gap))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=px(12))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.text = tk.Text(body, wrap="none", state="disabled", font="TkFixedFont", borderwidth=0,
                            highlightthickness=0, padx=px(8), pady=px(6), bg=pal["surface"], fg=pal["fg"],
                            insertbackground=pal["fg"])
        yscroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.xscroll = ttk.Scrollbar(body, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=self.xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        self.xscroll.grid(row=1, column=0, sticky="ew")
        self.text.tag_configure("time", foreground=pal["muted"])
        self.text.tag_configure("container", foreground=pal["accent"])
        # Level shows as both a row tint and a text color, so it doesn't rest on hue alone.
        self.text.tag_configure("error", foreground=pal["danger"], background=pal["row_anomaly"])
        self.text.tag_configure("warn", foreground=pal["warn"], background=pal["row_error"])

        self.bind("<Control-f>", lambda e: self.filter_entry.focus_set())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._run(lambda: self.client.container_logs(self.cluster, self.service), self._on_containers)

    # ---------- background work ----------
    def _run(self, work, on_done):
        """Runs `work` off the Tk thread and hands its result to `on_done` on the Tk thread."""
        self._busy = True

        def worker():
            try:
                self._result = ("ok", work())
            except Exception as e:  # AwsError carries a readable message; anything else is shown as-is
                self._result = ("error", e)

        threading.Thread(target=worker, daemon=True).start()
        self._poll(on_done)

    def _poll(self, on_done):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if self._result is None:
            self.after(100, self._poll, on_done)
            return
        status, payload = self._result
        self._result, self._busy = None, False
        if status == "error":
            hint = " Click AWS Login in the main window, then Refresh." if isinstance(payload, AwsLoginError) else ""
            self.status.config(text=f"Couldn't load the logs: {payload}{hint}")
        else:
            on_done(payload)
        if self._refresh_pending and not self._busy:
            self.refresh()

    # ---------- loading ----------
    def _on_containers(self, containers):
        self.containers = containers
        self.container_combo.configure(values=[ALL_CONTAINERS] + [c.container for c in containers])
        self.refresh()

    def _selected_containers(self):
        name = self.container_var.get()
        return [c for c in self.containers if name in (ALL_CONTAINERS, c.container)]

    def _range_minutes(self):
        return next(m for label, m in PR_LOG_RANGES if label == self.range_var.get())

    def _server_pattern(self):
        """The CloudWatch filter pattern; a regex filter is applied here instead, so it sends none."""
        return "" if self.regex_var.get() else self.filter_var.get().strip()

    def refresh(self):
        if self._busy:
            self._refresh_pending = True
            return
        self._refresh_pending = False
        if not self.containers:  # the lookup failed earlier; try it again
            self.status.config(text="Looking up the containers' log locations…")
            self._run(lambda: self.client.container_logs(self.cluster, self.service), self._on_containers)
            return
        containers, pattern = self._selected_containers(), self._server_pattern()
        since = time.time() - self._range_minutes() * 60
        self.status.config(text="Loading…")
        self._run(lambda: self.client.get_lines(containers, since, pattern), self._show_lines)

    def _show_lines(self, result):
        lines, truncated = result
        self._lines = [self._entry(line) for line in lines]
        self._last_ts = lines[-1].timestamp if lines else None
        where = self.container_var.get().lower()
        note = " (cut short: narrow the range or add a filter to see everything)" if truncated else ""
        self._load_note = (f"from {where}, {self.range_var.get().lower()}{note} · "
                           f"loaded {format_12h_time(time.time())}")
        self._render()
        self._schedule_follow()

    @staticmethod
    def _entry(line):
        text = PR_LOG_LEADING_TIME.sub("", line.message, count=1)
        return line, text, log_level(text)

    # ---------- drawing ----------
    def _line_filter(self):
        """(predicate, problem) for the client-side filters (level chips and regex); the predicate
        is None when none are on, and problem explains a regex that doesn't compile."""
        levels = {level for level, var in (("error", self.errors_only_var), ("warn", self.warnings_var))
                  if var.get()}
        regex, problem = None, ""
        if self.regex_var.get() and self.filter_var.get().strip():
            try:
                regex = re.compile(self.filter_var.get().strip(), re.IGNORECASE)
            except re.error as e:
                problem = f"regex not applied: {e}"
        if not levels and regex is None:
            return None, problem
        return (lambda entry: (not levels or entry[2] in levels)
                and (regex is None or regex.search(entry[1]) is not None)), problem

    def _render(self):
        """Redraws every loaded line that passes the filters, keeping the newest in view."""
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")
        self._insert(self._lines)
        self.text.see("end")

    def _insert(self, entries):
        keep, problem = self._line_filter()
        many = len(self._selected_containers()) > 1
        width = max((len(c.container) for c in self.containers), default=0) if many else 0
        # Wrapped lines indent under the message, past the time (and container) column.
        prefix_chars = len("00-00 00:00:00  ") + (width + 2 if many else 0)
        self.text.tag_configure("line", lmargin2=tkfont.nametofont("TkFixedFont").measure("0" * prefix_chars))
        self.text.config(state="normal")
        for line, text, level in entries:
            if keep is not None and not keep((line, text, level)):
                continue
            stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(line.timestamp))
            self.text.insert("end", stamp + "  ", ("time", "line"))
            if many:
                self.text.insert("end", line.container.ljust(width) + "  ", ("container", "line"))
            self.text.insert("end", text + "\n", (level, "line") if level else "line")
        excess = int(self.text.index("end-1c").split(".")[0]) - PR_LOG_MAX_LINES
        if excess > 0:
            self.text.delete("1.0", f"{excess + 1}.0")
        self.text.config(state="disabled")
        total = len(self._lines)
        shown = total if keep is None else sum(1 for e in self._lines if keep(e))
        count = f"{total} line{'s' if total != 1 else ''}" if keep is None else f"{shown} of {total} lines"
        self.status.config(text=" · ".join(p for p in (f"{count} {self._load_note}".strip(), problem) if p))

    def _apply_wrap(self):
        top, at_bottom = self.text.index("@0,0"), self.text.yview()[1] >= 0.999
        if self.wrap_var.get():
            self.text.configure(wrap="word")
            self.xscroll.grid_remove()
        else:
            self.text.configure(wrap="none")
            self.xscroll.grid()
        # Tk keeps stale hanging indents on lines drawn before the switch, so draw them afresh,
        # staying on the same line unless the view was following the end.
        self._render()
        if not at_bottom:
            self.text.yview(top)

    # ---------- filter ----------
    def _on_filter_typed(self):
        if self.filter_var.get():
            self.clear_btn.place(relx=1.0, rely=0.5, x=-2, anchor="e")
        else:
            self.clear_btn.place_forget()
        if self.regex_var.get():  # a regex filters as you type; a CloudWatch pattern waits for Enter
            self._apply_filter()

    def _apply_filter(self, now=False):
        if self._regex_job is not None:
            self.after_cancel(self._regex_job)
            self._regex_job = None
        if not self.regex_var.get():
            if now:
                self.refresh()
        elif now:
            self._render()
        else:
            self._regex_job = self.after(PR_LOG_REGEX_DELAY_MS, self._apply_filter, True)

    def _clear_filter(self):
        had_pattern = bool(self._server_pattern())
        self.filter_var.set("")
        if had_pattern:
            self.refresh()
        else:
            self._render()
        self.filter_entry.focus_set()

    def _toggle_regex(self):
        # The text moves between CloudWatch and here, so reload when it was (or now isn't) sent there.
        if self.filter_var.get().strip():
            self.refresh()
        else:
            self._render()

    # ---------- follow (tail -f) ----------
    def _toggle_follow(self):
        if self.follow_var.get():
            self.text.see("end")
        self._schedule_follow()

    def _schedule_follow(self):
        if self._follow_job is not None:
            self.after_cancel(self._follow_job)
            self._follow_job = None
        if self.follow_var.get():
            self._follow_job = self.after(PR_LOG_FOLLOW_MS, self._follow_tick)

    def _follow_tick(self):
        self._follow_job = None
        if self._busy or not self.containers:
            self._schedule_follow()
            return
        # CloudWatch times are whole milliseconds, so start 1 ms after the newest line loaded.
        since = (self._last_ts + 0.001) if self._last_ts else time.time() - PR_LOG_FOLLOW_MS / 1000
        containers, pattern = self._selected_containers(), self._server_pattern()

        def done(result):
            lines, _truncated = result
            at_bottom = self.text.yview()[1] >= 0.999
            entries = [self._entry(line) for line in lines]
            self._lines = (self._lines + entries)[-PR_LOG_MAX_LINES:]
            if lines:
                self._last_ts = lines[-1].timestamp
            self._load_note = f"following, {len(lines)} new · checked {format_12h_time(time.time())}"
            self._insert(entries)
            if at_bottom:
                self.text.see("end")
            self._schedule_follow()

        self._run(lambda: self.client.get_lines(containers, since, pattern), done)

    def _close(self):
        for job in (self._follow_job, self._regex_job):
            if job is not None:
                self.after_cancel(job)
        self.destroy()


MINI_FONT =(theme.FONT_FAMILY, 10)
MINI_FONT_BOLD = (theme.FONT_FAMILY_STRONG, 10)
MINI_ICON_SIZE = 13
MINI_REFRESH_MS = 500
MINI_STATUS_STYLES = {  # status -> (text, icon name, palette color key)
    "OK": ("OK", "ok", "ok"),
    "ANOMALY": ("ALERT", "anomaly", "danger"),
    "ERROR": ("ERR", "error", "caution"),
    "PENDING": ("...", "pending", "pending"),
    "BREACH": ("WARN", "breach", "warn"),
}
MINI_NEW_ERROR_TEXT = "[!]"


def short_name(name: str) -> str:
    """'CycleTrader (ECS)' -> 'Cycle', 'All Locations Combined' -> 'All' — keeps the strip thin."""
    base = name.split("(")[0].strip()
    if base.endswith("Trader") and len(base) > len("Trader"):
        return base[:-len("Trader")]
    return base.split()[0] if " " in base else base


class MiniView(tk.Toplevel):
    """Borderless always-on-top status strip shown while the main window is minimized.
    Mirrors the dashboard status per series and offers ways back into the full UI."""

    def __init__(self, app: "App"):
        super().__init__(app)
        self.app = app
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        self.body = tk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=1, pady=1)

        self.handle = tk.Label(self.body, text="⋮⋮", font=MINI_FONT, cursor="fleur")
        self.handle.pack(side="left", padx=(6, 2), pady=3)
        # Shape-coded icon (not just a colored dot) so the connection state reads without color.
        self.light = tk.Label(self.body)
        self.light.pack(side="left", padx=(2, 8))

        self.items_frame = tk.Frame(self.body)
        self.items_frame.pack(side="left")
        self.placeholder = tk.Label(self.items_frame, text="Not monitoring", font=MINI_FONT)
        self._items = {}  # key -> (frame, name_label, status_label, new_error_label)

        self.close_btn = close_btn = tk.Label(self.body, text="✕", font=MINI_FONT, cursor="hand2")
        close_btn.pack(side="right", padx=(2, 8))
        close_btn.bind("<Button-1>", lambda e: self.app.show_full_view())
        self.restore_btn = restore_btn = tk.Label(self.body, text="⤢", font=(theme.FONT_FAMILY, 12),
                                                  cursor="hand2")
        restore_btn.pack(side="right", padx=2)
        restore_btn.bind("<Button-1>", lambda e: self.app.show_full_view())
        self.silence_btn = tk.Label(self.body, text="Silence", bg="#b00020", fg="white",
                                    font=MINI_FONT_BOLD, padx=6, cursor="hand2")
        self.silence_btn.bind("<Button-1>", lambda e: self.app.alert_manager.acknowledge_all())
        self._fixed_widgets = [self.body, self.handle, self.light, self.items_frame, self.placeholder,
                               close_btn, restore_btn]

        for w in (self.body, self.handle, self.items_frame, self.placeholder):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", self._end_drag)
        for w in self._fixed_widgets:
            w.bind("<Button-3>", self._show_menu)
            w.bind("<Double-1>", lambda e: self.app.show_full_view())
        self._drag_offset = (0, 0)
        self._shown = False
        self.apply_theme()

    def apply_theme(self):
        pal = self.app.palette
        self.config(bg=pal["mini_border"])
        self.handle.config(fg=pal["mini_muted"])
        self.placeholder.config(fg=pal["mini_muted"])
        self.close_btn.config(fg=pal["mini_muted"])
        self.restore_btn.config(fg=pal["mini_fg"])
        for _frame, name_label, *_ in self._items.values():
            name_label.config(fg=pal["mini_fg"])
        self.refresh()

    # ---------- visibility ----------
    def show(self):
        s = self.app.cfg.settings
        self.refresh()
        self.update_idletasks()
        if s.mini_view_x is not None and s.mini_view_y is not None:
            x, y = s.mini_view_x, s.mini_view_y
        else:
            x = (self.winfo_screenwidth() - self.winfo_reqwidth()) // 2
            y = 4
        # Keep it reachable if the saved spot is now off-screen (e.g. monitor unplugged).
        x = min(max(x, self.winfo_vrootx()), self.winfo_vrootx() + self.winfo_vrootwidth() - 40)
        y = min(max(y, self.winfo_vrooty()), self.winfo_vrooty() + self.winfo_vrootheight() - 20)
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self._shown = True
        self.after(MINI_REFRESH_MS, self._tick)

    def hide(self):
        self._shown = False
        self.withdraw()

    def _tick(self):
        if not self._shown:
            return
        self.refresh()
        # Some apps (fullscreen video, other topmost windows) can push us down; re-assert.
        self.attributes("-topmost", True)
        self.after(MINI_REFRESH_MS, self._tick)

    # ---------- rendering ----------
    def refresh(self):
        app = self.app
        pal = app.palette
        self.light.config(image=app.icon(f"conn_{app.conn_state}", MINI_ICON_SIZE))

        active_alerts = [a for a in app.alert_manager.active_alerts() if not a.acknowledged]
        bg = pal["mini_alert_bg"] if active_alerts else pal["mini_bg"]
        if active_alerts:
            self.silence_btn.pack(side="right", padx=(2, 4), after=self.restore_btn)
        else:
            self.silence_btn.pack_forget()

        # A logs series only takes up room on the strip while it's alerting; 19 items won't fit.
        states = [s for s in list(app.monitor.states.values())
                  if s.kind != "logs" or s.status == "ANOMALY"]
        keys = [s.key for s in states]
        if list(self._items.keys()) != keys:
            for frame, *_ in self._items.values():
                frame.destroy()
            self._items = {}
            for state in states:
                self._items[state.key] = self._make_item(state)

        if states:
            self.placeholder.pack_forget()
        else:
            self.placeholder.pack(side="left", padx=4)

        name_to_app = {loc.name: loc.app_name for loc in app.cfg.locations}
        for state in states:
            frame, name_label, status_label, new_error_label = self._items[state.key]
            status = state.status
            if status == "OK" and state.kind == "location":
                app_name = name_to_app.get(state.name)
                if app_name and app._breaching_apps.get(app_name):
                    status = "BREACH"
            text, icon_name, color_key = MINI_STATUS_STYLES.get(status, (status, "pending", "mini_fg"))
            status_label.config(text=text, fg=pal[color_key], image=app.icon(icon_name, MINI_ICON_SIZE),
                                compound="left",
                                font=MINI_FONT_BOLD if status in ("ANOMALY", "BREACH") else MINI_FONT)
            new_error_label.config(fg=pal["mini_new_error"])
            if state.key in app._new_error_iids:
                if not new_error_label.winfo_manager():
                    new_error_label.pack(side="left", padx=(3, 0))
            else:
                new_error_label.pack_forget()

        if not app.monitor.is_running() and states:
            self.placeholder.config(text="(stopped)")
            self.placeholder.pack(side="left", padx=4)
        else:
            self.placeholder.config(text="Not monitoring")

        for w in [*self._fixed_widgets, self.light]:
            w.config(bg=bg)
        for widgets in self._items.values():
            for w in widgets:
                w.config(bg=bg)

    def _make_item(self, state):
        frame = tk.Frame(self.items_frame, cursor="hand2")
        frame.pack(side="left", padx=(4, 12))
        label = short_name(state.name) + (" logs" if state.kind == "logs" else "")
        name_label = tk.Label(frame, text=label, fg=self.app.palette["mini_fg"], font=MINI_FONT)
        name_label.pack(side="left")
        status_label = tk.Label(frame, text="", font=MINI_FONT)
        status_label.pack(side="left", padx=(4, 0))
        # Packed/unpacked by refresh() depending on whether the series has unseen errors.
        new_error_label = tk.Label(frame, text=MINI_NEW_ERROR_TEXT, fg=self.app.palette["mini_new_error"],
                                   font=MINI_FONT_BOLD, cursor="hand2")
        for w in (frame, name_label, status_label):
            w.bind("<Button-1>", lambda e, k=state.key: self.app.show_full_view(focus_key=k))
        new_error_label.bind("<Button-1>", lambda e, k=state.key: self._open_error_inbox(k))
        for w in (frame, name_label, status_label, new_error_label):
            w.bind("<Button-3>", self._show_menu)
        return frame, name_label, status_label, new_error_label

    def _open_error_inbox(self, key):
        self.app.open_error_inbox(key)
        self.refresh()  # drop the [!] immediately rather than on the next tick

    # ---------- interaction ----------
    def _start_drag(self, event):
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _on_drag(self, event):
        dx, dy = self._drag_offset
        self.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _end_drag(self, event):
        s = self.app.cfg.settings
        s.mini_view_x, s.mini_view_y = self.winfo_x(), self.winfo_y()
        self.app.cfg.save()

    def _show_menu(self, event):
        app = self.app
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Restore full view", command=app.show_full_view)
        for text, tab in (("Dashboard", app.dashboard_tab), ("CloudTrail", app.changes_tab),
                          ("Locations & Groups", app.locations_tab),
                          ("Settings", app.settings_tab), ("Logs", app.logs_tab)):
            menu.add_command(label=f"Open {text}", command=lambda t=tab: app.show_full_view(tab=t))
        menu.add_separator()
        running = app.monitor.is_running()
        menu.add_command(label="Stop Monitoring" if running else "Start Monitoring",
                         command=app._toggle_monitoring)
        menu.add_command(label="Refresh Now", command=app._refresh_now)
        menu.add_command(label="Silence All", command=app.alert_manager.acknowledge_all)
        menu.add_separator()
        menu.add_command(label="Exit", command=app._on_close)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()


class App(tk.Tk):
    def __init__(self):
        theme.enable_dpi_awareness()  # before the Tk root exists, so Tk sees the real DPI
        super().__init__()
        self.title("New Relic Anomaly Monitor")
        self.ui_scale = self.winfo_fpixels("1i") / 96.0
        width = min(self._px(1280), int(self.winfo_screenwidth() * 0.92))
        height = min(self._px(940), int(self.winfo_screenheight() * 0.88))
        self.geometry(f"{width}x{height}")
        self.minsize(self._px(760), self._px(560))

        self.cfg = config.AppConfig.load()
        self.theme_name = self.cfg.settings.theme or theme.system_theme()
        self.palette = theme.apply_theme(self, self.theme_name, self.ui_scale)
        self._icons = {}
        self.conn_state = "idle"
        self.session_logger = SessionLogger()
        self.client = NewRelicClient()
        self.event_queue: "queue.Queue" = queue.Queue()
        self._alert_widgets = {}
        self._awaiting_first_update = False
        self._chart_colors = {}
        self._charts_fetching = False
        self._chart_lines = {}
        self._legend_app_by_artist = {}
        self._legend_texts = {}
        self._legend_handles = {}
        self._highlighted_app = None
        self._breaching_apps = {}
        self._avg_annotations = {}
        self._new_error_iids = set()
        self._prev_newest_error_guid = {}
        self._error_cell_overlays = {}
        self._new_error_at = {}  # key -> when its highlight started (or last stood over its threshold)
        self._overlay_tick_id = None
        self._row_separators = {}
        self._last_metrics = None
        self._legend_names = []
        self._chart_timeline = []
        self._chart_values = {}
        self._crosshairs = {}
        self._crosshair_idx = None
        self._chart_relayout_job = None
        self._legend_px = 0  # measured height of the chart legend, reserved above the charts
        self._tree_hover_key = None
        self._tree_narrow = False
        self._changes_signature = None
        self._changes_shown = []
        self._avg_char_px = None
        self._details_row = None  # dashboard row whose details panel is open
        self._details_text_cache = None

        self.alert_manager = AlertManager(
            on_change=lambda: self.event_queue.put(("alerts_changed", None)),
            sound_enabled=self.cfg.settings.sound_enabled,
            tts_enabled=self.cfg.settings.tts_enabled,
            repeat_seconds=self.cfg.settings.alert_repeat_seconds,
        )
        self.monitor = MonitorEngine(
            client=self.client, cfg=self.cfg, logger=self.session_logger,
            on_update=lambda: self.event_queue.put(("update", None)),
            on_anomaly=lambda key, name, value, threshold: self.event_queue.put(
                ("anomaly", (key, name, value, threshold))),
            on_recover=lambda key: self.event_queue.put(("recover", key)),
        )

        self.tooltip = theme.Tooltip(self, lambda: self.palette, wraplength=self._px(520))
        self._build_ui()
        self.mini_view = MiniView(self)
        self.bind("<Map>", self._on_main_map)
        self.after(POLL_UI_MS, self._drain_queue)
        self.after(LEGEND_PULSE_INTERVAL_MS, self._animate_legend)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- theming helpers ----------
    def _px(self, n):
        """Logical (100%-scaling) pixels -> physical pixels at the current DPI."""
        return int(round(n * self.ui_scale))

    @property
    def chart_font(self):
        return CHART_FONT_PT * theme.text_scale_factor()

    def icon(self, name, size=18):
        key = (name, size)
        if key not in self._icons:
            self._icons[key] = theme.make_icon(self, name, self.palette, self._px(size))
        return self._icons[key]

    def _toggle_theme(self):
        name = "dark" if self.dark_var.get() else "light"
        if name == self.theme_name:
            return
        self.theme_name = name
        self.cfg.settings.theme = name
        self.cfg.save()
        self.palette = theme.apply_theme(self, name, self.ui_scale)
        self._icons = {}  # icons bake in palette colors; regenerated lazily below
        self._style_non_ttk_widgets()
        self._apply_tree_tags()
        self._changes_signature = None  # re-render so the row colors follow the theme
        self._render_changes()
        for iid in list(self._error_cell_overlays):
            self._remove_error_overlay(iid)
        for sep in self._row_separators.values():
            sep.config(bg=self.palette["border"])
        self._set_connection(self.conn_state, self._conn_message)
        self._render_dashboard()
        if self._last_metrics is not None:
            self._render_charts(self._last_metrics)
        else:
            self.chart_fig.set_facecolor(self.palette["chart_bg"])
            for key, title, _select in OVERVIEW_METRICS:
                self._style_axis(self.chart_axes[key], title)
            self.chart_canvas.draw_idle()
        self.mini_view.apply_theme()

    def _style_non_ttk_widgets(self):
        pal = self.palette
        for lb in (self.loc_listbox, self.grp_listbox):
            lb.config(bg=pal["surface"], fg=pal["fg"], selectbackground=pal["select"],
                      selectforeground=pal["fg"], highlightbackground=pal["border"],
                      highlightcolor=pal["border"])
        self.log_text.config(bg=pal["surface"], fg=pal["fg"], insertbackground=pal["fg"])
        self.change_detail.config(bg=pal["surface"], fg=pal["fg"], insertbackground=pal["fg"])
        self.ct_targets_text.config(bg=pal["surface"], fg=pal["fg"], insertbackground=pal["fg"])
        self.chart_viewport.config(bg=pal["chart_bg"])
        self.details_text.config(bg=pal["surface"], fg=pal["fg"], selectbackground=pal["select"],
                                 selectforeground=pal["fg"])

    # ---------- UI construction ----------
    def _build_ui(self):
        # Command bar: connection status on the left, grouped actions on the right.
        bar = ttk.Frame(self, padding=(self._px(16), self._px(12)))
        bar.pack(fill="x")

        # Actions are packed first so that, if space runs out, the status message is what gets clipped.
        self.dark_var = tk.BooleanVar(value=self.theme_name == "dark")
        ttk.Checkbutton(bar, text="Dark mode", style="Switch.TCheckbutton", variable=self.dark_var,
                        command=self._toggle_theme).pack(side="right", padx=(self._px(12), 0))
        ttk.Button(bar, text="Mini View", command=self.show_mini_view).pack(side="right", padx=self._px(4))
        ttk.Separator(bar, orient="vertical").pack(side="right", fill="y", padx=self._px(6))
        ttk.Button(bar, text="Silence All", command=self.alert_manager.acknowledge_all).pack(
            side="right", padx=self._px(4))
        clear_new_btn = ttk.Button(bar, text="Clear All New", command=self._clear_new_errors)
        clear_new_btn.pack(side="right", padx=self._px(4))
        self._add_tooltip(clear_new_btn, "Marks every container's new errors as seen, removing the "
                                         "green highlights without opening each inbox.")
        ttk.Separator(bar, orient="vertical").pack(side="right", fill="y", padx=self._px(6))
        ttk.Button(bar, text="Test Connection", command=self._test_connection).pack(side="right", padx=self._px(4))
        self.aws_login_btn = ttk.Button(bar, text="AWS Login", command=self._aws_login)
        self.aws_login_btn.pack(side="right", padx=self._px(4))
        self.refresh_btn = ttk.Button(bar, text="Refresh Now", command=self._refresh_now)
        self.refresh_btn.pack(side="right", padx=self._px(4))
        self.start_btn = ttk.Button(bar, text="Start Monitoring", style="Accent.TButton",
                                    command=self._toggle_monitoring)
        self.start_btn.pack(side="right", padx=self._px(4))

        self.conn_icon = ttk.Label(bar)
        self.conn_icon.pack(side="left", padx=(0, self._px(8)))
        self.status_title = ttk.Label(bar, style="Strong.TLabel")
        self.status_title.pack(side="left")
        self.status_detail = ttk.Label(bar, style="Muted.TLabel")
        self.status_detail.pack(side="left", padx=(self._px(10), 0))
        self.status_detail.bind("<Enter>", self._show_status_tooltip)
        self.status_detail.bind("<Leave>", lambda e: self.tooltip.hide())
        self._conn_message = ""

        self.loading_bar = ttk.Progressbar(bar, mode="indeterminate", length=self._px(110))
        self.loading_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self.loading_label.pack(side="left", padx=(self._px(12), 0))
        ttk.Separator(self, orient="horizontal").pack(fill="x")
        self._set_connection("idle", "")

        self.alert_banner = tk.Frame(self, bg="#b00020")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=self._px(16), pady=(self._px(12), self._px(16)))

        pad = self._px(12)
        self.dashboard_tab = ttk.Frame(self.notebook, padding=pad)
        self.changes_tab = ttk.Frame(self.notebook, padding=pad)
        self.locations_tab = ttk.Frame(self.notebook, padding=pad)
        self.settings_tab = ttk.Frame(self.notebook, padding=pad)
        self.logs_tab = ttk.Frame(self.notebook, padding=pad)

        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.changes_tab, text="CloudTrail")
        self.notebook.add(self.locations_tab, text="Locations & Groups")
        self.notebook.add(self.settings_tab, text="Settings")
        self.notebook.add(self.logs_tab, text="Logs")

        self._build_dashboard_tab()
        self._build_changes_tab()
        self._build_locations_tab()
        self._build_settings_tab()
        self._build_logs_tab()
        self._style_non_ttk_widgets()

    def _build_dashboard_tab(self):
        frame = self.dashboard_tab
        # The tree column (#0) holds the status badge: a shape-coded icon plus a text label.
        self.tree = ttk.Treeview(frame, columns=DASHBOARD_COLUMNS, show="tree headings", height=1)
        self.tree.heading("#0", text="Status", anchor="w")
        self.tree.column("#0", width=self._px(140), minwidth=self._px(120), anchor="w", stretch=False)
        headings = {
            "name": "Name", "kind": "Type", "value": "Error Rate (NR)", "threshold": "Threshold (NR)",
            "logs": "Log Errors (CW)", "updated": "Last Updated", "errors": "Error Inbox (New Relic)",
            "logerrs": "Log Inbox (CloudWatch)",
        }
        # Text left-aligned, numbers right-aligned so values line up for vertical scanning.
        layout = {"name": (175, "w"), "kind": (80, "w"), "value": (115, "e"), "threshold": (115, "e"),
                  "logs": (115, "e"), "updated": (110, "e"), "errors": (240, "w"), "logerrs": (240, "w")}
        for col in DASHBOARD_COLUMNS:
            width, anchor = layout[col]
            self.tree.heading(col, text=headings[col], anchor=anchor)
            # Only the two inbox summaries flex, so names and numbers keep their width.
            flex = col in ("errors", "logerrs")
            self.tree.column(col, width=self._px(width), minwidth=self._px(140 if flex else width),
                             anchor=anchor, stretch=flex)
        self._apply_display_columns()
        self.tree.pack(fill="x", expand=False, pady=(0, self._px(12)))
        self._apply_tree_tags()

        self.tree.bind("<Double-1>", self._on_dashboard_double_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_dashboard_select)
        self.tree.bind("<Button-3>", self._on_dashboard_right_click)
        # Column widths are recomputed after <Configure>/column drags, so resync once idle.
        self.tree.bind("<Configure>", self._on_tree_configure)
        self.tree.bind("<ButtonRelease-1>", lambda e: self.after_idle(self._on_columns_changed), add="+")
        self.tree.bind("<MouseWheel>", lambda e: self.after_idle(self._sync_error_overlays), add="+")
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self._hide_tree_tooltip())

        # Details (when a row is selected) sits to the left of the charts.
        lower = ttk.Frame(frame)
        lower.pack(fill="both", expand=True)
        self.charts_column = ttk.Frame(lower)
        self.charts_column.pack(side="left", fill="both", expand=True)
        self._build_details_panel(lower)
        self.details_frame.pack(side="left", fill="both", padx=(0, self._px(12)), before=self.charts_column)
        self._build_charts_panel(self.charts_column)
        self._clear_details()

    def _apply_display_columns(self):
        """Type (location/group) is obvious from the names, so it stays hidden to give the two
        inbox summaries room; when narrow, Last Updated goes too. Without CloudWatch tracking,
        the two log columns are dropped."""
        hidden = {"kind"}
        if self._tree_narrow:
            hidden.add("updated")
        if not self.cfg.settings.cloudwatch_enabled:
            hidden.update(LOG_COLUMNS)
        self.tree.configure(displaycolumns=[c for c in DASHBOARD_COLUMNS if c not in hidden])

    def _on_tree_configure(self, event):
        narrow = event.width < self._px(TREE_NARROW_WIDTH)
        if narrow != self._tree_narrow:
            self._tree_narrow = narrow
            self._apply_display_columns()
        self.after_idle(self._on_columns_changed)

    def _on_columns_changed(self):
        # Summaries are cut to fit their column, so a resize re-renders them at the new width.
        self._render_dashboard()

    def _cell_chars(self, column):
        """How many characters of a summary fit in the column at its current width."""
        if self._avg_char_px is None:
            self._avg_char_px = max(1.0, tkfont.nametofont("TkDefaultFont").measure("abcdefghij klmnopqrst") / 21)
        width = self.tree.column(column, "width") - self._px(16)
        return max(20, int(width / self._avg_char_px))

    def _apply_tree_tags(self):
        pal = self.palette
        # Plain surface for healthy rows; only rows that need attention get a soft tint.
        self.tree.tag_configure("ok", background=pal["surface"])
        self.tree.tag_configure("pending", background=pal["surface"], foreground=pal["muted"])
        self.tree.tag_configure("anomaly", background=pal["row_anomaly"])
        self.tree.tag_configure("error", background=pal["row_error"])
        self.tree.tag_configure("breach", background=pal["row_breach"])

    def _build_charts_panel(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill="x", pady=(0, self._px(4)))
        self.legend_var = tk.BooleanVar(value=self.cfg.settings.chart_legend_visible)
        legend_switch = ttk.Checkbutton(header, text="Legend", style="Switch.TCheckbutton",
                                        variable=self.legend_var, command=self._toggle_chart_legend)
        legend_switch.pack(side="right")
        ttk.Label(header, text="Charts: New Relic APM only (CloudWatch logs aren't charted)",
                  style="Muted.TLabel").pack(side="left")
        self._add_tooltip(legend_switch, "Hide the legend to give the charts more room. Hovering a chart "
                                         "still lists every service with its line style and values.")

        charts_frame = ttk.Frame(parent)
        charts_frame.pack(fill="both", expand=True)
        # The figure sits in a scrollable viewport: it fills the viewport when there's room, and
        # keeps a readable minimum height per chart row (scrolling) when there isn't.
        self.chart_viewport = tk.Canvas(charts_frame, highlightthickness=0, bd=0,
                                        yscrollincrement=self._px(40))
        self.chart_viewport.pack(fill="both", expand=True)
        # Placed over the right edge rather than packed beside it, so showing it doesn't change
        # the width the column count is chosen from (which could flip the layout back and forth).
        self.chart_scrollbar = ttk.Scrollbar(charts_frame, orient="vertical", command=self.chart_viewport.yview)
        self.chart_viewport.configure(yscrollcommand=self.chart_scrollbar.set)
        self.chart_viewport.bind("<Configure>", lambda e: self._sync_chart_viewport())

        self.chart_fig = Figure(figsize=(11, 4), dpi=CHART_DPI)
        self.chart_fig.set_facecolor(self.palette["chart_bg"])
        self.chart_axes = {}
        for i, (key, title, _select) in enumerate(OVERVIEW_METRICS):
            ax = self.chart_fig.add_subplot(math.ceil(len(OVERVIEW_METRICS) / CHART_COLUMNS), CHART_COLUMNS, i + 1)
            self._style_axis(ax, title)
            self.chart_axes[key] = ax
        self._axis_keys = {ax: key for key, ax in self.chart_axes.items()}

        self.chart_canvas = FigureCanvasTkAgg(self.chart_fig, master=self.chart_viewport)
        widget = self.chart_canvas.get_tk_widget()
        widget.configure(highlightthickness=0, bd=0)
        self._chart_window = self.chart_viewport.create_window(0, 0, window=widget, anchor="nw")
        widget.bind("<Configure>", self._on_chart_resize, add="+")
        widget.bind("<MouseWheel>", self._on_chart_wheel, add="+")
        self.chart_canvas.mpl_connect("pick_event", self._on_legend_pick)
        self.chart_canvas.mpl_connect("button_press_event", self._on_chart_click)
        self.chart_canvas.mpl_connect("motion_notify_event", self._on_chart_motion)
        self.chart_canvas.mpl_connect("axes_leave_event", lambda e: self._hide_crosshair())
        self.chart_canvas.mpl_connect("figure_leave_event", lambda e: self._hide_crosshair())

    def _style_axis(self, ax, title):
        pal = self.palette
        ax.set_facecolor(pal["chart_bg"])
        ax.set_title(title, fontsize=self.chart_font + 1, color=pal["fg"], loc="left", pad=8,
                     fontweight="semibold")
        ax.tick_params(labelsize=self.chart_font, colors=pal["muted"], length=0, pad=6)
        for side, spine in ax.spines.items():
            spine.set_visible(side == "bottom")
            spine.set_color(pal["chart_grid"])
        ax.grid(axis="y", color=pal["chart_grid"], linewidth=0.8)
        ax.set_axisbelow(True)

    def _build_changes_tab(self):
        """CloudTrail feed of deploys and other ECS changes on the configured clusters, newest
        first. Display only: none of these raise alerts."""
        frame = self.changes_tab
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, self._px(10)))
        self.changes_status = ttk.Label(top, text="Start monitoring to load CloudTrail changes.",
                                        style="Muted.TLabel")
        self.changes_status.pack(side="left")
        self._info_icon(top, CLOUDTRAIL_TIMING_NOTE).pack(side="left", padx=(self._px(8), 0))
        self._add_tooltip(self.changes_status, CLOUDTRAIL_TIMING_NOTE)

        # Packed right-to-left: Show Auto Scaling, then the PR filter, then the App filter.
        pr_box = ttk.Frame(top)
        app_box = ttk.Frame(top)
        self.app_filter_var = tk.StringVar()
        self.app_filter_var.trace_add("write", lambda *_: self._render_changes())
        self.pr_filter_var = tk.StringVar()
        self.pr_filter_var.trace_add("write", lambda *_: self._render_changes())
        self.show_scaling_var = tk.BooleanVar(value=self.cfg.settings.cloudtrail_show_autoscaling)
        scaling_switch = ttk.Checkbutton(top, text="Show Auto Scaling", style="Switch.TCheckbutton",
                                         variable=self.show_scaling_var, command=self._toggle_show_scaling)
        scaling_switch.pack(side="right")
        self._add_tooltip(scaling_switch, "Auto Scaling changes the number of running tasks hundreds of "
                                          "times a week, so those rows are hidden unless this is on. "
                                          "Deploys, creates, deletes and task stops are always shown.")
        pr_box.pack(side="right", padx=(0, self._px(20)))
        ttk.Label(pr_box, text="PR #").pack(side="left", padx=(0, self._px(6)))
        pr_entry = ttk.Entry(pr_box, textvariable=self.pr_filter_var, width=14)
        pr_entry.pack(side="left")
        ttk.Button(pr_box, text="Clear", command=lambda: self.pr_filter_var.set("")).pack(
            side="left", padx=(self._px(6), 0))
        self._add_tooltip(pr_entry, "Show only these PRs' environments: when each started, every deploy, "
                                    "and when it was torn down. Separate several with commas (3384, 3381). "
                                    "Type \"pr\" for all PR environments. PR numbers are per repo, so the "
                                    "App column says whether it's an SSR or Search API PR.")
        app_box.pack(side="right", padx=(0, self._px(20)))
        ttk.Label(app_box, text="App").pack(side="left", padx=(0, self._px(6)))
        app_combo = ttk.Combobox(app_box, textvariable=self.app_filter_var, width=16,
                                 postcommand=lambda: app_combo.configure(values=self._change_app_names()))
        app_combo.pack(side="left")
        ttk.Button(app_box, text="Clear", command=lambda: self.app_filter_var.set("")).pack(
            side="left", padx=(self._px(6), 0))
        self._add_tooltip(app_combo, "Show only one app's changes: pick it from the list, or type part of a "
                                     "name (\"ssr\" matches both SSR and SSR PRs, or part of a service name). "
                                     "Combines with the PR filter.")

        paned = ttk.PanedWindow(frame, orient="horizontal")
        paned.pack(fill="both", expand=True)
        list_frame = ttk.Frame(paned)
        columns = ("time", "target", "pr", "service", "change", "actor")
        self.changes_tree = ttk.Treeview(list_frame, columns=columns, show="headings")
        headings = {"time": "Time", "target": "App", "pr": "PR", "service": "Service", "change": "Change",
                    "actor": "By"}
        widths = {"time": 170, "target": 110, "pr": 70, "service": 210, "change": 270, "actor": 220}
        for col in columns:
            self.changes_tree.heading(col, text=headings[col], anchor="w")
            self.changes_tree.column(col, width=self._px(widths[col]), anchor="w", stretch=col == "change")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.changes_tree.yview)
        self.changes_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.changes_tree.pack(side="left", fill="both", expand=True)
        paned.add(list_frame, weight=3)

        detail_frame = ttk.LabelFrame(paned, text="Details", padding=self._px(12))
        self.pr_logs_btn = ttk.Button(detail_frame, text="View PR Container Logs (CloudWatch)",
                                      command=self._open_pr_logs, state="disabled")
        self.pr_logs_btn.pack(anchor="w", pady=(0, self._px(8)))
        self._add_tooltip(self.pr_logs_btn, "Opens the selected PR environment's container logs (SSR or "
                                            "Search API PRs on ECS), with a Follow switch to watch new lines "
                                            "as they arrive, like tail -f.")
        self.change_detail = tk.Text(detail_frame, width=40, wrap="word", state="disabled", borderwidth=0,
                                     highlightthickness=0, font="TkDefaultFont", padx=4, pady=4, spacing1=2)
        self.change_detail.pack(fill="both", expand=True)
        paned.add(detail_frame, weight=2)
        self.changes_tree.bind("<<TreeviewSelect>>", self._show_change_detail)
        self.changes_tree.bind("<Double-1>", self._filter_to_row_pr)

    def _filter_to_row_pr(self, event):
        """Double-click a PR row to see just that PR's history."""
        iid = self.changes_tree.identify_row(event.y)
        if iid and self._changes_shown[int(iid)].pr_number:
            self.pr_filter_var.set(self._changes_shown[int(iid)].pr_number)

    def _toggle_show_scaling(self):
        self.cfg.settings.cloudtrail_show_autoscaling = self.show_scaling_var.get()
        self.cfg.save()
        self._render_changes()

    def _pr_filter(self):
        """The PR numbers typed in the filter box; {"*"} for "pr" (every PR environment)."""
        text = self.pr_filter_var.get().strip().lower()
        if text in ("pr", "prs", "all prs"):
            return {"*"}
        return {part.strip().lstrip("#").removeprefix("pr").strip() for part in text.split(",")} - {""}

    def _change_app_names(self):
        """Apps for the App filter's list: the configured targets, plus any seen in the feed."""
        names = [t.label for t in self.cfg.settings.cloudtrail_targets]
        names += sorted({e.target for e in list(self.monitor.changes) if e.target} - set(names))
        return names

    def _app_filter_matches(self, event):
        """A name picked from the list matches exactly (so "SSR" leaves out "SSR PRs"); anything
        typed matches as a case-insensitive substring of the app or the service."""
        text = self.app_filter_var.get().strip()
        if not text:
            return True
        if text in self._change_app_names():
            return event.target == text
        text = text.lower()
        return text in event.target.lower() or text in (event.service or "").lower()

    def _visible_changes(self):
        show_scaling = self.cfg.settings.cloudtrail_show_autoscaling
        prs = self._pr_filter()
        return [e for e in list(self.monitor.changes)
                if (show_scaling or not e.is_autoscaling)
                and (not prs or (e.pr_number and ("*" in prs or e.pr_number in prs)))
                and self._app_filter_matches(e)]

    def _render_changes(self):
        monitor = self.monitor
        if monitor.changes_error:
            status = f"CloudTrail: {monitor.changes_error}"
        elif monitor.changes_checked_at is None:
            status = ("Loading CloudTrail changes…" if monitor.cloudtrail
                      else "Start monitoring to load CloudTrail changes.")
        else:
            changes = self._visible_changes()
            deploys = sum(1 for e in changes if e.kind == "deploy")
            prs = self._pr_filter()
            scope = ("" if not prs else " for all PRs" if "*" in prs
                     else f" for PR {', '.join('#' + n for n in sorted(prs))}")
            app = self.app_filter_var.get().strip()
            if app:
                scope += f" in {app}"
            status = (f"{len(changes)} change{'s' if len(changes) != 1 else ''} ({deploys} deploys){scope} in the "
                      f"last {self.cfg.settings.cloudtrail_lookback_hours} h · checked "
                      f"{format_12h_time(monitor.changes_checked_at)}")
        self.changes_status.config(text=status)

        changes = self._visible_changes()
        signature = (len(changes), changes[0].event_id if changes else None,
                     self.cfg.settings.cloudtrail_show_autoscaling, frozenset(self._pr_filter()),
                     self.app_filter_var.get().strip())
        if signature == self._changes_signature:
            return
        self._changes_signature = signature
        selected = self.changes_tree.selection()
        selected_id = self._changes_shown[int(selected[0])].event_id if selected else None
        self._changes_shown = changes
        self.changes_tree.delete(*self.changes_tree.get_children())
        pal = self.palette
        self.changes_tree.tag_configure("failed", foreground=pal["danger"])
        self.changes_tree.tag_configure("scale", foreground=pal["muted"])
        self.changes_tree.tag_configure("deploy", font="SunValleyBodyStrongFont")
        for i, e in enumerate(changes):
            self.changes_tree.insert("", "end", iid=str(i), tags=(e.kind,), values=(
                ErrorInboxDialog._fmt_time(e.timestamp), e.target, f"#{e.pr_number}" if e.pr_number else "",
                e.service or "-", e.summary, e.actor))
            if e.event_id == selected_id:
                self.changes_tree.selection_set(str(i))

    def _show_change_detail(self, event=None):
        sel = self.changes_tree.selection()
        if not sel:
            return
        e = self._changes_shown[int(sel[0])]
        self.pr_logs_btn.config(state="normal" if self._pr_logs_target(e) else "disabled")
        fields = (
            ("Time", ErrorInboxDialog._fmt_time(e.timestamp)),
            ("Change", f"{CHANGE_KIND_LABELS.get(e.kind, e.kind)}: {e.summary}"),
            ("Cluster / service", f"{e.cluster} / {e.service or '-'}"),
            ("PR", f"#{e.pr_number} ({e.target})" if e.pr_number else "- (not a PR environment)"),
            ("By", e.actor),
            ("Role", e.role or "-"),
            ("CloudTrail event", e.raw),
        )
        text = self.change_detail
        text.config(state="normal")
        text.delete("1.0", "end")
        text.tag_configure("field", font="SunValleyBodyStrongFont")
        text.tag_configure("value", spacing3=12)
        text.tag_configure("code", font="TkFixedFont")
        for label, value in fields:
            text.insert("end", f"{label}\n", "field")
            text.insert("end", f"{value}\n", "code" if label == "CloudTrail event" else "value")
        text.config(state="disabled")

    def _pr_logs_target(self, change):
        """The CloudTrail target a PR row came from (for its AWS profile), or None if the row
        isn't a PR environment's service."""
        if not (change.pr_number and change.service and change.cluster):
            return None
        return next((t for t in self.cfg.settings.cloudtrail_targets if t.label == change.target), None)

    def _open_pr_logs(self):
        sel = self.changes_tree.selection()
        if not sel:
            return
        change = self._changes_shown[int(sel[0])]
        target = self._pr_logs_target(change)
        if target:
            PrLogsDialog(self, change, target)

    def _chart_changes(self):
        """Deploy-type changes inside the charts' time window, as (x position, event)."""
        timeline = self._chart_timeline
        if len(timeline) < 2:
            return []
        markers = []
        for e in list(self.monitor.changes):
            if (not e.on_charts or e.kind not in CHART_MARKER_KINDS
                    or not timeline[0] <= e.timestamp <= timeline[-1]):
                continue
            i = max(j for j, t in enumerate(timeline) if t <= e.timestamp)
            nxt = timeline[i + 1] if i + 1 < len(timeline) else timeline[i] + 60
            markers.append((i + (e.timestamp - timeline[i]) / (nxt - timeline[i]), e))
        return markers

    def _build_locations_tab(self):
        frame = self.locations_tab
        pad = self._px(8)
        listbox_opts = dict(height=16, exportselection=False, activestyle="none", borderwidth=0,
                            highlightthickness=1, relief="flat", font="TkDefaultFont")
        left = ttk.LabelFrame(frame, text="Locations", padding=self._px(12))
        left.pack(side="left", fill="both", expand=True, padx=(0, pad))
        self.loc_listbox = tk.Listbox(left, **listbox_opts)
        self.loc_listbox.pack(fill="both", expand=True)
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(self._px(12), 0))
        ttk.Button(btns, text="Add", style="Accent.TButton", command=self._add_location).pack(side="left")
        ttk.Button(btns, text="Edit", command=self._edit_location).pack(side="left", padx=pad)
        ttk.Button(btns, text="Remove", command=self._remove_location).pack(side="left")

        right = ttk.LabelFrame(frame, text="Combined Groups", padding=self._px(12))
        right.pack(side="left", fill="both", expand=True, padx=(pad, 0))
        self.grp_listbox = tk.Listbox(right, **listbox_opts)
        self.grp_listbox.pack(fill="both", expand=True)
        btns2 = ttk.Frame(right)
        btns2.pack(fill="x", pady=(self._px(12), 0))
        ttk.Button(btns2, text="Add", style="Accent.TButton", command=self._add_group).pack(side="left")
        ttk.Button(btns2, text="Edit", command=self._edit_group).pack(side="left", padx=pad)
        ttk.Button(btns2, text="Remove", command=self._remove_group).pack(side="left")

        self._refresh_loc_lists()

    def _build_settings_tab(self):
        # Sub-tabs keep each group of settings narrow enough for the default window; one Save
        # button below them saves all three.
        save_row = ttk.Frame(self.settings_tab)
        save_row.pack(side="bottom", fill="x", pady=(self._px(12), 0))
        ttk.Button(save_row, text="Save Settings", style="Accent.TButton", command=self._save_settings).pack(side="left")
        ttk.Label(save_row, text="Saves the General, CloudWatch Logs and CloudTrail tabs together.",
                  style="Muted.TLabel").pack(side="left", padx=(self._px(12), 0))
        sections = ttk.Notebook(self.settings_tab)
        sections.pack(fill="both", expand=True)
        frame = ttk.Frame(sections, padding=self._px(12))
        sections.add(frame, text="General")
        s = self.cfg.settings
        pady = self._px(6)

        def row(r, label, var, width=10):
            ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w", pady=pady, padx=(0, self._px(16)))
            ttk.Entry(frame, textvariable=var, width=width).grid(row=r, column=1, sticky="w", pady=pady)

        self.poll_var = tk.StringVar(value=str(s.poll_interval_seconds))
        row(0, "Poll interval (seconds):", self.poll_var)

        self.mult_var = tk.StringVar(value=str(s.threshold_stdev_multiplier))
        row(1, "Auto threshold: std-dev multiplier:", self.mult_var)

        self.minsamp_var = tk.StringVar(value=str(s.min_samples_for_auto_threshold))
        row(2, "Auto threshold: min samples before it activates:", self.minsamp_var)

        self.fallback_var = tk.StringVar(value=str(s.flat_fallback_threshold_percent))
        row(3, "Auto threshold: fallback % before min samples:", self.fallback_var)

        self.repeat_var = tk.StringVar(value=str(s.alert_repeat_seconds))
        row(4, "Alert repeat interval (seconds):", self.repeat_var)

        self.minvol_var = tk.StringVar(value=str(s.min_transactions_for_alert))
        row(5, "Min transactions in window before alerting (0 = off):", self.minvol_var)

        self.sound_var = tk.BooleanVar(value=s.sound_enabled)
        ttk.Checkbutton(frame, text="Audible beep on anomaly", variable=self.sound_var).grid(
            row=6, column=1, sticky="w", pady=pady)

        self.tts_var = tk.BooleanVar(value=s.tts_enabled)
        ttk.Checkbutton(frame, text="Speak location name on anomaly", variable=self.tts_var).grid(
            row=7, column=1, sticky="w", pady=pady)

        self.errbox_var = tk.BooleanVar(value=s.error_inbox_enabled)
        ttk.Checkbutton(frame, text="Watch Errors Inbox per container (session log only)",
                         variable=self.errbox_var).grid(row=8, column=1, sticky="w", pady=pady)

        self.errbox_lookback_var = tk.StringVar(value=str(s.error_inbox_lookback_seconds))
        row(9, "Errors Inbox lookback window (seconds):", self.errbox_lookback_var)

        self.errbox_attr_var = tk.StringVar(value=s.error_inbox_container_attribute)
        row(10, "Errors Inbox container attribute:", self.errbox_attr_var, width=24)

        ttk.Label(frame, text=f"Region: {config.NEW_RELIC_REGION}  (set NEW_RELIC_REGION in .env, restart to apply)",
                  style="Muted.TLabel").grid(row=11, column=0, columnspan=2, sticky="w", pady=(self._px(16), 0))
        ttk.Label(frame, text=f"Account ID: {config.NEW_RELIC_ACCOUNT_ID or '(not set)'}  "
                               f"(set NEW_RELIC_ACCOUNT_ID in .env, restart to apply)",
                  style="Muted.TLabel").grid(row=12, column=0, columnspan=2, sticky="w", pady=(self._px(4), 0))

        cw = ttk.Frame(sections, padding=self._px(12))
        sections.add(cw, text="CloudWatch Logs")

        def cw_row(r, label, var, width=10):
            ttk.Label(cw, text=label).grid(row=r, column=0, sticky="w", pady=pady, padx=(0, self._px(16)))
            ttk.Entry(cw, textvariable=var, width=width).grid(row=r, column=1, sticky="w", pady=pady)

        self.cw_enabled_var = tk.BooleanVar(value=s.cloudwatch_enabled)
        ttk.Checkbutton(cw, text="Track CloudWatch error logs per realm (applies on next Start)",
                        variable=self.cw_enabled_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=pady)
        self.cw_profile_var = tk.StringVar(value=s.cloudwatch_aws_profile)
        cw_row(1, "AWS profile:", self.cw_profile_var, width=24)
        self.cw_region_var = tk.StringVar(value=s.cloudwatch_region)
        cw_row(2, "AWS region:", self.cw_region_var, width=24)
        self.cw_group_var = tk.StringVar(value=s.cloudwatch_log_group)
        cw_row(3, "Log group:", self.cw_group_var, width=32)
        self.cw_filter_var = tk.StringVar(value=s.cloudwatch_filter_pattern)
        cw_row(4, "Filter pattern:", self.cw_filter_var, width=48)
        self.cw_exclude_var = tk.StringVar(value="; ".join(s.cloudwatch_exclude_patterns))
        cw_row(5, "Ignore lines containing (separate with ;):", self.cw_exclude_var, width=48)
        self.cw_lookback_var = tk.StringVar(value=str(s.cloudwatch_lookback_seconds))
        cw_row(6, "Count window (seconds):", self.cw_lookback_var)
        self.cw_poll_var = tk.StringVar(value=str(s.cloudwatch_poll_interval_seconds))
        cw_row(7, "Poll interval (seconds):", self.cw_poll_var)
        self.cw_min_var = tk.StringVar(value=str(s.min_log_errors_for_alert))
        cw_row(8, "Min error lines in window before alerting:", self.cw_min_var)
        self.cw_fallback_var = tk.StringVar(value=str(s.cloudwatch_fallback_threshold))
        cw_row(9, "Fallback threshold (lines) before min samples:", self.cw_fallback_var)
        ttk.Label(cw, text="If logs show a login error, click AWS Login at the top\n"
                           "(runs aws sso login for the profile above).",
                  style="Muted.TLabel", justify="left").grid(row=10, column=0, columnspan=2, sticky="w",
                                                            pady=(self._px(12), 0))

        ct = ttk.Frame(sections, padding=self._px(12))
        sections.add(ct, text="CloudTrail")
        self.ct_enabled_var = tk.BooleanVar(value=s.cloudtrail_enabled)
        ct_header = ttk.Frame(ct)
        ct_header.grid(row=0, column=0, columnspan=2, sticky="w", pady=pady)
        ttk.Checkbutton(ct_header, text="Show ECS deploys/changes from CloudTrail (applies on next Start)",
                        variable=self.ct_enabled_var).pack(side="left")
        self._info_icon(ct_header, CLOUDTRAIL_TIMING_NOTE).pack(side="left", padx=(self._px(8), 0))
        self.ct_lookback_var = tk.StringVar(value=str(s.cloudtrail_lookback_hours))
        ttk.Label(ct, text="History to show (hours):").grid(row=1, column=0, sticky="w", pady=pady)
        ttk.Entry(ct, textvariable=self.ct_lookback_var, width=10).grid(row=1, column=1, sticky="w", pady=pady)
        self.ct_poll_var = tk.StringVar(value=str(s.cloudtrail_poll_interval_seconds))
        ttk.Label(ct, text="Poll interval (seconds):").grid(row=2, column=0, sticky="w", pady=pady)
        ttk.Entry(ct, textvariable=self.ct_poll_var, width=10).grid(row=2, column=1, sticky="w", pady=pady)
        ttk.Label(ct, text="Clusters to watch, one per line:\n"
                           "label | cluster | service prefix | profile | region | charts\n"
                           "End a cluster with * for per-PR clusters (e.g. mp-sapi-pr-env-*). "
                           "charts = yes/no: mark its deploys on the dashboard charts.",
                  style="Muted.TLabel", justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(pady, 0))
        self.ct_targets_text = tk.Text(ct, width=110, height=6, font="TkFixedFont", borderwidth=1, relief="solid",
                                       wrap="none")
        self.ct_targets_text.insert("1.0", "\n".join(
            f"{t.label} | {t.cluster} | {t.service_prefix} | {t.profile} | {t.region} | "
            f"{'yes' if t.show_on_charts else 'no'}" for t in s.cloudtrail_targets))
        self.ct_targets_text.grid(row=4, column=0, columnspan=2, sticky="w", pady=pady)


    def _build_logs_tab(self):
        frame = self.logs_tab
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, self._px(10)))
        ttk.Label(top, text=f"Log file: {self.session_logger.log_path}", style="Muted.TLabel").pack(side="left")
        ttk.Label(top, text=f"Readings CSV: {self.session_logger.csv_path.name}", style="Muted.TLabel").pack(
            side="left", padx=(self._px(16), 0))
        ttk.Button(top, text="Refresh", command=self._refresh_logs).pack(side="right")

        # No wrapping (one entry per line keeps it scannable), so long lines get a horizontal scrollbar.
        body = ttk.Frame(frame)
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.log_text = tk.Text(body, height=28, state="disabled", wrap="none", font="TkFixedFont",
                                borderwidth=0, highlightthickness=0, padx=self._px(10), pady=self._px(8))
        yscroll = ttk.Scrollbar(body, orient="vertical", command=self.log_text.yview)
        xscroll = ttk.Scrollbar(body, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self._refresh_logs()

    def _add_tooltip(self, widget, text):
        """Hover help for a widget, using the app's themed tooltip."""
        widget.bind("<Enter>", lambda e: self.tooltip.show_text(e.x_root, e.y_root, text), add="+")
        widget.bind("<Motion>", lambda e: self.tooltip.move(e.x_root, e.y_root), add="+")
        widget.bind("<Leave>", lambda e: self.tooltip.hide(), add="+")

    def _info_icon(self, parent, text):
        """A small ⓘ that shows the note on hover."""
        icon = ttk.Label(parent, text="ⓘ", style="Muted.TLabel", cursor="question_arrow")
        self._add_tooltip(icon, text)
        return icon

    # ---------- connection status ----------
    def _set_connection(self, state, message):
        """Status is conveyed by icon shape and an explicit text label, not color alone."""
        self.conn_state = state
        self._conn_message = message
        self.conn_icon.config(image=self.icon(f"conn_{state}"))
        self.status_title.config(text=f"Status: {CONN_TITLES[state]}")
        self.status_detail.config(text=truncate(message, 70))

    def _show_status_tooltip(self, event):
        if len(self._conn_message) > 70:
            self.tooltip.show_text(event.x_root, event.y_root, self._conn_message)

    # ---------- actions ----------
    def _test_connection(self):
        self._set_connection("testing", "Testing connection…")

        def worker():
            ok, message, needs_login = self._check_connections()
            self.event_queue.put(("conn_result", (ok, message, needs_login)))

        threading.Thread(target=worker, daemon=True).start()

    def _check_connections(self):
        """New Relic decides pass/fail; a CloudWatch problem (usually an expired AWS login) is
        reported alongside it but doesn't stop the New Relic monitoring from starting. The third
        value says whether AWS needs logging in to."""
        ok, message = self.client.test_connection()
        needs_login = False
        for aws_client in self.monitor.aws_clients():
            _aws_ok, aws_message = aws_client.test_connection()
            message = f"{message} · {aws_message}"
            needs_login = needs_login or aws_client.needs_login
        return ok, message, needs_login

    def _aws_login(self):
        """One-click `aws sso login`: the browser opens to approve it, then the connection is
        rechecked and CloudWatch reads the logs straight away instead of at its next poll."""
        s = self.cfg.settings
        profiles = ([s.cloudwatch_aws_profile] if s.cloudwatch_enabled else []) + \
            ([t.profile for t in s.cloudtrail_targets] if s.cloudtrail_enabled else [])
        self.aws_login_btn.config(state="disabled", text="Approve in browser…")

        def worker():
            # One login per SSO session; profiles that share one are covered together.
            results = [sso_login(profile) for profile in login_profiles(profiles or [s.cloudwatch_aws_profile])]
            failed = [message for ok, message in results if not ok]
            ok = not failed
            message = "; ".join(failed) if failed else "; ".join(message for _ok, message in results)
            self.event_queue.put(("aws_login_result", (ok, message)))

        threading.Thread(target=worker, daemon=True).start()

    def _toggle_monitoring(self):
        if self.monitor.is_running():
            self.monitor.stop()
            self.start_btn.config(text="Start Monitoring", style="Accent.TButton")
            self._stop_loading_feedback()
            return
        self._start_monitoring()

    def _start_monitoring(self):
        if not self.cfg.locations:
            messagebox.showwarning("No locations", "Add at least one location before starting monitoring.")
            return

        self.start_btn.config(state="disabled")
        self.refresh_btn.config(state="disabled")
        self._set_connection("testing", "Checking connection…")

        def worker():
            ok, message, needs_login = self._check_connections()
            self.event_queue.put(("start_conn_result", (ok, message, needs_login)))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_now(self):
        if not self.cfg.locations:
            messagebox.showwarning("No locations", "Add at least one location before refreshing.")
            return

        if self.monitor.is_running():
            self.monitor.refresh_now()
            return

        # Not running yet: refreshing implies starting monitoring so it keeps polling afterward.
        self._start_monitoring()

    def _seed_loading_rows(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        rows = [(f"loc:{loc.name}", loc.name, "location") for loc in self.cfg.locations if loc.enabled]
        rows += [(f"grp:{grp.name}", grp.name, "group") for grp in self.cfg.groups]
        for iid, name, kind in rows:
            self.tree.insert("", "end", iid=iid, text=" " + STATUS_LABELS["pending"], image=self.icon("pending"),
                             values=(name, kind) + ("-",) * (len(DASHBOARD_COLUMNS) - 2), tags=("pending",))
        self._fit_tree_height()

    def _start_loading_feedback(self):
        self._awaiting_first_update = True
        self._seed_loading_rows()
        self.loading_label.config(text="Loading first results...")
        self.loading_bar.pack(side="left", padx=(8, 0))
        self.loading_bar.start(12)

    def _stop_loading_feedback(self):
        self._awaiting_first_update = False
        self.loading_bar.stop()
        self.loading_bar.pack_forget()
        self.loading_label.config(text="")

    def _refresh_loc_lists(self):
        self.loc_listbox.delete(0, "end")
        for loc in self.cfg.locations:
            marker = "" if loc.enabled else " (disabled)"
            self.loc_listbox.insert("end", loc.name + marker)
        self.grp_listbox.delete(0, "end")
        for grp in self.cfg.groups:
            self.grp_listbox.insert("end", grp.name)

    def _add_location(self):
        dlg = LocationDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            self.cfg.locations.append(dlg.result)
            self.cfg.save()
            self._refresh_loc_lists()

    def _edit_location(self):
        sel = self.loc_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        dlg = LocationDialog(self, self.cfg.locations[idx])
        self.wait_window(dlg)
        if dlg.result:
            self.cfg.locations[idx] = dlg.result
            self.cfg.save()
            self._refresh_loc_lists()

    def _remove_location(self):
        sel = self.loc_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if messagebox.askyesno("Remove", f"Remove location '{self.cfg.locations[idx].name}'?"):
            del self.cfg.locations[idx]
            self.cfg.save()
            self._refresh_loc_lists()

    def _add_group(self):
        names = [l.name for l in self.cfg.locations]
        dlg = GroupDialog(self, names)
        self.wait_window(dlg)
        if dlg.result:
            self.cfg.groups.append(dlg.result)
            self.cfg.save()
            self._refresh_loc_lists()

    def _edit_group(self):
        sel = self.grp_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        names = [l.name for l in self.cfg.locations]
        dlg = GroupDialog(self, names, self.cfg.groups[idx])
        self.wait_window(dlg)
        if dlg.result:
            self.cfg.groups[idx] = dlg.result
            self.cfg.save()
            self._refresh_loc_lists()

    def _remove_group(self):
        sel = self.grp_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if messagebox.askyesno("Remove", f"Remove group '{self.cfg.groups[idx].name}'?"):
            del self.cfg.groups[idx]
            self.cfg.save()
            self._refresh_loc_lists()

    def _save_settings(self):
        s = self.cfg.settings
        try:
            s.poll_interval_seconds = max(5, int(self.poll_var.get()))
            s.threshold_stdev_multiplier = float(self.mult_var.get())
            s.min_samples_for_auto_threshold = max(1, int(self.minsamp_var.get()))
            s.flat_fallback_threshold_percent = float(self.fallback_var.get())
            s.alert_repeat_seconds = max(2, int(self.repeat_var.get()))
            s.min_transactions_for_alert = max(0, int(self.minvol_var.get()))
            s.error_inbox_lookback_seconds = max(5, int(self.errbox_lookback_var.get()))
            s.cloudwatch_lookback_seconds = max(60, int(self.cw_lookback_var.get()))
            s.cloudwatch_poll_interval_seconds = max(15, int(self.cw_poll_var.get()))
            s.min_log_errors_for_alert = max(0, int(self.cw_min_var.get()))
            s.cloudwatch_fallback_threshold = float(self.cw_fallback_var.get())
            s.cloudtrail_lookback_hours = max(1, min(24 * 90, int(self.ct_lookback_var.get())))  # CloudTrail keeps 90 days
            s.cloudtrail_poll_interval_seconds = max(30, int(self.ct_poll_var.get()))
        except ValueError:
            messagebox.showerror("Invalid input", "Please enter valid numbers.")
            return
        s.sound_enabled = self.sound_var.get()
        s.tts_enabled = self.tts_var.get()
        s.error_inbox_enabled = self.errbox_var.get()
        s.error_inbox_container_attribute = self.errbox_attr_var.get().strip() or "containerId"
        s.cloudwatch_enabled = self.cw_enabled_var.get()
        self._apply_display_columns()
        s.cloudwatch_aws_profile = self.cw_profile_var.get().strip()
        s.cloudwatch_region = self.cw_region_var.get().strip() or "us-east-1"
        s.cloudwatch_log_group = self.cw_group_var.get().strip()
        s.cloudwatch_filter_pattern = self.cw_filter_var.get().strip() or config.DEFAULT_CLOUDWATCH_FILTER
        s.cloudwatch_exclude_patterns = [x.strip() for x in self.cw_exclude_var.get().split(";") if x.strip()]
        s.cloudtrail_enabled = self.ct_enabled_var.get()
        targets = []
        for line in self.ct_targets_text.get("1.0", "end").splitlines():
            parts = [x.strip() for x in line.split("|")]
            if len(parts) < 2 or not parts[1]:
                continue
            parts += [""] * (6 - len(parts))
            targets.append(config.ChangeTarget(label=parts[0] or parts[1], cluster=parts[1], service_prefix=parts[2],
                                               profile=parts[3] or "xforce", region=parts[4] or "us-east-1",
                                               show_on_charts=parts[5].lower() not in ("no", "n", "false")))
        s.cloudtrail_targets = targets
        self.alert_manager.sound_enabled = s.sound_enabled
        self.alert_manager.tts_enabled = s.tts_enabled
        self.alert_manager.repeat_seconds = s.alert_repeat_seconds
        self.cfg.save()
        messagebox.showinfo("Saved", "Settings saved.")

    def _offer_aws_login(self, needs_login):
        if needs_login and str(self.aws_login_btn.cget("state")) != "disabled" and messagebox.askyesno(
                "AWS login expired",
                "Your AWS login has expired, so CloudWatch logs can't be read.\n\n"
                "Log in now? Your browser will open to approve it."):
            self._aws_login()

    def _refresh_logs(self):
        # see("end") would also scroll sideways to the end of the last (often very long) line,
        # leaving shorter lines looking blank. Instead keep the reader's horizontal position, and
        # only follow new entries if they were already at the bottom.
        at_bottom = self.log_text.yview()[1] >= 0.999
        top, left = self.log_text.yview()[0], self.log_text.xview()[0]
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(self.session_logger.tail()))
        self.log_text.config(state="disabled")
        # Once laid out: scroll positions set before Tk has measured the new text don't stick.
        self.after_idle(lambda: (self.log_text.yview_moveto(1.0 if at_bottom else top),
                                 self.log_text.xview_moveto(left)))

    # ---------- queue draining / rendering ----------
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "conn_result":
                    ok, message, needs_login = payload
                    self._set_connection("connected" if ok else "failed", message)
                    self._offer_aws_login(needs_login)
                elif kind == "aws_login_result":
                    ok, message = payload
                    self.aws_login_btn.config(state="normal", text="AWS Login")
                    self.session_logger.event("INFO" if ok else "ERROR", message)
                    if ok:
                        self.monitor.reset_aws_clients()
                        if self.monitor.is_running():
                            self.monitor.refresh_now()
                        self._test_connection()
                    else:
                        messagebox.showerror("AWS login", message)
                elif kind == "start_conn_result":
                    ok, message, needs_login = payload
                    self._set_connection("connected" if ok else "failed", message)
                    self.refresh_btn.config(state="normal")
                    if ok:
                        self.monitor.start()
                        self._offer_aws_login(needs_login)
                        self._clear_new_errors()
                        self.start_btn.config(text="Stop Monitoring", style="TButton", state="normal")
                        self._start_loading_feedback()
                    else:
                        self.start_btn.config(state="normal")
                        messagebox.showerror("Connection failed", f"Can't start monitoring:\n{message}")
                elif kind == "update":
                    self._render_changes()
                    self._render_dashboard()
                    self._refresh_logs()
                    self._refresh_charts()
                    if self._awaiting_first_update:
                        self._stop_loading_feedback()
                elif kind == "charts_data":
                    self._charts_fetching = False
                    self._render_charts(payload)
                elif kind == "charts_error":
                    self._charts_fetching = False
                    self.session_logger.event("ERROR", f"Charts: {payload}")
                elif kind == "anomaly":
                    key, name, value, threshold = payload
                    self.alert_manager.raise_alert(key, name, value, threshold)
                elif kind == "recover":
                    self.alert_manager.clear_alert(payload)
                elif kind == "alerts_changed":
                    self._render_alert_banner()
        except queue.Empty:
            pass
        self.after(POLL_UI_MS, self._drain_queue)

    def _series_index(self, app_name):
        if app_name not in self._chart_colors:
            self._chart_colors[app_name] = len(self._chart_colors)
        return self._chart_colors[app_name]

    def _color_for(self, app_name):
        colors = theme.series_colors(self.palette)
        return colors[self._series_index(app_name) % len(colors)]

    def _linestyle_for(self, app_name):
        # Past the palette's length colors repeat, so the repeats are told apart by dashing.
        return "-" if self._series_index(app_name) < len(theme.series_colors(self.palette)) else "--"

    def _refresh_charts(self):
        if self._charts_fetching:
            return
        app_names = [loc.app_name for loc in self.cfg.locations if loc.enabled]
        if not app_names:
            return
        self._charts_fetching = True

        def worker():
            try:
                data = self.client.get_overview_timeseries(
                    app_names, lookback_minutes=CHART_LOOKBACK_MINUTES, bucket_minutes=1)
                self.event_queue.put(("charts_data", data))
            except Exception as e:
                self.event_queue.put(("charts_error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _render_charts(self, metrics):
        self._last_metrics = metrics
        pal = self.palette
        enabled_names = [loc.app_name for loc in self.cfg.locations if loc.enabled]
        self._legend_names = enabled_names
        # One shared timeline across every series, so a series with missing buckets still lines
        # up by timestamp (and the crosshair reads every chart at the same moment).
        timeline = sorted({t for series in metrics.values() for points in series.values() for t, _v in points})
        x_of = {t: i for i, t in enumerate(timeline)}
        self._chart_timeline = timeline
        self._chart_values = {key: {app: dict(points) for app, points in metrics.get(key, {}).items()}
                              for key, _title, _select in OVERVIEW_METRICS}
        self._chart_lines = {}
        self._avg_annotations = {}  # ax.clear() below discards the old annotation artists too
        self._crosshairs = {}
        self._crosshair_idx = None
        markers = self._chart_changes()
        self.chart_fig.set_facecolor(pal["chart_bg"])
        for key, title, _select in OVERVIEW_METRICS:
            ax = self.chart_axes[key]
            ax.clear()
            self._style_axis(ax, title)
            series = metrics.get(key, {})
            for app_name in enabled_names:
                points = series.get(app_name)
                if not points:
                    continue
                line, = ax.plot([x_of[t] for t, _v in points], [v for _t, v in points],
                                color=self._color_for(app_name), linestyle=self._linestyle_for(app_name),
                                linewidth=1.6, solid_capstyle="round")
                self._chart_lines[(key, app_name)] = line
            if timeline:
                ax.set_xlim(0, max(len(timeline) - 1, 1))
            for x, _event in markers:  # deploys, so a spike can be lined up with what changed
                ax.axvline(x, color=pal["caution"], linewidth=1.2, linestyle=(0, (1, 2)), zorder=1)
            self._crosshairs[key] = ax.axvline(0, color=pal["muted"], linewidth=1, linestyle=(0, (4, 3)),
                                               visible=False, zorder=5)

        self._breaching_apps = self._compute_breaches(metrics, enabled_names)
        self._set_xticks()
        self._layout_charts()
        self._apply_highlight()
        self.chart_canvas.draw_idle()
        self._render_dashboard()

    # ---------- chart layout (responsive grid + legend) ----------
    def _on_chart_resize(self, event):
        if self._chart_relayout_job is not None:
            self.after_cancel(self._chart_relayout_job)
        self._chart_relayout_job = self.after(120, self._on_chart_resize_settled)

    def _on_chart_resize_settled(self):
        self._chart_relayout_job = None
        self._layout_charts()
        self._set_xticks()
        self._apply_highlight()  # the legend was rebuilt for the new width
        self.chart_canvas.draw_idle()

    def _sync_chart_viewport(self):
        """Sizes the figure to the viewport's width, and to its height unless that would squash the
        rows below the minimum, in which case the figure grows taller and the viewport scrolls."""
        viewport = self.chart_viewport
        width, height = viewport.winfo_width(), viewport.winfo_height()
        if width <= 1 or height <= 1:
            return
        rows = math.ceil(len(OVERVIEW_METRICS) / CHART_COLUMNS)
        fig_height = max(height, self._legend_px + rows * self._px(CHART_MIN_ROW_HEIGHT))
        viewport.itemconfigure(self._chart_window, width=width, height=fig_height)
        viewport.configure(scrollregion=(0, 0, width, fig_height))
        if fig_height > height:
            self.chart_scrollbar.place(relx=1, rely=0, relheight=1, anchor="ne")
            self.chart_scrollbar.lift()
        else:
            self.chart_scrollbar.place_forget()
            viewport.yview_moveto(0)

    def _on_chart_wheel(self, event):
        if self.chart_scrollbar.winfo_ismapped():
            self._hide_crosshair()  # the tooltip would otherwise describe the point scrolled away from
            self.chart_viewport.yview_scroll(int(-event.delta / 120), "units")

    def _toggle_chart_legend(self):
        self.cfg.settings.chart_legend_visible = self.legend_var.get()
        self.cfg.save()
        self._on_chart_resize_settled()

    def _layout_charts(self):
        """Legend re-wrapped to the width, with exactly its height reserved above the 2x2 charts."""
        widget = self.chart_canvas.get_tk_widget()
        width, height = widget.winfo_width(), widget.winfo_height()
        if width <= 1 or height <= 1:
            return
        legend_px = self._legend_px
        top = self._build_legend(width)
        if self._legend_px != legend_px:
            # The legend's height feeds into the figure's minimum height; resizing the figure
            # brings us back here through <Configure>.
            self.after_idle(self._sync_chart_viewport)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # tight_layout complains when the window is tiny
            self.chart_fig.tight_layout(rect=(0, 0, 1, top), h_pad=1.6, w_pad=2.2)

    def _build_legend(self, width):
        """(Re)creates the clickable legend, records its height and returns the figure fraction
        left for the charts."""
        for old_legend in list(self.chart_fig.legends):
            old_legend.remove()
        self._legend_app_by_artist = {}
        self._legend_texts = {}
        self._legend_handles = {}
        self._legend_px = 0
        names = self._legend_names
        if not names or not self.legend_var.get():
            return 0.98
        pal = self.palette
        font_px = self.chart_font * self.ui_scale * 96 / 72
        entry_width = max(len(n) for n in names) * font_px * 0.55 + self._px(56)
        ncol = max(1, min(len(names), int(width // entry_width)))
        handles = [Line2D([0], [0], color=self._color_for(n), linestyle=self._linestyle_for(n),
                          linewidth=2.4, label=n) for n in names]
        legend = self.chart_fig.legend(handles=handles, loc="upper center", ncol=ncol, fontsize=self.chart_font,
                                       frameon=False, bbox_to_anchor=(0.5, 1.0), labelcolor=pal["fg"],
                                       handlelength=1.8, columnspacing=1.8)
        for handle, text_artist, app_name in zip(legend.legend_handles, legend.get_texts(), names):
            handle.set_picker(8)
            text_artist.set_picker(True)
            self._legend_app_by_artist[handle] = app_name
            self._legend_app_by_artist[text_artist] = app_name
            self._legend_handles[app_name] = handle
            self._legend_texts[app_name] = text_artist
        renderer = self.chart_canvas.get_renderer()
        self._legend_px = int(math.ceil(legend.get_window_extent(renderer).height)) + self._px(8)
        return max(0.5, 1 - self._legend_px / self.chart_fig.bbox.height)

    def _set_xticks(self):
        timeline = self._chart_timeline
        if not timeline:
            return
        per_chart = self.chart_canvas.get_tk_widget().winfo_width() / CHART_COLUMNS
        count = max(2, min(6, int(per_chart // self._px(95))))
        n = len(timeline)
        step = max(1, math.ceil((n - 1) / (count - 1))) if n > 1 else 1
        idx = list(range(0, n, step))
        labels = [time.strftime("%I:%M %p", time.localtime(timeline[i])).lstrip("0") for i in idx]
        for ax in self.chart_axes.values():
            ax.set_xticks(idx)
            ax.set_xticklabels(labels)

    # ---------- highlight (legend click / row select) ----------
    def _set_highlight(self, app_name):
        self._highlighted_app = app_name
        self._apply_highlight()
        self.chart_canvas.draw_idle()

    def _apply_highlight(self):
        """Emphasizes the highlighted service and dims the rest, rather than hiding them,
        so its line can be read against the others."""
        highlighted = self._highlighted_app
        for (_key, app_name), line in self._chart_lines.items():
            focused = highlighted is None or app_name == highlighted
            line.set_alpha(1.0 if focused else DIM_ALPHA)
            line.set_linewidth(2.6 if app_name == highlighted else 1.6)
            line.set_zorder(4 if app_name == highlighted else 2)
        for app_name, handle in self._legend_handles.items():
            alpha = 1.0 if highlighted is None or app_name == highlighted else 0.35
            handle.set_alpha(alpha)
            self._legend_texts[app_name].set_alpha(alpha)
        self._update_average_annotations()

    @staticmethod
    def _format_avg(metric_key, avg):
        if metric_key == "response_time":
            return f"Avg: {avg:.2f}s"
        if metric_key == "throughput":
            return f"Avg: {avg:.1f} rpm"
        if metric_key == "errors":
            return f"Avg: {avg:.2f}%"
        return f"Avg: {avg:.2f}"

    def _update_average_annotations(self):
        for key, ax in self.chart_axes.items():
            old = self._avg_annotations.pop(key, None)
            if old:
                for artist in old:
                    try:
                        artist.remove()
                    except (ValueError, NotImplementedError):
                        pass
            if self._highlighted_app is None:
                continue
            line = self._chart_lines.get((key, self._highlighted_app))
            if line is None:
                continue
            ydata = line.get_ydata()
            if len(ydata) == 0:
                continue
            avg = sum(ydata) / len(ydata)
            color = line.get_color()
            refline = ax.axhline(avg, color=color, linestyle="--", linewidth=1.0, alpha=0.7)
            label = ax.text(
                0.98, 0.95, self._format_avg(key, avg), transform=ax.transAxes,
                ha="right", va="top", fontsize=self.chart_font, color=color,
                bbox=dict(boxstyle="round,pad=0.3", fc=self.palette["chart_bg"], ec=color, alpha=0.9),
            )
            self._avg_annotations[key] = (refline, label)

    def _on_legend_pick(self, event):
        app_name = self._legend_app_by_artist.get(event.artist)
        if app_name is None:
            return
        # Clicking the highlighted service again clears the highlight.
        self._set_highlight(None if app_name == self._highlighted_app else app_name)

    def _on_chart_click(self, event):
        if event.button == 3:  # right click anywhere on the chart area resets
            self._set_highlight(None)

    # ---------- crosshair (shared across all four charts) ----------
    def _on_chart_motion(self, event):
        timeline = self._chart_timeline
        if event.inaxes not in self._axis_keys or event.xdata is None or not timeline:
            self._hide_crosshair()
            return
        idx = min(max(int(round(event.xdata)), 0), len(timeline) - 1)
        x_root, y_root = self.winfo_pointerxy()
        if idx == self._crosshair_idx:
            self.tooltip.move(x_root, y_root)
            return
        self._crosshair_idx = idx
        for line in self._crosshairs.values():
            line.set_xdata([idx, idx])
            line.set_visible(True)
        self.chart_canvas.draw_idle()
        self.tooltip.show_custom(x_root, y_root, lambda body, pal: self._build_crosshair_tooltip(body, pal, idx))

    def _hide_crosshair(self):
        if self._crosshair_idx is None:
            return
        self._crosshair_idx = None
        for line in self._crosshairs.values():
            line.set_visible(False)
        self.tooltip.hide()
        self.chart_canvas.draw_idle()

    def _build_crosshair_tooltip(self, body, pal, idx):
        """Every service x every metric at the hovered timestamp; breaching values are flagged
        with a marker and color, the highlighted service is bold and the dimmed ones muted."""
        t = self._chart_timeline[idx]
        bg = pal["tooltip_bg"]
        gap = self._px(14)

        def cell(text, row, col, fg=pal["fg"], font="TkDefaultFont", sticky="e"):
            tk.Label(body, text=text, bg=bg, fg=fg, font=font).grid(
                row=row, column=col, sticky=sticky, padx=(gap if col > 1 else 0, 0), pady=1)

        cell(format_12h_time(t), 0, 0, font="SunValleyBodyStrongFont", sticky="w")
        for col, (key, _title, _select) in enumerate(OVERVIEW_METRICS, start=2):
            cell(METRIC_SHORT_LABELS[key], 1, col, fg=pal["muted"])
        for row, app_name in enumerate(self._legend_names, start=2):
            emphasized = app_name == self._highlighted_app
            dimmed = self._highlighted_app is not None and not emphasized
            fg = pal["muted"] if dimmed else pal["fg"]
            font = "SunValleyBodyStrongFont" if emphasized else "TkDefaultFont"
            swatch = tk.Canvas(body, width=self._px(22), height=self._px(10), bg=bg, highlightthickness=0)
            swatch.create_line(1, self._px(5), self._px(21), self._px(5), fill=self._color_for(app_name),
                               width=self._px(3), dash=() if self._linestyle_for(app_name) == "-" else (4, 2))
            swatch.grid(row=row, column=0, sticky="w", padx=(0, self._px(8)))
            cell(app_name, row, 1, fg=fg, font=font, sticky="w")
            for col, (key, _title, _select) in enumerate(OVERVIEW_METRICS, start=2):
                value = self._chart_values.get(key, {}).get(app_name, {}).get(t)
                if value is None:
                    cell("–", row, col, fg=pal["muted"])
                elif self._is_breach(key, app_name, value):
                    cell("▲ " + METRIC_VALUE_FORMATS[key].format(value), row, col, fg=pal["danger"],
                         font="SunValleyBodyStrongFont")
                else:
                    cell(METRIC_VALUE_FORMATS[key].format(value), row, col, fg=fg, font=font)

        # Deploys within a minute of the hovered point, from the CloudTrail feed.
        nearby = [e for x, e in self._chart_changes() if abs(x - idx) <= 1]
        for offset, e in enumerate(nearby[:6]):
            row = len(self._legend_names) + 2 + offset
            tk.Label(body, text=f"◆ {format_12h_time(e.timestamp)}  {e.target} {e.service}: {e.summary} "
                                f"({e.actor})", bg=bg, fg=pal["caution"], font="TkDefaultFont").grid(
                row=row, column=0, columnspan=len(OVERVIEW_METRICS) + 2, sticky="w",
                pady=(self._px(8) if offset == 0 else 1, 1))

    # ---------- thresholds ----------
    def _threshold_for(self, metric_key, app_name):
        if metric_key == "errors":
            loc = next((l for l in self.cfg.locations if l.app_name == app_name), None)
            if not loc:
                return None
            state = self.monitor.states.get(f"loc:{loc.name}")
            return state.threshold if state and state.threshold is not None else None
        return CHART_THRESHOLDS[metric_key]["default"]

    def _is_breach(self, metric_key, app_name, value):
        threshold = self._threshold_for(metric_key, app_name)
        if threshold is None:
            return False
        direction = "above" if metric_key == "errors" else CHART_THRESHOLDS[metric_key]["direction"]
        return value > threshold if direction == "above" else value < threshold

    def _compute_breaches(self, metrics, enabled_names):
        breaches = {}
        for key, _title, _select in OVERVIEW_METRICS:
            series = metrics.get(key, {})
            for app_name in enabled_names:
                points = series.get(app_name)
                if points and self._is_breach(key, app_name, points[-1][1]):
                    breaches.setdefault(app_name, set()).add(key)
        return breaches

    def _animate_legend(self):
        # Always sweep every legend text (not just when something is breaching) so a service
        # that just recovered gets reset back from mid-pulse instead of freezing red/enlarged.
        base_size, base_color = self.chart_font, self.palette["fg"]
        phase = (time.time() % LEGEND_PULSE_PERIOD_S) / LEGEND_PULSE_PERIOD_S
        size = base_size + LEGEND_PULSE_AMPLITUDE * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
        changed = False
        for app_name, text_artist in self._legend_texts.items():
            if self._breaching_apps.get(app_name):
                text_artist.set_fontsize(size)
                text_artist.set_color(self.palette["danger"])
                text_artist.set_fontweight("bold")
                changed = True
            elif text_artist.get_fontsize() != base_size or not same_color(text_artist.get_color(), base_color):
                text_artist.set_fontsize(base_size)
                text_artist.set_color(base_color)
                text_artist.set_fontweight("normal")
                changed = True
        if changed:
            self.chart_canvas.draw_idle()
        self.after(LEGEND_PULSE_INTERVAL_MS, self._animate_legend)

    # ---------- dashboard grid ----------
    @staticmethod
    def _row_for(key):
        """The dashboard row a series is shown on: a logs series shares its location's row."""
        return "loc:" + key[len("cw:"):] if key.startswith("cw:") else key

    @staticmethod
    def _cell_for(key):
        """The inbox column a series' records are summarised in."""
        return "logerrs" if key.startswith("cw:") else "errors"

    def _row_states(self, row):
        """(New Relic state, CloudWatch logs state) for a dashboard row; either may be None."""
        states = self.monitor.states
        logs = states.get(logs_key(row[len("loc:"):])) if row.startswith("loc:") else None
        return states.get(row), logs

    def _inbox_summary(self, state, column):
        if state is None or not state.records:
            return "-"
        count = len(state.records)
        if state.kind == "logs":
            latest = state.log_records[0]
            text = f"{count} line{'s' if count != 1 else ''} · {latest.level}: {latest.message}"
        else:
            latest = state.error_records[0]
            text = f"{count} error{'s' if count != 1 else ''} · {latest.error_class}: {latest.error_message}"
        # Cut to the column's width; the full text is on hover and in the details panel.
        return truncate(" ".join(text.split()), self._cell_chars(column))

    @staticmethod
    def _logs_cell(logs):
        if logs is None:
            return "-"
        if logs.status == "ERROR":
            return "⚠ error"
        if logs.value is None:
            return "…"
        mark = "▲ " if logs.status == "ANOMALY" else ""
        return f"{mark}{logs.value:.0f} / {logs.threshold:.0f}"

    def _track_new_records(self, state):
        """Flags a series whose newest record changed since the last render. The first poll of a
        session just establishes the baseline (whatever's already in the lookback window); only a
        different newest id after that counts as "new". Comparing ids rather than counts keeps
        working once a record list hits its cap and stops growing."""
        newest = state.newest_record_id
        if newest and newest != self._prev_newest_error_guid.get(state.key) and not self._awaiting_first_update:
            self._new_error_iids.add(state.key)
            self._new_error_at[state.key] = time.monotonic()
        self._prev_newest_error_guid[state.key] = newest

    def _render_dashboard(self):
        existing = set(self.tree.get_children())
        seen = set()
        for state in list(self.monitor.states.values()):
            if state.kind == "logs":
                continue  # shown in its location's row
            row = state.key
            seen.add(row)
            nr, logs = self._row_states(row)
            breached = self._breached_metrics(nr)
            # The badge is the worse of the two sources; a CloudWatch read failure (e.g. an expired
            # AWS login) only marks the Log Errors cell, so it can't paint every row as broken.
            if nr.status == "ANOMALY" or (logs is not None and logs.status == "ANOMALY"):
                badge = "anomaly"
            elif nr.status == "ERROR":
                badge = "error"
            elif nr.status == "OK" and breached:
                badge = "breach"
            else:
                badge = {"OK": "ok"}.get(nr.status, "pending")
            tag = "breach" if breached and badge != "anomaly" else badge
            for series in (nr, logs):
                if series is not None:
                    self._track_new_records(series)
            updated_at = max((x.last_updated for x in (nr, logs) if x is not None and x.last_updated), default=None)
            values = (
                nr.name, nr.kind,
                format_reading(nr.kind, nr.value), format_reading(nr.kind, nr.threshold),
                self._logs_cell(logs),
                format_12h_time(updated_at) if updated_at else "-",
                self._inbox_summary(nr, "errors"), self._inbox_summary(logs, "logerrs"),
            )
            item = dict(text=" " + STATUS_LABELS[badge], image=self.icon(badge), values=values, tags=(tag,))
            if row in existing:
                self.tree.item(row, **item)
            else:
                self.tree.insert("", "end", iid=row, **item)
        for row in existing - seen:
            self.tree.delete(row)
            for key in (row, logs_key(row[len("loc:"):]) if row.startswith("loc:") else None):
                if key:
                    self._new_error_iids.discard(key)
                    self._prev_newest_error_guid.pop(key, None)
                    self._remove_error_overlay(key)
        self._fit_tree_height()
        self._sync_error_overlays()
        self._render_details()

    def _breached_metrics(self, state):
        if state is None or state.kind != "location":
            return set()
        app_name = next((l.app_name for l in self.cfg.locations if l.name == state.name), None)
        return self._breaching_apps.get(app_name) or set()

    def _fit_tree_height(self):
        rows = len(self.tree.get_children())
        self.tree.configure(height=max(1, min(rows, TREE_MAX_ROWS)))

    # ---------- grid hover details ----------
    def _on_tree_motion(self, event):
        iid = self.tree.identify_row(event.y)
        region = self.tree.identify_region(event.x, event.y)
        column = self._column_id(self.tree.identify_column(event.x))
        if region == "heading":
            key = ("heading", column) if column in HEADING_TOOLTIPS else None
        else:
            key = (iid, column) if iid and region in ("cell", "tree") else None
        self._update_tree_tooltip(key, event.x_root, event.y_root)

    def _column_id(self, column):
        # identify_column() numbers *displayed* columns ("#3"), which shift when some are hidden.
        return column if column == "#0" else self.tree.column(column, "id")

    def _update_tree_tooltip(self, key, x_root, y_root):
        if key == self._tree_hover_key:
            if key is not None:
                self.tooltip.move(x_root, y_root)
            return
        self._tree_hover_key = key
        text = self._tree_tooltip_text(*key) if key else None
        if text:
            self.tooltip.show_text(x_root, y_root, text)
        else:
            self.tooltip.hide()

    def _hide_tree_tooltip(self):
        self._tree_hover_key = None
        self.tooltip.hide()

    def _tree_tooltip_text(self, row, column):
        if row == "heading":
            return HEADING_TOOLTIPS[column]
        nr, logs = self._row_states(row)
        if nr is None:
            return None
        if column == "#0":
            parts = ["New Relic: " + (self._status_explanation(nr) or "-")]
            if logs is not None:
                parts.append("CloudWatch logs: " + (self._status_explanation(logs) or "-"))
                parts.append(STATUS_SOURCES_NOTE)
            return "\n\n".join(parts)
        if column in ("value", "threshold"):
            return "New Relic: " + (self._status_explanation(nr) or "-") + "\n\n" + NR_ONLY_NOTE
        if column == "logs" and logs is not None:
            return "CloudWatch logs: " + (self._status_explanation(logs) or "-") + "\n\n" + CW_ONLY_NOTE
        state = logs if column == "logerrs" else nr if column == "errors" else None
        if state is None or not state.records:
            return None
        hint = "\n\nClick the row for the full details below the table; double-click this cell for the inbox."
        return self._latest_record_text(state) + hint

    def _latest_record_text(self, state):
        """The newest record in full -- nothing truncated -- for tooltips and the details panel."""
        count = len(state.records)
        if state.kind == "logs":
            rec = state.log_records[0]
            return (f"CloudWatch: {count} error log line{'s' if count != 1 else ''} this session. Latest:\n\n"
                    f"{rec.level}: {rec.message}\n\n"
                    f"Request: {(rec.method + ' ' + rec.url).strip() or '-'}\n"
                    f"ECS task: {rec.task_id or '-'}\n"
                    f"Time: {ErrorInboxDialog._fmt_time(rec.timestamp)}")
        rec = state.error_records[0]
        status = rec.status_code if rec.status_code is not None else "-"
        return (f"New Relic: {count} error{'s' if count != 1 else ''} this session. Latest:\n\n"
                f"{rec.error_class}\n{rec.error_message}\n\n"
                f"Endpoint: {(rec.method or '') + ' ' + rec.endpoint if rec.method else rec.endpoint}\n"
                f"HTTP status: {status}\nContainer: {rec.container}\n"
                f"Time: {ErrorInboxDialog._fmt_time(rec.timestamp)}")

    def _status_explanation(self, state):
        breached = self._breached_metrics(state)
        titles = {key: title for key, title, _select in OVERVIEW_METRICS}
        lines = []
        window_min = self.cfg.settings.cloudwatch_lookback_seconds / 60
        if state.status == "ERROR":
            lines.append(f"Query error:\n{state.error or 'unknown error'}")
        elif state.status == "ANOMALY" and state.value is not None and state.threshold is not None:
            if state.kind == "logs":
                lines.append(f"Anomaly: {state.value:.0f} error log lines in the last {window_min:g} min, "
                             f"above the threshold of {state.threshold:.1f}.")
            else:
                lines.append(f"Anomaly: error rate {state.value:.2f}% is above the {state.threshold:.2f}% threshold.")
        elif state.status == "PENDING":
            lines.append("Waiting for the first results…")
        elif state.status == "OK":
            if state.kind == "logs":
                lines.append(f"{state.value:.0f} error log lines in the last {window_min:g} min, within the "
                             f"threshold of {state.threshold:.1f}." if state.value is not None else
                             "Error log lines are within their threshold.")
            else:
                lines.append("Error rate is within its threshold.")
        if breached:
            lines.append("Chart threshold breached: " + ", ".join(titles[k] for k in sorted(breached)))
        return "\n\n".join(lines) or None

    # ---------- details panel (click a row) ----------
    def _build_details_panel(self, parent):
        """Full, untruncated latest New Relic error and CloudWatch log line for the selected row,
        as selectable text, so it can be read and copied without leaving the dashboard."""
        self.details_frame = ttk.LabelFrame(parent, text="Details", padding=self._px(10))
        bar = ttk.Frame(self.details_frame)
        bar.pack(fill="x", pady=(0, self._px(6)))
        self.details_title = ttk.Label(bar, style="Strong.TLabel")
        self.details_title.pack(side="left")
        ttk.Button(bar, text="Clear", command=self._clear_details).pack(side="right")
        self.details_log_btn = ttk.Button(bar, text="Open Log Inbox (CloudWatch)",
                                          command=lambda: self.open_error_inbox(logs_key(self._details_row[4:])))
        self.details_log_btn.pack(side="right", padx=(0, self._px(6)))
        self.details_err_btn = ttk.Button(bar, text="Open Error Inbox (New Relic)",
                                          command=lambda: self.open_error_inbox(self._details_row))
        self.details_err_btn.pack(side="right", padx=(0, self._px(6)))
        body = ttk.Frame(self.details_frame)
        body.pack(fill="both", expand=True)
        self.details_text = tk.Text(body, width=60, height=9, wrap="word", borderwidth=0, highlightthickness=0,
                                    font="TkDefaultFont", padx=4, pady=4, state="disabled")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.details_text.yview)
        self.details_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.details_text.pack(side="left", fill="both", expand=True)
        self.bind("<Escape>", lambda e: self._clear_details(), add="+")

    def _show_details(self, row):
        self._details_row = row
        self._details_text_cache = None
        self._render_details()

    def _clear_details(self):
        """The panel always stays visible; with no row selected it shows a prompt instead."""
        self._details_row = None
        self._details_text_cache = None
        if self.tree.selection():
            self.tree.selection_remove(self.tree.selection())
        self.details_title.config(text="No row selected")
        self.details_err_btn.config(state="disabled")
        self.details_log_btn.config(state="disabled")
        self._write_details([("Details", "Select a row in the table to see its latest New Relic error "
                                         "and CloudWatch log line here.")])

    def _render_details(self):
        row = self._details_row
        if row is None:
            return
        nr, logs = self._row_states(row)
        if nr is None:
            self._clear_details()
            return
        self.details_title.config(text=nr.name)
        self.details_err_btn.config(state="normal" if nr.error_records else "disabled")
        if logs is not None:
            self.details_log_btn.pack(side="right", padx=(0, self._px(6)), before=self.details_err_btn)
            self.details_log_btn.config(state="normal" if logs.log_records else "disabled")
        else:
            self.details_log_btn.pack_forget()

        sections = []
        if nr.error_records:
            sections.append(("New Relic — latest error", self._latest_record_text(nr)))
        elif nr.kind == "location":
            sections.append(("New Relic", "No errors captured this session."))
        else:
            sections.append(("Group", self._status_explanation(nr) or "-"))
        if logs is not None:
            if logs.log_records:
                rec = logs.log_records[0]
                sections.append(("CloudWatch — latest error log line", self._latest_record_text(logs)))
                sections.append(("Full log line", rec.raw))
            else:
                sections.append(("CloudWatch", "No error log lines captured this session."
                                 if logs.status != "ERROR" else f"Can't read logs: {logs.error}"))
        # Only rewrite when something changed, so a selection being copied isn't wiped every poll.
        if sections == self._details_text_cache:
            return
        self._details_text_cache = sections
        self._write_details(sections)

    def _write_details(self, sections):
        pal = self.palette
        text = self.details_text
        text.config(state="normal", bg=pal["surface"], fg=pal["fg"], selectbackground=pal["select"],
                    selectforeground=pal["fg"])
        text.delete("1.0", "end")
        text.tag_configure("heading", font="SunValleyBodyStrongFont", spacing1=6)
        text.tag_configure("raw", font="TkFixedFont")
        for heading, body in sections:
            text.insert("end", f"{heading}\n", "heading")
            text.insert("end", f"{body}\n\n", "raw" if heading == "Full log line" else ())
        text.config(state="disabled")  # read-only, but still selectable and copyable

    # ---------- widgets layered over the grid ----------
    def _remove_error_overlay(self, key):
        overlay = self._error_cell_overlays.pop(key, None)
        if overlay is not None:
            overlay.destroy()

    def _sync_row_separators(self):
        """Treeview can't draw row borders, so a 1px line is laid over the bottom of each row."""
        children = set(self.tree.get_children())
        for iid in list(self._row_separators):
            if iid not in children:
                self._row_separators.pop(iid).destroy()
        for iid in children:
            sep = self._row_separators.get(iid)
            if sep is None:
                sep = tk.Frame(self.tree, height=1, bd=0, bg=self.palette["border"])
                self._row_separators[iid] = sep
            bbox = self.tree.bbox(iid)
            if not bbox:
                sep.place_forget()
                continue
            x, y, width, height = bbox
            sep.place(x=x, y=y + height - 1, width=width, height=1)

    def _over_threshold(self, key):
        state = self.monitor.states.get(key)
        return state is not None and state.status == "ANOMALY"

    def _blend(self, start, end, t):
        a, b = self.winfo_rgb(start), self.winfo_rgb(end)
        return "#" + "".join(f"{round(x + (y - x) * t) >> 8:02x}" for x, y in zip(a, b))

    def _sync_error_overlays(self):
        """A series with new records since it was last opened gets its inbox cell (New Relic or
        CloudWatch) highlighted: red, until opened, while the series is over its threshold;
        otherwise a green blip that holds for NEW_ERROR_HOLD_S then fades back into the row."""
        self._sync_row_separators()
        pal, now = self.palette, time.monotonic()
        next_tick_s = None
        for key in list(self._new_error_iids):
            if self._over_threshold(key):
                self._new_error_at[key] = now  # once back under, the green blip runs from here
                continue
            elapsed = now - self._new_error_at.get(key, now)
            if elapsed >= NEW_ERROR_HOLD_S + NEW_ERROR_FADE_S:
                self._new_error_iids.discard(key)
                self._new_error_at.pop(key, None)
            else:
                wait = NEW_ERROR_FADE_FRAME_MS / 1000 if elapsed >= NEW_ERROR_HOLD_S else NEW_ERROR_HOLD_S - elapsed
                next_tick_s = wait if next_tick_s is None else min(next_tick_s, wait)
        for key in list(self._error_cell_overlays.keys()):
            if key not in self._new_error_iids or not self.tree.exists(self._row_for(key)):
                self._remove_error_overlay(key)
        for key in self._new_error_iids:
            row, column = self._row_for(key), self._cell_for(key)
            if not self.tree.exists(row):
                continue
            bbox = self.tree.bbox(row, column)
            overlay = self._error_cell_overlays.get(key)
            if not bbox:
                if overlay is not None:
                    overlay.place_forget()
                continue
            if self._over_threshold(key):
                bg, fg = pal["new_error_alert_bg"], pal["new_error_fg"]
            else:
                bg, fg = pal["new_error_bg"], pal["new_error_fg"]
                fade = (now - self._new_error_at.get(key, now) - NEW_ERROR_HOLD_S) / NEW_ERROR_FADE_S
                if fade > 0:
                    tags = self.tree.item(row, "tags")
                    row_bg = (tags and self.tree.tag_configure(tags[0], "background")) or pal["surface"]
                    bg, fg = self._blend(bg, row_bg, min(fade, 1)), self._blend(fg, pal["fg"], min(fade, 1))
            text = self.tree.set(row, column)
            if overlay is None:
                overlay = tk.Label(
                    self.tree, text=text, bg=bg, fg=fg,
                    anchor="w", font="TkDefaultFont", padx=self._px(6),
                )
                overlay.bind("<Double-1>", lambda e, k=key: self._on_error_cell_double_click(k))
                overlay.bind("<Button-1>", lambda e, k=key: self._on_error_cell_click(k))
                overlay.bind("<Button-3>", lambda e: self._on_dashboard_right_click())
                overlay.bind("<Motion>", lambda e, r=row, c=column: self._update_tree_tooltip(
                    (r, c), e.x_root, e.y_root))
                overlay.bind("<Leave>", lambda e: self._hide_tree_tooltip())
                self._error_cell_overlays[key] = overlay
            else:
                overlay.config(text=text, bg=bg, fg=fg)
            x, y, width, height = bbox
            overlay.place(x=x, y=y, width=width, height=height - 1)  # leave the row separator visible
        if self._overlay_tick_id is not None:
            self.after_cancel(self._overlay_tick_id)
            self._overlay_tick_id = None
        if next_tick_s is not None:
            self._overlay_tick_id = self.after(max(1, int(next_tick_s * 1000)), self._on_overlay_tick)

    def _on_overlay_tick(self):
        self._overlay_tick_id = None
        self._sync_error_overlays()

    def _clear_new_errors(self):
        """Clears every series' new-records flag (the green highlights) without opening any inbox."""
        self._new_error_iids.clear()
        self._new_error_at.clear()
        for key in list(self._error_cell_overlays.keys()):
            self._remove_error_overlay(key)
        self._hide_tree_tooltip()
        self.mini_view.refresh()

    def _on_error_cell_click(self, key):
        self.tree.selection_set(self._row_for(key))
        self._on_dashboard_select()

    def _on_error_cell_double_click(self, key):
        self.open_error_inbox(key)

    def _on_dashboard_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        column = self._column_id(self.tree.identify_column(event.x))
        nr, logs = self._row_states(row)
        if column in LOG_COLUMNS or (column != "errors" and nr is not None and not nr.records):
            if logs is not None:
                self.open_error_inbox(logs.key)
                return
        self.open_error_inbox(row)

    def open_error_inbox(self, key):
        """Clears the series' new-records flag and opens its inbox (Error Inbox for New Relic,
        Log Inbox for CloudWatch) if it has any records."""
        self._new_error_iids.discard(key)
        self._remove_error_overlay(key)
        self._hide_tree_tooltip()
        state = self.monitor.states.get(key)
        if not state or not state.records:
            return
        dialog = (LogInboxDialog if state.kind == "logs" else ErrorInboxDialog)(self, state)
        dialog.lift()
        dialog.focus_force()

    def _on_dashboard_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        self._show_details(sel[0])
        if not sel[0].startswith("loc:"):
            return  # groups have no chart line to highlight
        name = sel[0][len("loc:"):]
        loc = next((l for l in self.cfg.locations if l.name == name), None)
        if not loc:
            return
        self._set_highlight(loc.app_name)

    def _on_dashboard_right_click(self, event=None):
        self._set_highlight(None)

    def _render_alert_banner(self):
        active = [a for a in self.alert_manager.active_alerts() if not a.acknowledged]
        if not active:
            self.alert_banner.pack_forget()
            for w in self._alert_widgets.values():
                w.destroy()
            self._alert_widgets.clear()
            return

        self.alert_banner.pack(fill="x", before=self.notebook)
        active_keys = {a.key for a in active}
        for key in list(self._alert_widgets.keys()):
            if key not in active_keys:
                self._alert_widgets[key].destroy()
                del self._alert_widgets[key]
        for alert in active:
            if alert.key.startswith("cw:"):
                text = (f"LOG ERROR SPIKE: {alert.name} — {alert.value:.0f} error lines "
                        f"(threshold {alert.threshold:.1f})")
            else:
                text = f"ANOMALY: {alert.name} — {alert.value:.2f}% (threshold {alert.threshold:.2f}%)"
            if alert.key not in self._alert_widgets:
                row = tk.Frame(self.alert_banner, bg="#b00020")
                tk.Label(row, fg="white", bg="#b00020", text=text, font="SunValleyBodyStrongFont").pack(
                    side="left", padx=self._px(16), pady=self._px(8))
                ttk.Button(row, text="OK (silence)",
                           command=lambda k=alert.key: self.alert_manager.acknowledge(k)).pack(
                    side="right", padx=self._px(16), pady=self._px(6))
                row.pack(fill="x")
                self._alert_widgets[alert.key] = row
            else:
                row = self._alert_widgets[alert.key]
                row.winfo_children()[0].config(text=text)

    # ---------- mini view ----------
    def show_mini_view(self):
        self.iconify()  # stays on the taskbar, so clicking it there also brings the full view back
        self.mini_view.show()

    def show_full_view(self, focus_key=None, tab=None):
        self.mini_view.hide()
        self.deiconify()
        self.lift()
        self.focus_force()
        if focus_key is not None:
            tab = self.dashboard_tab
            row = self._row_for(focus_key)
            if self.tree.exists(row):
                self.tree.selection_set(row)
                self.tree.see(row)
        if tab is not None:
            self.notebook.select(tab)

    def _on_main_map(self, event):
        # <Map> on the root also fires for every child widget; only react to the window itself.
        if event.widget is self and self.mini_view._shown:
            self.mini_view.hide()

    def _on_close(self):
        self.monitor.stop()
        self.alert_manager.shutdown()
        self.destroy()
