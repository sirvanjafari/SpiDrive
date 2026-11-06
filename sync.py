"""
Two-way folder sync between this PC and the drive.

Set it up in config.py, for example:

    SYNC_FOLDERS = {
        r"D:\\SpiDrive Sync": "/Sync",
    }

Every 10 seconds SpiDrive compares the local folder with the drive folder:
  - new or changed on the PC      -> uploaded to the drive
  - new or changed on the drive   -> downloaded to the PC
    (including files you send from Telegram, if you sync "/Telegram Inbox")
  - renamed or moved on one side  -> renamed on the other (nothing re-uploaded)
  - deleted on one side           -> moved to a trash on the other side:
        PC files      -> DATA_DIR\\sync-trash\\<date>\\...
        drive files   -> /SpiDrive Trash/<date>/... on the drive
  - changed on both sides         -> both kept; the PC copy is renamed
                                     "name (conflict 2026-10-28 1416).ext"

A file counts as unchanged when its size and modified time are the same as
at the last sync. Nothing is ever deleted outright by sync: both trashes can
be emptied by hand.
"""
import asyncio
import os
import posixpath
import shutil
import time
import uuid

import config
from drive_core import DATA_DIR, norm

INTERVAL = 10
SETTLE_SECONDS = 3                   # skip files written in the last 3 s (still being saved)
WINDOWS = os.name == "nt"
TRASH_REMOTE = "/SpiDrive Trash"
TRASH_LOCAL = os.path.join(DATA_DIR, "sync-trash")
SYNC_TMP = os.path.join(DATA_DIR, "sync-tmp")
TRASH_KEEP_DAYS = 30
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "$recycle.bin", "system volume information"}
SKIP_PREFIX = ("~$", ".~lock", "~wrl")
SKIP_SUFFIX = (".tmp", ".crdownload", ".partial", ".part", ".download")


def key(rel):
    """Comparison key: Windows treats A.txt and a.txt as the same file."""
    return rel.casefold() if WINDOWS else rel


def same(a, b):
    """a, b = (size, mtime). FAT/exFAT disks store times in 2-second steps."""
    return a is not None and b is not None and a[0] == b[0] and abs(a[1] - b[1]) <= 2


def sig(v):
    return (v[0], int(v[1]) // 2)


def skip_name(name):
    n = name.lower()
    return n in SKIP_NAMES or n.startswith(SKIP_PREFIX) or n.endswith(SKIP_SUFFIX)


def configured_pairs():
    raw = getattr(config, "SYNC_FOLDERS", None) or {}
    if isinstance(raw, (list, tuple)):
        raw = dict(raw)
    return [(os.path.abspath(os.path.expanduser(str(l))), norm(str(r))) for l, r in raw.items()]


def scan_local(root):
    """{key: (rel, size, mtime)}, {key: rel} of folders, set of keys still being written."""
    files, dirs, busy = {}, {}, set()
    now = time.time()
    for cur, dnames, fnames in os.walk(root):
        dnames[:] = [x for x in dnames if not skip_name(x)]
        base = os.path.relpath(cur, root).replace(os.sep, "/")
        base = "" if base == "." else base
        for dn in dnames:
            rel = f"{base}/{dn}" if base else dn
            dirs[key(rel)] = rel
        for fn in fnames:
            if skip_name(fn):
                continue
            rel = f"{base}/{fn}" if base else fn
            try:
                st = os.stat(os.path.join(cur, fn))
            except OSError:
                continue
            if abs(now - st.st_mtime) < SETTLE_SECONDS:
                busy.add(key(rel))
            files[key(rel)] = (rel, st.st_size, st.st_mtime)
    return files, dirs, busy


class Syncer:
    def __init__(self, drive, log=print):
        self.d = drive
        self.log = log
        self.pairs = []
        self.warned = set()
        self.status = {}
        self._init_db()
        taken = []
        for local, remote in configured_pairs():
            problem = self._check_pair(local, remote, taken)
            if problem:
                self.log(f"sync: skipping {local} -> {remote}: {problem}")
                continue
            taken.append((local, remote))
        self.pairs = taken

    # ---------- setup

    def _init_db(self):
        self.d.db.executescript("""
        CREATE TABLE IF NOT EXISTS sync_files(
            pair TEXT NOT NULL, k TEXT NOT NULL, rel TEXT NOT NULL,
            size INTEGER NOT NULL, mtime REAL NOT NULL, PRIMARY KEY(pair, k));
        CREATE TABLE IF NOT EXISTS sync_dirs(
            pair TEXT NOT NULL, k TEXT NOT NULL, PRIMARY KEY(pair, k));
        """)
        self.d.db.commit()

    @staticmethod
    def _check_pair(local, remote, taken):
        data = os.path.abspath(DATA_DIR)
        def inside(a, b):
            a, b = os.path.normcase(a), os.path.normcase(b)
            return a == b or a.startswith(b.rstrip("\\/") + os.sep)
        if inside(local, data) or inside(data, local):
            return "the local folder must not contain SpiDrive's data folder"
        if os.path.splitdrive(local)[1] in ("", "\\", "/"):
            return "pick a folder, not a whole disk"
        if remote == TRASH_REMOTE or remote.startswith(TRASH_REMOTE + "/"):
            return "cannot sync the trash"
        for l2, r2 in taken:
            if inside(local, l2) or inside(l2, local):
                return f"overlaps with {l2}"
            if remote == r2 or remote.startswith(r2.rstrip("/") + "/") or r2.startswith(remote.rstrip("/") + "/"):
                return f"overlaps with {r2}"
        return None

    def describe(self):
        return [{"local": l, "remote": r, "status": self.status.get(r, "waiting")} for l, r in self.pairs]

    # ---------- state

    def _state(self, pair):
        files = {k: (rel, size, mtime) for k, rel, size, mtime in self.d.db.execute(
            "SELECT k, rel, size, mtime FROM sync_files WHERE pair=?", (pair,))}
        dirs = {k for (k,) in self.d.db.execute("SELECT k FROM sync_dirs WHERE pair=?", (pair,))}
        return files, dirs

    def _remember(self, pair, rel, size, mtime):
        self.d.db.execute("INSERT OR REPLACE INTO sync_files(pair,k,rel,size,mtime) VALUES(?,?,?,?,?)",
                          (pair, key(rel), rel, size, mtime))

    def _forget(self, pair, k):
        self.d.db.execute("DELETE FROM sync_files WHERE pair=? AND k=?", (pair, k))

    def _remember_dir(self, pair, k, keep=True):
        if keep:
            self.d.db.execute("INSERT OR IGNORE INTO sync_dirs(pair,k) VALUES(?,?)", (pair, k))
        else:
            self.d.db.execute("DELETE FROM sync_dirs WHERE pair=? AND k=?", (pair, k))

    # ---------- drive side

    def _scan_remote(self, root):
        pre = "/" if root == "/" else root + "/"
        files, dirs = {}, {}

        def add(path, size, mtime):
            if path.startswith(TRASH_REMOTE + "/"):
                return
            rel = path[len(pre):]
            if any(skip_name(part) for part in rel.split("/")):
                return
            files[key(rel)] = (rel, size, mtime)
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                dirs[key(d)] = d

        for path, size, mtime in self.d.db.execute(
                "SELECT path,size,mtime FROM files WHERE status='complete' AND substr(path,1,?)=?",
                (len(pre), pre)):
            add(path, size, mtime)
        for path, size, mtime in self.d.db.execute(
                "SELECT path,size,mtime FROM pending WHERE substr(path,1,?)=?", (len(pre), pre)):
            add(path, size, mtime)                      # waiting uploads count as on the drive
        for (path,) in self.d.db.execute("SELECT path FROM dirs WHERE substr(path,1,?)=?", (len(pre), pre)):
            if path.startswith(TRASH_REMOTE):
                continue
            rel = path[len(pre):]
            dirs[key(rel)] = rel
        return files, dirs

    def _rpath(self, root, rel):
        return norm(posixpath.join(root, rel))

    def _remote_trash(self, root, rel):
        stamp = time.strftime("%Y-%m-%d %H%M")
        dest = self.d.free_path(norm(f"{TRASH_REMOTE}/{stamp}{self._rpath(root, rel)}"))
        self.d.mkdir(posixpath.dirname(dest))
        self.d.move(self._rpath(root, rel), dest)

    # ---------- PC side

    @staticmethod
    def _lpath(root, rel):
        return os.path.join(root, *rel.split("/"))

    def _local_trash(self, root, rel):
        src = self._lpath(root, rel)
        stamp = time.strftime("%Y-%m-%d %H%M")
        dest = os.path.join(TRASH_LOCAL, stamp, os.path.basename(root), *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            base, ext = os.path.splitext(dest)
            dest = f"{base} ({uuid.uuid4().hex[:6]}){ext}"
        shutil.move(src, dest)

    async def _fetch(self, root, rroot, rel, local_rel=None):
        """Download drive file rroot/rel into the PC folder root; returns (size, mtime)."""
        e = self.d.get_entry(self._rpath(rroot, rel))
        if e is None or e["status"] not in ("complete", "pending"):
            raise FileNotFoundError(rel)
        target = self._lpath(root, local_rel or rel)
        os.makedirs(SYNC_TMP, exist_ok=True)
        tmp = os.path.join(SYNC_TMP, uuid.uuid4().hex)
        try:
            if e["status"] == "pending":
                await asyncio.to_thread(shutil.copyfile, e["tmp"], tmp)
            else:
                await self.d.download(e["path"], tmp)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            await asyncio.to_thread(shutil.move, tmp, target)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        os.utime(target, (e["mtime"], e["mtime"]))
        return e["size"], e["mtime"]

    async def _send(self, root, rel, full):
        """Queue a PC file for upload; returns (size, mtime) that was queued."""
        tmp = self.d.new_staging_path()
        await asyncio.to_thread(shutil.copy2, full, tmp)
        st = os.stat(full)
        if os.path.getsize(tmp) != st.st_size or abs(os.path.getmtime(tmp) - st.st_mtime) > 2:
            self.d._discard(tmp)
            raise BlockingIOError("file changed while copying")
        self.d.stage(tmp, self._rpath(root, rel))
        return st.st_size, st.st_mtime

    @staticmethod
    def _conflict_name(rel):
        stem, ext = posixpath.splitext(rel)
        return f"{stem} (conflict {time.strftime('%Y-%m-%d %H%M')}){ext}"

    # ---------- the sync itself

    async def run_once(self):
        actions = []
        for local, remote in self.pairs:
            try:
                actions += await self._sync_pair(local, remote)
            except Exception as e:                       # keep the other pairs going
                self.status[remote] = f"error: {e}"
                self._warn(remote, f"sync {local} failed: {e!r}")
        self._clean_trash()
        return actions

    def _warn(self, tag, msg):
        if tag not in self.warned:
            self.warned.add(tag)
            self.log(msg)

    async def _sync_pair(self, root, rroot):
        pair, did = rroot, []
        st_files, st_dirs = self._state(pair)

        # never mirror a missing folder as "everything deleted"
        if not os.path.isdir(root):
            if st_files or st_dirs:
                self.status[pair] = "paused"
                self._warn(("missing", root), f"sync: {root} is missing (unplugged disk?). Paused.")
                return did
            os.makedirs(root, exist_ok=True)
        if not self.d.is_dir(rroot):
            if st_files or st_dirs:
                self.status[pair] = "paused"
                self._warn(("missing", rroot), f"sync: {rroot} no longer exists on the drive. Paused. "
                           f"Remove it from SYNC_FOLDERS or create it again.")
                return did
            self.d.mkdir(rroot)
        self.warned.discard(("missing", root))
        self.warned.discard(("missing", rroot))

        lf, ld, busy = await asyncio.to_thread(scan_local, root)
        rf, rd = self._scan_remote(rroot)

        up, down, conflict, del_local, del_remote = [], [], [], [], []
        for k in set(lf) | set(rf) | set(st_files):
            if k in busy:
                continue
            l, r, s = lf.get(k), rf.get(k), st_files.get(k)
            lv, rv, sv = (l[1:] if l else None), (r[1:] if r else None), (s[1:] if s else None)
            if l and r:
                if same(lv, rv):
                    if not same(lv, sv):
                        self._remember(pair, l[0], *lv)
                    continue
                lch, rch = not same(lv, sv), not same(rv, sv)
                if lch and not rch:
                    up.append(k)
                elif rch and not lch:
                    down.append(k)
                else:
                    conflict.append(k)
            elif l:
                (del_local if s and same(lv, sv) else up).append(k)
            elif r:
                (del_remote if s and same(rv, sv) else down).append(k)
            else:
                self._forget(pair, k)

        # renames: a "deleted" file and a "new" file with the same size and time
        def match(new_keys, new_side, gone_keys, gone_side):
            gone = {}
            for g in gone_keys:
                gone.setdefault(sig(gone_side[g][1:]), []).append(g)
            pairs = []
            for n in new_keys:
                cands = gone.get(sig(new_side[n][1:]), [])
                if len(cands) == 1:
                    pairs.append((cands.pop(), n))
            return pairs

        for g, n in match([k for k in up if k not in rf], lf, del_remote, rf):     # renamed on PC
            old, new = rf[g][0], lf[n][0]
            try:
                if not self.d.exists(self._rpath(rroot, new)):
                    self.d.move(self._rpath(rroot, old), self._rpath(rroot, new))
                    self._forget(pair, g)
                    self._remember(pair, new, *lf[n][1:])
                    did.append(f"renamed on drive: {rroot}/{old} -> {new}")
                    up.remove(n)
                    del_remote.remove(g)
            except (OSError, ValueError) as e:
                self.log(f"sync: rename of {old} failed: {e}")
        for g, n in match([k for k in down if k not in lf], rf, del_local, lf):    # renamed on drive
            old, new = lf[g][0], rf[n][0]
            src, dst = self._lpath(root, old), self._lpath(root, new)
            try:
                if not os.path.exists(dst):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.rename(src, dst)
                    self._forget(pair, g)
                    self._remember(pair, new, *rf[n][1:])
                    did.append(f"renamed on PC: {old} -> {new}")
                    down.remove(n)
                    del_local.remove(g)
            except OSError as e:
                self.log(f"sync: rename of {old} failed: {e}")
        self.d.db.commit()

        for k in up:
            rel = lf[k][0]
            try:
                size, mtime = await self._send(rroot, rel, self._lpath(root, rel))
                self._remember(pair, rel, size, mtime)
                did.append(f"PC -> drive: {rel}")
            except BlockingIOError:
                pass                                         # try again next round
            except (OSError, IsADirectoryError) as e:
                self._warn(("up", pair, k), f"sync: cannot upload {rel}: {e}")
        for k in down:
            rel = rf[k][0]
            local_rel = lf[k][0] if k in lf else rel
            try:
                if k in lf:
                    self._local_trash(root, local_rel)       # keep the old version, just in case
                size, mtime = await self._fetch(root, rroot, rel, local_rel)
                self._remember(pair, local_rel, size, mtime)
                did.append(f"drive -> PC: {rel}")
            except (OSError, ValueError) as e:
                self._warn(("down", pair, k), f"sync: cannot download {rel}: {e}")
        for k in conflict:
            rel = lf[k][0]
            renamed = self._conflict_name(rel)
            try:
                os.rename(self._lpath(root, rel), self._lpath(root, renamed))
                size, mtime = await self._fetch(root, rroot, rf[k][0], rel)
                self._remember(pair, rel, size, mtime)
                did.append(f"conflict: kept both, your PC copy is now {renamed}")
            except (OSError, ValueError) as e:
                self._warn(("conflict", pair, k), f"sync: conflict on {rel} not resolved: {e}")
        for k in del_local:
            rel = lf[k][0]
            try:
                self._local_trash(root, rel)
                self._forget(pair, k)
                did.append(f"deleted on drive, moved to sync-trash on PC: {rel}")
            except OSError as e:
                self._warn(("dl", pair, k), f"sync: cannot remove {rel} from PC: {e}")
        for k in del_remote:
            rel = rf[k][0]
            try:
                self._remote_trash(rroot, rel)
                self._forget(pair, k)
                did.append(f"deleted on PC, moved to {TRASH_REMOTE} on drive: {rel}")
            except (OSError, ValueError) as e:
                self._warn(("dr", pair, k), f"sync: cannot remove {rel} from drive: {e}")
        self.d.db.commit()

        # folders (also empty ones), after the files
        for k in sorted(set(ld) | set(rd) | st_dirs, key=lambda x: -x.count("/")):
            in_l, in_r = k in ld, k in rd
            if in_l and in_r:
                self._remember_dir(pair, k)
            elif in_l:
                rel = ld[k]
                if k in st_dirs:                            # deleted on the drive
                    try:
                        os.rmdir(self._lpath(root, rel))     # only if empty
                        self._remember_dir(pair, k, False)
                    except OSError:
                        self.d.mkdir(self._rpath(rroot, rel))
                else:
                    self.d.mkdir(self._rpath(rroot, rel))
                    self._remember_dir(pair, k)
            elif in_r:
                rel = rd[k]
                rp = self._rpath(rroot, rel)
                if k in st_dirs and not self._remote_has_files(rp):    # deleted on the PC
                    await self.d.delete(rp)
                    self._remember_dir(pair, k, False)
                else:
                    os.makedirs(self._lpath(root, rel), exist_ok=True)
                    self._remember_dir(pair, k)
            else:
                self._remember_dir(pair, k, False)
        self.d.db.commit()
        self.status[pair] = f"in sync ({time.strftime('%H:%M')})"
        return did

    def _remote_has_files(self, rp):
        pre = rp + "/"
        for table in ("files", "pending"):
            if self.d.db.execute(f"SELECT 1 FROM {table} WHERE substr(path,1,?)=? LIMIT 1",
                                 (len(pre), pre)).fetchone():
                return True
        return False

    def _clean_trash(self):
        """Empty PC sync-trash folders older than TRASH_KEEP_DAYS."""
        if not os.path.isdir(TRASH_LOCAL):
            return
        cutoff = time.time() - TRASH_KEEP_DAYS * 86400
        for name in os.listdir(TRASH_LOCAL):
            p = os.path.join(TRASH_LOCAL, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
