# Copy this file to config.py and fill it in. Never upload config.py.
# (Only needed when running from source without the setup page.)

API_ID = 0                      # from https://my.telegram.org/apps
API_HASH = ""                   # from https://my.telegram.org/apps
PHONE = "+00000000000"          # your Telegram phone number
CHANNEL = ""                    # -100... id of your private storage channel

DATA_DIR = r"D:\SpiDrive\data"  # index, upload queue and caches
CHUNK_SIZE = 256 * 1024 * 1024  # part size for big files (max about 1.9 GB)
ENCRYPT = False                 # True = AES-256 before upload
PASSPHRASE = ""                 # required when ENCRYPT = True

WEB_PORT = 8000
DAV_PORT = 8080
DRIVE_SIZE = "1P"               # size File Explorer shows for the drive
PROXY_MODE = "auto"             # auto | manual | off
PROXY = ""                      # proxy link for manual mode
SYNC_FOLDERS = {}               # {r"D:\SpiDrive Sync": "/Sync"}
