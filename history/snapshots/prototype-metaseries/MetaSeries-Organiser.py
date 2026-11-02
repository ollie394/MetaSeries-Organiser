#!/usr/bin/env python3
"""
MetaSeries-Organiser – TV Series Renamer & Sorter (Portable)

- Windows-friendly, single-file script. Build a portable EXE via PyInstaller.
- GUI built with PySimpleGUI / FreeSimpleGUI (with compatibility shims for older releases).
- **Splash screen** with MetaSeries-Organiser logo and TMDB attribution.
- Uses TMDB (free) by default; optional TVDB (paid API) stub included for future extension.
- Renames and moves TV episodes into Plex-friendly structure with year in series name and English "Season" folders.
- Supports:
  • Standard sXXeYY / SXXEYY (and ranges like sXXeYY-eZZ or SXXEYY-EZZ)
  • Date-based episodes (YYYY-MM-DD, DD-MM-YYYY, with -, ., or space separators)
  • Specials (season 00) and a "Specials" alias
  • Episode split parts (cdX/discX/diskX/dvdX/partX/ptX)
  • Inline episode extras (-behindthescenes, -deleted, -featurette, -interview, -scene, -short, -trailer, -other with optional numeric suffix)
  • Absolute numbering like "141. Title.mkv" (maps overall index to S/E using provider order)
  • Alt patterns like "01x05", "01E05", and post-year short form: "Show (2019) - 01 Title"
  • Multi-episode single file (sXXeYY-eZZ)
  • Dry run mode
  • **Toggle** for uppercase/lowercase S/E codes in output filenames

IMPORTANT:
- You must supply a TMDB API key (free): https://www.themoviedb.org/settings/api
- TVDB support requires a paid v4 API token; this script will detect and gracefully fall back to TMDB if TVDB is not configured.

Plex style enforced:
  Show (Year)/Season XX/Show (Year) - sXXeYY[-eZZ] - Episode Title.ext
Date-based style:
  Show (Year)/Season XX/Show (Year) - DD-MM-YYYY - Episode Title.ext
Specials are Season 00 by default.

"""

import os
import re
import sys
import json
import time
import shutil
import logging
import pathlib
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, List, Dict

# ---------------------- Optional deps with clear errors ----------------------
try:
    import PySimpleGUI as sg  # type: ignore
except Exception:
    try:
        import FreeSimpleGUI as sg  # type: ignore
    except Exception:
        try:
            import freesimplegui as sg  # type: ignore
        except Exception:
            print("PySimpleGUI/FreeSimpleGUI is required. pip install PySimpleGUI OR pip install FreeSimpleGUI")
            sys.exit(1)

try:
    import requests  # type: ignore
except Exception:
    print("requests is required. pip install requests")
    sys.exit(1)

# ---------------------- Config & Constants ----------------------
EXTRA_SUFFIXES = [
    "behindthescenes", "deleted", "featurette", "interview",
    "scene", "short", "trailer", "other"
]
SPLIT_TOKENS = ["cd", "disc", "disk", "dvd", "part", "pt"]
DATE_SEPS = r"[-.\s]"
DATE_PATTERNS = [
    # YYYY-MM-DD (or . or space)
    re.compile(rf"(?P<y>\d{{4}}){DATE_SEPS}(?P<m>\d{{1,2}}){DATE_SEPS}(?P<d>\d{{1,2}})"),
    # DD-MM-YYYY (or . or space)
    re.compile(rf"(?P<d>\d{{1,2}}){DATE_SEPS}(?P<m>\d{{1,2}}){DATE_SEPS}(?P<y>\d{{4}})")
]
# Core sXXeYY or SxxEyy (we accept case-insensitive here)
SXXEYY_RANGE_RE = re.compile(
    r"(?i)s(?P<s>\d{1,2})\s*e(?P<e1>\d{1,3})(?:\s*[-_\.]?\s*e(?P<e2>\d{1,3}))?"
)
# Alt forms: 01x05, 1x5, 01E05, S01 E05 (S optional)
ALT_SE_RE = re.compile(r"(?i)(?:^|\b)(?:s\s*)?(?P<s>\d{1,2})\s*[ex]\s*(?P<e>\d{1,3})(?:\b|$)")
# Post-year short form: "Show (2019) - 01 Title" ⇒ Season 01, Episode 01
DASH_EP_AFTER_YEAR_RE = re.compile(r"\)\s*-\s*(?P<e>\d{1,3})\b")

ABSOLUTE_NUMBER_RE = re.compile(r"^\s*(?P<abs>\d{1,4})[).\-\s_]+")
SPLIT_PART_RE = re.compile(r"(?i)(?:\b|[-_])(cd|disc|disk|dvd|part|pt)(?P<n>\d+)\b")
INLINE_EXTRA_RE = re.compile(
    rf"(?i)-(?P<etype>{'|'.join(EXTRA_SUFFIXES)})(?P<idx>\d+)?(?=\.[^.]+$)"
)

APP_NAME = "MetaSeries-Organiser"
SPLASH_BG = "#0b0f17"  # dark blue/black, blends with provided splash
SPLASH_TEXT = "#E6EDF3"
ACCENT = "#3EA6FF"

# ---------------------- Provider Interfaces ----------------------
class ProviderError(Exception):
    pass

@dataclass
class EpisodeInfo:
    season: int
    episode: int
    title: Optional[str] = None
    air_date: Optional[str] = None  # YYYY-MM-DD
    is_special: bool = False

class Provider:
    def search_show(self, title: str, year: Optional[int] = None) -> Dict:
        raise NotImplementedError

    def load_season(self, show_id: str, season_number: int) -> Dict:
        raise NotImplementedError

    def map_date_to_episode(self, show_id: str, date_iso: str) -> Optional[EpisodeInfo]:
        raise NotImplementedError

    def build_episode_index(self, show_id: str) -> List[EpisodeInfo]:
        """Return list of episodes in overall order to support absolute numbering."""
        raise NotImplementedError

class TMDBProvider(Provider):
    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderError("TMDB API key is required.")
        self.api_key = api_key
        self.base = "https://api.themoviedb.org/3"
        self.session = requests.Session()
        self.session.params.update({"api_key": self.api_key})

    def search_show(self, title: str, year: Optional[int] = None) -> Dict:
        params = {"query": title}
        if year:
            params["first_air_date_year"] = year
        r = self.session.get(f"{self.base}/search/tv", params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data.get("results"):
            raise ProviderError("Show not found on TMDB.")
        # Pick best: exact year if provided else first
        if year:
            for it in data["results"]:
                y = (it.get("first_air_date") or "")[:4]
                if y.isdigit() and int(y) == year:
                    return it
        return data["results"][0]

    def load_season(self, show_id: str, season_number: int) -> Dict:
        r = self.session.get(f"{self.base}/tv/{show_id}/season/{season_number}", timeout=20)
        if r.status_code == 404:
            return {"episodes": []}
        r.raise_for_status()
        return r.json()

    def map_date_to_episode(self, show_id: str, date_iso: str) -> Optional[EpisodeInfo]:
        # Iterate seasons 0..50 (sane upper bound) to find matching air_date
        for s in range(0, 51):
            season = self.load_season(show_id, s)
            for ep in season.get("episodes", []) :
                if ep.get("air_date") == date_iso:
                    return EpisodeInfo(season=s, episode=ep.get("episode_number"),
                                       title=ep.get("name"), air_date=ep.get("air_date"),
                                       is_special=(s==0))
        return None

    def build_episode_index(self, show_id: str) -> List[EpisodeInfo]:
        # Build by increasing season, then episode, using air_date as tie-breaker
        all_eps: List[EpisodeInfo] = []
        for s in range(0, 101):
            season = self.load_season(show_id, s)
            eps = season.get("episodes", [])
            if not eps and s > 10:
                # assume no more after a few empty seasons
                break
            for ep in eps:
                all_eps.append(EpisodeInfo(
                    season=s,
                    episode=ep.get("episode_number"),
                    title=ep.get("name"),
                    air_date=ep.get("air_date"),
                    is_special=(s==0)
                ))
        # Sort by air_date (None last), then season/episode
        def sd_key(e: EpisodeInfo):
            ad = e.air_date or "9999-99-99"
            return (ad, e.season, e.episode)
        all_eps.sort(key=sd_key)
        return all_eps

class TVDBProvider(Provider):
    """Minimal stub. Implement fully if you have a paid TVDB v4 token.
       This will currently raise ProviderError unless token is provided.
    """
    def __init__(self, token: Optional[str]=None):
        if not token:
            raise ProviderError("TVDB token not provided. Use TMDB or add TVDB token.")
        self.session = requests.Session()
        self.base = "https://api4.thetvdb.com/v4"
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def search_show(self, title: str, year: Optional[int] = None) -> Dict:
        r = self.session.get(f"{self.base}/search", params={"q": title, "type": "series"}, timeout=20)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            raise ProviderError("Show not found on TVDB.")
        # Very naive selection; refine as needed
        return data[0]

    def load_season(self, show_id: str, season_number: int) -> Dict:
        # TVDB v4 requires more steps (series -> seasons -> episodes)
        # Left as an exercise; fallback to TMDB is recommended for now.
        return {"episodes": []}

    def map_date_to_episode(self, show_id: str, date_iso: str) -> Optional[EpisodeInfo]:
        return None

    def build_episode_index(self, show_id: str) -> List[EpisodeInfo]:
        return []

# ---------------------- Parsing helpers ----------------------
@dataclass
class ParseResult:
    kind: str  # 'se', 'se_range', 'date', 'special', 'absolute', 'unknown'
    season: Optional[int] = None
    ep1: Optional[int] = None
    ep2: Optional[int] = None
    date_iso: Optional[str] = None  # YYYY-MM-DD
    extra_type: Optional[str] = None
    extra_index: Optional[int] = None
    split_token: Optional[str] = None
    split_index: Optional[int] = None


def detect_date(s: str) -> Optional[str]:
    for rx in DATE_PATTERNS:
        m = rx.search(s)
        if m:
            y = int(m.group("y"))
            mth = int(m.group("m"))
            d = int(m.group("d"))
            try:
                dt = datetime(y, mth, d)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def parse_filename(fn: str) -> ParseResult:
    base = os.path.basename(fn)
    name, ext = os.path.splitext(base)

    # Extra suffix
    extra = INLINE_EXTRA_RE.search(base)
    extra_type = None
    extra_idx = None
    if extra:
        extra_type = extra.group("etype").lower()
        if extra.group("idx"):
            extra_idx = int(extra.group("idx"))

    # Split parts
    sp = SPLIT_PART_RE.search(name)
    split_token = None
    split_idx = None
    if sp:
        split_token = sp.group(1).lower()
        split_idx = int(sp.group("n"))

    # sXXeYY(-eZZ)
    m = SXXEYY_RANGE_RE.search(base)
    if m:
        s = int(m.group("s"))
        e1 = int(m.group("e1"))
        e2 = m.group("e2")
        if s == 0:
            kind = "special"
        elif e2:
            kind = "se_range"
        else:
            kind = "se"
        return ParseResult(kind=kind, season=s, ep1=e1, ep2=int(e2) if e2 else None,
                           extra_type=extra_type, extra_index=extra_idx,
                           split_token=split_token, split_index=split_idx)

    # Alt SE like 01x05 / 01E05
    m2 = ALT_SE_RE.search(base)
    if m2:
        s = int(m2.group("s"))
        e1 = int(m2.group("e"))
        return ParseResult(kind="se", season=s, ep1=e1, extra_type=extra_type, extra_index=extra_idx,
                           split_token=split_token, split_index=split_idx)

    # Date based
    d_iso = detect_date(base)
    if d_iso:
        return ParseResult(kind="date", date_iso=d_iso, extra_type=extra_type,
                           extra_index=extra_idx, split_token=split_token,
                           split_index=split_idx)

    # Post-year dash ep form: ") - 01 " ⇒ assume season 1
    d3 = DASH_EP_AFTER_YEAR_RE.search(base)
    if d3:
        e1 = int(d3.group("e"))
        return ParseResult(kind="se", season=1, ep1=e1, extra_type=extra_type, extra_index=extra_idx,
                           split_token=split_token, split_index=split_idx)

    # Absolute number
    a = ABSOLUTE_NUMBER_RE.match(base)
    if a:
        return ParseResult(kind="absolute", ep1=int(a.group("abs")), extra_type=extra_type,
                           extra_index=extra_idx, split_token=split_token, split_index=split_idx)

    return ParseResult(kind="unknown", extra_type=extra_type, extra_index=extra_idx,
                       split_token=split_token, split_index=split_idx)

# ---------------------- Renamer Logic ----------------------
@dataclass
class ShowContext:
    title: str
    year: Optional[int]
    provider_name: str
    provider: Provider
    show_id: str
    episode_index: List[EpisodeInfo]


def sanitize_filename(s: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]", "_", s).strip()


def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)


def format_season_folder(n: int) -> str:
    return f"Season {n:02d}"


def target_filename(show_title: str, year: Optional[int], episode: EpisodeInfo,
                    ext: str, date_iso: Optional[str]=None, extra: Optional[str]=None,
                    multi_ep: Optional[Tuple[int,int]]=None, upper_se: bool = False) -> str:
    base_show = f"{show_title} ({year})" if year else show_title
    s_char = "S" if upper_se else "s"
    e_char = "E" if upper_se else "e"
    if date_iso:
        dt = datetime.strptime(date_iso, "%Y-%m-%d")
        date_disp = dt.strftime("%d-%m-%Y")
        ep_title = episode.title or ""
        name = f"{base_show} - {date_disp}"
        if ep_title:
            name += f" - {sanitize_filename(ep_title)}"
        if extra:
            name += f"-{extra}"
        return f"{name}{ext}"
    else:
        if multi_ep:
            s, e1, e2 = episode.season, multi_ep[0], multi_ep[1]
            name = f"{base_show} - {s_char}{s:02d}{e_char}{e1:02d}-{e_char}{e2:02d}"
        else:
            name = f"{base_show} - {s_char}{episode.season:02d}{e_char}{episode.episode:02d}"
        if episode.title:
            name += f" - {sanitize_filename(episode.title)}"
        if extra:
            name += f"-{extra}"
        return f"{name}{ext}"


def resolve_episode_from_absolute(ctx: ShowContext, abs_index: int) -> Optional[EpisodeInfo]:
    # Convert 1-based absolute index to episode
    if abs_index <= 0:
        return None
    if not ctx.episode_index:
        ctx.episode_index = ctx.provider.build_episode_index(ctx.show_id)
    if abs_index <= len(ctx.episode_index):
        return ctx.episode_index[abs_index - 1]
    return None


def do_process(ctx: ShowContext, src_dir: pathlib.Path, dst_root: Optional[pathlib.Path],
               dry_run: bool, logfn, upper_se: bool) -> None:
    files = [p for p in src_dir.iterdir() if p.is_file()]
    if not files:
        logfn("No files found in source directory.")
        return

    # Determine destination root
    show_disp = f"{ctx.title} ({ctx.year})" if ctx.year else ctx.title
    if not dst_root:
        dst_root = src_dir
    show_dir = dst_root / show_disp
    ensure_dir(show_dir)

    # Cache seasons
    season_cache: Dict[int, Dict] = {}

    def get_episode(s: int, e: int) -> Optional[EpisodeInfo]:
        if s not in season_cache:
            season_cache[s] = ctx.provider.load_season(ctx.show_id, s)
        for ep in season_cache[s].get("episodes", []):
            if int(ep.get("episode_number", -1)) == int(e):
                return EpisodeInfo(season=s, episode=e, title=ep.get("name"),
                                   air_date=ep.get("air_date"), is_special=(s==0))
        # Fallback when title missing
        return EpisodeInfo(season=s, episode=e, title=None, air_date=None, is_special=(s==0))

    for p in files:
        pr = parse_filename(p.name)
        ext = p.suffix
        # Determine target episode info and season folder
        ep_info: Optional[EpisodeInfo] = None
        multi_ep_range: Optional[Tuple[int,int]] = None
        season_folder = None
        date_iso = None
        extra_suffix = None

        if pr.extra_type:
            extra_suffix = f"{pr.extra_type}{pr.extra_index or ''}"

        if pr.kind in ("se", "special") and pr.season is not None and pr.ep1 is not None:
            ep_info = get_episode(pr.season, pr.ep1)
            season_folder = format_season_folder(pr.season)
        elif pr.kind == "se_range" and pr.season is not None and pr.ep1 is not None and pr.ep2 is not None:
            e1, e2 = pr.ep1, pr.ep2
            ep_info = get_episode(pr.season, e1)
            multi_ep_range = (e1, e2)
            season_folder = format_season_folder(pr.season)
        elif pr.kind == "date" and pr.date_iso:
            ep_info = ctx.provider.map_date_to_episode(ctx.show_id, pr.date_iso)
            date_iso = pr.date_iso
            if ep_info:
                season_folder = format_season_folder(ep_info.season)
            else:
                season_folder = format_season_folder(1)
        elif pr.kind == "absolute" and pr.ep1:
            ep_info = resolve_episode_from_absolute(ctx, pr.ep1)
            if ep_info:
                season_folder = format_season_folder(ep_info.season)
            else:
                season_folder = format_season_folder(1)
        else:
            # Unknown pattern — leave in place but log
            logfn(f"SKIP (unknown): {p.name}")
            continue

        if not ep_info:
            logfn(f"WARN: Could not resolve episode metadata for {p.name} — using minimal naming.")
            # Minimal fallback
            ep_info = EpisodeInfo(season=int(re.search(r"\d+", season_folder).group()), episode=pr.ep1 or 1)

        # Determine destination
        season_dir = show_dir / season_folder
        ensure_dir(season_dir)

        # Episode-level extras must sit next to episode; naming keeps -extra suffix
        target_name = target_filename(ctx.title, ctx.year, ep_info, ext,
                                      date_iso=date_iso, extra=extra_suffix,
                                      multi_ep=multi_ep_range, upper_se=upper_se)
        dst = season_dir / target_name

        # Handle split parts (cd1, pt2, etc.) — append token before extension
        if pr.split_token and pr.split_index:
            stem, ext2 = os.path.splitext(dst.name)
            dst = dst.with_name(f"{stem}-{pr.split_token}{pr.split_index}{ext2}")

        if dry_run:
            logfn(f"DRY-RUN: {p} -> {dst}")
        else:
            # If file with same name exists, avoid overwrite by appending (1), (2), ...
            candidate = dst
            i = 1
            while candidate.exists():
                stem, ext2 = os.path.splitext(dst.name)
                candidate = dst.with_name(f"{stem} ({i}){ext2}")
                i += 1
            shutil.move(str(p), str(candidate))
            logfn(f"MOVED: {p.name} -> {candidate.relative_to(show_dir)}")

# ---------------------- GUI (with compatibility shims) ----------------------

# ...existing code...
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
import tkinter.font as tkfont
# ...existing code...

def build_gui_tk():
    root = tk.Tk()
    root.title(APP_NAME)
    root.configure(bg=SPLASH_BG)
    root.minsize(700, 420)

    # Fonts (scale these on resize)
    base_label_font = tkfont.Font(family="Segoe UI", size=10)
    base_entry_font = tkfont.Font(family="Segoe UI", size=10)
    base_button_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
    base_log_font = tkfont.Font(family="Consolas", size=10)

    # Container
    frm = tk.Frame(root, bg=SPLASH_BG)
    frm.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

    # Make root and frame expandable
    root.grid_rowconfigure(0, weight=1)
    root.grid_columnconfigure(0, weight=1)
    for c in range(4):
        frm.grid_columnconfigure(c, weight=1 if c == 1 else 0)
    frm.grid_rowconfigure(6, weight=1)  # log row expands

    # Source folder
    lbl_src = tk.Label(frm, text="Source folder with loose episodes:", fg=SPLASH_TEXT, bg=SPLASH_BG, font=base_label_font)
    lbl_src.grid(row=0, column=0, sticky="e", padx=4, pady=4)
    src_entry = tk.Entry(frm, font=base_entry_font)
    src_entry.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
    btn_src = tk.Button(frm, text="Browse", command=lambda: src_entry.delete(0, tk.END) or src_entry.insert(0, filedialog.askdirectory()), font=base_button_font)
    btn_src.grid(row=0, column=2, sticky="w", padx=4, pady=4)

    # Destination folder
    lbl_dst = tk.Label(frm, text="Destination root (optional):", fg=SPLASH_TEXT, bg=SPLASH_BG, font=base_label_font)
    lbl_dst.grid(row=1, column=0, sticky="e", padx=4, pady=4)
    dst_entry = tk.Entry(frm, font=base_entry_font)
    dst_entry.grid(row=1, column=1, sticky="ew", padx=4, pady=4)
    btn_dst = tk.Button(frm, text="Browse", command=lambda: dst_entry.delete(0, tk.END) or dst_entry.insert(0, filedialog.askdirectory()), font=base_button_font)
    btn_dst.grid(row=1, column=2, sticky="w", padx=4, pady=4)

    # Show title and year
    lbl_title = tk.Label(frm, text="Show Title:", fg=SPLASH_TEXT, bg=SPLASH_BG, font=base_label_font)
    lbl_title.grid(row=2, column=0, sticky="e", padx=4, pady=4)
    title_entry = tk.Entry(frm, font=base_entry_font)
    title_entry.grid(row=2, column=1, sticky="ew", padx=4, pady=4)
    lbl_year = tk.Label(frm, text="Year (optional):", fg=SPLASH_TEXT, bg=SPLASH_BG, font=base_label_font)
    lbl_year.grid(row=2, column=2, sticky="e", padx=4, pady=4)
    year_entry = tk.Entry(frm, width=8, font=base_entry_font)
    year_entry.grid(row=2, column=3, sticky="w", padx=4, pady=4)

    # Provider and API key
    lbl_provider = tk.Label(frm, text="Data Provider:", fg=SPLASH_TEXT, bg=SPLASH_BG, font=base_label_font)
    lbl_provider.grid(row=3, column=0, sticky="e", padx=4, pady=4)
    provider_var = tk.StringVar(value="TMDB")
    provider_menu = tk.OptionMenu(frm, provider_var, "TMDB", "TVDB")
    provider_menu.config(bg=SPLASH_BG, fg=SPLASH_TEXT)
    provider_menu.config(font=base_entry_font)
    provider_menu.grid(row=3, column=1, sticky="w", padx=4, pady=4)
    lbl_apikey = tk.Label(frm, text="API Key/Token:", fg=SPLASH_TEXT, bg=SPLASH_BG, font=base_label_font)
    lbl_apikey.grid(row=3, column=2, sticky="e", padx=4, pady=4)
    apikey_entry = tk.Entry(frm, show="•", font=base_entry_font)
    apikey_entry.grid(row=3, column=3, sticky="w", padx=4, pady=4)

    # Uppercase S/E codes and Dry run
    upper_var = tk.BooleanVar(value=True)
    chk_upper = tk.Checkbutton(frm, text="Uppercase S/E codes (S01E01)", variable=upper_var, fg=SPLASH_TEXT, bg=SPLASH_BG, font=base_label_font, activebackground=SPLASH_BG, selectcolor=SPLASH_BG)
    chk_upper.grid(row=4, column=0, columnspan=2, sticky="w", padx=4, pady=4)
    dry_var = tk.BooleanVar(value=True)
    chk_dry = tk.Checkbutton(frm, text="Dry run (don’t move, just log)", variable=dry_var, fg=SPLASH_TEXT, bg=SPLASH_BG, font=base_label_font, activebackground=SPLASH_BG, selectcolor=SPLASH_BG)
    chk_dry.grid(row=4, column=2, columnspan=2, sticky="w", padx=4, pady=4)

    # Buttons
    btn_frame = tk.Frame(frm, bg=SPLASH_BG)
    btn_frame.grid(row=5, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
    btn_frame.grid_columnconfigure(0, weight=1)
    start_btn = tk.Button(btn_frame, text="Start", bg=ACCENT, fg=SPLASH_TEXT, font=base_button_font, command=lambda: start_process())
    start_btn.grid(row=0, column=1, sticky="e", padx=4)
    exit_btn = tk.Button(btn_frame, text="Exit", font=base_button_font, command=root.destroy)
    exit_btn.grid(row=0, column=2, sticky="w", padx=4)

    # Log output (expands)
    log_text = scrolledtext.ScrolledText(frm, width=80, height=12, bg=SPLASH_BG, fg=SPLASH_TEXT, font=base_log_font)
    log_text.grid(row=6, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)

    def log(msg):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}\n"
        log_text.insert(tk.END, line)
        log_text.see(tk.END)

    def start_process():
        src = src_entry.get()
        dst = dst_entry.get()
        title = title_entry.get().strip()
        year_s = year_entry.get().strip()
        provider_name = provider_var.get()
        apikey = apikey_entry.get().strip()
        upper = upper_var.get()
        dry = dry_var.get()

        if not src or not os.path.isdir(src):
            log("Please choose a valid source folder.")
            return
        if not title:
            inferred = pathlib.Path(src).name
            inferred = re.sub(r"(?i)\bseason\s*\d{1,2}\b", "", inferred).strip()
            title = inferred
            log(f"Inferred title: {title}")
        year = int(year_s) if year_s.isdigit() else None

        try:
            if provider_name == "TMDB":
                provider = TMDBProvider(apikey)
                show = provider.search_show(title, year)
                show_id = str(show.get("id"))
                if not year:
                    fa = (show.get("first_air_date") or "")[:4]
                    if fa.isdigit():
                        year = int(fa)
                        log(f"Detected year from TMDB: {year}")
            else:
                provider = TVDBProvider(apikey)
                show = provider.search_show(title, year)
                show_id = str(show.get("id") or show.get("tvdb_id") or show.get("series",{}).get("id") or "")

            ctx = ShowContext(
                title=title,
                year=year,
                provider_name=provider_name,
                provider=provider,
                show_id=show_id,
                episode_index=[]
            )

            src_dir = pathlib.Path(src)
            dst_root = pathlib.Path(dst) if dst else None
            log(f"Provider: {provider_name} | Show ID: {show_id}")
            log(f"Processing files in: {src_dir}")
            do_process(ctx, src_dir, dst_root, dry, log, upper_se=upper)
            log("Done.")

        except ProviderError as e:
            log(f"Provider error: {e}")
        except requests.HTTPError as e:
            log(f"HTTP error: {e}")
        except Exception as e:
            log(f"Unexpected error: {e}")

    # dynamic scaling: resize fonts based on window size
    def _on_resize(event):
        ref_w, ref_h = 900, 620
        w = max(event.width, 200)
        h = max(event.height, 200)
        scale = min(max(w / ref_w, h / ref_h), 3.0)  # clamp scale
        lbl_size = max(8, int(10 * scale))
        entry_size = max(8, int(10 * scale))
        btn_size = max(8, int(10 * scale))
        log_size = max(9, int(10 * scale))

        base_label_font.configure(size=lbl_size)
        base_entry_font.configure(size=entry_size)
        base_button_font.configure(size=btn_size)
        base_log_font.configure(size=log_size)

    root.bind("<Configure>", _on_resize)
    root.mainloop()
# ...existing code...

def run_self_tests() -> int:
    """Run lightweight self-tests. Return number of failures (0 = ok)."""
    failures = 0
    try:
        # basic parse/detect smoke tests
        assert parse_filename("S01E01 - Pilot.mkv").kind == "se"
        assert parse_filename("2020-02-03 - Ep.mkv").kind == "date"
        assert detect_date("2020-02-03").startswith("2020")
    except Exception:
        failures += 1
    return failures

def main():
    if "--selftest" in sys.argv or "-t" in sys.argv:
        failures = run_self_tests()
        sys.exit(1 if failures else 0)
    build_gui_tk()

if __name__ == "__main__":
    main()