"""
TelegramDrive core.

Stores files in your private Telegram channel:
  - each file is split into chunks of CHUNK_SIZE bytes
  - each chunk is sent to the channel as one document message
  - a local SQLite index (DATA_DIR/index.db) remembers which messages
    belong to which file, plus folders, sizes and checksums
  - optional AES-256-CTR encryption (ENCRYPT / PASSPHRASE in config.py)

Files added through the web dashboard or the Windows drive are first saved
in DATA_DIR/incoming ("pending"), shown in the drive immediately, and then
uploaded to Telegram in the background by upload_worker().

The web dashboard (server.py) and the Windows drive (dav.py) use this module.
"""
import asyncio
import hashlib
import os
import posixpath
import re
import secrets
import shutil
import sqlite3
import time
import uuid

from telethon import TelegramClient
from telethon.tl.types import (DocumentAttributeFilename, MessageMediaDocument,
                               MessageMediaPhoto)

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = getattr(config, "SESSION_PATH", None) or os.path.join(BASE_DIR, "session")  # + ".session"
DATA_DIR = config.DATA_DIR
DB_PATH = os.path.join(DATA_DIR, "index.db")
INCOMING = os.path.join(DATA_DIR, "incoming")      # local copies waiting to go to Telegram
CHUNK_SIZE = int(config.CHUNK_SIZE)
TELEGRAM_MAX = 2000 * 1024 * 1024                  # 2 GB limit for non-premium accounts
REQUEST_SIZE = 512 * 1024                          # download block size
UPLOAD_RETRIES = 5
MEDIA_CACHE_SECONDS = 1800


# ---------------------------------------------------------------- helpers

class NotLoggedIn(RuntimeError):
    pass


def norm(path):
    """Normalise a drive path: forward slashes, one leading '/', no trailing '/'."""
    p = (path or "/").replace("\\", "/").strip("/")
    p = posixpath.normpath("/" + p)
    return "/" if p in ("/", ".") else p


def _derive_key(passphrase, salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode("utf-8"))


def _ctr(key, nonce, offset):
    """AES-CTR cipher positioned at an arbitrary byte offset of the file.
    CTR encrypt == decrypt, and it lets us start anywhere (needed for streaming)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    counter = (int.from_bytes(nonce, "big") + offset // 16) % (1 << 128)
    ctx = Cipher(algorithms.AES(key), modes.CTR(counter.to_bytes(16, "big"))).encryptor()
    if offset % 16:
        ctx.update(b"\0" * (offset % 16))
    return ctx


class ChunkReader:
    """File-like object exposing bytes [start, start+length) of a local file,
    optionally encrypted on the fly, hashing the plaintext as it is read.
    Lets Telethon upload a 1.9 GB chunk without loading it into RAM or
    writing a temporary copy."""

    def __init__(self, path, start, length, cipher_factory=None):
        self.f = open(path, "rb")
        self.start = start
        self.length = length
        self.pos = 0
        self.name = "chunk.bin"
        self._cipher_factory = cipher_factory
        self._cipher = None
        self._cipher_pos = None
        self._hash = hashlib.sha256()
        self.hashed = 0

    def read(self, n=-1):
        remaining = self.length - self.pos
        if n is None or n < 0 or n > remaining:
            n = remaining
        if n <= 0:
            return b""
        self.f.seek(self.start + self.pos)
        data = self.f.read(n)
        if not data:
            raise IOError("local file shrank while uploading")
        if self.pos == self.hashed:            # hash each byte exactly once, in order
            self._hash.update(data)
            self.hashed += len(data)
        if self._cipher_factory:
            if self._cipher is None or self._cipher_pos != self.pos:
                self._cipher = self._cipher_factory(self.start + self.pos)
            out = self._cipher.update(data)
            self._cipher_pos = self.pos + len(data)
        else:
            out = data
        self.pos += len(data)
        return out

    def seek(self, offset, whence=0):
        if whence == 0:
            new = offset
        elif whence == 1:
            new = self.pos + offset
        else:
            new = self.length + offset
        self.pos = max(0, min(self.length, new))
        return self.pos

    def tell(self):
        return self.pos

    def seekable(self):
        return True

    def hexdigest(self):
        return self._hash.hexdigest()

    def close(self):
        self.f.close()


# ---------------------------------------------------------------- drive

class Drive:
    def __init__(self):
        if not str(config.CHANNEL).strip():
            raise SystemExit("CHANNEL is empty in config.py. Put the -100... ID from telegram_login.py there.")
        if CHUNK_SIZE > TELEGRAM_MAX:
            raise SystemExit("CHUNK_SIZE is bigger than Telegram's 2 GB limit.")
        os.makedirs(DATA_DIR, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_db()
        from proxy import ProxyManager
        self.proxy = ProxyManager(config)              # direct, or through a proxy (proxy.py)
        self.client = None                             # created in connect()
        self.channel = None
        self._key = None
        self._media_cache = {}
        # background upload state
        os.makedirs(INCOMING, exist_ok=True)
        self._wake = asyncio.Event()
        self._current = None          # (pending id, asyncio task) being uploaded right now
        self._trash = set()           # staged files to delete once Windows releases them
        self._retry_at = {}           # pending id -> time of next attempt
        self.progress = {}            # pending id -> {"done", "total", "error"}
        self.recent = []              # finished uploads, newest last (for the dashboard)

    # ---------- lifecycle

    def _new_client(self, via):
        from proxy import client_kwargs
        c = TelegramClient(SESSION_PATH, config.API_ID, config.API_HASH, **client_kwargs(via))
        c.flood_sleep_threshold = 300                  # auto-wait on short flood limits
        return c

    async def connect(self, interactive=True):
        """interactive=True asks for the login code in the console if needed (drive.py,
        server.py). The app passes False and shows its setup wizard instead."""
        via = await self.proxy.choose()
        self.client = self._new_client(via)
        if interactive:
            await self.client.start(phone=config.PHONE)
        else:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise NotLoggedIn("Telegram login is missing or expired")
        cid = int(config.CHANNEL)
        try:
            self.channel = await self.client.get_entity(cid)
        except ValueError:
            await self.client.get_dialogs()            # refresh entity cache, then retry
            self.channel = await self.client.get_entity(cid)
        return self

    async def switch_connection(self, force_search=True):
        """Reconnect through another route (a new proxy, or direct). Uploads in
        progress fail once and are retried by the upload queue."""
        exclude = [self.proxy.current] if self.proxy.current else ["direct"]
        via = await self.proxy.choose(force_search=force_search, exclude=exclude if force_search else ())
        old = self.client
        if old is not None:
            try:
                await asyncio.wait_for(old.disconnect(), 10)
            except Exception:
                pass
        self.client = self._new_client(via)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise NotLoggedIn("Telegram login is missing or expired")
        self.channel = await self.client.get_entity(int(config.CHANNEL))
        self._media_cache.clear()
        return via

    async def close(self):
        if self.client is not None:
            await self.client.disconnect()
        self.db.close()

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *exc):
        await self.close()

    # ---------- database

    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS files(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            path       TEXT UNIQUE NOT NULL,
            size       INTEGER NOT NULL,
            mtime      REAL NOT NULL,
            created    REAL NOT NULL,
            encrypted  INTEGER NOT NULL DEFAULT 0,
            nonce      BLOB,
            chunk_size INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'uploading'
        );
        CREATE TABLE IF NOT EXISTS chunks(
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            idx     INTEGER NOT NULL,
            msg_id  INTEGER NOT NULL,
            start   INTEGER NOT NULL,
            size    INTEGER NOT NULL,
            sha256  TEXT NOT NULL,
            PRIMARY KEY(file_id, idx)
        );
        CREATE TABLE IF NOT EXISTS dirs(path TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value BLOB);
        CREATE INDEX IF NOT EXISTS chunks_msg ON chunks(msg_id);
        CREATE TABLE IF NOT EXISTS pending(
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            path    TEXT UNIQUE NOT NULL,
            tmp     TEXT NOT NULL,
            size    INTEGER NOT NULL,
            mtime   REAL NOT NULL,
            created REAL NOT NULL
        );
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(files)")}
        if "origin" not in cols:     # 'local' = uploaded by SpiDrive, 'telegram' = sent from Telegram
            self.db.execute("ALTER TABLE files ADD COLUMN origin TEXT NOT NULL DEFAULT 'local'")
        self.db.commit()

    def version(self):
        """Changes whenever the index changes (here or in another process)."""
        dv = self.db.execute("PRAGMA data_version").fetchone()[0]
        return f"{self.db.total_changes}-{dv}"

    def _meta(self, key):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def _set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
        self.db.commit()

    def _get_key(self):
        if self._key is None:
            if not config.PASSPHRASE:
                raise RuntimeError("This needs encryption but PASSPHRASE is empty in config.py.")
            salt = self._meta("salt")
            if salt is None:
                salt = secrets.token_bytes(16)
                self._set_meta("salt", salt)
            key = _derive_key(config.PASSPHRASE, salt)
            check = hashlib.sha256(b"tgdrive-check" + key).digest()
            stored = self._meta("key_check")
            if stored is None:
                self._set_meta("key_check", check)
            elif stored != check:
                raise RuntimeError("Wrong PASSPHRASE: it differs from the one used for earlier files.")
            self._key = key
        return self._key

    def _chunks(self, file_id):
        return self.db.execute(
            "SELECT * FROM chunks WHERE file_id=? ORDER BY idx", (file_id,)).fetchall()

    # ---------- queries
    #
    # An "entry" is a dict describing one file. Files waiting in the upload
    # queue have status 'pending' and a negative id; they shadow an older
    # Telegram copy at the same path until their upload finishes.

    def get_file(self, path):
        """The Telegram-stored file at `path` (any status), or None."""
        return self.db.execute("SELECT * FROM files WHERE path=?", (norm(path),)).fetchone()

    def get_file_by_id(self, file_id):
        return self.db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()

    def _pending_row(self, path):
        return self.db.execute("SELECT * FROM pending WHERE path=?", (norm(path),)).fetchone()

    @staticmethod
    def _pending_entry(p):
        return {"id": -p["id"], "path": p["path"], "size": p["size"], "mtime": p["mtime"],
                "created": p["created"], "encrypted": int(bool(config.ENCRYPT)), "nonce": None,
                "chunk_size": 0, "status": "pending", "origin": "local", "tmp": p["tmp"]}

    def get_entry(self, path):
        """Pending upload or stored file at `path`, as a dict, or None."""
        p = self._pending_row(path)
        if p is not None:
            return self._pending_entry(p)
        r = self.get_file(path)
        return dict(r) if r is not None else None

    def get_entry_by_id(self, entry_id):
        if entry_id < 0:
            p = self.db.execute("SELECT * FROM pending WHERE id=?", (-entry_id,)).fetchone()
            return self._pending_entry(p) if p is not None else None
        r = self.get_file_by_id(entry_id)
        if r is None or self._pending_row(r["path"]) is not None:
            return None
        return dict(r)

    def exists(self, path):
        return self.get_entry(path) is not None or self.is_dir(path)

    def free_path(self, path):
        """`path` if unused, otherwise 'name (2).ext', 'name (3).ext', ..."""
        path = norm(path)
        if not self.exists(path):
            return path
        stem, ext = posixpath.splitext(path)
        i = 2
        while True:
            p = f"{stem} ({i}){ext}"
            if not self.exists(p):
                return p
            i += 1

    def search(self, query, limit=300):
        """Files whose name contains `query` (case-insensitive)."""
        q = query.strip().lower()
        if not q:
            return []
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = (f"%{esc}%",)
        found = {p["path"]: self._pending_entry(p) for p in self.db.execute(
            "SELECT * FROM pending WHERE path LIKE ? ESCAPE '\\'", like)}
        for r in self.db.execute(
                "SELECT * FROM files WHERE status='complete' AND path LIKE ? ESCAPE '\\'", like):
            found.setdefault(r["path"], dict(r))
        hits = [e for p, e in sorted(found.items()) if q in posixpath.basename(p).lower()]
        return hits[:limit]

    def is_dir(self, path):
        path = norm(path)
        if path == "/":
            return True
        if self.db.execute("SELECT 1 FROM dirs WHERE path=?", (path,)).fetchone():
            return True
        pre = path + "/"
        for table in ("files", "pending"):
            if self.db.execute(f"SELECT 1 FROM {table} WHERE substr(path,1,?)=? LIMIT 1",
                               (len(pre), pre)).fetchone():
                return True
        return False

    def listdir(self, path="/"):
        """Return (sorted subfolder names, file entries) directly inside `path`."""
        path = norm(path)
        pre = "/" if path == "/" else path + "/"
        dirs, files = set(), {}
        for p in self.db.execute("SELECT * FROM pending WHERE substr(path,1,?)=?", (len(pre), pre)):
            rest = p["path"][len(pre):]
            if "/" in rest:
                dirs.add(rest.split("/")[0])
            else:
                files[p["path"]] = self._pending_entry(p)
        for r in self.db.execute("SELECT * FROM files WHERE substr(path,1,?)=?", (len(pre), pre)):
            rest = r["path"][len(pre):]
            if "/" in rest:
                dirs.add(rest.split("/")[0])
            else:
                files.setdefault(r["path"], dict(r))
        for (p,) in self.db.execute(
                "SELECT path FROM dirs WHERE substr(path,1,?)=?", (len(pre), pre)):
            dirs.add(p[len(pre):].split("/")[0])
        return sorted(dirs), [files[k] for k in sorted(files)]

    def walk(self, path="/"):
        """All complete files under a folder (recursive)."""
        path = norm(path)
        pre = "/" if path == "/" else path + "/"
        return self.db.execute(
            "SELECT * FROM files WHERE substr(path,1,?)=? AND status='complete' ORDER BY path",
            (len(pre), pre)).fetchall()

    def stats(self):
        r = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE status='complete'").fetchone()
        inc = self.db.execute("SELECT COUNT(*) FROM files WHERE status!='complete'").fetchone()[0]
        p = self.db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM pending").fetchone()
        return {"files": r[0], "bytes": r[1], "incomplete": inc,
                "pending": p[0], "pending_bytes": p[1]}

    def set_mtime(self, path, mtime):
        """Change a file's modified time (Windows sends it after copying a file)."""
        p = self._pending_row(path)
        if p is not None:
            try:
                os.utime(p["tmp"], (mtime, mtime))
            except OSError:
                pass
            self.db.execute("UPDATE pending SET mtime=? WHERE id=?", (mtime, p["id"]))
        else:
            self.db.execute("UPDATE files SET mtime=? WHERE path=?", (mtime, norm(path)))
        self.db.commit()

    # ---------- folders

    def mkdir(self, path):
        path = norm(path)
        if self.get_entry(path):
            raise FileExistsError(f"a file already exists at {path}")
        while path != "/":
            self.db.execute("INSERT OR IGNORE INTO dirs(path) VALUES(?)", (path,))
            path = posixpath.dirname(path)
        self.db.commit()

    def move(self, src, dst):
        """Rename/move a file or folder (only changes the index, nothing is re-uploaded)."""
        src, dst = norm(src), norm(dst)
        if src == "/" or dst == "/":
            raise ValueError("cannot move the root folder")
        if src == dst:
            return
        if self.exists(dst):
            raise FileExistsError(dst)
        if self.get_entry(src):
            self.db.execute("UPDATE pending SET path=? WHERE path=?", (dst, src))
            self.db.execute("UPDATE files SET path=? WHERE path=?", (dst, src))
        elif self.is_dir(src):
            if dst.startswith(src + "/"):
                raise ValueError("cannot move a folder inside itself")
            pre = src + "/"
            for table in ("files", "pending", "dirs"):
                rows = self.db.execute(
                    f"SELECT path FROM {table} WHERE path=? OR substr(path,1,?)=?",
                    (src, len(pre), pre)).fetchall()
                for (p,) in rows:
                    self.db.execute(f"UPDATE {table} SET path=? WHERE path=?", (dst + p[len(src):], p))
        else:
            raise FileNotFoundError(src)
        self.db.commit()
        if posixpath.dirname(dst) != "/":
            self.mkdir(posixpath.dirname(dst))

    async def copy(self, src, dst):
        """Copy a file or folder. Stored files are not uploaded again: the copy
        points at the same Telegram messages (they are only deleted from the
        channel when no copy uses them any more)."""
        src, dst = norm(src), norm(dst)
        if self.exists(dst):
            raise FileExistsError(dst)
        if self.get_entry(src):
            pairs = [(src, dst)]
        elif self.is_dir(src):
            if dst == src or dst.startswith(src + "/"):
                raise ValueError("cannot copy a folder inside itself")
            pre = src + "/"
            paths = {p for (p,) in self.db.execute(
                "SELECT path FROM pending WHERE substr(path,1,?)=? UNION "
                "SELECT path FROM files WHERE substr(path,1,?)=? AND status='complete'",
                (len(pre), pre, len(pre), pre))}
            pairs = [(p, dst + p[len(src):]) for p in sorted(paths)]
            self.mkdir(dst)
            for (d,) in self.db.execute(
                    "SELECT path FROM dirs WHERE substr(path,1,?)=?", (len(pre), pre)).fetchall():
                self.mkdir(dst + d[len(src):])
        else:
            raise FileNotFoundError(src)
        for a, b in pairs:
            e = self.get_entry(a)
            if e["status"] == "pending":
                tmp = self.new_staging_path()
                await asyncio.to_thread(shutil.copy2, e["tmp"], tmp)
                self.stage(tmp, b)
            elif e["status"] == "complete":
                cur = self.db.execute(
                    "INSERT INTO files(path,size,mtime,created,encrypted,nonce,chunk_size,status,origin) "
                    "VALUES(?,?,?,?,?,?,?,'complete',?)",
                    (b, e["size"], e["mtime"], time.time(), e["encrypted"], e["nonce"],
                     e["chunk_size"], e["origin"]))
                self.db.execute(
                    "INSERT INTO chunks(file_id,idx,msg_id,start,size,sha256) "
                    "SELECT ?,idx,msg_id,start,size,sha256 FROM chunks WHERE file_id=?",
                    (cur.lastrowid, e["id"]))
                self.db.commit()
        if posixpath.dirname(dst) != "/":
            self.mkdir(posixpath.dirname(dst))
        return len(pairs)

    # ---------- staging (files waiting to be uploaded to Telegram)

    def new_staging_path(self):
        return os.path.join(INCOMING, uuid.uuid4().hex)

    def stage(self, tmp, path, mtime=None):
        """Take ownership of the local file `tmp` and queue it for upload to
        `path`. It appears in the drive straight away."""
        path = norm(path)
        if path == "/" or self.is_dir(path):
            self._discard(tmp)
            raise IsADirectoryError(path)
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
        old = self._pending_row(path)
        if old is not None:
            self._cancel_if_current(old["id"])
            self._discard(old["tmp"])
        self.db.execute("DELETE FROM pending WHERE path=?", (path,))
        self.db.execute(
            "INSERT INTO pending(path,tmp,size,mtime,created) VALUES(?,?,?,?,?)",
            (path, tmp, os.path.getsize(tmp), os.path.getmtime(tmp), time.time()))
        self.db.commit()
        if posixpath.dirname(path) != "/":
            self.mkdir(posixpath.dirname(path))
        self._wake.set()
        return path

    def pending(self):
        return self.db.execute("SELECT * FROM pending ORDER BY id").fetchall()

    def current_pending_id(self):
        return self._current[0] if self._current else None

    def _cancel_if_current(self, pending_id):
        if self._current and self._current[0] == pending_id:
            self._current[1].cancel()

    def _discard(self, tmp):
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        except OSError:
            self._trash.add(tmp)      # still open (Windows); try again later

    def _empty_trash(self):
        for tmp in list(self._trash):
            try:
                os.remove(tmp)
                self._trash.discard(tmp)
            except FileNotFoundError:
                self._trash.discard(tmp)
            except OSError:
                pass

    def clean_incoming(self):
        """Remove staged files that no pending upload refers to (after a crash)."""
        keep = {os.path.normcase(os.path.abspath(p["tmp"])) for p in self.pending()}
        for name in os.listdir(INCOMING):
            full = os.path.join(INCOMING, name)
            if os.path.normcase(os.path.abspath(full)) not in keep:
                self._discard(full)
        for p in self.pending():
            if not os.path.exists(p["tmp"]):
                self.db.execute("DELETE FROM pending WHERE id=?", (p["id"],))
        self.db.commit()

    def _finished(self, path, state, size, error=None):
        self.recent.append({"path": path, "state": state, "total": size,
                            "error": error, "t": time.time()})
        del self.recent[:-200]

    async def upload_worker(self, log=print):
        """Runs forever: uploads pending files to Telegram one at a time,
        retrying with a growing delay when the network fails."""
        while True:
            self._empty_trash()
            now = time.time()
            job = None
            for p in self.pending():
                if self._retry_at.get(p["id"], 0) <= now:
                    job = p
                    break
            if job is None:
                waits = [t - now for t in self._retry_at.values() if t > now]
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=min(waits + [30]))
                except asyncio.TimeoutError:
                    pass
                continue

            pid, target = job["id"], job["path"]
            if not os.path.exists(job["tmp"]):
                self.db.execute("DELETE FROM pending WHERE id=?", (pid,))
                self.db.commit()
                continue
            prog = self.progress.setdefault(pid, {"done": 0, "total": job["size"], "error": None})
            prog["error"] = None

            def cb(done, total, prog=prog):
                prog["done"], prog["total"] = done, total

            task = asyncio.create_task(self.upload(job["tmp"], target, overwrite=True, progress=cb))
            self._current = (pid, task)
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                task.cancel()
                raise
            finally:
                self._current = None

            now_p = self.db.execute("SELECT * FROM pending WHERE id=?", (pid,)).fetchone()
            if task.cancelled() or now_p is None:
                # deleted or replaced while uploading
                self.progress.pop(pid, None)
                self._retry_at.pop(pid, None)
                if not task.cancelled() and task.exception() is None and now_p is None \
                        and self._pending_row(target) is None:
                    row = self.get_file_by_id(task.result()["id"])
                    if row is not None:
                        await self._delete_rows([row])
                continue
            exc = task.exception()
            if exc is not None:
                tries = prog.get("tries", 0) + 1
                prog["tries"] = tries
                wait = min(300, 15 * 2 ** (tries - 1))
                prog["error"] = f"{exc or type(exc).__name__}"
                self._retry_at[pid] = time.time() + wait
                log(f"upload failed ({target}): {exc!r}. Retrying in {wait}s")
                continue

            row = self.get_file_by_id(task.result()["id"])
            if row is not None and row["path"] != now_p["path"]:     # renamed while uploading
                other = self.get_file(now_p["path"])
                if other is not None and other["id"] != row["id"]:
                    await self._delete_rows([other])
                self.db.execute("UPDATE files SET path=? WHERE id=?", (now_p["path"], row["id"]))
            if row is not None and row["mtime"] != now_p["mtime"]:
                self.db.execute("UPDATE files SET mtime=? WHERE id=?", (now_p["mtime"], row["id"]))
            self.db.execute("DELETE FROM pending WHERE id=?", (pid,))
            self.db.commit()
            self._discard(now_p["tmp"])
            self.progress.pop(pid, None)
            self._retry_at.pop(pid, None)
            self._finished(now_p["path"], "done", now_p["size"])
            log(f"uploaded {now_p['path']}")

    # ---------- upload

    async def upload(self, local_path, remote_path, overwrite=False, progress=None):
        """Upload a local file. Interrupted uploads resume from the last finished chunk."""
        remote = norm(remote_path)
        if remote == "/" or self.is_dir(remote):
            raise IsADirectoryError(remote)
        size = os.path.getsize(local_path)
        mtime = os.path.getmtime(local_path)
        enc = bool(config.ENCRYPT)

        row = self.get_file(remote)
        if row is not None:
            if row["status"] == "complete":
                if not overwrite:
                    raise FileExistsError(remote)
                await self._delete_rows([row])
                row = None
            elif (row["size"], row["mtime"], row["encrypted"]) != (size, mtime, int(enc)):
                await self._delete_rows([row])         # stale partial upload of a different file
                row = None
        if row is None:
            self.db.execute(
                "INSERT INTO files(path,size,mtime,created,encrypted,nonce,chunk_size,status) "
                "VALUES(?,?,?,?,?,?,?,'uploading')",
                (remote, size, mtime, time.time(), int(enc),
                 secrets.token_bytes(16) if enc else None, CHUNK_SIZE))
            self.db.commit()
            row = self.get_file(remote)

        key = self._get_key() if enc else None
        chunk_size = row["chunk_size"]
        n = (size + chunk_size - 1) // chunk_size
        done = {c["idx"] for c in self._chunks(row["id"])}

        for idx in range(n):
            if idx in done:
                continue
            start = idx * chunk_size
            length = min(chunk_size, size - start)
            msg_id, digest = await self._upload_chunk(
                local_path, row, idx, n, start, length, key, progress)
            self.db.execute(
                "INSERT INTO chunks(file_id,idx,msg_id,start,size,sha256) VALUES(?,?,?,?,?,?)",
                (row["id"], idx, msg_id, start, length, digest))
            self.db.commit()

        self.db.execute("UPDATE files SET status='complete' WHERE id=?", (row["id"],))
        self.db.commit()
        if posixpath.dirname(remote) != "/":
            self.mkdir(posixpath.dirname(remote))
        return self.get_file_by_id(row["id"])

    async def _upload_chunk(self, local_path, row, idx, n, start, length, key, progress):
        remote, total = row["path"], row["size"]
        if key:
            name = f"{row['id']}_{idx}.bin"           # hide real names when encrypted
            caption = ""
            factory = lambda off: _ctr(key, row["nonce"], off)
        else:
            base = posixpath.basename(remote)
            name = base if n == 1 else f"{base}.part{idx + 1:03d}"
            caption = f"{remote[-900:]}\n[{idx + 1}/{n}]"
            factory = None

        def cb(sent, _):
            if progress:
                progress(start + sent, total)

        for attempt in range(1, UPLOAD_RETRIES + 1):
            reader = ChunkReader(local_path, start, length, factory)
            try:
                handle = await self.client.upload_file(
                    reader, file_size=length, file_name=name,
                    part_size_kb=512, progress_callback=cb)
                msg = await self.client.send_file(
                    self.channel, handle, force_document=True, caption=caption,
                    attributes=[DocumentAttributeFilename(name)])
                if reader.hashed != length:
                    raise IOError("chunk was not read completely")
                return msg.id, reader.hexdigest()
            except (OSError, ConnectionError, asyncio.TimeoutError) as e:
                if attempt == UPLOAD_RETRIES:
                    raise
                wait = 10 * attempt
                print(f"\n  network error ({e!r}), retrying chunk {idx + 1} in {wait}s ...")
                await asyncio.sleep(wait)
            finally:
                reader.close()

    # ---------- download / streaming

    async def _media(self, msg_id, fresh=False):
        hit = self._media_cache.get(msg_id)
        if hit and not fresh and time.time() - hit[1] < MEDIA_CACHE_SECONDS:
            return hit[0]
        msg = await self.client.get_messages(self.channel, ids=msg_id)
        if msg is None or msg.media is None:
            raise FileNotFoundError(f"message {msg_id} is missing from the channel")
        self._media_cache[msg_id] = (msg.media, time.time())
        return msg.media

    async def iter_bytes(self, remote, offset=0, length=None):
        """Async generator yielding the (decrypted) bytes of a stored file,
        optionally only a byte range. Used by download and by the servers."""
        row = self.get_file(remote)
        if row is None or row["status"] != "complete":
            raise FileNotFoundError(remote)
        size = row["size"]
        end = size if length is None else min(size, offset + length)
        if offset >= end:
            return
        key = self._get_key() if row["encrypted"] else None

        for ch in self._chunks(row["id"]):
            c_start, c_end = ch["start"], ch["start"] + ch["size"]
            if c_end <= offset or c_start >= end:
                continue
            lo = max(offset, c_start) - c_start
            hi = min(end, c_end) - c_start
            aligned = lo - lo % REQUEST_SIZE              # Telegram wants aligned offsets
            skip, remaining = lo - aligned, hi - lo
            cipher = _ctr(key, row["nonce"], c_start + lo) if key else None
            media = await self._media(ch["msg_id"])
            async for data in self.client.iter_download(
                    media, offset=aligned, request_size=REQUEST_SIZE):
                data = bytes(data)
                if skip:
                    if len(data) <= skip:
                        skip -= len(data)
                        continue
                    data, skip = data[skip:], 0
                if len(data) > remaining:
                    data = data[:remaining]
                if cipher:
                    data = cipher.update(data)
                remaining -= len(data)
                yield data
                if remaining <= 0:
                    break
            if remaining > 0:
                raise IOError(f"chunk {ch['idx']} of {remote} ended early")

    async def download(self, remote, local_path, progress=None):
        """Download a file to disk and verify every chunk's SHA-256."""
        row = self.get_file(remote)
        if row is None or row["status"] != "complete":
            raise FileNotFoundError(remote)
        folder = os.path.dirname(os.path.abspath(local_path))
        os.makedirs(folder, exist_ok=True)
        tmp = local_path + ".part"
        done = 0
        try:
            with open(tmp, "wb") as out:
                for ch in self._chunks(row["id"]):
                    h = hashlib.sha256()
                    async for data in self.iter_bytes(row["path"], ch["start"], ch["size"]):
                        out.write(data)
                        h.update(data)
                        done += len(data)
                        if progress:
                            progress(done, row["size"])
                    if ch["sha256"] and h.hexdigest() != ch["sha256"]:
                        raise IOError(f"checksum mismatch in chunk {ch['idx'] + 1} of {row['path']}")
            os.replace(tmp, local_path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        os.utime(local_path, (row["mtime"], row["mtime"]))
        return local_path

    async def iter_entry(self, entry, offset=0, length=None):
        """Like iter_bytes, but also works for files still waiting to upload
        (those are read from the local copy)."""
        if entry["status"] != "pending":
            async for data in self.iter_bytes(entry["path"], offset, length):
                yield data
            return
        end = entry["size"] if length is None else min(entry["size"], offset + length)
        with open(entry["tmp"], "rb") as f:
            f.seek(offset)
            pos = offset
            while pos < end:
                data = await asyncio.to_thread(f.read, min(REQUEST_SIZE, end - pos))
                if not data:
                    break
                pos += len(data)
                yield data

    # ---------- delete

    async def _delete_rows(self, rows):
        """Remove stored files from the index, and their messages from the
        channel unless another file (a copy) still uses them."""
        if not rows:
            return
        gone = {r["id"] for r in rows}
        ids = sorted({c["msg_id"] for r in rows for c in self._chunks(r["id"])})
        shared = set()
        for mid in ids:
            for (fid,) in self.db.execute("SELECT file_id FROM chunks WHERE msg_id=?", (mid,)):
                if fid not in gone:
                    shared.add(mid)
        ids = [m for m in ids if m not in shared]
        for i in range(0, len(ids), 100):
            await self.client.delete_messages(self.channel, ids[i:i + 100])
        for mid in ids:
            self._media_cache.pop(mid, None)
        for fid in gone:
            self.db.execute("DELETE FROM files WHERE id=?", (fid,))
        self.db.commit()

    async def delete(self, path):
        """Delete a file, or a folder and everything in it (also from the channel)."""
        path = norm(path)
        if self.get_entry(path) is not None:
            rows = [r for r in [self.get_file(path)] if r is not None]
            pend = [p for p in [self._pending_row(path)] if p is not None]
            is_folder = False
        else:
            pre = "/" if path == "/" else path + "/"
            rows = self.db.execute(
                "SELECT * FROM files WHERE substr(path,1,?)=?", (len(pre), pre)).fetchall()
            pend = self.db.execute(
                "SELECT * FROM pending WHERE substr(path,1,?)=?", (len(pre), pre)).fetchall()
            is_folder = True
        for p in pend:
            self._cancel_if_current(p["id"])
            self.db.execute("DELETE FROM pending WHERE id=?", (p["id"],))
            self._discard(p["tmp"])
        self.db.commit()
        await self._delete_rows(rows)
        if is_folder:
            pre = path + "/"
            self.db.execute("DELETE FROM dirs WHERE path=? OR substr(path,1,?)=?", (path, len(pre), pre))
            self.db.commit()
        return len({r["path"] for r in rows} | {p["path"] for p in pend})

    # ---------- Telegram -> drive

    INBOX = "/Telegram Inbox"
    _OWN_CAPTION = re.compile(r"\n\[\d+/\d+\]$")
    _OWN_ENC_NAME = re.compile(r"^\d+_\d+\.bin$")

    async def import_new_messages(self, min_age=20):
        """Add files that were sent or forwarded to the channel from Telegram
        (not by SpiDrive) to the drive, in /Telegram Inbox. Returns new paths."""
        last = int(self._meta("last_scanned") or 0)
        newest, imported = last, []
        async for msg in self.client.iter_messages(self.channel, min_id=last, reverse=True):
            if time.time() - msg.date.timestamp() < min_age:
                break                                   # too fresh: may be our own upload in progress
            newest = msg.id
            if not isinstance(msg.media, (MessageMediaDocument, MessageMediaPhoto)) or msg.file is None:
                continue
            caption = msg.message or ""
            if caption.startswith("#index_backup") or self._OWN_CAPTION.search(caption):
                continue
            if self.db.execute("SELECT 1 FROM chunks WHERE msg_id=?", (msg.id,)).fetchone():
                continue
            name = msg.file.name
            if name and self._OWN_ENC_NAME.match(name) and not caption:
                continue                                # leftover piece of an interrupted encrypted upload
            if not name:
                kind = "photo" if isinstance(msg.media, MessageMediaPhoto) else "file"
                name = f"{kind}_{msg.date:%Y%m%d_%H%M%S}_{msg.id}{msg.file.ext or ''}"
            name = name.replace("/", "_").replace("\\", "_")
            size = int(msg.file.size or 0)
            path = self.free_path(f"{self.INBOX}/{name}")
            cur = self.db.execute(
                "INSERT INTO files(path,size,mtime,created,encrypted,nonce,chunk_size,status,origin) "
                "VALUES(?,?,?,?,0,NULL,?,'complete','telegram')",
                (path, size, msg.date.timestamp(), time.time(), max(size, 1)))
            if size:
                self.db.execute(
                    "INSERT INTO chunks(file_id,idx,msg_id,start,size,sha256) VALUES(?,0,?,0,?,'')",
                    (cur.lastrowid, msg.id, size))
            self.db.commit()
            imported.append(path)
        if newest != last:
            self._set_meta("last_scanned", str(newest))
        if imported:
            self.mkdir(self.INBOX)
        return imported

    async def prune_missing(self):
        """Remove files whose messages were deleted in Telegram. Returns removed paths."""
        ids = [r[0] for r in self.db.execute("SELECT DISTINCT msg_id FROM chunks")]
        missing = set()
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            msgs = await self.client.get_messages(self.channel, ids=batch)
            for mid, m in zip(batch, msgs):
                if getattr(m, "media", None) is None:
                    missing.add(mid)
        if not missing:
            return []
        if len(missing) > 5 and len(missing) > len(ids) / 2:
            raise RuntimeError(f"{len(missing)} of {len(ids)} messages look missing; "
                               "not pruning (check the CHANNEL id).")
        marks = ",".join("?" * len(missing))
        rows = self.db.execute(
            f"SELECT DISTINCT f.* FROM files f JOIN chunks c ON c.file_id=f.id "
            f"WHERE c.msg_id IN ({marks})", tuple(missing)).fetchall()
        await self._delete_rows(rows)                   # also removes the file's other chunks
        return [r["path"] for r in rows]

    # ---------- index backup

    async def backup_index(self):
        """Send a copy of index.db to the channel. Without the index, the
        channel is just a pile of chunks, so do this regularly."""
        self.db.commit()
        tmp = os.path.join(DATA_DIR, "index_backup.db")
        bk = sqlite3.connect(tmp)
        self.db.backup(bk)
        bk.close()
        try:
            msg = await self.client.send_file(
                self.channel, tmp, force_document=True,
                caption=f"#index_backup {time.strftime('%Y-%m-%d %H:%M')}")
        finally:
            os.remove(tmp)
        self._set_meta("last_backup", str(time.time()))
        return msg.id
