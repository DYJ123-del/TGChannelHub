# TGChannelHub — Telegram Channel Video Relay

> **🌐 [中文](README.md) | [English](README.en.md)**

> Turn your Telegram channel into a **web video station**: no local download required — visitors open a link and watch/stream channel videos and photos right in the browser.

**TGChannelHub** is a self-hosted Telegram channel media relay. Your server acts as the middleman, pulling streams live from Telegram and playing them directly in the browser — **videos never touch disk, seekbar works, plays inside WeChat browser**. Supports **multi-channel management**: one instance proxies multiple channels, each with its own share link.

A deep fork of [tgfilestream](https://mau.dev/tulir/tgfilestream) (AGPL-3.0, Tulir Asokan), rebuilt from a single-channel bot into a multi-channel visual management system.

## ✨ Features

| Feature | Description |
|---|---|
| 🎬 Live streaming relay | Stream-as-you-play, no disk usage; HTTP Range seek support |
| 📚 Multi-channel management | Web admin page to add/remove channels, one-click copy per-channel share links |
| 🖼️ Telegram album aggregation | Same-group media merged into one card: hero image + grid layout |
| 📱 Mobile/WeChat ready | Optimized for WeChat X5 engine (playsinline, correct Range) |
| 🔒 Two-password system | Share password (visitors) + Admin password (managers) separated |
| 🚀 Concurrent acceleration | Multi-thread chunked download + DC connection warmup for fast start |
| 🏷️ Hashtag browsing | Tag bar + full tag cloud + clickable `#tag` in captions, filter by series |
| 🖼️ Image lightbox | Click any photo to open a fullscreen viewer, swipe/arrow to browse prev/next |

## 🚀 Quick Start (Docker recommended)

### 1. Get Telegram API credentials

Sign in at [my.telegram.org/apps](https://my.telegram.org/apps), create an app and grab:

- `api_id` (a number)
- `api_hash` (an alphanumeric string)

> ⚠️ These are **your account credentials** — keep them safe, never commit them to a public repo.

### 2. Get docker-compose.yml

```bash
git clone https://github.com/DYJ123-del/TGChannelHub.git
cd TGChannelHub
```

The repo ships with a ready `docker-compose.yml`. Edit to match your needs:

```yaml
services:
  tgchannelhub:
    build: .
    container_name: tgchannelhub
    restart: unless-stopped
    ports:
      - "8081:8081"        # public port, change to 8080/56200 etc.
    volumes:
      - ./data:/app/data   # all data lives here (see "Where does data live")
    environment:
      - TZ=Asia/Shanghai
      # ---- Telegram API credentials (required) ----
      - TG_API_ID=1234567
      - TG_API_HASH=abcdef0123456789abcdef0123456789
      # ---- Service listening ----
      - PORT=8081
      - HOST=0.0.0.0
      # ---- Public URL (used to generate share links; set to https://your.domain after reverse proxy) ----
      - PUBLIC_URL=http://192.168.1.100:8081
      # ---- Passwords (CHANGE THEM IMMEDIATELY after deploy!) ----
      - TG_UI_PASSWORD=change-me-share
      - TG_ADMIN_PASSWORD=change-me-admin
      # ---- Telegram session file (defaults under data) ----
      - TG_SESSION_NAME=/app/data/tgchannelhub
      # ---- Proxy (optional; only if direct TG connection times out / network restricted) ----
      # - TG_PROXY_HOST=192.168.1.2
      # - TG_PROXY_PORT=7890
      # - TG_PROXY_USER=user
      # - TG_PROXY_PASS=pass
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

> A ready-to-use `docker-compose.yml` is already in the repo — copy it and change the environment variables. No proxy needed? Just delete the `TG_PROXY_*` lines.

### 3. Start + first login

```bash
docker compose up -d --build

# First-time Telegram login (account that owns the channels/favorites)
docker exec -it tgchannelhub python3 scripts/login.py send <your-phone-number>
docker exec -it tgchannelhub python3 scripts/login.py sign <received-code>
# If 2FA is enabled on the account:
docker exec -it tgchannelhub python3 scripts/login.py password <2FA-password>
```

After login, refresh the web page. The session file is stored in `data/` and survives restarts.

## 📁 Where does data live (important)

All data lives under the `./data` directory mounted by compose. **Backup/migration = just pack this directory**:

| File | Purpose |
|---|---|
| `tgchannelhub.session` | Telegram login session (account "key", **never leak it**) |
| `channels.json` | List of proxied channels |
| `passwords.json` | Passwords you changed in the admin page (override env vars) |
| `thumbs/` | Thumbnail cache |
| `avatar_*.jpg` | Channel avatar cache |

Backup: `tar czf tgchannelhub-data.tar.gz data/`; to migrate, extract into the `data/` dir of the new deployment.

## 🔗 Access paths

| Path | Purpose | Password |
|---|---|---|
| `/` | Admin entry (redirects to admin page) | Admin |
| `/manage` | Admin page: add/remove channels / copy share links / change passwords | Admin |
| `/c` | Channel picker (shows all when multiple channels) | Share |
| `/c/<channel>` | Single-channel locked view (only that channel) | Share |
| `/watch/<id>/<name>` | Video/photo player page | Share |

**Typical usage**: you manage via `/manage`; share the `/c/<channel>` link with visitors — they enter the share password to watch.

## 🌐 Reverse proxy (optional, public access)

LAN users can directly use `http://<NAS-IP>:8081`. For public access, use a reverse proxy (Lucky/Nginx/Caddy all work):

```
https://video.example.com → container port 8081
```

Then set `PUBLIC_URL=https://video.example.com` and share links will use the new address automatically.

## 🧩 Common configurations

**Single channel, direct connection works:**

```yaml
environment:
  - TG_API_ID=1234567
  - TG_API_HASH=abcdef...
  - PUBLIC_URL=http://192.168.1.100:8081
  - TG_UI_PASSWORD=my_share_pw
  - TG_ADMIN_PASSWORD=my_admin_pw
```

**Multiple channels:** after startup, click "Load my channels" on `/manage` or type `t.me/xxx` manually — no config change or restart needed.

**Proxy required to reach TG:** enable the four `TG_PROXY_*` lines (HTTP proxy format).

## 🏷️ Tag browsing

- **Tag bar**: high-frequency `#tags` chips under the channel header — click to filter that series
- **Full tag cloud**: "🔍 All tags" link → `/cloud?c=<channel>` lists every tag including low-frequency ones
- **Clickable tags in captions**: any `#tag` text inside a message caption is a link — click to jump into its filtered view
- Filtering uses **Telegram's server-side search** (`get_messages(search="#tag")`), precise with zero local indexing

## 🖼️ Image lightbox

Click any photo (single message or inside an album) to open a fullscreen viewer:

- **Swipe left/right**, click ‹ › arrows, or use keyboard ← → to browse prev/next
- Counter (e.g. `3 / 31`), caption at bottom, ✕ to close
- Videos keep their original behavior (open the `/watch` player page)

## 📤 Upload center (personal cloud drive)

Turn a Telegram group into a personal cloud drive: upload NAS files straight to your group/channel and watch them back through this UI.

- **Path**: `/upload` (admin password protected; link on the admin page)
- **File browser**: browse directories under `UPLOAD_ROOT` (compose volume), multi-select files or whole folders (recursive)
- **Destination**: dropdown of all your groups/channels (via `get_dialogs`), or type `@username` / chat_id (e.g. your personal drive group ID)
- **Queue**: adjustable concurrency (1–16 files at once), pause/resume, cancel single queued tasks
- **Parallel speed-up**: files >10MB upload as 512KB chunks × 8 concurrent (`SaveBigFilePart` + `InputFileBig`) — ~4× faster than serial upload
- **Live progress**: progress bar, % / MB/s / elapsed, refreshed every 2s
- **Watch mode**: monitor a directory; new files auto-enqueue (mtime-stable check, auto `#tag` by parent dir name)
- Proxy/credentials are injected via compose env vars (`TG_PROXY_TYPE` supports `http`/`socks5`); the upload engine reuses the main client — no separate login, no session conflicts.

## 🛠 Development / Local run

```bash
pip install -r requirements.txt -e .
python3 -m channelrelay
```

Project layout:

```
├── channelrelay/
│   ├── channel_ui.py        # All Web UI routes (list/play/manage/auth/tags)
│   ├── telegram.py          # Telegram client + proxy injection
│   ├── paralleltransfer.py  # Concurrent chunked downloader (Telethon-compatible patch)
│   ├── config.py            # Environment variable config
│   ├── util.py              # packed id encode/decode / file names
│   └── __main__.py          # Entry point
├── scripts/login.py         # First-login helper (send/sign/password/verify)
└── docker-compose.yml       # Ready-to-use deployment template
```

## 📄 License

**AGPL-3.0**. This project is a derivative work of [tgfilestream](https://mau.dev/tulir/tgfilestream) (Tulir Asokan); the original copyright notice is preserved as required by AGPL-3.0. See [LICENSE](LICENSE).

## ⚠️ Disclaimer

This project is for learning and technical exchange only. Please comply with the Telegram Terms of Service and your local laws; **do not** use it to distribute infringing or prohibited content. All media comes from Telegram channels you are authorized to access.
