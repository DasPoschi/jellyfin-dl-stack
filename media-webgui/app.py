#!/usr/bin/env python3
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import paramiko
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from myjdapi import Myjdapi

# ============================================================
# Config from environment
# ============================================================
MYJD_EMAIL    = os.environ.get("MYJD_EMAIL", "")
MYJD_PASSWORD = os.environ.get("MYJD_PASSWORD", "")
MYJD_DEVICE   = os.environ.get("MYJD_DEVICE", "")

JELLYFIN_HOST    = os.environ.get("JELLYFIN_HOST", "192.168.1.1")
JELLYFIN_PORT    = int(os.environ.get("JELLYFIN_PORT", "22"))
JELLYFIN_USER    = os.environ.get("JELLYFIN_USER", "")
JELLYFIN_SSH_KEY = os.environ.get("JELLYFIN_SSH_KEY", "/ssh/id_ed25519")

JELLYFIN_MOVIES_DIR = os.environ.get("JELLYFIN_MOVIES_DIR", "").rstrip("/")
JELLYFIN_SERIES_DIR = os.environ.get("JELLYFIN_SERIES_DIR", "").rstrip("/")
JELLYFIN_DEST_DIR   = os.environ.get("JELLYFIN_DEST_DIR", "/jellyfin/Filme").rstrip("/")

JELLYFIN_API_BASE        = os.environ.get("JELLYFIN_API_BASE", "").rstrip("/")
JELLYFIN_API_KEY         = os.environ.get("JELLYFIN_API_KEY", "")
JELLYFIN_LIBRARY_REFRESH = os.environ.get("JELLYFIN_LIBRARY_REFRESH", "false").lower() == "true"

TMDB_API_KEY  = os.environ.get("TMDB_API_KEY", "")
TMDB_LANGUAGE = os.environ.get("TMDB_LANGUAGE", "de-DE")

CREATE_MOVIE_FOLDER   = os.environ.get("CREATE_MOVIE_FOLDER",   "true").lower() == "true"
CREATE_SERIES_FOLDERS = os.environ.get("CREATE_SERIES_FOLDERS", "true").lower() == "true"

MD5_DIR = os.environ.get("MD5_DIR", "/md5").rstrip("/")

BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "")

POLL_SECONDS      = float(os.environ.get("POLL_SECONDS", "5"))
MIN_VIDEO_SIZE_MB = int(os.environ.get("MIN_VIDEO_SIZE_MB", "200"))
MIN_VIDEO_BYTES   = MIN_VIDEO_SIZE_MB * 1024 * 1024

# JDownloader writes here inside container
JD_OUTPUT_PATH    = "/output"
PROXY_EXPORT_PATH = os.environ.get("PROXY_EXPORT_PATH", "/output/jd-proxies.jdproxies")
LOG_BUFFER_LIMIT  = int(os.environ.get("LOG_BUFFER_LIMIT", "500"))

URL_RE = re.compile(r"^https?://", re.I)

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".ogv",
    ".3gp", ".3g2",
}
IGNORE_EXTS = {".part", ".tmp", ".crdownload"}

SERIES_RE = re.compile(r"(?:^|[^a-z0-9])S(\d{1,2})E(\d{1,2})(?:[^a-z0-9]|$)", re.IGNORECASE)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# ============================================================
# Basic Auth (optional)
# ============================================================
def _auth_enabled() -> bool:
    return bool(BASIC_AUTH_USER and BASIC_AUTH_PASS)

def _check_basic_auth(req: Request) -> bool:
    if not _auth_enabled():
        return True
    auth = req.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
        user, _, pw = decoded.partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user, BASIC_AUTH_USER) and hmac.compare_digest(pw, BASIC_AUTH_PASS)

@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if not _check_basic_auth(request):
        return HTMLResponse(
            content="Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="jd-webgui"'},
        )
    return await call_next(request)

# ============================================================
# Logging
# ============================================================
_log_lock = threading.Lock()
_conn_log: list[str] = []

def log_connection(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    with _log_lock:
        _conn_log.append(entry)
        if len(_conn_log) > LOG_BUFFER_LIMIT:
            _conn_log.pop(0)

# ============================================================
# SSRF protection
# ============================================================
def _is_ssrf_target(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            try:
                host = socket.gethostbyname(host)
                addr = ipaddress.ip_address(host)
            except Exception:
                return False
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except Exception:
        return False

# ============================================================
# No-proxy opener (bypasses any system proxy)
# ============================================================
NO_PROXY_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)

def check_url_reachable(url: str) -> Optional[str]:
    if _is_ssrf_target(url):
        return "URL zeigt auf eine interne/private Adresse (nicht erlaubt)"
    try:
        req = urllib.request.Request(url, method="HEAD")
        with NO_PROXY_OPENER.open(req, timeout=10) as resp:
            _ = resp.status
        return None
    except urllib.error.HTTPError as e:
        if e.code < 500:
            return None
        return f"HTTP {e.code}"
    except Exception as e:
        return str(e)

# ============================================================
# Proxy list fetching with cache + size limit
# ============================================================
_PROXY_FETCH_LIMIT = 2 * 1024 * 1024   # 2 MB cap
_proxy_cache: Dict[str, Tuple[float, str]] = {}
_PROXY_CACHE_TTL = 300.0                # 5 minutes

def fetch_proxy_list(url: str) -> str:
    now = time.time()
    cached_ts, cached_text = _proxy_cache.get(url, (0.0, ""))
    if cached_text and now - cached_ts < _PROXY_CACHE_TTL:
        return cached_text
    req = urllib.request.Request(url)
    log_connection(f"HTTP GET {url} (no-proxy)")
    with NO_PROXY_OPENER.open(req, timeout=20) as resp:
        text = resp.read(_PROXY_FETCH_LIMIT).decode("utf-8", "replace")
    if "\n" not in text and re.search(r"\s", text):
        text = re.sub(r"\s+", "\n", text.strip())
    _proxy_cache[url] = (now, text)
    return text

# ============================================================
# Job state
# ============================================================
lock = threading.Lock()
jobs: Dict[str, "Job"] = {}

@dataclass
class Job:
    id: str
    url: str
    package_name: str
    library: str
    status: str = "queued"
    message: str = ""
    progress: float = 0.0
    cancel_requested: bool = False

# ============================================================
# Core helpers
# ============================================================
def ensure_env():
    missing = []
    for k, v in [
        ("MYJD_EMAIL",       MYJD_EMAIL),
        ("MYJD_PASSWORD",    MYJD_PASSWORD),
        ("JELLYFIN_USER",    JELLYFIN_USER),
        ("JELLYFIN_SSH_KEY", JELLYFIN_SSH_KEY),
    ]:
        if not v:
            missing.append(k)

    if not (JELLYFIN_DEST_DIR or (JELLYFIN_MOVIES_DIR and JELLYFIN_SERIES_DIR)):
        missing.append("JELLYFIN_DEST_DIR or (JELLYFIN_MOVIES_DIR+JELLYFIN_SERIES_DIR)")

    if JELLYFIN_LIBRARY_REFRESH and not (JELLYFIN_API_BASE and JELLYFIN_API_KEY):
        missing.append("JELLYFIN_API_BASE+JELLYFIN_API_KEY (required when JELLYFIN_LIBRARY_REFRESH=true)")

    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))

    # Validate SSH key path
    key = JELLYFIN_SSH_KEY
    if os.path.isdir(key):
        raise RuntimeError(
            f"JELLYFIN_SSH_KEY '{key}' ist ein Verzeichnis, keine Datei. "
            "Pruefe den SSH_KEY_PATH in Dockhand: er muss auf die Schluessel-DATEI zeigen "
            "(z. B. /root/.ssh/id_ed25519), nicht auf ein Verzeichnis."
        )
    if not os.path.isfile(key):
        raise RuntimeError(
            f"JELLYFIN_SSH_KEY '{key}' existiert nicht im Container. "
            "Pruefe den SSH_KEY_PATH in Dockhand und ob die Datei auf dem Host vorhanden ist."
        )

def get_device():
    jd = Myjdapi()
    log_connection(f"MyJDownloader connect as {MYJD_EMAIL or 'unknown'}")
    jd.connect(MYJD_EMAIL, MYJD_PASSWORD)

    wanted = (MYJD_DEVICE or "").strip()

    deadline = time.time() + 30
    last = None
    while time.time() < deadline:
        devs = jd.list_devices() or []
        last = devs

        def pick():
            if wanted:
                for d in devs:
                    if (d.get("name") or "") == wanted:
                        return d
            return devs[0] if devs else None

        chosen = pick()
        if chosen and chosen.get("status", "").upper() in ("ONLINE", "UNKNOWN"):
            return jd.get_device(device_name=chosen.get("name"))
        time.sleep(2)

    raise RuntimeError(
        f"Kein JDownloader-Geraet gefunden/online. "
        f"Gesucht: '{wanted or 'beliebig'}'. Gefunden: {last}"
    )

SSH_KNOWN_HOSTS = os.environ.get("SSH_KNOWN_HOSTS", "/ssh/known_hosts")

def ssh_connect() -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    if os.path.isfile(SSH_KNOWN_HOSTS):
        ssh.load_host_keys(SSH_KNOWN_HOSTS)
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        log_connection(f"SSH connect {JELLYFIN_USER}@{JELLYFIN_HOST}:{JELLYFIN_PORT} (known_hosts verified)")
    else:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        log_connection(f"SSH connect {JELLYFIN_USER}@{JELLYFIN_HOST}:{JELLYFIN_PORT} (WARNING: no known_hosts, accepting any host key)")
    ssh.connect(
        hostname=JELLYFIN_HOST,
        port=JELLYFIN_PORT,
        username=JELLYFIN_USER,
        key_filename=JELLYFIN_SSH_KEY,
        timeout=30,
    )
    return ssh

def sftp_mkdirs(sftp: paramiko.SFTPClient, remote_dir: str):
    parts = [p for p in remote_dir.split("/") if p]
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)

def sftp_upload(ssh: paramiko.SSHClient, local_path: str, remote_path: str):
    sftp = ssh.open_sftp()
    try:
        remote_dir = "/".join(remote_path.split("/")[:-1])
        if remote_dir:
            sftp_mkdirs(sftp, remote_dir)
        sftp.put(local_path, remote_path)
        log_connection(f"SFTP upload {os.path.basename(local_path)} -> {remote_path}")
    finally:
        sftp.close()

def remote_md5sum(ssh: paramiko.SSHClient, remote_path: str) -> str:
    _, stdout, stderr = ssh.exec_command(f"md5sum {remote_path!r}")
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if not out:
        raise RuntimeError(f"md5sum on remote failed: {err}")
    return out.split()[0]

def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def write_md5_sidecar(video_path: str, md5_hex: str) -> str:
    sidecar = os.path.join(MD5_DIR, os.path.basename(video_path) + ".md5")
    os.makedirs(MD5_DIR, exist_ok=True)
    with open(sidecar, "w") as f:
        f.write(f"{md5_hex}  {os.path.basename(video_path)}\n")
    return sidecar

def sanitize_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Unknown"

def ffprobe_ok(path: str) -> bool:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False

def is_video_file(path: str) -> bool:
    name = os.path.basename(path).lower()
    _, ext = os.path.splitext(name)
    if ext in IGNORE_EXTS:
        return False
    return ext in VIDEO_EXTS

DEMO_PATTERNS = {"big_buck_bunny", "bigbuckbunny", "big buck bunny", "bbb_sunflower"}

def is_demo_link(name: str) -> bool:
    lower = name.lower().replace("-", "_").replace(".", " ")
    return any(p in lower for p in DEMO_PATTERNS)

# ============================================================
# TMDB helpers
# ============================================================
def tmdb_request(path: str, params: Dict[str, str]) -> Any:
    params = {**params, "api_key": TMDB_API_KEY, "language": TMDB_LANGUAGE}
    qs = urllib.parse.urlencode(params)
    url = f"https://api.themoviedb.org/3{path}?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with NO_PROXY_OPENER.open(req, timeout=10) as resp:
        return json.loads(resp.read())

def tmdb_search_movie(query: str) -> Optional[Dict[str, Any]]:
    if not TMDB_API_KEY or not query:
        return None
    try:
        data = tmdb_request("/search/movie", {"query": query})
        results = data.get("results") or []
        return results[0] if results else None
    except Exception:
        return None

def tmdb_search_tv(query: str) -> Optional[Dict[str, Any]]:
    if not TMDB_API_KEY or not query:
        return None
    try:
        data = tmdb_request("/search/tv", {"query": query})
        results = data.get("results") or []
        return results[0] if results else None
    except Exception:
        return None

# ============================================================
# Library / path helpers
# ============================================================
def pick_library_target(library_choice: str, filename: str, package_name: str) -> str:
    if library_choice == "movies":
        return JELLYFIN_MOVIES_DIR or JELLYFIN_DEST_DIR
    if library_choice == "series":
        return JELLYFIN_SERIES_DIR or JELLYFIN_DEST_DIR
    if SERIES_RE.search(filename) or SERIES_RE.search(package_name or ""):
        return JELLYFIN_SERIES_DIR or JELLYFIN_DEST_DIR
    return JELLYFIN_MOVIES_DIR or JELLYFIN_DEST_DIR

def build_remote_paths(job_library: str, package_name: str, local_file: str) -> Tuple[str, str]:
    filename    = os.path.basename(local_file)
    base_target = pick_library_target(job_library, filename, package_name)

    m         = SERIES_RE.search(filename) or SERIES_RE.search(package_name or "")
    is_series = (job_library == "series") or (job_library == "auto" and m)

    if is_series:
        show_query = package_name or os.path.splitext(filename)[0]
        tv         = tmdb_search_tv(show_query) if TMDB_API_KEY else None
        show_name  = sanitize_name(tv["name"]) if tv and tv.get("name") else sanitize_name(show_query)

        season  = int(m.group(1)) if m else 1
        episode = int(m.group(2)) if m else 1

        if CREATE_SERIES_FOLDERS:
            remote_dir = f"{base_target}/{show_name}/Season {season:02d}"
        else:
            remote_dir = base_target

        ext             = os.path.splitext(filename)[1]
        remote_filename = f"{show_name} - S{season:02d}E{episode:02d}{ext}"
        return remote_dir, remote_filename

    movie_query = package_name or os.path.splitext(filename)[0]
    mv          = tmdb_search_movie(movie_query) if TMDB_API_KEY else None
    title       = mv.get("title") if mv else None
    date        = mv.get("release_date") if mv else None
    year        = date[:4] if isinstance(date, str) and len(date) >= 4 else None

    title_safe = sanitize_name(title) if title else sanitize_name(movie_query)
    year_safe  = year if year else ""

    if CREATE_MOVIE_FOLDER:
        folder     = f"{title_safe} ({year_safe})".strip() if year_safe else title_safe
        remote_dir = f"{base_target}/{folder}"
    else:
        remote_dir = base_target

    ext             = os.path.splitext(filename)[1]
    remote_filename = f"{title_safe} ({year_safe}){ext}".strip() if year_safe else f"{title_safe}{ext}"
    return remote_dir, remote_filename

# ============================================================
# Jellyfin refresh (optional)
# ============================================================
def jellyfin_refresh_library():
    if not (JELLYFIN_API_BASE and JELLYFIN_API_KEY):
        return
    headers = {"X-MediaBrowser-Token": JELLYFIN_API_KEY}
    for path in ("/Library/Refresh", "/library/refresh"):
        try:
            req = urllib.request.Request(
                JELLYFIN_API_BASE + path,
                method="POST",
                headers=headers,
            )
            with NO_PROXY_OPENER.open(req, timeout=15):
                pass
            log_connection(f"Jellyfin library refresh triggered via {path}")
            return
        except Exception as e:
            log_connection(f"Jellyfin refresh {path} failed: {e}")

# ============================================================
# JD cancel / cleanup helpers
# ============================================================
def call_raw_jd_api(dev, endpoints: List[str], payloads: List[Dict[str, Any]]) -> bool:
    method_candidates = ["action", "call", "api", "request"]
    for method_name in method_candidates:
        method = getattr(dev, method_name, None)
        if method is None:
            continue
        for ep, pl in zip(endpoints, payloads):
            try:
                method(ep, pl)
                return True
            except Exception:
                pass
    return False

def cancel_job(dev, jobid: str) -> str:
    # Remove from download list
    links, pkg_map = query_links_and_packages(dev, jobid)
    link_ids = [l.get("uuid") for l in links if l.get("uuid") is not None]
    pkg_ids  = list(pkg_map)
    for ep, pl in [
        ("downloads/remove_links",          {"linkIds": link_ids, "packageIds": []}),
        ("downloadcontroller/remove_links", {"linkIds": link_ids, "packageIds": []}),
        ("downloads/remove_links",          {"linkIds": [], "packageIds": pkg_ids}),
        ("downloadcontroller/remove_links", {"linkIds": [], "packageIds": pkg_ids}),
    ]:
        try:
            call_raw_jd_api(dev, [ep], [pl])
        except Exception:
            pass

    # Remove from linkgrabber
    try:
        lg_links = dev.linkgrabber.query_links([{
            "jobUUIDs":    [int(jobid)] if str(jobid).isdigit() else [jobid],
            "maxResults":  -1,
            "startAt":     0,
            "uuid":        True,
            "packageUUID": True,
        }]) or []
        lg_link_ids = [l.get("uuid") for l in lg_links if l.get("uuid") is not None]
        lg_pkg_ids  = list({l.get("packageUUID") for l in lg_links if l.get("packageUUID") is not None})
        if lg_link_ids or lg_pkg_ids:
            dev.linkgrabber.remove_links(link_ids=lg_link_ids, package_ids=lg_pkg_ids)
    except Exception:
        pass

    return "Download abgebrochen."

def try_remove_from_jd(dev, links: List[Dict[str, Any]], pkg_map: Dict[Any, Dict[str, Any]]) -> str:
    link_ids = [l.get("uuid") for l in links if l.get("uuid") is not None]
    pkg_ids  = list(pkg_map.keys())
    try:
        dev.downloads.remove_links(link_ids=link_ids, package_ids=pkg_ids)
        return "JDownloader: Paket/Links entfernt."
    except Exception:
        pass
    try:
        call_raw_jd_api(
            dev,
            ["downloads/remove_links", "downloadcontroller/remove_links"],
            [{"linkIds": link_ids, "packageIds": pkg_ids}] * 2,
        )
        return "JDownloader: Paket/Links entfernt (raw API)."
    except Exception as e:
        return f"JDownloader-Cleanup fehlgeschlagen: {e}"

# ============================================================
# Download monitoring helpers
# ============================================================
def query_links_and_packages(dev, jobid: str) -> Tuple[List[Dict[str, Any]], Dict[Any, Dict[str, Any]]]:
    links = dev.downloads.query_links([{
        "jobUUIDs":    [int(jobid)] if jobid.isdigit() else [jobid],
        "maxResults":  -1,
        "startAt":     0,
        "name":        True,
        "finished":    True,
        "running":     True,
        "bytesLoaded": True,
        "bytesTotal":  True,
        "bytes":       True,
        "totalBytes":  True,
        "status":      True,
        "packageUUID": True,
        "uuid":        True,
    }])

    pkg_ids = sorted({l.get("packageUUID") for l in links if l.get("packageUUID") is not None})
    pkgs = dev.downloads.query_packages([{
        "packageUUIDs": pkg_ids,
        "maxResults":   -1,
        "startAt":      0,
        "saveTo":       True,
        "uuid":         True,
        "finished":     True,
        "running":      True,
    }]) if pkg_ids else []
    pkg_map = {p.get("uuid"): p for p in pkgs}
    return links, pkg_map

def local_paths_from_links(links: List[Dict[str, Any]], pkg_map: Dict[Any, Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    for l in links:
        name = l.get("name")
        if not name:
            continue
        pkg     = pkg_map.get(l.get("packageUUID"))
        save_to = pkg.get("saveTo") if pkg else None
        base    = save_to if isinstance(save_to, str) else JD_OUTPUT_PATH
        paths.append(os.path.join(base, name))

    out, seen = [], set()
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

def calculate_progress(links: List[Dict[str, Any]]) -> float:
    total  = 0
    loaded = 0
    for l in links:
        if l.get("finished"):
            bytes_total = l.get("bytesTotal") or l.get("totalBytes") or 0
            total  += bytes_total
            loaded += bytes_total
            continue
        bytes_total  = l.get("bytesTotal")  or l.get("totalBytes")  or 0
        bytes_loaded = l.get("bytesLoaded") or l.get("bytes")       or 0
        total  += bytes_total
        loaded += min(bytes_loaded, bytes_total)
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (loaded / total) * 100.0))

# ============================================================
# Linkgrabber filter (pre-download)
# ============================================================
def _filter_linkgrabber(dev, jobid: str) -> Tuple[int, int]:
    """Wait for link crawler, then remove non-video and sub-minimum-size links.
    Returns (accepted, rejected) counts."""
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            crawlers = dev.linkgrabber.query_link_crawlers([{"collectorInfo": True}]) or []
            if not any(c.get("crawling") for c in crawlers):
                break
        except Exception:
            break
        time.sleep(2)

    links = []
    try:
        links = dev.linkgrabber.query_links([{
            "jobUUIDs":    [int(jobid)] if str(jobid).isdigit() else [jobid],
            "maxResults":  -1,
            "startAt":     0,
            "name":        True,
            "size":        True,
            "uuid":        True,
            "packageUUID": True,
        }]) or []
    except Exception:
        pass

    to_remove_ids = []
    keep_ids      = []
    keep_pkg_ids  = set()
    for link in links:
        name       = link.get("name", "")
        size       = link.get("size", -1)
        _, ext     = os.path.splitext(name.lower())
        is_video   = ext in VIDEO_EXTS
        big_enough = size < 0 or size >= MIN_VIDEO_BYTES
        if is_video and big_enough:
            keep_ids.append(link.get("uuid"))
            if link.get("packageUUID") is not None:
                keep_pkg_ids.add(link.get("packageUUID"))
        else:
            to_remove_ids.append(link.get("uuid"))

    if to_remove_ids:
        try:
            dev.linkgrabber.remove_links(link_ids=to_remove_ids, package_ids=[])
        except Exception:
            pass

    if keep_ids:
        try:
            dev.linkgrabber.move_to_downloadlist(
                link_ids=keep_ids,
                package_ids=list(keep_pkg_ids),
            )
        except Exception:
            pass

    return len(keep_ids), len(to_remove_ids)


# ============================================================
# Worker
# ============================================================
def worker(jobid: str):
    try:
        ensure_env()
        dev = get_device()

        # Filter linkgrabber: keep only video files >= MIN_VIDEO_SIZE_MB
        with lock:
            job = jobs.get(jobid)
            if job:
                job.status  = "collecting"
                job.message = f"Filtere Links (nur Videos \u2265 {MIN_VIDEO_SIZE_MB} MB)\u2026"
        accepted, rejected = _filter_linkgrabber(dev, jobid)
        with lock:
            job = jobs.get(jobid)
            if job:
                if accepted == 0 and rejected > 0:
                    job.status   = "failed"
                    job.message  = (
                        f"Keine Video-Dateien \u2265 {MIN_VIDEO_SIZE_MB} MB gefunden "
                        f"({rejected} Link(s) verworfen)."
                    )
                    job.progress = 0.0
                    return
                if accepted == 0 and rejected == 0:
                    job.message = "Linkgrabber leer (Links bereits in Download-Queue). Filter wird nach Download angewendet."
                else:
                    job.message = f"{accepted} Video(s) akzeptiert, {rejected} verworfen."

        while True:
            with lock:
                job = jobs.get(jobid)
            if not job:
                return
            if job.cancel_requested:
                cancel_msg = cancel_job(dev, jobid)
                with lock:
                    job.status   = "canceled"
                    job.message  = cancel_msg or "Download abgebrochen und Dateien entfernt."
                    job.progress = 0.0
                return

            links, pkg_map = query_links_and_packages(dev, jobid)

            if not links:
                with lock:
                    job.status   = "collecting"
                    job.message  = "Warte auf Link-Crawler\u2026"
                    job.progress = 0.0
                time.sleep(POLL_SECONDS)
                continue

            all_demo = all(is_demo_link(l.get("name", "")) for l in links)
            if all_demo and not is_demo_link(job.url):
                cancel_msg = cancel_job(dev, jobid)
                with lock:
                    job.status   = "failed"
                    base_msg     = "JDownloader lieferte das Demo-Video Big Buck Bunny statt des gew\u00fcnschten Links."
                    job.message  = f"{base_msg} {cancel_msg}" if cancel_msg else base_msg
                    job.progress = 0.0
                return

            all_finished = all(bool(l.get("finished")) for l in links)
            if not all_finished:
                progress = calculate_progress(links)
                with lock:
                    job.status   = "downloading"
                    done         = sum(1 for l in links if l.get("finished"))
                    job.message  = f"Download l\u00e4uft\u2026 ({done}/{len(links)} fertig)"
                    job.progress = progress
                time.sleep(POLL_SECONDS)
                continue

            local_paths = local_paths_from_links(links, pkg_map)
            video_files = [p for p in local_paths if is_video_file(p) and os.path.isfile(p)]

            if not video_files:
                with lock:
                    job.status   = "failed"
                    job.message  = "Keine Video-Datei gefunden (Whitelist)."
                    job.progress = 0.0
                return

            valid_videos = [p for p in video_files if ffprobe_ok(p)]
            if not valid_videos:
                with lock:
                    job.status   = "failed"
                    job.message  = "ffprobe: keine g\u00fcltige Video-Datei."
                    job.progress = 0.0
                return

            # Rename local files to package name before upload
            pkg_base = sanitize_name(job.package_name) if job.package_name and job.package_name != "WebGUI" else ""
            if pkg_base:
                renamed = []
                for idx, f in enumerate(valid_videos):
                    ext      = os.path.splitext(f)[1]
                    suffix   = f".part{idx + 1}" if len(valid_videos) > 1 else ""
                    new_path = os.path.join(os.path.dirname(f), f"{pkg_base}{suffix}{ext}")
                    try:
                        os.rename(f, new_path)
                        renamed.append(new_path)
                    except Exception:
                        renamed.append(f)
                valid_videos = renamed

            with lock:
                job.status   = "upload"
                job.message  = f"Download fertig. MD5/Upload/Verify f\u00fcr {len(valid_videos)} Datei(en)\u2026"
                job.progress = 100.0

            ssh = ssh_connect()
            try:
                for f in valid_videos:
                    md5_hex  = md5_file(f)
                    md5_path = write_md5_sidecar(f, md5_hex)

                    remote_dir, remote_name = build_remote_paths(job.library, job.package_name, f)
                    remote_file = f"{remote_dir}/{remote_name}"
                    remote_md5f = remote_file + ".md5"

                    sftp_upload(ssh, f, remote_file)
                    sftp_upload(ssh, md5_path, remote_md5f)

                    remote_md5 = remote_md5sum(ssh, remote_file)
                    if remote_md5.lower() != md5_hex.lower():
                        raise RuntimeError(f"MD5 mismatch for {os.path.basename(f)}: local={md5_hex} remote={remote_md5}")

                    try:
                        os.remove(f)
                    except Exception:
                        pass
                    try:
                        os.remove(md5_path)
                    except Exception:
                        pass

            finally:
                ssh.close()

            jd_cleanup_msg = try_remove_from_jd(dev, links, pkg_map)

            if JELLYFIN_LIBRARY_REFRESH:
                jellyfin_refresh_library()

            with lock:
                job.status   = "finished"
                job.message  = "Upload + MD5 OK. " + (jd_cleanup_msg or "JDownloader: Paket/Links entfernt.")
                job.progress = 100.0
            return

    except Exception as e:
        with lock:
            job = jobs.get(jobid)
            if job:
                job.status   = "failed"
                job.message  = str(e)
                job.progress = 0.0

# ============================================================
# Web
# ============================================================
@app.get("/favicon.ico")
def favicon():
    return HTMLResponse(status_code=204)

@app.get("/jobs", response_class=HTMLResponse)
def jobs_get():
    with lock:
        job_list = list(jobs.values())
    with _log_lock:
        log_lines = list(_conn_log)
    return HTMLResponse(render_jobs_page(job_list, log_lines))

def esc(s: str) -> str:
    return (s
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;"))

def render_jobs_page(job_list: list, log_lines: list) -> str:
    rows = ""
    for j in reversed(job_list):
        bar_color = {
            "finished":    "#4caf50",
            "failed":      "#f44336",
            "canceled":    "#9e9e9e",
            "uploading":   "#2196f3",
            "upload":      "#2196f3",
            "downloading": "#ff9800",
            "collecting":  "#9c27b0",
        }.get(j.status, "#607d8b")
        rows += (
            f"<tr>"
            f"<td>{esc(j.package_name)}</td>"
            f"<td>{esc(j.url[:60])}{'...' if len(j.url) > 60 else ''}</td>"
            f"<td>{esc(j.status)}</td>"
            f"<td>{esc(j.message)}</td>"
            f"<td>"
            f"  <div style='background:#ddd;border-radius:4px;height:12px;width:120px'>"
            f"    <div style='background:{bar_color};width:{j.progress:.0f}%;height:12px;border-radius:4px'></div>"
            f"  </div>"
            f"  {j.progress:.0f}%"
            f"</td>"
            f"<td>"
            f"  <form method='post' action='/cancel/{esc(j.id)}' style='display:inline'>"
            f"    <button type='submit'>Abbrechen</button>"
            f"  </form>"
            f"</td>"
            f"</tr>"
        )

    log_html = "\n".join(esc(l) for l in reversed(log_lines[-50:]))

    return f"""<!DOCTYPE html>
<html lang='de'>
<head>
  <meta charset='UTF-8'>
  <meta http-equiv='refresh' content='5'>
  <title>JD WebGUI - Jobs</title>
  <link rel='stylesheet' href='/static/style.css'>
</head>
<body>
  <h1>JD WebGUI</h1>
  <nav>
    <a href='/'>Neuer Download</a> |
    <a href='/jobs'>Jobs</a> |
    <a href='/proxies'>Proxies</a>
  </nav>

  <h2>Aktive Jobs</h2>
  <form method='post' action='/clear-finished'>
    <button type='submit'>Erledigte entfernen</button>
  </form>
  <table>
    <thead>
      <tr>
        <th>Paket</th><th>URL</th><th>Status</th><th>Info</th><th>Fortschritt</th><th>Aktion</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <h2>Verbindungslog</h2>
  <pre class='log'>{log_html}</pre>
</body>
</html>"""

def render_page(message: str = "", error: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang='de'>
<head>
  <meta charset='UTF-8'>
  <title>JD WebGUI</title>
  <link rel='stylesheet' href='/static/style.css'>
</head>
<body>
  <h1>JD WebGUI</h1>
  <nav>
    <a href='/'>Neuer Download</a> |
    <a href='/jobs'>Jobs</a> |
    <a href='/proxies'>Proxies</a>
  </nav>

  <form method='post' action='/submit'>
    <label>URL:<br>
      <input name='url' type='url' required size='80' placeholder='https://...' />
    </label><br><br>
    <label>Paketname (optional):<br>
      <input name='package_name' placeholder='z. B. Sister Act (1992)' />
    </label><br><br>
    <label>Bibliothek:<br>
      <select name='library'>
        <option value='auto'>Auto</option>
        <option value='movies'>Filme</option>
        <option value='series'>Serien</option>
      </select>
    </label><br><br>
    <button type='submit'>Download starten</button>
  </form>

  {"<p class='error'>" + esc(error) + "</p>" if error else ""}
  {"<p class='ok'>"    + esc(message) + "</p>" if message else ""}

  <hr>
  <small>
    Video-Whitelist: {", ".join(sorted(VIDEO_EXTS))}<br>
    Mindestgr&ouml;&szlig;e: {MIN_VIDEO_SIZE_MB} MB
  </small>
</body>
</html>"""

@dataclass
class ProxyCheckResult:
    proto:      str
    host:       str
    port:       int
    ok:         bool
    latency_ms: int
    exit_ip:    str
    error:      str


def render_proxies_page(
    socks5_in:    str = "",
    socks4_in:    str = "",
    export_path:  str = "",
    error:        str = "",
    message:      str = "",
    check_results: Optional[List[ProxyCheckResult]] = None,
    jd_message:   str = "",
) -> str:
    # ── build check-results table ──────────────────────────────
    results_html = ""
    if check_results is not None:
        ok_count   = sum(1 for r in check_results if r.ok)
        fail_count = len(check_results) - ok_count
        results_html = (
            f"<h2>Prüfergebnis: {ok_count} funktionierend, {fail_count} fehlgeschlagen</h2>"
        )
        if jd_message:
            color = "#388e3c" if "erfolgreich" in jd_message.lower() or "ok" in jd_message.lower() else "#e65100"
            results_html += f"<p style='color:{color};font-weight:bold'>{esc(jd_message)}</p>"
        results_html += (
            "<table>"
            "<thead><tr><th>Proxy</th><th>Status</th><th>Latenz</th><th>Exit-IP</th><th>Fehler</th></tr></thead>"
            "<tbody>"
        )
        for r in check_results:
            color = "#388e3c" if r.ok else "#c62828"
            icon  = "✅" if r.ok else "❌"
            results_html += (
                f"<tr style='color:{color}'>"
                f"<td><code>{esc(r.proto)}://{esc(r.host)}:{r.port}</code></td>"
                f"<td>{icon} {'OK' if r.ok else 'Fehler'}</td>"
                f"<td>{r.latency_ms} ms</td>"
                f"<td>{esc(r.exit_ip)}</td>"
                f"<td>{esc(r.error)}</td>"
                f"</tr>"
            )
        results_html += "</tbody></table>"

    return f"""<!DOCTYPE html>
<html lang='de'>
<head>
  <meta charset='UTF-8'>
  <title>JD WebGUI - Proxies</title>
  <link rel='stylesheet' href='/static/style.css'>
</head>
<body>
  <h1>JD WebGUI - Proxy-Verwaltung</h1>
  <nav>
    <a href='/'>Neuer Download</a> |
    <a href='/jobs'>Jobs</a> |
    <a href='/proxies'>Proxies</a>
  </nav>

  {"<p class='error'>" + esc(error) + "</p>" if error else ""}
  {"<p class='ok'>" + esc(message) + "</p>" if message else ""}

  <form method='post' action='/proxies'>
    <p style='color:#555;font-size:.9em'>
      Proxies werden automatisch von proxyscrape.com geladen oder können manuell eingetragen werden.<br>
      <strong>Testen &amp; einbinden</strong> prüft jeden Proxy auf Funktion und trägt die
      funktionierenden direkt in JDownloader ein.
    </p>
    <label>SOCKS5-Proxies (eine pro Zeile, Format: <code>host:port</code>):<br>
      <textarea name='socks5_in' rows='8' cols='70'>{esc(socks5_in)}</textarea>
    </label><br><br>
    <label>SOCKS4-Proxies (eine pro Zeile, Format: <code>host:port</code>):<br>
      <textarea name='socks4_in' rows='8' cols='70'>{esc(socks4_in)}</textarea>
    </label><br><br>
    <button type='submit' name='action' value='preview'>Vorschau</button>
    &nbsp;
    <button type='submit' name='action' value='check'
            style='background:#1976d2;color:#fff;border:none;padding:.4em .9em;border-radius:4px;cursor:pointer'>
      ✅ Testen &amp; in JDownloader einbinden
    </button>
  </form>

  {results_html}
</body>
</html>"""

# ============================================================
# Proxy helpers
# ============================================================
def format_proxy_lines(raw: str, proto: str) -> str:
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        lines.append(f"{proto}://{line}")
    return "\n".join(lines)

def build_jdproxies_payload(text: str) -> Dict[str, Any]:
    if not text.strip():
        raise ValueError("Keine Proxy-Eintr\u00e4ge zum Speichern.")
    entries: List[Dict[str, Any]] = []
    type_map = {
        "socks5": "SOCKS5",
        "socks4": "SOCKS4",
        "http":   "HTTP",
        "https":  "HTTPS",
    }
    for line in text.splitlines():
        line   = line.strip()
        if not line:
            continue
        parsed  = urllib.parse.urlparse(line)
        proto   = (parsed.scheme or "").lower()
        host    = parsed.hostname or ""
        port    = parsed.port or 1080
        if not host:
            continue
        jd_type = type_map.get(proto, "SOCKS5")
        entries.append({
            "type":     jd_type,
            "address":  host,
            "port":     port,
            "username": "",
            "password": "",
            "enabled":  True,
        })
    if not entries:
        raise ValueError("Keine validen Proxy-Eintr\u00e4ge gefunden.")
    return {"proxies": entries, "version": 1}


# ---------------------------------------------------------------------------
# Proxy-Check helpers
# ---------------------------------------------------------------------------

def _parse_proxy_entries(socks5_in: str, socks4_in: str) -> List[dict]:
    """Return list of {proto, host, port} dicts from the two textarea inputs."""
    result = []
    for proto, text in [("socks5", socks5_in), ("socks4", socks4_in)]:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # allow bare host:port  OR  scheme://host:port
            if "://" not in line:
                line = proto + "://" + line
            parsed = urllib.parse.urlparse(line)
            host = parsed.hostname or ""
            port = parsed.port or 1080
            if not host:
                continue
            result.append({"proto": proto, "host": host, "port": port})
    return result


def _check_one_proxy(entry: dict, timeout: int = 10) -> "ProxyCheckResult":
    import time
    proto = entry["proto"]
    host  = entry["host"]
    port  = entry["port"]
    proxy_url = f"{proto}://{host}:{port}"
    t0 = time.monotonic()
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({
                "http":  proxy_url,
                "https": proxy_url,
            })
        )
        with opener.open("http://ip-api.com/json?fields=query", timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            exit_ip = data.get("query", "?")
        latency_ms = int((time.monotonic() - t0) * 1000)
        return ProxyCheckResult(proto=proto, host=host, port=port,
                                ok=True, latency_ms=latency_ms,
                                exit_ip=exit_ip, error="")
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return ProxyCheckResult(proto=proto, host=host, port=port,
                                ok=False, latency_ms=latency_ms,
                                exit_ip="", error=str(exc)[:120])


def check_proxies_parallel(
    entries: List[dict],
    timeout: int = 10,
    max_workers: int = 40,
) -> List["ProxyCheckResult"]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_check_one_proxy, e, timeout): e for e in entries}
        for fut in as_completed(futs):
            results.append(fut.result())
    # sort: working first (by latency), failed last
    results.sort(key=lambda r: (0 if r.ok else 1, r.latency_ms))
    return results


def push_proxies_to_jd(working: List["ProxyCheckResult"]) -> str:
    """Write .jdproxies file + try to push via MyJD API. Returns status message."""
    messages = []

    # 1) Write .jdproxies file to shared volume
    export_path = os.environ.get("PROXY_EXPORT_PATH", "")
    if export_path:
        try:
            type_map = {"socks5": "SOCKS5", "socks4": "SOCKS4", "http": "HTTP", "https": "HTTP"}
            entries = []
            for r in working:
                entries.append({
                    "type":     type_map.get(r.proto, "SOCKS5"),
                    "address":  r.host,
                    "port":     r.port,
                    "username": "",
                    "password": "",
                    "enabled":  True,
                })
            payload = json.dumps({"proxies": entries, "version": 1}, indent=2)
            with open(export_path, "w") as fh:
                fh.write(payload)
            messages.append(f"✅ .jdproxies gespeichert: {export_path} ({len(entries)} Einträge)")
        except Exception as exc:
            messages.append(f"⚠️ Datei-Export fehlgeschlagen: {exc}")
    else:
        messages.append("ℹ️ PROXY_EXPORT_PATH nicht gesetzt — Datei-Export übersprungen")

    # 2) Try MyJD API push
    try:
        dev = _get_jd_device()
        proxy_list = []
        for r in working:
            type_map2 = {"socks5": "SOCKS5", "socks4": "SOCKS4", "http": "HTTP", "https": "HTTP"}
            proxy_list.append({
                "type":     type_map2.get(r.proto, "SOCKS5"),
                "address":  r.host,
                "port":     r.port,
                "username": "",
                "password": "",
                "enabled":  True,
            })
        value_json = json.dumps(proxy_list)
        dev.system.call_action(
            "config/set",
            params=[{
                "interfaceName": "org.jdownloader.settings.GeneralSettings",
                "storage":       "GeneralSettings",
                "key":           "proxylist",
                "value":         value_json,
            }]
        )
        messages.append(f"✅ {len(proxy_list)} Proxy/s via MyJD-API an JDownloader übermittelt")
    except Exception as exc:
        messages.append(f"⚠️ MyJD-API-Push fehlgeschlagen: {exc}")

    return " | ".join(messages)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return render_main_page()


@app.post("/submit", response_class=HTMLResponse)
async def submit(
    request: Request,
    url:     str = Form(""),
    title:   str = Form(""),
    dest:    str = Form("downloads"),
    season:  str = Form(""),
    episode: str = Form(""),
):
    url   = url.strip()
    title = title.strip()
    if not url:
        return render_main_page(error="Bitte eine URL eingeben.")

    # build package name
    pkg = title or url
    if season:
        pkg += f" S{int(season):02d}"
    if episode:
        pkg += f"E{int(episode):02d}"

    # determine target path
    dest_map = {
        "downloads": os.environ.get("OUTPUT_DIR", "/output"),
        "movies":    os.environ.get("JELLYFIN_MOVIES_DIR", "/output/movies"),
        "series":    os.environ.get("JELLYFIN_SERIES_DIR", "/output/series"),
    }
    saveto = dest_map.get(dest, dest_map["downloads"])

    try:
        dev = _get_jd_device()
        dev.linkgrabber.add_links([{
            "autostart":       True,
            "links":           url,
            "packageName":     pkg,
            "destinationFolder": saveto,
            "overwritePackagizerRules": True,
        }])
        return render_main_page(message=f"✅ Hinzugefügt: <b>{pkg}</b>")
    except Exception as exc:
        return render_main_page(error=f"Fehler: {exc}")


@app.post("/cancel/{jobid}", response_class=HTMLResponse)
async def cancel_job(jobid: str):
    try:
        dev = _get_jd_device()
        dev.downloads.remove_links(link_ids=[], package_ids=[int(jobid)])
    except Exception:
        pass
    return RedirectResponse("/jobs", status_code=303)


@app.post("/clear-finished", response_class=HTMLResponse)
async def clear_finished():
    try:
        dev = _get_jd_device()
        packages = dev.downloads.query_packages([{}])
        ids = [p["uuid"] for p in (packages or []) if p.get("status") in ("Finished", "Fertig")]
        if ids:
            dev.downloads.remove_links(link_ids=[], package_ids=ids)
    except Exception:
        pass
    return RedirectResponse("/jobs", status_code=303)


@app.get("/proxies", response_class=HTMLResponse)
async def proxies_get():
    socks5 = os.environ.get("PROXY_LIST_SOCKS5", "")
    socks4 = os.environ.get("PROXY_LIST_SOCKS4", "")
    export = os.environ.get("PROXY_EXPORT_PATH", "")
    return render_proxies_page(socks5_in=socks5, socks4_in=socks4, export_path=export)


@app.post("/proxies", response_class=HTMLResponse)
async def proxies_post(
    request: Request,
    socks5_proxies: str = Form(""),
    socks4_proxies: str = Form(""),
    export_path:    str = Form(""),
    action:         str = Form("preview"),
):
    entries = _parse_proxy_entries(socks5_proxies, socks4_proxies)

    if action == "preview" or not entries:
        msg = f"{len(entries)} Proxy/s erkannt." if entries else "Keine Proxies eingegeben."
        return render_proxies_page(
            socks5_in=socks5_proxies,
            socks4_in=socks4_proxies,
            export_path=export_path,
            message=msg,
        )

    # action == "check"
    results = check_proxies_parallel(entries)
    working = [r for r in results if r.ok]
    jd_msg  = ""
    if working:
        jd_msg = push_proxies_to_jd(working)

    return render_proxies_page(
        socks5_in=socks5_proxies,
        socks4_in=socks4_proxies,
        export_path=export_path,
        check_results=results,
        jd_message=jd_msg,
    )
