#!/usr/bin/env python3
"""
MetaSeries-Organiser V2 — TV Series Renamer & Sorter (Portable, TVDB v4 + TMDB attribution)

This version implements the majority of the "V2 developer-ready spec":
- Full TVDB v4 flow (token auth header) → search (disambiguation) → seasons (order=official) → episodes → real lists
- Language preference + fallback ("preferred → en → original")
- User-selectable episode ordering: aired / dvd / absolute (with sensible fallback chains)
- Always includes Season 0 (Specials)
- show_info() fills Title + Year so ID-only runs still format as "Show (Year)"
- Filenames include the episode title; multi-ep & specials naming; safe sanitisation & truncation
- Optional .nfo sidecar with episode metadata
- Resilient HTTP calls: exponential backoff + jitter; honours Retry-After; logs endpoint + code
- Prominent logging with timestamps + Copy Log / Save Log buttons; wrap toggle; H+V scrollbars
- Manual resolver dialog for ambiguous files (top matches with title, SxxEyy/absolute, air date, overview preview)
- Pre-commit "Review & Approve" table (Original → Proposed), per-row approve + Approve All, summary footer
- Undo last run: manifest.json written under ./data/manifests; one-click restore with conflict checks
- Portable Mode: MSO_PORTABLE=1 or portable.flag or settings toggle → stores all state under ./data and ./config beside exe
- CredentialsStore abstraction: OS keyring if available (non-portable), or portable vault (XOR-keystream with passphrase)
- Responsive Tkinter UI with dynamic font scaling (reflow-ish, not just zoom) and a dockable-ish log area

Notes:
* TVDB v4 endpoints are implemented per current public docs patterns. Minor field-name mismatches can happen; code includes
  robust fallbacks (aired/dvd/absolute). If TheTVDB changes field names, update map_tvdb_episode().
* PySimpleGUI path is still supported but advanced V2 features (manual resolver, review table, undo) are implemented for Tk UI.
* TMDB is still available, but TVDB is the primary provider per spec. We removed any baked-in TVDB key. You must supply your own.
* Packaging: use PyInstaller in one-folder mode and include ./assets via --add-data "assets;assets".

Copyright (c) 2025
"""

from __future__ import annotations
import os
import re
import sys
import json
import time
import math
import base64
import hashlib
import random
import shutil
import logging
import pathlib
import unicodedata
import threading
import html
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

# Optional GUI backends (PySimpleGUI / FreeSimpleGUI) — Tkinter is the primary GUI
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

# Tkinter
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import tkinter.font as tkfont

try:
    import requests  # type: ignore
except Exception:
    print("The 'requests' package is required. Run: pip install requests")
    sys.exit(1)

APP_NAME = "MetaSeries-Organiser"
SPLASH_BG = "#0b0f17"
SPLASH_TEXT = "#E6EDF3"
ACCENT = "#3EA6FF"

# ------------ Logging ------------
def app_root() -> pathlib.Path:
    # Resolve the application root; in PyInstaller, sys._MEIPASS points to temp dir with bundled files
    if hasattr(sys, "_MEIPASS"):
        try:
            return pathlib.Path(getattr(sys, "_MEIPASS")).resolve()
        except Exception:
            pass
    # For script runs, use file directory; fallback to cwd
    try:
        here = pathlib.Path(__file__).resolve().parent
        return here
    except Exception:
        return pathlib.Path(os.getcwd()).resolve()

def portable_mode_enabled() -> bool:
    # Enabled if env, portable.flag present, or settings says so (settings read later; this is early boot detection)
    if os.environ.get("MSO_PORTABLE", "") == "1":
        return True
    flag = app_root() / "portable.flag"
    if flag.exists():
        return True
    # settings read later; UI toggle can switch mode for next runs
    return False

def state_root() -> pathlib.Path:
    # When portable → all writes under ./data ; otherwise use standard user dirs
    root = app_root()
    data = root / "data"
    if portable_mode_enabled():
        (data / "logs").mkdir(parents=True, exist_ok=True)
        (data / "manifests").mkdir(parents=True, exist_ok=True)
        (data / "cache").mkdir(parents=True, exist_ok=True)
        (root / "config").mkdir(parents=True, exist_ok=True)
        return data
    else:
        # Non-portable: still co-locate logs next to script to keep it simple
        (root / "data" / "logs").mkdir(parents=True, exist_ok=True)
        (root / "data" / "manifests").mkdir(parents=True, exist_ok=True)
        (root / "config").mkdir(parents=True, exist_ok=True)
        return root / "data"

LOG_PATH = state_root() / "logs" / "MSO-Logs.txt"
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s:%(levelname)s:%(name)s:%(message)s"
)
logger = logging.getLogger("MSO-V2")
logger.setLevel(logging.INFO)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console_fmt = logging.Formatter('%(levelname)s : %(message)s')
console.setFormatter(console_fmt)
logger.addHandler(console)
logger.info("MetaSeries-Organiser V2 started. Portable=%s", portable_mode_enabled())

# ------------ Settings & Credentials ------------
SETTINGS_PATH = app_root() / "config" / "settings.json"
DEFAULT_SETTINGS = {
    "portable": portable_mode_enabled(),
    "preferred_language": "en",
    "order": "aired",   # aired | dvd | absolute
    "wrap_log": True,
    "last_window_size": [900, 620],
}

def load_settings() -> Dict[str, Any]:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # merge defaults
        out = DEFAULT_SETTINGS.copy()
        out.update(data or {})
        return out
    except Exception:
        return DEFAULT_SETTINGS.copy()

def save_settings(s: Dict[str, Any]) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save settings: %s", e)

class CredentialsStore:
    """Keyring in normal mode, AES-GCM encrypted passphrase vault in portable mode."""
    def __init__(self, portable: bool):
        self.portable = portable
        self.vault_path = state_root() / "vault" / "creds.json.enc"
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        self._passphrase: Optional[str] = None
        self._cached: Dict[str, str] = {}
        self._loaded = False
        self._memory: Dict[str, str] = {}
        self._keyring = None
        if not portable:
            try:
                import keyring
                self._keyring = keyring
            except Exception:
                logger.warning("OS keyring unavailable; credentials will be kept in memory only.")

    def set_session_passphrase(self, pw: Optional[str]):
        self._passphrase = pw
        self._loaded = False
        self._cached = {}

    def _derive_key(self, salt: bytes, passphrase: str) -> bytes:
        return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)

    def _save_vault(self):
        if not self._passphrase:
            raise RuntimeError("A passphrase is required to save the portable credential vault.")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        salt, nonce = os.urandom(16), os.urandom(12)
        key = self._derive_key(salt, self._passphrase)
        plain = json.dumps(self._cached).encode("utf-8")
        blob = base64.b64encode(b"MSOV1" + salt + nonce + AESGCM(key).encrypt(nonce, plain, b"MSO credentials v1"))
        tmp = self.vault_path.with_suffix(self.vault_path.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.vault_path)
        self._loaded = True

    def _load_vault(self):
        if self._loaded:
            return
        if not self.vault_path.exists():
            self._cached, self._loaded = {}, True
            return
        if not self._passphrase:
            raise RuntimeError("Enter the portable vault passphrase to unlock saved credentials.")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.b64decode(self.vault_path.read_bytes())
        if raw[:5] != b"MSOV1" or len(raw) < 34:
            raise RuntimeError("Unsupported or damaged portable credential vault.")
        salt, nonce, ciphertext = raw[5:21], raw[21:33], raw[33:]
        try:
            plain = AESGCM(self._derive_key(salt, self._passphrase)).decrypt(nonce, ciphertext, b"MSO credentials v1")
            self._cached = json.loads(plain.decode("utf-8"))
        except Exception as e:
            raise RuntimeError("Vault passphrase is incorrect or the vault is damaged.") from e
        self._loaded = True

    def get(self, key: str) -> Optional[str]:
        if self.portable:
            self._load_vault()
            return self._cached.get(key)
        if self._keyring:
            try: return self._keyring.get_password(APP_NAME, key)
            except Exception as e: logger.warning("Keyring read failed: %s", e)
        return self._memory.get(key)

    def set(self, key: str, value: str) -> None:
        if self.portable:
            self._load_vault()
            self._cached[key] = value
            self._save_vault()
        elif self._keyring:
            try:
                self._keyring.set_password(APP_NAME, key, value)
            except Exception as e:
                logger.warning("Keyring write failed; retaining credential for this session only: %s", e)
                self._memory[key] = value
        else:
            self._memory[key] = value

# ------------ Utilities & parsing ------------
EXTRA_SUFFIXES = [
    "behindthescenes", "deleted", "featurette", "interview",
    "scene", "short", "trailer", "other"
]
SPLIT_TOKENS = ["cd", "disc", "disk", "dvd", "part", "pt"]
DATE_SEPS = r"[-.\s]"
DATE_PATTERNS = [
    re.compile(rf"(?P<y>\d{{4}}){DATE_SEPS}(?P<m>\d{{1,2}}){DATE_SEPS}(?P<d>\d{{1,2}})"),
    re.compile(rf"(?P<d>\d{{1,2}}){DATE_SEPS}(?P<m>\d{{1,2}}){DATE_SEPS}(?P<y>\d{{4}})"),
]
SXXEYY_RANGE_RE = re.compile(r"(?i)s(?P<s>\d{1,2})\s*e(?P<e1>\d{1,3})(?:\s*[-_\.]?\s*e(?P<e2>\d{1,3}))?")
ALT_SE_RE = re.compile(r"(?i)(?:^|\b)(?:s\s*)?(?P<s>\d{1,2})\s*[ex]\s*(?P<e>\d{1,3})(?:\b|$)")
DASH_EP_AFTER_YEAR_RE = re.compile(r"\)\s*-\s*(?P<e>\d{1,3})\b")
ABSOLUTE_NUMBER_RE = re.compile(r"^\s*(?P<abs>\d{1,4})[).\-\s_]+")
SPLIT_PART_RE = re.compile(r"(?i)(?:\b|[-_])(cd|disc|disk|dvd|part|pt)(?P<n>\d+)\b")
INLINE_EXTRA_RE = re.compile(rf"(?i)-(?P<etype>{'|'.join(EXTRA_SUFFIXES)})(?P<idx>\d+)?(?=\.[^.]+$)")

@dataclass
class ParseResult:
    kind: str
    season: Optional[int] = None
    ep1: Optional[int] = None
    ep2: Optional[int] = None
    date_iso: Optional[str] = None
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

    # Extras
    extra = INLINE_EXTRA_RE.search(base)
    extra_type = extra.group("etype").lower() if extra else None
    extra_idx = int(extra.group("idx")) if extra and extra.group("idx") else None

    # Split parts
    sp = SPLIT_PART_RE.search(name)
    split_token = sp.group(1).lower() if sp else None
    split_idx = int(sp.group("n")) if sp else None

    m = SXXEYY_RANGE_RE.search(base)
    if m:
        s = int(m.group("s"))
        e1 = int(m.group("e1"))
        e2 = m.group("e2")
        kind = "special" if s == 0 else ("se_range" if e2 else "se")
        return ParseResult(kind=kind, season=s, ep1=e1, ep2=int(e2) if e2 else None,
                           extra_type=extra_type, extra_index=extra_idx,
                           split_token=split_token, split_index=split_idx)

    m2 = ALT_SE_RE.search(base)
    if m2:
        s = int(m2.group("s"))
        e1 = int(m2.group("e"))
        return ParseResult(kind="se", season=s, ep1=e1, extra_type=extra_type, extra_index=extra_idx,
                           split_token=split_token, split_index=split_idx)

    d_iso = detect_date(base)
    if d_iso:
        return ParseResult(kind="date", date_iso=d_iso, extra_type=extra_type,
                           extra_index=extra_idx, split_token=split_token, split_index=split_idx)

    d3 = DASH_EP_AFTER_YEAR_RE.search(base)
    if d3:
        e1 = int(d3.group("e"))
        return ParseResult(kind="se", season=1, ep1=e1, extra_type=extra_type, extra_index=extra_idx,
                           split_token=split_token, split_index=split_idx)

    a = ABSOLUTE_NUMBER_RE.match(base)
    if a:
        return ParseResult(kind="absolute", ep1=int(a.group("abs")), extra_type=extra_type,
                           extra_index=extra_idx, split_token=split_token, split_index=split_idx)

    return ParseResult(kind="unknown", extra_type=extra_type, extra_index=extra_idx,
                       split_token=split_token, split_index=split_idx)

# ------------ Title normalisation & matching ------------
def _normalize_title(s: str) -> str:
    s = unicodedata.normalize('NFKD', s)
    s = s.encode('ascii', 'ignore').decode('ascii')
    s = s.casefold()
    s = re.sub(r"[‘’ʼ´`]", "'", s)
    s = re.sub(r"[“”]", '"', s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _token_sort(s: str) -> str:
    toks = _normalize_title(s).split()
    toks.sort()
    return " ".join(toks)

def _title_similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, _token_sort(a), _token_sort(b)).ratio()

def extract_candidate_title(filename: str, series_title: str) -> Tuple[Optional[str], Optional[int]]:
    base = os.path.splitext(os.path.basename(filename))[0]
    s = re.sub(r"[._]+", " ", base)
    pat = re.compile(rf"(?i)^\s*{re.escape(series_title)}\s*(?:\(\d{{4}}\))?\s*[-:–—]*\s*")
    s = pat.sub("", s)
    season_hint = None
    m = re.search(r"(?i)\b(\d{1,2})\s*e\b", s)
    if m:
        try:
            season_hint = int(m.group(1))
        except Exception:
            season_hint = None
    s = re.sub(r"(?i)\bS\s*\d{1,3}\b", " ", s)
    s = re.sub(r"(?i)\bS\s*\d{1,3}E\s*\d{1,3}\b", " ", s)
    s = re.sub(r"(?i)\b\d{1,2}[xX]\d{1,3}\b", " ", s)
    s = re.sub(r"(?i)\bE\s*\d{1,3}\b", " ", s)
    s = re.sub(r"(?i)\bs\s*\d{1,3}\b", " ", s)
    s = re.sub(r"(?i)\b\d{1,3}\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s if s else None, season_hint)

# ------------ Provider Interfaces ------------
class ProviderError(Exception):
    pass

@dataclass
class EpisodeInfo:
    season: int
    episode: int
    title: Optional[str] = None
    air_date: Optional[str] = None  # YYYY-MM-DD
    is_special: bool = False
    absolute: Optional[int] = None
    dvd_season: Optional[int] = None
    dvd_episode: Optional[int] = None
    overview: Optional[str] = None

class Provider:
    def __init__(self, language: str = "en"):
        self.language = language

    def search_show(self, title: str, year: Optional[int] = None) -> Dict:
        raise NotImplementedError

    def load_season(self, show_id: str, season_number: int, order: str = "aired") -> Dict:
        raise NotImplementedError

    def map_date_to_episode(self, show_id: str, date_iso: str, order: str = "aired") -> Optional[EpisodeInfo]:
        raise NotImplementedError

    def build_episode_index(self, show_id: str, order: str = "aired") -> List[EpisodeInfo]:
        raise NotImplementedError

    def show_info(self, show_id: str) -> Dict:
        raise NotImplementedError

# ------------ HTTP helpers with backoff ------------
def backoff_sleep(attempt: int, base_delay: float = 0.5, cap: float = 16.0) -> float:
    # exp backoff with jitter
    delay = min(cap, base_delay * (2 ** attempt))
    jitter = random.uniform(0, delay * 0.25)
    return delay + jitter

def request_with_backoff(sess: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    attempts = 0
    last_exc = None
    while attempts < 6:
        try:
            r = sess.request(method, url, timeout=kwargs.pop("timeout", 20), **kwargs)
            # Retry on 429 / 5xx
            if r.status_code in (429, 500, 502, 503, 504):
                ra = r.headers.get("Retry-After")
                wait = float(ra) if ra and ra.isdigit() else backoff_sleep(attempts)
                logger.warning("HTTP %s %s -> %s; retrying in %.1fs", method, url, r.status_code, wait)
                time.sleep(wait)
                attempts += 1
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            wait = backoff_sleep(attempts)
            logger.warning("HTTP error on %s %s: %s; retrying in %.1fs", method, url, e, wait)
            time.sleep(wait)
            attempts += 1
    if last_exc:
        raise last_exc
    raise RuntimeError("Request failed after retries")

# ------------ TVDB v4 Provider ------------
class TVDBProvider(Provider):
    """
    TVDB v4 integration using a user-provided token (Bearer).
    - search_show: /v4/search?q=...&type=series
    - seasons: /v4/series/{id}/seasons
    - episodes: /v4/series/{id}/episodes/{seasonType}/{seasonNumber}?page=1...
      seasonType: official (per spec) but user may choose ordering mapping when naming
    - show_info: /v4/series/{id} or /v4/series/{id}/extended to get name + firstAired
    Language handling: sets Accept-Language header for preferred language (fallbacks handled in code).
    """
    def __init__(self, token: str, language: str = "en", pin: str = ""):
        super().__init__(language=language)
        if not token:
            raise ProviderError("TVDB API key is required.")
        self.base = "https://api4.thetvdb.com/v4"
        self.session = requests.Session()
        if language:
            self.session.headers.update({"Accept-Language": language})
        # TVDB API keys are exchanged for a short-lived JWT. A JWT already
        # supplied by an advanced user remains supported for this session.
        self.api_key = None
        self.pin = pin
        if token.count(".") == 2:
            self._set_jwt(token)
        else:
            self.api_key = token
            self._login()

    def _login(self):
        body = {"apikey": self.api_key}
        if self.pin:
            body["pin"] = self.pin
        response = request_with_backoff(self.session, "POST", f"{self.base}/login", json=body)
        jwt = (response.json().get("data") or {}).get("token")
        if not jwt:
            raise ProviderError("TVDB login succeeded without returning a session token.")
        self._set_jwt(jwt)

    def _set_jwt(self, jwt: str):
        self.session.headers.update({"Authorization": f"Bearer {jwt}"})

    def _get(self, path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        try:
            r = request_with_backoff(self.session, "GET", url, params=params or {})
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 401 or not self.api_key:
                raise
            logger.info("TVDB session expired; exchanging API key for a fresh token.")
            self._login()
            r = request_with_backoff(self.session, "GET", url, params=params or {})
        try:
            return r.json()
        except Exception:
            raise ProviderError(f"TVDB: Non-JSON response from {path}")

    def search_show(self, title: str, year: Optional[int] = None) -> Dict:
        data = self._get("/search", params={"q": title, "type": "series"}).get("data", [])
        if not data:
            # Try fallback language "en" if preferred failed
            if self.language and self.language != "en":
                hdrs = dict(self.session.headers)
                hdrs["Accept-Language"] = "en"
                r = request_with_backoff(self.session, "GET", f"{self.base}/search", params={"q": title, "type": "series"}, headers=hdrs)
                data = r.json().get("data", [])
        if not data:
            raise ProviderError("Show not found on TVDB.")
        # Prefer exact year if provided (compare firstAired year)
        if year:
            exact = [it for it in data if str(it.get("year") or it.get("firstAired") or "")[:4] == str(year)]
            if exact:
                data = exact
        # Preserve ambiguity for the UI instead of silently choosing result 0.
        return {"_candidates": data} if len(data) > 1 else data[0]

    def show_info(self, show_id: str) -> Dict:
        # Try extended first to get year easily
        d = self._get(f"/series/{show_id}/extended").get("data") or {}
        if not d:
            d = self._get(f"/series/{show_id}").get("data") or {}
        return d

    def list_seasons_official(self, show_id: str) -> List[int]:
        # Return list of official season numbers including 0 if present
        d = self._get(f"/series/{show_id}/seasons").get("data", [])
        nums = []
        for s in d:
            if (s.get("type", "").lower() == "official") and isinstance(s.get("number"), int):
                nums.append(int(s["number"]))
        # include 0 if there is an explicit Specials season present in TVDB:
        if 0 not in nums:
            # some series still have 0; try to detect via episodes endpoint
            try:
                eps0 = self._get(f"/series/{show_id}/episodes/official/0").get("data", [])
                if eps0:
                    nums = [0] + nums
            except Exception:
                pass
        nums = sorted(set(nums))
        return nums

    def _list_episodes_official(self, show_id: str, season_number: int) -> List[Dict[str, Any]]:
        # Iterate pages
        out = []
        page = 1
        while True:
            d = self._get(f"/series/{show_id}/episodes/official/{season_number}", params={"page": page})
            items = d.get("data", [])
            out.extend(items)
            links = d.get("links") or {}
            if not links or not links.get("next"):
                break
            page += 1
        return out

    def load_season(self, show_id: str, season_number: int, order: str = "aired") -> Dict:
        # Always fetch official season list; episodes raw, then map into EpisodeInfo per order
        raw_eps = self._list_episodes_official(show_id, season_number)
        episodes = []
        for ep in raw_eps:
            episodes.append(map_tvdb_episode(ep))
        # Return a dict like TMDBProvider does
        return {"episodes": [e.__dict__ for e in episodes]}

    def map_date_to_episode(self, show_id: str, date_iso: str, order: str = "aired") -> Optional[EpisodeInfo]:
        # search all official seasons including 0
        seasons = self.list_seasons_official(show_id)
        for s in seasons:
            eps = self._list_episodes_official(show_id, s)
            for ep in eps:
                air = ep.get("aired") or ep.get("firstAired")
                if air and str(air)[:10] == date_iso:
                    return map_tvdb_episode(ep)
        return None

    def build_episode_index(self, show_id: str, order: str = "aired") -> List[EpisodeInfo]:
        seasons = self.list_seasons_official(show_id)
        all_eps: List[EpisodeInfo] = []
        for s in seasons:
            eps = self._list_episodes_official(show_id, s)
            for ep in eps:
                all_eps.append(map_tvdb_episode(ep))
        # sort by chosen order
        def key_air(e: EpisodeInfo):
            ad = e.air_date or "9999-99-99"
            return (ad, e.season, e.episode)
        def key_dvd(e: EpisodeInfo):
            ds = e.dvd_season if e.dvd_season is not None else e.season
            de = e.dvd_episode if e.dvd_episode is not None else e.episode
            return (ds, de, e.season, e.episode)
        def key_abs(e: EpisodeInfo):
            a = e.absolute if e.absolute else 999999
            return (a, e.season, e.episode)
        if order == "dvd":
            all_eps.sort(key=key_dvd)
        elif order == "absolute":
            all_eps.sort(key=key_abs)
        else:
            all_eps.sort(key=key_air)
        return all_eps

def map_tvdb_episode(ep: Dict[str, Any]) -> EpisodeInfo:
    # TVDB v4 typical fields (robust to missing)
    season = ep.get("seasonNumber") or ep.get("season_number") or 0
    episode = ep.get("number") or ep.get("episodeNumber") or ep.get("episode_number") or 0
    title = ep.get("name") or ep.get("episodeName") or ep.get("title")
    air = ep.get("aired") or ep.get("firstAired") or ep.get("firstAiredIso")
    absn = ep.get("absoluteNumber") or ep.get("absolute_number")
    dvdS = ep.get("dvdSeason") or ep.get("dvd_season")
    dvdE = ep.get("dvdEpisodeNumber") or ep.get("dvd_episode")
    overview = ep.get("overview") or ep.get("summary")
    return EpisodeInfo(
        season=int(season or 0),
        episode=int(episode or 0),
        title=title or None,
        air_date=(str(air)[:10] if air else None),
        is_special=(int(season or 0) == 0),
        absolute=int(absn) if absn else None,
        dvd_season=int(dvdS) if dvdS not in (None, "") else None,
        dvd_episode=int(dvdE) if dvdE not in (None, "") else None,
        overview=overview or None,
    )

# ------------ TMDB Provider (kept for compatibility) ------------
class TMDBProvider(Provider):
    def __init__(self, api_key: str, language: str = "en"):
        super().__init__(language=language)
        if not api_key:
            raise ProviderError("TMDB API key is required.")
        self.api_key = api_key
        self.base = "https://api.themoviedb.org/3"
        self.session = requests.Session()
        self.session.params.update({"api_key": self.api_key})
        if language:
            self.session.params.update({"language": language})

    def _get(self, path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        r = request_with_backoff(self.session, "GET", url, params=params or {})
        return r.json()

    def search_show(self, title: str, year: Optional[int] = None) -> Dict:
        params = {"query": title}
        if year:
            params["first_air_date_year"] = year
        data = self._get("/search/tv", params=params)
        res = data.get("results", [])
        if not res:
            raise ProviderError("Show not found on TMDB.")
        if year:
            exact = [it for it in res if (it.get("first_air_date") or "")[:4] == str(year)]
            if exact:
                res = exact
        return {"_candidates": res} if len(res) > 1 else res[0]

    def load_season(self, show_id: str, season_number: int, order: str = "aired") -> Dict:
        # TMDB only has aired order
        try:
            r = request_with_backoff(self.session, "GET", f"{self.base}/tv/{show_id}/season/{season_number}")
            data = r.json()
            return data
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return {"episodes": []}
            raise

    def map_date_to_episode(self, show_id: str, date_iso: str, order: str = "aired") -> Optional[EpisodeInfo]:
        for s in range(0, 51):
            season = self.load_season(show_id, s)
            for ep in season.get("episodes", []):
                if ep.get("air_date") == date_iso:
                    return EpisodeInfo(season=s, episode=ep.get("episode_number"),
                                       title=ep.get("name"), air_date=ep.get("air_date"),
                                       is_special=(s==0))
        return None

    def build_episode_index(self, show_id: str, order: str = "aired") -> List[EpisodeInfo]:
        all_eps: List[EpisodeInfo] = []
        for s in range(0, 101):
            season = self.load_season(show_id, s)
            eps = season.get("episodes", [])
            if not eps and s > 10:
                break
            for ep in eps:
                all_eps.append(EpisodeInfo(
                    season=s,
                    episode=ep.get("episode_number"),
                    title=ep.get("name"),
                    air_date=ep.get("air_date"),
                    is_special=(s==0)
                ))
        def sd_key(e: EpisodeInfo):
            ad = e.air_date or "9999-99-99"
            return (ad, e.season, e.episode)
        all_eps.sort(key=sd_key)
        return all_eps

    def show_info(self, show_id: str) -> Dict:
        r = request_with_backoff(self.session, "GET", f"{self.base}/tv/{show_id}")
        return r.json()

# ------------ Renamer core ------------
def sanitize_filename(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", "_", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:240]  # safe truncation

def ensure_dir(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)

def format_season_folder(n: int) -> str:
    return f"Season {n:02d}"

def target_filename(show_title: str, year: Optional[int], episode: EpisodeInfo,
                    ext: str, date_iso: Optional[str]=None, extra: Optional[str]=None,
                    multi_ep: Optional[Tuple[int,int]]=None, upper_se: bool = True,
                    order: str = "aired") -> str:
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

@dataclass
class ShowContext:
    title: str
    year: Optional[int]
    provider_name: str
    provider: Provider
    show_id: str
    episode_index: List[EpisodeInfo]
    order: str = "aired"

def resolve_episode_by_title(ctx: ShowContext, guess_title: str, season_hint: Optional[int] = None) -> Optional[EpisodeInfo]:
    if not guess_title:
        return None
    best = (0.0, None)
    best_ep = None
    seasons = range(0, 51) if season_hint is None else [season_hint]
    for s in seasons:
        season = ctx.provider.load_season(ctx.show_id, s, order=ctx.order)
        eps = season.get("episodes", [])
        for ep in eps:
            title = ep.get("title") or ep.get("name") or ""
            score = _title_similarity(guess_title, title)
            if score > best[0]:
                best = (score, s)
                best_ep = EpisodeInfo(
                    season=s, episode=int(ep.get("episode") or ep.get("episode_number") or 0),
                    title=title, air_date=ep.get("air_date") or ep.get("air_date"),
                    is_special=(s==0),
                    absolute=ep.get("absolute") or None
                )
    # broaden search if hinted and nothing found
    if best_ep is None and season_hint is not None:
        for s in range(0, 51):
            if s == season_hint:
                continue
            season = ctx.provider.load_season(ctx.show_id, s, order=ctx.order)
            for ep in season.get("episodes", []):
                title = ep.get("title") or ep.get("name") or ""
                score = _title_similarity(guess_title, title)
                if score > best[0]:
                    best = (score, s)
                    best_ep = EpisodeInfo(
                        season=s, episode=int(ep.get("episode") or ep.get("episode_number") or 0),
                        title=title, air_date=ep.get("air_date"),
                        is_special=(s==0),
                        absolute=ep.get("absolute") or None
                    )
    if best_ep and best[0] >= 0.80:
        return best_ep
    # exact normalized equality
    norm_guess = _normalize_title(guess_title)
    for s in range(0, 51):
        season = ctx.provider.load_season(ctx.show_id, s, order=ctx.order)
        for ep in season.get("episodes", []):
            title = ep.get("title") or ep.get("name") or ""
            if _normalize_title(title) == norm_guess:
                return EpisodeInfo(season=s, episode=int(ep.get("episode") or ep.get("episode_number") or 0),
                                   title=title, air_date=ep.get("air_date"), is_special=(s==0))
    return None

def resolve_episode_from_absolute(ctx: ShowContext, abs_index: int) -> Optional[EpisodeInfo]:
    if abs_index <= 0:
        return None
    if not ctx.episode_index:
        ctx.episode_index = ctx.provider.build_episode_index(ctx.show_id, order=ctx.order)
    if abs_index <= len(ctx.episode_index):
        return ctx.episode_index[abs_index - 1]
    return None

# ------------ .nfo sidecar ------------
def render_nfo(ep: EpisodeInfo, show_title: str, year: Optional[int]) -> str:
    esc = lambda v: html.escape(str(v or "").strip(), quote=True)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<episodedetails>
  <title>{esc(ep.title)}</title>
  <showtitle>{esc(show_title)}</showtitle>
  {"<year>"+str(year)+"</year>" if year else ""}
  <season>{ep.season}</season>
  <episode>{ep.episode}</episode>
  {"<aired>"+esc(ep.air_date)+"</aired>" if ep.air_date else ""}
  {"<plot>"+esc(ep.overview)+"</plot>" if ep.overview else ""}
</episodedetails>
"""

def write_nfo(nfo_path: pathlib.Path, ep: EpisodeInfo, show_title: str, year: Optional[int]):
    try:
        tmp = nfo_path.with_suffix(nfo_path.suffix + ".tmp")
        tmp.write_text(render_nfo(ep, show_title, year), encoding="utf-8")
        os.replace(tmp, nfo_path)
    except Exception as e:
        logger.warning("Failed to write NFO %s: %s", nfo_path, e)

# ------------ Manifest / Undo ------------
def manifests_dir() -> pathlib.Path:
    d = state_root() / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    return d

def latest_manifest_path() -> Optional[pathlib.Path]:
    mdir = manifests_dir()
    files = sorted(mdir.glob("manifest-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None

def write_manifest(items: List[Dict[str, Any]], source_root: pathlib.Path) -> pathlib.Path:
    manifest = {
        "version": 1,
        "started_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_root": str(source_root),
        "items": items,
    }
    p = manifests_dir() / f"manifest-{int(time.time() * 1000)}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p

def save_manifest_state(path: pathlib.Path, manifest: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def file_sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def undo_from_manifest(manifest_path: pathlib.Path, logfn) -> None:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = data.get("items", [])
        failures = 0
        for it in reversed(items):
            if it.get("action") == "create":
                if it.get("status") not in ("done", "planned") or not it.get("sha256"):
                    continue
                made = pathlib.Path(it.get("path", ""))
                if made.exists() and (not it.get("sha256") or file_sha256(made) == it["sha256"]):
                    made.unlink()
                    logfn(f"[UNDO] Removed created sidecar: {made}")
                else:
                    logfn(f"[UNDO] Sidecar missing or changed; left untouched: {made}")
                    failures += 1
                continue
            if it.get("action") != "rename":
                continue
            src = pathlib.Path(it.get("to"))
            dst = pathlib.Path(it.get("from"))
            if not src.exists():
                logfn(f"[UNDO] Missing current file (skipped): {src}")
                failures += 1
                continue
            expected = it.get("sha256")
            if expected and file_sha256(src) != expected:
                logfn(f"[UNDO] Hash mismatch; left untouched: {src}")
                failures += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            candidate = dst
            i = 1
            while candidate.exists():
                stem, ext = os.path.splitext(dst.name)
                candidate = dst.with_name(f"{stem} (undo {i}){ext}")
                i += 1
            shutil.move(str(src), str(candidate))
            logfn(f"[UNDO] {src} -> {candidate}")
        if failures:
            logfn(f"[UNDO] Completed with {failures} issue(s). See log.")
        else:
            logfn("[UNDO] Completed successfully.")
    except Exception as e:
        logfn(f"[UNDO] Failed: {e}")

# ------------ Planner & Executor ------------
@dataclass
class PlanItem:
    src: pathlib.Path
    dst: pathlib.Path
    nfo: Optional[pathlib.Path]
    description: str
    extra: Optional[str] = None
    approved: bool = True
    episode: Optional[EpisodeInfo] = None
    candidates: List[Tuple[float, EpisodeInfo]] = None
    show_title: str = ""
    year: Optional[int] = None
    tags: List[str] = None

class PlanList(list):
    skipped_count: int = 0

def plan_moves(ctx: ShowContext, src_dir: pathlib.Path, dst_root: Optional[pathlib.Path],
               logfn, upper_se: bool, write_sidecar: bool) -> List[PlanItem]:
    files = [p for p in src_dir.iterdir() if p.is_file()]
    if not files:
        logfn("No files found in source directory.")
        return []

    show_disp = f"{ctx.title} ({ctx.year})" if ctx.year else ctx.title
    if not dst_root:
        dst_root = src_dir
    show_dir = dst_root / show_disp
    season_cache: Dict[int, Dict] = {}
    title_catalog: Optional[List[EpisodeInfo]] = None

    def get_episode(s: int, e: int) -> Optional[EpisodeInfo]:
        if s not in season_cache:
            season_cache[s] = ctx.provider.load_season(ctx.show_id, s, order=ctx.order)
        for ep in season_cache[s].get("episodes", []):
            # accommodate both provider dicts
            epno = int(ep.get("episode") or ep.get("episode_number") or ep.get("number") or -1)
            if epno == int(e):
                return EpisodeInfo(
                    season=s, episode=epno, title=ep.get("title") or ep.get("name"),
                    air_date=ep.get("air_date") or ep.get("airDate") or ep.get("aired") or None,
                    is_special=(s==0),
                    absolute=ep.get("absolute") or ep.get("absoluteNumber"),
                    dvd_season=ep.get("dvd_season") or ep.get("dvdSeason"),
                    dvd_episode=ep.get("dvd_episode") or ep.get("dvdEpisodeNumber"),
                    overview=ep.get("overview"),
                )
        return EpisodeInfo(season=s, episode=e, title=None, air_date=None, is_special=(s==0))

    plan: List[PlanItem] = PlanList()

    for p in files:
        pr = parse_filename(p.name)
        ext = p.suffix
        candidates: List[Tuple[float, EpisodeInfo]] = []
        ep_info: Optional[EpisodeInfo] = None
        multi_ep_range: Optional[Tuple[int,int]] = None
        season_folder = None
        date_iso = None
        extra_suffix = f"{pr.extra_type}{pr.extra_index or ''}" if pr.extra_type else None

        if pr.kind in ("se", "special") and pr.season is not None and pr.ep1 is not None:
            ep_info = get_episode(pr.season, pr.ep1)
            season_folder = format_season_folder(pr.season)
        elif pr.kind == "se_range" and pr.season is not None and pr.ep1 is not None and pr.ep2 is not None:
            e1, e2 = pr.ep1, pr.ep2
            ep_info = get_episode(pr.season, e1)
            multi_ep_range = (e1, e2)
            season_folder = format_season_folder(pr.season)
        elif pr.kind == "date" and pr.date_iso:
            ep_info = ctx.provider.map_date_to_episode(ctx.show_id, pr.date_iso, order=ctx.order)
            date_iso = pr.date_iso
            season_folder = format_season_folder(ep_info.season if ep_info else 1)
        elif pr.kind == "absolute" and pr.ep1:
            ep_info = resolve_episode_from_absolute(ctx, pr.ep1)
            season_folder = format_season_folder(ep_info.season if ep_info else 1)
        else:
            # Manual resolver path: propose top matches by title similarity
            cand_title, season_hint = extract_candidate_title(p.name, ctx.title)
            if cand_title:
                if title_catalog is None:
                    title_catalog = ctx.provider.build_episode_index(ctx.show_id, order=ctx.order)
                for epi in title_catalog:
                    if season_hint is not None and epi.season != season_hint:
                        continue
                    score = _title_similarity(cand_title, epi.title or "")
                    if score >= 0.35:  # retain weaker matches for a human decision
                        candidates.append((score, epi))
                if not candidates and season_hint is not None:
                    for epi in title_catalog:
                        score = _title_similarity(cand_title, epi.title or "")
                        if score >= 0.35:
                            candidates.append((score, epi))
                candidates.sort(key=lambda t: t[0], reverse=True)
                if candidates:
                    ep_info = candidates[0][1]
                    season_folder = format_season_folder(ep_info.season)
                else:
                    logfn(f"SKIP (no match by title): {p.name}")
                    plan.skipped_count += 1
                    continue
            else:
                logfn(f"SKIP (unknown): {p.name}")
                plan.skipped_count += 1
                continue

        if not ep_info:
            logfn(f"WARN: Could not resolve episode metadata for {p.name} — using minimal naming.")
            m = re.search(r"\d+", season_folder or "")
            season_num = int(m.group()) if m else 1
            ep_num = int(pr.ep1) if pr.ep1 else 1
            ep_info = EpisodeInfo(season=season_num, episode=ep_num, title=None, air_date=None, is_special=(season_num==0))

        season_dir = show_dir / season_folder
        target_name = target_filename(ctx.title, ctx.year, ep_info, ext,
                                      date_iso=date_iso, extra=extra_suffix,
                                      multi_ep=multi_ep_range, upper_se=upper_se, order=ctx.order)
        dst = season_dir / target_name
        if pr.split_token and pr.split_index:
            stem, ext2 = os.path.splitext(dst.name)
            dst = dst.with_name(f"{stem}-{pr.split_token}{pr.split_index}{ext2}")

        nfo = dst.with_suffix(".nfo") if write_sidecar else None
        plan.append(PlanItem(src=p, dst=dst, nfo=nfo, description=f"{p.name} -> {dst.name}", extra=extra_suffix, approved=True,
                             episode=ep_info, candidates=(candidates if pr.kind not in ("se", "special", "se_range", "date", "absolute") else []),
                             show_title=ctx.title, year=ctx.year,
                             tags=(["extra"] if extra_suffix else []) + (["special"] if ep_info.season == 0 else []) +
                                  (["split"] if pr.split_token else []) + (["merge"] if multi_ep_range else [])))

    return plan

def execute_plan(plan: List[PlanItem], logfn) -> pathlib.Path:
    manifest_items = []
    manifest_path = manifests_dir() / f"manifest-{int(time.time() * 1000)}.json"
    manifest = {"version": 1, "started_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source_root": str(plan[0].src.parent if plan else pathlib.Path(".")), "items": manifest_items}
    # Persist every planned operation before the first filesystem mutation.
    for it in plan:
        if it.approved:
            manifest_items.append({"action": "rename", "from": str(it.src), "to": str(it.dst),
                                   "size": it.src.stat().st_size if it.src.exists() else None,
                                   "sha256": file_sha256(it.src) if it.src.exists() else None,
                                   "status": "planned"})
            if it.nfo:
                manifest_items.append({"action": "create", "path": str(it.nfo), "sha256": None, "status": "planned"})
    save_manifest_state(manifest_path, manifest)
    manifest_by_src = {i["from"]: i for i in manifest_items if i.get("action") == "rename"}
    for it in plan:
        if not it.approved:
            logfn(f"SKIP (not approved): {it.src.name}")
            continue
        candidate = it.dst
        i = 1
        while candidate.exists():
            stem, ext2 = os.path.splitext(it.dst.name)
            candidate = it.dst.with_name(f"{stem} ({i}){ext2}")
            i += 1
        it.dst.parent.mkdir(parents=True, exist_ok=True)
        # Update the pre-written recovery record with the conflict-adjusted destination.
        rec = manifest_by_src[str(it.src)]
        rec["to"] = str(candidate)
        save_manifest_state(manifest_path, manifest)
        shutil.move(str(it.src), str(candidate))
        logfn(f"MOVED: {it.src.name} -> {candidate}")
        rec["status"] = "done"
        rec["sha256"] = file_sha256(candidate)
        save_manifest_state(manifest_path, manifest)
        if it.nfo:
            try:
                if it.episode:
                    nfo_path = candidate.with_suffix(".nfo")
                    nfo_rec = next(x for x in manifest_items if x.get("action") == "create" and x.get("path") == str(it.nfo))
                    nfo_rec["path"] = str(nfo_path)
                    if nfo_path.exists():
                        nfo_rec["status"] = "skipped-conflict"
                        logfn(f"WARN: Existing NFO left unchanged: {nfo_path}")
                        save_manifest_state(manifest_path, manifest)
                        continue
                    xml = render_nfo(it.episode, it.show_title, it.year)
                    nfo_rec["sha256"] = hashlib.sha256(xml.encode("utf-8")).hexdigest()
                    save_manifest_state(manifest_path, manifest)
                    write_nfo(nfo_path, it.episode, it.show_title, it.year)
                    nfo_rec["status"] = "done"
                    save_manifest_state(manifest_path, manifest)
            except Exception as e:
                logfn(f"WARN: Could not write sidecar for {candidate.name}: {e}")
    return manifest_path

# ------------ Tkinter UI (V2 features) ------------
def resource_root() -> pathlib.Path:
    return app_root()

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

def show_splash_tk(root: tk.Tk, skip: bool = False):
    if skip:
        return
    top = tk.Toplevel(root)
    top.title(APP_NAME)
    top.configure(bg=SPLASH_BG)
    top.attributes("-topmost", True)
    w, h = 540, 380
    x = max(0, (top.winfo_screenwidth() - w)//2)
    y = max(0, (top.winfo_screenheight() - h)//3)
    top.geometry(f"{w}x{h}+{x}+{y}")

    meta_path = find_asset("MetaSeries Logo.png", "MetaSeriesLogo.png", "metaseries_logo.png")
    tmdb_path = find_asset("TMDB Logo.png", "tmdb_logo.png", "blue_square_2.png")
    tvdb_path = find_asset("TVDB Logo.png", "tvdb_logo.png", "TVDBlogo.png", "tvdb-logo.png")

    try:
        if tmdb_path and tmdb_path.lower().endswith(".svg"):
            tmdb_png = ensure_png_from_svg(tmdb_path, out_size=96); tmdb_path = tmdb_png or None
    except Exception: tmdb_path = None
    try:
        if tvdb_path and tvdb_path.lower().endswith(".svg"):
            tvdb_png = ensure_png_from_svg(tvdb_path, out_size=96); tvdb_path = tvdb_png or None
    except Exception: tvdb_path = None

    meta_img = tk.PhotoImage(file=meta_path).subsample(2) if (meta_path and meta_path.lower().endswith(".png")) else None
    tmdb_img = tk.PhotoImage(file=tmdb_path).subsample(4) if (tmdb_path and tmdb_path.lower().endswith(".png")) else None
    tvdb_img = tk.PhotoImage(file=tvdb_path).subsample(4) if (tvdb_path and tvdb_path.lower().endswith(".png")) else None

    pad = {"padx": 10, "pady": 5}
    if meta_img:
        tk.Label(top, image=meta_img, bg=SPLASH_BG).pack(**pad)
    else:
        tk.Label(top, text=APP_NAME, font=("Segoe UI", 18, "bold"), fg=SPLASH_TEXT, bg=SPLASH_BG).pack(**pad)

    tk.Label(top,
             text=("This MetaSeries-Organiser uses TMDB and the TMDB APIs but is not endorsed, "
                   "certified, or otherwise approved by TMDB"),
             wraplength=500, justify="center",
             fg=SPLASH_TEXT, bg=SPLASH_BG, font=("Segoe UI", 9)).pack(**pad)

    tk.Label(top,
             text="Metadata provided by TheTVDB. Please consider adding missing information or subscribing.",
             wraplength=500, justify="center",
             fg=SPLASH_TEXT, bg=SPLASH_BG, font=("Segoe UI", 9)).pack(**pad)

    if tmdb_img or tvdb_img:
        logo_frame = tk.Frame(top, bg=SPLASH_BG); logo_frame.pack(pady=6)
        if tmdb_img: tk.Label(logo_frame, image=tmdb_img, bg=SPLASH_BG).pack(side="left", padx=6)
        if tvdb_img: tk.Label(logo_frame, image=tvdb_img, bg=SPLASH_BG).pack(side="left", padx=6)

    tk.Button(top, text="Continue", command=top.destroy, bg=ACCENT, fg=SPLASH_TEXT, relief=tk.FLAT).pack(pady=10)
    top.after(4000, top.destroy)
    top._meta_img = meta_img; top._tmdb_img = tmdb_img; top._tvdb_img = tvdb_img
    top.grab_set(); root.wait_window(top)

class LogPanel:
    def __init__(self, parent: tk.Widget, wrap: bool = True):
        self.frame = ttk.Frame(parent)
        self.wrap = wrap
        self.text = tk.Text(self.frame, height=16, width=100, wrap=tk.WORD if wrap else tk.NONE)
        self.text.configure(font=("Consolas", 10))
        self.vsb = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.hsb = ttk.Scrollbar(self.frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        self.frame.grid_rowconfigure(0, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)

    def widget(self) -> tk.Widget:
        return self.frame

    def log(self, message: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {message}\n"
        self.text.insert("end", line)
        self.text.see("end")
        try:
            logger.info(message)
        except Exception:
            pass

    def copy_all(self):
        self.text.event_generate("<<SelectAll>>")
        self.text.event_generate("<<Copy>>")
        self.text.tag_remove("sel", "1.0", "end")

    def save_to_file(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="MSO-Logs.txt")
        if not p:
            return
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.text.get("1.0", "end"))

    def toggle_wrap(self):
        self.wrap = not self.wrap
        self.text.configure(wrap=(tk.WORD if self.wrap else tk.NONE))

def build_gui_tk():
    settings = load_settings()
    root = tk.Tk()
    root.title(APP_NAME)
    ww, wh = settings.get("last_window_size", [900, 620])
    root.geometry(f"{ww}x{wh}+100+100")

    show_splash_tk(root, skip=("--nosplash" in sys.argv))

    # Vars
    src_var = tk.StringVar()
    dst_var = tk.StringVar()
    title_var = tk.StringVar()
    year_var = tk.StringVar()
    series_id_var = tk.StringVar()
    provider_var = tk.StringVar(value="TVDB")
    tvdb_token_var = tk.StringVar()
    tvdb_pin_var = tk.StringVar()
    tmdb_key_var = tk.StringVar()
    lang_var = tk.StringVar(value=settings.get("preferred_language","en"))
    order_var = tk.StringVar(value=settings.get("order","aired"))
    upper_var = tk.BooleanVar(value=True)
    dry_var = tk.BooleanVar(value=True)
    nfo_var = tk.BooleanVar(value=False)
    wrap_var = tk.BooleanVar(value=bool(settings.get("wrap_log", True)))
    portable_var = tk.BooleanVar(value=bool(settings.get("portable", portable_mode_enabled())))

    # Layout grid setup
    for i in range(3):
        root.grid_columnconfigure(i, weight=1 if i == 1 else 0)
    root.grid_rowconfigure(11, weight=1)

    def row(r, text, widget):
        ttk.Label(root, text=text).grid(row=r, column=0, sticky="w", padx=6, pady=4)
        widget.grid(row=r, column=1, sticky="we", padx=6, pady=4)

    # Widgets
    row(0, "Source folder:", ttk.Entry(root, textvariable=src_var, width=50))
    ttk.Button(root, text="Browse", command=lambda: src_var.set(filedialog.askdirectory() or src_var.get())).grid(row=0, column=2, padx=6)

    row(1, "Destination folder (optional):", ttk.Entry(root, textvariable=dst_var, width=50))
    ttk.Button(root, text="Browse", command=lambda: dst_var.set(filedialog.askdirectory() or dst_var.get())).grid(row=1, column=2, padx=6)

    row(2, "Show Title:", ttk.Entry(root, textvariable=title_var, width=50))
    row(3, "Year (optional):", ttk.Entry(root, textvariable=year_var, width=10))
    row(4, "Series ID (optional):", ttk.Entry(root, textvariable=series_id_var, width=20))

    provider_frame = ttk.Frame(root)
    ttk.Combobox(provider_frame, textvariable=provider_var, values=["TVDB", "TMDB"], width=12, state="readonly").pack(side="left", padx=4)
    ttk.Label(provider_frame, text="Language:").pack(side="left")
    ttk.Entry(provider_frame, textvariable=lang_var, width=6).pack(side="left", padx=4)
    ttk.Label(provider_frame, text="Order:").pack(side="left")
    ttk.Combobox(provider_frame, textvariable=order_var, values=["aired","dvd","absolute"], state="readonly", width=10).pack(side="left", padx=4)
    row(5, "Provider / Lang / Order:", provider_frame)

    creds_frame = ttk.Frame(root)
    ttk.Label(creds_frame, text="TVDB API Key:").pack(side="left")
    ttk.Entry(creds_frame, textvariable=tvdb_token_var, width=28, show="•").pack(side="left", padx=6)
    ttk.Label(creds_frame, text="TMDB Key:").pack(side="left")
    ttk.Entry(creds_frame, textvariable=tmdb_key_var, width=24, show="•").pack(side="left", padx=6)
    row(6, "API credentials:", creds_frame)
    row(7, "TVDB PIN (if issued):", ttk.Entry(root, textvariable=tvdb_pin_var, width=18, show="•"))

    ttk.Checkbutton(root, text="Uppercase S/E codes (S01E01)", variable=upper_var).grid(row=8, column=0, columnspan=2, sticky="w", padx=6)
    ttk.Checkbutton(root, text="Dry run (don’t move, just log)", variable=dry_var).grid(row=9, column=0, columnspan=2, sticky="w", padx=6)
    ttk.Checkbutton(root, text="Write .nfo sidecar files", variable=nfo_var).grid(row=8, column=2, sticky="w", padx=6)
    ttk.Checkbutton(root, text="Portable Mode (store state locally)", variable=portable_var).grid(row=9, column=2, sticky="w", padx=6)

    # Log panel + controls
    ttk.Label(root, text="Log:").grid(row=11, column=0, sticky="nw", padx=6)
    logpanel = LogPanel(root, wrap=wrap_var.get())
    logpanel.widget().grid(row=11, column=1, columnspan=2, sticky="nsew", padx=6, pady=6)
    btns = ttk.Frame(root)
    ttk.Button(btns, text="Copy Log", command=logpanel.copy_all).pack(side="left", padx=4)
    ttk.Button(btns, text="Save Log", command=logpanel.save_to_file).pack(side="left", padx=4)
    ttk.Checkbutton(btns, text="Wrap", variable=wrap_var, command=logpanel.toggle_wrap).pack(side="left", padx=4)
    def undo_last_run_ui():
        manifest = latest_manifest_path()
        if not manifest:
            logpanel.log("[UNDO] No run manifest found.")
            return
        threading.Thread(target=lambda: undo_from_manifest(manifest,
            lambda m: root.after(0, lambda msg=m: logpanel.log(msg))), daemon=True).start()
    ttk.Button(btns, text="Undo last run", command=undo_last_run_ui).pack(side="left", padx=8)
    btns.grid(row=12, column=1, sticky="w", padx=6, pady=4)

    # Credentials store
    creds = CredentialsStore(portable=portable_var.get())

    def ask_passphrase_if_needed():
        if portable_var.get():
            pw = os.environ.get("MSO_VAULT_PASSPHRASE")
            if not pw:
                pw = simpledialog.askstring("Vault Passphrase", "Enter passphrase for portable credentials vault:", show="•")
            if not pw:
                messagebox.showwarning("Vault", "No passphrase set. Credentials will not be saved.")
                return None
            creds.set_session_passphrase(pw)
        return True

    # Try load stored creds
    try:
        ask_passphrase_if_needed()
        tv = creds.get("tvdb_token")
        if tv: tvdb_token_var.set(tv)
        tp = creds.get("tvdb_pin")
        if tp: tvdb_pin_var.set(tp)
        tm = creds.get("tmdb_key")
        if tm: tmdb_key_var.set(tm)
    except Exception as e:
        logpanel.log(f"Credential load warning: {e}")

    def save_creds():
        try:
            ask_passphrase_if_needed()
            if tvdb_token_var.get().strip():
                creds.set("tvdb_token", tvdb_token_var.get().strip())
            if tvdb_pin_var.get().strip():
                creds.set("tvdb_pin", tvdb_pin_var.get().strip())
            if tmdb_key_var.get().strip():
                creds.set("tmdb_key", tmdb_key_var.get().strip())
            messagebox.showinfo("Credentials", "Saved.")
        except Exception as e:
            messagebox.showwarning("Credentials", f"Failed to save: {e}")

    ttk.Button(root, text="Save Credentials", command=save_creds).grid(row=6, column=2, sticky="e", padx=6)

    # Dynamic scaling
    base_label_font = tkfont.Font(family="Segoe UI", size=10)
    base_entry_font = tkfont.Font(family="Segoe UI", size=10)
    base_button_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
    base_log_font = tkfont.Font(family="Consolas", size=10)
    root.option_add("*Font", base_label_font)

    ref_w = 900
    def _apply_scale(w, h):
        scale = max(0.8, min(2.5, (w / ref_w)))
        s = max(8, int(10 * scale))
        base_label_font.configure(size=s); base_entry_font.configure(size=s); base_button_font.configure(size=s); base_log_font.configure(size=s)

    def _on_resize(event):
        _apply_scale(root.winfo_width(), root.winfo_height())
    root.bind("<Configure>", _on_resize)

    # Manual resolver dialog
    def manual_resolve_dialog(candidates: List[Tuple[float, EpisodeInfo]], item: Optional[PlanItem] = None) -> Optional[EpisodeInfo]:
        dlg = tk.Toplevel(root); dlg.title("Resolve Episode"); dlg.geometry("700x400+150+150")
        ttk.Label(dlg, text="Choose the best match (type to filter):").pack(anchor="w", padx=8, pady=4)
        filter_var = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=filter_var); ent.pack(fill="x", padx=8)
        cols = ("score","season","episode","title","air","overview")
        tv = ttk.Treeview(dlg, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            tv.heading(c, text=c.capitalize())
            tv.column(c, width=70 if c in ("score","season","episode") else (220 if c=="title" else 170), stretch=(c in ("title","overview")))
        vs = ttk.Scrollbar(dlg, orient="vertical", command=tv.yview); tv.configure(yscrollcommand=vs.set)
        tv.pack(side="left", fill="both", expand=True, padx=(8,0), pady=8); vs.pack(side="left", fill="y", pady=8)
        preview_var = tk.StringVar(value="Select a match to preview the output filename.")
        ttk.Label(dlg, textvariable=preview_var, wraplength=680).pack(fill="x", padx=8, pady=3)
        # populate
        rows = []
        for sc, ep in candidates[:80]:
            rows.append((f"{sc:.2f}", ep.season, ep.episode, ep.title or "", ep.air_date or "", (ep.overview or "")[:280]))
        def refresh():
            tv.delete(*tv.get_children())
            q = _normalize_title(filter_var.get())
            for row in rows:
                if q and q not in _normalize_title(row[3] + " " + row[5]):
                    continue
                tv.insert("", "end", values=row)
        refresh()
        filter_var.trace_add("write", lambda *_: refresh())
        chosen = {"v": None}
        def preview(_event=None):
            cur = tv.focus()
            if not cur: return
            vals = tv.item(cur, "values")
            for score, ep in candidates:
                if ep.season == int(vals[1]) and ep.episode == int(vals[2]) and (ep.title or "") == vals[3]:
                    if item:
                        name = target_filename(item.show_title, item.year, ep, item.src.suffix,
                                                upper_se=upper_var.get(), order=order_var.get())
                        preview_var.set(f"Preview: {format_season_folder(ep.season)} / {name}")
                    break
        tv.bind("<<TreeviewSelect>>", preview)
        def ok():
            cur = tv.focus()
            if not cur: 
                messagebox.showinfo("Pick one", "Select a row."); return
            vals = tv.item(cur,"values")
            s, e, title, air = int(vals[1]), int(vals[2]), vals[3], vals[4]
            for sc, epi in candidates:
                if epi.season==s and epi.episode==e and (epi.title or "")==title:
                    chosen["v"] = epi; break
            dlg.destroy()
        ttk.Button(dlg, text="OK", command=ok).pack(pady=6)
        dlg.transient(root); dlg.grab_set(); root.wait_window(dlg)
        return chosen["v"]

    # Review & Approve dialog
    def review_and_approve(plan: List[PlanItem]) -> List[PlanItem]:
        dlg = tk.Toplevel(root); dlg.title("Review & Approve"); dlg.geometry("900x420+140+140")
        cols = ("approve","original","proposed","tags")
        tv = ttk.Treeview(dlg, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            tv.heading(c, text=c.capitalize())
            tv.column(c, width=65 if c=="approve" else (360 if c in ("original","proposed") else 130), stretch=(c in ("original","proposed")))
        vs = ttk.Scrollbar(dlg, orient="vertical", command=tv.yview); tv.configure(yscrollcommand=vs.set)
        tv.pack(side="left", fill="both", expand=True, padx=(8,0), pady=8); vs.pack(side="left", fill="y", pady=8)

        def tag_for(it: PlanItem) -> str:
            tags = list(it.tags or [])
            if it.dst.exists(): tags.append("conflict")
            return ", ".join(tags)

        for i, it in enumerate(plan):
            tv.insert("", "end", iid=str(i), values=("✓" if it.approved else "", str(it.src), str(it.dst), tag_for(it)))

        counts = {}
        for it in plan:
            counts[str(it.dst)] = counts.get(str(it.dst), 0) + 1
        conflict_count = sum(1 for it in plan if it.dst.exists() or counts[str(it.dst)] > 1)
        summary = ttk.Label(dlg, text=f"{len(plan)} proposed · {getattr(plan, 'skipped_count', 0)} skipped · {conflict_count} destination conflict(s)")
        summary.pack(side="bottom", fill="x", padx=8, pady=4)

        def resolve_selected():
            cur = tv.focus()
            if not cur: return
            idx = int(cur); item = plan[idx]
            options = item.candidates or []
            if not options:
                messagebox.showinfo("Resolver", "No alternative title matches were found for this file.", parent=dlg)
                return
            ep = manual_resolve_dialog(options, item)
            if ep:
                item.episode = ep
                item.dst = item.dst.parent.parent / format_season_folder(ep.season) / target_filename(
                    item.show_title, item.year, ep, item.src.suffix, upper_se=upper_var.get(), order=order_var.get())
                item.nfo = item.dst.with_suffix(".nfo") if nfo_var.get() else None
                tv.set(cur, "proposed", str(item.dst))
                tv.set(cur, "tags", tag_for(item))
            else:
                item.approved = False
                tv.set(cur, "approve", "")

        # Ask for a human choice when the best title score is weak or the top
        # alternatives are close enough to be genuinely ambiguous.
        for idx, item in enumerate(plan):
            options = item.candidates or []
            if options and (options[0][0] < 0.88 or (len(options) > 1 and options[0][0] - options[1][0] < 0.07)):
                choice = manual_resolve_dialog(options, item)
                if choice:
                    item.episode = choice
                    item.dst = item.dst.parent.parent / format_season_folder(choice.season) / target_filename(
                        item.show_title, item.year, choice, item.src.suffix, upper_se=upper_var.get(), order=order_var.get())
                    item.nfo = item.dst.with_suffix(".nfo") if nfo_var.get() else None
                    tv.set(str(idx), "proposed", str(item.dst))
                    tv.set(str(idx), "tags", tag_for(item))
                else:
                    item.approved = False
                    tv.set(str(idx), "approve", "")

        def toggle_selected():
            cur = tv.focus()
            if not cur: return
            idx = int(cur)
            plan[idx].approved = not plan[idx].approved
            tv.set(cur, "approve", "✓" if plan[idx].approved else "")

        def approve_all():
            for i in range(len(plan)):
                plan[i].approved = True
                tv.set(str(i), "approve", "✓")

        ttk.Button(dlg, text="Toggle selected", command=toggle_selected).pack(side="left", padx=8, pady=6)
        ttk.Button(dlg, text="Resolve episode…", command=resolve_selected).pack(side="left", padx=8, pady=6)
        ttk.Button(dlg, text="Approve All", command=approve_all).pack(side="left", padx=8, pady=6)
        done = {"ok": False}
        def ok():
            done["ok"] = True; dlg.destroy()
        ttk.Button(dlg, text="Commit", command=ok).pack(side="right", padx=8, pady=6)
        dlg.transient(root); dlg.grab_set(); root.wait_window(dlg)
        return plan if done["ok"] else []

    def choose_series_dialog(candidates):
        dlg = tk.Toplevel(root); dlg.title("Choose TV series"); dlg.geometry("760x360")
        ttk.Label(dlg, text="Several series match. Select the correct show:").pack(anchor="w", padx=8, pady=6)
        cols = ("title", "year", "network", "overview")
        tv = ttk.Treeview(dlg, columns=cols, show="headings", selectmode="browse")
        for col, width in (("title", 180), ("year", 75), ("network", 130), ("overview", 340)):
            tv.heading(col, text=col.title()); tv.column(col, width=width, stretch=(col=="overview"))
        tv.pack(fill="both", expand=True, padx=8, pady=4)
        for i, item in enumerate(candidates):
            year = str(item.get("year") or item.get("firstAired") or item.get("first_air_date") or "")[:4]
            tv.insert("", "end", iid=str(i), values=(item.get("name") or item.get("seriesName") or item.get("title") or "(untitled)", year,
                item.get("network") or item.get("originalNetwork") or "", (item.get("overview") or item.get("overview") or "")[:220]))
        selected = {"item": None}
        def accept():
            cur = tv.focus()
            if cur:
                selected["item"] = candidates[int(cur)]
                dlg.destroy()
        ttk.Button(dlg, text="Use selected series", command=accept).pack(pady=6)
        dlg.transient(root); dlg.grab_set(); root.wait_window(dlg)
        return selected["item"]

    def on_ui_and_wait(callback):
        result = {"value": None, "error": None}
        ready = threading.Event()
        def invoke():
            try: result["value"] = callback()
            except Exception as e: result["error"] = e
            finally: ready.set()
        root.after(0, invoke)
        ready.wait()
        if result["error"]: raise result["error"]
        return result["value"]

    def start():
        if start_btn.instate(["disabled"]):
            return
        start_btn.configure(state="disabled")
        src = pathlib.Path(src_var.get().strip())
        dst = pathlib.Path(dst_var.get().strip()) if dst_var.get().strip() else None
        title = title_var.get().strip()
        year_s = year_var.get().strip()
        series_id = series_id_var.get().strip()
        upper = bool(upper_var.get()); dry = bool(dry_var.get()); write_sidecar = bool(nfo_var.get())
        provider_name = provider_var.get(); language = lang_var.get().strip() or "en"; order = order_var.get()
        tvdb_key_input = tvdb_token_var.get().strip()
        tvdb_pin_input = tvdb_pin_var.get().strip()
        tmdb_key_input = tmdb_key_var.get().strip()

        settings_now = load_settings()
        settings_now.update({"preferred_language": language, "order": order, "wrap_log": bool(wrap_var.get()), "portable": bool(portable_var.get())})
        save_settings(settings_now)

        def worker():
            def log(m): root.after(0, lambda msg=m: logpanel.log(msg))
            try:
                if not src.is_dir(): raise ValueError("Please choose a valid source folder.")
                show_title = title
                if not show_title and not series_id:
                    show_title = re.sub(r"(?i)\bseason\s*\d{1,2}\b", "", src.name).strip()
                    root.after(0, lambda t=show_title: title_var.set(t))
                year = int(year_s) if year_s.isdigit() else None
                if provider_name == "TVDB":
                    key = tvdb_key_input or creds.get("tvdb_token")
                    if not key: raise ProviderError("Enter your TVDB API key.")
                    provider = TVDBProvider(key, language=language, pin=tvdb_pin_input)
                else:
                    key = tmdb_key_input or creds.get("tmdb_key")
                    if not key: raise ProviderError("Enter your TMDB API key.")
                    provider = TMDBProvider(key, language=language)
                if series_id:
                    show_id = series_id
                    if not show_title or not year:
                        info = provider.show_info(show_id)
                        if not show_title: show_title = info.get("name") or info.get("seriesName") or info.get("title") or show_title
                        if not year:
                            y = str(info.get("firstAired") or info.get("first_air_date") or "")[:4]
                            if y.isdigit(): year = int(y)
                else:
                    result = provider.search_show(show_title, year)
                    candidates = result.get("_candidates", [result])
                    if len(candidates) > 1:
                        result = on_ui_and_wait(lambda: choose_series_dialog(candidates))
                        if result is None: raise ProviderError("Series selection cancelled.")
                    show_id = str(result.get("id") or result.get("tvdb_id") or result.get("series", {}).get("id") or result.get("tvdbId") or "")
                    show_title = result.get("name") or result.get("seriesName") or result.get("title") or show_title
                    if not year:
                        y = str(result.get("firstAired") or result.get("year") or result.get("first_air_date") or "")[:4]
                        if y.isdigit(): year = int(y)
                    if not show_id: raise ProviderError("Could not resolve Series ID from provider search.")
                if not show_title: raise ProviderError("The selected series has no usable title.")
                root.after(0, lambda t=show_title: title_var.set(t))
                if year: root.after(0, lambda y=year: year_var.set(str(y)))
                ctx = ShowContext(title=show_title, year=year, provider_name=provider_name, provider=provider, show_id=show_id, episode_index=[], order=order)
                log(f"Provider: {provider_name} | Show ID: {show_id} | Order: {order} | Lang: {language}")
                log(f"Planning from: {src}")
                plan = plan_moves(ctx, src, dst, log, upper, write_sidecar)
                if not plan: log("Nothing to do."); return
                plan = on_ui_and_wait(lambda: review_and_approve(plan))
                if not plan: log("Cancelled."); return
                if dry:
                    for item in plan:
                        if item.approved: log(f"DRY-RUN: {item.src} -> {item.dst}")
                    log("Dry run complete."); return
                manifest_path = execute_plan(plan, log)
                log(f"Manifest saved: {manifest_path}")
            except Exception as e:
                log(f"Run failed: {type(e).__name__}: {e}")
                root.after(0, lambda msg=str(e): messagebox.showerror("MetaSeries Organiser", msg))
            finally:
                root.after(0, lambda: start_btn.configure(state="normal"))
        threading.Thread(target=worker, daemon=True).start()

    start_btn = ttk.Button(root, text="Start", command=start)
    start_btn.grid(row=13, column=1, pady=8, sticky="e")
    ttk.Button(root, text="Exit", command=lambda: (settings.update({"last_window_size":[root.winfo_width(), root.winfo_height()]}), save_settings(settings), root.destroy())).grid(row=13, column=2, pady=8, sticky="e")

    root.minsize(780, 540)
    root.mainloop()

# ------------ PSG path (basic compatibility) ------------
def build_gui_psg():
    if sg is None:
        print("PySimpleGUI not available."); return
    # Keep the simpler V1.5 layout; advanced features are in Tk UI
    layout = [
        [sg.Text("Source folder:"), sg.Input(key="src"), sg.FolderBrowse()],
        [sg.Text("Destination root (optional):"), sg.Input(key="dst"), sg.FolderBrowse()],
        [sg.Text("Show Title:"), sg.Input(key="title", size=(40,1)), sg.Text("Year:"), sg.Input(key="year", size=(8,1))],
        [sg.Text("Series ID:"), sg.Input(key="series_id", size=(20,1))],
        [sg.Text("Provider:"), sg.Combo(["TVDB","TMDB"], key="provider", default_value="TVDB"), sg.Text("Language:"), sg.Input(key="lang", size=(6,1), default_text="en"),
         sg.Text("Order:"), sg.Combo(["aired","dvd","absolute"], key="order", default_value="aired", size=(10,1))],
        [sg.Text("TVDB Token:"), sg.Input(key="tvdb", password_char="•", size=(28,1)), sg.Text("TMDB Key:"), sg.Input(key="tmdb", password_char="•", size=(24,1))],
        [sg.Checkbox("Uppercase S/E", key="upper", default=True), sg.Checkbox("Dry run", key="dry", default=True), sg.Checkbox("Write .nfo", key="nfo", default=False)],
        [sg.Button("Start"), sg.Button("Exit")],
        [sg.Multiline(key="log", size=(100,20), autoscroll=True, disabled=False)]
    ]
    win = sg.Window(APP_NAME, layout, finalize=True)
    while True:
        ev, v = win.read()
        if ev in (sg.WINDOW_CLOSED, "Exit"):
            break
        if ev == "Start":
            try:
                src = pathlib.Path(v.get("src") or "")
                dst = pathlib.Path(v.get("dst")) if v.get("dst") else None
                title = (v.get("title") or "").strip()
                year_s = (v.get("year") or "").strip()
                series_id = (v.get("series_id") or "").strip()
                provider_name = v.get("provider") or "TVDB"
                language = (v.get("lang") or "en").strip()
                order = v.get("order") or "aired"
                tvdb_token = (v.get("tvdb") or "").strip()
                tmdb_key = (v.get("tmdb") or "").strip()
                upper = bool(v.get("upper"))
                dry = bool(v.get("dry"))
                nfo = bool(v.get("nfo"))

                if not src.exists():
                    win["log"].print("Invalid source"); continue
                year = int(year_s) if year_s.isdigit() else None

                if provider_name == "TVDB":
                    provider = TVDBProvider(tvdb_token, language=language)
                else:
                    provider = TMDBProvider(tmdb_key, language=language)

                if series_id:
                    show_id = series_id
                    info = provider.show_info(show_id)
                    if not title:
                        title = info.get("name") or info.get("seriesName") or title
                    if not year:
                        y = (info.get("firstAired") or info.get("first_air_date") or "")[:4]
                        if str(y).isdigit(): year = int(y)
                else:
                    show = provider.search_show(title, year)
                    show_id = str(show.get("id") or show.get("tvdb_id") or show.get("series",{}).get("id") or "")
                    if not year:
                        y = (show.get("firstAired") or show.get("year") or show.get("first_air_date") or "")[:4]
                        if str(y).isdigit(): year = int(y)

                ctx = ShowContext(title=title, year=year, provider_name=provider_name, provider=provider, show_id=show_id, episode_index=[], order=order)
                def logfn(m): win["log"].print(m)
                plan = plan_moves(ctx, src, dst, logfn, upper, nfo)
                if dry:
                    for it in plan:
                        if it.approved:
                            win["log"].print(f"DRY-RUN: {it.src} -> {it.dst}")
                else:
                    execute_plan(plan, logfn)
                win["log"].print("Done.")
            except Exception as e:
                win["log"].print(f"Error: {e}")
    win.close()

# ------------ Entry ------------
def main():
    # Prefer Tk UI for V2 features; use PSG if --psg is passed explicitly
    USE_PSG = ("--psg" in sys.argv) and (sg is not None)
    if USE_PSG:
        build_gui_psg()
    else:
        build_gui_tk()

if __name__ == "__main__":
    main()
