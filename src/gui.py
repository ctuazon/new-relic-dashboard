"""Tkinter desktop UI: connection light, live dashboard, location/group config, settings, logs."""
import math
import queue
import threading
import time
import tkinter as tk
import warnings
from tkinter import messagebox, ttk

import matplotlib
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.colors import same_color
from matplotlib.gridspec import GridSpec
from matplotlib.lines import Line2D

from . import config, theme
from .alerts import AlertManager
from .logger import SessionLogger
from .monitor import MonitorEngine
from .newrelic_client import NewRelicClient, OVERVIEW_METRICS

matplotlib.rcParams["font.family"] = [theme.FONT_FAMILY, "DejaVu Sans"]

POLL_UI_MS = 300
CHART_LOOKBACK_MINUTES = 60
CHART_DPI = 96  # so chart point sizes match Tk's (10.5pt = 14px at 100% scaling)
CHART_FONT_PT = 10.5
CHART_GRID_BREAKPOINT = 1100  # logical px: narrower than this, the 1x4 strip becomes a 2x2 grid
CHART_MIN_2X2_HEIGHT = 380  # ...but only if there's enough height to stack two rows
DIM_ALPHA = 0.15
METRIC_SHORT_LABELS = {"response_time": "Resp (s)", "apdex": "Apdex", "throughput": "rpm", "errors": "Err %"}
METRIC_VALUE_FORMATS = {"response_time": "{:.3f}", "apdex": "{:.2f}", "throughput": "{:,.0f}", "errors": "{:.2f}"}
TREE_MAX_ROWS = 10
TREE_NARROW_WIDTH = 1050  # logical px
ERRORS_SUMMARY_CHARS = 48
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
LEGEND_PULSE_INTERVAL_MS = 150
LEGEND_PULSE_AMPLITUDE = 3.5
LEGEND_PULSE_PERIOD_S = 1.0


def format_12h_time(epoch_seconds: float) -> str:
    """Formats an epoch timestamp as h:mm:ss AM/PM (no leading zero on the hour)."""
    return time.strftime("%I:%M:%S %p", time.localtime(epoch_seconds)).lstrip("0")


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


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
        self.title(f"Error Inbox — {state.name}")
        scale = parent.ui_scale
        self.geometry(f"{int(1200 * scale)}x{int(600 * scale)}")
        self.records = state.error_records

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


MINI_FONT = (theme.FONT_FAMILY, 10)
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

        states = list(app.monitor.states.values())
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
        name_label = tk.Label(frame, text=short_name(state.name), fg=self.app.palette["mini_fg"], font=MINI_FONT)
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
        for text, tab in (("Dashboard", app.dashboard_tab), ("Locations & Groups", app.locations_tab),
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
        self._row_separators = {}
        self._last_metrics = None
        self._legend_names = []
        self._chart_timeline = []
        self._chart_values = {}
        self._crosshairs = {}
        self._crosshair_idx = None
        self._chart_cols = len(OVERVIEW_METRICS)
        self._chart_relayout_job = None
        self._tree_hover_key = None
        self._tree_narrow = False

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
        ttk.Separator(bar, orient="vertical").pack(side="right", fill="y", padx=self._px(6))
        ttk.Button(bar, text="Test Connection", command=self._test_connection).pack(side="right", padx=self._px(4))
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
        self.locations_tab = ttk.Frame(self.notebook, padding=pad)
        self.settings_tab = ttk.Frame(self.notebook, padding=pad)
        self.logs_tab = ttk.Frame(self.notebook, padding=pad)

        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.locations_tab, text="Locations & Groups")
        self.notebook.add(self.settings_tab, text="Settings")
        self.notebook.add(self.logs_tab, text="Logs")

        self._build_dashboard_tab()
        self._build_locations_tab()
        self._build_settings_tab()
        self._build_logs_tab()
        self._style_non_ttk_widgets()

    def _build_dashboard_tab(self):
        frame = self.dashboard_tab
        columns = ("name", "kind", "value", "threshold", "updated", "errors")
        # The tree column (#0) holds the status badge: a shape-coded icon plus a text label.
        self.tree = ttk.Treeview(frame, columns=columns, show="tree headings", height=1)
        self.tree.heading("#0", text="Status", anchor="w")
        self.tree.column("#0", width=self._px(150), minwidth=self._px(120), anchor="w", stretch=False)
        headings = {
            "name": "Name", "kind": "Type", "value": "Error Rate %", "threshold": "Threshold %",
            "updated": "Last Updated", "errors": "Error Inbox",
        }
        # Text left-aligned, numbers right-aligned so values line up for vertical scanning.
        layout = {"name": (220, "w"), "kind": (95, "w"), "value": (125, "e"), "threshold": (125, "e"),
                  "updated": (135, "e"), "errors": (300, "w")}
        for col in columns:
            width, anchor = layout[col]
            self.tree.heading(col, text=headings[col], anchor=anchor)
            # Only the Error Inbox column flexes, so names and numbers keep their width.
            self.tree.column(col, width=self._px(width), minwidth=self._px(width if col != "errors" else 140),
                             anchor=anchor, stretch=col == "errors")
        self.tree.pack(fill="x", expand=False, pady=(0, self._px(12)))
        self._apply_tree_tags()

        self.tree.bind("<Double-1>", self._on_dashboard_double_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_dashboard_select)
        self.tree.bind("<Button-3>", self._on_dashboard_right_click)
        # Column widths are recomputed after <Configure>/column drags, so resync once idle.
        self.tree.bind("<Configure>", self._on_tree_configure)
        self.tree.bind("<ButtonRelease-1>", lambda e: self.after_idle(self._sync_error_overlays), add="+")
        self.tree.bind("<MouseWheel>", lambda e: self.after_idle(self._sync_error_overlays), add="+")
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self._hide_tree_tooltip())

        self._build_charts_panel(frame)

    def _on_tree_configure(self, event):
        # When narrow, drop the low-value Type column so the Error Inbox summary keeps its room.
        narrow = event.width < self._px(TREE_NARROW_WIDTH)
        if narrow != self._tree_narrow:
            self._tree_narrow = narrow
            columns = self.tree["columns"]
            self.tree.configure(displaycolumns=[c for c in columns if c != "kind"] if narrow else columns)
        self.after_idle(self._sync_error_overlays)

    def _apply_tree_tags(self):
        pal = self.palette
        # Plain surface for healthy rows; only rows that need attention get a soft tint.
        self.tree.tag_configure("ok", background=pal["surface"])
        self.tree.tag_configure("pending", background=pal["surface"], foreground=pal["muted"])
        self.tree.tag_configure("anomaly", background=pal["row_anomaly"])
        self.tree.tag_configure("error", background=pal["row_error"])
        self.tree.tag_configure("breach", background=pal["row_breach"])

    def _build_charts_panel(self, parent):
        charts_frame = ttk.Frame(parent)
        charts_frame.pack(fill="both", expand=True)

        self.chart_fig = Figure(figsize=(11, 4), dpi=CHART_DPI)
        self.chart_fig.set_facecolor(self.palette["chart_bg"])
        self.chart_axes = {}
        for i, (key, title, _select) in enumerate(OVERVIEW_METRICS):
            ax = self.chart_fig.add_subplot(1, len(OVERVIEW_METRICS), i + 1)
            self._style_axis(ax, title)
            self.chart_axes[key] = ax
        self._axis_keys = {ax: key for key, ax in self.chart_axes.items()}

        self.chart_canvas = FigureCanvasTkAgg(self.chart_fig, master=charts_frame)
        widget = self.chart_canvas.get_tk_widget()
        widget.configure(highlightthickness=0, bd=0)
        widget.pack(fill="both", expand=True)
        widget.bind("<Configure>", self._on_chart_resize, add="+")
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
        frame = ttk.Frame(self.settings_tab, padding=self._px(8))
        frame.pack(fill="both", expand=True)
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

        ttk.Button(frame, text="Save Settings", style="Accent.TButton", command=self._save_settings).grid(
            row=13, column=0, pady=self._px(20), sticky="w")

    def _build_logs_tab(self):
        frame = self.logs_tab
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, self._px(10)))
        ttk.Label(top, text=f"Log file: {self.session_logger.log_path}", style="Muted.TLabel").pack(side="left")
        ttk.Label(top, text=f"Readings CSV: {self.session_logger.csv_path.name}", style="Muted.TLabel").pack(
            side="left", padx=(self._px(16), 0))
        ttk.Button(top, text="Refresh", command=self._refresh_logs).pack(side="right")

        self.log_text = tk.Text(frame, height=28, state="disabled", wrap="none", font="TkFixedFont",
                                borderwidth=0, highlightthickness=0, padx=self._px(10), pady=self._px(8))
        self.log_text.pack(fill="both", expand=True)
        self._refresh_logs()

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
            ok, message = self.client.test_connection()
            self.event_queue.put(("conn_result", (ok, message)))

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
            self.tree.insert("", "end", iid=f"loc:{loc.name}", text=" " + STATUS_LABELS["pending"],
                             image=self.icon("pending"), values=(loc.name, "location", "-", "-", "-", "-"),
                             tags=("pending",))
        for grp in self.cfg.groups:
            self.tree.insert("", "end", iid=f"grp:{grp.name}", text=" " + STATUS_LABELS["pending"],
                             image=self.icon("pending"), values=(grp.name, "group", "-", "-", "-", "-"),
                             tags=("pending",))
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
                    self._set_connection("connected" if ok else "failed", message)
                elif kind == "start_conn_result":
                    ok, message = payload
                    self._set_connection("connected" if ok else "failed", message)
                    self.refresh_btn.config(state="normal")
                    if ok:
                        self.monitor.start()
                        self._new_error_iids.clear()
                        for iid in list(self._error_cell_overlays.keys()):
                            self._remove_error_overlay(iid)
                        self.start_btn.config(text="Stop Monitoring", style="TButton", state="normal")
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

    def _layout_charts(self):
        """1x4 strip when there's room, 2x2 grid when the window gets narrow; legend re-wrapped to
        the width, with exactly its height reserved above the charts."""
        widget = self.chart_canvas.get_tk_widget()
        width, height = widget.winfo_width(), widget.winfo_height()
        if width <= 1 or height <= 1:
            return
        count = len(OVERVIEW_METRICS)
        narrow = width < self._px(CHART_GRID_BREAKPOINT) and height >= self._px(CHART_MIN_2X2_HEIGHT)
        cols = 2 if narrow else count
        if cols != self._chart_cols:
            grid = GridSpec(math.ceil(count / cols), cols, figure=self.chart_fig)
            for i, ax in enumerate(self.chart_axes.values()):
                ax.set_subplotspec(grid[i // cols, i % cols])
            self._chart_cols = cols
        top = self._build_legend(width)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # tight_layout complains when the window is tiny
            self.chart_fig.tight_layout(rect=(0, 0, 1, top), h_pad=1.6, w_pad=2.2)

    def _build_legend(self, width):
        """(Re)creates the clickable legend and returns the figure fraction left for the charts."""
        for old_legend in list(self.chart_fig.legends):
            old_legend.remove()
        self._legend_app_by_artist = {}
        self._legend_texts = {}
        self._legend_handles = {}
        names = self._legend_names
        if not names:
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
        legend_height = legend.get_window_extent(renderer).height + self._px(8)
        return max(0.5, 1 - legend_height / self.chart_fig.bbox.height)

    def _set_xticks(self):
        timeline = self._chart_timeline
        if not timeline:
            return
        per_chart = self.chart_canvas.get_tk_widget().winfo_width() / self._chart_cols
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
    def _render_dashboard(self):
        existing = set(self.tree.get_children())
        seen = set()
        for state in self.monitor.states.values():
            iid = state.key
            seen.add(iid)
            value = f"{state.value:.2f}" if state.value is not None else "-"
            threshold = f"{state.threshold:.2f}" if state.threshold is not None else "-"
            updated = format_12h_time(state.last_updated) if state.last_updated else "-"
            breached = self._breached_metrics(state)
            if state.status == "OK" and breached:
                badge = "breach"
            else:
                badge = {"OK": "ok", "ANOMALY": "anomaly", "ERROR": "error"}.get(state.status, "pending")
            tag = "breach" if breached else badge
            if state.error_records:
                latest = state.error_records[0]
                count = len(state.error_records)
                summary = " ".join(f"{latest.error_class}: {latest.error_message}".split())
                # Short summary only; the full exception shows on hover and in the Error Inbox.
                errors = truncate(f"{count} error{'s' if count != 1 else ''} · {summary}", ERRORS_SUMMARY_CHARS)
            else:
                errors = "-"
            newest_guid = state.error_records[0].guid if state.error_records else None
            # The first poll of a session just establishes the baseline (whatever's already
            # sitting in the lookback window) -- only a different newest guid after that counts
            # as "new". Comparing guids rather than counts keeps working once the per-location
            # record list hits its cap and stops growing.
            if (newest_guid and newest_guid != self._prev_newest_error_guid.get(iid)
                    and not self._awaiting_first_update):
                self._new_error_iids.add(iid)
            self._prev_newest_error_guid[iid] = newest_guid
            values = (state.name, state.kind, value, threshold, updated, errors)
            item = dict(text=" " + STATUS_LABELS[badge], image=self.icon(badge), values=values, tags=(tag,))
            if iid in existing:
                self.tree.item(iid, **item)
            else:
                self.tree.insert("", "end", iid=iid, **item)
        for iid in existing - seen:
            self.tree.delete(iid)
            self._new_error_iids.discard(iid)
            self._prev_newest_error_guid.pop(iid, None)
            self._remove_error_overlay(iid)
        self._fit_tree_height()
        self._sync_error_overlays()

    def _breached_metrics(self, state):
        if state.kind != "location":
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
        key = (iid, self.tree.identify_column(event.x)) if iid and region in ("cell", "tree") else None
        self._update_tree_tooltip(key, event.x_root, event.y_root)

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

    def _tree_tooltip_text(self, iid, column):
        state = self.monitor.states.get(iid)
        if state is None:
            return None
        if column == "#0":
            return self._status_explanation(state)
        # identify_column() numbers *displayed* columns, which shift when Type is hidden.
        if column == "overlay" or self.tree.column(column, "id") == "errors":
            column = "errors"
        if column == "errors" and state.error_records:
            rec = state.error_records[0]
            count = len(state.error_records)
            status = rec.status_code if rec.status_code is not None else "-"
            endpoint = f"{rec.method or ''} {rec.endpoint}".strip()
            return (f"{count} error{'s' if count != 1 else ''} captured. Latest:\n\n"
                    f"{rec.error_class}\n{rec.error_message}\n\n"
                    f"Endpoint: {endpoint}\n"
                    f"HTTP status: {status}\nContainer: {rec.container}\n"
                    f"Time: {ErrorInboxDialog._fmt_time(rec.timestamp)}\n\n"
                    f"Double-click to open the Error Inbox.")
        return None

    def _status_explanation(self, state):
        breached = self._breached_metrics(state)
        titles = {key: title for key, title, _select in OVERVIEW_METRICS}
        lines = []
        if state.status == "ERROR":
            lines.append(f"Query error:\n{state.error or 'unknown error'}")
        elif state.status == "ANOMALY" and state.value is not None and state.threshold is not None:
            lines.append(f"Anomaly: error rate {state.value:.2f}% is above the {state.threshold:.2f}% threshold.")
        elif state.status == "PENDING":
            lines.append("Waiting for the first results…")
        elif state.status == "OK":
            lines.append("Error rate is within its threshold.")
        if breached:
            lines.append("Chart threshold breached: " + ", ".join(titles[k] for k in sorted(breached)))
        return "\n\n".join(lines) or None

    # ---------- widgets layered over the grid ----------
    def _remove_error_overlay(self, iid):
        overlay = self._error_cell_overlays.pop(iid, None)
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

    def _sync_error_overlays(self):
        self._sync_row_separators()
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
                    self.tree, text=text, bg=self.palette["new_error_bg"], fg=self.palette["new_error_fg"],
                    anchor="w", font="TkDefaultFont", padx=self._px(6),
                )
                overlay.bind("<Double-1>", lambda e, i=iid: self._on_error_cell_double_click(i))
                overlay.bind("<Button-1>", lambda e, i=iid: self._on_error_cell_click(i))
                overlay.bind("<Button-3>", lambda e: self._on_dashboard_right_click())
                overlay.bind("<Motion>", lambda e, i=iid: self._update_tree_tooltip(
                    (i, "overlay"), e.x_root, e.y_root))
                overlay.bind("<Leave>", lambda e: self._hide_tree_tooltip())
                self._error_cell_overlays[iid] = overlay
            else:
                overlay.config(text=text)
            x, y, width, height = bbox
            overlay.place(x=x, y=y, width=width, height=height - 1)  # leave the row separator visible

    def _on_error_cell_click(self, iid):
        self.tree.selection_set(iid)
        self._on_dashboard_select()

    def _on_error_cell_double_click(self, iid):
        self.open_error_inbox(iid)

    def _on_dashboard_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.open_error_inbox(iid)

    def open_error_inbox(self, iid):
        """Clears the series' new-error flag and opens its Error Inbox (if it has any errors)."""
        self._new_error_iids.discard(iid)
        self._remove_error_overlay(iid)
        self._hide_tree_tooltip()
        state = self.monitor.states.get(iid)
        if not state or not state.error_records:
            return
        dialog = ErrorInboxDialog(self, state)
        dialog.lift()
        dialog.focus_force()

    def _on_dashboard_select(self, event=None):
        sel = self.tree.selection()
        if not sel or not sel[0].startswith("loc:"):
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
            if self.tree.exists(focus_key):
                self.tree.selection_set(focus_key)
                self.tree.see(focus_key)
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
