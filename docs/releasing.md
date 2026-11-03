# Preparing a release

The source project contains no saved credentials or media files. Build from this folder rather than from a working installation with personal state.

## Verify the source

1. Update `APP_VERSION` in `metaseries.py`, the version in the README, `CHANGELOG.md`, `packaging/version_info.txt` and `packaging/windows-readme.txt` together. Update the versioned output paths in the build script, source packager and Windows workflow for the same release.
2. Install `requirements-dev.txt` in a clean Python environment and run `python -m pytest -q tests`.
3. Open the app and check its main screen, About window, series chooser, episode resolver and approval preview. Check keyboard access and common Windows display scales.
4. Test Preview only, Copy, Move, cancellation and Undo with disposable files. Test provider authentication manually with credentials you are authorised to use.
5. Review the files being committed. Keep `data/`, personal configuration, vaults, logs, virtual environments and build outputs out of the repository.

## Build and inspect Windows output

Run `scripts/build-windows.ps1` from PowerShell. It runs the automated tests, creates the source archive, invokes [PyInstaller](https://pyinstaller.org/en/stable/spec-files.html), copies the licence and user documentation, and creates Windows and source ZIPs with SHA-256 checksums in `dist/2.0.0/`.

The Windows folder includes a short startup README and the matching source project under `source/MetaSeries-Organiser/`. The user guide and history links point into that source folder, so the documentation can be opened after extracting the Windows ZIP.

Launch `dist/2.0.0/MetaSeries-Organiser/MetaSeries-Organiser.exe` on a Windows computer or clean Windows account without a separate Python installation. Confirm the logos, window icon, BLAKE3 verification, secure credential backend and portable vault work in the packaged application. Automated source tests do not replace this check.

Do not add `portable.flag` or any saved credentials to the release archive. Users can enable portable mode after extracting it.

## Prepare the source archive

Run `python scripts/package_source.py` to refresh the source archive separately. It creates `dist/2.0.0/MetaSeries-Organiser-Source.zip` and a SHA-256 checksum using an explicit list of source files and project directories. Runtime state, personal configuration, caches, build outputs and virtual environments are excluded. The project remains intact.

Review the archive before uploading it to a new GitHub repository. Its top-level `MetaSeries-Organiser` directory contains the source project, including GitHub workflow files. Extract it locally when preparing a Git commit; do not commit the ZIP itself.

## Publish

The GitHub workflows run offline source tests on Windows and syntax checks on Ubuntu for pushes and pull requests. The Windows package workflow can be run manually, or by pushing a version tag beginning with `v`. It uploads a build artifact for review and does not publish a GitHub release.

After reviewing the source and Windows archive, create the release in GitHub, attach the ZIP and its checksum, and include the matching changelog notes. State which Windows version and Python version were used for validation. If live-provider checks were not performed, say so in the release notes.

The distributed executable is not code-signed by this build. Describe it as unsigned when that is relevant to the release's installation instructions.
