import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import __publisher__, __title__, __version__


class PackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        path = Path(__file__).resolve().parents[1] / "packaging" / "build.py"
        spec = importlib.util.spec_from_file_location("marstek_build", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        self.build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.build)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.build.ROOT = Path(self.temp.name)
        self.build.DIST = self.build.ROOT / "dist"
        self.build.DIST.mkdir()

    def test_windows_compiler_receives_version_and_publisher(self) -> None:
        with patch.object(self.build, "find_inno_setup", return_value="ISCC.exe"), \
                patch.dict(self.build.os.environ, {"ISCC": "ISCC.exe"}), \
                patch.object(self.build, "run") as run:
            self.build.package_windows()
        self.assertIn(f"/DAppVersion={__version__}", run.call_args.args)
        self.assertIn(f"/DAppPublisher={__publisher__}", run.call_args.args)
        self.assertEqual(self.build.NAME, __title__)

    def test_linux_package_metadata(self) -> None:
        (self.build.DIST / __title__).mkdir()
        static = self.build.ROOT / "src" / "static"
        static.mkdir(parents=True)
        (static / "logo.png").write_bytes(b"test icon")
        with patch.object(self.build, "run") as run, patch.object(Path, "symlink_to"):
            self.build.package_linux()
        control = (self.build.ROOT / "build" / "deb" / "DEBIAN" / "control").read_text()
        self.assertIn(f"Version: {__version__}\n", control)
        self.assertIn(f"Maintainer: {__publisher__}\n", control)
        desktop = self.build.ROOT / "build" / "deb" / "usr" / "share" / "applications" / "marstek-dashboard.desktop"
        self.assertIn(f"Name={__title__}\n", desktop.read_text())
        self.assertEqual(run.call_args.args[0], "dpkg-deb")

    def test_macos_disk_image_contains_app_bundle(self) -> None:
        bundle = self.build.DIST / f"{__title__}.app" / "Contents"
        bundle.mkdir(parents=True)
        (bundle / "Info.plist").write_text("test plist")
        with patch.object(self.build, "run") as run, patch.object(Path, "symlink_to") as link:
            self.build.package_macos()
        staging = self.build.ROOT / "build" / "dmg"
        self.assertTrue((staging / f"{__title__}.app" / "Contents" / "Info.plist").exists())
        link.assert_called_once_with("/Applications", target_is_directory=True)
        self.assertIn(staging, run.call_args.args)

    def test_macos_iconset_generates_all_apple_sizes(self) -> None:
        from PIL import Image

        static = self.build.ROOT / "src" / "static"
        static.mkdir(parents=True)
        Image.new("RGBA", (512, 512), (20, 20, 20, 255)).save(static / "logo.png")
        with patch.object(self.build, "run") as run:
            self.build.build_macos_icon()
        iconset = self.build.ROOT / "build" / "Marstek Dashboard.iconset"
        self.assertEqual(len(list(iconset.glob("*.png"))), 10)
        with Image.open(iconset / "icon_512x512@2x.png") as icon:
            self.assertEqual(icon.size, (1024, 1024))
        self.assertEqual(run.call_args.args[:3], ("iconutil", "-c", "icns"))


if __name__ == "__main__":
    unittest.main()