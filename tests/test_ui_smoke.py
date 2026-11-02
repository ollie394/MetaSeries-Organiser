"""Real Tk smoke tests with disposable media and completely offline providers.

The root is hidden so these tests do not interrupt a desktop session. Tk still
constructs the real views, runs callbacks, and services the worker event queue.
"""

from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metaseries as mso

REAL_TK = tk.Tk
REAL_CREDENTIALS = mso.CredentialsStore


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class OfflineProvider:
    def __init__(self, *args):
        self.cancel_event = args[-1]
        self.progress = lambda message: None

    def search_show(self, title, year=None):
        return {"id": "42", "name": "Example Show"}

    def show_info(self, show_id):
        return {"name": "Example Show", "firstAired": "2026-10-02"}

    def build_episode_index(self, show_id, order="aired"):
        return [mso.EpisodeInfo(1, 2, "The Arrival", "2026-10-02", absolute=2)]

    def _get(self, path):
        return {"success": True}


class MemoryCredentials:
    def __init__(self, portable=False):
        self.portable = portable
        self.vault_path = mso.state_root() / "vault" / "test-only.enc"
        self._passphrase = None
        self.values = {"tvdb_token": "offline-test-token", "tmdb_key": "offline-test-key"}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value
        return True

    def set_session_passphrase(self, value):
        self._passphrase = value


class Desktop:
    def __init__(self, root):
        self.root = root
        self.errors = []
        self.dialog_timer = None
        root.report_callback_exception = lambda kind, value, trace: self.errors.append(value)

    def button(self, text):
        return next(widget for widget in descendants(self.root)
                    if isinstance(widget, ttk.Button) and widget.cget("text") == text
                    and widget.winfo_toplevel() == self.root)

    def entry(self, label):
        label_widget = next(widget for widget in descendants(self.root)
                            if isinstance(widget, ttk.Label) and widget.cget("text") == label)
        return next(widget for widget in label_widget.master.grid_slaves(
            row=label_widget.grid_info()["row"], column=1) if isinstance(widget, ttk.Entry))

    def set_value(self, widget, value):
        self.root.setvar(widget.cget("textvariable") or widget.cget("variable"), value)

    def checkbutton(self, text):
        return next(widget for widget in descendants(self.root)
                    if isinstance(widget, ttk.Checkbutton) and text in widget.cget("text"))

    def combo(self, values):
        return next(widget for widget in descendants(self.root)
                    if isinstance(widget, ttk.Combobox) and tuple(widget.cget("values")) == tuple(values))

    def label_values(self):
        for widget in descendants(self.root):
            if isinstance(widget, ttk.Label) and widget.cget("textvariable"):
                yield str(self.root.getvar(widget.cget("textvariable")))

    def spin_until(self, predicate, seconds=5):
        timed_out = []

        def cancel_on_timeout():
            timed_out.append(True)
            try:
                self.button("Cancel").invoke()
                for window in list(self.root.winfo_children()):
                    if isinstance(window, tk.Toplevel):
                        window.destroy()
            except tk.TclError:
                pass

        deadline = self.root.after(round(seconds * 1000), cancel_on_timeout)
        try:
            while not predicate() and not timed_out:
                self.root.update()
                time.sleep(0.005)
        finally:
            try:
                self.root.after_cancel(deadline)
            except tk.TclError:
                pass
        assert not timed_out, "The UI worker or a modal dialog did not complete"
        assert not self.errors, f"Tk callback failed: {self.errors}"

    def approve_dialogs(self):
        def accept_if_present():
            for window in list(self.root.winfo_children()):
                if not isinstance(window, tk.Toplevel):
                    continue
                buttons = [widget for widget in descendants(window) if isinstance(widget, ttk.Button)]
                for button in buttons:
                    if button.cget("text") == "Use selected series":
                        tree = next(widget for widget in descendants(window) if isinstance(widget, ttk.Treeview))
                        tree.selection_set(tree.get_children()[0])
                        button.invoke()
                        break
                    if button.cget("text") in ("Accept preview", "Approve and copy", "Approve and move"):
                        button.invoke()
                        break
            self.dialog_timer = self.root.after(10, accept_if_present)

        self.dialog_timer = self.root.after(10, accept_if_present)

    def finish(self):
        self.spin_until(lambda: not any(thread.name == "MSO-worker" for thread in threading.enumerate())
                        and str(self.button("Cancel").cget("state")) == "disabled")


@pytest.fixture(scope="module")
def tk_runtime():
    """Use one Tcl/Tk interpreter per process, as the application does."""
    try:
        root = REAL_TK()
    except tk.TclError:
        if sys.platform == "win32":
            raise
        pytest.skip("A display is required for actual Tk smoke tests")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def desktop(tmp_path, monkeypatch, tk_runtime):
    root = tk_runtime
    ui = Desktop(root)
    monkeypatch.setattr(mso.tk, "Tk", lambda: root)
    monkeypatch.setattr(root, "mainloop", lambda: None)
    monkeypatch.setattr(mso, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(mso, "state_root", lambda portable=None: tmp_path / "state")
    monkeypatch.setattr(mso, "user_state_root", lambda: tmp_path / "profile")
    monkeypatch.setattr(mso, "_PORTABLE_OVERRIDE", False)
    monkeypatch.setattr(mso, "load_settings", lambda: dict(mso.DEFAULT_SETTINGS))
    monkeypatch.setattr(mso, "configure_logging", lambda: None)
    monkeypatch.setattr(mso, "enable_dpi_awareness", lambda: None)
    monkeypatch.setattr(mso, "show_splash_tk", lambda *args, **kwargs: None)
    monkeypatch.setattr(mso, "CredentialsStore", MemoryCredentials)
    monkeypatch.setattr(mso, "TVDBProvider", OfflineProvider)
    monkeypatch.setattr(mso, "TMDBProvider", OfflineProvider)
    monkeypatch.setattr(mso.messagebox, "showinfo", lambda *args, **kwargs: None)

    def unexpected_message(title, message, **kwargs):
        ui.errors.append(AssertionError(f"Unexpected {title} message: {message}"))

    monkeypatch.setattr(mso.messagebox, "showwarning", unexpected_message)
    monkeypatch.setattr(mso.messagebox, "showerror", unexpected_message)

    def block_http(*args, **kwargs):
        raise AssertionError("Tk smoke tests must not use the network")

    monkeypatch.setattr(mso.requests.sessions.Session, "request", block_http)
    monkeypatch.delenv("MSO_PORTABLE", raising=False)
    monkeypatch.delenv("MSO_STATE_DIR", raising=False)
    monkeypatch.delenv("MSO_VAULT_PASSPHRASE", raising=False)
    mso.build_gui_tk()
    root.update_idletasks()
    try:
        yield ui
        assert not ui.errors
    finally:
        try:
            if ui.dialog_timer:
                root.after_cancel(ui.dialog_timer)
            ui.button("Cancel").invoke()
            for window in list(root.winfo_children()):
                if isinstance(window, tk.Toplevel):
                    window.destroy()
            end = time.monotonic() + 5
            while any(thread.name == "MSO-worker" for thread in threading.enumerate()) and time.monotonic() < end:
                root.update()
                time.sleep(0.005)
            for timer in root.tk.call("after", "info"):
                root.after_cancel(timer)
            for widget in list(root.winfo_children()):
                widget.destroy()
            root.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass


def test_tabs_and_selected_provider_credentials(desktop):
    ui = desktop
    notebook = next(widget for widget in descendants(ui.root) if isinstance(widget, ttk.Notebook))
    assert [notebook.tab(tab, "text") for tab in notebook.tabs()] == [
        "Organise files", "Output & safety", "Connections & settings"]
    ui.button("Manage connection").invoke()
    assert notebook.index(notebook.select()) == 2
    assert ui.entry("TVDB API key").master.winfo_manager() == "grid"
    assert ui.entry("TMDB API key").master.winfo_manager() == ""
    ui.set_value(ui.combo(["TVDB", "TMDB"]), "TMDB")
    assert ui.entry("TVDB API key").master.winfo_manager() == ""
    assert ui.entry("TMDB API key").master.winfo_manager() == "grid"
    ui.button("Test selected provider").invoke()
    ui.finish()
    ui.button("Save credentials").invoke()
    assert str(ui.combo(["TVDB", "TMDB"]).cget("state")) == "readonly"


def test_preview_copy_and_undo_use_disposable_files(desktop, tmp_path):
    ui = desktop
    source = tmp_path / "incoming"
    source.mkdir()
    video = source / "Example.S01E02.mkv"
    payload = b"test-only episode bytes\0\xff"
    video.write_bytes(payload)
    output = tmp_path / "library"
    ui.set_value(ui.entry("Source folder"), str(source))
    ui.set_value(ui.entry("Destination"), str(output))
    ui.set_value(ui.entry("Series title"), "Example Show")
    ui.approve_dialogs()
    ui.button("Preview changes").invoke()
    ui.finish()
    assert video.read_bytes() == payload
    assert not output.exists()
    assert "Preview complete" in next(widget for widget in descendants(ui.root)
                                      if isinstance(widget, tk.Text)).get("1.0", "end")
    ui.set_value(ui.checkbutton("Preview only"), False)
    ui.set_value(ui.combo(["Move", "Copy"]), "Copy")
    ui.button("Review & copy").invoke()
    ui.finish()
    destination = output / "Example Show (2026)" / "Season 01" / "Example Show (2026) - S01E02 - The Arrival.mkv"
    assert video.read_bytes() == payload
    assert destination.read_bytes() == payload
    ui.button("Undo last run").invoke()
    ui.finish()
    assert video.read_bytes() == payload
    assert not destination.exists()
    assert str(ui.entry("Source folder").cget("state")) == "normal"
    assert str(ui.combo(["Quick", "Verified", "Custom"]).cget("state")) == "readonly"
    custom_method = ui.combo(list(mso.VERIFICATION_DESCRIPTIONS))
    assert str(custom_method.cget("state")) == "disabled"


def test_cancel_restores_controls_and_keeps_source(desktop, tmp_path, monkeypatch):
    class WaitingProvider(OfflineProvider):
        def search_show(self, title, year=None):
            self.cancel_event.wait(5)
            mso.check_cancel(self.cancel_event)
            raise AssertionError("The test should cancel this provider")

    monkeypatch.setattr(mso, "TVDBProvider", WaitingProvider)
    ui = desktop
    source = tmp_path / "incoming"
    source.mkdir()
    video = source / "Example.S01E02.mkv"
    video.write_bytes(b"untouched")
    ui.set_value(ui.entry("Source folder"), str(source))
    ui.button("Preview changes").invoke()
    assert str(ui.entry("Source folder").cget("state")) == "disabled"
    assert str(ui.button("Cancel").cget("state")) == "normal"
    ui.button("Cancel").invoke()
    ui.finish()
    assert str(ui.entry("Source folder").cget("state")) == "normal"
    assert str(ui.combo(["TVDB", "TMDB"]).cget("state")) == "readonly"
    assert str(ui.button("Preview changes").cget("state")) == "normal"
    assert video.read_bytes() == b"untouched"


def test_transfer_failure_is_visible_without_false_success(desktop, tmp_path, monkeypatch):
    ui = desktop
    source = tmp_path / "incoming"
    source.mkdir()
    video = source / "Example.S01E02.mkv"
    video.write_bytes(b"original source")
    ui.set_value(ui.entry("Source folder"), str(source))
    ui.set_value(ui.entry("Destination"), str(tmp_path / "library"))
    ui.set_value(ui.checkbutton("Preview only"), False)
    ui.set_value(ui.combo(["Move", "Copy"]), "Copy")
    real_execute = mso.execute_plan
    occupied = []

    def concurrent_destination(plan, *args, **kwargs):
        destination = plan[0].dst
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"a file created after approval")
        occupied.append(destination)
        return real_execute(plan, *args, **kwargs)

    monkeypatch.setattr(mso, "execute_plan", concurrent_destination)
    ui.approve_dialogs()
    ui.button("Review & copy").invoke()
    ui.finish()
    assert video.read_bytes() == b"original source"
    assert occupied[0].read_bytes() == b"a file created after approval"
    assert any("Finished with 1 issue(s)" in text for text in ui.label_values())
    log = next(widget for widget in descendants(ui.root) if isinstance(widget, tk.Text)).get("1.0", "end")
    assert "Finished with 1 issue(s)" in log
    assert "Organising complete" not in log


def test_wrong_vault_passphrase_can_be_corrected_without_restart(desktop, monkeypatch):
    ui = desktop
    store = REAL_CREDENTIALS(portable=True)
    store.set_session_passphrase("correct test passphrase")
    store.set("tvdb_token", "restored-offline-token")
    monkeypatch.setattr(mso, "CredentialsStore", REAL_CREDENTIALS)
    ui.checkbutton("Use portable mode").invoke()
    ui.set_value(ui.entry("TVDB API key"), "")
    warnings = []
    monkeypatch.setattr(mso.messagebox, "showwarning", lambda title, message, **kwargs: warnings.append((title, message)))
    answers = iter(["incorrect test passphrase", "correct test passphrase", "correct test passphrase"])
    monkeypatch.setattr(mso.simpledialog, "askstring", lambda *args, **kwargs: next(answers))
    ui.button("Test selected provider").invoke()
    assert len(warnings) == 1
    assert "incorrect" in warnings[0][1]
    ui.button("Test selected provider").invoke()
    ui.finish()
    log = next(widget for widget in descendants(ui.root) if isinstance(widget, tk.Text)).get("1.0", "end")
    assert "TVDB credential test passed" in log
    ui.button("Load saved keys").invoke()
    assert ui.root.getvar(ui.entry("TVDB API key").cget("textvariable")) == "restored-offline-token"
    assert len(warnings) == 1
