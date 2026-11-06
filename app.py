"""
SpiDrive app: the program SpiDrive.exe runs.

  - only one copy runs at a time (starting it again just opens the dashboard)
  - first run: opens the setup wizard in the browser
  - then: dashboard + drive S: + uploads + sync, with an icon next to the clock
  - logs go to %LOCALAPPDATA%\\SpiDrive\\spidrive.log
  - "Start with Windows" is kept in the registry (HKCU Run)

Options:  --console   show logs in this window, no tray icon
          --autostart started by Windows at sign-in (don't open the browser)
"""
import asyncio
import os
import subprocess
import sys
import threading
import time
import webbrowser

import settings

FROZEN = getattr(sys, "frozen", False)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MUTEX_NAME = "Local\\SpiDriveSingleInstance"


# ---------------------------------------------------------------- small helpers

def log_to_file():
    os.makedirs(settings.APP_DIR, exist_ok=True)
    try:
        if os.path.getsize(settings.LOG_FILE) > 5 * 1024 * 1024:     # keep the log small
            os.replace(settings.LOG_FILE, settings.LOG_FILE + ".old")
    except OSError:
        pass
    f = open(settings.LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f


def single_instance():
    """True if this is the only SpiDrive running. Keeps the handle alive in a global."""
    if os.name != "nt":
        return True
    import ctypes
    global _MUTEX
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != 183            # ERROR_ALREADY_EXISTS


def release_instance():
    if os.name == "nt" and globals().get("_MUTEX"):
        import ctypes
        ctypes.windll.kernel32.CloseHandle(_MUTEX)


def launch_command(extra=()):
    if FROZEN:
        return [sys.executable, *extra]
    return [sys.executable, os.path.abspath(__file__), *extra]


def set_autostart(enabled):
    if os.name != "nt":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            exe = launch_command(["--autostart"])
            if not FROZEN and exe[0].lower().endswith("python.exe"):
                exe[0] = exe[0][:-10] + "pythonw.exe"                 # no console window
            winreg.SetValueEx(k, "SpiDrive", 0, winreg.REG_SZ, subprocess.list2cmdline(exe))
        else:
            try:
                winreg.DeleteValue(k, "SpiDrive")
            except FileNotFoundError:
                pass


def tray_image(size=64):
    """SpiDrive icon (blue tile with a white paper plane), drawn at any size."""
    from PIL import Image, ImageDraw
    k = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2 * k, 2 * k, 62 * k, 62 * k), radius=14 * k, fill=(42, 143, 216, 255))
    d.polygon([(49 * k, 16 * k), (13 * k, 30 * k), (25 * k, 34 * k), (29 * k, 47 * k),
               (35 * k, 40 * k), (45 * k, 47 * k)], fill="white")
    d.line([(25 * k, 34 * k), (49 * k, 16 * k)], fill=(42, 143, 216, 255), width=max(1, round(2 * k)))
    return img


# ---------------------------------------------------------------- the app

class App:
    def __init__(self, console=False, autostart=False):
        self.console = console
        self.autostart = autostart
        self.loop = asyncio.new_event_loop()
        self.stop_event = None
        self.restart = False
        self.values = settings.load()
        self.icon = None
        self.worker = None
        self.state = "Starting…"

    def url(self):
        return f"http://127.0.0.1:{int(self.values['WEB_PORT'])}"

    def log(self, msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    def set_state(self, text):
        self.state = text
        if self.icon is not None:
            self.icon.title = f"SpiDrive: {text}"

    # ---------- running SpiDrive (in a background thread with its own event loop)

    async def main(self):
        self.stop_event = asyncio.Event()
        settings.install(self.values)
        if not settings.is_set_up(self.values):
            self.set_state("waiting for setup")
            import setup_server
            ok = await setup_server.run(self.values, self.stop_event, int(self.values["WEB_PORT"]),
                                        self.log, opened=lambda: webbrowser.open(self.url()))
            if not ok:
                return
            self.values = settings.load()
            settings.install(self.values)
        try:
            set_autostart(bool(self.values.get("AUTOSTART")))
        except OSError as e:
            self.log(f"could not change 'start with Windows': {e}")

        import server                          # imported only now: it reads the settings
        from drive_core import NotLoggedIn
        self.set_state("running")
        if not self.autostart and not self.console:
            self.loop.call_later(4, lambda: webbrowser.open(self.url()))
        try:
            await server.amain(stop_event=self.stop_event, interactive=False,
                               hooks={"restart": self.request_restart, "log": self.log})
        except NotLoggedIn:
            self.log("Telegram login expired: starting setup again")
            try:
                os.remove(settings.session_file(self.values))
            except OSError:
                pass
            self.restart = True

    def run_loop(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.main())
        except Exception as e:
            self.log(f"SpiDrive stopped because of an error: {e!r}")
            import traceback
            traceback.print_exc()
            self.set_state("stopped (see log)")
        finally:
            self.loop.close()
            if self.icon is not None:
                self.icon.stop()

    def request_restart(self):
        self.restart = True
        self.stop_event.set()

    def _alive(self):
        return self.worker is not None and self.worker.is_alive() and not self.loop.is_closed()

    def quit(self):
        if self._alive() and self.stop_event is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        elif self.icon is not None:
            self.icon.stop()

    def restart_from_menu(self):
        if self._alive():
            self.loop.call_soon_threadsafe(self.request_restart)
        else:
            self.restart = True
            if self.icon is not None:
                self.icon.stop()

    # ---------- tray icon

    def open_drive(self):
        letter = str(self.values.get("DRIVE_LETTER") or "S:")
        if os.path.exists(letter + "\\"):
            os.startfile(letter + "\\")
        else:
            webbrowser.open(self.url())

    def run_tray(self, worker):
        try:
            import pystray
        except ImportError:
            self.log("pystray is not installed: running without a tray icon")
            worker.join()
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open SpiDrive", lambda: webbrowser.open(self.url()), default=True),
            pystray.MenuItem("Open drive in File Explorer", lambda: self.open_drive()),
            pystray.MenuItem("Open log", lambda: os.startfile(settings.LOG_FILE) if os.name == "nt" else None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart", lambda: self.restart_from_menu()),
            pystray.MenuItem("Quit SpiDrive", lambda: self.quit()),
        )
        self.icon = pystray.Icon("SpiDrive", tray_image(), f"SpiDrive: {self.state}", menu)
        self.icon.run()                         # returns when the worker calls icon.stop()
        worker.join(timeout=30)

    def run(self):
        worker = self.worker = threading.Thread(target=self.run_loop, name="spidrive", daemon=False)
        worker.start()
        if self.console:
            try:
                while worker.is_alive():
                    worker.join(0.5)
            except KeyboardInterrupt:
                self.quit()
                worker.join(30)
        else:
            self.run_tray(worker)
        return self.restart


def main():
    args = set(sys.argv[1:])
    console = "--console" in args or (not FROZEN and sys.stdout is not None and sys.stdout.isatty()
                                       and "--tray" not in args)
    if not console:
        log_to_file()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # a restarting copy waits for the old one to finish
    for attempt in range(40 if "--after-restart" in args else 1):
        if single_instance():
            break
        release_instance()
        time.sleep(0.5)
    else:
        port = int(settings.load().get("WEB_PORT", 8000))
        if "--autostart" not in args:
            webbrowser.open(f"http://127.0.0.1:{port}")             # already running: show it
        return

    app = App(console=console, autostart="--autostart" in args)
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "SpiDrive starting", flush=True)
    if app.run():
        release_instance()
        extra = ["--after-restart"] + [a for a in ("--console",) if console]
        flags = 0x08000000 if (os.name == "nt" and not console) else 0      # CREATE_NO_WINDOW
        subprocess.Popen(launch_command(extra), creationflags=flags, close_fds=True)


if __name__ == "__main__":
    main()
