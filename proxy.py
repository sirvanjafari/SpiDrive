"""
Reaching Telegram through a proxy, chosen by hand or found automatically.

Modes (PROXY_MODE in Settings or config.py):
  "auto"    default. Connect directly when Telegram is reachable. When it is not,
            find a working proxy:
              1. the proxy that worked last time
              2. a proxy program on this PC (v2rayN, Nekoray, Clash, Tor...)
              3. proxies that worked before
              4. public lists on GitHub (PROXY_SOURCES), each proxy tested for real
            and switch to another one by itself if the proxy stops working.
  "manual"  always use PROXY: a link such as
              tg://proxy?server=1.2.3.4&port=443&secret=dd...   (MTProxy)
              https://t.me/proxy?server=...                       (MTProxy)
              socks5://user:pass@127.0.0.1:10808                  (SOCKS5)
              http://127.0.0.1:10809                              (HTTP)
  "off"     always connect directly.

Privacy: Telegram traffic is encrypted end to end between SpiDrive and Telegram,
so a proxy cannot read it; it only sees when and how much data flows. MTProto
proxies (made for Telegram) are preferred over random SOCKS proxies.
"""
import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import random
import re
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

DEFAULT_SOURCES = [
    "https://raw.githubusercontent.com/SoliSpirit/mtproto/master/all_proxies.txt",
    "https://raw.githubusercontent.com/Argh94/Proxy-List/main/MTProto.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/tg/mtproto.json",
]
# Proxy programs people run on their own PC: (type, port, label)
LOCAL_PORTS = [
    ("socks5", 10808, "v2rayN"), ("socks5", 2080, "Nekoray / sing-box"), ("socks5", 7890, "Clash"),
    ("socks5", 7891, "Clash"), ("socks5", 1080, "local SOCKS"), ("socks5", 9150, "Tor Browser"),
    ("socks5", 9050, "Tor"), ("http", 10809, "v2rayN (HTTP)"),
]
TCP_TIMEOUT = 4          # seconds for the quick reachability check
PROBE_TIMEOUT = 15       # seconds for a real Telegram handshake through a proxy
MAX_CANDIDATES = 600
PROBE_TOP = 24           # how many of the fastest-answering proxies get a real test
PROBE_PARALLEL = 6
KEEP_GOOD = 12

logging.getLogger("telethon.network").setLevel(logging.ERROR)   # failed probes are expected


# ---------------------------------------------------------------- parsing

class Proxy:
    __slots__ = ("kind", "host", "port", "secret", "user", "password", "label")

    def __init__(self, kind, host, port, secret=None, user=None, password=None, label=None):
        self.kind, self.host, self.port = kind, host, int(port)
        self.secret, self.user, self.password, self.label = secret, user, password, label

    @property
    def link(self):
        if self.kind == "mtproto":
            return f"tg://proxy?server={self.host}&port={self.port}&secret={self.secret}"
        auth = f"{quote(self.user, safe='')}:{quote(self.password or '', safe='')}@" if self.user else ""
        return f"{self.kind}://{auth}{self.host}:{self.port}"

    def describe(self):
        if self.label:
            return f"{self.label} ({self.host}:{self.port})"
        name = {"mtproto": "MTProxy", "socks5": "SOCKS5", "http": "HTTP proxy"}[self.kind]
        return f"{name} {self.host}:{self.port}"

    def __eq__(self, other):
        return isinstance(other, Proxy) and self.link == other.link

    def __hash__(self):
        return hash(self.link)

    def __repr__(self):
        return f"Proxy({self.link})"


def normalize_secret(secret):
    """MTProxy secret -> 32 hex characters, or None when Telethon cannot use it.
    'dd' secrets are fine (padded mode). 'ee' secrets (fake-TLS) are not supported."""
    s = unquote(str(secret or "")).strip()
    if not s:
        return None
    raw = None
    if re.fullmatch(r"[0-9a-fA-F]+", s) and len(s) % 2 == 0:
        raw = bytes.fromhex(s)
    else:
        try:
            raw = base64.urlsafe_b64decode(s.replace("+", "-").replace("/", "_") + "=" * (-len(s) % 4))
        except (binascii.Error, ValueError):
            return None
    if len(raw) >= 1 and raw[0] == 0xEE:
        return None                                   # fake-TLS: Telethon cannot speak it
    if len(raw) == 17 and raw[0] == 0xDD:
        raw = raw[1:]
    if len(raw) != 16:
        return None
    return raw.hex()


def _valid_host(h):
    return bool(h) and len(h) < 254 and re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", h) is not None


def _valid_port(p):
    try:
        return 0 < int(p) < 65536
    except (TypeError, ValueError):
        return False


def parse_link(text):
    """One proxy link (tg://, t.me, socks5://, http://) -> Proxy, or None."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    if low.startswith(("tg://proxy", "tg://socks", "https://t.me/proxy", "https://t.me/socks",
                       "http://t.me/proxy", "t.me/proxy", "t.me/socks", "https://telegram.me/proxy")):
        q = {k.lower(): v[0] for k, v in parse_qs(t.split("?", 1)[1] if "?" in t else "").items()}
        host, port = q.get("server", ""), q.get("port", "")
        if not (_valid_host(host) and _valid_port(port)):
            return None
        if "socks" in low.split("?")[0]:
            return Proxy("socks5", host, port, user=q.get("user"), password=q.get("pass"))
        sec = normalize_secret(q.get("secret"))
        return Proxy("mtproto", host, port, sec) if sec else None
    m = re.match(r"^(socks5h?|socks|http)://", low)
    if m:
        u = urlsplit(t)
        kind = "http" if m.group(1) == "http" else "socks5"
        if not (_valid_host(u.hostname or "") and _valid_port(u.port)):
            return None
        return Proxy(kind, u.hostname, u.port, user=unquote(u.username) if u.username else None,
                     password=unquote(u.password) if u.password else None)
    return None


def _from_obj(o, hint):
    if not isinstance(o, dict):
        return None
    host = o.get("host") or o.get("server") or o.get("ip") or o.get("addr")
    port = o.get("port")
    if not (isinstance(host, str) and _valid_host(host) and _valid_port(port)):
        return None
    if o.get("secret"):
        sec = normalize_secret(o["secret"])
        return Proxy("mtproto", host, port, sec) if sec else None
    if hint in ("socks5", "http"):
        return Proxy(hint, host, port, user=o.get("user") or o.get("username"),
                     password=o.get("pass") or o.get("password"))
    return None


def parse_list(text, hint="auto"):
    """Every usable proxy in a downloaded list (links, JSON, or ip:port lines when
    the source is marked socks5+ / http+)."""
    out, seen = [], set()

    def add(p):
        if p and p.link not in seen:
            seen.add(p.link)
            out.append(p)

    stripped = text.strip()
    if stripped[:1] in "[{":
        try:
            data = json.loads(stripped)
            stack = [data]
            while stack:
                x = stack.pop()
                if isinstance(x, list):
                    stack.extend(reversed(x))
                elif isinstance(x, dict):
                    p = _from_obj(x, hint)
                    if p:
                        add(p)
                    else:
                        stack.extend(v for v in x.values() if isinstance(v, (list, dict)))
            return out
        except ValueError:
            pass
    for m in re.finditer(r"(?:tg://(?:proxy|socks)|https?://t\.me/(?:proxy|socks))\?[^\s\"'<>]+", text, re.I):
        add(parse_link(m.group(0).replace("&amp;", "&")))
    for m in re.finditer(r"(?:socks5h?|socks|http)://[^\s\"'<>]+", text, re.I):
        if "t.me/" not in m.group(0):
            add(parse_link(m.group(0)))
    if hint in ("socks5", "http"):
        for m in re.finditer(r"(?m)^\s*([A-Za-z0-9.\-]+):(\d{2,5})\s*$", text):
            if _valid_port(m.group(2)):
                add(Proxy(hint, m.group(1), m.group(2)))
    return out


def split_source(src):
    """'socks5+https://...' -> ('socks5', 'https://...')."""
    m = re.match(r"^(socks5|http|mtproto)\+(https?://.+)$", src.strip(), re.I)
    return (m.group(1).lower(), m.group(2)) if m else ("auto", src.strip())


def mirrors(url):
    """GitHub raw files are often blocked; jsDelivr serves the same file."""
    urls = [url]
    m = re.match(r"^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", url)
    if m:
        user, repo, branch, path = m.groups()
        urls.append(f"https://cdn.jsdelivr.net/gh/{user}/{repo}@{branch}/{path}")
        urls.append(f"https://fastly.jsdelivr.net/gh/{user}/{repo}@{branch}/{path}")
    return urls


# ---------------------------------------------------------------- telethon glue

def client_kwargs(p):
    """Extra TelegramClient(...) arguments for a proxy (empty = direct)."""
    if p is None:
        return {}
    if p.kind == "mtproto":
        from telethon import connection
        return {"connection": connection.ConnectionTcpMTProxyRandomizedIntermediate,
                "proxy": (p.host, p.port, p.secret)}
    if p.kind == "socks5":
        return {"proxy": ("socks5", p.host, p.port, True, p.user, p.password)}
    return {"proxy": ("http", p.host, p.port, True, p.user, p.password)}


async def probe_telegram(p, api_id, api_hash, timeout=PROBE_TIMEOUT):
    """Seconds a real Telegram handshake took through p (None = direct), or None if it failed.
    Uses a throwaway session, so the user's login is never touched."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    c = TelegramClient(StringSession(), int(api_id), api_hash, connection_retries=1, retry_delay=0,
                       timeout=8, auto_reconnect=False, **client_kwargs(p))
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(c.connect(), timeout)
        return time.monotonic() - t0 if c.is_connected() else None
    except Exception:
        return None
    finally:
        try:
            await asyncio.wait_for(c.disconnect(), 5)
        except Exception:
            pass


async def tcp_rtt(host, port, timeout=TCP_TIMEOUT):
    t0 = time.monotonic()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return time.monotonic() - t0
    except Exception:
        return None


async def fetch_text(url, timeout=20):
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        async with s.get(url, headers={"User-Agent": "SpiDrive"}) as r:
            if r.status != 200:
                raise OSError(f"HTTP {r.status}")
            return await r.text(errors="replace")


# ---------------------------------------------------------------- the manager

class ProxyManager:
    """Picks how SpiDrive reaches Telegram. cfg = the config module or a settings dict."""

    def __init__(self, cfg, log=print):
        self.cfg = cfg
        self.log = log
        self.current = None              # Proxy, or None = direct
        self.latency = None
        self.searching = False
        self.last_error = None
        self.lock = asyncio.Lock()
        # swappable for tests
        self.probe = lambda p: probe_telegram(p, self._get("API_ID", 0), self._get("API_HASH", ""))
        self.tcp = tcp_rtt
        self.fetch = fetch_text

    # ---------- settings

    def _get(self, key, default=None):
        if isinstance(self.cfg, dict):
            return self.cfg.get(key, default)
        return getattr(self.cfg, key, default)

    @property
    def mode(self):
        m = str(self._get("PROXY_MODE", "auto") or "auto").lower()
        return m if m in ("auto", "manual", "off") else "auto"

    @property
    def sources(self):
        src = self._get("PROXY_SOURCES", None)
        if src is None:
            return list(DEFAULT_SOURCES)
        if isinstance(src, str):
            src = src.split()
        return [s.strip() for s in src if s and s.strip()]

    def _state_path(self):
        return os.path.join(str(self._get("DATA_DIR", ".")), "proxy_state.json")

    def _cache_path(self, url):
        return os.path.join(str(self._get("DATA_DIR", ".")), "proxy_lists",
                            hashlib.sha1(url.encode()).hexdigest()[:16] + ".txt")

    def load_state(self):
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_state(self, chosen):
        st = self.load_state()
        st["current"] = chosen.link if chosen else "direct"
        if chosen and chosen.label is None:
            good = [chosen.link] + [g for g in st.get("good", []) if g != chosen.link]
            st["good"] = good[:KEEP_GOOD]
        st["updated"] = time.time()
        try:
            os.makedirs(os.path.dirname(self._state_path()), exist_ok=True)
            with open(self._state_path(), "w", encoding="utf-8") as f:
                json.dump(st, f, indent=1)
        except OSError:
            pass

    def status(self):
        return {"mode": self.mode, "via": self.current.describe() if self.current else "direct",
                "link": self.current.link if self.current else "",
                "kind": self.current.kind if self.current else "direct",
                "latency_ms": round(self.latency * 1000) if self.latency else None,
                "searching": self.searching, "error": self.last_error}

    def _use(self, p, latency):
        self.current, self.latency, self.last_error = p, latency, None
        self.save_state(p)
        self.log(f"connection: {'direct' if p is None else 'through ' + p.describe()}"
                 + (f" ({round(latency * 1000)} ms)" if latency else ""))
        return p

    # ---------- choosing

    async def choose(self, force_search=False, exclude=()):
        """Decide how to connect. Returns a Proxy, or None for a direct connection."""
        async with self.lock:
            mode = self.mode
            if mode == "off":
                self.current, self.latency = None, None
                return None
            if mode == "manual":
                p = parse_link(str(self._get("PROXY", "") or ""))
                if p is None:
                    raise ValueError("The proxy link in Settings is not valid. Use a tg://proxy, "
                                     "t.me/proxy, socks5:// or http:// link.")
                self.current, self.latency = p, None
                return p
            if not (self._get("API_ID") and self._get("API_HASH")):
                return None                          # cannot test before the app ID is known
            excluded = {x.link if isinstance(x, Proxy) else str(x) for x in exclude}
            self.searching = True
            try:
                if not force_search:
                    saved = self.load_state().get("current")
                    if saved and saved != "direct" and saved not in excluded:
                        p = parse_link(saved)
                        if p:
                            lat = await self.probe(p)
                            if lat is not None:
                                return self._use(p, lat)
                if "direct" not in excluded:
                    lat = await self.probe(None)
                    if lat is not None:
                        return self._use(None, lat)
                self.log("connection: Telegram is not reachable directly, looking for a proxy…")
                found = await self.find(excluded)
                if found:
                    return self._use(*found)
                # nothing new works: keep the route we were told to avoid, if it still works
                for link in excluded:
                    p = None if link == "direct" else parse_link(link)
                    if link == "direct" or p is not None:
                        lat = await self.probe(p)
                        if lat is not None:
                            self.log("connection: no other route found, keeping the current one")
                            return self._use(p, lat)
                self.last_error = "No working proxy found. Turn on a VPN, or add a proxy link in Settings."
                self.log("connection: " + self.last_error)
                self.current, self.latency = None, None
                return None
            finally:
                self.searching = False

    async def find(self, excluded=()):
        """(proxy, latency) of a working proxy, or None."""
        excluded = set(excluded)
        # 1. a proxy program on this PC
        for kind, port, label in LOCAL_PORTS:
            p = Proxy(kind, "127.0.0.1", port, label=label)
            if p.link in excluded:
                continue
            if await self.tcp("127.0.0.1", port, 1.5) is not None:
                lat = await self.probe(p)
                if lat is not None:
                    return p, lat
        # 2. proxies that worked before
        for link in self.load_state().get("good", []):
            p = parse_link(link)
            if p and p.link not in excluded:
                lat = await self.probe(p)
                if lat is not None:
                    return p, lat
        # 3. public lists
        cands = [c for c in await self.gather_candidates() if c.link not in excluded]
        if not cands:
            return None
        random.shuffle(cands)                         # spread load across list order
        cands.sort(key=lambda c: c.kind != "mtproto")  # MTProto first
        cands = cands[:MAX_CANDIDATES]
        sem = asyncio.Semaphore(100)

        async def rtt(c):
            async with sem:
                return c, await self.tcp(c.host, c.port, TCP_TIMEOUT)

        timed = [(c, t) for c, t in await asyncio.gather(*(rtt(c) for c in cands)) if t is not None]
        timed.sort(key=lambda ct: (ct[0].kind != "mtproto", ct[1]))
        top = [c for c, _ in timed[:PROBE_TOP]]
        self.log(f"connection: {len(cands)} proxies listed, {len(timed)} answered, testing the fastest")
        for i in range(0, len(top), PROBE_PARALLEL):
            batch = top[i:i + PROBE_PARALLEL]
            results = await asyncio.gather(*(self.probe(c) for c in batch))
            ok = [(c, lat) for c, lat in zip(batch, results) if lat is not None]
            if ok:
                return min(ok, key=lambda cl: cl[1])
        return None

    async def gather_candidates(self):
        async def one(src):
            hint, url = split_source(src)
            text = None
            for u in mirrors(url):
                try:
                    text = await self.fetch(u)
                    break
                except Exception:
                    continue
            cache = self._cache_path(url)
            if text is not None:
                try:
                    os.makedirs(os.path.dirname(cache), exist_ok=True)
                    with open(cache, "w", encoding="utf-8") as f:
                        f.write(text)
                except OSError:
                    pass
            else:
                try:
                    with open(cache, encoding="utf-8") as f:
                        text = f.read()                 # offline: last downloaded copy
                except OSError:
                    self.log(f"connection: could not download {url}")
                    return []
            return parse_list(text, hint)

        lists = await asyncio.gather(*(one(s) for s in self.sources))
        out, seen = [], set()
        for lst in lists:
            for p in lst:
                if p.link not in seen:
                    seen.add(p.link)
                    out.append(p)
        return out

    async def test_link(self, link):
        p = parse_link(link)
        if p is None:
            raise ValueError("That is not a proxy link SpiDrive understands.")
        lat = await self.probe(p)
        return {"ok": lat is not None, "latency_ms": round(lat * 1000) if lat else None,
                "via": p.describe()}
