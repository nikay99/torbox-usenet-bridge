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
- **Respects TorBox limits:** waits for a free slot, stays below the hourly add limit and pauses after a 429, instead of failing (which would make Radarr/Sonarr blocklist good releases)
- **Auto-repair:** if TorBox removes a download and a library symlink breaks, the stored NZB is submitted again (at most once per day)
- **Optional audio language guard:** reject releases (e.g. `MULTi`) that turn out not to contain your language, triggered instantly via webhook
- **Stuck-import handling:** resolves Radarr/Sonarr imports stuck on "Unable to determine if file is a sample" (large files on a network mount)
- **Optional cleanup:** removes TorBox downloads your library no longer uses (off by default)
- Sample clips are never linked, so they can't break imports

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
| `JOB_TIMEOUT_MINUTES` | `90` | Mark a job as failed this long after it was submitted to TorBox |
| `TORBOX_MAX_ACTIVE` | `10` | Active TorBox slots of your plan (Pro: 10). New jobs wait while all slots are busy |
| `TORBOX_SUBMITS_PER_HOUR` | `55` | Max new usenet downloads per hour (TorBox allows 60) |
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
| `GUARD_SECONDS` | `120` | Check interval (fallback if the webhook isn't used) |

The guard is off unless both `ARRS` and `REQUIRED_AUDIO_LANGS` are set. With only `ARRS` set, the bridge still resolves stuck imports.

**Instant check via webhook:** in Radarr/Sonarr add **Settings → Connect → + → Webhook** with URL `http://<bridge>:8383/guard?apikey=<SAB_API_KEY>`, method `POST`, and enable *On File Import* / *On File Upgrade*. The bridge then checks ~20 seconds after each import instead of waiting for the next interval.

### Cleanup (optional)

TorBox keeps every download you ever added: rejected `MULTi` versions, broken uploads, replaced older versions. With cleanup enabled, the bridge periodically deletes TorBox usenet **and torrent** items that no symlink in `LIBRARY_DIRS` points to, that are finished or failed, and older than `CLEANUP_MIN_AGE_HOURS`.

> ⚠️ This deletes **anything in your TorBox account** that your library doesn't use — including items you added by hand or through other tools. Start with the dry run and check the log.

| Variable | Default | Description |
|----------|---------|-------------|
| `CLEANUP_ENABLED` | `0` | Turn the cleanup on |
| `CLEANUP_DRY_RUN` | `1` | Only log what would be deleted |
| `CLEANUP_MIN_AGE_HOURS` | `48` | Never touch items younger than this |
| `CLEANUP_SECONDS` | `21600` | Interval (6 h) |
| `EXTRA_LINK_ROOTS` | — | Colon-separated mounts whose symlinks also count as "in use", e.g. a Decypharr mount (`/mnt/decypharr/__all__`) for torrents |

If the library contains no symlinks at all (e.g. the mount is down), nothing is deleted.

## TorBox limits

The bridge is built around TorBox's documented limits (per API key, subject to change):

- **Active downloads:** Pro allows 10 at a time (torrents and usenet combined). A big season request would otherwise fail from the 11th episode on — the bridge keeps those jobs queued until a slot frees up.
- **New usenet downloads:** 60 per hour (`createusenetdownload`). The bridge stays at `TORBOX_SUBMITS_PER_HOUR`.
- **429 Too Many Requests:** locks the whole key for ~5 minutes. The bridge honours `Retry-After` and pauses all calls; jobs are not marked as failed.
- **Re-adding:** constantly re-adding expired content can be treated as abuse, so auto-repair resubmits a release at most once per day.

## Streaming performance

TorBox's WebDAV is often limited to a few MB/s per connection, which can be too slow for 4K. Letting rclone read each file over several parallel connections helps a lot — in our tests from ~4.5 MB/s to ~17 MB/s per file:

```
--vfs-read-chunk-size 32M --vfs-read-chunk-streams 6
```

(already set in `docker-compose.example.yml`, requires rclone ≥ 1.68)

## Notes

- TorBox's API sits behind Cloudflare, which blocks some default user agents; the bridge sends its own `User-Agent` (override with `USER_AGENT`).
- TorBox may remove downloads that haven't been accessed for a while. Auto-repair resubmits them, as long as the release is still available on Usenet.
- Playback speed depends on your connection to TorBox WebDAV, not on this bridge (see *Streaming performance*).
- For torrents, use a tool like [Decypharr](https://github.com/sirrobot01/decypharr) alongside this bridge.

## Disclaimer

This project only forwards NZB files between your own Radarr/Sonarr and your own TorBox account. It does not host, index or distribute any content. You are responsible for complying with the laws of your country and the terms of the services you use.

## License

[MIT](LICENSE)
