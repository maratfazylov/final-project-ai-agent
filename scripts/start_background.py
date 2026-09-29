"""Start a local polling process with a private log. No cloud deployment is implied."""

import os
import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path
from research_agent.config import Settings

settings = Settings.load()
parser = argparse.ArgumentParser()
parser.add_argument("--restart", action="store_true", help="Gracefully reload this project's polling process")
args = parser.parse_args()
pid_file = settings.data_dir / "bot.pid"
stop_request = settings.data_dir / "bot.stop_request"
if pid_file.exists():
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        if not args.restart:
            print("Polling process is already running")
            raise SystemExit(0)
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True
        ).stdout.strip()
        expected = str(Path(sys.executable)) + " -m research_agent.cli bot"
        if not command.startswith(expected):
            raise SystemExit("PID does not belong to this project; restart refused")
        # A second SIGTERM can interrupt python-telegram-bot's graceful shutdown.
        # Repeated restart requests must wait for the same process, not signal it again.
        if not stop_request.exists() or stop_request.read_text().strip() != str(pid):
            stop_request.write_text(str(pid), encoding="utf-8")
            os.kill(pid, signal.SIGTERM)
        for _ in range(150):
            if not pid_file.exists():
                break
            time.sleep(0.2)
        else:
            raise SystemExit("Bot is still finishing work. Retry restart after it stops.")
    except (ProcessLookupError, ValueError):
        pass
stop_request.unlink(missing_ok=True)
log_path = settings.data_dir / "bot.log"
with log_path.open("ab") as log:
    os.chmod(log_path, 0o600)
    child = subprocess.Popen(
        [sys.executable, "-m", "research_agent.cli", "bot"],
        cwd=Path(__file__).resolve().parent.parent,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
time.sleep(4)
if child.poll() is not None:
    print("Polling process failed to start; inspect local data/bot.log")
    raise SystemExit(1)
print("Polling process started; PID:", child.pid)
