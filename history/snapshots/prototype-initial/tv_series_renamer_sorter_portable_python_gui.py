#!/usr/bin/env python3
"""
MetaSeries-Organiser – TV Series Renamer & Sorter (Portable)

- Windows-friendly, single-file script. Build a portable EXE via PyInstaller.
- GUI built with PySimpleGUI (with compatibility shims for older releases).
- **Splash screen** with MetaSeries-Organiser logo and TMDB attribution.
- Uses TMDB (free) by default; optional TVDB (paid API) stub included for future extension.
- Renames and moves TV episodes into Plex-friendly structure with year in series name and English "Season" folders.
- Supports:
  • Standard sXXeYY (and sXXeYY-eZZ) patterns
  • Date-based episodes (YYYY-MM-DD, DD-MM-YYYY, with -, ., or space separators)
  • Specials (season 00) and a "Specials" alias
  • Episode split parts (cdX/discX/diskX/dvdX/partX/ptX)
  • Inline episode extras (-behindthescenes, -deleted, -featurette, -interview, -scene, -short, -trailer, -other with optional numeric suffix)
  • Absolute numbering like "141. Title.mkv" (maps overall index to S/E using provider order)
  • Multi-episode single file (sXXeYY-eZZ)
  • Dry run mode

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
    print("PySimpleGUI is required. pip install PySimpleGUI")
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
SXXEYY_RANGE_RE = re.compile(
    r"(?i)s(?P<s>\d{1,2})e(?P<e1>\d{1,3})(?:\s*[-_\.]?\s*e(?P<e2>\d{1,3}))?"
)
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

    # Date based
    d_iso = detect_date(base)
    if d_iso:
        return ParseResult(kind="date", date_iso=d_iso, extra_type=extra_type,
                           extra_index=extra_idx, split_token=split_token,
                           split_index=split_idx)

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
                    multi_ep: Optional[Tuple[int,int]]=None) -> str:
    base_show = f"{show_title} ({year})" if year else show_title
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
            name = f"{base_show} - s{s:02d}e{e1:02d}-e{e2:02d}"
        else:
            name = f"{base_show} - s{episode.season:02d}e{episode.episode:02d}"
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
               dry_run: bool, logfn) -> None:
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
                                      multi_ep=multi_ep_range)
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

def _set_theme_safe(theme_name: str = "SystemDefault") -> None:
    """Apply a theme in a way that works across old/new PySimpleGUI versions.
    - Newer: sg.theme("SystemDefault")
    - Older: sg.ChangeLookAndFeel("SystemDefault")
    Silently ignore if neither API exists.
    """
    try:
        if hasattr(sg, "theme"):
            try:
                sg.theme(theme_name)
                return
            except Exception:
                pass
        if hasattr(sg, "ChangeLookAndFeel"):
            try:
                sg.ChangeLookAndFeel(theme_name)
                return
            except Exception:
                pass
    except Exception:
        pass


def _separator_widget():
    # Older PSG didn't have HSeparator
    return sg.HSeparator() if hasattr(sg, "HSeparator") else sg.Text("-" * 80, background_color=SPLASH_BG)


def _multiline_log(**kwargs):
    """Create a Multiline that works on older PSG builds.
    Avoids newer-only args like write_only by default.
    """
    supported = {
        "key": kwargs.get("key", "log"),
        "size": kwargs.get("size", (100, 20)),
        "autoscroll": kwargs.get("autoscroll", True),
        "disabled": kwargs.get("disabled", False),
        "reroute_stdout": kwargs.get("reroute_stdout", False),
        "reroute_stderr": kwargs.get("reroute_stderr", False),
        "background_color": kwargs.get("background_color", SPLASH_BG),
        "text_color": kwargs.get("text_color", SPLASH_TEXT),
    }
    return sg.Multiline(**supported)

# ---------- Asset helpers (PyInstaller-friendly) ----------

def resource_root() -> pathlib.Path:
    """Return a base path for bundled or source runs without using __file__.
    Order: PyInstaller _MEIPASS → argv[0] folder → CWD.
    """
    # PyInstaller bundle root
    if hasattr(sys, "_MEIPASS"):
        try:
            return pathlib.Path(getattr(sys, "_MEIPASS")).resolve()
        except Exception:
            pass
    # Script path from argv[0]
    try:
        a0 = sys.argv[0]
        if a0:
            p = pathlib.Path(a0).resolve().parent
            if p.exists():
                return p
    except Exception:
        pass
    # Fallback to working directory
    return pathlib.Path(os.getcwd()).resolve()


def find_asset(*names: str) -> Optional[str]:
    roots = [resource_root(), resource_root() / "assets"]
    for r in roots:
        for n in names:
            p = r / n
            if p.exists():
                return str(p)
        # Try partial match (e.g., blue_square_2-<hash>.svg)
        for p in r.glob("*blue_square_2*"):
            if p.suffix.lower() in (".png", ".svg"):
                return str(p)
    return None


def ensure_png_from_svg(svg_path: str, out_size: int = 128) -> Optional[str]:
    """If cairosvg is available, convert svg->png and return png path; else None."""
    try:
        import cairosvg  # type: ignore
        out = os.path.splitext(svg_path)[0] + ".png"
        cairosvg.svg2png(url=svg_path, write_to=out, output_width=out_size)
        return out
    except Exception:
        return None


# ---------- Splash Screen ----------

def show_splash(skip: bool = False):
    if skip:
        return
    _set_theme_safe("SystemDefault")

    meta_logo = find_asset("MetaSeries Logo.png", "MetaSeriesLogo.png", "metaseries_logo.png")
    blue_logo = find_asset("blue_square_2.png", "blue_square_2.svg")
    if blue_logo and blue_logo.lower().endswith(".svg"):
        png = ensure_png_from_svg(blue_logo, out_size=96)
        blue_logo = png or None

    # Build a compact, dark splash that matches the provided theme
    col = [
        [
            sg.Image(meta_logo, subsample=2, pad=((0,0),(10,5)), background_color=SPLASH_BG) if meta_logo else sg.Text(APP_NAME, font=("Segoe UI", 20, "bold"), text_color=SPLASH_TEXT, background_color=SPLASH_BG)
        ],
        [
            sg.Text(
                "This MetaSeries-Organiser uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by TMDB",
                size=(60, 3),
                justification="center",
                text_color=SPLASH_TEXT,
                background_color=SPLASH_BG,
                font=("Segoe UI", 9),
                pad=((10,10),(0,5)),
            )
        ],
        [
            sg.Image(blue_logo, subsample=4, background_color=SPLASH_BG, pad=(0,5)) if blue_logo else sg.Text("", background_color=SPLASH_BG)
        ],
        [sg.Button("Continue", button_color=(SPLASH_TEXT, ACCENT), border_width=0, pad=((0,0),(5,10)))]
    ]

    layout = [[sg.Column(col, background_color=SPLASH_BG, element_justification="center", expand_x=True)]]

    try:
        win = sg.Window(APP_NAME, layout, no_titlebar=False, keep_on_top=True, finalize=True, background_color=SPLASH_BG, element_justification="center")
    except TypeError:
        win = sg.Window(APP_NAME, layout, keep_on_top=True, background_color=SPLASH_BG)

    # Auto-close after 2500 ms or on button
    ev, _ = win.read(timeout=2500)
    win.close()


# ---------- Main Window ----------

def build_gui():
    _set_theme_safe("SystemDefault")

    layout = [
        [sg.Text("Source folder with loose episodes:", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="src"), sg.FolderBrowse()],
        [sg.Text("Destination root (optional):", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="dst"), sg.FolderBrowse()],
        [sg.Text("Show Title:", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="title", size=(40,1)), sg.Text("Year (optional):", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="year", size=(8,1))],
        [sg.Text("Data Provider:", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Combo(["TMDB", "TVDB"], key="provider", default_value="TMDB", readonly=True if hasattr(sg, "Combo") else False),
         sg.Text("API Key/Token:", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="apikey", password_char="•", size=(30,1))],
        [sg.Checkbox("Dry run (don’t move, just log)", key="dry", default=True, text_color=SPLASH_TEXT, background_color=SPLASH_BG)],
        [_separator_widget()],
        [sg.Button("Start", button_color=(SPLASH_TEXT, ACCENT)), sg.Button("Exit")],
        [_multiline_log(key="log", size=(100,20), autoscroll=True, disabled=False)]
    ]
    title = APP_NAME
    try:
        return sg.Window(title, layout, finalize=True, background_color=SPLASH_BG)
    except TypeError:
        # Some very old PSG versions don't support finalize kwarg
        win = sg.Window(title, layout, background_color=SPLASH_BG)
        return win


def log(window, msg):
    timestamp = time.strftime("%H:%M:%S")
    line = f"[{timestamp}] {msg}"
    elem = window["log"]
    try:
        elem.print(line)
    except Exception:
        try:
            # Fallback to update/append
            elem.update(value=line + "\n", append=True)
        except Exception:
            pass


# ---------------------- Self-tests (no GUI) ----------------------

def run_self_tests() -> int:
    """Basic parser tests to ensure core functionality stays intact."""
    failures = 0

    def expect(cond, msg):
        nonlocal failures
        if not cond:
            print("[FAIL]", msg)
            failures += 1
        else:
            print("[OK]  ", msg)

    # sXXeYY
    pr = parse_filename("Show - s01e02 - Title.mkv")
    expect(pr.kind == "se" and pr.season == 1 and pr.ep1 == 2, "parse s01e02")

    # sXXeYY-eZZ
    pr = parse_filename("Show - s02e18-e19 - Two Parter.mkv")
    expect(pr.kind == "se_range" and pr.season == 2 and pr.ep1 == 18 and pr.ep2 == 19, "parse s02e18-e19")

    # Date-based YYYY-MM-DD
    pr = parse_filename("The Colbert Report - 2011-11-15 - Guest.avi")
    expect(pr.kind == "date" and pr.date_iso == "2011-11-15", "parse date yyyy-mm-dd")

    # Date-based DD-MM-YYYY
    pr = parse_filename("Show - 15-11-2011 - Ep.avi")
    expect(pr.kind == "date" and pr.date_iso == "2011-11-15", "parse date dd-mm-yyyy")

    # Absolute numbering
    pr = parse_filename("141. Birthright, Part I.mkv")
    expect(pr.kind == "absolute" and pr.ep1 == 141, "parse absolute number")

    # Extras with index
    pr = parse_filename("Game of Thrones - S01E01 - Winter is Coming-deleted2.mkv")
    expect(pr.extra_type == "deleted" and pr.extra_index == 2, "parse inline extra with idx")

    # Split parts
    pr = parse_filename("Show - s01e01 - pt1.avi")
    expect(pr.split_token == "pt" and pr.split_index == 1, "parse split part pt1")

    # New tests: resource root & asset find fallbacks
    root = resource_root()
    expect(root.exists(), "resource_root returns an existing path")
    missing = find_asset("__definitely_not_here__.png")
    expect(missing is None, "find_asset returns None for missing file")

    print("Self-tests complete.")
    return failures


# ---------------------- Main ----------------------

def main():
    # CLI helpers: allow --selftest and avoid launching GUI in CI contexts
    if "--selftest" in sys.argv or "-t" in sys.argv:
        failures = run_self_tests()
        sys.exit(1 if failures else 0)

    # Show environment info in console (useful for debugging PSG issues)
    try:
        psg_ver = getattr(sg, "__version__", "unknown")
        print(f"[Info] PySimpleGUI version: {psg_ver}")
    except Exception:
        pass

    # Splash (skip with --nosplash)
    show_splash(skip=("--nosplash" in sys.argv))

    win = build_gui()
    while True:
        ev, values = win.read()
        if ev in (sg.WINDOW_CLOSED, "Exit"):
            break
        if ev == "Start":
            try:
                src = values.get("src") or ""
                dst = values.get("dst") or ""
                title = (values.get("title") or "").strip()
                year_s = (values.get("year") or "").strip()
                provider_name = values.get("provider")
                apikey = (values.get("apikey") or "").strip()
                dry = bool(values.get("dry"))

                if not src or not os.path.isdir(src):
                    log(win, "Please choose a valid source folder.")
                    continue
                if not title:
                    # Try to infer from folder name
                    inferred = pathlib.Path(src).name
                    # Drop common suffixes like Season XX
                    inferred = re.sub(r"(?i)\bseason\s*\d{1,2}\b", "", inferred).strip()
                    title = inferred
                    log(win, f"Inferred title: {title}")
                year = int(year_s) if year_s.isdigit() else None

                # Provider
                if provider_name == "TMDB":
                    provider = TMDBProvider(apikey)
                    # Find show
                    show = provider.search_show(title, year)
                    show_id = str(show.get("id"))
                    if not year:
                        fa = (show.get("first_air_date") or "")[:4]
                        if fa.isdigit():
                            year = int(fa)
                            log(win, f"Detected year from TMDB: {year}")
                else:
                    provider = TVDBProvider(apikey)  # may raise if token missing
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
                log(win, f"Provider: {provider_name} | Show ID: {show_id}")
                log(win, f"Processing files in: {src_dir}")
                do_process(ctx, src_dir, dst_root, dry, lambda m: log(win, m))
                log(win, "Done.")

            except ProviderError as e:
                log(win, f"Provider error: {e}")
            except requests.HTTPError as e:
                log(win, f"HTTP error: {e}")
            except Exception as e:
                log(win, f"Unexpected error: {e}")
    win.close()

if __name__ == "__main__":
    main()
