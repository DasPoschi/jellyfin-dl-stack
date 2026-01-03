+26-2
#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import os
import random
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List
from urllib.request import urlopen

import paramiko
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output").rstrip("/")
MD5_DIR = os.environ.get("MD5_DIR", "/md5").rstrip("/")

JELLYFIN_HOST = os.environ.get("JELLYFIN_HOST", "192.168.1.1")
JELLYFIN_PORT = int(os.environ.get("JELLYFIN_PORT", "22"))
JELLYFIN_USER = os.environ.get("JELLYFIN_USER", "")
JELLYFIN_SSH_KEY = os.environ.get("JELLYFIN_SSH_KEY", "/ssh/id_ed25519")

JELLYFIN_MOVIES_DIR = os.environ.get("JELLYFIN_MOVIES_DIR", "/jellyfin/Filme").rstrip("/")
JELLYFIN_SERIES_DIR = os.environ.get("JELLYFIN_SERIES_DIR", "/jellyfin/Serien").rstrip("/")

ENGINE_DEFAULT = os.environ.get("ENGINE_DEFAULT", "auto").strip().lower()
YTDLP_FORMAT = os.environ.get("YTDLP_FORMAT", "bestvideo+bestaudio/best")

BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "").strip()
BASIC_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "").strip()

PROXY_MODE = os.environ.get("PROXY_MODE", "round_robin").strip().lower()
PROXY_LIST_RAW = os.environ.get("PROXY_LIST", "")
PROXY_SOURCES = {
    "socks5": "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt",
    "socks4": "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt",
    "http": "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
}

URL_RE = re.compile(r"^https?://", re.I)
YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)", re.I)

VIDEO_EXTS = (".mkv",".mp4",".m4v",".avi",".mov",".wmv",".flv",".webm",".ts",".m2ts",".mpg",".mpeg",".vob",".ogv",".3gp",".3g2")
SERIES_RE = re.compile(r"(?:^|[^a-z0-9])S(\d{1,2})E(\d{1,2})(?:[^a-z0-9]|$)", re.IGNORECASE)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

def _auth_enabled() -> bool:
    return bool(BASIC_AUTH_USER and BASIC_AUTH_PASS)

def _check_basic_auth(req: Request) -> bool:
    if not _auth_enabled():
        return True
    hdr = req.headers.get("authorization", "")
    if not hdr.lower().startswith("basic "):
        return False
    b64 = hdr.split(" ", 1)[1].strip()
    try:
        raw = base64.b64decode(b64).decode("utf-8", "replace")
    except Exception:
        return False
    if ":" not in raw:
@@ -82,88 +88,106 @@ class Job:
    engine: str
    library: str
    proxy: str
    status: str
    message: str

jobs: Dict[str, Job] = {}
lock = threading.Lock()
_rr_idx = 0

def parse_proxy_list(raw: str) -> List[str]:
    out = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    seen = set()
    dedup = []
    for x in out:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup

def pick_proxy(forced_proxy: str = "") -> str:
    global _rr_idx
    if forced_proxy:
        return forced_proxy.strip()
    if PROXY_MODE == "off" or not PROXIES:
        return ""
    if PROXY_MODE == "random":
        return random.choice(PROXIES)
    p = PROXIES[_rr_idx % len(PROXIES)]
    _rr_idx += 1
    return p

def format_proxy_lines(raw: str, scheme: str) -> str:
    scheme = scheme.strip().lower()
    if scheme not in {"socks5", "socks4", "http", "https"}:
        raise ValueError("Unsupported proxy scheme")
    out = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "://" in s:
            s = s.split("://", 1)[1].strip()
        if ":" not in s:
            continue
        host, port = s.rsplit(":", 1)
        host, port = host.strip(), port.strip()
        if not host or not port.isdigit():
            continue
        out.append(f"{scheme}://{host}:{port}")
    seen=set(); ded=[]
    for x in out:
        if x not in seen:
            seen.add(x); ded.append(x)
    return "\n".join(ded)

def fetch_proxy_source(url: str) -> str:
    with urlopen(url, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")

def load_proxy_sources() -> List[str]:
    chunks = []
    for scheme, url in PROXY_SOURCES.items():
        try:
            raw = fetch_proxy_source(url)
        except Exception as exc:
            print(f"Proxy source failed: {url} error={exc}")
            continue
        formatted = format_proxy_lines(raw, scheme)
        if formatted:
            chunks.append(formatted)
    combined = "\n".join(chunks)
    return parse_proxy_list(combined)

PROXIES = parse_proxy_list("\n".join([PROXY_LIST_RAW, "\n".join(load_proxy_sources())]))

def pick_engine(url: str, forced: str) -> str:
    forced = (forced or "").strip().lower()
    if forced and forced != "auto":
        return forced
    u = url.lower()
    if YOUTUBE_RE.search(u):
        return "ytdlp"
    if u.split("?")[0].endswith(VIDEO_EXTS):
        return "direct"
    return "direct"

def run_ytdlp(url: str, out_dir: str, fmt: str, proxy: str):
    cmd = ["yt-dlp", "-f", fmt, "-o", f"{out_dir}/%(title)s.%(ext)s", url]
    if proxy:
        cmd += ["--proxy", proxy]
    subprocess.check_call(cmd)

def run_aria2(url: str, out_dir: str, proxy: str):
    cmd = ["aria2c", "--dir", out_dir, "--allow-overwrite=true", "--auto-file-renaming=false", url]
    if proxy:
        cmd += ["--all-proxy", proxy]
    subprocess.check_call(cmd)

def md5_file(path: str) -> str:
    h = hashlib.md5()
