# PyInstaller spec: builds the "Marstek Dashboard" desktop app (onedir; app bundle on macOS).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from src import __publisher__, __title__, __version__  # noqa: E402

NAME = __title__
STATIC = ROOT / "src" / "static"

datas = [(str(STATIC), "src/static")]
binaries = []
hiddenimports = collect_submodules("src")

# These ship data files, DLLs or lazily imported modules that static analysis misses.
for package in ("nicegui", "webview", "screeninfo", "clr_loader", "pythonnet"):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception:
        continue  # e.g. pythonnet exists only on Windows
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

icon = str(STATIC / "logo.ico") if sys.platform == "win32" else (
    str(STATIC / "logo.icns") if sys.platform == "darwin" else None)
version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo,
    )
    version_tuple = tuple(int(part) for part in __version__.split(".")) + (0,)
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple, fileType=1),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", __publisher__),
                StringStruct("FileDescription", NAME),
                StringStruct("FileVersion", __version__),
                StringStruct("ProductName", NAME),
                StringStruct("ProductVersion", __version__),
                StringStruct("OriginalFilename", f"{NAME}.exe"),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

analysis = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    console=False,
    icon=str(STATIC / "logo.ico") if sys.platform == "win32" else None,
    version=version_info,
)
collected = COLLECT(exe, analysis.binaries, analysis.datas, name=NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        collected,
        name=f"{NAME}.app",
        icon=icon,
        bundle_identifier="com.marstek.dashboard",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": NAME,
            "CFBundleShortVersionString": __version__,
            "MarstekPublisher": __publisher__,
        },
    )
