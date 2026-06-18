"""One-shot helper to restart the desktop bridge after a self-update.

Run DETACHED (e.g. `start "" pythonw.exe restart_bridge.py`) so it survives the bridge
process it kills. It waits a moment (so the triggering command can return), force-kills
any running bridge.py python(s) EXCEPT itself, then relaunches bridge.py hidden via
pythonw. Used to apply a freshly-downloaded bridge.py without a reboot on installs that
have no run.bat self-restart loop (e.g. Startup-shortcut launch).
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ME = os.getpid()

time.sleep(2)  # let the shell command that launched us return + reply first

try:
    # Match BOTH python.exe and pythonw.exe — a previous restart launched the bridge as
    # pythonw, so a python.exe-only filter would miss it and spawn a DUPLICATE (two bridges
    # fight over the same LiveKit identity → endless reconnect ping-pong).
    out = subprocess.run(
        ["wmic", "process", "where", "name='python.exe' or name='pythonw.exe'",
         "get", "processid,commandline", "/format:csv"],
        capture_output=True, text=True, timeout=25,
    ).stdout
except Exception:  # noqa: BLE001
    out = ""

for ln in out.splitlines():
    if "bridge.py" in ln and "restart_bridge.py" not in ln:
        pid = ln.strip().rsplit(",", 1)[-1].strip()
        if pid.isdigit() and int(pid) != ME:
            try:
                subprocess.run(["taskkill", "/f", "/pid", pid], capture_output=True, timeout=15)
            except Exception:  # noqa: BLE001
                pass

time.sleep(2)

pyw = os.path.join(HERE, "venv", "Scripts", "pythonw.exe")
py = pyw if os.path.exists(pyw) else sys.executable
DETACHED = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
try:
    subprocess.Popen([py, os.path.join(HERE, "bridge.py")], cwd=HERE, creationflags=DETACHED)
except Exception:  # noqa: BLE001
    subprocess.Popen([py, os.path.join(HERE, "bridge.py")], cwd=HERE)
