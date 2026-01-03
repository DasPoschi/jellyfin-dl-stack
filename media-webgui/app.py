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
        return False
    user, pw = raw.split(":", 1)
    return user == BASIC_AUTH_USER and pw == BASIC_AUTH_PASS

def _auth_challenge() -> HTMLResponse:
    return HTMLResponse("Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="media-webgui"'})

@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if not _check_basic_auth(request):
        return _auth_challenge()
    return await call_next(request)

@dataclass
class Job:
    id: str
    url: str
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

PROXIES = parse_proxy_list(PROXY_LIST_RAW)

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
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()

def write_md5_sidecar(local_file: str, md5_hex: str) -> str:
    os.makedirs(MD5_DIR, exist_ok=True)
    base = os.path.basename(local_file)
    md5p = os.path.join(MD5_DIR, base + ".md5")
    with open(md5p, "w", encoding="utf-8") as f:
        f.write(f"{md5_hex}  {base}\n")
    return md5p

def ssh_connect() -> paramiko.SSHClient:
    if not JELLYFIN_USER:
        raise RuntimeError("JELLYFIN_USER missing")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
        sftp_mkdirs(sftp, os.path.dirname(remote_path))
        sftp.put(local_path, remote_path)
    except Exception as e:
        raise RuntimeError(f"SFTP upload failed: local={local_path} remote={remote_path} error={e}")
    finally:
        sftp.close()

def remote_md5sum(ssh: paramiko.SSHClient, remote_path: str) -> str:
    cmd = f"md5sum {shlex.quote(remote_path)}"
    _, stdout, stderr = ssh.exec_command(cmd, timeout=120)
    out = stdout.read().decode("utf-8", "replace").strip()
    err = stderr.read().decode("utf-8", "replace").strip()
    if err and not out:
        raise RuntimeError(f"Remote md5sum failed: {err}")
    if not out:
        raise RuntimeError("Remote md5sum returned empty output")
    return out.split()[0]

def choose_target_dir(library: str, filename: str) -> str:
    library = (library or "auto").lower()
    if library == "series":
        return JELLYFIN_SERIES_DIR
    if library == "movies":
        return JELLYFIN_MOVIES_DIR
    if SERIES_RE.search(filename):
        return JELLYFIN_SERIES_DIR
    return JELLYFIN_MOVIES_DIR

def list_output_files(before: set) -> List[str]:
    now = set()
    for root, _, files in os.walk(OUTPUT_DIR):
        for fn in files:
            now.add(os.path.join(root, fn))
    new = [p for p in sorted(now) if p not in before]
    final = []
    for p in new:
        low = p.lower()
        if low.endswith((".part",".tmp",".crdownload")):
            continue
        final.append(p)
    return final

def worker(jobid: str):
    try:
        with lock:
            job = jobs[jobid]

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        before = set()
        for root, _, files in os.walk(OUTPUT_DIR):
            for fn in files:
                before.add(os.path.join(root, fn))

        engine = pick_engine(job.url, job.engine)
        proxy = job.proxy

        with lock:
            job.status = "downloading"
            job.message = f"Engine={engine} Proxy={'none' if not proxy else proxy}"

        if engine == "ytdlp":
            run_ytdlp(job.url, OUTPUT_DIR, YTDLP_FORMAT, proxy)
        else:
            run_aria2(job.url, OUTPUT_DIR, proxy)

        new_files = list_output_files(before)
        if not new_files:
            raise RuntimeError("No output file detected in /output")

        ssh = ssh_connect()
        try:
            for f in new_files:
                if not os.path.isfile(f):
                    continue

                md5_hex = md5_file(f)
                md5_path = write_md5_sidecar(f, md5_hex)

                target_dir = choose_target_dir(job.library, os.path.basename(f))
                remote_file = f"{target_dir}/{os.path.basename(f)}"
                remote_md5f = remote_file + ".md5"

                with lock:
                    job.status = "upload"
                    job.message = f"Uploading: {os.path.basename(f)} -> {remote_file}"

                sftp_upload(ssh, f, remote_file)
                sftp_upload(ssh, md5_path, remote_md5f)

                remote_md5 = remote_md5sum(ssh, remote_file)
                if remote_md5.lower() != md5_hex.lower():
                    raise RuntimeError(f"MD5 mismatch: local={md5_hex} remote={remote_md5}")

                try: os.remove(f)
                except Exception: pass
                try: os.remove(md5_path)
                except Exception: pass

        finally:
            ssh.close()

        with lock:
            job.status = "finished"
            job.message = f"OK ({len(new_files)} file(s))"

    except Exception as e:
        with lock:
            jobs[jobid].status = "failed"
            jobs[jobid].message = str(e)

def render_nav(active: str) -> str:
    def link(label: str, href: str, key: str) -> str:
        style = "font-weight:700;" if active == key else ""
        return f"<a href='{href}' style='margin-right:14px; {style}'>{label}</a>"
    return "<div style='margin: 8px 0 14px 0;'>" + link("Downloads","/","downloads") + link("Proxies","/proxies","proxies") + "</div>"

def render_downloads(error: str = "") -> str:
    rows = ""
    with lock:
        job_list = list(jobs.values())[::-1]

    for j in job_list:
        rows += (
            f"<tr>"
            f"<td><code>{j.id}</code></td>"
            f"<td style='max-width:560px; word-break:break-all;'>{j.url}</td>"
            f"<td>{j.engine}</td>"
            f"<td>{j.library}</td>"
            f"<td>{'none' if not j.proxy else j.proxy}</td>"
            f"<td><b>{j.status}</b><br/><small>{j.message}</small></td>"
            f"</tr>"
        )

    err_html = f"<p class='error'>{error}</p>" if error else ""
    proxy_note = f"{len(PROXIES)} configured, mode={PROXY_MODE}" if PROXIES else "none configured"
    return f"""
    <html><head>
      <link rel="stylesheet" href="/static/style.css">
      <meta charset="utf-8">
      <title>Media WebGUI</title>
    </head>
    <body>
      <h1>Media WebGUI</h1>
      {render_nav("downloads")}
      {err_html}

      <form method="post" action="/submit">
        <div class="row"><label>Link</label><br/>
          <input name="url" placeholder="https://..." required />
        </div>

        <div class="row"><label>Engine</label><br/>
          <select name="engine">
            <option value="auto">auto</option>
            <option value="ytdlp">ytdlp</option>
            <option value="direct">direct (aria2)</option>
          </select>
        </div>

        <div class="row"><label>Library</label><br/>
          <select name="library">
            <option value="auto">auto</option>
            <option value="movies">movies</option>
            <option value="series">series</option>
          </select>
        </div>

        <div class="row"><label>Proxy (optional)</label><br/>
          <input name="proxy" placeholder="leer = auto ({proxy_note})" />
        </div>

        <button type="submit">Start</button>
      </form>

      <p class="hint">
        Output: <code>{OUTPUT_DIR}</code> | MD5: <code>{MD5_DIR}</code> | Proxies: <code>{proxy_note}</code>
      </p>

      <table>
        <thead><tr><th>JobID</th><th>URL</th><th>Engine</th><th>Library</th><th>Proxy</th><th>Status</th></tr></thead>
        <tbody>
          {rows if rows else "<tr><td colspan='6'><em>No jobs yet.</em></td></tr>"}
        </tbody>
      </table>
    </body></html>
    """

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(render_downloads())

@app.post("/submit")
def submit(url: str = Form(...), engine: str = Form("auto"), library: str = Form("auto"), proxy: str = Form("")):
    url = url.strip()
    if not URL_RE.match(url):
        return HTMLResponse(render_downloads("Only http(s) URLs supported"), status_code=400)

    engine = (engine or ENGINE_DEFAULT).strip().lower()
    library = (library or "auto").strip().lower()

    chosen_proxy = pick_proxy(proxy.strip())
    jobid = str(int(time.time() * 1000))

    with lock:
        jobs[jobid] = Job(id=jobid, url=url, engine=engine, library=library, proxy=chosen_proxy, status="queued", message="queued")

    t = threading.Thread(target=worker, args=(jobid,), daemon=True)
    t.start()
    return RedirectResponse(url="/", status_code=303)

def render_proxies_page(error: str = "", s5: str = "", s4: str = "", hp: str = "", out_text: str = "") -> str:
    err_html = f"<p class='error'>{error}</p>" if error else ""
    return f"""
    <html><head>
      <link rel="stylesheet" href="/static/style.css">
      <meta charset="utf-8">
      <title>Proxies</title>
    </head>
    <body>
      <h1>Media WebGUI</h1>
      {render_nav("proxies")}
      {err_html}

      <form method="post" action="/proxies">
        <div class="row"><label>SOCKS5 (IP:PORT je Zeile)</label><br/>
          <textarea name="s5" rows="6" style="width:100%; max-width:860px; padding:10px; border:1px solid #ccc; border-radius:8px;">{s5}</textarea>
        </div>
        <div class="row"><label>SOCKS4 (IP:PORT je Zeile)</label><br/>
          <textarea name="s4" rows="6" style="width:100%; max-width:860px; padding:10px; border:1px solid #ccc; border-radius:8px;">{s4}</textarea>
        </div>
        <div class="row"><label>HTTP (IP:PORT je Zeile)</label><br/>
          <textarea name="hp" rows="6" style="width:100%; max-width:860px; padding:10px; border:1px solid #ccc; border-radius:8px;">{hp}</textarea>
        </div>
        <button type="submit">In Import-Format umwandeln</button>
      </form>

      <h2 style="margin-top:18px;">Import-Liste (zum Kopieren)</h2>
      <p class="hint">Keine Prüfung/Validierung. In .env in <code>PROXY_LIST</code> einfügen (eine Zeile pro Proxy).</p>
      <textarea id="out" rows="12" readonly style="width:100%; max-width:860px; padding:10px; border:1px solid #ccc; border-radius:8px;">{out_text}</textarea><br/>
      <button type="button" onclick="navigator.clipboard.writeText(document.getElementById('out').value)">Kopieren</button>
    </body></html>
    """

@app.get("/proxies", response_class=HTMLResponse)
def proxies_get():
    return HTMLResponse(render_proxies_page())

@app.post("/proxies", response_class=HTMLResponse)
def proxies_post(s5: str = Form(""), s4: str = Form(""), hp: str = Form("")):
    try:
        o1 = format_proxy_lines(s5, "socks5")
        o2 = format_proxy_lines(s4, "socks4")
        o3 = format_proxy_lines(hp, "http")
        combined = "\n".join([x for x in [o1, o2, o3] if x.strip()])
        return HTMLResponse(render_proxies_page(s5=s5, s4=s4, hp=hp, out_text=combined))
    except Exception as e:
        return HTMLResponse(render_proxies_page(error=str(e), s5=s5, s4=s4, hp=hp, out_text=""), status_code=400)
