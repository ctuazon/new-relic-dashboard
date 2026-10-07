"""Look & feel: Sun Valley (Fluent) ttk theme with light/dark palettes, OS-scaled fonts,
shape-coded status icons and a hover tooltip, shared by the main window, dialogs and mini view."""
import base64
import ctypes
import struct
import sys
import tkinter as tk
import zlib
from tkinter import font as tkfont, ttk

try:
    import sv_ttk
except ImportError:  # falls back to a plainer built-in theme that still follows the palette
    sv_ttk = None

FONT_FAMILY = "Segoe UI"
FONT_FAMILY_STRONG = "Segoe UI Semibold"
MONO_FAMILY = "Consolas"
BASE_FONT_PT = 11  # ~14.7px at 100% display scaling; points (not pixels) so it follows OS DPI
SMALL_FONT_PT = 10

# Okabe-Ito colorblind-safe palette (black swapped per theme for a visible neutral, appended last).
# Yellow sits late in the order because it's the lowest-contrast entry on a light background.
OKABE_ITO = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442"]

PALETTES = {
    "light": {
        "bg": "#fafafa", "surface": "#ffffff", "fg": "#1c1c1c", "muted": "#5c5c5c",
        "border": "#e3e3e3", "select": "#dce9f8",
        "row_anomaly": "#fdecec", "row_breach": "#fbdcd9", "row_error": "#fff5db",
        "ok": "#1a7f37", "danger": "#c62828", "warn": "#d9480f", "caution": "#9a6700",
        "pending": "#8a8a8a", "glyph": "#ffffff",
        "tooltip_bg": "#ffffff", "tooltip_border": "#c8c8c8",
        "chart_bg": "#fafafa", "chart_grid": "#e6e6e6", "series_neutral": "#000000",
        "mini_bg": "#ffffff", "mini_alert_bg": "#ffe0e0", "mini_border": "#c8c8c8",
        "mini_fg": "#222222", "mini_muted": "#888888", "mini_new_error": "#7b1fa2",
        "new_error_bg": "#0b3d0b", "new_error_fg": "#ffffff",
    },
    "dark": {
        "bg": "#1c1c1c", "surface": "#232323", "fg": "#fafafa", "muted": "#a8a8a8",
        "border": "#333333", "select": "#253a55",
        "row_anomaly": "#45201f", "row_breach": "#56221f", "row_error": "#3d3216",
        "ok": "#3fb950", "danger": "#ff6b6b", "warn": "#ff922b", "caution": "#e3b341",
        "pending": "#8b8b8b", "glyph": "#111111",
        "tooltip_bg": "#2b2b2b", "tooltip_border": "#4a4a4a",
        "chart_bg": "#1c1c1c", "chart_grid": "#333333", "series_neutral": "#e0e0e0",
        "mini_bg": "#232323", "mini_alert_bg": "#4a1f22", "mini_border": "#4a4a4a",
        "mini_fg": "#eeeeee", "mini_muted": "#9a9a9a", "mini_new_error": "#ce93d8",
        "new_error_bg": "#2e7d32", "new_error_fg": "#ffffff",
    },
}

# name -> (shape, glyph, palette color key). Distinct shapes so status never relies on color alone.
ICON_SPECS = {
    "ok": ("circle", "check", "ok"),
    "anomaly": ("triangle", "bang", "danger"),
    "breach": ("diamond", "bang", "warn"),
    "error": ("square", "cross", "caution"),
    "pending": ("ring", None, "pending"),
    "conn_connected": ("circle", "check", "ok"),
    "conn_failed": ("triangle", "bang", "danger"),
    "conn_testing": ("ring", None, "caution"),
    "conn_idle": ("ring", None, "pending"),
}


def enable_dpi_awareness():
    """Lets Windows render the app at native DPI (crisp text that scales with the display
    setting) instead of bitmap-stretching it. Must run before the Tk root is created."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def text_scale_factor() -> float:
    """Windows' Accessibility > Text size setting (1.0 - 2.25), applied on top of DPI scaling."""
    if sys.platform != "win32":
        return 1.0
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Accessibility") as key:
            value, _ = winreg.QueryValueEx(key, "TextScaleFactor")
        return max(1.0, min(2.25, int(value) / 100))
    except (OSError, ValueError):
        return 1.0


def system_theme() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
        except OSError:
            pass
    return "light"


def set_dark_titlebar(window, dark: bool):
    """Matches the Windows 10/11 title bar to the theme (no-op elsewhere or on older builds)."""
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20 on 20H1+, 19 before)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
    except (AttributeError, OSError):
        pass


def _configure_fonts(root):
    scale = text_scale_factor()

    def pt(n):
        return max(1, round(n * scale))

    body, small = pt(BASE_FONT_PT), pt(SMALL_FONT_PT)
    specs = {
        "TkDefaultFont": (FONT_FAMILY, body), "TkTextFont": (FONT_FAMILY, body),
        "TkMenuFont": (FONT_FAMILY, body), "TkTooltipFont": (FONT_FAMILY, body),
        "TkHeadingFont": (FONT_FAMILY_STRONG, body), "TkCaptionFont": (FONT_FAMILY_STRONG, body),
        "TkSmallCaptionFont": (FONT_FAMILY, small), "TkIconFont": (FONT_FAMILY, body),
        "TkFixedFont": (MONO_FAMILY, small),
        # sv-ttk defines these in pixels, which would ignore OS scaling; redefine in points.
        "SunValleyCaptionFont": (FONT_FAMILY, small),
        "SunValleyBodyFont": (FONT_FAMILY, body),
        "SunValleyBodyStrongFont": (FONT_FAMILY_STRONG, body),
        "SunValleyBodyLargeFont": (FONT_FAMILY, pt(13)),
        "SunValleySubtitleFont": (FONT_FAMILY_STRONG, pt(14)),
    }
    existing = set(tkfont.names(root))
    for name, (family, size) in specs.items():
        if name in existing:
            tkfont.Font(root=root, name=name, exists=True).configure(family=family, size=size)
        else:
            tkfont.Font(root=root, name=name, family=family, size=size)


def apply_theme(root, name: str, ui_scale: float) -> dict:
    """Switches the whole app to the light or dark theme and returns its palette."""
    pal = PALETTES[name]
    style = ttk.Style(root)
    if sv_ttk is not None:
        sv_ttk.set_theme(name, root)
    else:
        style.theme_use("clam")
        style.configure(".", background=pal["bg"], foreground=pal["fg"], fieldbackground=pal["surface"])
        root.tk_setPalette(background=pal["bg"], foreground=pal["fg"])
    _configure_fonts(root)  # after set_theme: sv-ttk creates its fonts on first load

    def px(n):
        return int(round(n * ui_scale))

    linespace = tkfont.nametofont("TkDefaultFont", root=root).metrics("linespace")
    style.configure("Treeview", background=pal["surface"], fieldbackground=pal["surface"],
                    foreground=pal["fg"], rowheight=linespace + px(16), font="TkDefaultFont")
    style.map("Treeview", background=[("selected", pal["select"])], foreground=[("selected", pal["fg"])])
    style.configure("Heading", font="SunValleyBodyStrongFont", padding=(px(10), px(8)))
    style.configure("Treeview.Heading", font="SunValleyBodyStrongFont", padding=(px(10), px(8)))
    for btn in ("TButton", "Accent.TButton"):
        style.configure(btn, padding=(px(12), px(6), px(12), px(7)))
    style.configure("TNotebook.Tab", padding=(px(16), px(8)))
    style.configure("Muted.TLabel", foreground=pal["muted"])
    style.configure("Strong.TLabel", font="SunValleyBodyStrongFont")
    style.configure("TLabelframe.Label", font="SunValleyBodyStrongFont")
    set_dark_titlebar(root, name == "dark")
    return pal


def series_colors(pal) -> list:
    return OKABE_ITO + [pal["series_neutral"]]


# ---------- status icons (rasterized here so they get real alpha with no extra dependency) ----------
def _seg_dist2(px_, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px_ - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    cx, cy = ax + t * dx - px_, ay + t * dy - py
    return cx * cx + cy * cy


def _in_shape(shape, x, y):
    if shape == "circle":
        return (x - 0.5) ** 2 + (y - 0.5) ** 2 <= 0.47 ** 2
    if shape == "ring":
        r2 = (x - 0.5) ** 2 + (y - 0.5) ** 2
        return 0.33 ** 2 <= r2 <= 0.46 ** 2
    if shape == "diamond":
        return abs(x - 0.5) + abs(y - 0.5) <= 0.5
    if shape == "square":
        return 0.08 <= x <= 0.92 and 0.08 <= y <= 0.92
    if shape == "triangle":  # apex up; inside if within both slanted edges and above the base
        return y <= 0.94 and abs(x - 0.5) <= (y - 0.04) * 0.52
    return False


def _in_glyph(glyph, shape, x, y):
    if glyph == "check":
        w2 = 0.085 ** 2
        return (_seg_dist2(x, y, 0.27, 0.52, 0.43, 0.68) <= w2
                or _seg_dist2(x, y, 0.43, 0.68, 0.74, 0.35) <= w2)
    if glyph == "bang":
        top, bottom, dot = (0.36, 0.68, 0.81) if shape == "triangle" else (0.24, 0.6, 0.75)
        return ((abs(x - 0.5) <= 0.065 and top <= y <= bottom)
                or (x - 0.5) ** 2 + (y - dot) ** 2 <= 0.07 ** 2)
    if glyph == "cross":
        w2 = 0.075 ** 2
        return (_seg_dist2(x, y, 0.3, 0.3, 0.7, 0.7) <= w2
                or _seg_dist2(x, y, 0.7, 0.3, 0.3, 0.7) <= w2)
    return False


def _hex_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def _png(width, height, rows):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_icon(master, name, pal, size) -> tk.PhotoImage:
    shape, glyph, color_key = ICON_SPECS[name]
    fill, mark = _hex_rgb(pal[color_key]), _hex_rgb(pal["glyph"])
    ss = 4  # supersamples per axis, for anti-aliased edges
    rows = []
    for py in range(size):
        row = []
        for px_ in range(size):
            r = g = b = a = 0
            for sy in range(ss):
                for sx in range(ss):
                    x, y = (px_ + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size
                    if not _in_shape(shape, x, y):
                        continue
                    c = mark if glyph and _in_glyph(glyph, shape, x, y) else fill
                    r, g, b, a = r + c[0], g + c[1], b + c[2], a + 1
            if a:
                row += [r // a, g // a, b // a, 255 * a // (ss * ss)]
            else:
                row += [0, 0, 0, 0]
        rows.append(row)
    data = base64.b64encode(_png(size, size, rows)).decode("ascii")
    return tk.PhotoImage(master=master, data=data, format="png")


class Tooltip:
    """Borderless popup that follows the pointer. Content is either plain text or built by
    a callback, so the same window serves cell hovers and the chart crosshair readout."""

    def __init__(self, master, get_palette, wraplength=480):
        self.master = master
        self.get_palette = get_palette
        self.wraplength = wraplength
        self.win = None
        self.body = None

    def _ensure(self):
        pal = self.get_palette()
        if self.win is None:
            self.win = tk.Toplevel(self.master)
            self.win.withdraw()
            self.win.overrideredirect(True)
            self.win.attributes("-topmost", True)
            self.body = tk.Frame(self.win)
            self.body.pack(fill="both", expand=True, padx=1, pady=1)
        self.win.config(bg=pal["tooltip_border"])
        self.body.config(bg=pal["tooltip_bg"], padx=10, pady=8)
        for child in self.body.winfo_children():
            child.destroy()
        return pal

    def show_text(self, x_root, y_root, text):
        pal = self._ensure()
        tk.Label(self.body, text=text, justify="left", anchor="w", wraplength=self.wraplength,
                 bg=pal["tooltip_bg"], fg=pal["fg"], font="TkDefaultFont").pack(anchor="w")
        self._place(x_root, y_root)

    def show_custom(self, x_root, y_root, build):
        pal = self._ensure()
        build(self.body, pal)
        self._place(x_root, y_root)

    def move(self, x_root, y_root):
        if self.win is not None and self.win.winfo_viewable():
            self._place(x_root, y_root)

    def _place(self, x_root, y_root):
        win = self.win
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x, y = x_root + 16, y_root + 18
        right = win.winfo_vrootx() + win.winfo_vrootwidth()
        bottom = win.winfo_vrooty() + win.winfo_vrootheight()
        if x + w > right:
            x = x_root - w - 12
        if y + h > bottom:
            y = y_root - h - 12
        win.geometry(f"+{x}+{y}")
        win.deiconify()
        win.lift()

    def hide(self):
        if self.win is not None:
            self.win.withdraw()
