JARVIS DESKTOP BRIDGE  -  voice-control this Windows PC (and the ROG) from
OpenJarvis-Avengers.

WHAT IT DOES
  Once installed, OpenJarvis-Avengers can operate this machine by voice
  ("...on my laptop" / "...on the rog"):
    - open / close any app, open URLs, go to folders
    - volume (whole-system and per-app), play / pause / next music, play a song
    - BRIGHTNESS up/down/set, screenshot, show desktop / minimise / close window
    - new folder, organise (tidy) a folder, search for files, move / delete (to
      Recycle Bin)
    - system status (CPU/RAM/disk), lock, and power: sleep / shutdown / restart /
      sign out (these ask you to confirm first)
  It stays ON: it auto-starts hidden at every logon and reconnects on its own.

INSTALL  (no admin needed - installs into your user profile)
  1. Unzip this folder anywhere.
  2. Double-click  Install.bat
  3. When asked:
       - Machine label: type  laptop  (on the laptop) or  rog  (on the ROG)
       - BRIDGE_TOKEN : paste your bridge token (the same one the phone uses)
  4. That's it. It installs, starts now, and will run at every boot.

  Repeat on the ROG (label it "rog").

VERIFY
  Say: "what's the CPU on my laptop"  or  "open Notepad on my laptop".

UNINSTALL
  Double-click  Uninstall.bat  (stops it + removes auto-start + deletes files).

NOTES
  - Brightness controls the BUILT-IN display; external monitors use their own
    buttons.
  - Shell command execution is enabled (the agent can run commands here). Edit
    %LOCALAPPDATA%\JarvisDesktopBridge\run.bat and set
    JARVIS_BRIDGE_ALLOW_SHELL=0 to disable it.
  - Python 3.10+ is required; the installer auto-installs it via winget if it's
    missing.
