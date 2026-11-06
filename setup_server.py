"""
First-run setup wizard (runs in the browser instead of the console).

  1. Telegram app ID   (skipped when build.bat built one into the .exe)
  2. Already use SpiDrive?  -> import the old folder (config.py + login + data)
  3. Phone number -> login code -> 2-step password (if you have one)
  4. Storage channel: create "MyDrive" or pick one you already own
  5. Options: encryption, drive letter, a sync folder, start with Windows

app.py runs this until setup is finished, then starts SpiDrive normally.
"""
import asyncio
import os

from aiohttp import web

import settings
from proxy import ProxyManager, client_kwargs

try:
    from telethon import TelegramClient, utils
    from telethon import errors as tg_errors
    from telethon.tl.functions.channels import CreateChannelRequest
except ImportError:                        # lets the tests run without Telethon
    TelegramClient = utils = tg_errors = CreateChannelRequest = None

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP_HTML = os.path.join(HERE, "web", "setup.html")


def _err_name(e):
    return type(e).__name__


def friendly(e):
    """Telegram errors in plain words."""
    name = _err_name(e)
    seconds = getattr(e, "seconds", None)
    return {
        "PhoneNumberInvalidError": "That phone number is not valid. Use the full number with country code, like +98912…",
        "PhoneNumberBannedError": "Telegram has banned this phone number.",
        "PhoneCodeInvalidError": "That code is wrong. Check the code Telegram sent and try again.",
        "PhoneCodeExpiredError": "That code has expired. Go back and ask for a new one.",
        "PasswordHashInvalidError": "Wrong 2-step verification password.",
        "ApiIdInvalidError": "The API ID / API hash are not valid. Copy them again from my.telegram.org.",
        "FloodWaitError": f"Telegram asks you to wait {seconds or 'a few'} seconds before trying again.",
        "PhoneNumberUnoccupiedError": "There is no Telegram account with this number. Create one in the Telegram app first.",
        "ConnectionError": "Can't reach Telegram. Open 'Connection' below to find a proxy or paste one.",
        "TimeoutError": "Telegram did not answer in time. Open 'Connection' below to find a proxy or paste one.",
        "OSError": "Can't reach Telegram. Open 'Connection' below to find a proxy or paste one.",
    }.get(name, str(e) or name)


class Wizard:
    def __init__(self, values, on_done):
        self.v = values
        self.on_done = on_done
        self.client = None
        self.phone_hash = None
        self.lock = asyncio.Lock()
        self.pm = ProxyManager(values)            # reads PROXY_MODE etc. from the live values

    async def _client(self):
        if self.client is None:
            os.makedirs(os.path.dirname(self.v["SESSION_PATH"]), exist_ok=True)
            via = await self.pm.choose()
            self.client = TelegramClient(self.v["SESSION_PATH"], int(self.v["API_ID"]), self.v["API_HASH"],
                                         **client_kwargs(via))
        if not self.client.is_connected():
            await asyncio.wait_for(self.client.connect(), 40)
        return self.client

    # ---------- connection (proxy)

    def proxy_status(self):
        st = self.pm.status()
        st["sources"] = self.pm.sources
        st["manual_link"] = self.v.get("PROXY", "")
        return st

    async def set_proxy(self, mode, link):
        mode = str(mode or "auto").lower()
        if mode not in ("auto", "manual", "off"):
            raise ValueError("Unknown connection mode.")
        if mode == "manual":
            from proxy import parse_link
            if parse_link(link or "") is None:
                raise ValueError("Paste a tg://proxy, t.me/proxy, socks5:// or http:// link.")
        self.v["PROXY_MODE"], self.v["PROXY"] = mode, (link or "").strip()
        await self.close()                       # next step reconnects the new way
        await self._client()
        return self.proxy_status()

    async def find_proxy(self):
        await self.close()
        self.v["PROXY_MODE"] = "auto"
        via = await self.pm.choose(force_search=True,
                                   exclude=[self.pm.current] if self.pm.current else ["direct"])
        os.makedirs(os.path.dirname(self.v["SESSION_PATH"]), exist_ok=True)
        self.client = TelegramClient(self.v["SESSION_PATH"], int(self.v["API_ID"]), self.v["API_HASH"],
                                     **client_kwargs(via))
        await asyncio.wait_for(self.client.connect(), 40)
        return self.proxy_status()

    async def close(self):
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    # ---------- steps

    async def state(self):
        out = {"need_api": not (self.v["API_ID"] and self.v["API_HASH"]),
               "logged_in": False, "me": None,
               "legacy": settings.legacy_candidates(),
               "letter": self.v["DRIVE_LETTER"], "data_dir": self.v["DATA_DIR"],
               "phone": self.v.get("PHONE", "")}
        if not out["need_api"]:
            try:
                c = await self._client()
                if await c.is_user_authorized():
                    me = await c.get_me()
                    out["logged_in"] = True
                    out["me"] = me.first_name or me.username or "you"
            except Exception as e:
                out["error"] = friendly(e)
        return out

    async def set_api(self, api_id, api_hash):
        api_id = str(api_id).strip()
        api_hash = str(api_hash).strip()
        if not api_id.isdigit() or len(api_hash) < 20:
            raise ValueError("Enter the numeric API ID and the long API hash from my.telegram.org.")
        await self.close()
        self.v["API_ID"], self.v["API_HASH"] = int(api_id), api_hash

    async def import_folder(self, folder):
        done = settings.import_legacy(folder.strip().strip('"'), self.v)
        await self.close()
        return done

    async def send_code(self, phone):
        phone = phone.strip().replace(" ", "")
        if not phone.startswith("+"):
            phone = "+" + phone
        c = await self._client()
        sent = await c.send_code_request(phone)
        self.phone_hash = sent.phone_code_hash
        self.v["PHONE"] = phone

    async def sign_in(self, code):
        c = await self._client()
        try:
            await c.sign_in(phone=self.v["PHONE"], code=code.strip().replace(" ", ""),
                            phone_code_hash=self.phone_hash)
        except Exception as e:
            if _err_name(e) == "SessionPasswordNeededError":
                return {"need_password": True}
            raise
        return {"ok": True}

    async def password(self, pw):
        c = await self._client()
        await c.sign_in(password=pw)
        return {"ok": True}

    async def channels(self):
        """Broadcast channels you created (possible storage channels)."""
        c = await self._client()
        out = []
        async for dlg in c.iter_dialogs():
            ent = dlg.entity
            if getattr(ent, "broadcast", False) and getattr(ent, "creator", False):
                out.append({"id": utils.get_peer_id(ent), "title": dlg.name})
        current = str(self.v.get("CHANNEL") or "")
        return {"channels": out, "current": current}

    async def finish(self, data):
        c = await self._client()
        if not await c.is_user_authorized():
            raise ValueError("Log in first.")
        choice = str(data.get("channel", "new"))
        if choice == "new":
            title = (data.get("title") or "MyDrive").strip()[:120] or "MyDrive"
            res = await c(CreateChannelRequest(title=title, about="SpiDrive storage", megagroup=False))
            ent = res.chats[0]
            self.v["CHANNEL"], self.v["CHANNEL_TITLE"] = utils.get_peer_id(ent), title
        else:
            self.v["CHANNEL"] = int(choice)
            self.v["CHANNEL_TITLE"] = data.get("title", "")

        if data.get("encrypt"):
            pw = data.get("passphrase") or ""
            if not self.v.get("PASSPHRASE"):
                if len(pw) < 8:
                    raise ValueError("Choose an encryption passphrase of at least 8 characters.")
                self.v["PASSPHRASE"] = pw
            self.v["ENCRYPT"] = True
        else:
            self.v["ENCRYPT"] = False
        letter = str(data.get("letter") or "S").strip().upper()[:1]
        if letter and letter.isalpha() and letter not in "ABC":
            self.v["DRIVE_LETTER"] = letter + ":"
        sync_local = (data.get("sync_local") or "").strip().strip('"')
        if sync_local:
            self.v["SYNC_FOLDERS"] = {sync_local: data.get("sync_remote") or "/Sync"}
        self.v["AUTOSTART"] = bool(data.get("autostart", True))
        settings.save(self.v)
        await self.close()
        self.on_done()
        return {"ok": True, "channel": self.v["CHANNEL"]}


async def _ready(value):
    return value


def make_app(wizard):
    routes = web.RouteTableDef()

    def handler(fn):
        async def wrapped(req):
            try:
                data = await req.json() if req.method == "POST" and req.can_read_body else {}
                async with wizard.lock:
                    result = await fn(data)
                return web.json_response(result if isinstance(result, dict) else {"ok": True, "result": result})
            except Exception as e:
                return web.json_response({"error": friendly(e)}, status=400)
        return wrapped

    @routes.get("/")
    async def page(req):
        return web.FileResponse(SETUP_HTML, headers={"Cache-Control": "no-cache"})

    routes.get("/api/setup/state")(handler(lambda d: wizard.state()))
    routes.post("/api/setup/api")(handler(lambda d: wizard.set_api(d.get("api_id"), d.get("api_hash"))))
    routes.post("/api/setup/import")(handler(lambda d: wizard.import_folder(d.get("folder", ""))))
    routes.post("/api/setup/phone")(handler(lambda d: wizard.send_code(d.get("phone", ""))))
    routes.post("/api/setup/code")(handler(lambda d: wizard.sign_in(d.get("code", ""))))
    routes.post("/api/setup/password")(handler(lambda d: wizard.password(d.get("password", ""))))
    routes.get("/api/setup/channels")(handler(lambda d: wizard.channels()))
    routes.get("/api/setup/proxy")(handler(lambda d: _ready(wizard.proxy_status())))
    routes.post("/api/setup/proxy")(handler(lambda d: wizard.set_proxy(d.get("mode"), d.get("link", ""))))
    routes.post("/api/setup/proxy/find")(handler(lambda d: wizard.find_proxy()))
    routes.post("/api/setup/finish")(handler(lambda d: wizard.finish(d)))

    @routes.get("/api/stats")
    async def not_yet(req):                     # the dashboard asks this while waiting
        return web.json_response({"setup": True}, status=503)

    app = web.Application()
    app.add_routes(routes)
    return app


async def run(values, stop_event, port, log=print, opened=None):
    """Serve the wizard until it is finished (returns True) or SpiDrive is quit (False)."""
    done = asyncio.Event()
    wizard = Wizard(values, done.set)
    runner = web.AppRunner(make_app(wizard), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    log(f"setup: open http://127.0.0.1:{port} to set up SpiDrive")
    if opened:
        opened()
    try:
        waiters = [asyncio.create_task(done.wait()), asyncio.create_task(stop_event.wait())]
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for t in waiters:
            t.cancel()
        await asyncio.sleep(0.5)                 # let the last response reach the browser
        return done.is_set()
    finally:
        await wizard.close()
        await runner.cleanup()
