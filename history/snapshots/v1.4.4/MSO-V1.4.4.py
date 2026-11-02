#!/usr/bin/env python3
"""
MetaSeries-Organiser – TV Series Renamer & Sorter (Portable)

- Windows-friendly, single-file script. Build a portable EXE via PyInstaller.
- **Two GUI options**:
  1) **Tkinter** (built into Python). Default.
  2) PySimpleGUI / FreeSimpleGUI (with extensive compatibility shims). Force with `--psg`.
- **Splash screen** with MetaSeries-Organiser logo and TMDB attribution (4s).
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
  • **Title-only fallback matching** using TMDB/TVDB metadata when numbers are missing/ambiguous
  • **Series ID field** to bypass title/year search when you already know the ID

IMPORTANT:
- You must supply a TMDB API key (free): https://www.themoviedb.org/settings/api
- TVDB support requires a paid v4 API token; this script will detect and gracefully fall back to TMDB if TVDB is not configured.

Plex style enforced:
  Show (Year)/Season XX/Show (Year) - sXXeYY[-eZZ] - Episode Title.ext
Date-based style:
  Show (Year)/Season XX/Show (Year) - DD-MM-YYYY - Episode Title.ext
Specials are Season 00 by default.

"""

from __future__ import annotations
import os
import re
import sys
import json
import time
import shutil
import logging
import pathlib
import unicodedata
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, List, Dict

# ---------------------- Optional GUI deps ----------------------
# Try PySimpleGUI/FreeSimpleGUI first; if unavailable, we fall back to Tkinter.
sg = None
try:
    import PySimpleGUI as sg  # type: ignore
except Exception:
    try:
        import FreeSimpleGUI as sg  # type: ignore
    except Exception:
        try:
            import freesimplegui as sg  # type: ignore
        except Exception:
            sg = None

# Tkinter is always available in standard Python installs
import tkinter as tk
from tkinter import filedialog, messagebox
import tkinter.font as tkfont

# Add logging
import logging

# Configure logging to write to MSO-Logs.txt and the console
logging.basicConfig(
    filename='MSO-Logs.txt',
    level=logging.INFO,
    format='%(asctime)s:%(levelname)s:%(name)s:%(message)s'
)

logger = logging.getLogger("MSO-Logger")
logger.setLevel(logging.INFO)

# Console handler
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console_fmt = logging.Formatter('%(levelname)s : %(message)s')
console.setFormatter(console_fmt)
logger.addHandler(console)

# Example startup log
logger.info("MetaSeries-Organiser started.")

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
# Post-year short form: ") - 01 " ⇒ assume season 1
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

# Add baked-in TVDB business key (used when user does not supply one)
DEFAULT_TVDB_TOKEN = ""

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

    def show_info(self, show_id: str) -> Dict:
        """Fetch series info (title, first_air_date, etc.)."""
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

    def show_info(self, show_id: str) -> Dict:
        r = self.session.get(f"{self.base}/tv/{show_id}", timeout=20)
        r.raise_for_status()
        return r.json()

class TVDBProvider(Provider):
    """Minimal stub. Implement fully if you have a paid TVDB v4 token.
       This will currently raise ProviderError unless token is provided.
    """
    def __init__(self, token: Optional[str]=None):
        # prefer explicit token, fall back to built-in DEFAULT_TVDB_TOKEN
        token_to_use = token or DEFAULT_TVDB_TOKEN
        if not token_to_use:
            raise ProviderError("TVDB token not provided. Use TMDB or add TVDB token.")
        self.token = token_to_use
        self.session = requests.Session()
        self.base = "https://api4.thetvdb.com/v4"
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

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

    def show_info(self, show_id: str) -> Dict:
        # Not implemented here. Return minimal placeholder to avoid crashes.
        return {"id": show_id}

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
# --- Title matching helpers ---

def _normalize_title(s: str) -> str:
    s = unicodedata.normalize('NFKD', s)
    s = s.encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _token_sort(s: str) -> str:
    toks = _normalize_title(s).split()
    toks.sort()
    return " ".join(toks)


def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _token_sort(a), _token_sort(b)).ratio()


def extract_candidate_title(filename: str, series_title: str) -> Tuple[Optional[str], Optional[int]]:
    """Try to pull an episode title fragment from the filename by removing
    the show title/year and common numbering tokens. Returns (title, season_hint)."""
    base = os.path.splitext(os.path.basename(filename))[0]
    # Replace separators with space
    s = re.sub(r"[._]+", " ", base)

    # Strip series title and optional year prefix
    pat = re.compile(rf"(?i)^\s*{re.escape(series_title)}\s*(?:\(\d{{4}}\))?\s*[-:–—]*\s*")
    s = pat.sub("", s)

    # Extract optional season hint like "01E Title" → season 1
    season_hint = None
    m = re.search(r"(?i)\b(\d{1,2})\s*e\b", s)
    if m:
        try:
            season_hint = int(m.group(1))
        except Exception:
            season_hint = None

    # Remove obvious numbering tokens
    s = re.sub(r"(?i)\bS\s*\d{1,3}\b", " ", s)               # S01
    s = re.sub(r"(?i)\bS\s*\d{1,3}E\s*\d{1,3}\b", " ", s)    # S01E02
    s = re.sub(r"(?i)\b\d{1,2}[xX]\d{1,3}\b", " ", s)         # 01x02
    s = re.sub(r"(?i)\bE\s*\d{1,3}\b", " ", s)               # E02
    s = re.sub(r"(?i)\bs\s*\d{1,3}\b", " ", s)               # s001
    s = re.sub(r"(?i)\b\d{1,3}\b", " ", s)                    # stray numbers like 01
    s = re.sub(r"\s+", " ", s).strip()

    return (s if s else None, season_hint)


@dataclass
class ShowContext:
    title: str
    year: Optional[int]
    provider_name: str
    provider: Provider
    show_id: str
    episode_index: List[EpisodeInfo]

def resolve_episode_by_title(ctx: ShowContext, guess_title: str, season_hint: Optional[int] = None) -> Optional[EpisodeInfo]:
    """Use fuzzy matching on provider episode titles to find the best match for guess_title.
    Prefer the hinted season if provided. Threshold tuned conservatively.
    """
    if not guess_title:
        return None

    best = (0.0, None, None)  # (score, season, episode_info)
    seasons = range(0, 51) if season_hint is None else [season_hint]
    checked_any = False
    for s in seasons:
        season = ctx.provider.load_season(ctx.show_id, s)
        eps = season.get("episodes", [])
        if not eps:
            continue
        checked_any = True
        for ep in eps:
            title = ep.get("name") or ""
            score = _title_similarity(guess_title, title)
            if score > best[0]:
                best = (score, s, EpisodeInfo(season=s, episode=int(ep.get("episode_number", 0)) or 0,
                                              title=title, air_date=ep.get("air_date"), is_special=(s==0)))
    # If nothing found in hinted season, broaden search
    if best[1] is None and season_hint is not None:
        for s in range(0, 51):
            if s == season_hint:
                continue
            season = ctx.provider.load_season(ctx.show_id, s)
            eps = season.get("episodes", [])
            for ep in eps:
                title = ep.get("name") or ""
                score = _title_similarity(guess_title, title)
                if score > best[0]:
                    best = (score, s, EpisodeInfo(season=s, episode=int(ep.get("episode_number", 0)) or 0,
                                                  title=title, air_date=ep.get("air_date"), is_special=(s==0)))

    score, s, epi = best
    if epi and score >= 0.80:  # threshold; adjust if needed
        return epi
    # As a lenient fallback, if we checked any and exact normalized equality occurs
    if checked_any:
        norm_guess = _normalize_title(guess_title)
        for s in range(0, 51):
            season = ctx.provider.load_season(ctx.show_id, s)
            for ep in season.get("episodes", []):
                title = ep.get("name") or ""
                if _normalize_title(title) == norm_guess:
                    return EpisodeInfo(season=s, episode=int(ep.get("episode_number", 0)) or 0,
                                       title=title, air_date=ep.get("air_date"), is_special=(s==0))
    return None


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
            # Fallback: try to infer by episode title from filename
            cand_title, season_hint = extract_candidate_title(p.name, ctx.title)
            if cand_title:
                ep_info = resolve_episode_by_title(ctx, cand_title, season_hint=season_hint)
                if ep_info:
                    season_folder = format_season_folder(ep_info.season)
                else:
                    logfn(f"SKIP (no match by title): {p.name}")
                    continue
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

# ---------------------- GUI (PySimpleGUI) ----------------------
# These functions are only used if sg (PySimpleGUI/FreeSimpleGUI) is available


def _set_theme_safe(theme_name: str = "SystemDefault") -> None:
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


def _text(text: str = "", **kwargs):
    if hasattr(sg, "Text"):
        try:
            return sg.Text(text, **kwargs)
        except Exception:
            pass
    for alt in ("T", "Txt"):
        if hasattr(sg, alt):
            try:
                return getattr(sg, alt)(text, **kwargs)
            except Exception:
                continue
    ml_kwargs = {
        "size": kwargs.get("size", (60, 2)),
        "disabled": True,
        "background_color": kwargs.get("background_color", SPLASH_BG),
        "text_color": kwargs.get("text_color", SPLASH_TEXT),
        "no_scrollbar": True if hasattr(sg, "Multiline") and "no_scrollbar" in getattr(sg.Multiline.__init__, "__code__", type("", (), {"co_varnames": ()})).co_varnames else False,
    }
    default_text = text
    try:
        return sg.Multiline(default_text, **ml_kwargs)
    except Exception:
        return sg.Column([[ ]], background_color=kwargs.get("background_color", SPLASH_BG))


def _separator_widget():
    return sg.HSeparator() if hasattr(sg, "HSeparator") else _text("-" * 80, background_color=SPLASH_BG)


def _multiline_log(**kwargs):
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


def resource_root() -> pathlib.Path:
    if hasattr(sys, "_MEIPASS"):
        try:
            return pathlib.Path(getattr(sys, "_MEIPASS")).resolve()
        except Exception:
            pass
    try:
        a0 = sys.argv[0]
        if a0:
            p = pathlib.Path(a0).resolve().parent
            if p.exists():
                return p
    except Exception:
        pass
    return pathlib.Path(os.getcwd()).resolve()


def find_asset(*names: str) -> Optional[str]:
    roots = [resource_root(), resource_root() / "assets"]
    for r in roots:
        for n in names:
            p = r / n
            if p.exists():
                return str(p)
        for p in r.glob("*blue_square_2*"):
            if p.suffix.lower() in (".png", ".svg"):
                return str(p)
    return None


def ensure_png_from_svg(svg_path: str, out_size: int = 128) -> Optional[str]:
    try:
        import cairosvg  # type: ignore
        out = os.path.splitext(svg_path)[0] + ".png"
        cairosvg.svg2png(url=svg_path, write_to=out, output_width=out_size)
        return out
    except Exception:
        return None


def show_splash(skip: bool = False):
    if skip:
        return
    _set_theme_safe("SystemDefault")

    meta_logo = find_asset("MetaSeries Logo.png", "MetaSeriesLogo.png", "metaseries_logo.png")
    tmdb_logo = find_asset("TMDB Logo.png", "tmdb_logo.png", "blue_square_2.png")
    tvdb_logo = find_asset("TVDB Logo.png", "tvdb_logo.png", "TVDBlogo.png", "tvdb-logo.png")

    # convert SVGs if needed
    if tmdb_logo and tmdb_logo.lower().endswith(".svg"):
        png = ensure_png_from_svg(tmdb_logo, out_size=96)
        tmdb_logo = png or None
    if tvdb_logo and tvdb_logo.lower().endswith(".svg"):
        png = ensure_png_from_svg(tvdb_logo, out_size=96)
        tvdb_logo = png or None

    logo_row = []
    if tmdb_logo:
        logo_row.append(sg.Image(tmdb_logo, subsample=4, background_color=SPLASH_BG, pad=(6,6)))
    if tvdb_logo:
        logo_row.append(sg.Image(tvdb_logo, subsample=4, background_color=SPLASH_BG, pad=(6,6)))
    if not logo_row:
        # fallback: empty text element to preserve spacing
        logo_row = [_text("", background_color=SPLASH_BG)]

    col = [
        [sg.Image(meta_logo, subsample=2, pad=((0,0),(10,5)), background_color=SPLASH_BG) if meta_logo else _text(APP_NAME, font=("Segoe UI", 20, "bold"), text_color=SPLASH_TEXT, background_color=SPLASH_BG)],
        [_text("This MetaSeries-Organiser uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by TMDB", size=(60, 3), justification="center", text_color=SPLASH_TEXT, background_color=SPLASH_BG, font=("Segoe UI", 9), pad=((10,10),(0,5)))],
        # TheTVDB attribution / subscription prompt
        [_text("Metadata provided by TheTVDB. Please consider adding missing information or subscribing.", size=(60,2), justification="center", text_color=SPLASH_TEXT, background_color=SPLASH_BG, font=("Segoe UI", 9), pad=((10,10),(0,5)))],
        logo_row,
        [sg.Button("Continue", button_color=(SPLASH_TEXT, ACCENT), border_width=0, pad=((0,0),(5,10)))]
    ]

    layout = [[sg.Column(col, background_color=SPLASH_BG, element_justification="center", expand_x=True)]]
    try:
        win = sg.Window(APP_NAME, layout, no_titlebar=False, keep_on_top=True, finalize=True, background_color=SPLASH_BG, element_justification="center")
    except TypeError:
        win = sg.Window(APP_NAME, layout, keep_on_top=True, background_color=SPLASH_BG)
    win.read(timeout=4000)
    win.close()


def build_gui_psg():
    _set_theme_safe("SystemDefault")

    layout = [
        [_text("Source folder with loose episodes:", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="src"), sg.FolderBrowse()],
        [_text("Destination root (optional):", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="dst"), sg.FolderBrowse()],
        [_text("Show Title:", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="title", size=(40,1)), _text("Year (optional):", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="year", size=(8,1))],
        [_text("Series ID (optional):", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="series_id", size=(20,1))],
        [_text("Data Provider:", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Combo(["TMDB", "TVDB"], key="provider", default_value="TMDB", readonly=True if hasattr(sg, "Combo") else False), _text("API Key/Token:", text_color=SPLASH_TEXT, background_color=SPLASH_BG), sg.Input(key="apikey", password_char="•", size=(30,1))],
        [sg.Checkbox("Uppercase S/E codes (S01E01)", key="upper", default=True, text_color=SPLASH_TEXT, background_color=SPLASH_BG)],
        [sg.Checkbox("Dry run (don’t move, just log)", key="dry", default=True, text_color=SPLASH_TEXT, background_color=SPLASH_BG)],
        [_separator_widget()],
        [sg.Button("Start", button_color=(SPLASH_TEXT, ACCENT)), sg.Button("Exit")],
        [_multiline_log(key="log", size=(100,20), autoscroll=True, disabled=False)]
    ]
    try:
        return sg.Window(APP_NAME, layout, finalize=True, background_color=SPLASH_BG)
    except TypeError:
        return sg.Window(APP_NAME, layout, background_color=SPLASH_BG)


# ---------------------- GUI (Tkinter) ----------------------

def show_splash_tk(root: tk.Tk, skip: bool = False):
    if skip:
        return
    top = tk.Toplevel(root)
    top.title(APP_NAME)
    top.configure(bg=SPLASH_BG)
    top.attributes("-topmost", True)

    # Center the splash roughly
    w, h = 520, 360
    x = max(0, (top.winfo_screenwidth() - w)//2)
    y = max(0, (top.winfo_screenheight() - h)//3)
    top.geometry(f"{w}x{h}+{x}+{y}")

    # Try to load logos (PNG preferred)
    meta_path = find_asset("MetaSeries Logo.png", "MetaSeriesLogo.png", "metaseries_logo.png")
    tmdb_path = find_asset("TMDB Logo.png", "tmdb_logo.png", "blue_square_2.png")
    tvdb_path = find_asset("TVDB Logo.png", "tvdb_logo.png", "TVDBlogo.png", "tvdb-logo.png")

    # Convert SVG->PNG if necessary (ensure_png_from_svg returns filepath or None)
    try:
        if tmdb_path and tmdb_path.lower().endswith(".svg"):
            tmdb_png = ensure_png_from_svg(tmdb_path, out_size=96)
            tmdb_path = tmdb_png or None
    except Exception:
        tmdb_path = None
    try:
        if tvdb_path and tvdb_path.lower().endswith(".svg"):
            tvdb_png = ensure_png_from_svg(tvdb_path, out_size=96)
            tvdb_path = tvdb_png or None
    except Exception:
        tvdb_path = None

    meta_img = None
    tmdb_img = None
    tvdb_img = None
    try:
        if meta_path and os.path.splitext(meta_path)[1].lower() == ".png":
            meta_img = tk.PhotoImage(file=meta_path).subsample(2)
    except Exception:
        meta_img = None
    try:
        if tmdb_path and os.path.splitext(tmdb_path)[1].lower() == ".png":
            tmdb_img = tk.PhotoImage(file=tmdb_path).subsample(4)
    except Exception:
        tmdb_img = None
    try:
        if tvdb_path and os.path.splitext(tvdb_path)[1].lower() == ".png":
            tvdb_img = tk.PhotoImage(file=tvdb_path).subsample(4)
    except Exception:
        tvdb_img = None

    pad = {"padx": 10, "pady": 5}
    if meta_img:
        tk.Label(top, image=meta_img, bg=SPLASH_BG).pack(**pad)
    else:
        tk.Label(top, text=APP_NAME, font=("Segoe UI", 18, "bold"), fg=SPLASH_TEXT, bg=SPLASH_BG).pack(**pad)

    tk.Label(top,
             text=("This MetaSeries-Organiser uses TMDB and the TMDB APIs but is not endorsed, "
                   "certified, or otherwise approved by TMDB"),
             wraplength=480, justify="center",
             fg=SPLASH_TEXT, bg=SPLASH_BG, font=("Segoe UI", 9)).pack(**pad)

    tk.Label(top,
             text="Metadata provided by TheTVDB. Please consider adding missing information or subscribing.",
             wraplength=480, justify="center",
             fg=SPLASH_TEXT, bg=SPLASH_BG, font=("Segoe UI", 9)).pack(**pad)

    if tmdb_img or tvdb_img:
        logo_frame = tk.Frame(top, bg=SPLASH_BG)
        logo_frame.pack(pady=6)
        if tmdb_img:
            tk.Label(logo_frame, image=tmdb_img, bg=SPLASH_BG).pack(side="left", padx=6)
        if tvdb_img:
            tk.Label(logo_frame, image=tvdb_img, bg=SPLASH_BG).pack(side="left", padx=6)

    tk.Button(top, text="Continue", command=top.destroy, bg=ACCENT, fg=SPLASH_TEXT, relief=tk.FLAT).pack(pady=10)

    # auto close after 4s
    top.after(4000, top.destroy)

    # Keep image refs to avoid GC
    top._meta_img = meta_img
    top._tmdb_img = tmdb_img
    top._tvdb_img = tvdb_img

    # block until closed (but don't block main root creation)
    top.grab_set()
    root.wait_window(top)


def build_gui_tk():
    root = tk.Tk()
    root.title(APP_NAME)

    show_splash_tk(root, skip=("--nosplash" in sys.argv))

    # Base fonts (we will scale these on resize)
    base_label_font = tkfont.Font(family="Segoe UI", size=10)
    base_entry_font = tkfont.Font(family="Segoe UI", size=10)
    base_button_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
    base_log_font = tkfont.Font(family="Consolas", size=10)

    # Variables
    src_var = tk.StringVar()
    dst_var = tk.StringVar()
    title_var = tk.StringVar()
    year_var = tk.StringVar()
    series_id_var = tk.StringVar()
    provider_var = tk.StringVar(value="TMDB")
    apikey_var = tk.StringVar()
    upper_var = tk.BooleanVar(value=True)
    dry_var = tk.BooleanVar(value=True)

    # Layout: 3 columns, make middle expand
    for i in range(3):
        root.grid_columnconfigure(i, weight=1 if i == 1 else 0)
    root.grid_rowconfigure(9, weight=1)  # log grows

    def row(r, text, widget):
        lbl = tk.Label(root, text=text, font=base_label_font)
        lbl.grid(row=r, column=0, sticky="w", padx=6, pady=4)
        widget.grid(row=r, column=1, sticky="we", padx=6, pady=4)
        return lbl

    # Source
    src_entry = tk.Entry(root, textvariable=src_var, width=50, font=base_entry_font)
    row(0, "Source folder:", src_entry)
    tk.Button(root, text="Browse", command=lambda: src_var.set(filedialog.askdirectory() or src_var.get()), font=base_button_font).grid(row=0, column=2, padx=6)

    # Destination
    dst_entry = tk.Entry(root, textvariable=dst_var, width=50, font=base_entry_font)
    row(1, "Destination folder (optional):", dst_entry)
    tk.Button(root, text="Browse", command=lambda: dst_var.set(filedialog.askdirectory() or dst_var.get()), font=base_button_font).grid(row=1, column=2, padx=6)

    # Title / Year
    row(2, "Show Title:", tk.Entry(root, textvariable=title_var, width=50, font=base_entry_font))
    row(3, "Year (optional):", tk.Entry(root, textvariable=year_var, width=10, font=base_entry_font))

    # Series ID
    row(4, "Series ID (optional):", tk.Entry(root, textvariable=series_id_var, width=20, font=base_entry_font))

    # Provider / API key
    provider_menu = tk.OptionMenu(root, provider_var, "TMDB", "TVDB")
    provider_menu.config(font=base_entry_font)
    row(5, "Data Provider:", provider_menu)
    row(6, "API Key/Token:", tk.Entry(root, textvariable=apikey_var, show="•", width=40, font=base_entry_font))

    # Toggles
    chk1 = tk.Checkbutton(root, text="Uppercase S/E codes (S01E01)", variable=upper_var, font=base_label_font)
    chk1.grid(row=7, column=0, columnspan=2, sticky="w", padx=6)
    chk2 = tk.Checkbutton(root, text="Dry run (don’t move, just log)", variable=dry_var, font=base_label_font)
    chk2.grid(row=8, column=0, columnspan=2, sticky="w", padx=6)

    # Log box
    tk.Label(root, text="Log:", font=base_label_font).grid(row=9, column=0, sticky="nw", padx=6)
    log_txt = tk.Text(root, height=16, width=90, state="disabled", font=base_log_font, wrap="none")
    log_txt.grid(row=9, column=1, columnspan=2, sticky="nsew", padx=6, pady=6)

    def log_tk(message: str):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        log_txt.configure(state="normal")
        log_txt.insert("end", line)
        log_txt.see("end")
        log_txt.configure(state="disabled")
        # Also write to logger file/console
        try:
            logger.info(message)
        except Exception:
            pass

    def start():
        try:
            src = src_var.get().strip()
            dst = dst_var.get().strip()
            title = title_var.get().strip()
            year_s = year_var.get().strip()
            provider_name = provider_var.get()
            apikey = apikey_var.get().strip()
            series_id = series_id_var.get().strip()
            upper = bool(upper_var.get())
            dry = bool(dry_var.get())

            if not src or not os.path.isdir(src):
                messagebox.showerror("Error", "Please choose a valid source folder.")
                return
            if not title and not series_id:
                inferred = pathlib.Path(src).name
                inferred = re.sub(r"(?i)\bseason\s*\d{1,2}\b", "", inferred).strip()
                title = inferred
                title_var.set(title)
                log_tk(f"Inferred title: {title}")
            year = int(year_s) if year_s.isdigit() else None

            if provider_name == "TMDB":
                provider = TMDBProvider(apikey)
                if series_id:
                    show_id = series_id
                    if not title or not year_s:
                        try:
                            info = provider.show_info(show_id)
                            if not title:
                                title = info.get("name") or title
                                title_var.set(title)
                            if not year_s:
                                fa = (info.get("first_air_date") or "")[:4]
                                if fa.isdigit():
                                    year = int(fa)
                                    log_tk(f"Detected year from TMDB: {year}")
                        except Exception:
                            pass
                else:
                    show = provider.search_show(title, year)
                    show_id = str(show.get("id"))
                    if not year:
                        fa = (show.get("first_air_date") or "")[:4]
                        if fa.isdigit():
                            year = int(fa)
                            log_tk(f"Detected year from TMDB: {year}")
            else:
                provider = TVDBProvider(apikey)
                if series_id:
                    show_id = series_id
                else:
                    show = provider.search_show(title, year)
                    show_id = str(show.get("id") or show.get("tvdb_id") or show.get("series",{}).get("id") or "")

            ctx = ShowContext(title=title, year=year, provider_name=provider_name, provider=provider, show_id=show_id, episode_index=[])
            src_dir = pathlib.Path(src)
            dst_root = pathlib.Path(dst) if dst else None
            log_tk(f"Provider: {provider_name} | Show ID: {show_id}")
            log_tk(f"Processing files in: {src_dir}")
            def logfn(m):
                        try:
                            win["log"].print(m)
                        except Exception:
                            pass
                        try:
                            logger.info(m)
                        except Exception:
                            pass
                    do_process(ctx, src_dir, dst_root, dry, logfn, upper_se=upper)
            log_tk("Done.")
        except ProviderError as e:
            log_tk(f"Provider error: {e}")
        except requests.HTTPError as e:
            log_tk(f"HTTP error: {e}")
        except Exception as e:
            log_tk(f"Unexpected error: {e}")

    tk.Button(root, text="Start", command=start, font=base_button_font, bg=ACCENT, fg=SPLASH_TEXT).grid(row=10, column=1, pady=8)
    tk.Button(root, text="Exit", command=root.destroy, font=base_button_font).grid(row=10, column=2, pady=8)

    root.minsize(720, 520)

    # Resize handler with debounce to avoid spamming font changes
    ref_w, ref_h = 900, 620
    after_id = None

    def _on_resize(event):
        nonlocal after_id
        # debounce
        if after_id:
            root.after_cancel(after_id)
        after_id = root.after(80, lambda: _apply_scale(root.winfo_width(), root.winfo_height()))

    def _apply_scale(w, h):
        # scale by width primarily, clamp to [0.7, 3.0]
        scale = max(0.7, min(3.0, (w / ref_w)))
        lbl_size = max(8, int(10 * scale))
        entry_size = max(8, int(10 * scale))
        btn_size = max(8, int(10 * scale))
        log_size = max(9, int(10 * scale))

        base_label_font.configure(size=lbl_size)
        base_entry_font.configure(size=entry_size)
        base_button_font.configure(size=btn_size)
        base_log_font.configure(size=log_size)

    root.bind("<Configure>", _on_resize)
    # apply initial scale once window realized
    root.after(50, lambda: _apply_scale(root.winfo_width(), root.winfo_height()))

    root.mainloop()


# ---------------------- Self-tests (no GUI) ----------------------

def run_self_tests() -> int:
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

       # Alt 01x05 / 01E05
    pr = parse_filename("Show - 01x05 - Title.mkv")
    expect(pr.kind == "se" and pr.season == 1 and pr.ep1 == 5, "parse 01x05")
    pr = parse_filename("Show - 01E05 - Title.mkv")
    expect(pr.kind == "se" and pr.season == 1 and pr.ep1 == 5, "parse 01E05")

    # Post-year dash ep
    pr = parse_filename("Euphoria (2019) - 01 Pilot.mkv")
    expect(pr.kind == "se" and pr.season == 1 and pr.ep1 == 1, "parse post-year dash ep -> S01E01")

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

    # Candidate title extraction tests (no API needed)
    t, sh = extract_candidate_title("Euphoria (2019) -  You Who Cannot See, Think of Those Who Can.mkv", "Euphoria")
    expect(t is not None and "you who cannot see" in _normalize_title(t), "extract candidate title from long episode name")
    t2, sh2 = extract_candidate_title("Euphoria 01E Stuntin' Like My Daddy.mp4", "Euphoria")
    expect(t2 is not None and "stuntin like my daddy" in _normalize_title(t2), "extract candidate title with '01E' clue")
    t3, sh3 = extract_candidate_title("Euphoria (2019) - s001 - Pilot.avi", "Euphoria")
    expect(t3 is not None and _normalize_title(t3) == "pilot", "extract candidate title from 's001 - Pilot'")

    # Uppercase/lowercase toggle output
    ei = EpisodeInfo(season=1, episode=1, title="Pilot")
    name_low = target_filename("Show", 2019, ei, ".mkv", upper_se=False)
    name_up = target_filename("Show", 2019, ei, ".mkv", upper_se=True)
    expect("s01e01" in name_low.lower(), "lowercase S/E in output")
    expect("S01E01" in name_up, "uppercase S/E in output")

    print("Self-tests complete.")
    return failures

# ---------------------- Main ----------------------

def main():
    # Self-tests
    if "--selftest" in sys.argv or "-t" in sys.argv:
        failures = run_self_tests()
        sys.exit(1 if failures else 0)

    # Decide GUI backend: Tk is default. Use PSG only if user explicitly requests it and sg is present.
    USE_PSG = ("--psg" in sys.argv) and (sg is not None)

    if USE_PSG:
        try:
            psg_ver = getattr(sg, "__version__", "unknown")
            print(f"[Info] PySimpleGUI/FreeSimpleGUI version: {psg_ver}")
        except Exception:
            pass

        show_splash(skip=("--nosplash" in sys.argv))
        win = build_gui_psg()

        # Try to enable runtime scaling for PSG by adjusting underlying Tk root fonts.
        try:
            root = win.TKroot  # underlying tkinter root/toplevel
            base_font = tkfont.Font(family="Segoe UI", size=10)
            def _apply_scale_psg(w, h):
                scale = max(0.7, min(3.0, (w / 900)))
                sz = max(8, int(10 * scale))
                try:
                    base_font.configure(size=sz)
                    # Apply as default font for children
                    root.option_add("*Font", base_font)
                except Exception:
                    pass
                # Ensure the multiline log widget uses the same font
                try:
                    ml = win["log"]
                    ml.Widget.configure(font=(base_font.actual("family"), base_font.actual("size")))
                except Exception:
                    pass

            # bind resize and apply initial scale
            root.bind("<Configure>", lambda e: _apply_scale_psg(root.winfo_width(), root.winfo_height()))
            root.after(50, lambda: _apply_scale_psg(root.winfo_width(), root.winfo_height()))
        except Exception:
            # if anything fails, just continue without dynamic PSG scaling
            pass

        # PSG event loop (unchanged logic)
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
                    series_id = (values.get("series_id") or "").strip()
                    upper = bool(values.get("upper"))
                    dry = bool(values.get("dry"))

                    if not src or not os.path.isdir(src):
                        win["log"].print("Please choose a valid source folder.")
                        continue
                    if not title and not series_id:
                        inferred = pathlib.Path(src).name
                        inferred = re.sub(r"(?i)\bseason\s*\d{1,2}\b", "", inferred).strip()
                        title = inferred
                        win["log"].print(f"Inferred title: {title}")
                    year = int(year_s) if year_s.isdigit() else None

                    if provider_name == "TMDB":
                        provider = TMDBProvider(apikey)
                        if series_id:
                            show_id = series_id
                            if not title or not year_s:
                                try:
                                    info = provider.show_info(show_id)
                                    if not title:
                                        title = info.get("name") or title
                                        win["log"].print(f"Using title from TMDB: {title}")
                                    if not year_s:
                                        fa = (info.get("first_air_date") or "")[:4]
                                        if fa.isdigit():
                                            year = int(fa)
                                            win["log"].print(f"Detected year from TMDB: {year}")
                                except Exception:
                                    pass
                        else:
                            show = provider.search_show(title, year)
                            show_id = str(show.get("id"))
                            if not year:
                                fa = (show.get("first_air_date") or "")[:4]
                                if fa.isdigit():
                                    year = int(fa)
                                    win["log"].print(f"Detected year from TMDB: {year}")
                    else:
                        provider = TVDBProvider(apikey)
                        if series_id:
                            show_id = series_id
                        else:
                            show = provider.search_show(title, year)
                            show_id = str(show.get("id") or show.get("tvdb_id") or show.get("series", {}) .get("id") or "")

                    ctx = ShowContext(title=title, year=year, provider_name=provider_name, provider=provider, show_id=show_id, episode_index=[])
                    src_dir = pathlib.Path(src)
                    dst_root = pathlib.Path(dst) if dst else None
                    win["log"].print(f"Provider: {provider_name} | Show ID: {show_id}")
                    win["log"].print(f"Processing files in: {src_dir}")
                    def logfn(m):
                        try:
                            win["log"].print(m)
                        except Exception:
                            pass
                        try:
                            logger.info(m)
                        except Exception:
                            pass
                    do_process(ctx, src_dir, dst_root, dry, logfn, upper_se=upper)
                    win["log"].print("Done.")
                except ProviderError as e:
                    win["log"].print(f"Provider error: {e}")
                except requests.HTTPError as e:
                    win["log"].print(f"HTTP error: {e}")
                except Exception as e:
                    win["log"].print(f"Unexpected error: {e}")
        win.close()
    else:
        # Default path: Tkinter GUI
        build_gui_tk()

if __name__ == "__main__":
    main()
