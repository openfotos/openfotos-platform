# PyInstaller specification for the unsigned supervised desktop pilot.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


repository = Path(SPECPATH).resolve().parent
desktop_source = repository / "apps" / "desktop" / "src"
package_sources = [
    repository / "packages" / name / "src"
    for name in ("contracts", "storage", "vision")
]
datas = collect_data_files("openfotos_desktop", includes=["assets/*.svg", "assets/*.png"])
datas.extend(
    [
        (str(repository / "LICENSE"), "."),
        (str(repository / "NOTICE"), "."),
        (str(repository / "THIRD_PARTY_NOTICES.md"), "."),
        (str(repository / "build" / "THIRD_PARTY_LICENSES.txt"), "."),
        (str(repository / "licenses" / "YuNet-MIT.txt"), "licenses"),
        (str(repository / "licenses" / "SFace-Apache-2.0.txt"), "licenses"),
    ]
)
icon = (
    repository / "packaging" / "icons" / ("onenodeai-studio.icns" if sys.platform == "darwin" else "onenodeai-studio.ico")
)

analysis = Analysis(
    [str(repository / "packaging" / "desktop_entry.py")],
    pathex=[str(desktop_source), *(str(path) for path in package_sources)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="OneNodeAIStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch="arm64" if sys.platform == "darwin" else None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon),
)
collected = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="OneNodeAIStudio",
)
if sys.platform == "darwin":
    application = BUNDLE(
        collected,
        name="OneNodeAI Studio.app",
        icon=str(icon),
        bundle_identifier="com.onenodeai.studio",
        info_plist={
            "CFBundleDisplayName": "OneNodeAI Studio",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
        },
        target_arch="arm64",
        codesign_identity=None,
        entitlements_file=None,
    )
