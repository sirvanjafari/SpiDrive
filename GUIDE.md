# SpiDrive

Your Telegram account as a personal cloud drive on Windows. Files live in a private Telegram channel; SpiDrive shows them as drive **S:** in File Explorer, in a web dashboard, and (optionally) keeps a normal folder on your PC in two-way sync.

## What it does

- **Drive S: in File Explorer.** Drag and drop files and folders, rename, delete, open. The drive shows its own size (10 TB by default) and has no per-file limit.
- **Web dashboard** at http://127.0.0.1:8000. Browse, search, upload, and stream photos, music and video without downloading the whole file. Storage stats are in the sidebar.
- **From Telegram.** Anything you send or forward to the channel appears in **Telegram Inbox** within about 15 seconds.
- **Two-way folder sync.** A folder on your PC mirrors a folder on the drive.
- **Share.** Send any file to a Telegram contact with one click.
- **Large files** are split into parts automatically.
- **Optional AES-256 encryption** before anything leaves your PC.
- **Safe uploads.** Files are saved on your PC first, so copying finishes fast. Uploads resume after a network drop or a restart.

## Files

| File | What it is |
|---|---|
| `SpiDrive.bat` | Run from source: installs whatever is missing, then starts SpiDrive (one file for everything) |
| `config.py` | Your settings (see below). Keep it private. |
| `session.session` | Your Telegram login. **Never share it.** |
| `server.py` | Runs everything: dashboard, drive, uploads, sync, sharing |
| `drive_core.py` | Storage engine: chunks, encryption, index, upload queue |
| `dav.py` | The drive server that S: connects to |
| `mount.py` | Starts and stops rclone for S: |
| `sync.py` | Two-way folder sync |
| `drive.py` | Command line: `upload`, `download`, `ls`, `restore-index`… |
| `web/index.html` | The dashboard |
| `app.py` | The app: tray icon, setup wizard, restarts, start with Windows |
| `settings.py` | App settings in `%LOCALAPPDATA%\SpiDrive\settings.json` (replaces `config.py` in the app) |
| `setup_server.py`, `web/setup.html` | The first-run setup wizard |
| `build.bat`, `build_helper.py`, `SpiDrive.iss` | Build `SpiDrive.exe` and `SpiDrive-Setup.exe` |
| `THIRD_PARTY.txt` | Licenses of the open-source parts |
| `proxy.py` | Connects through a proxy when Telegram is blocked: finds one by itself, or uses yours |

## The app (SpiDrive.exe), the easy way

### Build the installer (once, on your PC)

1. Put all the files in one folder, together with your current `config.py`. Its API ID gets built into the app, so users don't need one of their own.
2. Optional: download the WinFsp `.msi` from https://winfsp.dev/rel/ into a folder named `installer`, so the installer can set up WinFsp too.
3. Double-click **`build.bat`**. It installs the build tools, makes `dist\SpiDrive\SpiDrive.exe`, then **`dist\SpiDrive-Setup.exe`**.

### Install and use

1. Run **SpiDrive-Setup.exe**. No admin rights are needed, except the one Windows prompt for WinFsp.
2. A setup page opens in your browser:
   - Log in with your phone number and the code Telegram sends you.
   - Choose a storage channel: create **MyDrive**, or pick one you already own.
   - Choose encryption, the drive letter, an optional sync folder, and whether SpiDrive starts with Windows.
   - **Already using SpiDrive?** The page offers to import your old folder (config, login and files).
3. SpiDrive's icon sits next to the clock. Click it to open the dashboard; right-click it for *Open drive*, *Open log*, *Restart* and *Quit*.
4. Change anything later with the **gear button** in the dashboard. Saving restarts SpiDrive by itself.

The app keeps its settings, login, index and logs in `%LOCALAPPDATA%\SpiDrive`. Uninstalling keeps that folder, so your file list is never lost. Secrets in `settings.json` are protected with Windows' own encryption and only work for your Windows account.

`app_id.py` (created by the build) contains your API hash. Don't upload it to GitHub.

## Run from source: SpiDrive.bat

Put the files in one folder and double-click **`SpiDrive.bat`**, every time. Each time, it checks what is
missing and only does that, then starts SpiDrive with its icon next to the clock:

1. **Python:** finds Python 3.10 or newer and creates the `venv` folder.
2. **Packages:** installs the packages, the first time and whenever `requirements.txt` changes.
   If the download is blocked, it retries through a VPN app on this PC (v2rayN, Clash, Nekoray).
3. **WinFsp:** installs WinFsp for drive S:. Windows asks for permission once.
4. **rclone:** installs rclone.
5. **Start:** starts SpiDrive. The first time, a setup page opens in your browser. There you log in to Telegram,
   choose the channel and options, and, if Telegram is blocked, connect through a proxy.
   If this folder already has a `config.py` and `session.session`, the page offers to use them.

`SpiDrive.bat console` starts it in the window instead, with the log shown there.
The first run takes a few minutes; later runs start in seconds. It replaces the old
`install.bat`, `setup_rclone.bat`, `start.bat` and `setup_drive.bat`.

## Settings in config.py

```python
API_ID = 123456
API_HASH = "..."
PHONE = "+98..."
CHANNEL = -100...                 # from the setup page / telegram_login.py
DATA_DIR = r"D:\TelegramDrive\data"
CHUNK_SIZE = 256 * 1024 * 1024    # part size for big files (max about 1.9 GB)
ENCRYPT = False                   # True = AES-256 before upload
PASSPHRASE = ""                   # needed when ENCRYPT = True. If you lose it, encrypted files are lost.
WEB_PORT = 8000
DAV_PORT = 8080

# optional
DRIVE_SIZE = "10T"                # size File Explorer shows for S: (Telegram itself has no limit)
RCLONE_CACHE_SIZE = "20G"         # rclone's local cache limit
SYNC_FOLDERS = {                  # two-way sync: PC folder -> drive folder
    r"D:\SpiDrive Sync": "/Sync",
}
WEB_HOST = "0.0.0.0"              # only to open the dashboard from your phone (needs WEB_PASSWORD)
WEB_PASSWORD = "choose-one"
PROXY_MODE = "auto"               # auto | manual | off
PROXY = ""                        # manual proxy link
```

Restart SpiDrive after changing `config.py`.

## When Telegram is blocked: proxy

In **Settings → Connection to Telegram** (or with `PROXY_MODE` in `config.py`):

| Mode | What it does |
|---|---|
| **Automatic** (default) | Connects directly when Telegram is reachable. When it is not, it uses the first of these that works: the proxy that worked last time; a VPN app on this PC (v2rayN, Nekoray, Clash, Tor); proxies that worked before; public MTProto lists on GitHub. Each proxy is tested for real before use. If the proxy stops working, SpiDrive switches to another one by itself. |
| **Always use my proxy** | Uses the link you paste: `tg://proxy?…`, `https://t.me/proxy?…`, `socks5://…` or `http://…` |
| **Always connect directly** | No proxy. |

**Find a working proxy now** in Settings searches again. The proxy lists can be changed under
*Proxy lists used by Automatic*, one address per line. When GitHub is blocked, the same files are fetched
through jsDelivr; when both are blocked, the last downloaded copy is used.

**Privacy:** Telegram traffic is encrypted end to end, so a proxy cannot read your files. It can see
when and how much data you send. MTProto proxies are preferred because they are made for Telegram.
"Fake-TLS" proxies (secrets starting with `ee`) are skipped, because SpiDrive's Telegram library
cannot use them.

## Two-way folder sync

Add `SYNC_FOLDERS` to `config.py` and restart. Every 10 seconds:

| You do this… | SpiDrive does this |
|---|---|
| Add or change a file in the PC folder | Uploads it to the drive folder |
| Add or change a file on the drive (dashboard, S:, Telegram) | Downloads it to the PC folder |
| Rename or move a file on either side | Renames it on the other side, without uploading again |
| Delete on the PC | Moves the drive copy to `/SpiDrive Trash` on the drive |
| Delete on the drive | Moves the PC copy to `data\sync-trash` (emptied after 30 days) |
| Change the same file on both sides | Keeps both: your PC copy becomes `name (conflict date).ext` |

Sync never deletes anything permanently. Empty `/SpiDrive Trash` yourself in the dashboard when you're sure. If the PC folder disappears (for example, an unplugged USB disk), sync pauses instead of deleting anything. Tip: syncing `/Telegram Inbox` brings everything you send from your phone straight into a folder on your PC.

## File status (like OneDrive)

The dashboard shows where each file lives, with a progress bar while it moves:

| Icon | Meaning |
|---|---|
| Cloud | **Online only.** Only in Telegram; opens online, takes no space on this PC. |
| Green check outline | **On this PC.** In a synced folder, or recently opened from drive S: (freed after an hour). |
| Green filled check | **Always on this PC.** You chose *Keep on this PC*; opens instantly, also offline. |
| Turning arrows | **Uploading / Downloading** with a live percentage and progress bar. |

Hover a file (or folder) for **Keep on this PC** or **Free up space**. Kept files live in
`data\offline` (decrypted), and *On this PC* in the sidebar shows how much space they take.

## Sharing a file

In the dashboard, click the **paper-plane** button next to a file and type an **@username**, a phone number saved in your Telegram contacts, or **me** (your Saved Messages). The recipient gets a normal, readable file. Encrypted and split files are decrypted and joined first. Telegram's limit for one file is 2 GB (4 GB with Premium).

## On your phone

Set `WEB_HOST = "0.0.0.0"` and a `WEB_PASSWORD`, restart SpiDrive, and open `http://<your PC's IP>:8000` on your phone while it's on the same Wi-Fi. In Chrome, choose **Add to Home screen** to get a SpiDrive icon. Your PC must be on.

## If something goes wrong

- **Lost or damaged index (or a new PC):** the index (`data\index.db`) is the list of which messages belong to which file. SpiDrive backs it up to the channel every day. To get it back:
  ```
  venv\Scripts\activate
  python drive.py restore-index
  ```
  Close SpiDrive first. Your old index is kept as a copy.
- **S: doesn't appear:** the SpiDrive window prints the reason. rclone's own log is `data\rclone.log`.
- **"FloodWait" pauses:** Telegram is limiting upload speed. SpiDrive waits and continues on its own.
- **Uploads stuck "retrying":** check your internet. SpiDrive keeps retrying.

## Keep private

`config.py` (your API hash), `session.session` (full access to your Telegram account) and your `PASSPHRASE`. Don't upload these to GitHub or send them to anyone.
