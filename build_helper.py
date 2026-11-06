"""
Prepares what build.bat puts inside SpiDrive.exe and the installer:
  build/spidrive.ico   the icon
  app_id.py            your Telegram API ID + hash (from config.py), so users skip that step
  build/rclone.exe     rclone, so the drive letter works without a separate install
  build/winfsp.msi     WinFsp installer, run by SpiDrive-Setup.exe when it is missing
"""
import glob
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
os.makedirs(BUILD, exist_ok=True)
sys.path.insert(0, HERE)


def step(msg):
    print("   " + msg, flush=True)


def make_icon():
    from app import tray_image
    sizes = [16, 24, 32, 48, 64, 128, 256]
    big = tray_image(256)
    big.save(os.path.join(BUILD, "spidrive.ico"), sizes=[(s, s) for s in sizes])
    step("icon: build\\spidrive.ico")


def make_app_id():
    import settings
    api_id, api_hash = 0, ""
    cfg = os.path.join(HERE, "config.py")
    if os.path.exists(cfg):
        old = settings.read_legacy_config(cfg)
        api_id, api_hash = int(old.get("API_ID") or 0), str(old.get("API_HASH") or "")
    with open(os.path.join(HERE, "app_id.py"), "w", encoding="utf-8") as f:
        f.write("# Created by build_helper.py. Keep it private: do not upload it to GitHub.\n")
        f.write(f"API_ID = {api_id}\nAPI_HASH = {api_hash!r}\n")
    step("app ID: built in from config.py" if api_id else
         "app ID: none found (the setup wizard will ask for it)")


def find_rclone():
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [os.path.join(HERE, "rclone.exe"), shutil.which("rclone") or "",
             os.path.join(local, "Microsoft", "WinGet", "Links", "rclone.exe")]
    cands += sorted(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                           "Rclone.Rclone*", "*", "rclone.exe")), reverse=True)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def copy_rclone():
    src = find_rclone()
    dst = os.path.join(BUILD, "rclone.exe")
    if src:
        shutil.copy2(src, dst)
        step(f"rclone: included ({src})")
    else:
        if os.path.exists(dst):
            os.remove(dst)
        step("rclone: NOT FOUND. Put rclone.exe in this folder for the drive letter to work.")


def copy_winfsp():
    dst = os.path.join(BUILD, "winfsp.msi")
    home = os.path.expanduser("~")
    cands = (glob.glob(os.path.join(HERE, "installer", "winfsp*.msi")) + glob.glob(os.path.join(HERE, "winfsp*.msi"))
             + glob.glob(os.path.join(home, "Downloads", "winfsp*.msi")))
    if cands:
        newest = max(cands, key=os.path.getmtime)
        shutil.copy2(newest, dst)
        step(f"WinFsp: included ({os.path.basename(newest)})")
    else:
        if os.path.exists(dst):
            os.remove(dst)
        step("WinFsp: not included. Download the .msi from https://winfsp.dev/rel/ into the "
             "'installer' folder to bundle it (otherwise users install WinFsp themselves).")


if __name__ == "__main__":
    make_icon()
    make_app_id()
    copy_rclone()
    copy_winfsp()
