# Contributing

Please describe the behaviour you want to change before opening a large refactor. Small fixes should include a clear example of the problem and the result after the change.

## Set up

Use Python 3.11 or newer on Windows:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe metaseries.py
```

## Project layout

| Path | Purpose |
| --- | --- |
| `metaseries.py` | Metadata providers, filename planning, transfer and recovery logic, and desktop workflow |
| `ui_theme.py` | Shared interface colours, fonts and widget styles |
| `assets/` | Application branding and provider logos |
| `tests/` | Automated checks using disposable files and provider fixtures |
| `packaging/` | PyInstaller configuration and Windows executable metadata |
| `scripts/build-windows.ps1` | Test, package and ZIP a Windows build |
| `scripts/package_source.py` | Create a clean source ZIP using an explicit file list |
| `docs/` | Release guidance and screenshots |

## Changes that affect files

Keep transfer and recovery behaviour explicit. A proposed change should preserve the review step, destination conflict checks, ownership checks and journal updates. Exercise changes to Move, Copy, cancellation and Undo using a temporary folder with disposable files. Never use a personal media library for automated tests.

For provider changes, add recorded or constructed responses without API keys or personal information. Tests should work offline. Live-provider checks are manual and should be identified as such in the pull request.

## Interface changes

Check the three tabs (**Organise files**, **Output & safety**, **Connections & settings**) and dialogs at 100%, 125% and 150% display scaling. Resize to the minimum supported window size and make sure primary actions, status messages and long paths remain accessible. Attach a screenshot when it helps explain a visible change.

Set `MSO_STATE_DIR` to a temporary directory for isolated settings, logs and recovery records. This override also applies in portable mode. Normal-mode credentials still use the operating system keyring; avoid saving real credentials during tests.

## Pull requests and bug reports

State what changed, why it changed and how you checked it. For a bug report, include the app version, Windows version, provider and minimal steps to reproduce. A few sample filenames usually help more than an entire log.

Review attachments before sharing them. Remove API keys, PINs, passphrases and personal paths. Runtime settings, vault files and recovery records are local state and should not be committed.
