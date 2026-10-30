"""torbox-usenet-bridge: a SABnzbd-compatible download client for Radarr/Sonarr backed by TorBox Usenet.

Flow: Radarr/Sonarr send an NZB (addfile/addurl) -> the bridge submits it to TorBox Usenet -> waits until
TorBox has the files -> creates symlinks under COMPLETE_DIR/<category>/<name>/ that point into the TorBox
WebDAV mount (MOUNT_DIR, e.g. via rclone) -> reports "Completed" in the history. Nothing is downloaded locally.

TorBox limits are respected: submissions wait for a free slot (TORBOX_MAX_ACTIVE), stay below the hourly
createusenetdownload budget (TORBOX_SUBMITS_PER_HOUR) and pause all API calls after a 429. "Busy" never
marks a job as failed, so Radarr/Sonarr don't blocklist good releases.

Repair: if a library symlink points into the mount but its target is gone (TorBox removed the download),
the stored NZB is submitted to TorBox again (at most once per day per release).

Audio language guard (optional): releases whose name matches GUARD_RELEASE_REGEX (default: MULTi) are checked
after import using the real audio tracks reported by Radarr/Sonarr (mediaInfo). If none of
REQUIRED_AUDIO_LANGS is present, the grab is marked as failed (-> blocklist + new search) and the file is removed.
Radarr/Sonarr can trigger the check right after an import via the webhook /guard?apikey=SAB_API_KEY.

Cleanup (optional, off by default): deletes TorBox downloads that no library symlink uses anymore.
"""
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "1.1.0"


def env_secret(name, file_default):
    """Read a secret from NAME or from the file given in NAME_FILE."""
    if os.environ.get(name):
        return os.environ[name].strip()
    path = os.environ.get(name + "_FILE", file_default)
    if path and os.path.exists(path):
        return open(path).read().strip()
    raise SystemExit(f"Missing {name} (or {name}_FILE)")


def flag(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


API = os.environ.get("TORBOX_API_URL", "https://api.torbox.app/v1/api")
TORBOX_KEY = env_secret("TORBOX_API_KEY", "/app/secrets/torbox.key")
SAB_KEY = env_secret("SAB_API_KEY", "")
PORT = int(os.environ.get("PORT", "8383"))
MOUNT_DIR = os.environ.get("MOUNT_DIR", "/mnt/torbox").rstrip("/")
COMPLETE_DIR = os.environ.get("COMPLETE_DIR", "/data/downloads")
LIBRARY_DIRS = [d for d in os.environ.get("LIBRARY_DIRS", "/data/media").split(":") if d]
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
CATEGORIES = [c for c in os.environ.get("CATEGORIES", "radarr,sonarr").split(",") if c]
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
REPAIR_SECONDS = int(os.environ.get("REPAIR_SECONDS", "3600"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_MINUTES", "90")) * 60
VIDEO_EXT = tuple(os.environ.get("VIDEO_EXTENSIONS", ".mkv,.mp4,.avi,.m4v,.ts,.mov,.wmv").split(","))
MIN_VIDEO_BYTES = int(os.environ.get("MIN_VIDEO_MB", "50")) * 1024 * 1024
UID, GID = int(os.environ.get("PUID", "1000")), int(os.environ.get("PGID", "1000"))
# TorBox is behind Cloudflare, which rejects Python's default User-Agent ("error code: 1010")
USER_AGENT = os.environ.get("USER_AGENT", f"torbox-usenet-bridge/{__version__}")
# TorBox limits (Pro: 10 active downloads, 60 createusenetdownload calls per hour)
MAX_ACTIVE = int(os.environ.get("TORBOX_MAX_ACTIVE", "10"))
SUBMITS_PER_HOUR = int(os.environ.get("TORBOX_SUBMITS_PER_HOUR", "55"))
# Language guard (disabled unless ARRS and REQUIRED_AUDIO_LANGS are set): "name|url|apikey", comma separated
ARRS = [a.split("|") for a in os.environ.get("ARRS", "").split(",") if a.count("|") == 2]
LANGS = [l.strip() for l in os.environ.get("REQUIRED_AUDIO_LANGS", "").split(",") if l.strip()]
REQUIRED = re.compile(r"\b(" + "|".join(map(re.escape, LANGS)) + r")\b", re.I) if LANGS else None
GUARD_RELEASE = re.compile(os.environ.get("GUARD_RELEASE_REGEX", r"\bMULTI\b"), re.I)
GUARD_SECONDS = int(os.environ.get("GUARD_SECONDS", "120"))
GUARD_WAKE = threading.Event()  # set by the /guard webhook -> check right after an import
# Cleanup (off by default): deletes TorBox items that no library symlink uses
CLEANUP_ENABLED = flag("CLEANUP_ENABLED", "0")
CLEANUP_DRY_RUN = flag("CLEANUP_DRY_RUN", "1")
CLEANUP_SECONDS = int(os.environ.get("CLEANUP_SECONDS", "21600"))
CLEANUP_MIN_AGE = int(os.environ.get("CLEANUP_MIN_AGE_HOURS", "48")) * 3600
# Other mounts whose symlinks count as "in use" (e.g. a Decypharr mount for torrents), colon separated
EXTRA_LINK_ROOTS = [d.rstrip("/") for d in os.environ.get("EXTRA_LINK_ROOTS", "").split(":") if d]

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("torbox-usenet-bridge")
os.makedirs(os.path.join(DATA_DIR, "nzbs"), exist_ok=True)
DB_LOCK = threading.Lock()


def db():
    con = sqlite3.connect(os.path.join(DATA_DIR, "jobs.db"), timeout=30)
    con.row_factory = sqlite3.Row
    return con


with db() as c:
    c.execute("""create table if not exists jobs (
        nzo_id text primary key, name text, category text, status text, torbox_id integer,
        storage text, bytes integer default 0, progress real default 0, message text default '',
        added real, finished real, nzb_file text)""")
    if "submitted" not in [r[1] for r in c.execute("pragma table_info(jobs)")]:
        c.execute("alter table jobs add column submitted real")  # timeout counts from submission, not queue time
    c.execute("create table if not exists repairs (item text primary key, at real)")


# ---------------------------------------------------------------- TorBox
class TorBoxBusy(Exception):
    """TorBox temporarily unavailable (429, slots full, hourly budget) -> retry later, never a failure."""


PAUSED_UNTIL = 0.0
SUBMITS = []  # timestamps of recent submissions


def torbox(method, path, fields=None, files=None, body=None):
    """Minimal TorBox API call (multipart when fields/files are given, JSON when body is given)."""
    global PAUSED_UNTIL
    if time.time() < PAUSED_UNTIL:
        raise TorBoxBusy("TorBox API paused after 429 until " + time.strftime("%H:%M:%S", time.localtime(PAUSED_UNTIL)))
    headers = {"Authorization": "Bearer " + TORBOX_KEY, "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif fields is not None or files is not None:
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in (fields or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (fname, content) in (files or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                         f"Content-Type: application/x-nzb\r\n\r\n".encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        if e.code == 429:  # the lockout applies to the whole key (~5 min) -> pause every caller
            PAUSED_UNTIL = time.time() + int(e.headers.get("Retry-After") or 300)
            log.warning("TorBox 429 (%s) -> pausing until %s", text[:80], time.strftime("%H:%M:%S", time.localtime(PAUSED_UNTIL)))
            raise TorBoxBusy("TorBox 429")
        try:
            return json.loads(text)
        except ValueError:
            return {"success": False, "detail": f"HTTP {e.code}: {text[:200]}"}


def torbox_list(kind):
    """Full mylist (TorBox returns at most 1000 entries per page)."""
    out, seen, offset = [], set(), 0
    while True:
        page = torbox("GET", f"/{kind}/mylist?bypass_cache=true&offset={offset}&limit=1000").get("data") or []
        new = [d for d in page if d.get("id") not in seen]
        seen.update(d.get("id") for d in new)
        out += new
        if len(page) < 1000 or not new:
            return out
        offset += len(page)


def active_downloads():
    """Entries occupying a TorBox slot (TorBox flags them with active=true, incl. unpacking and seeding)."""
    return sum(1 for kind in ("usenet", "torrents") for d in torbox_list(kind) if d.get("active"))


def submit_nzb(name, nzb_bytes):
    now = time.time()
    SUBMITS[:] = [t for t in SUBMITS if now - t < 3600]
    if len(SUBMITS) >= SUBMITS_PER_HOUR:
        raise TorBoxBusy("hourly budget for new usenet downloads reached")
    SUBMITS.append(now)
    r = torbox("POST", "/usenet/createusenetdownload", fields={"name": name}, files={"file": (name + ".nzb", nzb_bytes)})
    d = r.get("data") or {}
    tid = d.get("usenetdownload_id") or d.get("id")
    if not r.get("success") or not tid:
        msg = str(r.get("detail") or r.get("error") or "TorBox rejected the download")
        if re.search(r"active download limit|slot|per 1 hour|rate limit|too many", msg, re.I):
            raise TorBoxBusy(msg)
        raise RuntimeError(msg)
    return int(tid)


def torbox_item(tid):
    r = torbox("GET", f"/usenet/mylist?id={tid}&bypass_cache=true")
    d = r.get("data")
    return d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else None)


# ---------------------------------------------------------------- Jobs
def safe_name(s):
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s).strip(" .")
    return s[:200] or "download"


def add_job(name, category, nzb_bytes):
    nzo_id = "SABnzbd_nzo_" + uuid.uuid4().hex[:12]
    name = safe_name(re.sub(r"\.nzb$", "", name, flags=re.I))
    nzb_path = os.path.join(DATA_DIR, "nzbs", nzo_id + ".nzb")
    with open(nzb_path, "wb") as f:
        f.write(nzb_bytes)
    with DB_LOCK, db() as c:
        c.execute("insert into jobs (nzo_id,name,category,status,added,nzb_file) values (?,?,?,?,?,?)",
                  (nzo_id, name, category or "", "Queued", time.time(), nzb_path))
    log.info("New job %s: %s [%s]", nzo_id, name, category)
    return nzo_id


def set_job(nzo_id, **kw):
    cols = ",".join(f"{k}=?" for k in kw)
    with DB_LOCK, db() as c:
        c.execute(f"update jobs set {cols} where nzo_id=?", (*kw.values(), nzo_id))


def webdav_paths(item):
    """Locate the video files of a TorBox download inside the WebDAV mount."""
    base = os.path.join(MOUNT_DIR, item.get("name") or "")
    found = []
    for f in item.get("files") or []:
        rel = f.get("short_name") or os.path.basename(f.get("name") or "")
        if not rel.lower().endswith(VIDEO_EXT) or (f.get("size") or 0) < MIN_VIDEO_BYTES:
            continue
        if re.search(r"(^|[/._-])sample([/._-]|$)", (f.get("name") or rel).lower()):
            continue  # sample clips (Sample/...) make imports in Radarr/Sonarr fail
        candidates = [os.path.join(base, rel)]
        full = f.get("name") or ""
        if "/" in full:  # TorBox sometimes reports "<folder>/<file>"
            candidates.append(os.path.join(MOUNT_DIR, full))
        found.append((rel, candidates, f.get("size") or 0))
    return found


def finalize(job, item):
    """Create the symlinks once the files are visible in the mount. Returns True when done."""
    files = webdav_paths(item)
    if not files:
        names = [(f.get("short_name") or f.get("name") or "").lower() for f in item.get("files") or []]
        if any(re.search(r"\.(rar|r\d\d|\d{3})$", n) for n in names):
            raise RuntimeError("Only RAR archives, TorBox could not extract them (incomplete release)")
        raise RuntimeError("No video file found in the TorBox download")
    target_dir = os.path.join(COMPLETE_DIR, job["category"] or "", job["name"])
    links, total = [], 0
    for rel, candidates, size in files:
        src = next((p for p in candidates if os.path.exists(p)), None)
        if not src:
            return False  # mount does not list the file yet (directory cache), retry later
        links.append((src, os.path.join(target_dir, os.path.basename(rel))))
        total += size
    os.makedirs(target_dir, exist_ok=True)
    for src, dst in links:
        if os.path.islink(dst) or os.path.exists(dst):
            os.remove(dst)
        os.symlink(src, dst)
        os.lchown(dst, UID, GID)
    os.chown(target_dir, UID, GID)
    set_job(job["nzo_id"], status="Completed", storage=target_dir, bytes=total, progress=1.0, finished=time.time(), message="")
    log.info("Completed %s -> %s (%d file(s))", job["nzo_id"], target_dir, len(links))
    return True


def worker():
    while True:
        try:
            with DB_LOCK, db() as c:
                jobs = [dict(r) for r in c.execute("select * from jobs where status in ('Queued','Downloading','Processing')")]
            # check free TorBox slots once per pass, and only if something needs to be submitted
            free = None
            if any(not j["torbox_id"] for j in jobs):
                try:
                    free = MAX_ACTIVE - active_downloads()
                except TorBoxBusy:
                    free = 0
            for job in jobs:
                try:
                    if not job["torbox_id"]:
                        if free is not None and free <= 0:
                            if job["message"] != "Waiting for a free TorBox slot":
                                set_job(job["nzo_id"], message="Waiting for a free TorBox slot")
                            continue
                        try:
                            tid = submit_nzb(job["name"], open(job["nzb_file"], "rb").read())
                        except TorBoxBusy as e:
                            free = 0  # slots full / hourly budget / 429 -> stay queued, retry later
                            set_job(job["nzo_id"], message=f"Waiting: {e}"[:300])
                            continue
                        free = (free or 1) - 1
                        set_job(job["nzo_id"], torbox_id=tid, status="Downloading", submitted=time.time(), message="")
                        log.info("Submitted %s to TorBox (id %s)", job["nzo_id"], tid)
                        continue
                    item = torbox_item(job["torbox_id"])
                    if not item:
                        continue
                    state = (item.get("download_state") or "").lower()
                    set_job(job["nzo_id"], progress=float(item.get("progress") or 0), bytes=int(item.get("size") or 0))
                    if "fail" in state or "error" in state:
                        raise RuntimeError(f"TorBox reports: {item.get('download_state')}")
                    if item.get("download_present"):
                        set_job(job["nzo_id"], status="Processing")
                        finalize(job, item)
                    elif state in ("processing", "uploading", "completed") or item.get("download_finished"):
                        set_job(job["nzo_id"], status="Processing")
                    if time.time() - (job["submitted"] or job["added"]) > JOB_TIMEOUT:
                        raise RuntimeError("Timed out")
                except TorBoxBusy:
                    continue  # temporary (429) -> next pass, not a failure
                except Exception as e:  # one broken job must not block the others
                    log.warning("Job %s failed: %s", job["nzo_id"], e)
                    set_job(job["nzo_id"], status="Failed", message=str(e)[:300], finished=time.time())
        except Exception:
            log.exception("Worker error")
        time.sleep(POLL_SECONDS)


def repair_loop():
    """Check library symlinks into the mount; resubmit the stored NZB when a target disappeared."""
    while True:
        time.sleep(REPAIR_SECONDS)
        try:
            broken = {}
            for root in LIBRARY_DIRS:
                for dp, _, fns in os.walk(root):
                    for fn in fns:
                        p = os.path.join(dp, fn)
                        if os.path.islink(p) and os.readlink(p).startswith(MOUNT_DIR + "/") and not os.path.exists(p):
                            broken[os.readlink(p)[len(MOUNT_DIR) + 1:].split("/")[0]] = p
            if not broken:
                continue
            with DB_LOCK, db() as c:
                done = [dict(r) for r in c.execute("select * from jobs where status='Completed'")]
            for job in done:
                item_name = None
                for dp, _, fns in os.walk(job["storage"] or "/nonexistent"):
                    for fn in fns:
                        lp = os.path.join(dp, fn)
                        if os.path.islink(lp):
                            item_name = os.readlink(lp)[len(MOUNT_DIR) + 1:].split("/")[0]
                if item_name in broken and os.path.exists(job["nzb_file"]):
                    with DB_LOCK, db() as c:
                        last = c.execute("select at from repairs where item=?", (item_name,)).fetchone()
                    if last and time.time() - last[0] < 24 * 3600:
                        continue  # at most once per day (TorBox treats constant re-adds as abuse)
                    try:
                        tid = submit_nzb(job["name"], open(job["nzb_file"], "rb").read())
                    except TorBoxBusy:
                        break
                    with DB_LOCK, db() as c:
                        c.execute("insert or replace into repairs (item, at) values (?,?)", (item_name, time.time()))
                    log.info("Repair: resubmitted %s to TorBox (id %s)", item_name, tid)
        except Exception:
            log.exception("Repair error")


# ---------------------------------------------------------------- Cleanup (optional)
def used_names():
    """Names of all TorBox downloads that a library symlink points to (MOUNT_DIR and EXTRA_LINK_ROOTS)."""
    names = set()
    for root in LIBRARY_DIRS:
        for dp, _, fns in os.walk(root):
            for fn in fns:
                p = os.path.join(dp, fn)
                if not os.path.islink(p):
                    continue
                target = os.readlink(p)
                for base in [MOUNT_DIR] + EXTRA_LINK_ROOTS:
                    if target.startswith(base + "/"):
                        name = target[len(base) + 1:].split("/")[0]
                        names.update({name, os.path.splitext(name)[0]})
    return names


def cleanup_candidates():
    """TorBox downloads no library file uses anymore, that are finished/failed and old enough."""
    used = used_names()
    if not used:
        return []  # empty library or missing mount -> rather delete nothing
    with DB_LOCK, db() as c:
        active = {r[0] for r in c.execute("select torbox_id from jobs where status in ('Queued','Downloading','Processing')")}
    out = []
    for kind, id_field in (("usenet", "usenet_id"), ("torrents", "torrent_id")):
        for d in torbox_list(kind):
            name = d.get("name") or ""
            created = time.mktime(time.strptime((d.get("created_at") or "")[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            done = d.get("download_finished") or d.get("download_present") or "fail" in (d.get("download_state") or "").lower()
            if (name in used or os.path.splitext(name)[0] in used or d.get("id") in active or not done
                    or time.time() - created < CLEANUP_MIN_AGE):
                continue
            out.append((kind, id_field, d["id"], name))
    return out


def cleanup_loop():
    """Remove rejected/replaced/broken downloads from TorBox so only the version in use remains."""
    while CLEANUP_ENABLED:
        try:
            for kind, id_field, tid, name in cleanup_candidates():
                if CLEANUP_DRY_RUN:
                    log.info("Cleanup (dry run): would delete %s %s: %s", kind, tid, name)
                    continue
                path = "/usenet/controlusenetdownload" if kind == "usenet" else "/torrents/controltorrent"
                r = torbox("POST", path, body={id_field: tid, "operation": "delete"})
                log.info("Cleanup: deleted %s %s (%s): %s", kind, tid, "ok" if r.get("success") else r.get("detail"), name)
        except Exception:
            log.exception("Cleanup error")
        time.sleep(CLEANUP_SECONDS)


# ---------------------------------------------------------------- Audio language guard
def arr(base, key, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + "/api/v3" + path, data=data, method=method,
                                 headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def lacks_language(title, langs):
    return bool(GUARD_RELEASE.search(title) and langs and not REQUIRED.search(langs))


def reject_file(name, base, key, file_id, file_kind, history_query, label):
    """Mark the grab as failed (blocklist + new search) and remove the file."""
    hist = (arr(base, key, "GET", history_query) or {})
    records = hist.get("records", hist) if isinstance(hist, dict) else hist
    grabs = [h for h in records or [] if h.get("eventType") == "grabbed"]
    if grabs:
        arr(base, key, "POST", f"/history/failed/{grabs[0]['id']}")
    arr(base, key, "DELETE", f"/{file_kind}/{file_id}")
    log.info("Language guard [%s]: %s has none of %s -> blocklisted, removed, searching again", name, label, LANGS)


def unstick_imports(name, base, key):
    """Large files on the TorBox mount can make Radarr/Sonarr fail to read the runtime in time
    ("Unable to determine if file is a sample") -> they stay in importPending -> import them manually."""
    radarr = name.lower().startswith("radarr")
    for r in (arr(base, key, "GET", "/queue?pageSize=200") or {}).get("records", []):
        msgs = " ".join(m for s in r.get("statusMessages") or [] for m in s.get("messages") or [])
        if r.get("trackedDownloadState") != "importPending" or "sample" not in msgs.lower():
            continue
        qs = urllib.parse.urlencode({"downloadId": r["downloadId"], "folder": r.get("outputPath") or "", "filterExistingFiles": "true"})
        files = []
        for c in arr(base, key, "GET", "/manualimport?" + qs) or []:
            item = {"path": c["path"], "quality": c["quality"], "languages": c.get("languages") or [],
                    "releaseGroup": c.get("releaseGroup"), "downloadId": r["downloadId"], "indexerFlags": c.get("indexerFlags", 0)}
            if radarr:
                item["movieId"] = r["movieId"]
            else:
                eps = [e["id"] for e in c.get("episodes") or []]
                if not eps or not c.get("series"):
                    continue
                item.update(seriesId=c["series"]["id"], episodeIds=eps)
            files.append(item)
        if files:
            arr(base, key, "POST", "/command", {"name": "ManualImport", "files": files, "importMode": "auto"})
            log.info("Stuck import [%s]: %s imported manually (%d file(s))", name, r.get("title"), len(files))


def language_guard():
    while ARRS:
        for name, base, key in ARRS:
            try:
                unstick_imports(name, base, key)
            except Exception:
                log.exception("Stuck-import check failed for %s", name)
            if not REQUIRED:
                continue
            try:
                if name.lower().startswith("radarr"):
                    for m in arr(base, key, "GET", "/movie"):
                        f = m.get("movieFile")
                        if not f:
                            continue
                        title = f.get("sceneName") or f.get("relativePath") or ""
                        langs = (f.get("mediaInfo") or {}).get("audioLanguages") or ""
                        if lacks_language(title, langs):
                            reject_file(name, base, key, f["id"], "moviefile",
                                        f"/history/movie?movieId={m['id']}", f"{m['title']} ({langs})")
                else:
                    for s in arr(base, key, "GET", "/series"):
                        for f in arr(base, key, "GET", f"/episodefile?seriesId={s['id']}"):
                            title = f.get("sceneName") or f.get("relativePath") or ""
                            langs = (f.get("mediaInfo") or {}).get("audioLanguages") or ""
                            if lacks_language(title, langs):
                                eps = arr(base, key, "GET", f"/episode?episodeFileId={f['id']}") or []
                                q = f"/history?episodeId={eps[0]['id']}&pageSize=20&sortKey=date&sortDirection=descending" if eps else "/history?pageSize=1"
                                reject_file(name, base, key, f["id"], "episodefile", q, f"{s['title']} {title} ({langs})")
            except Exception:
                log.exception("Language guard error for %s", name)
        if GUARD_WAKE.wait(GUARD_SECONDS):
            GUARD_WAKE.clear()
            time.sleep(20)  # Radarr/Sonarr need a moment after the import to read mediaInfo (audio tracks)


# ---------------------------------------------------------------- SABnzbd API
def slot_queue(j):
    mb = (j["bytes"] or 0) / 1048576
    return {"nzo_id": j["nzo_id"], "filename": j["name"], "cat": j["category"], "mb": f"{mb:.2f}",
            "mbleft": f"{mb * (1 - (j['progress'] or 0)):.2f}", "percentage": str(int((j["progress"] or 0) * 100)),
            "status": "Downloading" if j["status"] != "Queued" else "Queued", "timeleft": "0:10:00", "priority": "Normal"}


def slot_history(j):
    return {"nzo_id": j["nzo_id"], "name": j["name"], "nzb_name": j["name"] + ".nzb", "category": j["category"],
            "status": j["status"], "storage": j["storage"] or "", "bytes": j["bytes"] or 0,
            "fail_message": j["message"] or "", "download_time": int((j["finished"] or time.time()) - (j["added"] or time.time())),
            "completed": int(j["finished"] or 0)}


def sab_config():
    cats = [{"name": "*", "dir": "", "order": 0, "pp": "3", "script": "None", "priority": -100}]
    cats += [{"name": c, "dir": c, "order": i + 1, "pp": "3", "script": "None", "priority": -100} for i, c in enumerate(CATEGORIES)]
    return {"config": {"misc": {"complete_dir": COMPLETE_DIR, "download_dir": COMPLETE_DIR, "pre_check": False,
                                "enable_tv_sorting": False, "tv_categories": [], "enable_movie_sorting": False,
                                "movie_categories": [], "enable_date_sorting": False, "date_categories": [],
                                "history_retention": "", "history_retention_option": "all", "history_retention_number": 0},
                       "categories": cats, "servers": [], "sorters": []}}


class Handler(BaseHTTPRequestHandler):
    server_version = f"torbox-usenet-bridge/{__version__}"

    def log_message(self, fmt, *args):
        pass

    def reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def params(self):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        files = {}
        if self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/form-data"):
                msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
                for part in msg.iter_parts():
                    nm = part.get_param("name", header="content-disposition")
                    if part.get_filename():
                        files[nm] = (part.get_filename(), part.get_payload(decode=True))
                    else:
                        q[nm] = part.get_payload(decode=True).decode(errors="replace")
            elif raw:
                q.update(dict(urllib.parse.parse_qsl(raw.decode(errors="replace"))))
        return q, files

    def handle_api(self):
        url = urllib.parse.urlparse(self.path)
        if url.path.rstrip("/") == "/guard":  # Radarr/Sonarr webhook (Connect -> Webhook, "On Import")
            if dict(urllib.parse.parse_qsl(url.query)).get("apikey") != SAB_KEY:
                return self.reply({"status": False}, 403)
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            GUARD_WAKE.set()
            return self.reply({"status": True})
        if not url.path.rstrip("/").endswith("/api"):
            return self.reply({"error": "not found"}, 404)
        q, files = self.params()
        mode = q.get("mode", "")
        if mode == "version":
            return self.reply({"version": "4.3.3"})
        if q.get("apikey") != SAB_KEY:
            return self.reply({"status": False, "error": "API Key Incorrect"}, 403)
        with DB_LOCK, db() as c:
            jobs = [dict(r) for r in c.execute("select * from jobs order by added desc")]
        if mode == "get_config":
            return self.reply(sab_config())
        if mode == "get_cats":
            return self.reply({"categories": ["*"] + CATEGORIES})
        if mode in ("fullstatus", "status"):
            return self.reply({"status": {"completedir": COMPLETE_DIR, "warnings": [], "paused": False}})
        if mode == "warnings":
            return self.reply({"warnings": []})
        if mode == "queue":
            if q.get("name") == "delete":
                return self.delete(q.get("value", ""))
            if q.get("name") in ("pause", "resume"):
                return self.reply({"status": True})
            slots = [slot_queue(j) for j in jobs if j["status"] in ("Queued", "Downloading", "Processing")]
            return self.reply({"queue": {"paused": False, "slots": slots, "noofslots": len(slots), "status": "Downloading" if slots else "Idle",
                                         "speedlimit": "", "speedlimit_abs": "", "kbpersec": "0", "mb": "0", "mbleft": "0"}})
        if mode == "history":
            if q.get("name") == "delete":
                return self.delete(q.get("value", ""))
            slots = [slot_history(j) for j in jobs if j["status"] in ("Completed", "Failed")]
            limit = int(q.get("limit") or len(slots) or 1)
            return self.reply({"history": {"slots": slots[:limit], "noofslots": len(slots)}})
        if mode == "addfile":
            f = files.get("name") or files.get("nzbfile") or next(iter(files.values()), None)
            if not f:
                return self.reply({"status": False, "error": "no file"}, 400)
            nzo = add_job(q.get("nzbname") or f[0], q.get("cat", ""), f[1])
            return self.reply({"status": True, "nzo_ids": [nzo]})
        if mode == "addurl":
            url = q.get("name", "")
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=120) as r:
                    content = r.read()
            except Exception as e:
                return self.reply({"status": False, "error": f"Fetching NZB failed: {e}"}, 400)
            nzo = add_job(q.get("nzbname") or os.path.basename(urllib.parse.urlparse(url).path) or "download", q.get("cat", ""), content)
            return self.reply({"status": True, "nzo_ids": [nzo]})
        return self.reply({"status": True})

    def delete(self, value):
        ids = [v for v in value.split(",") if v]
        with DB_LOCK, db() as c:
            for i in ids:
                c.execute("delete from jobs where nzo_id=?", (i,))
        return self.reply({"status": True, "nzo_ids": ids})

    do_GET = handle_api
    do_POST = handle_api


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=repair_loop, daemon=True).start()
    threading.Thread(target=language_guard, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    log.info("torbox-usenet-bridge %s listening on :%d (mount %s, complete dir %s, language guard %s, cleanup %s)",
             __version__, PORT, MOUNT_DIR, COMPLETE_DIR, LANGS if REQUIRED and ARRS else "off",
             ("dry run" if CLEANUP_DRY_RUN else "on") if CLEANUP_ENABLED else "off")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
