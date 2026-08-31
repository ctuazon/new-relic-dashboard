"""Tkinter desktop UI: connection light, live dashboard, location/group config, settings, logs."""
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.lines import Line2D

from . import config
from .alerts import AlertManager
from .logger import SessionLogger
from .monitor import MonitorEngine
from .newrelic_client import NewRelicClient, OVERVIEW_METRICS

POLL_UI_MS = 300
CHART_LOOKBACK_MINUTES = 60
CHART_COLOR_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]

# Industry-standard-ish defaults for the 3 metrics the app doesn't already threshold.
# "errors" deliberately has no fixed default here: it reuses each location's own existing
# error-rate threshold (custom or auto-computed), per the ask to reuse what's already there.
CHART_THRESHOLDS = {
    "response_time": {"direction": "above", "default": 1.0},   # seconds — common "slow" cutoff
    "apdex": {"direction": "below", "default": 0.7},           # Apdex Fair/Poor boundary
    "throughput": {"direction": "below", "default": 1.0},      # rpm — near-zero traffic/outage
}
BREACH_LEGEND_COLOR = "#d00000"
BREACH_ROW_COLOR = "#ff4d4d"
LEGEND_PULSE_INTERVAL_MS = 150
LEGEND_BASE_FONTSIZE = 7  # matches the fontsize=7 used when the legend is (re)created
LEGEND_PULSE_AMPLITUDE = 3.5
LEGEND_PULSE_PERIOD_S = 1.0
NEW_ERROR_CELL_BG = "#0b3d0b"
NEW_ERROR_CELL_FG = "#ffffff"


def format_12h_time(epoch_seconds: float) -> str:
    """Formats an epoch timestamp as h:mm:ss AM/PM (no leading zero on the hour)."""
    return time.strftime("%I:%M:%S %p", time.localtime(epoch_seconds)).lstrip("0")


class LocationDialog(tk.Toplevel):
    def __init__(self, parent, location=None):
        super().__init__(parent)
        self.title("Location")
        self.result = None
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

        ttk.Label(
            self,
            text=("Tip: the query must return one numeric column, e.g.\n"
                  "SELECT percentage(count(*), WHERE error IS true) FROM Transaction\n"
                  "WHERE appName = 'X' SINCE 5 minutes ago"),
            foreground="gray", justify="left",
        ).grid(row=5, column=0, columnspan=2, padx=8, pady=6, sticky="w")

        btns = ttk.Frame(self)
        btns.grid(row=6, column=0, columnspan=2, pady=10)
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
        self.destroy()


class GroupDialog(tk.Toplevel):
    def __init__(self, parent, all_location_names, group=None):
        super().__init__(parent)
        self.title("Combined Group")
        self.result = None
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
        self.title(f"Error Inbox — {state.name}")
        self.geometry("900x520")
        self.records = state.error_records

        columns = ("time", "class", "endpoint", "status", "container")
        self.list_tree = ttk.Treeview(self, columns=columns, show="headings", height=12)
        headings = {
            "time": "Time", "class": "Error Class", "endpoint": "Endpoint",
            "status": "HTTP Status", "container": "Container",
        }
        widths = {"time": 140, "class": 220, "endpoint": 220, "status": 90, "container": 140}
        for col in columns:
            self.list_tree.heading(col, text=headings[col])
            self.list_tree.column(col, width=widths[col], anchor="w")
        self.list_tree.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        detail_frame = ttk.LabelFrame(self, text="Details", padding=8)
        detail_frame.pack(fill="x", padx=8, pady=(0, 8))
        self.detail_text = tk.Text(detail_frame, height=9, wrap="word", state="disabled")
        self.detail_text.pack(fill="both", expand=True)

        for i, rec in enumerate(self.records):
            self.list_tree.insert("", "end", iid=str(i), values=(
                self._fmt_time(rec.timestamp), rec.error_class, rec.endpoint,
                rec.status_code if rec.status_code is not None else "-", rec.container,
            ))

        self.list_tree.bind("<<TreeviewSelect>>", self._show_detail)
        if self.records:
            self.list_tree.selection_set("0")
            self._show_detail()

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
        text = (
            f"Time: {self._fmt_time(rec.timestamp)}\n"
            f"Error Class: {rec.error_class}\n"
            f"Message: {rec.error_message}\n"
            f"Endpoint: {rec.endpoint}\n"
            f"HTTP Method: {rec.method or '-'}\n"
            f"HTTP Status: {rec.status_code if rec.status_code is not None else '-'}\n"
            f"Container: {rec.container}\n"
        )
        self.detail_text.config(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("end", text)
        self.detail_text.config(state="disabled")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("New Relic Anomaly Monitor")
        self.geometry("1000x900")

        self.cfg = config.AppConfig.load()
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
        self._isolated_app = None
        self._breaching_apps = {}
        self._avg_annotations = {}
        self._new_error_iids = set()
        self._prev_error_counts = {}
        self._error_cell_overlays = {}

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

        self._build_ui()
        self.after(POLL_UI_MS, self._drain_queue)
        self.after(LEGEND_PULSE_INTERVAL_MS, self._animate_legend)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI construction ----------
    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        self.light_canvas = tk.Canvas(top, width=22, height=22, highlightthickness=0)
        self.light = self.light_canvas.create_oval(2, 2, 20, 20, fill="gray")
        self.light_canvas.pack(side="left", padx=(0, 8))

        self.status_var = tk.StringVar(value="Not tested")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=(0, 16))

        ttk.Button(top, text="Test Connection", command=self._test_connection).pack(side="left", padx=4)
        self.start_btn = ttk.Button(top, text="Start Monitoring", command=self._toggle_monitoring)
        self.start_btn.pack(side="left", padx=4)
        self.refresh_btn = ttk.Button(top, text="Refresh Now", command=self._refresh_now)
        self.refresh_btn.pack(side="left", padx=4)
        ttk.Button(top, text="Silence All", command=self.alert_manager.acknowledge_all).pack(side="left", padx=4)

        self.loading_bar = ttk.Progressbar(top, mode="indeterminate", length=110)
        self.loading_label = ttk.Label(top, text="", foreground="#555")
        self.loading_label.pack(side="left", padx=(12, 0))

        self.alert_banner = tk.Frame(self, bg="#b00020")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.dashboard_tab = ttk.Frame(self.notebook)
        self.locations_tab = ttk.Frame(self.notebook)
        self.settings_tab = ttk.Frame(self.notebook)
        self.logs_tab = ttk.Frame(self.notebook)

        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.locations_tab, text="Locations & Groups")
        self.notebook.add(self.settings_tab, text="Settings")
        self.notebook.add(self.logs_tab, text="Logs")

        self._build_dashboard_tab()
        self._build_locations_tab()
        self._build_settings_tab()
        self._build_logs_tab()

    def _build_dashboard_tab(self):
        frame = self.dashboard_tab
        columns = ("name", "kind", "value", "threshold", "status", "updated", "errors")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=9)
        headings = {
            "name": "Name", "kind": "Type", "value": "Error Rate %", "threshold": "Threshold %",
            "status": "Status", "updated": "Last Updated", "errors": "Error Inbox",
        }
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=140, anchor="center")
        self.tree.column("name", width=200, anchor="w")
        self.tree.column("errors", width=320, anchor="w")
        self.tree.pack(fill="x", expand=False, padx=6, pady=6)

        self.tree.tag_configure("ok", background="#e6ffed")
        self.tree.tag_configure("anomaly", background="#ffe0e0")
        self.tree.tag_configure("error", background="#fff3cd")
        self.tree.tag_configure("pending", background="#f0f0f0")
        self.tree.tag_configure("breach", background=BREACH_ROW_COLOR)
        self.tree.bind("<Double-1>", self._on_dashboard_double_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_dashboard_select)
        self.tree.bind("<Button-3>", self._on_dashboard_right_click)
        self.tree.bind("<Configure>", lambda e: self._sync_error_overlays())

        self._build_charts_panel(frame)

    def _build_charts_panel(self, parent):
        charts_frame = ttk.Frame(parent)
        charts_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.chart_fig = Figure(figsize=(11, 4), dpi=90)
        self.chart_axes = {}
        for i, (key, title, _select) in enumerate(OVERVIEW_METRICS):
            ax = self.chart_fig.add_subplot(1, len(OVERVIEW_METRICS), i + 1)
            ax.set_title(title, fontsize=9)
            ax.tick_params(labelsize=7)
            self.chart_axes[key] = ax
        self.chart_fig.tight_layout(rect=(0, 0, 1, 0.88))

        self.chart_canvas = FigureCanvasTkAgg(self.chart_fig, master=charts_frame)
        self.chart_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.chart_canvas.mpl_connect("pick_event", self._on_legend_pick)
        self.chart_canvas.mpl_connect("button_press_event", self._on_chart_click)

    def _build_locations_tab(self):
        frame = self.locations_tab
        left = ttk.LabelFrame(frame, text="Locations", padding=6)
        left.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.loc_listbox = tk.Listbox(left, height=16, exportselection=False)
        self.loc_listbox.pack(fill="both", expand=True)
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="Add", command=self._add_location).pack(side="left", padx=3)
        ttk.Button(btns, text="Edit", command=self._edit_location).pack(side="left", padx=3)
        ttk.Button(btns, text="Remove", command=self._remove_location).pack(side="left", padx=3)

        right = ttk.LabelFrame(frame, text="Combined Groups", padding=6)
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        self.grp_listbox = tk.Listbox(right, height=16, exportselection=False)
        self.grp_listbox.pack(fill="both", expand=True)
        btns2 = ttk.Frame(right)
        btns2.pack(fill="x", pady=6)
        ttk.Button(btns2, text="Add", command=self._add_group).pack(side="left", padx=3)
        ttk.Button(btns2, text="Edit", command=self._edit_group).pack(side="left", padx=3)
        ttk.Button(btns2, text="Remove", command=self._remove_group).pack(side="left", padx=3)

        self._refresh_loc_lists()

    def _build_settings_tab(self):
        frame = ttk.Frame(self.settings_tab, padding=12)
        frame.pack(fill="both", expand=True)
        s = self.cfg.settings

        def row(r, label, var, width=10):
            ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=var, width=width).grid(row=r, column=1, sticky="w", pady=4)

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
            row=6, column=1, sticky="w")

        self.tts_var = tk.BooleanVar(value=s.tts_enabled)
        ttk.Checkbutton(frame, text="Speak location name on anomaly", variable=self.tts_var).grid(
            row=7, column=1, sticky="w")

        self.errbox_var = tk.BooleanVar(value=s.error_inbox_enabled)
        ttk.Checkbutton(frame, text="Watch Errors Inbox per container (session log only)",
                         variable=self.errbox_var).grid(row=8, column=1, sticky="w")

        self.errbox_lookback_var = tk.StringVar(value=str(s.error_inbox_lookback_seconds))
        row(9, "Errors Inbox lookback window (seconds):", self.errbox_lookback_var)

        self.errbox_attr_var = tk.StringVar(value=s.error_inbox_container_attribute)
        row(10, "Errors Inbox container attribute:", self.errbox_attr_var, width=24)

        ttk.Label(frame, text=f"Region: {config.NEW_RELIC_REGION}  (set NEW_RELIC_REGION in .env, restart to apply)",
                  foreground="gray").grid(row=11, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(frame, text=f"Account ID: {config.NEW_RELIC_ACCOUNT_ID or '(not set)'}  "
                               f"(set NEW_RELIC_ACCOUNT_ID in .env, restart to apply)",
                  foreground="gray").grid(row=12, column=0, columnspan=2, sticky="w")

        ttk.Button(frame, text="Save Settings", command=self._save_settings).grid(
            row=13, column=0, pady=16, sticky="w")

    def _build_logs_tab(self):
        frame = self.logs_tab
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text=f"Log file: {self.session_logger.log_path}").pack(side="left")
        ttk.Label(top, text=f"Readings CSV: {self.session_logger.csv_path.name}").pack(side="left", padx=(16, 0))
        ttk.Button(top, text="Refresh", command=self._refresh_logs).pack(side="right")

        self.log_text = tk.Text(frame, height=28, state="disabled", wrap="none")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)
        self._refresh_logs()

    # ---------- actions ----------
    def _test_connection(self):
        self.status_var.set("Testing...")
        self.light_canvas.itemconfig(self.light, fill="#f1c40f")

        def worker():
            ok, message = self.client.test_connection()
            self.event_queue.put(("conn_result", (ok, message)))

        threading.Thread(target=worker, daemon=True).start()

    def _toggle_monitoring(self):
        if self.monitor.is_running():
            self.monitor.stop()
            self.start_btn.config(text="Start Monitoring")
            self._stop_loading_feedback()
            return
        self._start_monitoring()

    def _start_monitoring(self):
        if not self.cfg.locations:
            messagebox.showwarning("No locations", "Add at least one location before starting monitoring.")
            return

        self.start_btn.config(state="disabled")
        self.refresh_btn.config(state="disabled")
        self.status_var.set("Checking connection...")
        self.light_canvas.itemconfig(self.light, fill="#f1c40f")

        def worker():
            ok, message = self.client.test_connection()
            self.event_queue.put(("start_conn_result", (ok, message)))

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
        for loc in self.cfg.locations:
            if not loc.enabled:
                continue
            self.tree.insert("", "end", iid=f"loc:{loc.name}",
                              values=(loc.name, "location", "-", "-", "Loading...", "-", "-"),
                              tags=("pending",))
        for grp in self.cfg.groups:
            self.tree.insert("", "end", iid=f"grp:{grp.name}",
                              values=(grp.name, "group", "-", "-", "Loading...", "-", "-"),
                              tags=("pending",))

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
        except ValueError:
            messagebox.showerror("Invalid input", "Please enter valid numbers.")
            return
        s.sound_enabled = self.sound_var.get()
        s.tts_enabled = self.tts_var.get()
        s.error_inbox_enabled = self.errbox_var.get()
        s.error_inbox_container_attribute = self.errbox_attr_var.get().strip() or "containerId"
        self.alert_manager.sound_enabled = s.sound_enabled
        self.alert_manager.tts_enabled = s.tts_enabled
        self.alert_manager.repeat_seconds = s.alert_repeat_seconds
        self.cfg.save()
        messagebox.showinfo("Saved", "Settings saved.")

    def _refresh_logs(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(self.session_logger.tail()))
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ---------- queue draining / rendering ----------
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "conn_result":
                    ok, message = payload
                    self.light_canvas.itemconfig(self.light, fill="#2ecc71" if ok else "#e74c3c")
                    self.status_var.set(message)
                elif kind == "start_conn_result":
                    ok, message = payload
                    self.light_canvas.itemconfig(self.light, fill="#2ecc71" if ok else "#e74c3c")
                    self.status_var.set(message)
                    self.refresh_btn.config(state="normal")
                    if ok:
                        self.monitor.start()
                        self._new_error_iids.clear()
                        for iid in list(self._error_cell_overlays.keys()):
                            self._remove_error_overlay(iid)
                        self.start_btn.config(text="Stop Monitoring", state="normal")
                        self._start_loading_feedback()
                    else:
                        self.start_btn.config(state="normal")
                        messagebox.showerror("Connection failed", f"Can't start monitoring:\n{message}")
                elif kind == "update":
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

    def _color_for(self, app_name):
        if app_name not in self._chart_colors:
            idx = len(self._chart_colors) % len(CHART_COLOR_PALETTE)
            self._chart_colors[app_name] = CHART_COLOR_PALETTE[idx]
        return self._chart_colors[app_name]

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
        enabled_names = [loc.app_name for loc in self.cfg.locations if loc.enabled]
        self._chart_lines = {}
        self._avg_annotations = {}  # ax.clear() below discards the old annotation artists too
        for key, title, _select in OVERVIEW_METRICS:
            ax = self.chart_axes[key]
            ax.clear()
            ax.set_title(title, fontsize=9)
            ax.tick_params(labelsize=7)
            series = metrics.get(key, {})
            tick_points = None
            for app_name in enabled_names:
                points = series.get(app_name)
                if not points:
                    continue
                values = [p[1] for p in points]
                line, = ax.plot(range(len(values)), values, color=self._color_for(app_name), linewidth=1.2)
                self._chart_lines[(key, app_name)] = line
                if tick_points is None or len(points) > len(tick_points):
                    tick_points = points
            if tick_points:
                n = len(tick_points)
                step = max(1, n // 4)
                idx = list(range(0, n, step))
                ax.set_xticks(idx)
                labels = [time.strftime("%I:%M %p", time.localtime(tick_points[i][0])).lstrip("0")
                          for i in idx]
                ax.set_xticklabels(labels, fontsize=7)

        for old_legend in list(self.chart_fig.legends):
            old_legend.remove()
        handles = [Line2D([0], [0], color=self._color_for(name), label=name) for name in enabled_names]
        legend = self.chart_fig.legend(handles=handles, loc="upper center", ncol=min(len(handles), 6) or 1,
                                        fontsize=7, frameon=False, bbox_to_anchor=(0.5, 1.0))
        self._legend_app_by_artist = {}
        self._legend_texts = {}
        for legline, text_artist, app_name in zip(legend.legend_handles, legend.get_texts(), enabled_names):
            legline.set_picker(6)
            self._legend_app_by_artist[legline] = app_name
            self._legend_texts[app_name] = text_artist

        self.chart_fig.tight_layout(rect=(0, 0, 1, 0.88))
        self._apply_isolation()
        self._breaching_apps = self._compute_breaches(metrics, enabled_names)
        self.chart_canvas.draw_idle()
        self._render_dashboard()

    def _apply_isolation(self):
        for (_key, app_name), line in self._chart_lines.items():
            line.set_visible(self._isolated_app is None or app_name == self._isolated_app)
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
            if self._isolated_app is None:
                continue
            line = self._chart_lines.get((key, self._isolated_app))
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
                ha="right", va="top", fontsize=7, color=color,
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec=color, alpha=0.85),
            )
            self._avg_annotations[key] = (refline, label)

    def _on_legend_pick(self, event):
        app_name = self._legend_app_by_artist.get(event.artist)
        if app_name is None:
            return
        self._isolated_app = app_name
        self._apply_isolation()
        self.chart_canvas.draw_idle()

    def _on_chart_click(self, event):
        if event.button == 3:  # right click anywhere on the chart area resets
            self._isolated_app = None
            self._apply_isolation()
            self.chart_canvas.draw_idle()

    def _threshold_for(self, metric_key, app_name):
        if metric_key == "errors":
            loc = next((l for l in self.cfg.locations if l.app_name == app_name), None)
            if not loc:
                return None
            state = self.monitor.states.get(f"loc:{loc.name}")
            return state.threshold if state and state.threshold is not None else None
        return CHART_THRESHOLDS[metric_key]["default"]

    def _compute_breaches(self, metrics, enabled_names):
        breaches = {}
        for key, _title, _select in OVERVIEW_METRICS:
            direction = "above" if key == "errors" else CHART_THRESHOLDS[key]["direction"]
            series = metrics.get(key, {})
            for app_name in enabled_names:
                points = series.get(app_name)
                if not points:
                    continue
                threshold = self._threshold_for(key, app_name)
                if threshold is None:
                    continue
                latest_value = points[-1][1]
                breached = latest_value > threshold if direction == "above" else latest_value < threshold
                if breached:
                    breaches.setdefault(app_name, set()).add(key)
        return breaches

    def _animate_legend(self):
        # Always sweep every legend text (not just when something is breaching) so a service
        # that just recovered gets reset back from mid-pulse instead of freezing red/enlarged.
        phase = (time.time() % LEGEND_PULSE_PERIOD_S) / LEGEND_PULSE_PERIOD_S
        size = LEGEND_BASE_FONTSIZE + LEGEND_PULSE_AMPLITUDE * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
        changed = False
        for app_name, text_artist in self._legend_texts.items():
            if self._breaching_apps.get(app_name):
                text_artist.set_fontsize(size)
                text_artist.set_color(BREACH_LEGEND_COLOR)
                text_artist.set_fontweight("bold")
                changed = True
            elif text_artist.get_fontsize() != LEGEND_BASE_FONTSIZE or text_artist.get_color() != "black":
                text_artist.set_fontsize(LEGEND_BASE_FONTSIZE)
                text_artist.set_color("black")
                text_artist.set_fontweight("normal")
                changed = True
        if changed:
            self.chart_canvas.draw_idle()
        self.after(LEGEND_PULSE_INTERVAL_MS, self._animate_legend)

    def _render_dashboard(self):
        existing = set(self.tree.get_children())
        seen = set()
        name_to_app = {loc.name: loc.app_name for loc in self.cfg.locations}
        for state in self.monitor.states.values():
            iid = state.key
            seen.add(iid)
            value = f"{state.value:.2f}" if state.value is not None else "-"
            threshold = f"{state.threshold:.2f}" if state.threshold is not None else "-"
            updated = format_12h_time(state.last_updated) if state.last_updated else "-"
            status = state.error if state.status == "ERROR" and state.error else state.status
            tag = state.status.lower()
            if state.kind == "location":
                app_name = name_to_app.get(state.name)
                if app_name and self._breaching_apps.get(app_name):
                    tag = "breach"
            if state.error_records:
                latest = state.error_records[0]
                summary = f"{latest.error_class}: {latest.error_message}"
                if len(summary) > 65:
                    summary = summary[:62] + "..."
                errors = f"{len(state.error_records)} found (double-click for details) — {summary}"
            else:
                errors = "-"
            prev_count = self._prev_error_counts.get(iid, 0)
            # The first poll of a session just establishes the baseline (whatever's already
            # sitting in the lookback window) -- only growth from there counts as "new".
            if len(state.error_records) > prev_count and not self._awaiting_first_update:
                self._new_error_iids.add(iid)
            self._prev_error_counts[iid] = len(state.error_records)
            values = (state.name, state.kind, value, threshold, status, updated, errors)
            if iid in existing:
                self.tree.item(iid, values=values, tags=(tag,))
            else:
                self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))
        for iid in existing - seen:
            self.tree.delete(iid)
            self._new_error_iids.discard(iid)
            self._prev_error_counts.pop(iid, None)
            self._remove_error_overlay(iid)
        self._sync_error_overlays()

    def _remove_error_overlay(self, iid):
        overlay = self._error_cell_overlays.pop(iid, None)
        if overlay is not None:
            overlay.destroy()

    def _sync_error_overlays(self):
        for iid in list(self._error_cell_overlays.keys()):
            if iid not in self._new_error_iids or not self.tree.exists(iid):
                self._remove_error_overlay(iid)
        for iid in self._new_error_iids:
            if not self.tree.exists(iid):
                continue
            bbox = self.tree.bbox(iid, "errors")
            overlay = self._error_cell_overlays.get(iid)
            if not bbox:
                if overlay is not None:
                    overlay.place_forget()
                continue
            text = self.tree.set(iid, "errors")
            if overlay is None:
                overlay = tk.Label(
                    self.tree, text=text, bg=NEW_ERROR_CELL_BG, fg=NEW_ERROR_CELL_FG,
                    anchor="w", font=("TkDefaultFont", 9), padx=4,
                )
                overlay.bind("<Double-1>", lambda e, i=iid: self._on_error_cell_double_click(i))
                overlay.bind("<Button-1>", lambda e, i=iid: self._on_error_cell_click(i))
                overlay.bind("<Button-3>", lambda e: self._on_dashboard_right_click())
                self._error_cell_overlays[iid] = overlay
            else:
                overlay.config(text=text)
            x, y, width, height = bbox
            overlay.place(x=x, y=y, width=width, height=height)

    def _on_error_cell_click(self, iid):
        self.tree.selection_set(iid)
        self._on_dashboard_select()

    def _on_error_cell_double_click(self, iid):
        self._new_error_iids.discard(iid)
        self._remove_error_overlay(iid)
        state = self.monitor.states.get(iid)
        if state and state.error_records:
            ErrorInboxDialog(self, state)

    def _on_dashboard_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self._new_error_iids.discard(iid)
        self._remove_error_overlay(iid)
        state = self.monitor.states.get(iid)
        if not state or not state.error_records:
            return
        ErrorInboxDialog(self, state)

    def _on_dashboard_select(self, event=None):
        sel = self.tree.selection()
        if not sel or not sel[0].startswith("loc:"):
            return  # groups have no chart line to isolate
        name = sel[0][len("loc:"):]
        loc = next((l for l in self.cfg.locations if l.name == name), None)
        if not loc:
            return
        self._isolated_app = loc.app_name
        self._apply_isolation()
        self.chart_canvas.draw_idle()

    def _on_dashboard_right_click(self, event=None):
        self._isolated_app = None
        self._apply_isolation()
        self.chart_canvas.draw_idle()

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
            text = f"ANOMALY: {alert.name} — {alert.value:.2f}% (threshold {alert.threshold:.2f}%)"
            if alert.key not in self._alert_widgets:
                row = tk.Frame(self.alert_banner, bg="#b00020")
                tk.Label(row, fg="white", bg="#b00020", text=text).pack(side="left", padx=8, pady=4)
                ttk.Button(row, text="OK (silence)",
                           command=lambda k=alert.key: self.alert_manager.acknowledge(k)).pack(
                    side="right", padx=8, pady=4)
                row.pack(fill="x")
                self._alert_widgets[alert.key] = row
            else:
                row = self._alert_widgets[alert.key]
                row.winfo_children()[0].config(text=text)

    def _on_close(self):
        self.monitor.stop()
        self.alert_manager.shutdown()
        self.destroy()
