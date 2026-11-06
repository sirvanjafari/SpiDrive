"""
SpiDrive: web dashboard + Windows drive, in one program.

    python server.py          (or double-click start.bat)

  Dashboard:     http://127.0.0.1:8000
  Windows drive: S: in File Explorer, through rclone (set up once with setup_rclone.bat)
                 or Windows' own network drive (setup_drive.bat)

Both show the same files. While it runs it also:
  - uploads files added from the dashboard or File Explorer to Telegram
  - imports files you send or forward to the channel from Telegram  (every 15 s)
  - removes files whose messages you deleted in Telegram           (every 10 min)
  - backs up the index to the channel                              (every 24 h)
  - keeps SYNC_FOLDERS from config.py in two-way sync               (every 10 s)
  - sends files to Telegram contacts when you press Share
"""
import asyncio
import base64
import mimetypes
import os
import posixpath
import secrets
import shutil
import sys
import time
import uuid

from aiohttp import web

import config
import dav
import sync
from drive_core import BASE_DIR, DATA_DIR, Drive, norm
from mount import Mount

HOST = getattr(config, "WEB_HOST", "127.0.0.1")     # keep 127.0.0.1 = only this PC can open it
PORT = int(getattr(config, "WEB_PORT", 8000))
DAV_PORT = int(getattr(config, "DAV_PORT", 8080))   # Windows drive; always local-only
PASSWORD = getattr(config, "WEB_PASSWORD", "")      # optional; required if you change WEB_HOST
INDEX_HTML = os.path.join(BASE_DIR, "web", "index.html")
SYNC_SECONDS = 15
PRUNE_SECONDS = 600
BACKUP_SECONDS = 24 * 3600

DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv",
           ".odt", ".rtf", ".epub", ".srt"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"}


def kind_of(path):
    ext = posixpath.splitext(path)[1].lower()
    mt = mimetypes.guess_type(path)[0] or ""
    if mt.startswith("image/"):
        return "image"
    if mt.startswith("video/"):
        return "video"
    if mt.startswith("audio/"):
        return "audio"
    if ext in DOC_EXT:
        return "document"
    if ext in ARCHIVE_EXT:
        return "archive"
    return "other"


def file_json(r):
    return {
        "id": r["id"], "name": posixpath.basename(r["path"]), "path": r["path"],
        "folder": posixpath.dirname(r["path"]), "size": r["size"], "mtime": r["mtime"],
        "created": r["created"], "encrypted": bool(r["encrypted"]), "status": r["status"],
        "origin": r["origin"], "kind": kind_of(r["path"]),
    }


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- handlers

routes = web.RouteTableDef()


def drive_of(req) -> Drive:
    return req.app["drive"]


@routes.get("/")
async def index(req):
    return web.FileResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})


@routes.get("/api/list")
async def api_list(req):
    d = drive_of(req)
    path = norm(req.query.get("path", "/"))
    if not d.is_dir(path):
        raise FileNotFoundError(f"No such folder: {path}")
    dirs, files = d.listdir(path)
    return web.json_response({
        "path": path,
        "dirs": [{"name": n, "path": norm(posixpath.join(path, n))} for n in dirs],
        "files": [file_json(r) for r in files],
    })


@routes.get("/api/search")
async def api_search(req):
    rows = drive_of(req).search(req.query.get("q", ""))
    return web.json_response({"files": [file_json(r) for r in rows]})


@routes.get("/api/stats")
async def api_stats(req):
    d = drive_of(req)
    kinds = {}
    total = count = incomplete = 0
    queued = {p["path"] for p in d.pending()}
    for path, size, status in d.db.execute("SELECT path, size, status FROM files"):
        if status != "complete":
            incomplete += path not in queued      # interrupted uploads nobody is finishing
            continue
        k = kinds.setdefault(kind_of(path), {"count": 0, "bytes": 0})
        k["count"] += 1
        k["bytes"] += size
        total += size
        count += 1
    folders = len({posixpath.dirname(p) for (p,) in d.db.execute("SELECT path FROM files")}
                  | {p for (p,) in d.db.execute("SELECT path FROM dirs")})
    s = d.stats()
    last_backup = d._meta("last_backup")
    return web.json_response({
        "files": count, "bytes": total, "folders": folders,
        "incomplete": incomplete,
        "pending": s["pending"], "pending_bytes": s["pending_bytes"],
        "kinds": kinds, "messages": d.db.execute("SELECT COUNT(DISTINCT msg_id) FROM chunks").fetchone()[0],
        "encryption": bool(config.ENCRYPT),
        "channel": getattr(d.channel, "title", ""),
        "last_backup": float(last_backup) if last_backup else None,
        "drive": f"http://localhost:{DAV_PORT}",
        "sync": req.app["syncer"].describe(),
        "connection": d.proxy.status(),
        "version": d.version(),
    })


@routes.get("/api/jobs")
async def api_jobs(req):
    """Upload queue for the dashboard's panel (includes files copied in File Explorer)."""
    d = drive_of(req)
    cur = d.current_pending_id()
    jobs = []
    for p in d.pending():
        prog = d.progress.get(p["id"], {})
        state = "uploading" if p["id"] == cur else ("retry" if prog.get("error") else "queued")
        jobs.append({"id": f"p{p['id']}", "path": p["path"], "state": state,
                     "done": prog.get("done", 0), "total": prog.get("total", p["size"]),
                     "error": prog.get("error"), "t": p["created"]})
    cutoff = time.time() - 600
    for sh in req.app["shares"]:
        if sh["state"] == "sharing" or sh["t"] >= cutoff:
            jobs.append({k: sh[k] for k in ("id", "path", "state", "done", "total", "error", "t")})
    for i, r in enumerate(d.recent):
        if r["t"] >= cutoff:
            jobs.append({"id": f"r{int(r['t'] * 1000)}-{i}", "path": r["path"], "state": r["state"],
                         "done": r["total"], "total": r["total"], "error": r["error"], "t": r["t"]})
    return web.json_response({"jobs": jobs})


@routes.post("/api/upload")
async def api_upload(req):
    """One or more files as multipart; a 'relpath' field before a file sets its
    path relative to ?path= (used for folder uploads)."""
    d = drive_of(req)
    dest = norm(req.query.get("path", "/"))
    reader = await req.multipart()
    queued, rel = [], None
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "relpath":
            rel = (await part.text()).strip()
            continue
        if part.name != "file":
            continue
        name = (rel or part.filename or "file").replace("\\", "/")
        rel = None
        remote = norm(posixpath.join(dest, name))
        if not remote.startswith(dest.rstrip("/") + "/"):
            raise ValueError(f"bad file name: {name}")
        tmp = d.new_staging_path()
        try:
            with open(tmp, "wb") as f:
                while True:
                    chunk = await part.read_chunk(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        except BaseException:
            d._discard(tmp)
            raise
        queued.append(d.stage(tmp, d.free_path(remote)))
    return web.json_response({"queued": queued})


@routes.post("/api/mkdir")
async def api_mkdir(req):
    data = await req.json()
    path = drive_of(req).free_path(data["path"])
    drive_of(req).mkdir(path)
    return web.json_response({"path": path})


@routes.post("/api/move")
async def api_move(req):
    data = await req.json()
    drive_of(req).move(data["src"], data["dst"])
    return web.json_response({"ok": True})


@routes.post("/api/delete")
async def api_delete(req):
    data = await req.json()
    path = norm(data["path"])
    if path == "/":
        raise ValueError("refusing to delete the whole drive")
    n = await drive_of(req).delete(path)
    return web.json_response({"deleted": n})


# ---------------------------------------------------------------- sharing

TELEGRAM_LIMIT = 2000 * 1024 * 1024
TELEGRAM_LIMIT_PREMIUM = 4000 * 1024 * 1024


async def resolve_chat(d, to):
    to = to.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if to.lower().startswith(prefix):
            to = to[len(prefix):]
    if to.lower() in ("me", "saved", "saved messages", "self"):
        return "me", "Saved Messages"
    if to.replace(" ", "").isdigit():
        to = "+" + to.replace(" ", "")                  # phone number without +
    try:
        chat = await d.client.get_entity(to)
    except (ValueError, TypeError):
        raise ValueError(f"Could not find '{to}' on Telegram. Use their @username, or a phone "
                         "number that is saved in your Telegram contacts.")
    name = getattr(chat, "username", None)
    label = "@" + name if name else (getattr(chat, "first_name", None) or getattr(chat, "title", None) or to)
    return chat, label


async def share_file(d, job, path, to):
    """Send one drive file to a Telegram chat, as a normal readable file."""
    e = d.get_entry(path)
    if e is None or e["status"] not in ("complete", "pending"):
        raise FileNotFoundError(path)
    chat, label = await resolve_chat(d, to)
    job["path"] = f"{path}  →  {label}"
    name = posixpath.basename(path)

    # Fast: the file already is one plain Telegram file -> send that same file (no upload).
    if e["status"] == "complete" and not e["encrypted"]:
        chunks = d._chunks(e["id"])
        if len(chunks) == 1:
            msg = await d.client.get_messages(d.channel, ids=chunks[0]["msg_id"])
            tg_name = getattr(getattr(msg, "file", None), "name", None)
            if msg is not None and msg.media is not None and (tg_name == name or e["origin"] == "telegram"):
                await d.client.send_file(chat, msg.media, caption=name)
                return label

    me = await d.client.get_me()
    limit = TELEGRAM_LIMIT_PREMIUM if getattr(me, "premium", False) else TELEGRAM_LIMIT
    if e["size"] > limit:
        raise ValueError(f"Telegram cannot send a file over {limit // 2**20} MB in one message. "
                         "Download it and share it another way.")
    # Slow: rebuild the plain file (decrypt and join parts), then upload it to the chat.
    folder = os.path.join(DATA_DIR, "share-tmp", uuid.uuid4().hex)
    os.makedirs(folder, exist_ok=True)
    local = os.path.join(folder, name.replace(":", "_"))
    total = max(1, e["size"] * 2)

    def progress(done, _total, base=0):
        job["done"], job["total"] = base + done, total

    try:
        if e["status"] == "pending":
            await asyncio.to_thread(shutil.copyfile, e["tmp"], local)
        else:
            await d.download(path, local, progress=progress)
        small_image = kind_of(name) == "image" and e["size"] < 10 * 1024 * 1024
        await d.client.send_file(chat, local, caption=name, force_document=not small_image,
                                 progress_callback=lambda a, b: progress(a, b, e["size"]))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return label


@routes.post("/api/share")
async def api_share(req):
    data = await req.json()
    path, to = norm(data["path"]), str(data.get("to", "")).strip()
    if not to:
        raise ValueError("Who should get the file? Enter an @username, a phone number, or 'me'.")
    d = drive_of(req)
    if d.get_entry(path) is None:
        raise FileNotFoundError(path)
    job = {"id": f"s{uuid.uuid4().hex[:10]}", "path": f"{path}  →  {to}", "state": "sharing",
           "done": 0, "total": 0, "error": None, "t": time.time()}
    req.app["shares"].append(job)
    del req.app["shares"][:-50]

    async def run():
        try:
            label = await share_file(d, job, path, to)
            job["state"] = "done"
            log(f"shared {path} with {label}")
        except Exception as e:
            job["state"], job["error"] = "error", str(e) or type(e).__name__
            log(f"share failed ({path}): {e!r}")
        job["t"] = time.time()
        job["done"] = job["total"]

    req.app["share_tasks"].add(t := asyncio.create_task(run()))
    t.add_done_callback(req.app["share_tasks"].discard)
    return web.json_response({"ok": True, "id": job["id"]})


# ---------------------------------------------------------------- phone ("Add to Home screen")

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" '
            'rx="112" fill="#2a8fd8"/><path fill="#fff" d="M392 132 104 243c-20 8-20 19-3 24l74 23 28 86c3 '
            '9 2 13 12 13 7 0 11-3 15-7l35-34 74 54c14 8 23 4 27-12l48-228c5-20-8-29-22-22z"/></svg>')


@routes.get("/icon.svg")
async def icon(req):
    return web.Response(text=ICON_SVG, content_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=86400"})


@routes.get("/manifest.webmanifest")
async def manifest(req):
    return web.json_response({
        "name": "SpiDrive", "short_name": "SpiDrive", "start_url": "/", "display": "standalone",
        "background_color": "#121417", "theme_color": "#2a8fd8",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
    }, content_type="application/manifest+json")


# ---------------------------------------------------------------- settings page

import re

SIZE_RE = re.compile(r"^\s*\d+(\.\d+)?\s*[KMGTP]?(I?B)?\s*$", re.I)


def app_mode():
    """True in the app (settings.json); False when run from the command line with config.py."""
    return bool(getattr(config, "__spidrive_settings__", False))


def current_letter():
    if app_mode():
        return str(getattr(config, "DRIVE_LETTER", "") or "")
    import mount
    return mount.wanted_letter() or ""


@routes.get("/api/settings")
async def api_settings_get(req):
    d = drive_of(req)
    pairs = getattr(config, "SYNC_FOLDERS", None) or {}
    if isinstance(pairs, (list, tuple)):
        pairs = dict(pairs)
    enc_files = d.db.execute("SELECT COUNT(*) FROM files WHERE encrypted=1").fetchone()[0]
    return web.json_response({
        "editable": app_mode(),
        "drive_letter": current_letter(),
        "drive_size": str(getattr(config, "DRIVE_SIZE", "10T")),
        "encrypt": bool(config.ENCRYPT),
        "has_passphrase": bool(config.PASSPHRASE),
        "encrypted_files": enc_files,
        "passphrase_locked": d._meta("key_check") is not None,
        "chunk_mb": int(config.CHUNK_SIZE) // (1024 * 1024),
        "sync": [{"local": l, "remote": r} for l, r in pairs.items()],
        "autostart": bool(getattr(config, "AUTOSTART", False)),
        "phone_access": HOST not in ("127.0.0.1", "localhost"),
        "has_web_password": bool(PASSWORD),
        "data_dir": DATA_DIR,
        "channel": getattr(d.channel, "title", ""),
        "web_port": PORT,
        "proxy_mode": d.proxy.mode,
        "proxy_link": str(getattr(config, "PROXY", "") or ""),
        "proxy_sources": d.proxy.sources,
        "proxy_status": d.proxy.status(),
    })


@routes.post("/api/settings")
async def api_settings_post(req):
    if not app_mode():
        raise ValueError("SpiDrive was started from the command line: change config.py instead, then restart.")
    import settings as store
    data = await req.json()
    d = drive_of(req)
    v = store.load()

    letter = str(data.get("drive_letter", v["DRIVE_LETTER"]) or "").strip().upper()[:1]
    if not letter.isalpha() or letter in "ABC":
        raise ValueError("Pick a drive letter from D to Z.")
    v["DRIVE_LETTER"] = letter + ":"

    size = str(data.get("drive_size", v["DRIVE_SIZE"])).strip()
    if not SIZE_RE.match(size):
        raise ValueError("Drive size should look like 10T, 500G or 2TB.")
    v["DRIVE_SIZE"] = size

    chunk = int(data.get("chunk_mb", int(v["CHUNK_SIZE"]) // 2**20))
    if not 16 <= chunk <= 1900:
        raise ValueError("Part size must be between 16 and 1900 MB.")
    v["CHUNK_SIZE"] = chunk * 1024 * 1024

    want_enc = bool(data.get("encrypt"))
    new_pass = data.get("passphrase") or ""
    if new_pass:
        if len(new_pass) < 8:
            raise ValueError("The encryption passphrase must have at least 8 characters.")
        if v["PASSPHRASE"] and new_pass != v["PASSPHRASE"] and d._meta("key_check") is not None:
            raise ValueError("The passphrase cannot be changed: files were already encrypted with the "
                             "current one and could not be opened any more.")
        v["PASSPHRASE"] = new_pass
    if want_enc and not v["PASSPHRASE"]:
        raise ValueError("Choose an encryption passphrase to turn encryption on.")
    v["ENCRYPT"] = want_enc

    pairs = {}
    for p in data.get("sync", []) or []:
        local, remote = str(p.get("local", "")).strip().strip('"'), str(p.get("remote", "")).strip()
        if not local and not remote:
            continue
        if not os.path.isabs(local):
            raise ValueError(f"Sync folder '{local}' must be a full path, like D:\\SpiDrive Sync.")
        pairs[local] = norm(remote or "/Sync")
    v["SYNC_FOLDERS"] = pairs

    v["AUTOSTART"] = bool(data.get("autostart"))

    pmode = str(data.get("proxy_mode", v.get("PROXY_MODE", "auto"))).lower()
    if pmode not in ("auto", "manual", "off"):
        raise ValueError("Unknown connection mode.")
    plink = str(data.get("proxy_link", v.get("PROXY", "")) or "").strip()
    if pmode == "manual":
        from proxy import parse_link
        if parse_link(plink) is None:
            raise ValueError("Paste a valid proxy link: tg://proxy, t.me/proxy, socks5:// or http://.")
    v["PROXY_MODE"], v["PROXY"] = pmode, plink
    if "proxy_sources" in data:
        srcs = [x.strip() for x in data.get("proxy_sources") or [] if str(x).strip()]
        from proxy import DEFAULT_SOURCES
        v["PROXY_SOURCES"] = None if srcs == DEFAULT_SOURCES else srcs

    if data.get("phone_access"):
        pw = data.get("web_password") or ""
        if pw and len(pw) < 6:
            raise ValueError("The phone access password must have at least 6 characters.")
        if pw:
            v["WEB_PASSWORD"] = pw
        if not v["WEB_PASSWORD"]:
            raise ValueError("Set a password for phone access.")
        v["WEB_HOST"] = "0.0.0.0"
    else:
        v["WEB_HOST"] = "127.0.0.1"

    store.save(v)
    restart = req.app["hooks"].get("restart")
    if restart:
        asyncio.get_running_loop().call_later(0.8, restart)
    return web.json_response({"ok": True, "restarting": bool(restart)})


# ---------------------------------------------------------------- connection / proxy

@routes.get("/api/proxy")
async def api_proxy(req):
    d = drive_of(req)
    st = d.proxy.status()
    st["sources"] = d.proxy.sources
    st["manual_link"] = str(getattr(config, "PROXY", "") or "")
    return web.json_response(st)


@routes.post("/api/proxy/find")
async def api_proxy_find(req):
    """Look for another working route now (the dashboard's 'Find a proxy' button)."""
    d = drive_of(req)
    if d.proxy.mode == "off":
        raise ValueError("Proxy is turned off in Settings.")
    if not d.proxy.searching:
        async def run():
            try:
                await d.switch_connection(force_search=True)
            except Exception as e:
                d.proxy.last_error = str(e) or type(e).__name__
                log(f"connection: switching failed: {e!r}")
        req.app["share_tasks"].add(t := asyncio.create_task(run()))
        t.add_done_callback(req.app["share_tasks"].discard)
    return web.json_response({"ok": True, "searching": True})


@routes.post("/api/proxy/test")
async def api_proxy_test(req):
    data = await req.json()
    return web.json_response(await drive_of(req).proxy.test_link(str(data.get("link", ""))))


@routes.post("/api/restart")
async def api_restart(req):
    restart = req.app["hooks"].get("restart")
    if not restart:
        raise ValueError("Restart SpiDrive yourself (close its window and start it again).")
    asyncio.get_running_loop().call_later(0.5, restart)
    return web.json_response({"ok": True})


@routes.post("/api/sync")
async def api_sync(req):
    new = await drive_of(req).import_new_messages(min_age=0 if req.query.get("now") else 20)
    return web.json_response({"imported": new})


@routes.get("/f/{id:-?\\d+}/{name:.*}")
async def stream(req):
    """Stream a file with HTTP Range support, so videos can seek and play
    before the whole file is downloaded."""
    d = drive_of(req)
    e = d.get_entry_by_id(int(req.match_info["id"]))
    if e is None or e["status"] not in ("complete", "pending"):
        raise web.HTTPNotFound()
    return await dav.send_entry(req, d, e, "attachment" if req.query.get("dl") else "inline")


# ---------------------------------------------------------------- middleware

@web.middleware
async def guard(req, handler):
    if PASSWORD:
        ok = False
        auth = req.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")[2]
                ok = secrets.compare_digest(pw, PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="SpiDrive"'})
    try:
        return await handler(req)
    except web.HTTPException:
        raise
    except FileNotFoundError as e:
        return web.json_response({"error": f"Not found: {e}"}, status=404)
    except FileExistsError as e:
        return web.json_response({"error": f"Already exists: {e}"}, status=409)
    except (IsADirectoryError, ValueError, RuntimeError, KeyError) as e:
        return web.json_response({"error": str(e)}, status=400)


# ---------------------------------------------------------------- background

async def every(seconds, fn, label):
    while True:
        try:
            result = await fn()
            if result:
                for item in result if isinstance(result, list) else [result]:
                    log(f"{label}: {item}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"{label} failed: {e!r}")
        await asyncio.sleep(seconds)


async def watch_connection(d):
    """Every minute, check Telegram still answers. In 'auto' mode, two failed checks in a
    row switch to another route (another proxy, or direct when that works again)."""
    from telethon.tl.functions.help import GetNearestDcRequest
    fails = 0
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.wait_for(d.client(GetNearestDcRequest()), 25)
            fails = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            fails += 1
            log(f"connection: Telegram did not answer ({type(e).__name__}), check {fails}")
            if fails >= 2 and d.proxy.mode == "auto" and not d.proxy.searching:
                try:
                    await d.switch_connection(force_search=True)
                    fails = 0
                except Exception as e2:
                    d.proxy.last_error = str(e2) or type(e2).__name__
                    log(f"connection: switching failed: {e2!r}")


async def maybe_backup(d):
    last = d._meta("last_backup")
    if last is None or time.time() - float(last) > BACKUP_SECONDS:
        await d.backup_index()
        return "index backed up to the channel"


async def start_site(runner, host, port, label):
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
        return True
    except OSError as e:
        log(f"could not open {label} on {host}:{port} ({e.strerror or e}). "
            f"Is SpiDrive already running, or is the port used by another program?")
        return False


async def amain(stop_event=None, interactive=True, hooks=None):
    """Run SpiDrive until stop_event is set (app) or Ctrl+C (command line)."""
    if not os.path.exists(INDEX_HTML):
        raise SystemExit(f"Missing {INDEX_HTML}. Put index.html in a 'web' folder next to server.py.")
    d = Drive()
    await d.connect(interactive=interactive)
    d.clean_incoming()

    web_app = web.Application(middlewares=[guard], client_max_size=1024 ** 4)
    web_app.add_routes(routes)
    web_app["drive"] = d
    web_app["hooks"] = hooks or {}
    web_app["shares"] = []
    web_app["share_tasks"] = set()
    syncer = sync.Syncer(d, log)
    web_app["syncer"] = syncer
    web_runner = web.AppRunner(web_app, access_log=None)
    await web_runner.setup()

    dav_runner = web.AppRunner(dav.make_app(d), access_log=None)
    await dav_runner.setup()

    tasks = []
    mount = Mount(DAV_PORT, log)
    try:
        if not await start_site(web_runner, HOST, PORT, "the dashboard"):
            return 1
        dav_ok = await start_site(dav_runner, "127.0.0.1", DAV_PORT, "the Windows drive")
        if dav_ok:
            try:                                      # Windows may reach "localhost" over IPv6
                await web.TCPSite(dav_runner, "::1", DAV_PORT).start()
            except OSError:
                pass

        tasks = [
            asyncio.create_task(d.upload_worker(log)),
            asyncio.create_task(every(SYNC_SECONDS, d.import_new_messages, "from Telegram")),
            asyncio.create_task(every(PRUNE_SECONDS, d.prune_missing, "deleted in Telegram")),
            asyncio.create_task(every(3600, lambda: maybe_backup(d), "backup")),
            asyncio.create_task(watch_connection(d)),
        ]
        if syncer.pairs:
            tasks.append(asyncio.create_task(every(sync.INTERVAL, syncer.run_once, "sync")))
        me = await d.client.get_me()
        log(f"logged in as {me.first_name}, channel '{getattr(d.channel, 'title', '?')}'")
        waiting = d.stats()["pending"]
        if waiting:
            log(f"{waiting} file(s) waiting to upload, continuing")
        log(f"dashboard:     http://127.0.0.1:{PORT}")
        letter = await mount.start() if dav_ok else None
        if letter:
            log(f"Windows drive: {letter} is ready (open This PC in File Explorer)")
        elif dav_ok:
            log(f"Windows drive: http://localhost:{DAV_PORT}   (run setup_rclone.bat once to give it a letter)")
        for l, r in syncer.pairs:
            log(f"sync:          {l}  <->  {r}")
        if stop_event is None:
            log("keep this window open. Ctrl+C stops SpiDrive.")
        else:
            log("running. Use the SpiDrive icon next to the clock to open or quit it.")
        await (stop_event or asyncio.Event()).wait()   # run until Quit (app) or Ctrl+C
    finally:
        await mount.stop()                            # unmount before the drive server goes away
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dav_runner.cleanup()
        await web_runner.cleanup()
        await d.close()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")      # Persian names in the Windows console
    except Exception:
        pass
    if HOST not in ("127.0.0.1", "localhost") and not PASSWORD:
        raise SystemExit("WEB_HOST is not 127.0.0.1, so set WEB_PASSWORD in config.py first.")
    try:
        sys.exit(asyncio.run(amain()) or 0)
    except KeyboardInterrupt:
        print("\nSpiDrive stopped. Unfinished uploads continue next time you start it.")


if __name__ == "__main__":
    main()
