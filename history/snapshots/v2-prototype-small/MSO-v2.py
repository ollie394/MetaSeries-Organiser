#!/usr/bin/env python3
"""
MetaSeries-Organiser – MSO-V5 (Developer-ready)
- Implements portable mode, credentials abstraction, retry/backoff, undo manifest,
  review/approve UI, improved normalization & language fallbacks, and safer logging.
- Target: Python 3.13 (Windows-friendly). Build one-folder exe with PyInstaller.
Note: This is a single-file implementation intended for iterative improvement.
"""

from __future__ import annotations
import os
import sys
import email
import re
import json
import time
import math
import shutil
import hashlib
import base64
import logging
import random
import pathlib
import unicodedata
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict, Any

# Third-party optional imports
try:
    import requests  # type: ignore
except Exception:
    print("Missing dependency: requests (pip install requests)")
    sys.exit(1)

# Optional encryption/backoff extras
try:
    from cryptography.fernet import Fernet  # type: ignore
    from cryptography.hazmat.primitives import hashes  # type: ignore
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # type: ignore
    CRYPTO_AVAILABLE = True
except Exception:
    CRYPTO_AVAILABLE = False

try:
    import keyring  # type: ignore
    KEYRING_AVAILABLE = True
except Exception:
    KEYRING_AVAILABLE = False

# GUI backends
sg = None
try:
    import PySimpleGUI as sg  # type: ignore
except Exception:
    try:
        import FreeSimpleGUI as sg  # type: ignore
    except Exception:
        sg = None

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

# ---------------------- App constants ----------------------
APP_NAME = "MetaSeries-Organiser"
ACCENT = "#3EA6FF"
SPLASH_BG = "#0b0f17"
SPLASH_TEXT = "#E6EDF3"

# Remove any embedded/shared TVDB token to respect credentials requirement
DEFAULT_TVDB_TOKEN = None  # must be provided by user if using TVDB

# ---------------------- Logging & paths ----------------------
def app_root() -> pathlib.Path:
    """
    Resolve application root respecting PyInstaller _MEIPASS and portable mode indicators.
    Portable mode if:
      - MSO_PORTABLE=1 env var
      - portable.flag file next to executable
    """
    # Candidate root from sys._MEIPASS (PyInstaller one-file) or current script location
    try:
        if getattr(sys, "_MEIPASS", None):
            candidate = pathlib.Path(getattr(sys, "_MEIPASS")).resolve()
        else:
            candidate = pathlib.Path(__file__).resolve().parent
    except Exception:
        candidate = pathlib.Path(os.getcwd()).resolve()

    # If MSO_PORTABLE=1 or portable.flag present, prefer candidate (local folder)
    if os.environ.get("MSO_PORTABLE", "") == "1" or (candidate / "portable.flag").exists():
        return candidate

    # Otherwise prefer per-user appdata for non-portable runs (Windows)
    if sys.platform.startswith("win"):
        try:
            local_appdata = pathlib.Path(os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or candidate)
            return local_appdata / APP_NAME
        except Exception:
            return candidate
    else:
        # unix-like: use ~/.config/APP_NAME
        try:
            home = pathlib.Path(os.path.expanduser("~"))
            return home / ".config" / APP_NAME
        except Exception:
            return candidate

ROOT = app_root()
ASSETS_DIR = ROOT / "assets"
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
MANIFESTS_DIR = DATA_DIR / "manifests"
LOGS_DIR = DATA_DIR / "logs"
VAULT_DIR = DATA_DIR / "vault"

for p in (CONFIG_DIR, MANIFESTS_DIR, LOGS_DIR, VAULT_DIR):
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

LOG_FILE = LOGS_DIR / f"mso_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.log"

# Configure logger to write to file in ./data/logs and console
logger = logging.getLogger("MSO-V5")
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(fh)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logger.addHandler(ch)

logger.info("MSO-V5 starting. App root: %s (portable=%s)", str(ROOT), str(os.environ.get("MSO_PORTABLE", "") == "1" or (pathlib.Path(__file__).resolve().parent / "portable.flag").exists()))

# ---------------------- Utilities ----------------------
def now_isoutc() -> str:
    return datetime.now(timezone.utc).isoformat()

def compute_sha256(path: pathlib.Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except Exception:
        return None

def safe_truncate_filename(name: str, max_bytes: int = 240) -> str:
    """
    Truncate filename to fit within max_bytes using UTF-8 bytes; preserve extension if present.
    """
    try:
        name = name.strip()
        stem, ext = os.path.splitext(name)
        enc = ext.encode("utf-8")
        max_stem_bytes = max_bytes - len(enc)
        s = stem.encode("utf-8")
        if len(s) <= max_stem_bytes:
            return (stem + ext)[:max_bytes]
        # truncate without breaking utf-8
        truncated = s[:max_stem_bytes]
        while True:
            try:
                ts = truncated.decode("utf-8")
                return ts + ext
            except Exception:
                truncated = truncated[:-1]
    except Exception:
        return name[:max_bytes]

def normalize_str_for_match(s: str) -> str:
    """Unicode NFKD, strip diacritics, casefold, normalize punctuation/whitespace."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold()
    # replace punctuation with spaces, keep alnum
    s = re.sub(r"[^\w\s\-]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def title_similarity(a: str, b: str) -> float:
    # token-sort + SequenceMatcher-like ratio for fuzzy matching
    try:
        from difflib import SequenceMatcher
        ta = " ".join(sorted(normalize_str_for_match(a).split()))
        tb = " ".join(sorted(normalize_str_for_match(b).split()))
        return SequenceMatcher(None, ta, tb).ratio()
    except Exception:
        return 0.0

# ---------------------- Backoff / retry wrapper ----------------------
def request_with_backoff(session: requests.Session, method: str, url: str,
                         max_attempts: int = 5, backoff_base: float = 0.5,
                         backoff_cap: float = 16.0, **kwargs) -> requests.Response:
    """
    Retry wrapper: exponential backoff with jitter; honor Retry-After header when present.
    Retries only on 429 (rate limit) and 5xx (server) errors. 4xx client errors (401/403/404/etc.)
    are treated as fatal and raised immediately so the UI can surface authentication/NotFound.
    """
    attempt = 0
    last_exc = None
    while attempt < max_attempts:
        attempt += 1
        try:
            resp = session.request(method, url, timeout=20, **kwargs)
            # success
            if resp.status_code < 400:
                return resp

            # Rate limit or server error => retry with backoff (honour Retry-After if present)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                ra = resp.headers.get("Retry-After")
                if ra:
                    try:
                        wait = float(ra)
                    except Exception:
                        try:
                            t = email.utils.parsedate_to_datetime(ra)
                            wait = max(0, (t - datetime.now(timezone.utc)).total_seconds())
                        except Exception:
                            wait = None
                    if wait and wait > 0:
                        logger.warning("Server requested Retry-After=%s seconds for %s", wait, url)
                        time.sleep(wait)
                        continue
                backoff = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
                jitter = random.uniform(0, backoff * 0.4)
                sleep_for = backoff + jitter
                logger.warning("Request %s %s returned %s. Backing off %.2fs (attempt %d/%d)", method, url, resp.status_code, sleep_for, attempt, max_attempts)
                last_exc = requests.HTTPError(f"HTTP {resp.status_code} for {url}")
                time.sleep(sleep_for)
                continue

            # Other client errors (4xx except 429) -> raise immediately (do not retry)
            resp.raise_for_status()
            return resp

        except requests.HTTPError as e:
            # If HTTPError has a response and it's a client error (<500), treat as fatal
            r = getattr(e, "response", None)
            if r is not None and r.status_code < 500 and r.status_code != 429:
                logger.error("Client error %s for %s - will not retry", r.status_code, url)
                raise
            last_exc = e

        except requests.RequestException as e:
            last_exc = e

        # transient error path -> backoff and retry
        backoff = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
        jitter = random.uniform(0, backoff * 0.4)
        sleep_for = backoff + jitter
        logger.warning("Network error: %s; retrying in %.2fs (attempt %d/%d)", last_exc, sleep_for, attempt, max_attempts)
        time.sleep(sleep_for)

    if last_exc:
        raise last_exc
    raise requests.RequestException("Unknown error in request_with_backoff")

# ---------------------- CredentialsStore abstraction ----------------------
class CredentialsStore:
    """
    Multi-backend credentials store.

    Modes:
      - mode="system"  : system keychain (keyring preferred; falls back to DPAPI / security / Secret Service)
      - mode="portable": portable encrypted vault (file next to app or in DATA_DIR); requires cryptography
      - mode="session" : in-memory only (no persistence)

    Portable vault format (per-service file): b64(salt(16) + nonce(12) + ciphertext + tag)
    Key derivation: hashlib.scrypt(passphrase, salt, n=2**14, r=8, p=1, dklen=32)
    Encryption: AESGCM (AES-256-GCM)
    """

    def __init__(self, service: str = APP_NAME, mode: Optional[str] = None):
        self.service = service
        # Auto-detect portable flag if present; user can override via mode param
        auto_portable = os.environ.get("MSO_PORTABLE", "") == "1" or (pathlib.Path(__file__).resolve().parent / "portable.flag").exists()
        if mode is None:
            # default: portable if auto_portable else system
            self.mode = "portable" if auto_portable else "system"
        else:
            if mode not in ("system", "portable", "session"):
                raise ValueError("mode must be 'system'|'portable'|'session'")
            self.mode = mode

        # in-memory session store
        self._session_store: Dict[str, str] = {}

        # portable vault state
        self._portable_unlocked = False
        self._portable_key: Optional[bytes] = None  # raw derived key kept in memory when unlocked (if "remember for session")
        self._portable_paths: Dict[str, pathlib.Path] = {}
        # vault dir per-service
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        self.base_vault_path = VAULT_DIR

        # choose best system backend
        self._system_backend = None
        if self.mode == "system":
            if KEYRING_AVAILABLE:
                self._system_backend = "keyring"
            else:
                if sys.platform.startswith("win"):
                    self._system_backend = "dpapi"
                elif sys.platform.startswith("darwin"):
                    self._system_backend = "security_cli"
                else:
                    # try secretstorage
                    try:
                        import secretstorage  # type: ignore
                        self._system_backend = "secretservice"
                    except Exception:
                        self._system_backend = None

    # ---------------- System helpers ----------------
    def _system_set(self, key: str, value: str) -> None:
        if KEYRING_AVAILABLE:
            import keyring  # type: ignore
            keyring.set_password(self.service, key, value)
            return
        backend = self._system_backend
        if backend == "dpapi":
            # DPAPI via CryptProtectData -> store base64 blob in a file under DATA_DIR
            blob = _dpapi_protect(value.encode("utf-8"))
            p = self.base_vault_path / f"{self.service}__{key}.dpapi"
            p.write_bytes(base64.b64encode(blob))
            return
        if backend == "security_cli":
            # macOS security CLI: add-generic-password (overwrite by deleting first)
            try:
                subprocess.run(["security", "add-generic-password", "-a", key, "-s", self.service, "-w", value, "-U"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                return
            except Exception as e:
                raise RuntimeError(f"macOS security CLI set failed: {e}")
        if backend == "secretservice":
            try:
                import secretstorage  # type: ignore
                bus = secretstorage.dbus_init()
                collection = secretstorage.get_default_collection(bus)
                label = f"{self.service}:{key}"
                attributes = {"service": self.service, "account": key}
                secret = value.encode("utf-8")
                # store, overwrite existing if any
                # remove existing first
                for item in collection.search_items(attributes):
                    item.delete()
                collection.create_item(label, attributes, secret, replace=True)
                return
            except Exception as e:
                raise RuntimeError(f"Secret Service store failed: {e}")
        raise RuntimeError("No system keychain backend available.")

    def _system_get(self, key: str) -> Optional[str]:
        if KEYRING_AVAILABLE:
            import keyring  # type: ignore
            return keyring.get_password(self.service, key)
        backend = self._system_backend
        if backend == "dpapi":
            p = self.base_vault_path / f"{self.service}__{key}.dpapi"
            if not p.exists():
                return None
            try:
                blob = base64.b64decode(p.read_bytes())
                data = _dpapi_unprotect(blob)
                return data.decode("utf-8")
            except Exception:
                return None
        if backend == "security_cli":
            try:
                proc = subprocess.run(["security", "find-generic-password", "-s", self.service, "-a", key, "-w"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                return proc.stdout.decode("utf-8").strip()
            except subprocess.CalledProcessError:
                return None
        if backend == "secretservice":
            try:
                import secretstorage  # type: ignore
                bus = secretstorage.dbus_init()
                collection = secretstorage.get_default_collection(bus)
                attributes = {"service": self.service, "account": key}
                items = collection.search_items(attributes)
                for item in items:
                    secret = item.get_secret()
                    if isinstance(secret, bytes):
                        return secret.decode("utf-8")
                    else:
                        return str(secret)
                return None
            except Exception:
                return None
        return None

    def _system_delete(self, key: str) -> None:
        if KEYRING_AVAILABLE:
            import keyring  # type: ignore
            try:
                keyring.delete_password(self.service, key)
            except Exception:
                pass
            return
        backend = self._system_backend
        if backend == "dpapi":
            p = self.base_vault_path / f"{self.service}__{key}.dpapi"
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
            return
        if backend == "security_cli":
            try:
                subprocess.run(["security", "delete-generic-password", "-s", self.service, "-a", key], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except Exception:
                pass
            return
        if backend == "secretservice":
            try:
                import secretstorage  # type: ignore
                bus = secretstorage.dbus_init()
                collection = secretstorage.get_default_collection(bus)
                attributes = {"service": self.service, "account": key}
                for item in collection.search_items(attributes):
                    item.delete()
            except Exception:
                pass
            return

    # ---------------- Portable vault helpers ----------------
    def _portable_path_for(self, key: str) -> pathlib.Path:
        return self.base_vault_path / f"{self.service}__{key}.secret"

    def _portable_store(self, key: str, ciphertext_b64: bytes) -> None:
        p = self._portable_path_for(key)
        p.write_bytes(ciphertext_b64)

    def _portable_load(self, key: str) -> Optional[bytes]:
        p = self._portable_path_for(key)
        if not p.exists():
            return None
        return p.read_bytes()

    def _derive_key_scrypt(self, passphrase: str, salt: bytes) -> bytes:
        # scrypt parameters chosen for reasonable work on modern machines; adjust if needed.
        return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)

    def _portable_encrypt(self, plaintext: bytes, passphrase: str) -> bytes:
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography package required for portable vault encryption.")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        salt = os.urandom(16)
        key = self._derive_key_scrypt(passphrase, salt)
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        ct = aesgcm.encrypt(nonce, plaintext, None)
        blob = salt + nonce + ct
        return base64.b64encode(blob)

    def _portable_decrypt(self, blob_b64: bytes, passphrase: str) -> bytes:
        if not CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography package required for portable vault decryption.")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        try:
            blob = base64.b64decode(blob_b64)
            salt = blob[:16]
            nonce = blob[16:28]
            ct = blob[28:]
            key = self._derive_key_scrypt(passphrase, salt)
            aesgcm = AESGCM(key)
            pt = aesgcm.decrypt(nonce, ct, None)
            return pt
        except Exception as e:
            raise RuntimeError("Decryption failed: " + str(e))

    # Public API
    def set(self, key: str, value: str, passphrase: Optional[str] = None, remember_session: bool = False) -> None:
        """
        Store a secret.
        - For mode=system: uses system keychain (no passphrase required)
        - For mode=portable: requires passphrase. If remember_session=True, derived key is cached in memory for later get() calls in the same session.
        - For mode=session: stored only in memory.
        """
        if self.mode == "session":
            self._session_store[key] = value
            return
        if self.mode == "system":
            self._system_set(key, value)
            return
        # portable
        if not passphrase:
            raise RuntimeError("Passphrase required for portable vault set().")
        enc = self._portable_encrypt(value.encode("utf-8"), passphrase)
        self._portable_store(key, enc)
        if remember_session:
            # derive and cache raw key (so subsequent get() can decrypt without prompting)
            salt = base64.b64decode(enc)[:16]
            self._portable_key = self._derive_key_scrypt(passphrase, salt)
            self._portable_unlocked = True

    def get(self, key: str, passphrase: Optional[str] = None) -> Optional[str]:
        """
        Retrieve secret. For portable mode, passphrase is required unless remember_session was used earlier.
        """
        if self.mode == "session":
            return self._session_store.get(key)
        if self.mode == "system":
            return self._system_get(key)
        # portable
        data = self._portable_load(key)
        if data is None:
            return None
        # if we have cached session key, try using it to decrypt quickly
        if self._portable_unlocked and self._portable_key:
            try:
                if not CRYPTO_AVAILABLE:
                    raise RuntimeError("cryptography required")
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
                blob = base64.b64decode(data)
                salt = blob[:16]
                nonce = blob[16:28]
                ct = blob[28:]
                aesgcm = AESGCM(self._portable_key)
                pt = aesgcm.decrypt(nonce, ct, None)
                return pt.decode("utf-8")
            except Exception:
                # fall through to normal decrypt (passphrase)
                pass
        if passphrase is None:
            raise RuntimeError("Passphrase required to decrypt portable vault entry.")
        pt = self._portable_decrypt(data, passphrase)
        return pt.decode("utf-8")

    def delete(self, key: str) -> None:
        if self.mode == "session":
            self._session_store.pop(key, None)
            return
        if self.mode == "system":
            self._system_delete(key)
            return
        p = self._portable_path_for(key)
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

    def clear_session(self) -> None:
        """Clear any in-memory caches (call on exit or when 'forget session' requested)."""
        self._session_store.clear()
        self._portable_key = None
        self._portable_unlocked = False

    def list_system_keys(self, candidates: Optional[List[str]] = None) -> Dict[str, bool]:
        """
        Probe common key names in the system keychain (or DPAPI files) and return
        a dict {keyname: exists_bool}. This is a lightweight probe used by the
        migration UI to present likely keys to import.
        """
        if self.mode != "system":
            probe = CredentialsStore(self.service, mode="system")
        else:
            probe = self
        if candidates is None:
            candidates = ["tmdb_api_key", "tmdb_token", "tvdb_api_key", "tvdb_token", "api_key"]
        out: Dict[str, bool] = {}
        for k in candidates:
            try:
                v = probe._system_get(k)
                out[k] = v is not None
            except Exception:
                out[k] = False
        # also probe DPAPI files if present
        try:
            for p in self.base_vault_path.glob(f"{self.service}__*.dpapi"):
                name = p.name.split("__", 1)[1].rsplit(".dpapi", 1)[0]
                out.setdefault(name, True)
        except Exception:
            pass
        return out

    def export_system_keys(self, keys: List[str]) -> Dict[str, str]:
        """
        Return dict of key->value for the supplied keys, using the system backend.
        Missing keys are omitted.
        """
        if self.mode != "system":
            sysstore = CredentialsStore(self.service, mode="system")
        else:
            sysstore = self
        res: Dict[str, str] = {}
        for k in keys:
            try:
                v = sysstore._system_get(k)
                if v is not None:
                    res[k] = v
            except Exception:
                continue
        return res

    def _portable_find_any_blob(self) -> Optional[bytes]:
        """Return the first portable vault blob found for this service (raw b64 bytes) or None."""
        for p in self.base_vault_path.glob(f"{self.service}__*.secret"):
            try:
                return p.read_bytes()
            except Exception:
                continue
        return None

    def unlock_portable_with_passphrase(self, passphrase: str) -> bool:
        """
        Try to unlock the portable vault using the given passphrase by attempting to
        decrypt the first found secret blob. If successful, cache derived key in memory.
        """
        blob = self._portable_find_any_blob()
        if not blob:
            # nothing to unlock (vault empty) — still allow caching a derived key from a fresh passphrase
            # create derived key from a random salt to allow set() with remember_session logic.
            salt = os.urandom(16)
            self._portable_key = self._derive_key_scrypt(passphrase, salt)
            self._portable_unlocked = True
            return True
        try:
            # decode to get salt and validate using decrypt
            raw = base64.b64decode(blob)
            salt = raw[:16]
            key = self._derive_key_scrypt(passphrase, salt)
            # attempt decrypt using key (try AESGCM decrypt of blob)
            if not CRYPTO_AVAILABLE:
                raise RuntimeError("cryptography required to validate passphrase")
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
            aesgcm = AESGCM(key)
            nonce = raw[16:28]
            ct = raw[28:]
            aesgcm.decrypt(nonce, ct, None)  # will raise if passphrase wrong
            # success -> cache key
            self._portable_key = key
            self._portable_unlocked = True
            return True
        except Exception:
            return False

# ------------------ platform DPAPI helpers ------------------
# keep these below the class to avoid NameError in earlier code
def _dpapi_protect(data: bytes) -> bytes:
    """Use Windows CryptProtectData to protect bytes. Returns raw blob."""
    if not sys.platform.startswith("win"):
        raise RuntimeError("DPAPI only available on Windows")
    try:
        import ctypes
        from ctypes import wintypes
        CryptProtectData = ctypes.windll.crypt32.CryptProtectData
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        in_blob = DATA_BLOB()
        in_blob.cbData = len(data)
        in_blob.pbData = ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char))
        out_blob = DATA_BLOB()
        if CryptProtectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)) == 0:
            raise RuntimeError("CryptProtectData failed")
        # copy out bytes
        buf = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        # LocalFree the out_blob.pbData if necessary
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        return buf
    except Exception as e:
        raise RuntimeError("DPAPI protect failed: " + str(e))

def _dpapi_unprotect(blob: bytes) -> bytes:
    if not sys.platform.startswith("win"):
        raise RuntimeError("DPAPI only available on Windows")
    try:
        import ctypes
        from ctypes import wintypes
        CryptUnprotectData = ctypes.windll.crypt32.CryptUnprotectData
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        in_blob = DATA_BLOB()
        in_blob.cbData = len(blob)
        in_blob.pbData = ctypes.cast(ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char))
        out_blob = DATA_BLOB()
        if CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)) == 0:
            raise RuntimeError("CryptUnprotectData failed")
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        return result
    except Exception as e:
        raise RuntimeError("DPAPI unprotect failed: " + str(e))
# ...existing code...
class CredentialsStore:
    # ...existing code...
    def list_system_keys(self, candidates: Optional[List[str]] = None) -> Dict[str, bool]:
        """
        Probe common key names in the system keychain (or DPAPI files) and return
        a dict {keyname: exists_bool}. This is a lightweight probe used by the
        migration UI to present likely keys to import.
        """
        if self.mode != "system":
            probe = CredentialsStore(self.service, mode="system")
        else:
            probe = self
        if candidates is None:
            candidates = ["tmdb_api_key", "tmdb_token", "tvdb_api_key", "tvdb_token", "api_key"]
        out: Dict[str, bool] = {}
        for k in candidates:
            try:
                v = probe._system_get(k)
                out[k] = v is not None
            except Exception:
                out[k] = False
        # also probe DPAPI files if present
        try:
            for p in self.base_vault_path.glob(f"{self.service}__*.dpapi"):
                name = p.name.split("__", 1)[1].rsplit(".dpapi", 1)[0]
                out.setdefault(name, True)
        except Exception:
            pass
        return out

    def export_system_keys(self, keys: List[str]) -> Dict[str, str]:
        """
        Return dict of key->value for the supplied keys, using the system backend.
        Missing keys are omitted.
        """
        if self.mode != "system":
            sysstore = CredentialsStore(self.service, mode="system")
        else:
            sysstore = self
        res: Dict[str, str] = {}
        for k in keys:
            try:
                v = sysstore._system_get(k)
                if v is not None:
                    res[k] = v
            except Exception:
                continue
        return res

    def _portable_find_any_blob(self) -> Optional[bytes]:
        """Return the first portable vault blob found for this service (raw b64 bytes) or None."""
        for p in self.base_vault_path.glob(f"{self.service}__*.secret"):
            try:
                return p.read_bytes()
            except Exception:
                continue
        return None

    def unlock_portable_with_passphrase(self, passphrase: str) -> bool:
        """
        Try to unlock the portable vault using the given passphrase by attempting to
        decrypt the first found secret blob. If successful, cache derived key in memory.
        """
        blob = self._portable_find_any_blob()
        if not blob:
            # nothing to unlock (vault empty) — still allow caching a derived key from a fresh passphrase
            # create derived key from a random salt to allow set() with remember_session logic.
            salt = os.urandom(16)
            self._portable_key = self._derive_key_scrypt(passphrase, salt)
            self._portable_unlocked = True
            return True
        try:
            # decode to get salt and validate using decrypt
            raw = base64.b64decode(blob)
            salt = raw[:16]
            key = self._derive_key_scrypt(passphrase, salt)
            # attempt decrypt using key (try AESGCM decrypt of blob)
            if not CRYPTO_AVAILABLE:
                raise RuntimeError("cryptography required to validate passphrase")
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
            aesgcm = AESGCM(key)
            nonce = raw[16:28]
            ct = raw[28:]
            aesgcm.decrypt(nonce, ct, None)  # will raise if passphrase wrong
            # success -> cache key
            self._portable_key = key
            self._portable_unlocked = True
            return True
        except Exception:
            return False

# ----------------- UI helpers: passphrase dialog + migration helpers -----------------
def ask_passphrase_dialog(parent: tk.Tk, title: str = "Vault Passphrase", initial_remember: bool = True) -> Optional[Tuple[str, bool]]:
    """
    Simple modal Toplevel that asks for a passphrase and a 'remember for session' checkbox.
    Returns (passphrase, remember_bool) or None if cancelled.
    """
    dlg = tk.Toplevel(parent)
    dlg.transient(parent)
    dlg.grab_set()
    dlg.title(title)
    dlg.resizable(False, False)

    tk.Label(dlg, text="Passphrase:", anchor="w").grid(row=0, column=0, padx=8, pady=(10,4), sticky="w")
    pw_var = tk.StringVar()
    entry = tk.Entry(dlg, textvariable=pw_var, show="•", width=36)
    entry.grid(row=1, column=0, padx=8, pady=(0,8))
    remember_var = tk.BooleanVar(value=initial_remember)
    chk = tk.Checkbutton(dlg, text="Remember for this session", variable=remember_var)
    chk.grid(row=2, column=0, sticky="w", padx=8, pady=(0,8))

    result: Dict[str, Any] = {"ok": False, "pass": None, "remember": False}

    def on_ok():
        result["ok"] = True
        result["pass"] = pw_var.get()
        result["remember"] = bool(remember_var.get())
        dlg.destroy()

    def on_cancel():
        dlg.destroy()

    btn_frame = tk.Frame(dlg)
    btn_frame.grid(row=3, column=0, pady=8)
    tk.Button(btn_frame, text="OK", width=12, command=on_ok).pack(side="left", padx=6)
    tk.Button(btn_frame, text="Cancel", width=12, command=on_cancel).pack(side="left", padx=6)

    entry.focus_set()
    parent.wait_window(dlg)
    if not result["ok"]:
        return None
    return (result["pass"], result["remember"])

def migrate_system_to_portable_ui(parent: tk.Tk, service: str, ui_log_fn):
    """
    Interactive migration: probe system keychain for common keys, ask user which to import,
    then write them into the portable vault (prompting for passphrase).
    ui_log_fn is used to append messages to the app log.
    """
    sys_cs = CredentialsStore(service, mode="system")
    candidates = sys_cs.list_system_keys()
    found = [k for k, ok in candidates.items() if ok]
    if not found:
        messagebox.showinfo("Migrate", "No known keys found in system keychain to import.")
        return

    # ask user which keys to import (simple comma-separated prompt)
    choices_str = ", ".join(found)
    ask = messagebox.askyesno("Migrate keys", f"Found these keys in system keychain:\n\n{choices_str}\n\nImport all into portable vault?")
    if not ask:
        # allow user to pick via simpledialog if they declined
        pick = simpledialog.askstring("Select keys", f"Enter comma-separated keys to import (available: {choices_str})", parent=parent)
        if not pick:
            ui_log_fn("Migration cancelled by user.")
            return
        want = [p.strip() for p in pick.split(",") if p.strip()]
    else:
        want = found

    # create portable store and ask for passphrase to lock items into
    portable_cs = CredentialsStore(service, mode="portable")
    # ask passphrase (will be used for encrypting)
    dlg = ask_passphrase_dialog(parent, title="Set passphrase for portable vault", initial_remember=True)
    if dlg is None:
        ui_log_fn("Migration cancelled (no passphrase).")
        return
    passphrase, remember = dlg

    exported = sys_cs.export_system_keys(want)
    if not exported:
        ui_log_fn("No keys were available to export.")
        return

    for k, v in exported.items():
        try:
            portable_cs.set(k, v, passphrase=passphrase, remember_session=remember)
            ui_log_fn(f"Migrated key: {k}")
        except Exception as e:
            ui_log_fn(f"Failed to migrate {k}: {e}")

    messagebox.showinfo("Migrate", "Migration complete.")
# ...existing code...

# ----------------- Wiring into main_tk -----------------
# Insert the following UI controls inside your existing main_tk() where appropriate
# (for example, near the provider / API key row). The snippet below shows the controls
# and the callbacks to unlock/migrate vaults.

# ...existing code...
def _insert_creds_ui(root, row_index, ui_state):
    """
    Call this helper from main_tk (after creating root and base fonts).
    ui_state is a dict where we'll store 'creds_store' for reuse.
    Example usage inside main_tk:
        ui_state = {}
        _insert_creds_ui(root, 6, ui_state)
    """
    creds_mode_var = tk.StringVar(value="portable" if os.environ.get("MSO_PORTABLE", "") == "1" else "system")
    tk.Label(root, text="Secret storage:", anchor="w").grid(row=row_index, column=0, sticky="w", padx=6, pady=4)
    mode_menu = tk.OptionMenu(root, creds_mode_var, "system", "portable", "session")
    mode_menu.config(width=12)
    mode_menu.grid(row=row_index, column=1, sticky="w", padx=6, pady=4)

    status_lbl = tk.Label(root, text="Locked", fg="red")
    status_lbl.grid(row=row_index + 1, column=1, sticky="w", padx=6)

    def unlock_action():
        mode = creds_mode_var.get()
        cs = CredentialsStore(APP_NAME, mode=mode)
        if mode == "portable":
            dlg = ask_passphrase_dialog(root, title="Unlock portable vault", initial_remember=True)
            if dlg is None:
                ui_log("Unlock cancelled.")
                return
            passphrase, remember = dlg
            ok = cs.unlock_portable_with_passphrase(passphrase)
            if not ok:
                messagebox.showerror("Unlock failed", "Passphrase incorrect or vault unreadable.")
                status_lbl.config(text="Locked", fg="red")
                return
            cs._portable_unlocked = True
            if remember:
                cs._portable_key = cs._portable_key  # already set by unlock
            ui_state["creds_store"] = cs
            status_lbl.config(text="Unlocked (portable)", fg="green")
            ui_log(f"Portable vault unlocked. Remember={remember}")
        elif mode == "system":
            # just create system-backed store
            ui_state["creds_store"] = cs
            status_lbl.config(text="System keychain", fg="green")
            ui_log("Using system keychain.")
        else:
            ui_state["creds_store"] = cs
            status_lbl.config(text="Session only", fg="green")
            ui_log("Using session-only secret storage.")

    def migrate_action():
        # require a system backend to migrate from
        migrate_system_to_portable_ui(root, APP_NAME, ui_log)

    btn_frame = tk.Frame(root)
    btn_frame.grid(row=row_index, column=2, sticky="e", padx=6)
    tk.Button(btn_frame, text="Unlock", command=unlock_action, width=10).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Migrate", command=migrate_action, width=10).pack(side="left", padx=4)

    return creds_mode_var, status_lbl, ui_state

# Usage note:
# - Call _insert_creds_ui(root, <desired-row>, ui_state) from main_tk after root creation.
# - Keep ui_state and pass ui_state["creds_store"] to wherever you construct API providers so they can read keys.
# - Example in start(): cs = ui_state.get("creds_store") or CredentialsStore(APP_NAME); api_key = cs.get("tmdb_api_key", passphrase_if_needed)
