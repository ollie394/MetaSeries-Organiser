#!/usr/bin/env python3
"""
MetaSeries-Organiser V3 — TV series renamer and sorter.

Requires requests. Install keyring for secure OS credential storage and
cryptography for the AES-GCM portable vault. PNG logos belong in ./assets.
Run with --nosplash to skip the splash; --psg uses the supported Tk workflow.

Moves are journalled, published without overwrite, checksum verified before
source deletion, and recoverable through Undo. Missing episode mappings are
skipped. Ambiguous title matches require a human choice before approval.
Choose Move or Copy in the UI. Copy creates independent verified files and
keeps the originals; Undo removes only the unchanged copies and new sidecars.

TVDB supports aired/DVD/absolute order with mappings matched by episode ID.
TMDB absolute numbers use regular aired episodes (excluding specials); use
TVDB for DVD order. Preferred titles fall back to English then original.

Portable state is stored in data beside the script/executable. Normal state
uses the user profile. Bundled resources use _MEIPASS independently of state.
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
import queue
import uuid
import tempfile
from urllib.parse import urlsplit, urlunsplit, parse_qs
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict, Any

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
    """Executable directory for persistent portable state, never _MEIPASS."""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent


def user_state_root() -> pathlib.Path:
    if os.name == "nt":
        base = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = pathlib.Path.home() / "Library" / "Application Support"
    else:
        base = pathlib.Path(os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local" / "share")
    return base / APP_NAME


_PORTABLE_OVERRIDE = None


def portable_mode_enabled() -> bool:
    if _PORTABLE_OVERRIDE is not None:
        return _PORTABLE_OVERRIDE
    if os.environ.get("MSO_PORTABLE") == "1" or (app_root() / "portable.flag").exists():
        return True
    for path in (user_state_root() / "config" / "settings.json", app_root() / "config" / "settings.json"):
        try:
            return bool(json.loads(path.read_text(encoding="utf-8")).get("portable", False))
        except (OSError, ValueError):
            continue
    return False


def state_root(portable: Optional[bool] = None) -> pathlib.Path:
    portable = portable_mode_enabled() if portable is None else portable
    return app_root() / "data" if portable else user_state_root()


def check_state_writable(portable: Optional[bool] = None):
    directory = state_root(portable)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=directory) as probe:
        probe.write(b"MSO")
        probe.flush()


def redact_secrets(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?i)Bearer\s+[^\s]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(api[_-]?key|access_token|refresh_token|token|pin|password|authorization)([=:\s]+)[^&\s,}]+", r"\1\2[redacted]", text)
    return text


class SecretFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact_secrets(record.getMessage())
        record.args = ()
        return True


logger = logging.getLogger("MSO-V3")
logger.setLevel(logging.INFO)
console = logging.StreamHandler()
console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
console.addFilter(SecretFilter())
logger.addHandler(console)
_file_handler = None


def configure_logging():
    global _file_handler
    directory = state_root() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    new = logging.FileHandler(directory / "MSO-Logs.txt", encoding="utf-8")
    new.setFormatter(logging.Formatter("%(asctime)s:%(levelname)s:%(message)s"))
    new.addFilter(SecretFilter())
    if _file_handler:
        logger.removeHandler(_file_handler)
        _file_handler.close()
    _file_handler = new
    logger.addHandler(new)

# ------------ Settings & Credentials ------------
DEFAULT_SETTINGS = {"portable": False, "preferred_language": "en", "order": "aired",
                    "operation": "move", "wrap_log": True, "last_window_size": [1000, 720]}


def settings_path(portable: Optional[bool] = None) -> pathlib.Path:
    return state_root(portable) / "config" / "settings.json"


def load_settings() -> Dict[str, Any]:
    out = DEFAULT_SETTINGS.copy()
    for path in (settings_path(), app_root() / "config" / "settings.json"):
        try:
            out.update(json.loads(path.read_text(encoding="utf-8")))
            break
        except (OSError, ValueError):
            continue
    out["portable"] = portable_mode_enabled()
    return out


def save_settings(settings: Dict[str, Any]) -> None:
    atomic_json(settings_path(), settings)
    # Bootstrap choice lives in the user profile, so a saved portable toggle
    # can be discovered before choosing the next run's state directory.
    if portable_mode_enabled():
        bootstrap = user_state_root() / "config" / "settings.json"
        atomic_json(bootstrap, settings)


def atomic_json(path: pathlib.Path, data: Dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = pathlib.Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            json.dump(data, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

class CredentialsStore:
    """Keyring in normal mode, AES-GCM encrypted passphrase vault in portable mode."""
    def __init__(self, portable: bool):
        self.portable = portable
        self.vault_path = state_root(portable) / "vault" / "creds.json.enc"
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
        if self._passphrase == pw:
            return
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
            try:
                value = self._memory.get(key)
                return value if value is not None else self._keyring.get_password(APP_NAME, key)
            except Exception as e: logger.warning("Keyring read failed: %s", e)
        return self._memory.get(key)

    def set(self, key: str, value: str) -> bool:
        """Return True only when the credential was persisted securely."""
        if self.portable:
            self._load_vault()
            self._cached[key] = value
            self._save_vault()
            return True
        if self._keyring:
            try:
                self._keyring.set_password(APP_NAME, key, value)
                self._memory.pop(key, None)
                return True
            except Exception:
                logger.warning("Keyring write failed; retaining credential for this session only.")
        self._memory[key] = value
        return False

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
INLINE_EXTRA_RE = re.compile(rf"(?i)-(?P<etype>{'|'.join(EXTRA_SUFFIXES)})(?P<idx>\d+)?(?=(?:-(?:{'|'.join(SPLIT_TOKENS)})\d+)?\.[^.]+$)")

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
        kind = "se_range" if e2 else ("special" if s == 0 else "se")
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
    provider_id: Optional[str] = None

class Provider:
    def __init__(self, language: str = "en"):
        self.language = language
        self.cancel_event = None
        self.progress = lambda message: None

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

class Cancelled(Exception):
    pass


def check_cancel(event=None):
    if event is not None and event.is_set():
        raise Cancelled("Cancelled.")


def request_with_backoff(sess: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    event = kwargs.pop("cancel_event", None)
    timeout = kwargs.pop("timeout", (5, 15))
    safe_url = urlunsplit((*urlsplit(url)[:3], "", ""))
    for attempt in range(6):
        check_cancel(event)
        try:
            response = sess.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == 5:
                raise ProviderError(f"Network request failed: {method} {safe_url}") from None
            response = None
        else:
            if response.status_code not in (429, 500, 502, 503, 504):
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    # Construct a safe exception; never include response.request URL.
                    raise requests.HTTPError(f"HTTP {response.status_code}: {method} {safe_url}", response=response) from None
                return response
            if attempt == 5:
                raise ProviderError(f"HTTP {response.status_code}: retry limit reached for {safe_url}")
        delay = backoff_sleep(attempt)
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    pass
        delay = max(0.0, delay)
        logger.warning("Request to %s will retry in %.1fs", safe_url, delay)
        if event is not None:
            if event.wait(delay):
                raise Cancelled("Cancelled during retry.")
        else:
            time.sleep(delay)

# ------------ TVDB v4 Provider ------------
class TVDBProvider(Provider):
    """TVDB v4 API-key exchange, official episode catalogue and order mappings."""
    def __init__(self, token: str, language: str = "en", pin: str = "", cancel_event=None):
        super().__init__(language)
        self.cancel_event = cancel_event
        if not token:
            raise ProviderError("TVDB API key is required.")
        self.base = "https://api4.thetvdb.com/v4"
        self.session = requests.Session()
        self.api_key = None if token.count(".") == 2 else token
        self.pin = pin
        self._shows = {}
        self._indices = {}
        self._languages = None
        if self.api_key:
            self._login()
        else:
            self._set_jwt(token)

    def _login(self):
        body = {"apikey": self.api_key}
        if self.pin:
            body["pin"] = self.pin
        response = request_with_backoff(self.session, "POST", self.base + "/login",
                                        json=body, cancel_event=self.cancel_event)
        token = (response.json().get("data") or {}).get("token")
        if not token:
            raise ProviderError("TVDB login returned no session token.")
        self._set_jwt(token)

    def _set_jwt(self, token):
        self.session.headers.update({"Authorization": "Bearer " + token})

    def _get(self, path, params=None):
        try:
            response = request_with_backoff(self.session, "GET", self.base + path,
                                            params=params or {}, cancel_event=self.cancel_event)
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 401 or not self.api_key:
                raise
            self._login()
            response = request_with_backoff(self.session, "GET", self.base + path,
                                            params=params or {}, cancel_event=self.cancel_event)
        try:
            return response.json()
        except ValueError:
            raise ProviderError("TVDB returned invalid JSON.") from None

    def _optional_get(self, path, params=None):
        try:
            return self._get(path, params)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return {}
            raise

    def _language_codes(self):
        preferred = self.language.split("-")[0].lower()
        common = {"en":"eng", "fr":"fra", "de":"deu", "es":"spa", "it":"ita", "ja":"jpn",
                  "pt":"por", "zh":"zho", "nl":"nld", "ko":"kor", "ru":"rus", "pl":"pol"}
        if len(preferred) == 2 and preferred not in common:
            if self._languages is None:
                self._languages = self._get("/languages").get("data", [])
            for item in self._languages:
                if item.get("shortCode") == preferred or item.get("iso639_1") == preferred:
                    preferred = item["id"]
                    break
        preferred = common.get(preferred, preferred)
        if len(preferred) != 3:
            raise ProviderError("TVDB requires a supported language code (for example en or eng).")
        return list(dict.fromkeys([preferred, "eng"]))

    def search_show(self, title, year=None):
        results = self._get("/search", {"query": title, "type": "series"}).get("data") or []
        if year:
            exact = [r for r in results if str(r.get("year") or r.get("firstAired") or "")[:4] == str(year)]
            results = exact or results
        if not results:
            raise ProviderError("Show not found on TVDB.")
        return {"_candidates": results} if len(results) > 1 else results[0]

    def show_info(self, show_id):
        if show_id not in self._shows:
            original = self._get(f"/series/{show_id}/extended").get("data") or {}
            result = dict(original)
            translations = []
            for language in self._language_codes():
                translated = self._optional_get(f"/series/{show_id}/translations/{language}").get("data") or {}
                translations.append(translated)
            for key in ("name", "overview"):
                result[key] = next((t.get(key) for t in translations if t.get(key)), original.get(key))
            self._shows[show_id] = result
        return self._shows[show_id]

    def list_seasons_official(self, show_id):
        seasons = self.show_info(show_id).get("seasons") or []
        nums = []
        for season in seasons:
            typ = season.get("type") or {}
            slug = typ.get("type") or typ.get("name") or "" if isinstance(typ, dict) else str(typ)
            if str(slug).lower() in ("official", "aired", "aired order"):
                nums.append(int(season["number"]))
        return sorted(set(nums))

    def _list_episodes(self, show_id, season_type="official", season=None, language=None):
        path = f"/series/{show_id}/episodes/{season_type}"
        if language:
            path += "/" + language
        params = {"page": 0}
        if season is not None:
            params["season"] = season
        result = []
        visited = set()
        while True:
            check_cancel(self.cancel_event)
            page = params["page"]
            if page in visited:
                raise ProviderError("TVDB returned a pagination loop.")
            visited.add(page)
            self.progress(f"Loading TVDB {season_type} episodes, page {page + 1}")
            response = self._get(path, params)
            data = response.get("data") or {}
            episodes = data.get("episodes", []) if isinstance(data, dict) else data
            if not isinstance(episodes, list) or any(not isinstance(ep, dict) for ep in episodes):
                raise ProviderError("Unexpected TVDB episode response.")
            result.extend(episodes)
            nxt = (response.get("links") or {}).get("next")
            if nxt is None or nxt == "":
                break
            if isinstance(nxt, int) or str(nxt).isdigit():
                params["page"] = int(nxt)
            else:
                query = parse_qs(urlsplit(str(nxt)).query)
                if "page" not in query:
                    raise ProviderError("TVDB pagination link has no page number.")
                params["page"] = int(query["page"][0])
        return result

    def _list_episodes_official(self, show_id, season_number):
        return self._list_episodes(show_id, "official", season_number)

    def build_episode_index(self, show_id, order="aired"):
        cache_key = (show_id, order)
        if cache_key in self._indices:
            return self._indices[cache_key]
        if (show_id, "aired") not in self._indices:
            original = self._list_episodes(show_id)
            translations = []
            for language in self._language_codes():
                try:
                    rows = self._list_episodes(show_id, language=language)
                except requests.HTTPError as exc:
                    if exc.response is None or exc.response.status_code != 404:
                        raise
                    rows = []
                translations.append({str(r.get("id")): r for r in rows})
            index = []
            for raw in original:
                ep = map_tvdb_episode(raw)
                for key in ("title", "overview"):
                    source_key = "name" if key == "title" else key
                    value = next((lookup.get(ep.provider_id, {}).get(source_key)
                                  for lookup in translations if lookup.get(ep.provider_id, {}).get(source_key)), getattr(ep, key))
                    setattr(ep, key, value)
                index.append(ep)
            index.sort(key=lambda e: (e.season, e.episode))
            self._indices[(show_id, "aired")] = index
        index = [replace(ep) for ep in self._indices[(show_id, "aired")]]
        if order in ("dvd", "absolute"):
            try:
                ordered = self._list_episodes(show_id, order)
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                ordered = []
            by_id = {str(r.get("id")): r for r in ordered}
            for ep in index:
                raw = by_id.get(ep.provider_id)
                if raw:
                    if order == "dvd":
                        ep.dvd_season = integer_or_none(raw.get("seasonNumber"))
                        ep.dvd_episode = integer_or_none(raw.get("number"))
                    elif not ep.is_special:
                        ep.absolute = integer_or_none(raw.get("number"))
            if order == "absolute":
                # Explicit provider numbers are required; never infer them from
                # list position, which may include specials or missing episodes.
                index.sort(key=lambda e: (e.absolute is None, e.absolute or 0, e.season, e.episode))
            else:
                index.sort(key=lambda e: episode_numbers(e, "dvd"))
        self._indices[cache_key] = index
        return index

    def load_season(self, show_id, season_number, order="aired"):
        episodes = self.build_episode_index(show_id, order)
        return {"episodes": [e.__dict__ for e in episodes if episode_numbers(e, order)[0] == season_number]}

    def map_date_to_episode(self, show_id, date_iso, order="aired"):
        matches = [e for e in self.build_episode_index(show_id, order) if e.air_date == date_iso]
        return matches[0] if len(matches) == 1 else None


def integer_or_none(value):
    if value in (None, ""):
        return None
    try:
        number = int(value)
        return number if float(value) == number else None
    except (ValueError, TypeError):
        return None


def map_tvdb_episode(raw):
    season = integer_or_none(raw.get("seasonNumber", raw.get("season_number"))) or 0
    number = integer_or_none(raw.get("number", raw.get("episodeNumber", raw.get("episode_number")))) or 0
    return EpisodeInfo(season=season, episode=number, title=raw.get("name") or raw.get("episodeName"),
                       air_date=str(raw.get("aired") or raw.get("firstAired") or "")[:10] or None,
                       is_special=season == 0, absolute=integer_or_none(raw.get("absoluteNumber")),
                       dvd_season=integer_or_none(raw.get("dvdSeason")),
                       dvd_episode=integer_or_none(raw.get("dvdEpisodeNumber")),
                       overview=raw.get("overview"), provider_id=str(raw.get("id")) if raw.get("id") is not None else None)


class TMDBProvider(Provider):
    def __init__(self, api_key, language="en", cancel_event=None):
        super().__init__(language)
        self.cancel_event = cancel_event
        if not api_key:
            raise ProviderError("TMDB API key is required.")
        self.base = "https://api.themoviedb.org/3"
        self.session = requests.Session()
        self.session.params.update({"api_key": api_key})
        self._shows = {}
        self._seasons = {}
        self._indices = {}

    def _get(self, path, params=None):
        response = request_with_backoff(self.session, "GET", self.base + path,
                                        params=params or {}, cancel_event=self.cancel_event)
        return response.json()

    def _languages(self, original=None):
        return list(dict.fromkeys([self.language or "en", "en", original or ""]))

    def search_show(self, title, year=None):
        results = []
        for language in self._languages():
            params = {"query": title, "language": language}
            if year:
                params["first_air_date_year"] = year
            results = self._get("/search/tv", params).get("results") or []
            if results:
                break
        if not results:
            raise ProviderError("Show not found on TMDB.")
        return {"_candidates": results} if len(results) > 1 else results[0]

    def show_info(self, show_id):
        if show_id not in self._shows:
            first = self._get(f"/tv/{show_id}", {"language": self.language})
            variants = [first]
            for language in self._languages(first.get("original_language"))[1:]:
                variants.append(self._get(f"/tv/{show_id}", {"language": language}))
            info = dict(first)
            for key in ("name", "overview"):
                info[key] = next((v.get(key) for v in variants if v.get(key)), first.get("original_name") if key == "name" else None)
            self._shows[show_id] = info
        return self._shows[show_id]

    def list_seasons_official(self, show_id):
        return sorted({int(s["season_number"]) for s in self.show_info(show_id).get("seasons", [])})

    def load_season(self, show_id, season_number, order="aired"):
        if order == "dvd":
            raise ProviderError("TMDB does not supply one authoritative DVD order. Select TVDB for DVD numbering.")
        key = (show_id, season_number)
        if key not in self._seasons:
            variants = []
            original = self.show_info(show_id).get("original_language")
            for language in self._languages(original):
                try:
                    variants.append(self._get(f"/tv/{show_id}/season/{season_number}", {"language": language}))
                except requests.HTTPError as exc:
                    if exc.response is None or exc.response.status_code != 404:
                        raise
            if not variants:
                self._seasons[key] = {"episodes": []}
            else:
                rows = []
                lookups = [{e["episode_number"]: e for e in v.get("episodes", [])} for v in variants]
                numbers = sorted({number for lookup in lookups for number in lookup})
                for number in numbers:
                    raw = next(lookup[number] for lookup in lookups if number in lookup)
                    ep = dict(raw)
                    for field_name in ("name", "overview"):
                        ep[field_name] = next((v.get(ep["episode_number"], {}).get(field_name) for v in lookups
                                               if v.get(ep["episode_number"], {}).get(field_name)), None)
                    rows.append(ep)
                self._seasons[key] = {"episodes": rows}
        return self._seasons[key]

    def build_episode_index(self, show_id, order="aired"):
        if order == "dvd":
            raise ProviderError("Select TVDB for DVD numbering.")
        if show_id not in self._indices:
            episodes = []
            for season in self.list_seasons_official(show_id):
                check_cancel(self.cancel_event)
                self.progress(f"Loading TMDB season {season}")
                for raw in self.load_season(show_id, season).get("episodes", []):
                    episodes.append(EpisodeInfo(season=season, episode=int(raw["episode_number"]),
                        title=raw.get("name"), air_date=raw.get("air_date"), is_special=season == 0,
                        overview=raw.get("overview"), provider_id=str(raw.get("id"))))
            episodes.sort(key=lambda e: (e.season, e.episode))
            # TMDB has no absolute field: define its fallback as regular aired
            # episodes only. Specials stay unnumbered and keep S00E codes.
            number = 0
            for ep in episodes:
                if not ep.is_special:
                    number += 1
                    ep.absolute = number
            self._indices[show_id] = episodes
        return self._indices[show_id]

    def map_date_to_episode(self, show_id, date_iso, order="aired"):
        matches = [e for e in self.build_episode_index(show_id, order) if e.air_date == date_iso]
        return matches[0] if len(matches) == 1 else None

# ------------ Renamer core ------------
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".webm", ".flv", ".vob", ".ogm"}


def filename_units(text):
    return max(len(text.encode("utf-16-le")) // 2, len(text.encode("utf-8")))


def truncate_component(text, limit):
    while text and filename_units(text) > limit:
        text = text[:-1]
    return text.rstrip(" .")


def sanitize_filename(text, limit=220):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", str(text))
    text = re.sub(r"\s+", " ", text).strip(" .") or "Untitled"
    if re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", text):
        text = "_" + text
    return truncate_component(text, limit) or "_"


def fit_filename_stem(stem, limit, append=""):
    stem = sanitize_filename(stem, 1000)
    token_pattern = "|".join(EXTRA_SUFFIXES + SPLIT_TOKENS)
    match = re.search(rf"((?:-(?:{token_pattern})\d*)+)$", stem, re.I)
    tail = (match.group(1) if match else "") + append
    prefix = stem[:match.start()] if match else stem
    budget = limit - filename_units(tail)
    if budget < 20:
        raise ValueError("Filename suffixes leave insufficient room for the episode code.")
    code = re.search(r" - (?:[sS]\d+[eE]\d+(?:-[eE]\d+)?|\d{2}-\d{2}-\d{4}|\d{3,}(?:-\d+)?)", prefix)
    if code and filename_units(prefix) > budget:
        marker = code.group()
        show_budget = min(90, max(8, budget - filename_units(marker) - 25))
        head = truncate_component(prefix[:code.start()], show_budget) + marker
        prefix = head + truncate_component(prefix[code.end():], max(0, budget - filename_units(head)))
    return truncate_component(prefix, budget) + tail


def safe_destination(parent, filename, collision_suffix=""):
    parent = pathlib.Path(parent).resolve()
    # Conservative Windows limit, leaving room for NFO and copy/recovery paths.
    budget = min(220, 240 - len(str(parent).encode("utf-16-le")) // 2 - 1)
    suffix = pathlib.Path(filename).suffix
    if budget < 40:
        raise ValueError("Destination path is too long. Choose a shorter destination folder.")
    stem = fit_filename_stem(pathlib.Path(filename).stem, budget - filename_units(suffix), collision_suffix)
    return parent / (stem + suffix)


def same_path(first, second):
    return os.path.normcase(str(pathlib.Path(first).resolve())) == os.path.normcase(str(pathlib.Path(second).resolve()))


def path_key(path):
    return os.path.normcase(str(pathlib.Path(path).resolve()))


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def format_season_folder(number):
    return f"Season {number:02d}"


def episode_numbers(ep, order="aired"):
    if order == "dvd" and ep.dvd_season is not None and ep.dvd_episode is not None:
        return ep.dvd_season, ep.dvd_episode
    return ep.season, ep.episode


def target_filename(show_title, year, episode, ext, date_iso=None, extra=None,
                    multi_ep=None, upper_se=True, order="aired", split=None):
    show = sanitize_filename(f"{show_title} ({year})" if year else show_title, 90)
    s_char, e_char = ("S", "E") if upper_se else ("s", "e")
    season, number = episode_numbers(episode, order)
    if date_iso:
        code = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d-%m-%Y")
    elif order == "absolute" and not episode.is_special:
        if episode.absolute is None:
            raise ValueError("Episode has no verified absolute number.")
        code = f"{episode.absolute:03d}"
        if multi_ep:
            code = f"{multi_ep[0]:03d}-{multi_ep[1]:03d}"
    else:
        code = f"{s_char}{season:02d}{e_char}{number:02d}"
        if multi_ep:
            code = f"{s_char}{season:02d}{e_char}{multi_ep[0]:02d}-{e_char}{multi_ep[1]:02d}"
    name = f"{show} - {code}"
    if episode.title:
        name += " - " + sanitize_filename(episode.title, 100)
    if extra:
        name += "-" + sanitize_filename(extra, 30)
    if split:
        name += "-" + sanitize_filename(split, 20)
    return fit_filename_stem(name, 220 - filename_units(ext)) + ext.lower()


@dataclass
class ShowContext:
    title: str
    year: Optional[int]
    provider_name: str
    provider: Provider
    show_id: str
    episode_index: List[EpisodeInfo]
    order: str = "aired"


def resolve_episode_by_title(ctx, guess_title, season_hint=None):
    index = ctx.episode_index or ctx.provider.build_episode_index(ctx.show_id, ctx.order)
    matches = [(_title_similarity(guess_title, ep.title or ""), ep) for ep in index
               if season_hint is None or episode_numbers(ep, ctx.order)[0] == season_hint]
    matches.sort(key=lambda pair: pair[0], reverse=True)
    if matches and matches[0][0] >= 0.88 and (len(matches) == 1 or matches[0][0] - matches[1][0] >= 0.07):
        return matches[0][1]
    return None


def resolve_episode_from_absolute(ctx, abs_index):
    if abs_index <= 0:
        return None
    if not ctx.episode_index:
        ctx.episode_index = ctx.provider.build_episode_index(ctx.show_id, ctx.order)
    matches = [ep for ep in ctx.episode_index if not ep.is_special and ep.absolute == abs_index]
    if not matches and any(not ep.is_special and ep.absolute is None for ep in ctx.episode_index):
        absolute_index = ctx.provider.build_episode_index(ctx.show_id, "absolute")
        mappings = {ep.provider_id or (ep.season, ep.episode): ep.absolute for ep in absolute_index if not ep.is_special}
        for ep in ctx.episode_index:
            if not ep.is_special and ep.absolute is None:
                ep.absolute = mappings.get(ep.provider_id or (ep.season, ep.episode))
        matches = [ep for ep in ctx.episode_index if not ep.is_special and ep.absolute == abs_index]
    return matches[0] if len(matches) == 1 else None


# ------------ NFO sidecars ------------
def render_nfo(ep, show_title, year):
    esc = lambda value: html.escape(str(value if value is not None else "").strip(), quote=True)
    return (f'<episodedetails>\n  <title>{esc(ep.title)}</title>\n'
            f'  <showtitle>{esc(show_title)}</showtitle>\n'
            + (f'  <year>{year}</year>\n' if year else "")
            + f'  <season>{ep.season}</season>\n  <episode>{ep.episode}</episode>\n'
            + (f'  <aired>{esc(ep.air_date)}</aired>\n' if ep.air_date else "")
            + (f'  <plot>{esc(ep.overview)}</plot>\n' if ep.overview else "")
            + '</episodedetails>\n')


def nfo_bytes(episodes, show_title, year, order="aired"):
    mapped = []
    for ep in episodes:
        season, number = episode_numbers(ep, order)
        if order == "absolute" and not ep.is_special and ep.absolute is not None:
            season, number = 1, ep.absolute
        mapped.append(replace(ep, season=season, episode=number))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + ''.join(render_nfo(ep, show_title, year) for ep in mapped)).encode("utf-8")


def file_identity(path):
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino}


def owns_file(path, identity):
    return bool(identity and path.is_file() and file_identity(path) == identity)


def write_nfo(nfo_path, ep, show_title, year, episodes=None, order="aired", on_created=None):
    payload = nfo_bytes(episodes or [ep], show_title, year, order)
    # Exclusive creation prevents overwriting an existing sidecar, including
    # one created after review. Binary output gives the same hash on Windows.
    with nfo_path.open("xb") as stream:
        if on_created:
            on_created(file_identity(nfo_path))
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    actual = file_sha256(nfo_path)
    if actual != hashlib.sha256(payload).hexdigest():
        raise OSError("NFO verification failed.")
    return actual


# ------------ Manifest / Undo ------------
def manifests_dir():
    directory = state_root() / "manifests"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def latest_manifest_path():
    files = sorted(manifests_dir().glob("manifest-*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not data.get("undone") and any(it.get("status") in ("done", "copying", "verified", "failed", "writing", "undoing")
                                               for it in data.get("items", [])):
                return path
        except (OSError, ValueError):
            continue
    return None


def save_manifest_state(path, manifest):
    atomic_json(path, manifest)


def write_manifest(items, source_root):
    path = manifests_dir() / f"manifest-{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    save_manifest_state(path, {"version": 2, "source_root": str(source_root), "items": items})
    return path


def file_sha256(path, cancel_event=None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancel(cancel_event)
            digest.update(block)
    return digest.hexdigest()


def verified_copy(src, dst, expected, on_state, cancel_event=None):
    """Create an independent, verified copy; never unlink the source."""
    check_cancel(cancel_event)
    if not expected or file_sha256(src, cancel_event) != expected:
        raise OSError("Source changed after approval/hash recording.")
    source_identity = file_identity(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("xb") as target:
        on_state("copying", file_identity(dst))
        with src.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                check_cancel(cancel_event)
                target.write(block)
        target.flush()
        os.fsync(target.fileno())
    shutil.copystat(src, dst)
    if file_sha256(dst, cancel_event) != expected:
        raise OSError("Copy checksum mismatch; source retained.")
    on_state("verified", file_identity(dst))
    check_cancel(cancel_event)
    if file_identity(src) != source_identity or file_sha256(src, cancel_event) != expected:
        raise OSError("Source changed during copy; source retained.")
    on_state("done", file_identity(dst))


def verified_move(src, dst, expected, on_state, cancel_event=None):
    """Publish exclusively; verify destination BEFORE deleting source."""
    check_cancel(cancel_event)
    if not expected or file_sha256(src, cancel_event) != expected:
        raise OSError("Source changed after approval/hash recording.")
    source_identity = file_identity(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    linked = False
    try:
        os.link(src, dst)  # Fast same-volume path; fails rather than overwrites.
        linked = True
    except FileExistsError:
        raise
    except OSError:
        # Different volume or a filesystem without hard-link support.
        with dst.open("xb") as target:
            on_state("copying", file_identity(dst))
            with src.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    check_cancel(cancel_event)
                    target.write(block)
            target.flush()
            os.fsync(target.fileno())
        shutil.copystat(src, dst)
    if linked:
        on_state("copying", file_identity(dst))
    if file_sha256(dst, cancel_event) != expected:
        raise OSError("Destination hash mismatch; source retained.")
    on_state("verified", file_identity(dst))
    check_cancel(cancel_event)
    # Revalidate source identity/content before the irreversible unlink.
    if file_identity(src) != source_identity or file_sha256(src, cancel_event) != expected:
        raise OSError("Source changed during move; source retained.")
    src.unlink()
    on_state("done", file_identity(dst))


def undo_from_manifest(manifest_path, logfn, cancel_event=None, progress=None):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = data.get("items", [])
    failures = 0
    for number, it in enumerate(reversed(items), 1):
        check_cancel(cancel_event)
        if progress:
            progress(number, len(items), "Undo")
        status = it.get("status")
        if status in ("planned", "undone", "skipped", "skipped-conflict") or not status:
            continue
        try:
            expected = it.get("sha256")
            if not expected:
                raise OSError("No recorded checksum; left untouched.")
            if it.get("action") in ("create", "copy"):
                is_copy = it["action"] == "copy"
                target = pathlib.Path(it["to"] if is_copy else it["path"])
                if not target.exists():
                    it["status"] = "undone"
                else:
                    if not owns_file(target, it.get("destination_identity")) or file_sha256(target, cancel_event) != expected:
                        raise OSError("Created file changed or ownership could not be verified.")
                    target.unlink()
                    it["status"] = "undone"
                    logfn(f"[UNDO] Removed {'copied file' if is_copy else 'sidecar'}: {target}")
            elif it.get("action") == "rename":
                current, original = pathlib.Path(it["to"]), pathlib.Path(it["from"])
                # Recover an undo interrupted after the original was restored.
                if status == "undoing" and not current.exists() and owns_file(original, it.get("restore_identity")) and file_sha256(original, cancel_event) == expected:
                    it["status"] = "undone"
                else:
                    identity = it.get("destination_identity")
                    if not current.is_file():
                        raise OSError("Moved file is missing.")
                    if data.get("version", 1) >= 2 and not owns_file(current, identity):
                        raise OSError("Destination ownership changed; left untouched.")
                    if file_sha256(current, cancel_event) != expected:
                        raise OSError("Destination checksum changed; left untouched.")
                    if original.exists():
                        if status == "undoing" and owns_file(original, it.get("restore_identity")) and file_sha256(original, cancel_event) == expected:
                            current.unlink()
                            it["status"] = "undone"
                            logfn(f"[UNDO] Finished interrupted restore: {original}")
                        elif status in ("copying", "verified", "failed") and owns_file(original, it.get("source_identity")) and file_sha256(original, cancel_event) == expected:
                            # Forward copy completed but source deletion did not.
                            current.unlink()
                            it["status"] = "undone"
                            logfn(f"[UNDO] Removed verified duplicate: {current}")
                        else:
                            raise FileExistsError("Original path is occupied; left untouched.")
                    else:
                        it["status"] = "undoing"
                        save_manifest_state(manifest_path, data)
                        def checkpoint(state, identity):
                            it["restore_identity"] = identity
                            save_manifest_state(manifest_path, data)
                        verified_move(current, original, expected, checkpoint, cancel_event)
                        it["status"] = "undone"
                        logfn(f"[UNDO] {current} -> {original}")
            it.pop("undo_error", None)
            save_manifest_state(manifest_path, data)
        except Cancelled:
            save_manifest_state(manifest_path, data)
            raise
        except Exception as exc:
            failures += 1
            it["undo_error"] = redact_secrets(exc)
            save_manifest_state(manifest_path, data)
            logfn(f"[UNDO] Skipped {it.get('to') or it.get('path')}: {redact_secrets(exc)}")
    data["undone"] = all(it.get("status") in ("planned", "undone", "skipped", "skipped-conflict") for it in items)
    save_manifest_state(manifest_path, data)
    logfn(f"[UNDO] Finished with {failures} issue(s)." if failures else "[UNDO] Completed successfully.")
    return failures


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
    candidates: List[Tuple[float, EpisodeInfo]] = field(default_factory=list)
    show_title: str = ""
    year: Optional[int] = None
    tags: List[str] = field(default_factory=list)
    resolved: bool = True
    conflict: bool = False
    no_op: bool = False
    proposed: Optional[pathlib.Path] = None
    show_dir: Optional[pathlib.Path] = None
    order: str = "aired"
    upper_se: bool = True
    date_iso: Optional[str] = None
    multi_ep: Optional[Tuple[int, int]] = None
    split: Optional[str] = None
    sidecar: bool = False
    episodes: List[EpisodeInfo] = field(default_factory=list)
    operation: str = "move"


class PlanList(list):
    skipped_count = 0


def rebuild_item(item, episode=None):
    if episode is not None:
        item.episode = episode
        item.episodes = [episode]
        item.resolved = True
        item.approved = True
    ep = item.episode
    season, number = episode_numbers(ep, item.order)
    if item.order == "absolute" and not ep.is_special:
        season = 1
    item.proposed = safe_destination(item.show_dir / format_season_folder(season),
        target_filename(item.show_title, item.year, ep, item.src.suffix, date_iso=item.date_iso,
                        extra=item.extra, multi_ep=item.multi_ep, split=item.split,
                        upper_se=item.upper_se, order=item.order))
    item.dst = item.proposed
    item.nfo = item.dst.with_suffix(".nfo") if item.sidecar else None
    item.tags = (["extra"] if item.extra else []) + (["special"] if ep.is_special else []) + (["split"] if item.split else []) + (["merge"] if item.multi_ep else [])


def finalize_plan_destinations(plan):
    reserved = set()
    for item in plan:
        base = item.proposed or item.dst
        item.no_op = same_path(item.src, base)
        item.conflict = False
        candidate = base
        if not item.no_op and item.approved and item.resolved:
            index = 0
            while (candidate.exists() or path_key(candidate) in reserved or
                   (item.sidecar and (candidate.with_suffix(".nfo").exists() or path_key(candidate.with_suffix(".nfo")) in reserved))):
                index += 1
                candidate = safe_destination(base.parent, base.name, collision_suffix=f" ({index})")
            item.conflict = index > 0
            reserved.add(path_key(candidate))
            if item.sidecar:
                reserved.add(path_key(candidate.with_suffix(".nfo")))
        item.dst = candidate
        item.nfo = candidate.with_suffix(".nfo") if item.sidecar else None
        item.description = f"{item.src} -> {candidate}"


def plan_moves(ctx, src_dir, dst_root, logfn, upper_se, write_sidecar, cancel_event=None, progress=None):
    src_dir = src_dir.resolve()
    all_files = sorted(p for p in src_dir.iterdir() if p.is_file() and not p.is_symlink())
    files = [p for p in all_files if p.suffix.lower() in VIDEO_EXTENSIONS]
    plan = PlanList()
    plan.skipped_count = len(all_files) - len(files)
    if not files:
        logfn("No supported video files found.")
        return plan
    show_name = sanitize_filename(f"{ctx.title} ({ctx.year})" if ctx.year else ctx.title, 100)
    show_dir = (dst_root or src_dir).resolve() / show_name
    if dst_root is None and src_dir.parent.name == show_name and re.fullmatch(r"Season \d+", src_dir.name, re.I):
        show_dir = src_dir.parent
    catalogue = ctx.episode_index or ctx.provider.build_episode_index(ctx.show_id, ctx.order)
    ctx.episode_index = catalogue
    def numbered_match(season, number, source=None, parsed=None):
        # S/E input uses aired numbering. A filename exactly matching this
        # app's output can already use DVD order, so recognize it first.
        aired = [e for e in catalogue if (e.season, e.episode) == (season, number)]
        selected = [e for e in catalogue if episode_numbers(e, ctx.order) == (season, number)]
        if source is not None and parsed is not None and ctx.order == "dvd":
            extra = f"{parsed.extra_type}{parsed.extra_index or ''}" if parsed.extra_type else None
            split = f"{parsed.split_token}{parsed.split_index}" if parsed.split_token and parsed.split_index else None
            exact = [e for e in selected if source.name == target_filename(ctx.title, ctx.year, e, source.suffix,
                      extra=extra, split=split, upper_se=upper_se, order=ctx.order,
                      multi_ep=(parsed.ep1, parsed.ep2) if parsed.ep2 is not None else None)]
            if len(exact) == 1:
                parsed.numbering = "dvd"
                return exact[0]
        matches = selected if getattr(parsed, "numbering", None) == "dvd" else aired or selected
        return matches[0] if len(matches) == 1 else None
    for number, source in enumerate(files, 1):
        check_cancel(cancel_event)
        if progress:
            progress(number, len(files), "Planning")
        parsed = parse_filename(source.name)
        # Recognize our own absolute output even when the show has a year.
        # S/E-coded input continues to be interpreted in the selected order.
        if not SXXEYY_RANGE_RE.search(source.name):
            min_digits = 1 if ctx.order == "absolute" else 3
            absolute_code = re.search(rf" - (\d{{{min_digits},4}})(?:-(\d{{{min_digits},4}}))?(?= - |$)", source.stem)
            if absolute_code and parsed.kind != "date":
                parsed.kind = "absolute_range" if absolute_code.group(2) else "absolute"
                parsed.ep1 = int(absolute_code.group(1))
                parsed.ep2 = int(absolute_code.group(2)) if absolute_code.group(2) else None
        episode = None
        candidates = []
        merged = []
        multi = None
        date = None
        if parsed.kind in ("se", "special", "se_range") and parsed.season is not None and parsed.ep1 is not None:
            episode = numbered_match(parsed.season, parsed.ep1, source, parsed)
            if parsed.kind == "se_range" and episode:
                first, last = parsed.ep1, parsed.ep2
                if last < first:
                    episode = None
                for ep_number in range(first, last + 1):
                    match = numbered_match(parsed.season, ep_number, parsed=parsed)
                    if match is None:
                        episode = None
                        break
                    merged.append(match)
                if episode:
                    if ctx.order == "absolute" and not episode.is_special:
                        nums = [e.absolute for e in merged]
                        if None in nums or nums != list(range(nums[0], nums[0] + len(nums))):
                            episode = None
                        else:
                            multi = (nums[0], nums[-1])
                    else:
                        pairs = [episode_numbers(e, ctx.order) for e in merged]
                        values = [pair[1] for pair in pairs]
                        if any(pair[0] != pairs[0][0] for pair in pairs) or values != list(range(values[0], values[0] + len(values))):
                            episode = None
                        else:
                            multi = (values[0], values[-1])
        elif parsed.kind == "date" and parsed.date_iso:
            matches = [e for e in catalogue if e.air_date == parsed.date_iso]
            episode = matches[0] if len(matches) == 1 else None
            date = parsed.date_iso
        elif parsed.kind in ("absolute", "absolute_range") and parsed.ep1:
            episode = resolve_episode_from_absolute(ctx, parsed.ep1)
            if parsed.kind == "absolute_range" and episode:
                merged = [resolve_episode_from_absolute(ctx, n) for n in range(parsed.ep1, parsed.ep2 + 1)]
                if not merged or any(e is None for e in merged):
                    episode = None
                elif ctx.order == "absolute":
                    multi = (parsed.ep1, parsed.ep2)
                else:
                    pairs = [episode_numbers(e, ctx.order) for e in merged]
                    values = [pair[1] for pair in pairs]
                    if any(pair[0] != pairs[0][0] for pair in pairs) or values != list(range(values[0], values[0] + len(values))):
                        episode = None
                    else:
                        multi = (values[0], values[-1])
        else:
            guessed, hint = extract_candidate_title(source.name, ctx.title)
            if guessed:
                candidates = sorted([(_title_similarity(guessed, e.title or ""), e) for e in catalogue
                                     if hint is None or episode_numbers(e, ctx.order)[0] == hint], key=lambda row: row[0], reverse=True)
                candidates = [pair for pair in candidates if pair[0] >= 0.35][:80]
                if candidates:
                    episode = candidates[0][1]
        if not episode or (ctx.order == "absolute" and not episode.is_special and episode.absolute is None):
            logfn(f"SKIP (unresolved metadata): {source.name}")
            plan.skipped_count += 1
            continue
        resolved = not candidates or (candidates[0][0] >= 0.88 and (len(candidates) == 1 or candidates[0][0] - candidates[1][0] >= 0.07))
        item = PlanItem(source, source, None, "", extra=(f"{parsed.extra_type}{parsed.extra_index or ''}" if parsed.extra_type else None),
            episode=episode, candidates=candidates, show_title=ctx.title, year=ctx.year, resolved=resolved, approved=resolved,
            show_dir=show_dir, order=ctx.order, upper_se=upper_se, date_iso=date, multi_ep=multi,
            split=(f"{parsed.split_token}{parsed.split_index}" if parsed.split_token and parsed.split_index else None),
            sidecar=write_sidecar, episodes=merged or [episode])
        rebuild_item(item)
        plan.append(item)
    finalize_plan_destinations(plan)
    return plan


def execute_plan(plan, logfn, cancel_event=None, progress=None, operation="move"):
    if operation not in ("move", "copy"):
        raise ValueError("File operation must be Move or Copy.")
    phase = "Copying" if operation == "copy" else "Moving"
    eligible = [item for item in plan if item.approved and item.resolved and not (item.no_op or same_path(item.src, item.dst))]
    manifest_path = manifests_dir() / f"manifest-{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    manifest = {"version": 2, "operation": operation, "started_at": datetime.now(timezone.utc).isoformat(), "items": [], "undone": False}
    records = []
    for index, item in enumerate(eligible, 1):
        check_cancel(cancel_event)
        if progress:
            progress(index - 1, len(eligible), "Verifying sources")
        record = {"action":"copy" if operation == "copy" else "rename", "from":str(item.src), "to":str(item.dst),
                  "size":item.src.stat().st_size, "sha256":file_sha256(item.src, cancel_event),
                  "source_identity":file_identity(item.src), "status":"planned"}
        manifest["items"].append(record)
        nfo_record = None
        if item.nfo:
            payload = nfo_bytes(item.episodes or [item.episode], item.show_title, item.year, item.order)
            nfo_record = {"action":"create", "path":str(item.nfo), "status":"planned",
                          "sha256":hashlib.sha256(payload).hexdigest()}
            manifest["items"].append(nfo_record)
        records.append((item, record, nfo_record))
    save_manifest_state(manifest_path, manifest)
    failures = 0
    for number, (item, record, sidecar) in enumerate(records, 1):
        check_cancel(cancel_event)
        if progress:
            progress(number - 1, len(records), phase)
        try:
            if item.dst.exists():
                raise FileExistsError("Approved destination is now occupied. Review the plan again.")
            # The created-file callback records ownership and marks copying.
            # A failure before creation remains planned and is never undone.
            save_manifest_state(manifest_path, manifest)
            def checkpoint(status, identity):
                record["status"] = status
                record["destination_identity"] = identity
                save_manifest_state(manifest_path, manifest)
            transfer = verified_copy if operation == "copy" else verified_move
            transfer(item.src, item.dst, record["sha256"], checkpoint, cancel_event)
            logfn(f"{'COPIED' if operation == 'copy' else 'MOVED'}: {item.src} -> {item.dst}")
        except Cancelled:
            manifest["cancelled"] = True
            save_manifest_state(manifest_path, manifest)
            raise
        except Exception as exc:
            record["error"] = redact_secrets(exc)
            # Keep copying/verified state for recovery; planned errors have
            # made no mutation and must never be mistaken for a completed move.
            failures += 1
            save_manifest_state(manifest_path, manifest)
            logfn(f"FAILED: {item.src}: {redact_secrets(exc)}")
            continue
        if sidecar:
            if item.nfo.exists():
                sidecar["status"] = "skipped-conflict"
                logfn(f"NFO already exists; left unchanged: {item.nfo}")
            else:
                try:
                    sidecar["status"] = "writing"
                    save_manifest_state(manifest_path, manifest)
                    def created(identity):
                        sidecar["destination_identity"] = identity
                        save_manifest_state(manifest_path, manifest)
                    actual = write_nfo(item.nfo, item.episode, item.show_title, item.year,
                                       item.episodes, item.order, created)
                    if actual != sidecar["sha256"]:
                        raise OSError("Sidecar checksum mismatch.")
                    sidecar["status"] = "done"
                    logfn(f"NFO written: {item.nfo}")
                except Exception as exc:
                    sidecar["status"] = "failed"
                    sidecar["error"] = redact_secrets(exc)
                    failures += 1
                    logfn(f"NFO FAILED: {item.nfo}: {redact_secrets(exc)}")
            save_manifest_state(manifest_path, manifest)
        if progress:
            progress(number, len(records), phase)
    manifest["finished"] = True
    manifest["failure_count"] = failures
    save_manifest_state(manifest_path, manifest)
    logfn(f"Run finished: {len(records)} approved operation(s), {failures} issue(s).")
    return manifest_path


# ------------ Tkinter UI ------------
def resource_root() -> pathlib.Path:
    return pathlib.Path(getattr(sys, "_MEIPASS", app_root()))

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
    root.title(APP_NAME + " V3")
    width, height = settings.get("last_window_size", [1000, 720])
    root.geometry(f"{max(820, width)}x{max(640, height)}")
    root.minsize(820, 640)
    try:
        check_state_writable()
        configure_logging()
    except OSError as exc:
        messagebox.showwarning("Storage unavailable", f"Cannot write application state: {exc}\nDisable portable mode or use a writable application folder.", parent=root)
    show_splash_tk(root, skip="--nosplash" in sys.argv)

    src_var, dst_var, title_var, year_var, series_id_var = [tk.StringVar() for _ in range(5)]
    provider_var = tk.StringVar(value="TVDB")
    tvdb_key_var, tvdb_pin_var, tmdb_key_var = [tk.StringVar() for _ in range(3)]
    lang_var = tk.StringVar(value=settings.get("preferred_language", "en"))
    order_var = tk.StringVar(value=settings.get("order", "aired"))
    upper_var = tk.BooleanVar(value=True)
    dry_var = tk.BooleanVar(value=True)
    saved_operation = settings.get("operation", "move")
    operation_var = tk.StringVar(value="Copy" if saved_operation == "copy" else "Move")
    nfo_var = tk.BooleanVar(value=False)
    wrap_var = tk.BooleanVar(value=settings.get("wrap_log", True))
    portable_var = tk.BooleanVar(value=portable_mode_enabled())
    status_var = tk.StringVar(value="Ready")
    progress_var = tk.DoubleVar(value=0)
    events = queue.Queue()
    cancel_event = threading.Event()
    state = {"thread": None, "busy": False, "closing": False, "dialogs": set()}
    creds = None
    disabled_states = []

    form = ttk.Frame(root, padding=8)
    form.pack(fill="x")
    form.columnconfigure(1, weight=1)
    def row(number, label, variable, password=False):
        ttk.Label(form, text=label).grid(row=number, column=0, sticky="w", padx=4, pady=4)
        entry = ttk.Entry(form, textvariable=variable, show="•" if password else "")
        entry.grid(row=number, column=1, sticky="ew", padx=4, pady=4)
        return entry
    row(0, "Source folder", src_var)
    ttk.Button(form, text="Browse", command=lambda: src_var.set(filedialog.askdirectory() or src_var.get())).grid(row=0, column=2)
    row(1, "Destination folder (optional)", dst_var)
    ttk.Button(form, text="Browse", command=lambda: dst_var.set(filedialog.askdirectory() or dst_var.get())).grid(row=1, column=2)
    row(2, "Show title", title_var)
    row(3, "Year (optional)", year_var)
    row(4, "Series ID (optional)", series_id_var)
    options = ttk.Frame(form)
    options.grid(row=5, column=0, columnspan=3, sticky="ew", pady=4)
    for label, variable, values, width in (("Provider", provider_var, ["TVDB", "TMDB"], 9),
                                           ("Output order", order_var, ["aired", "dvd", "absolute"], 10)):
        ttk.Label(options, text=label).pack(side="left", padx=4)
        ttk.Combobox(options, textvariable=variable, values=values, state="readonly", width=width).pack(side="left", padx=4)
    ttk.Label(options, text="Language").pack(side="left", padx=4)
    ttk.Entry(options, textvariable=lang_var, width=8).pack(side="left", padx=4)
    row(6, "TVDB API key", tvdb_key_var, True)
    row(7, "TVDB PIN (if issued)", tvdb_pin_var, True)
    row(8, "TMDB API key", tmdb_key_var, True)
    checks = ttk.Frame(form)
    checks.grid(row=9, column=0, columnspan=3, sticky="ew", pady=4)
    ttk.Checkbutton(checks, text="Uppercase S/E", variable=upper_var).pack(side="left", padx=4)
    ttk.Checkbutton(checks, text="Dry run", variable=dry_var).pack(side="left", padx=4)
    ttk.Checkbutton(checks, text="Write NFO", variable=nfo_var).pack(side="left", padx=4)
    portable_button = ttk.Checkbutton(checks, text="Portable mode", variable=portable_var)
    portable_button.pack(side="left", padx=4)
    ttk.Label(checks, text="File operation:").pack(side="left", padx=4)
    ttk.Combobox(checks, textvariable=operation_var, values=["Move", "Copy"], state="readonly", width=7).pack(side="left", padx=4)
    credential_buttons = ttk.Frame(form)
    credential_buttons.grid(row=10, column=0, columnspan=3, sticky="ew", pady=4)

    logpanel = LogPanel(root, wrap=wrap_var.get())
    logpanel.widget().pack(fill="both", expand=True, padx=8, pady=4)
    log_controls = ttk.Frame(root)
    log_controls.pack(fill="x", padx=8)
    ttk.Button(log_controls, text="Copy log", command=logpanel.copy_all).pack(side="left", padx=4)
    ttk.Button(log_controls, text="Save log", command=logpanel.save_to_file).pack(side="left", padx=4)
    ttk.Checkbutton(log_controls, text="Wrap", variable=wrap_var, command=logpanel.toggle_wrap).pack(side="left", padx=4)
    ttk.Label(root, textvariable=status_var, anchor="w").pack(fill="x", padx=12, pady=4)
    bar = ttk.Progressbar(root, variable=progress_var, maximum=100)
    bar.pack(fill="x", padx=12, pady=4)
    actions = ttk.Frame(root, padding=8)
    actions.pack(fill="x")

    def post(callback):
        events.put(callback)

    def log(message):
        clean = redact_secrets(message)
        post(lambda: logpanel.log(clean))

    def progress(number, total, phase):
        def update():
            progress_var.set(100 * number / max(total, 1))
            status_var.set(f"{phase}: {number}/{total}")
        post(update)

    def provider_progress(message):
        post(lambda: status_var.set(message))

    def current_settings():
        return {"portable": portable_var.get(), "preferred_language": lang_var.get().strip() or "en",
                "order": order_var.get(), "operation": operation_var.get().lower(), "wrap_log": wrap_var.get(),
                "last_window_size": [root.winfo_width(), root.winfo_height()]}

    def persist_settings():
        try:
            save_settings(current_settings())
        except OSError as exc:
            logpanel.log(f"Settings could not be saved: {exc}")

    def set_busy(value):
        state["busy"] = value
        if value:
            disabled_states.clear()
            def disable(widget):
                for child in widget.winfo_children():
                    if isinstance(child, (ttk.Entry, ttk.Button, ttk.Checkbutton, ttk.Combobox)):
                        disabled_states.append((child, child.cget("state")))
                        child.configure(state="disabled")
                    else:
                        disable(child)
            disable(form)
        else:
            for widget, old in disabled_states:
                if widget.winfo_exists():
                    widget.configure(state=old)
            disabled_states.clear()
        start_btn.configure(state="disabled" if value else "normal")
        undo_btn.configure(state="disabled" if value else "normal")
        cancel_btn.configure(state="normal" if value else "disabled")

    def request_cancel():
        if not state["busy"]:
            return
        cancel_event.set()
        status_var.set("Cancelling safely; waiting for the current operation…")
        for dialog in list(state["dialogs"]):
            if dialog.winfo_exists():
                dialog.destroy()

    def close():
        if state["closing"]:
            return
        state["closing"] = True
        persist_settings()
        if state["busy"]:
            request_cancel()
            status_var.set("Closing safely; waiting for the worker to stop…")
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)

    def launch(task):
        if state["busy"] or state["closing"]:
            return
        cancel_event.clear()
        set_busy(True)
        progress_var.set(0)
        def worker():
            outcome = "Finished"
            try:
                task()
                check_cancel(cancel_event)
            except Cancelled:
                outcome = "Cancelled safely"
                log("Cancelled. Completed operations remain available through Undo.")
            except Exception as exc:
                outcome = "Failed; see log"
                log(f"Failed: {type(exc).__name__}: {redact_secrets(exc)}")
            finally:
                post(lambda value=outcome: status_var.set(value))
        thread = threading.Thread(target=worker, daemon=False, name="MSO-worker")
        state["thread"] = thread
        thread.start()

    def pump():
        while True:
            try:
                callback = events.get_nowait()
            except queue.Empty:
                break
            callback()
        thread = state["thread"]
        if thread is not None and not thread.is_alive():
            thread.join()
            state["thread"] = None
            set_busy(False)
        if state["closing"] and not state["busy"]:
            root.destroy()
            return
        root.after(50, pump)

    def on_ui_and_wait(callback):
        ready = threading.Event()
        result = {}
        def invoke():
            try:
                check_cancel(cancel_event)
                if state["closing"]:
                    raise Cancelled()
                result["value"] = callback()
            except Exception as exc:
                result["error"] = exc
            finally:
                ready.set()
        post(invoke)
        while not ready.wait(0.1):
            check_cancel(cancel_event)
        check_cancel(cancel_event)
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def make_dialog(title, geometry):
        dialog = tk.Toplevel(root)
        dialog.title(title)
        dialog.geometry(geometry)
        dialog.transient(root)
        state["dialogs"].add(dialog)
        return dialog

    def wait_dialog(dialog):
        dialog.grab_set()
        root.wait_window(dialog)
        state["dialogs"].discard(dialog)
        # Nested resolver dialogs should return modality to the review window.
        for other in state["dialogs"]:
            if other.winfo_exists():
                other.grab_set()
                break

    def tree_with_scrollbars(parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=8, pady=6)
        tree = ttk.Treeview(frame, columns=[name for name, width in columns], show="headings", selectmode="browse")
        for name, width in columns:
            tree.heading(name, text=name.replace("_", " ").title())
            tree.column(name, width=width, minwidth=width, stretch=False)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def manual_resolve_dialog(candidates, item):
        dialog = make_dialog("Resolve episode", "900x480")
        query = tk.StringVar()
        ttk.Label(dialog, text="Choose an episode; type to filter title, number or overview.").pack(anchor="w", padx=8, pady=4)
        entry = ttk.Entry(dialog, textvariable=query)
        entry.pack(fill="x", padx=8)
        preview_var = tk.StringVar(value="Select a match to preview the full destination.")
        buttons = ttk.Frame(dialog)
        buttons.pack(side="bottom", fill="x", padx=8, pady=6)
        ttk.Label(dialog, textvariable=preview_var, wraplength=850).pack(side="bottom", fill="x", padx=8)
        tree = tree_with_scrollbars(dialog, [("confidence", 85), ("number", 100), ("absolute", 70), ("title", 260), ("air_date", 100), ("overview", 340)])
        chosen = {"episode": None}
        def refresh(*_):
            tree.delete(*tree.get_children())
            search = _normalize_title(query.get())
            for index, (score, ep) in enumerate(candidates):
                season, number = episode_numbers(ep, item.order)
                code = f"S{season:02d}E{number:02d}"
                if search and search not in _normalize_title(f"{code} {ep.absolute or ''} {ep.title or ''} {ep.overview or ''}"):
                    continue
                tree.insert("", "end", iid=str(index), values=(f"{score:.2f}", code, ep.absolute or "", ep.title or "", ep.air_date or "", (ep.overview or "")[:280]))
        def preview(_=None):
            selected = tree.selection()
            if selected:
                ep = candidates[int(selected[0])][1]
                temporary = replace(item, tags=list(item.tags), episodes=list(item.episodes))
                try:
                    rebuild_item(temporary, ep)
                    preview_var.set(str(temporary.proposed))
                except ValueError as exc:
                    preview_var.set(str(exc))
        def accept():
            selected = tree.selection()
            if selected:
                ep = candidates[int(selected[0])][1]
                if item.order == "absolute" and not ep.is_special and ep.absolute is None:
                    messagebox.showwarning("No absolute number", "This episode has no verified absolute number.", parent=dialog)
                    return
                chosen["episode"] = ep
                dialog.destroy()
        ttk.Button(buttons, text="Use selected episode", command=accept).pack(side="right")
        ttk.Button(buttons, text="Leave unresolved", command=dialog.destroy).pack(side="left")
        tree.bind("<<TreeviewSelect>>", preview)
        tree.bind("<Double-1>", lambda _: accept())
        dialog.bind("<Return>", lambda _: accept())
        query.trace_add("write", refresh)
        refresh()
        entry.focus_set()
        wait_dialog(dialog)
        return chosen["episode"]

    def review_and_approve(plan):
        dialog = make_dialog("Review and approve", "1100x520")
        summary_var = tk.StringVar()
        buttons = ttk.Frame(dialog)
        buttons.pack(side="bottom", fill="x", padx=8, pady=6)
        ttk.Label(dialog, textvariable=summary_var).pack(side="bottom", fill="x", padx=8, pady=4)
        tree = tree_with_scrollbars(dialog, [("approve", 65), ("original", 480), ("proposed", 480), ("tags", 230)])
        accepted = {"value": False}
        def refresh():
            finalize_plan_destinations(plan)
            selected = tree.selection()
            tree.delete(*tree.get_children())
            for index, item in enumerate(plan):
                tags = [item.operation] + list(item.tags)
                if not item.resolved:
                    tags.append("unresolved")
                if item.conflict:
                    tags.append("conflict adjusted")
                if item.no_op:
                    tags.append("already correct")
                if item.nfo and item.nfo.exists():
                    tags.append("NFO exists")
                tree.insert("", "end", iid=str(index), values=("✓" if item.approved and item.resolved else "", str(item.src), str(item.dst), ", ".join(tags)))
            if selected and tree.exists(selected[0]):
                tree.selection_set(selected[0])
                tree.focus(selected[0])
            approved = sum(i.approved and i.resolved and not i.no_op for i in plan)
            conflicts = sum(i.conflict for i in plan)
            unresolved = sum(not i.resolved for i in plan)
            mode = plan[0].operation.title() if plan else "Move"
            summary_var.set(f"{mode} files · {len(plan)} proposed · {approved} approved files · {plan.skipped_count} skipped · {conflicts} conflicts adjusted · {unresolved} unresolved · {sum(i.no_op for i in plan)} already correct")
        def resolve(index):
            item = plan[index]
            if not item.candidates:
                messagebox.showinfo("No alternatives", "No title alternatives were found for this file.", parent=dialog)
                return
            choice = manual_resolve_dialog(item.candidates, item)
            if choice:
                rebuild_item(item, choice)
            else:
                item.resolved = False
                item.approved = False
            if dialog.winfo_exists():
                refresh()
        def resolve_selected():
            selected = tree.selection()
            if selected:
                resolve(int(selected[0]))
        def toggle():
            selected = tree.selection()
            if not selected:
                return
            item = plan[int(selected[0])]
            if not item.resolved:
                resolve(int(selected[0]))
            else:
                item.approved = not item.approved
                refresh()
        def approve_all():
            for item in plan:
                item.approved = item.resolved
            refresh()
        def commit():
            accepted["value"] = True
            dialog.destroy()
        ttk.Button(buttons, text="Toggle selected", command=toggle).pack(side="left", padx=4)
        ttk.Button(buttons, text="Resolve episode", command=resolve_selected).pack(side="left", padx=4)
        ttk.Button(buttons, text="Approve all resolved", command=approve_all).pack(side="left", padx=4)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="Accept plan", command=commit).pack(side="right", padx=4)
        refresh()
        def resolve_initial():
            for index, item in enumerate(plan):
                if cancel_event.is_set() or not dialog.winfo_exists():
                    return
                if not item.resolved:
                    resolve(index)
        dialog.after_idle(resolve_initial)
        wait_dialog(dialog)
        return plan if accepted["value"] else []

    def choose_series_dialog(candidates):
        dialog = make_dialog("Choose TV series", "900x400")
        ttk.Label(dialog, text="Select the correct series.").pack(anchor="w", padx=8, pady=4)
        chosen = {"value": None}
        buttons = ttk.Frame(dialog)
        buttons.pack(side="bottom", fill="x", padx=8, pady=6)
        tree = tree_with_scrollbars(dialog, [("title", 240), ("year", 65), ("network", 160), ("overview", 450)])
        for index, item in enumerate(candidates):
            tree.insert("", "end", iid=str(index), values=(item.get("name") or item.get("seriesName") or item.get("title") or "Untitled",
                        str(item.get("year") or item.get("firstAired") or item.get("first_air_date") or "")[:4],
                        item.get("network") or item.get("originalNetwork") or "", (item.get("overview") or "")[:280]))
        def accept():
            selected = tree.selection()
            if selected:
                chosen["value"] = candidates[int(selected[0])]
                dialog.destroy()
        ttk.Button(buttons, text="Use selected series", command=accept).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="left")
        tree.bind("<Double-1>", lambda _: accept())
        dialog.bind("<Return>", lambda _: accept())
        wait_dialog(dialog)
        return chosen["value"]

    def unlock():
        if creds and creds.portable and not creds._passphrase:
            password = os.environ.get("MSO_VAULT_PASSPHRASE") or simpledialog.askstring("Portable vault", "Enter vault passphrase:", show="•", parent=root)
            if not password:
                return False
            creds.set_session_passphrase(password)
        return creds is not None

    def init_credentials(load=True):
        nonlocal creds
        creds = CredentialsStore(portable_var.get())
        if load and (not creds.portable or creds.vault_path.exists()):
            if not unlock():
                return
            for key, variable in (("tvdb_token", tvdb_key_var), ("tvdb_pin", tvdb_pin_var), ("tmdb_key", tmdb_key_var)):
                value = creds.get(key)
                if value:
                    variable.set(value)

    def change_portable():
        global _PORTABLE_OVERRIDE
        old = portable_mode_enabled()
        try:
            check_state_writable(portable_var.get())
            _PORTABLE_OVERRIDE = portable_var.get()
            init_credentials(load=False)
            configure_logging()
            save_settings(current_settings())
            logpanel.log("Credential backend changed. Save credentials to store the entered keys in this mode.")
        except Exception as exc:
            _PORTABLE_OVERRIDE = old
            portable_var.set(old)
            init_credentials(load=False)
            messagebox.showwarning("Portable mode", f"Could not change mode: {redact_secrets(exc)}", parent=root)
    portable_button.configure(command=change_portable)

    def save_creds():
        try:
            if not unlock():
                return
            results = []
            for key, variable in (("tvdb_token", tvdb_key_var), ("tvdb_pin", tvdb_pin_var), ("tmdb_key", tmdb_key_var)):
                if variable.get().strip():
                    results.append(creds.set(key, variable.get().strip()))
            text = "Saved securely." if results and all(results) else "Available for this session only; secure storage is unavailable." if results else "No credentials entered."
            messagebox.showinfo("Credentials", text, parent=root)
        except Exception as exc:
            messagebox.showwarning("Credentials", f"Could not save: {redact_secrets(exc)}", parent=root)

    def provider_snapshot():
        if creds is None:
            raise ValueError("Credential storage is unavailable. Choose a writable state location.")
        name = provider_var.get()
        key_var, key_name = (tvdb_key_var, "tvdb_token") if name == "TVDB" else (tmdb_key_var, "tmdb_key")
        key = key_var.get().strip()
        if not key:
            if not unlock():
                raise Cancelled()
            key = creds.get(key_name)
        if not key:
            raise ValueError(f"Enter a {name} API key.")
        pin = tvdb_pin_var.get().strip()
        return name, key, pin, lang_var.get().strip() or "en"

    def make_provider(snapshot):
        name, key, pin, language = snapshot
        provider = TVDBProvider(key, language, pin, cancel_event) if name == "TVDB" else TMDBProvider(key, language, cancel_event)
        provider.progress = provider_progress
        return provider

    def test_credentials():
        try:
            snapshot = provider_snapshot()
        except Cancelled:
            return
        except Exception as exc:
            messagebox.showwarning("Credentials", redact_secrets(exc), parent=root)
            return
        def test():
            provider = make_provider(snapshot)
            if snapshot[0] == "TVDB":
                provider._get("/languages")
            else:
                result = provider._get("/authentication")
                if result.get("success") is False:
                    raise ProviderError("TMDB rejected this credential.")
            log(f"{snapshot[0]} credential test passed.")
        launch(test)

    ttk.Button(credential_buttons, text="Save credentials", command=save_creds).pack(side="left", padx=4)
    ttk.Button(credential_buttons, text="Test selected provider", command=test_credentials).pack(side="left", padx=4)
    try:
        init_credentials()
    except Exception as exc:
        logpanel.log(f"Credential load: {redact_secrets(exc)}")

    def start():
        if state["busy"]:
            return
        try:
            if not src_var.get().strip():
                raise ValueError("Choose a source folder.")
            source = pathlib.Path(src_var.get().strip()).resolve()
            if not source.is_dir():
                raise ValueError("Choose a valid source folder.")
            destination = pathlib.Path(dst_var.get().strip()).resolve() if dst_var.get().strip() else None
            title = title_var.get().strip()
            year_text = year_var.get().strip()
            if year_text and (not year_text.isdigit() or len(year_text) != 4):
                raise ValueError("Year must contain four digits.")
            year = int(year_text) if year_text else None
            series_id = series_id_var.get().strip()
            snapshot = provider_snapshot()
            order = order_var.get()
            if snapshot[0] == "TMDB" and order == "dvd":
                raise ValueError("Select TVDB for DVD ordering.")
            upper, dry, sidecar = upper_var.get(), dry_var.get(), nfo_var.get()
            operation = operation_var.get().lower()
            check_state_writable()
            persist_settings()
        except Cancelled:
            return
        except Exception as exc:
            messagebox.showwarning("Cannot start", redact_secrets(exc), parent=root)
            return
        def process():
            provider = make_provider(snapshot)
            show_title = title or re.sub(r"(?i)\bseason\s*\d+\b", "", source.name).strip()
            selected_id = series_id
            if not selected_id:
                result = provider.search_show(show_title, year)
                candidates = result.get("_candidates", [result])
                result = on_ui_and_wait(lambda: choose_series_dialog(candidates)) if len(candidates) > 1 else candidates[0]
                if result is None:
                    raise Cancelled()
                if snapshot[0] == "TVDB":
                    selected_id = str(result.get("tvdb_id") or result.get("tvdbId") or result.get("id") or "").removeprefix("series-")
                else:
                    selected_id = str(result.get("id") or "")
            if not selected_id.isdigit():
                raise ProviderError("Series ID must be numeric.")
            info = provider.show_info(selected_id)
            show_title = info.get("name") or info.get("seriesName") or info.get("original_name") or show_title
            show_year = year
            if not show_year:
                value = str(info.get("firstAired") or info.get("first_air_date") or "")[:4]
                show_year = int(value) if value.isdigit() else None
            post(lambda: title_var.set(show_title))
            post(lambda: year_var.set(str(show_year) if show_year else ""))
            post(lambda: series_id_var.set(selected_id))
            if snapshot[0] == "TMDB" and order == "absolute":
                log("TMDB absolute numbering uses regular aired episodes in season/episode order; specials are excluded.")
            ctx = ShowContext(show_title, show_year, snapshot[0], provider, selected_id, [], order)
            plan = plan_moves(ctx, source, destination, log, upper, sidecar, cancel_event, progress)
            for item in plan:
                item.operation = operation
            if not plan:
                log("Nothing to do.")
                return
            approved = on_ui_and_wait(lambda: review_and_approve(plan))
            if not approved:
                log("Review cancelled.")
                return
            check_cancel(cancel_event)
            if dry:
                for item in approved:
                    if item.approved and item.resolved and not item.no_op:
                        log(f"DRY RUN: {operation.upper()} {item.src} -> {item.dst}")
                log("Dry run complete.")
            else:
                manifest = execute_plan(approved, log, cancel_event, progress, operation=operation)
                log(f"Recovery record: {manifest}")
        launch(process)

    def undo():
        if state["busy"]:
            return
        try:
            manifest = latest_manifest_path()
            if not manifest:
                logpanel.log("No pending run to undo.")
                return
        except Exception as exc:
            logpanel.log(f"Cannot read recovery records: {exc}")
            return
        launch(lambda: undo_from_manifest(manifest, log, cancel_event, progress))

    start_btn = ttk.Button(actions, text="Start", command=start)
    start_btn.pack(side="left", padx=4)
    undo_btn = ttk.Button(actions, text="Undo last run", command=undo)
    undo_btn.pack(side="left", padx=4)
    cancel_btn = ttk.Button(actions, text="Cancel", command=request_cancel, state="disabled")
    cancel_btn.pack(side="left", padx=4)
    ttk.Button(actions, text="Exit", command=close).pack(side="right", padx=4)
    root.after(50, pump)
    root.mainloop()


# ------------ Entry ------------
def main():
    if "--psg" in sys.argv:
        logger.info("Using the Tkinter workflow; the legacy PSG path has been retired.")
    build_gui_tk()


if __name__ == "__main__":
    main()
