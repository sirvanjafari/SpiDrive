"""
TelegramDrive command line.

  python drive.py info
  python drive.py upload  <local file or folder> [remote folder] [--overwrite]
  python drive.py download <remote file or folder> [local folder]
  python drive.py ls [remote folder]
  python drive.py mkdir <remote folder>
  python drive.py mv <remote src> <remote dst>
  python drive.py rm <remote file or folder> [-y]
  python drive.py backup
  python drive.py restore-index [-y]    (after a crash or on a new PC)
"""
import argparse
import asyncio
import os
import posixpath
import socket
import sqlite3
import sys
import time

from drive_core import DATA_DIR, DB_PATH, Drive, norm
import config


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class Progress:
    def __init__(self, label):
        self.label = label
        self.t0 = time.time()
        self.last = 0.0

    def __call__(self, done, total):
        now = time.time()
        if now - self.last < 0.5 and done < total:
            return
        self.last = now
        pct = done * 100 / total if total else 100.0
        speed = done / max(now - self.t0, 1e-6)
        print(f"\r  {self.label}: {pct:5.1f}%  {human(done)} / {human(total)}  {human(speed)}/s    ",
              end="", flush=True)

    def finish(self):
        print()


# ---------------------------------------------------------------- commands

async def upload_one(d, local, remote, overwrite):
    row = d.get_file(remote)
    if row and row["status"] == "complete" and not overwrite:
        print(f"skip (already on drive): {remote}")
        return
    if row and row["status"] != "complete":
        print(f"resuming: {remote}")
    print(f"upload {local}  ->  {remote}  ({human(os.path.getsize(local))})")
    p = Progress(posixpath.basename(remote))
    await d.upload(local, remote, overwrite=overwrite, progress=p)
    p.finish()


async def cmd_upload(d, a):
    src = a.local
    dest = norm(a.remote or "/")
    if os.path.isdir(src):
        base = os.path.basename(os.path.normpath(src))
        for root, _, files in os.walk(src):
            for f in sorted(files):
                lp = os.path.join(root, f)
                rel = os.path.relpath(lp, src).replace(os.sep, "/")
                await upload_one(d, lp, norm(posixpath.join(dest, base, rel)), a.overwrite)
    elif os.path.isfile(src):
        await upload_one(d, src, norm(posixpath.join(dest, os.path.basename(src))), a.overwrite)
    else:
        print(f"not found: {src}")
        return 1
    print("done.")


async def cmd_download(d, a):
    remote = norm(a.remote)
    dest = a.local or "."
    row = d.get_file(remote)
    if row is not None:
        jobs = [(row, os.path.join(dest, posixpath.basename(remote)))]
    elif d.is_dir(remote):
        base = posixpath.basename(remote) or "drive"
        pre_len = 1 if remote == "/" else len(remote) + 1
        jobs = [(r, os.path.join(dest, base, *r["path"][pre_len:].split("/")))
                for r in d.walk(remote)]
    else:
        print(f"not found on drive: {remote}")
        return 1
    for r, local in jobs:
        print(f"download {r['path']}  ->  {local}  ({human(r['size'])})")
        p = Progress(posixpath.basename(r["path"]))
        await d.download(r["path"], local, progress=p)
        p.finish()
    print("done.")


async def cmd_ls(d, a):
    path = norm(a.remote or "/")
    if not d.is_dir(path):
        print(f"not a folder: {path}")
        return 1
    dirs, files = d.listdir(path)
    print(f"{path}")
    for name in dirs:
        print(f"  {'<DIR>':>10}                    {name}/")
    for r in files:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created"]))
        flag = {"complete": "", "pending": "  (uploading)"}.get(r["status"], "  (incomplete)")
        lock = " [enc]" if r["encrypted"] else ""
        print(f"  {human(r['size']):>10}  {when}  {posixpath.basename(r['path'])}{lock}{flag}")
    if not dirs and not files:
        print("  (empty)")


async def cmd_mkdir(d, a):
    d.mkdir(a.remote)
    print(f"created {norm(a.remote)}")


async def cmd_mv(d, a):
    d.move(a.src, a.dst)
    print(f"moved {norm(a.src)} -> {norm(a.dst)}")


async def cmd_rm(d, a):
    path = norm(a.remote)
    if not d.get_file(path) and not d.is_dir(path):
        print(f"not found on drive: {path}")
        return 1
    if not a.yes:
        if input(f"Delete {path} permanently (also from Telegram)? [y/N] ").strip().lower() != "y":
            print("cancelled.")
            return
    n = await d.delete(path)
    print(f"deleted {n} file(s).")


async def cmd_info(d, a):
    me = await d.client.get_me()
    s = d.stats()
    print(f"account   : {me.first_name} (@{me.username})")
    print(f"channel   : {getattr(d.channel, 'title', '?')}  ({config.CHANNEL})")
    print(f"index     : {os.path.join(config.DATA_DIR, 'index.db')}")
    print(f"files     : {s['files']}  ({human(s['bytes'])})")
    print(f"incomplete: {s['incomplete']}")
    print(f"chunk size: {human(config.CHUNK_SIZE)}")
    print(f"encryption: {'ON' if config.ENCRYPT else 'off'}")


def spidrive_running():
    """True if the dashboard port is taken (SpiDrive is running)."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", int(getattr(config, "WEB_PORT", 8000))))
        return False
    except OSError:
        return True
    finally:
        s.close()


async def cmd_restore(d, a):
    """Replace index.db with the newest #index_backup from the channel."""
    if spidrive_running():
        print("SpiDrive is running. Close its window first, then run this again.")
        return 1
    waiting = d.stats()["pending"]
    if waiting and not a.yes:
        print(f"{waiting} file(s) are still waiting to upload and would be forgotten.")
        print("Let SpiDrive finish them first, or add -y to restore anyway.")
        return 1
    msg = None
    async for m in d.client.iter_messages(d.channel, search="#index_backup", limit=20):
        if m.file and (m.message or "").startswith("#index_backup"):
            msg = m
            break
    if msg is None:
        print("No index backup found in the channel.")
        return 1
    print(f"Newest backup in the channel: {msg.message[len('#index_backup'):].strip()}  ({human(msg.file.size)})")
    tmp = os.path.join(DATA_DIR, "index_restore.db")
    await d.client.download_media(msg, file=tmp)
    try:
        test = sqlite3.connect(tmp)
        n = test.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        test.close()
    except sqlite3.DatabaseError as e:
        os.remove(tmp)
        print(f"That backup is not a valid index ({e}). Nothing changed.")
        return 1
    if not a.yes:
        if input(f"It lists {n} file(s). Replace your current index with it? [y/N] ").strip().lower() != "y":
            os.remove(tmp)
            print("cancelled.")
            return
    d.db.close()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB_PATH + suffix):
            os.replace(DB_PATH + suffix, f"{DB_PATH}.before-restore-{stamp}{suffix}")
    os.replace(tmp, DB_PATH)
    print(f"Restored. Your old index was kept as index.db.before-restore-{stamp}.")
    print("Start SpiDrive again.")


async def cmd_backup(d, a):
    mid = await d.backup_index()
    print(f"index backed up to the channel (message {mid}).")


# ---------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(description="TelegramDrive")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("upload", help="upload a file or folder")
    s.add_argument("local")
    s.add_argument("remote", nargs="?", default="/", help="drive folder (default /)")
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=cmd_upload)

    s = sub.add_parser("download", help="download a file or folder")
    s.add_argument("remote")
    s.add_argument("local", nargs="?", default=".", help="local folder (default: current)")
    s.set_defaults(func=cmd_download)

    s = sub.add_parser("ls", help="list a drive folder")
    s.add_argument("remote", nargs="?", default="/")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("mkdir", help="create a drive folder")
    s.add_argument("remote")
    s.set_defaults(func=cmd_mkdir)

    s = sub.add_parser("mv", help="move/rename on the drive")
    s.add_argument("src")
    s.add_argument("dst")
    s.set_defaults(func=cmd_mv)

    s = sub.add_parser("rm", help="delete from the drive")
    s.add_argument("remote")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_rm)

    sub.add_parser("info", help="show account and drive status").set_defaults(func=cmd_info)
    sub.add_parser("backup", help="back up the index to the channel").set_defaults(func=cmd_backup)
    s = sub.add_parser("restore-index", help="restore the index from the newest backup in the channel")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_restore)
    return p


async def run(a):
    d = Drive()
    await d.connect()
    try:
        return await a.func(d, a)
    finally:
        await d.close()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Persian file names in the Windows console
    except Exception:
        pass
    args = build_parser().parse_args()
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nstopped. Run the same upload again to resume.")
        code = 1
    except (FileExistsError, FileNotFoundError, IsADirectoryError, ValueError, RuntimeError, IOError) as e:
        print(f"\nerror: {e}")
        code = 1
    sys.exit(code or 0)


if __name__ == "__main__":
    main()
