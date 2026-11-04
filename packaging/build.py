"""Build the desktop app and wrap it in a native installer for the current OS.

Run on each target platform (PyInstaller cannot cross-compile):
    python -m pip install -r requirements.txt -r requirements-build.txt
    python packaging/build.py

Windows -> dist/installer/MarstekDashboard-Setup-<version>.exe   (needs Inno Setup 6)
macOS   -> dist/MarstekDashboard-<version>.dmg
Linux   -> dist/marstek-dashboard_<version>_<arch>.deb
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT))
from src import __publisher__, __title__, __version__  # noqa: E402

NAME = __title__


def run(*command: str | Path) -> None:
    print("+", " ".join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True, cwd=ROOT)


def build_macos_icon() -> None:
    from PIL import Image

    iconset = ROOT / "build" / "Marstek Dashboard.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    sizes = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    with Image.open(ROOT / "src" / "static" / "logo.png") as source:
        logo = source.convert("RGBA")
        for filename, size in sizes.items():
            logo.resize((size, size), Image.Resampling.LANCZOS).save(iconset / filename)
    icon = ROOT / "src" / "static" / "logo.icns"
    run("iconutil", "-c", "icns", "-o", icon, iconset)


def build_app() -> None:
    if sys.platform == "win32":
        from PIL import Image

        with Image.open(ROOT / "src" / "static" / "logo.png") as image:
            image.convert("RGBA").save(
                ROOT / "src" / "static" / "logo.ico", format="ICO",
                sizes=[(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)],
            )
    elif sys.platform == "darwin":
        build_macos_icon()
    shutil.rmtree(DIST / NAME, ignore_errors=True)
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", DIST, "--workpath", ROOT / "build", ROOT / "packaging" / "marstek_dashboard.spec")


def find_inno_setup() -> str | None:
    candidates = [shutil.which("iscc")]
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            for version in ("7", "6"):
                candidates += [Path(base) / f"Inno Setup {version}" / "ISCC.exe",
                               Path(base) / "Programs" / f"Inno Setup {version}" / "ISCC.exe"]
    return next((str(path) for path in candidates if path and Path(path).exists()), None)


def package_windows() -> None:
    iscc = os.environ.get("ISCC") or find_inno_setup()
    if not iscc:
        sys.exit("Inno Setup 6 not found. Install it (https://jrsoftware.org/isinfo.php) "
                 f"or set ISCC. The app itself is built in {DIST / NAME}.")
    run(iscc, f"/DAppVersion={__version__}", f"/DAppPublisher={__publisher__}",
        ROOT / "packaging" / "installer.iss")


def package_macos() -> None:
    staging = ROOT / "build" / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    shutil.copytree(DIST / f"{NAME}.app", staging / f"{NAME}.app")
    (staging / "Applications").symlink_to("/Applications", target_is_directory=True)
    target = DIST / f"MarstekDashboard-{__version__}.dmg"
    target.unlink(missing_ok=True)
    run("hdiutil", "create", "-volname", NAME, "-srcfolder", staging,
        "-ov", "-format", "UDZO", target)


def package_linux() -> None:
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    staging = ROOT / "build" / "deb"
    shutil.rmtree(staging, ignore_errors=True)
    install_dir = staging / "opt" / "marstek-dashboard"
    shutil.copytree(DIST / NAME, install_dir)

    binary_link = staging / "usr" / "bin" / "marstek-dashboard"
    binary_link.parent.mkdir(parents=True)
    binary_link.symlink_to(f"/opt/marstek-dashboard/{NAME}")

    icon = staging / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps" / "marstek-dashboard.png"
    icon.parent.mkdir(parents=True)
    shutil.copy(ROOT / "src" / "static" / "logo.png", icon)

    desktop = staging / "usr" / "share" / "applications" / "marstek-dashboard.desktop"
    desktop.parent.mkdir(parents=True)
    desktop.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={NAME}\nComment=Marstek battery monitor\n"
        "Exec=marstek-dashboard\nIcon=marstek-dashboard\nCategories=Utility;\nTerminal=false\n")

    control = staging / "DEBIAN" / "control"
    control.parent.mkdir(parents=True)
    control.write_text(
        "Package: marstek-dashboard\n"
        f"Version: {__version__}\nSection: utils\nPriority: optional\n"
        f"Architecture: {arch}\nMaintainer: {__publisher__}\n"
        "Depends: libegl1, libopengl0, libxkbcommon0, libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libxcb-shape0, libxcb-xinerama0\n"
        "Description: Desktop widget for monitoring a Marstek battery on the local network\n")

    target = DIST / f"marstek-dashboard_{__version__}_{arch}.deb"
    target.unlink(missing_ok=True)
    run("dpkg-deb", "--build", "--root-owner-group", staging, target)


def main() -> None:
    build_app()
    packagers = {"win32": package_windows, "darwin": package_macos}
    packagers.get(sys.platform, package_linux)()


if __name__ == "__main__":
    main()
