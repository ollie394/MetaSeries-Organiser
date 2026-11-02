# MetaSeries Organiser source history

These 21 source snapshots show the retained development versions of MetaSeries Organiser. They were imported from local archives, not recovered from an original Git commit history.

The current maintained application is version 2.0.0. Older labels such as V3 through V8 are retained script revision names; they do not replace the current product version.

## Reading this archive

- Original filenames are preserved inside each snapshot directory. Identical aliases and backups are listed once in the provenance manifest.
- Dates below are observed file modification dates in UTC. They are not verified publication or release dates.
- Syntax checks do not establish that an old application works with current operating systems or provider APIs.
- Nonempty embedded credential defaults were removed from three copies. Original files were left intact.
- Personal settings, logs, recovery records and historical executables are excluded.

## Retained snapshots

| Snapshot | Original filename | Observed modification date (UTC) | Notes |
| --- | --- | --- | --- |
| [Initial unversioned prototype](snapshots/prototype-initial/README.md) | `tv_series_renamer_sorter_portable_python_gui.py` | 2025-09-02 | Prototype; release number unknown. |
| [V1.2](snapshots/v1.2/README.md) | `tv_series_renamer_sorter_portable_python_gui V1.2.py` | 2025-09-02 | Known syntax error, line 499. |
| [V1.3](snapshots/v1.3/README.md) | `tv_series_renamer_sorter_portable_python_gui V1.3.py` | 2025-09-02 |  |
| [Unversioned MetaSeries prototype](snapshots/prototype-metaseries/README.md) | `MetaSeries-Organiser.py` | 2025-09-02 | Prototype; release number unknown. |
| [V1.4](snapshots/v1.4/README.md) | `MSO V1.4.py` | 2025-09-02 | Identical alias or backup recorded. |
| [V1.4.1](snapshots/v1.4.1/README.md) | `MSO-V1.4.1.py` | 2025-09-02 |  |
| [V1.4.2](snapshots/v1.4.2/README.md) | `MSO-V1.4.2.py` | 2025-09-02 |  |
| [V1.4.3](snapshots/v1.4.3/README.md) | `MSO-V1.4.3.py` | 2025-09-02 | Credential default removed. |
| [V1.4.4](snapshots/v1.4.4/README.md) | `MSO-V1.4.4.py` | 2025-09-02 | Credential default removed. Known syntax error, line 1066. |
| [V1.5](snapshots/v1.5/README.md) | `MSO-V1.5.py` | 2025-09-02 | Credential default removed. |
| [V2](snapshots/v2/README.md) | `MSO-V2.py` | 2025-09-03 | Identical alias or backup recorded. |
| [Experimental V2 snapshot (smaller)](snapshots/v2-prototype-small/README.md) | `MSO-v2.py` | 2025-09-04 | Prototype; release number unknown. |
| [Experimental V2 snapshot](snapshots/v2-prototype/README.md) | `MSO-v2.py` | 2025-09-04 | Prototype; release number unknown. |
| [V2.5](snapshots/v2.5/README.md) | `MSO-v2.5.py` | 2025-09-04 |  |
| [V2.6](snapshots/v2.6/README.md) | `MSO-v2.6.py` | 2025-09-04 |  |
| [V3](snapshots/v3/README.md) | `MSO-V3.py` | 2026-10-10 | Stale internal version wording. |
| [V4](snapshots/v4/README.md) | `MSO-V4.py` | 2026-10-10 | Stale internal version wording. |
| [V5](snapshots/v5/README.md) | `MSO-V5.py` | 2026-10-10 | Stale internal version wording. |
| [V6](snapshots/v6/README.md) | `MSO-V6.py` | 2026-11-02 | Stale internal version wording. |
| [V7](snapshots/v7/README.md) | `MSO-V7.py` | 2026-11-02 | Stale internal version wording. |
| [V8](snapshots/v8/README.md) | `MSO-V8.py` | 2026-11-02 | Stale internal version wording. |

## Ambiguous names and duplicate copies

- The earlier `MSO-V1.4.1.py` alias is identical to `MSO V1.4.py`. It is recorded with V1.4. The later, distinct V1.4.1 source is retained separately.
- The V2 backup is identical to the main V2 source and is recorded as a duplicate origin.
- Two different experimental files are both named `MSO-v2.py`; separate prototype directories keep their identities clear.
- No clearly labelled V1.0 or V1.1 source was found. The unversioned prototypes have not been assigned invented version numbers.

See [manifest.json](manifest.json) for relative source paths, original and staged SHA-256 hashes, duplicate origins, sanitisation records and syntax results.
