# Version 2.0.0 validation

Checked on 2 November 2026 using Windows 11 (64-bit) and Python 3.13.2. The 2.0.0 release was rebuilt after updating the application and Windows executable version information.

The 2.0.0 offline run passed all 91 tests in 4.17 seconds: 86 core and credential-storage checks, and five tests that create the actual Tk interface. All files, configuration and vaults used by these tests were temporary. Provider responses and the operating system keyring were isolated from personal accounts.

The checks cover filename parsing, episode planning, conflicts, each verification method, copy and move operations, cancellation and recovery records, Undo protection, NFO output, secret redaction, encrypted-vault failures and settings isolation. The interface tests run Preview, approve a copy, undo it, cancel a worker, restore controls and recover from an incorrect vault passphrase. A transfer conflict must report an issue rather than success.

The standalone Windows application was rebuilt with PyInstaller 6.15.0 into `dist/2.0.0/`. Its main screen and About window were opened and confirmed to display 2.0.0; the executable's FileVersion and ProductVersion also report 2.0.0. The README screenshot shows this rebuilt application. The broader packaged navigation and resource checks were completed on the preceding development build, while the real Tk tests were rerun for 2.0.0. Archive integrity and exclusion of personal settings, credentials, logs and recovery records were checked separately. The original V8 source remains unchanged.

Dependencies used for the 2.0.0 build:

| Package | Version |
| --- | --- |
| requests | 2.32.3 |
| blake3 | 1.0.11 |
| xxhash | 3.8.2 |
| keyring | 25.7.0 |
| cryptography | 49.0.0 |
| Pillow | 12.3.0 |
| pytest | 8.4.2 |
| PyInstaller | 6.15.0 |

Live TheTVDB and TMDB account authentication was not tested. The source has not been GUI-tested on macOS or Linux, and the Windows executable is unsigned. GitHub's Python 3.11/3.13 checks will run after the source is uploaded; they have not run remotely during this preparation.
