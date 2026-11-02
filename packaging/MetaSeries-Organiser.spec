# Build on Windows: python -m PyInstaller packaging/MetaSeries-Organiser.spec
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

if sys.platform != 'win32':
    raise SystemExit('Build the Windows application on Windows.')

project_root = Path(SPECPATH).parent

# Keyring discovers its backends dynamically. Keep their distribution metadata
# and the installed backends so Windows Credential Manager works when frozen.
hiddenimports = collect_submodules('keyring.backends', on_error='ignore')
hiddenimports += ['blake3', 'xxhash', 'cryptography.hazmat.primitives.ciphers.aead', 'PIL.ImageTk']
datas = [(str(project_root / 'assets'), 'assets')]
datas += copy_metadata('keyring')

analysis = Analysis(
    [str(project_root / 'metaseries.py')],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name='MetaSeries-Organiser',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(project_root / 'assets' / 'MetaSeries_Organiser_Icon_MultiSize.ico'),
    version=str(project_root / 'packaging' / 'version_info.txt'),
    contents_directory='_internal',
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name='MetaSeries-Organiser',
)
