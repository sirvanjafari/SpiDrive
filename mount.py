"""
SpiDrive as a real drive letter, using rclone + WinFsp (both free, open source).

server.py calls this. It is switched on by the file drive_letter.txt next to
server.py (setup_rclone.bat creates it). Delete that file to switch it off.

rclone connects to SpiDrive's own drive server (dav.py, http://127.0.0.1:8080)
and WinFsp turns that into a normal disk in File Explorer. Compared with
Windows' built-in network drive this:
  - shows SpiDrive's own size and usage (DRIVE_SIZE in config.py, default 10 TB)
  - has no 4 GB per-file limit
  - lets videos start playing straight from the drive
  - is faster when browsing
"""
import asyncio
import glob
import os
import shutil
import string
import subprocess
import sys

import config
from dav import drive_size
from drive_core import BASE_DIR, DATA_DIR

LETTER_FILE = os.path.join(BASE_DIR, "drive_letter.txt")
RCLONE_CONF = os.path.join(DATA_DIR, "rclone.conf")
RCLONE_CACHE = os.path.join(DATA_DIR, "rclone-cache")
RCLONE_LOG = os.path.join(DATA_DIR, "rclone.log")
CACHE_MAX = str(getattr(config, "RCLONE_CACHE_SIZE", "20G"))
REMOTE = "spidrive"


def wanted_letter():
    """'S:' from drive_letter.txt, or None if rclone mode is off."""
    try:
        with open(LETTER_FILE, encoding="utf-8-sig") as f:
            txt = f.read().strip().upper()
    except FileNotFoundError:
        return None
    if txt and txt[0] in string.ascii_uppercase and txt[0] not in "ABC":
        return txt[0] + ":"
    return None


def find_rclone():
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        str(getattr(config, "RCLONE_PATH", "") or ""),
        os.path.join(BASE_DIR, "rclone.exe"),
        shutil.which("rclone") or "",
        os.path.join(local, "Microsoft", "WinGet", "Links", "rclone.exe"),
    ]
    candidates += sorted(glob.glob(os.path.join(
        local, "Microsoft", "WinGet", "Packages", "Rclone.Rclone*", "*", "rclone.exe")), reverse=True)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def winfsp_installed():
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        if base and os.path.exists(os.path.join(base, "WinFsp", "bin", "winfsp-x64.dll")):
            return True
    return False


def letter_in_use(letter):
    return os.path.exists(letter + "\\")


def write_conf(dav_port):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(RCLONE_CONF, "w", encoding="utf-8") as f:
        f.write(f"[{REMOTE}]\ntype = webdav\nurl = http://127.0.0.1:{dav_port}\nvendor = other\n")


def mount_args(exe, letter):
    _, size_text = drive_size()
    return [
        exe, "mount", f"{REMOTE}:", letter,
        "--config", RCLONE_CONF,
        "--volname", "SpiDrive",
        "--vfs-cache-mode", "full",            # needed so programs can edit and seek files
        "--cache-dir", RCLONE_CACHE,
        "--vfs-cache-max-size", CACHE_MAX,
        "--vfs-cache-max-age", "1h",
        "--dir-cache-time", "15s",             # new files (e.g. from Telegram) show within 15 s
        "--vfs-disk-space-total-size", size_text,
        "--log-file", RCLONE_LOG,
        "--log-level", "NOTICE",
    ]


def last_log_lines(n=4):
    try:
        with open(RCLONE_LOG, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        return lines[-n:]
    except OSError:
        return []


class Mount:
    def __init__(self, dav_port, log):
        self.dav_port = dav_port
        self.log = log
        self.proc = None
        self.letter = None
        self.watch = None
        self.stopping = False

    async def start(self):
        """Mount the drive. Returns the letter, or None if not mounted."""
        letter = wanted_letter()
        if letter is None:
            return None
        if not sys.platform.startswith("win"):
            self.log("rclone drive: only works on Windows, skipped")
            return None
        exe = find_rclone()
        if exe is None:
            self.log("rclone drive: rclone was not found. Run setup_rclone.bat, "
                     "or put rclone.exe in the SpiDrive folder.")
            return None
        if not winfsp_installed():
            self.log("rclone drive: WinFsp is not installed. Run setup_rclone.bat.")
            return None
        if letter_in_use(letter):
            self.log(f"rclone drive: {letter} is already in use. If it is the old SpiDrive drive, "
                     f"run:  net use {letter} /delete   (or run setup_rclone.bat again)")
            return None
        write_conf(self.dav_port)
        self.letter = letter
        ok = await self._spawn(exe)
        if ok:
            self.watch = asyncio.create_task(self._watchdog(exe))
            return letter
        return None

    async def _spawn(self, exe):
        try:
            open(RCLONE_LOG, "w").close()               # fresh log each start
        except OSError:
            pass
        # Same console as SpiDrive: Ctrl+C reaches rclone too, so it unmounts cleanly.
        self.proc = await asyncio.create_subprocess_exec(
            *mount_args(exe, self.letter), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):                              # up to 30 s
            if letter_in_use(self.letter):
                return True
            if self.proc.returncode is not None:
                break
            await asyncio.sleep(0.5)
        self.log(f"rclone drive: could not mount {self.letter}.")
        for line in last_log_lines():
            self.log(f"  rclone: {line}")
        self.log(f"  full log: {RCLONE_LOG}")
        await self._kill()
        return False

    async def _watchdog(self, exe):
        restarts = 0
        while not self.stopping:
            await self.proc.wait()
            if self.stopping:
                return
            restarts += 1
            self.log(f"rclone drive stopped unexpectedly (code {self.proc.returncode}).")
            for line in last_log_lines(2):
                self.log(f"  rclone: {line}")
            if restarts > 3:
                self.log("rclone drive: giving up after 3 restarts. Restart SpiDrive to try again.")
                return
            await asyncio.sleep(5)
            if not self.stopping and await self._spawn(exe):
                self.log(f"rclone drive: {self.letter} is back")

    async def _kill(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        """Unmount. After Ctrl+C rclone already got the signal and unmounts by itself;
        give it a few seconds before forcing it."""
        self.stopping = True
        if self.watch:
            self.watch.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                await asyncio.wait_for(asyncio.shield(self.proc.wait()), 5)
            except asyncio.TimeoutError:
                await self._kill()
