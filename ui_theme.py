"""Shared desktop theme and small, reusable view helpers."""
from __future__ import annotations

import pathlib
import tkinter as tk
from tkinter import ttk

BG = "#0D131D"
PANEL = "#151F2D"
FIELD = "#0F1825"
BORDER = "#2A394C"
TEXT = "#EAF2FA"
MUTED = "#9CAFC5"
ACCENT = "#48B7FF"
SUCCESS = "#72DCB0"
WARNING = "#F2CB7C"
ERROR = "#FF999D"


def apply_theme(root):
    root.configure(background=BG)
    from tkinter import font as tkfont
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        tkfont.nametofont(name).configure(family="Segoe UI", size=10)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", "#245275")
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10),
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    focuscolor=ACCENT, troughcolor=FIELD)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=PANEL)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Brand.TLabel", font=("Segoe UI", 18, "bold"))
    style.configure("Heading.TLabel", font=("Segoe UI", 15, "bold"))
    style.configure("Section.TLabel", background=PANEL, font=("Segoe UI", 11, "bold"))
    style.configure("Card.TLabel", background=PANEL)
    style.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED)
    style.configure("Accent.TLabel", foreground=ACCENT)
    style.configure("Badge.TLabel", background="#183B50", foreground="#89D6FF", padding=(10, 5),
                    font=("Segoe UI", 9, "bold"))
    style.configure("TButton", background="#25364A", foreground=TEXT, borderwidth=0,
                    padding=(14, 9), focusthickness=2, focuscolor=ACCENT)
    style.map("TButton", background=[("disabled", "#192432"), ("pressed", "#37516E"), ("active", "#30465F")],
              foreground=[("disabled", "#6F839A")])
    style.configure("Primary.TButton", background=ACCENT, foreground="#061724", font=("Segoe UI", 10, "bold"))
    style.map("Primary.TButton", background=[("disabled", "#233E51"), ("pressed", "#249BDD"), ("active", "#7ACDFF")],
              foreground=[("disabled", "#829BAE")])
    style.configure("Quiet.TButton", background=BG, foreground=MUTED)
    style.map("Quiet.TButton", background=[("active", PANEL)], foreground=[("active", TEXT), ("disabled", "#60748B")])
    style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT,
                    borderwidth=1, padding=(10, 8))
    style.map("TEntry", bordercolor=[("focus", ACCENT)], fieldbackground=[("disabled", "#1A2636")],
              foreground=[("disabled", "#7A8FA7")])
    style.configure("TCombobox", fieldbackground=FIELD, background="#25364A", foreground=TEXT,
                    arrowcolor=MUTED, borderwidth=1, padding=(9, 7))
    style.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
              foreground=[("disabled", "#7A8FA7"), ("readonly", TEXT)],
              selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)],
              bordercolor=[("focus", ACCENT)])
    style.configure("TCheckbutton", background=PANEL, foreground=TEXT, padding=(0, 5),
                    indicatorbackground=FIELD, indicatormargin=(0, 0, 8, 0))
    style.map("TCheckbutton", background=[("active", PANEL)],
              indicatorbackground=[("selected", ACCENT), ("disabled", "#25364A")],
              foreground=[("disabled", "#7A8FA7")])
    style.configure("Footer.TCheckbutton", background=BG, foreground=MUTED)
    style.map("Footer.TCheckbutton", background=[("active", BG)])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 10))
    style.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(18, 11),
                    borderwidth=0, font=("Segoe UI", 10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", PANEL), ("active", "#1C2B3C")],
              foreground=[("selected", ACCENT), ("active", TEXT)])
    style.configure("TSeparator", background=BORDER)
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
                    borderwidth=0, thickness=4)
    style.configure("TScrollbar", background="#2D4055", troughcolor=BG, borderwidth=0,
                    arrowcolor=MUTED, arrowsize=12)
    style.map("TScrollbar", background=[("active", "#49637D")])
    style.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                    borderwidth=0, rowheight=34, font=("Segoe UI", 10))
    style.configure("Treeview.Heading", background="#213247", foreground=MUTED,
                    borderwidth=0, padding=(10, 10), font=("Segoe UI", 9, "bold"))
    style.map("Treeview", background=[("selected", "#214968")], foreground=[("selected", "#FFFFFF")])
    style.map("Treeview.Heading", background=[("active", "#30465F")])
    return style


def load_image(path, size):
    """Resample the supplied brand artwork for the current display density."""
    if not path:
        return None
    try:
        from PIL import Image, ImageTk
        with Image.open(path) as source:
            image = source.convert("RGBA")
            image.thumbnail(size, Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(image)
    except (ImportError, OSError, tk.TclError):
        return None


def set_icon(window, path):
    if path and pathlib.Path(path).exists():
        try:
            window.iconbitmap(default=str(path))
        except tk.TclError:
            pass


def fit_dialog(dialog, root, width, height):
    dialog.update_idletasks()
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    scale = max(1.0, float(root.tk.call("tk", "scaling")) * 72 / 96)
    width = min(round(width * scale), max(400, screen_w - 60))
    height = min(round(height * scale), max(300, screen_h - 100))
    x = max(0, min(root.winfo_x() + (root.winfo_width() - width) // 2, screen_w - width))
    y = max(0, min(root.winfo_y() + (root.winfo_height() - height) // 2, screen_h - height - 60))
    dialog.configure(background=BG)
    dialog.geometry(f"{width}x{height}+{x}+{y}")
    dialog.minsize(min(width, round(640 * scale)), min(height, round(380 * scale)))


class ScrollPage(ttk.Frame):
    """A notebook page that stays usable on small displays and supports wheel scrolling."""
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, background=BG, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.body = ttk.Frame(self.canvas, padding=(0, 4, 12, 8))
        self.body.columnconfigure(0, weight=1)
        self.window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._resize)
        self.canvas.bind("<Configure>", self._resize)
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")

    def _resize(self, _=None):
        self.canvas.itemconfigure(self.window, width=max(1, self.canvas.winfo_width()))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _wheel(self, event):
        if not self.winfo_ismapped():
            return
        target = self.winfo_containing(event.x_root, event.y_root)
        current = target
        while current is not None and current is not self:
            if isinstance(current, (ttk.Combobox, tk.Text, ttk.Treeview)):
                return
            current = current.master
        if current is self and self.body.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(-int(event.delta / 120), "units")


def card(parent, number, heading, description=None):
    outer = ttk.Frame(parent, style="Card.TFrame", padding=16)
    outer.pack(fill="x", pady=(0, 12))
    outer.columnconfigure(0, weight=1)
    heading_row = ttk.Frame(outer, style="Card.TFrame")
    heading_row.pack(fill="x", pady=(0, 10))
    ttk.Label(heading_row, text=number, style="Badge.TLabel").pack(side="left", padx=(0, 10))
    ttk.Label(heading_row, text=heading, style="Section.TLabel").pack(side="left")
    if description:
        label = ttk.Label(outer, text=description, style="CardMuted.TLabel", wraplength=700)
        label.pack(anchor="w", fill="x", pady=(0, 8))
        outer.bind("<Configure>", lambda event: label.configure(wraplength=max(200, event.width - 40)), add="+")
    body = ttk.Frame(outer, style="Card.TFrame")
    body.pack(fill="x")
    body.columnconfigure(1, weight=1)
    return body


def field(parent, row, label, variable, password=False, width=None):
    ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 16), pady=6)
    entry = ttk.Entry(parent, textvariable=variable, show="•" if password else "", width=width or 24)
    entry.grid(row=row, column=1, sticky="ew", pady=6)
    return entry
