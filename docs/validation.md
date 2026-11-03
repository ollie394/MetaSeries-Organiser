# Version 2.0.0 validation

Refreshed on 3 November 2026 using Windows 11 (64-bit) and Python 3.13.2. Version 2.0.0 was rebuilt from the current repository source, with the ollie394 licence and publisher details, updated documentation and organised development history.

The refreshed 2.0.0 offline run passed all 91 tests in 3.93 seconds: 86 core and credential-storage checks, and five tests that create the actual Tk interface. All files, configuration and vaults used by these tests were temporary. Provider responses and the operating system keyring were isolated from personal accounts.

The checks cover filename parsing, episode planning, conflicts, each verification method, copy and move operations, cancellation and recovery records, Undo protection, NFO output, secret redaction, encrypted-vault failures and settings isolation. The interface tests run Preview, approve a copy, undo it, cancel a worker, restore controls and recover from an incorrect vault passphrase. A transfer conflict must report an issue rather than success.

The standalone Windows application was rebuilt with PyInstaller 6.15.0 into `dist/2.0.0/`. The refreshed executable's main screen and About window were opened using disposable portable state. They show version 2.0.0, and About credits ollie394. Windows FileVersion and ProductVersion report 2.0.0; CompanyName and LegalCopyright use ollie394. The main-screen screenshot continues to show the same 2.0.0 interface. The broader packaged navigation and resource checks were completed on the preceding development build, while the real Tk tests were rerun for this refresh.

The Windows download includes the matching source project under `source/MetaSeries-Organiser/`, with the user guide and all 21 distinct historical source versions. Archive integrity, source-file equality, documentation links, checksums and exclusion of personal settings, credentials, logs and recovery records were checked separately.

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

Live TheTVDB and TMDB account authentication was not tested. The source has not been GUI-tested on macOS or Linux, and the Windows executable is unsigned. Local results above use Python 3.13.2; remote Python 3.11/3.13 and Ubuntu syntax results are recorded on the repository's [GitHub Actions page](https://github.com/ollie394/MetaSeries-Organiser/actions).
