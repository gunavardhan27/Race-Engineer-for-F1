# Prompt 16: Package as a Windows app

Before starting: read antigravity_pack/FEATURE_STATUS.md (from prompt 00a) and only build what is missing, partial or broken for this prompt. Extend existing code; don't rebuild what works. Packet layouts: docs/udp_spec_2026.h.

```
Turn the project into an installable Windows desktop app. Don't change any race logic.

1. app.py — new entry point:
   - Starts the engine (everything main.py runs) in a background thread with its own asyncio loop.
   - Opens the dashboard in a native window with pywebview (Edge WebView2 backend), pointing at
     the local dashboard with the token. Window title "Race Engineer", remembers size/position.
   - System tray icon (pystray + Pillow): Open dashboard, Quiet mode toggle, Start/stop
     recording, Open data folder, Open logs, Quit. Closing the window hides it to the tray;
     only Quit stops the engine.
   - Single-instance lock: a second launch brings the existing window to the front instead of
     starting another engine (two instances would fight over UDP port 20777).
   - Clean shutdown: stop UDP listener, flush the database and recordings, then exit.
   - Keep main.py working as the CLI for development.

2. Paths for a frozen app:
   - resource_path() for bundled read-only files (dashboard static files, lines/, prompts/,
     default yaml files), working both from source and from PyInstaller (sys._MEIPASS / exe dir).
   - All writable data in %LOCALAPPDATA%\RaceEngineer: config.yaml (copied from defaults on first
     run), commands.yaml, knowledge/, data/ (database, raw recordings), models/, logs/.
     Never write next to the exe (Program Files is read-only).
   - Add "Open data folder" so I can find recordings and exports.

3. First run:
   - A setup page in the window: choose audio input/output devices (with a test beep and a
     mic level meter), choose LLM provider, enter Groq API key, choose persona and profanity.
   - Store the Groq API key in Windows Credential Manager via the keyring package, not in
     config.yaml. Fall back to the GROQ_API_KEY environment variable if set.
   - Download models (faster-whisper, Piper voice) on first run with a progress bar, into
     models/. Don't bundle them in the exe.
   - Show the in-game UDP settings to use and the button bindings from commands.yaml.

4. Robustness:
   - No console window. Rotating log files in logs/ (5 × 5 MB).
   - Global exception handler: write the traceback to the log and show a dialog with
     "Open logs".
   - Status bar in the window: telemetry receiving / no packets, LLM provider in use, usage
     guard state.
   - Settings toggle "Start with Windows" (shortcut in the user's Startup folder), off by default.

5. Build files:
   - assets/icon.ico (simple placeholder icon, multiple sizes).
   - RaceEngineer.spec for PyInstaller in one-folder mode (not one-file: faster startup, fewer
     antivirus false positives). Include all data files and collect binaries/data for
     faster_whisper, ctranslate2, onnxruntime, the TTS packages, pywebview and anything else the
     build needs. windowed=True, icon set, version info resource with the app version.
   - build.bat: runs tests, cleans build/ and dist/, runs PyInstaller with the spec, then
     builds the installer with Inno Setup if ISCC.exe is found
     (check "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" and PATH).
   - installer.iss (Inno Setup 6): installs to Program Files\Race Engineer, Start Menu shortcut,
     optional desktop shortcut, uninstaller, offers to add Windows Firewall rules for UDP 20777
     (local) and TCP for the dashboard port on private networks only. Uninstall keeps
     %LOCALAPPDATA%\RaceEngineer unless I tick "delete my data".
   - Single version number in one place (version.py), used by the app, spec and installer.
   - Update requirements.txt with pyinstaller, pywebview, pystray, pillow, keyring (pinned).

6. Verify:
   - Build the app, launch dist\RaceEngineer\RaceEngineer.exe, and fix every missing-module or
     missing-file error until it starts cleanly, replays a recording with --replay passed
     through, and speaks a test line.
   - Add docs/BUILD.md with the exact build steps.

Tests: resource_path from source and frozen, first-run config copy, single-instance lock,
keyring fallback to env var.
```
