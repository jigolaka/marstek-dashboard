# Marstek Local Dashboard

A local NiceGUI dashboard for discovering and monitoring Marstek devices through the Marstek Device Open API. The first release is read-only and binds to localhost.

## Run on Windows

```powershell
if (!(Test-Path .\venv)) { py -m venv .\venv }
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:8080>. Device API must be enabled in the Marstek app, and the dashboard machine must share the selected LAN with the device. The scan sends the documented UDP discovery request only on the interface selected by the user.

Device telemetry is stored locally in `data/marstek.sqlite3`. Supported model families and field/unit sources are listed in [docs/device-api-matrix.md](docs/device-api-matrix.md). Unsupported model identities are shown as unsupported rather than mapped to a guessed capability profile.

Discovery uses the Marstek Open API UDP broadcast directly. Keenetic DHCP lease import is not yet implemented and is not required for Marstek devices that respond to the documented broadcast.

The API's UDP port defaults to `30000`; set a different value in the scan view if the device has a custom port. The app does not issue device configuration or control commands.

## Build and install on macOS

Build on the Mac that will run the app; PyInstaller does not cross-compile between operating systems. Install Python 3.12 and the Xcode command line tools first. Copy or clone the project to the Mac without copying the Windows `venv`, `build`, or `dist` folders, then run:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-build.txt
python -m unittest discover tests
python packaging/build.py
```

The build produces `dist/MarstekDashboard-0.2.4.dmg`. Open the DMG and drag `Marstek Dashboard.app` to Applications. This local build is not signed or notarized, so macOS may require approval in Privacy & Security on first launch.