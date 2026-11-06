"""
SpiDrive settings for the app version (replaces hand-editing config.py).

Settings live in  %LOCALAPPDATA%\\SpiDrive\\settings.json  and are edited in
the setup wizard and the dashboard's Settings page. Secrets (passphrase, API
hash, web password) are protected with Windows DPAPI, so the file is useless
on another PC or Windows account.

install() puts the settings in place of the old config module, so the rest of
SpiDrive (which does `import config`) works unchanged. Running the old way
(python server.py next to a config.py) still works too.
"""
import ast
import base64
import json
import os
import shutil
import sys
import types

if os.name == "nt":
    APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "SpiDrive")
else:
    APP_DIR = os.path.join(os.path.expanduser("~"), ".spidrive")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
LOG_FILE = os.path.join(APP_DIR, "spidrive.log")
SECRETS = ("API_HASH", "PASSPHRASE", "WEB_PASSWORD")

DEFAULTS = {
    "API_ID": 0,
    "API_HASH": "",
    "PHONE": "",
    "CHANNEL": "",
    "CHANNEL_TITLE": "",
    "SESSION_PATH": os.path.join(APP_DIR, "session"),
    "DATA_DIR": os.path.join(APP_DIR, "data"),
    "CHUNK_SIZE": 256 * 1024 * 1024,
    "ENCRYPT": False,
    "PASSPHRASE": "",
    "WEB_HOST": "127.0.0.1",
    "WEB_PORT": 8000,
    "WEB_PASSWORD": "",
    "DAV_PORT": 8080,
    "RC_PORT": 5573,
    "DRIVE_LETTER": "S:",
    "DRIVE_SIZE": "10T",
    "RCLONE_CACHE_SIZE": "20G",
    "SYNC_FOLDERS": {},
    "AUTOSTART": True,
    "PROXY_MODE": "auto",          # auto | manual | off   (see proxy.py)
    "PROXY": "",                   # manual proxy link
    "PROXY_SOURCES": None,         # None = proxy.DEFAULT_SOURCES
}


# ---------------------------------------------------------------- secret protection

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _dpapi(data, protect):
        crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(data, len(data))
        src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _Blob()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        if not fn(ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(out)):
            raise OSError("Windows could not unlock a saved secret (was settings.json copied from another PC?)")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)


def protect(text):
    if not text:
        return ""
    raw = text.encode("utf-8")
    if os.name == "nt":
        return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode()
    return "plain:" + base64.b64encode(raw).decode()


def unprotect(value):
    if not value or not isinstance(value, str):
        return value or ""
    kind, _, b64 = value.partition(":")
    if kind == "dpapi":
        return _dpapi(base64.b64decode(b64), False).decode("utf-8")
    if kind == "plain":
        return base64.b64decode(b64).decode("utf-8")
    return value


# ---------------------------------------------------------------- load / save

def _embedded_app_id():
    """API ID built into the .exe by build.bat (app_id.py), if any."""
    try:
        import app_id
        return int(app_id.API_ID), str(app_id.API_HASH)
    except Exception:
        return 0, ""


def load():
    values = dict(DEFAULTS)
    values["SYNC_FOLDERS"] = {}
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            stored = json.load(f)
        for k, v in stored.items():
            values[k] = unprotect(v) if k in SECRETS else v
    if not values["API_ID"] or not values["API_HASH"]:
        api_id, api_hash = _embedded_app_id()
        if api_id:
            values["API_ID"], values["API_HASH"] = api_id, api_hash
    return values


def save(values):
    os.makedirs(APP_DIR, exist_ok=True)
    out = {}
    for k, v in values.items():
        if k not in DEFAULTS:
            continue
        out[k] = protect(v) if k in SECRETS else v
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    os.replace(tmp, SETTINGS_FILE)


def session_file(values):
    return values["SESSION_PATH"] + ".session"


def is_set_up(values):
    return bool(values.get("API_ID") and values.get("API_HASH") and str(values.get("CHANNEL")).strip()
                and os.path.exists(session_file(values)))


def install(values):
    """Make `import config` return these settings."""
    mod = types.ModuleType("config")
    mod.__dict__.update(values)
    mod.__spidrive_settings__ = True
    sys.modules["config"] = mod
    return mod


# ---------------------------------------------------------------- import from the old setup

_OPS = {ast.Mult: lambda a, b: a * b, ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b, ast.Pow: lambda a, b: a ** b}


def _literal(node):
    """Evaluate the simple expressions config.py uses (numbers, strings, 1900*1024*1024, dicts)."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _literal(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _literal(node.left), _literal(node.right)
        if isinstance(node.op, ast.Pow) and abs(b) > 64:
            raise ValueError("too big")
        return _OPS[type(node.op)](a, b)
    if isinstance(node, ast.Dict):
        return {_literal(k): _literal(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(x) for x in node.elts]
    raise ValueError("unsupported")


def read_legacy_config(path):
    """Values from an old config.py, read without running it."""
    with open(path, encoding="utf-8-sig") as f:
        tree = ast.parse(f.read(), path)
    out = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            try:
                out[stmt.targets[0].id] = _literal(stmt.value)
            except (ValueError, TypeError, ZeroDivisionError, OverflowError):
                pass
    return out


def import_legacy(folder, values):
    """Take over an existing SpiDrive folder (config.py + session.session + data).
    The data folder stays where it is. Returns a list of what was imported."""
    folder = os.path.abspath(folder)
    cfg_path = os.path.join(folder, "config.py")
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f"No config.py in {folder}")
    old = read_legacy_config(cfg_path)
    done = []
    for k in DEFAULTS:
        if k in old and k not in ("SESSION_PATH",) and old[k] not in (None, ""):
            values[k] = old[k]
            done.append(k)
    if not os.path.isabs(str(values["DATA_DIR"])):
        values["DATA_DIR"] = os.path.join(folder, str(values["DATA_DIR"]))
    old_session = os.path.join(folder, "session.session")
    if os.path.exists(old_session):
        os.makedirs(APP_DIR, exist_ok=True)
        shutil.copy2(old_session, session_file(values))
        done.append("login (session.session)")
    letter_file = os.path.join(folder, "drive_letter.txt")
    if os.path.exists(letter_file):
        with open(letter_file, encoding="utf-8-sig") as f:
            txt = f.read().strip().upper()
        if txt[:1].isalpha():
            values["DRIVE_LETTER"] = txt[0] + ":"
    if isinstance(values.get("SYNC_FOLDERS"), (list, tuple)):
        values["SYNC_FOLDERS"] = dict(values["SYNC_FOLDERS"])
    return done


def legacy_candidates():
    """Folders that look like an existing SpiDrive install (to offer in the wizard)."""
    found = []
    here = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__))
    for p in [os.getcwd(), here, r"D:\TelegramDrive", r"C:\TelegramDrive",
              os.path.join(os.path.expanduser("~"), "TelegramDrive")]:
        if os.path.exists(os.path.join(p, "config.py")) and os.path.exists(os.path.join(p, "session.session")):
            if os.path.abspath(p) not in found:
                found.append(os.path.abspath(p))
    return found
