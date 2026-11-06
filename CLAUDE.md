# SpiDrive: notes for Claude Code

SpiDrive turns the user's own Telegram account into a personal cloud drive on Windows. Files are stored as
messages in a private Telegram channel. A local SQLite index maps files to messages. The user sees the files
as a drive in File Explorer, in a web dashboard, and in two-way synced folders.

The owner writes in Persian or English. Answer in the language of their message. They are not a
professional developer, so explain steps plainly and give exact commands.

## Run and test

- `SpiDrive.bat` installs whatever is missing (venv, packages, WinFsp, rclone), then starts `app.py` with a
  tray icon. Use `SpiDrive.bat console` to see the log in the window.
- From the venv: `venv\Scripts\python app.py --console` (logs to the console, no tray).
- Logs: `%LOCALAPPDATA%\SpiDrive\spidrive.log`. Settings: `%LOCALAPPDATA%\SpiDrive\settings.json`.
- Dashboard: http://127.0.0.1:8000. The drive server (WebDAV) for rclone: http://127.0.0.1:8080.
- `python drive.py ls | upload | download | info | restore-index` is a command-line tool (it uses `config.py`).
- No automated test suite is in the repo yet. Earlier tests used a fake `telethon` package (stub modules)
  and fake Telegram clients. Adding `tests/` with pytest and such fakes would be welcome.

**Never commit or print:** `config.py`, `app_id.py`, `*.session`, `data/`, `settings.json`. They hold the API
hash, the Telegram login and the encryption passphrase.

## Architecture

| File | Role |
|---|---|
| `drive_core.py` | Storage engine: split files into parts (`CHUNK_SIZE`), AES-256-CTR encryption (scrypt key, per-file nonce), SHA-256 per part, SQLite index (`files`, `chunks`, `dirs`, `pending`, `offline`, `meta`), upload queue with local staging (`stage()` + `upload_worker()`), copy-by-reference, delete with message reference counting, Telegram Inbox import, prune, index backup, "Keep on this PC" (`keep()`, `keep_worker()`), transfer progress (`note_transfer()`, `activity()`). `iter_entry()` reads any byte range (local copy if any, else Telegram) and is what every reader uses. |
| `proxy.py` | How to reach Telegram: direct, a manual proxy link, or Automatic (local VPN app → remembered proxies → public MTProto lists on GitHub, each tested for real; switches when a proxy dies). `client_kwargs()` turns a choice into `TelegramClient` arguments. |
| `server.py` | aiohttp: dashboard API + static page, background tasks (upload worker, keep worker, Telegram import, prune, backup, sync, connection watcher), and `amain()` which also starts the WebDAV app and the rclone mount. |
| `dav.py` | WebDAV server that the drive letter uses (via rclone). `send_entry()` = HTTP Range streaming, shared with the dashboard. |
| `mount.py` | Starts and stops `rclone mount` (WinFsp) for drive S:, with its own size (`DRIVE_SIZE`). Quits rclone through its remote-control port. `cached_fully()` reads rclone's VFS cache metadata. |
| `sync.py` | Two-way folder sync (`SYNC_FOLDERS`). Rename detection by size+mtime, trash on both sides, conflict copies, pause when a folder disappears. |
| `app.py` | The program users run: single instance, tray icon (pystray), setup wizard on first run, restart, start with Windows. |
| `settings.py` | Settings in `%LOCALAPPDATA%\SpiDrive\settings.json` (secrets protected with DPAPI). `install()` makes `import config` return them. It can import an old `config.py` folder. |
| `setup_server.py`, `web/setup.html` | First-run wizard: Telegram login (code, 2-step password), proxy, storage channel, options. |
| `web/index.html` | Dashboard: browse, search, upload, stream, share, Settings, file status icons and progress bars. |
| `build.bat`, `build_helper.py`, `SpiDrive.iss` | PyInstaller `SpiDrive.exe` + Inno Setup `SpiDrive-Setup.exe`. |

Conventions: Python 3.10+, asyncio everywhere, Telethon for MTProto. Drive paths are POSIX-style
(`/folder/file`), normalised with `norm()`. A file "entry" is a dict; files still uploading have
`status='pending'` and a negative id. Settings come from the `config` module (real `config.py` from the
command line, or `settings.install()` in the app). The old `install.bat`, `setup_rclone.bat`, `start.bat`
and `setup_drive.bat` are obsolete (replaced by `SpiDrive.bat`) and can be deleted.

## Current task: show SpiDrive in File Explorer like OneDrive (Cloud Files API)

**Goal.** Replace the rclone/WinFsp drive letter with a sync root folder (for example
`%USERPROFILE%\SpiDrive`) in File Explorer's navigation pane. Files are placeholders, and Windows itself shows:
- the status icons (cloud = online only, green check outline = on this PC, filled green check = always kept,
  sync arrows = syncing);
- a progress bar while a file downloads;
- **Always keep on this device** and **Free up space** in the right-click menu.

Keep the rclone drive as a fallback setting (for example `DRIVE_MODE = "explorer" | "rclone"`) until the new
mode is proven.

**Docs.** Cloud Filter API: https://learn.microsoft.com/en-us/windows/win32/cfapi/cloud-files-functions ·
pitfalls FAQ: https://learn.microsoft.com/en-us/answers/questions/2288103/cloud-file-api-faq ·
Microsoft's C++ "CloudMirror" sample (Windows-classic-samples) is the reference implementation to mirror.

**Suggested design (new `explorer.py`, plus a ctypes binding module `cfapi.py`):**

1. **Registration.** Register the sync root once, with `StorageProviderSyncRootManager.Register`
   (WinRT, via the `winrt-Windows.Storage.Provider` PyPI packages) so it shows in the navigation pane.
   `CfRegisterSyncRoot` from `cldapi.dll` is the low-level alternative. Microsoft says to use one, not both.
   - Id: `SpiDrive!<user SID>!<account>`. Display name: SpiDrive. Icon from the exe.
   - Hydration policy: full. Population policy: always full, which is simplest: create every placeholder
     up front from the index.
   - Unregister on uninstall.
2. **Connect.** `CfConnectSyncRoot` with a callback table:
   - `FETCH_DATA`: when a placeholder is opened or pinned, read the requested range with
     `drive.iter_entry()`, and answer with `CfExecute(CF_OPERATION_TYPE_TRANSFER_DATA)` in 4 KB-aligned
     chunks. Report progress (`CfReportProviderProgress`) so Explorer shows the bar.
   - `CANCEL_FETCH_DATA`: stop the read.
   - `NOTIFY_DELETE` and `NOTIFY_RENAME`: acknowledge quickly (there is a fixed timeout), then apply
     `drive.delete()` (consider moving to `/SpiDrive Trash` like sync.py does) or `drive.move()`.
   - Callbacks run on Windows thread-pool threads: hand the work to the asyncio loop with
     `asyncio.run_coroutine_threadsafe`, and keep ctypes callback objects alive (module-level references).
3. **Placeholders.** On connect, create placeholders for every folder and file from the index
   (`CfCreatePlaceholders`):
   - FileIdentity = the file id, or the drive path, as bytes.
   - Set size, times and attributes, and mark them in-sync.
   - Keep a local map of path ↔ identity so changes can be applied as deltas.
4. **Changes from the cloud** (Telegram Inbox, dashboard, sync, other devices): create, update
   (`CfUpdatePlaceholder` when size or mtime changed; dehydrate first if needed) or delete placeholders.
   Poll `drive.version()` or add a change hook in `drive_core`.
5. **Changes on the PC.** A new or replaced file in the sync root is a normal file, not a placeholder:
   - Watch with `ReadDirectoryChangesW`, or poll like `sync.py`.
   - Stage it: `drive.new_staging_path()` + copy + `drive.stage()`.
   - After the upload, `CfConvertToPlaceholder` + `CfSetInSyncState(IN_SYNC)`. While uploading, mark it
     not-in-sync, so Explorer shows the sync arrows.
   - Saving over a placeholder removes its reparse point. Detect this and treat it as a local edit (see the
     FAQ).
6. **Pin state.** Windows handles **Always keep on this device** (it requests hydration) and **Free up space**
   (it dehydrates). Optionally mirror the pinned state into the existing `offline` table, so the dashboard's
   status icons agree.
7. **Encryption.** Unchanged: hydration goes through `iter_entry()`, which decrypts.
8. **App integration.**
   - Start and stop the provider in `server.amain()` instead of `Mount` when `DRIVE_MODE == "explorer"`.
   - Add the choice to Settings and the setup wizard.
   - `SpiDrive.bat` and `SpiDrive.iss` then no longer need rclone or WinFsp in that mode.

**Manual test plan on Windows:**
1. Start the app and check that SpiDrive appears in the navigation pane with all folders.
2. Check that files show the cloud icon and take no disk space.
3. Open a large video: it should start playing, with a progress bar in Explorer.
4. Right-click → **Always keep on this device**: the filled green check appears. **Free up space**: back to
   the cloud icon.
5. Drag in a folder: the sync arrows appear, then the green checks; the files appear in the dashboard and in
   the Telegram channel.
6. Rename, move and delete in Explorer: the dashboard follows. Do the same in the dashboard: Explorer follows.
7. Send a file from the phone to the channel: it appears in Telegram Inbox in Explorer.
8. Repeat with encryption on, through a proxy, and after restarting the app.
