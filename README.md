# Media WebGUI (ohne JDownloader)

Dieses Projekt lädt Links über:
- **yt-dlp** (YouTube & unterstützte Video-Plattformen)
- **aria2c** (direkte HTTP/HTTPS-Links, z.B. .mkv/.mp4)
- **hoster** (aria2c mit optionalen HTTP-Headern, z.B. Cookies)

Danach:
- erzeugt es eine **MD5** und speichert sie als Sidecar
- kopiert Datei + .md5 per **SFTP** auf die Jellyfin-VM
- prüft die Remote-MD5
- löscht lokale Dateien nach Erfolg

## Wichtig
- Keine Umgehung von Paywalls/DRM/Captchas. Für "Hoster" funktioniert das nur, wenn du eine **direkte** Download-URL hast bzw. eine legitime Authentifizierung (Headers/Cookies) nutzt.

## Start
1) `.env.example` -> `.env` kopieren und Werte setzen.
2) SSH Key ablegen: `data/ssh/id_ed25519` (chmod 600)
3) `docker compose up -d --build`
4) WebUI: `http://<host>:8081`

## Proxies
- Proxies werden **nur** an yt-dlp/aria2 übergeben (pro Job), beeinflussen also nicht SFTP/Jellyfin.
- `PROXY_LIST` enthält eine Zeile pro Proxy: `socks5://IP:PORT`, `http://IP:PORT`, ...
- Die Proxy-Listen werden 2× täglich aus den TheSpeedX-Quellen geladen und ins richtige Format gebracht.

## Hoster-Engine
- Engine `hoster` nutzt **aria2c** und akzeptiert zusätzliche HTTP-Header (z.B. `Cookie:` oder `User-Agent:`) im Formular.
- Ein Header pro Zeile. Leere Zeilen/Kommentare werden ignoriert.
