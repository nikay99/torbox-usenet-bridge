# torbox-usenet-bridge

A tiny SABnzbd-compatible download client that lets **Radarr** and **Sonarr** use **TorBox Usenet** — without downloading anything locally.

Radarr/Sonarr think they are talking to SABnzbd. The bridge hands the NZB to TorBox, waits until TorBox has the files, and then creates **symlinks** that point into your TorBox WebDAV mount (e.g. rclone). Radarr/Sonarr import the symlinks, and your media server streams straight from TorBox.

```
Radarr / Sonarr ──NZB──▶ torbox-usenet-bridge ──API──▶ TorBox Usenet
                                 │
                                 └─ symlink ──▶ /mnt/torbox/<release>/<file>.mkv  (rclone WebDAV mount)
```

- Single Python file, standard library only — no dependencies
- Speaks the subset of the SABnzbd API that Radarr/Sonarr use (`version`, `get_config`, `get_cats`, `queue`, `history`, `addfile`, `addurl`, `delete`)
- Nothing is downloaded to your disk; the only data that passes through the bridge is the NZB file
- **Auto-repair:** if TorBox removes a download and a library symlink breaks, the stored NZB is submitted again
- **Optional audio language guard:** reject releases (e.g. `MULTi`) that turn out not to contain your language

> Not affiliated with TorBox, SABnzbd, Radarr or Sonarr.

## Requirements

- A TorBox plan with Usenet access and an API key
- The TorBox WebDAV (`https://webdav.torbox.app`) mounted with rclone, visible **at the same path** in the bridge, Radarr and Sonarr (e.g. `/mnt/torbox`)
- A Usenet indexer configured in Radarr/Sonarr (directly or via Prowlarr)

## Quick start

```bash
git clone https://github.com/nikay99/torbox-usenet-bridge
cd torbox-usenet-bridge
cp .env.example .env        # fill in TORBOX_API_KEY and SAB_API_KEY
docker compose -f docker-compose.example.yml up -d --build
```

Then in Radarr / Sonarr: **Settings → Download Clients → + → SABnzbd**

| Field    | Value                                  |
|----------|----------------------------------------|
| Host     | host/IP of the bridge                  |
| Port     | `8383`                                 |
| API Key  | your `SAB_API_KEY`                     |
| Category | `radarr` (Radarr) / `sonarr` (Sonarr)  |

Click **Test** — it should succeed. Make sure the path in `COMPLETE_DIR` (default `/data/downloads`) is the same inside Radarr/Sonarr, or add a Remote Path Mapping.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TORBOX_API_KEY` / `TORBOX_API_KEY_FILE` | — | TorBox API key (or a file containing it) |
| `SAB_API_KEY` / `SAB_API_KEY_FILE` | — | API key Radarr/Sonarr must send |
| `PORT` | `8383` | Listen port |
| `MOUNT_DIR` | `/mnt/torbox` | Where the TorBox WebDAV is mounted |
| `COMPLETE_DIR` | `/data/downloads` | Where symlinks are created (`<dir>/<category>/<name>/`) |
| `LIBRARY_DIRS` | `/data/media` | Colon-separated library roots scanned by auto-repair |
| `CATEGORIES` | `radarr,sonarr` | Categories reported to the *arrs |
| `DATA_DIR` | `/app/data` | Job database and stored NZBs (keep this persistent) |
| `POLL_SECONDS` | `15` | TorBox status poll interval |
| `REPAIR_SECONDS` | `3600` | Interval of the broken-symlink check |
| `JOB_TIMEOUT_MINUTES` | `90` | Mark a job as failed after this time |
| `MIN_VIDEO_MB` | `50` | Ignore smaller files (samples) |
| `VIDEO_EXTENSIONS` | `.mkv,.mp4,...` | File types to link |
| `PUID` / `PGID` | `1000` | Owner of created symlinks |
| `LOG_LEVEL` | `INFO` | Python log level |

### Audio language guard (optional)

Releases tagged `MULTi` often contain several audio languages, but the name doesn't tell you which. If you require a specific language, the guard checks the real audio tracks Radarr/Sonarr read after import. When none of your languages is present, the grab is marked as failed (→ blocklisted, Radarr/Sonarr search again) and the file is removed.

| Variable | Example | Description |
|----------|---------|-------------|
| `ARRS` | `radarr\|http://radarr:7878\|KEY,sonarr\|http://sonarr:8989\|KEY` | *arr instances to watch (`name\|url\|apikey`, name must start with `radarr` or `sonarr`) |
| `REQUIRED_AUDIO_LANGS` | `ger,deu,german` | Accepted language names/codes as reported by mediaInfo |
| `GUARD_RELEASE_REGEX` | `\bMULTI\b` | Only releases matching this are checked |
| `GUARD_SECONDS` | `300` | Check interval |

The guard is off unless both `ARRS` and `REQUIRED_AUDIO_LANGS` are set.

## Notes

- TorBox's API sits behind Cloudflare, which blocks some default user agents; the bridge sends its own `User-Agent` (override with `USER_AGENT`).
- TorBox may remove downloads that haven't been accessed for a while. Auto-repair resubmits them, as long as the release is still available on Usenet.
- Playback speed depends on your connection to TorBox WebDAV, not on this bridge.
- For torrents, use a tool like [Decypharr](https://github.com/sirrobot01/decypharr) alongside this bridge.

## Disclaimer

This project only forwards NZB files between your own Radarr/Sonarr and your own TorBox account. It does not host, index or distribute any content. You are responsible for complying with the laws of your country and the terms of the services you use.

## License

[MIT](LICENSE)
