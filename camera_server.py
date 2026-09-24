#!/usr/bin/env python3
"""
Camera Dashboard Server
========================
Runs alongside sound_recorder.py and serves:
  GET /                       → dashboard HTML page
  GET /snapshot               → latest JPEG snapshot (written by recorder)
  GET /videos                 → JSON list of .mp4 files from Nextcloud
  GET /video-proxy?file=x.mp4 → stream a video from Nextcloud to browser
  GET /log                    → Server-Sent Events log stream
  GET /config                 → current settings as JSON
  POST /config                → save settings to recorder.conf
  POST /restart               → restart sound-recorder systemd service

Usage:
    python3 camera_server.py [--port 6464]
"""

import argparse
import configparser
import datetime
import json
import os
import queue
import socketserver
import subprocess
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# ── Config ────────────────────────────────────────────────────
PORT          = int(os.environ.get("CAM_PORT",   6464))
CONF_FILE     = Path(os.environ.get("CONF_FILE", "/home/mikael/recorder.conf"))
HTML_FILE     = Path(__file__).parent / "dashboard.html"
SNAPSHOT_PATH = "/tmp/sc_snapshot.jpg"   # written by sound_recorder.py

# Nextcloud — same env-vars as the recorder
NC_URL        = os.environ.get("NC_URL",        "https://cloud.nylen.org")
NC_USER       = os.environ.get("NC_USER",       "")
NC_PASS       = os.environ.get("NC_PASS",       "")
NC_REMOTE_DIR = os.environ.get("NC_REMOTE_DIR", "/CrashCam")

# Dashboard settings password — set via env-var (never hardcode in JS)
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "crashcam")

DEFAULT_CONF = {
    "threshold":         "0.15",
    "pre_trigger_secs":  "5",
    "post_trigger_secs": "60",
    "log_rms_interval":  "1",
    "video_fps":         "20",
    "frame_width":       "1280",
    "frame_height":      "720",
}

# ── Shared state ──────────────────────────────────────────────
_log_queues: list = []
_log_lock         = threading.Lock()


# ─────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────

def read_conf() -> dict:
    cfg = dict(DEFAULT_CONF)
    if CONF_FILE.exists():
        parser = configparser.ConfigParser()
        parser.read(CONF_FILE)
        if "recorder" in parser:
            cfg.update(dict(parser["recorder"]))
    return cfg


def write_conf(data: dict):
    parser = configparser.ConfigParser()
    parser["recorder"] = data
    with open(CONF_FILE, "w") as fh:
        parser.write(fh)


# ─────────────────────────────────────────────────────────────
# Nextcloud helpers
# ─────────────────────────────────────────────────────────────

def _nc_auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(NC_USER, NC_PASS)


def list_nc_videos() -> list:
    """Return [{name, size_mb, modified}] for .mp4 files in NC_REMOTE_DIR."""
    if not NC_USER or not NC_PASS:
        return []

    url  = f"{NC_URL}/remote.php/dav/files/{NC_USER}{NC_REMOTE_DIR}/"
    body = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:displayname/>
    <d:getcontentlength/>
    <d:getlastmodified/>
    <d:resourcetype/>
  </d:prop>
</d:propfind>"""

    try:
        r = requests.request(
            "PROPFIND", url,
            auth=_nc_auth(),
            headers={"Depth": "1", "Content-Type": "application/xml"},
            data=body,
            timeout=15,
        )
        if r.status_code != 207:
            print(f"Nextcloud PROPFIND returned HTTP {r.status_code}", flush=True)
            return []

        root   = ET.fromstring(r.text)
        ns     = {"d": "DAV:"}
        videos = []

        for resp in root.findall("d:response", ns):
            href = resp.findtext("d:href", "", ns)
            name = href.rstrip("/").split("/")[-1]
            if not name.endswith(".mp4"):
                continue

            size_raw = ""
            mod_raw  = ""

            # Nextcloud splits properties across multiple propstat blocks
            for propstat in resp.findall("d:propstat", ns):
                status = propstat.findtext("d:status", "", ns)
                if "200" not in status:
                    continue
                prop = propstat.find("d:prop", ns)
                if prop is None:
                    continue
                cl = prop.findtext("d:getcontentlength", "", ns)
                lm = prop.findtext("d:getlastmodified",  "", ns)
                if cl:
                    size_raw = cl
                if lm:
                    mod_raw  = lm

            try:
                size_mb = round(int(size_raw) / 1_048_576, 1)
            except (ValueError, TypeError):
                size_mb = 0

            videos.append({
                "name":     name,
                "size_mb":  size_mb,
                "modified": mod_raw,
            })

        videos.sort(key=lambda x: x["modified"], reverse=True)
        return videos

    except Exception as exc:
        print(f"Nextcloud list error: {exc}", flush=True)
        return []


def proxy_nc_video(filename: str, handler):
    """Stream a video file from Nextcloud to the HTTP client."""
    safe = Path(filename).name   # strip path traversal
    if not safe.endswith(".mp4"):
        handler.send_error(400, "Only .mp4 files supported")
        return

    url = f"{NC_URL}/remote.php/dav/files/{NC_USER}{NC_REMOTE_DIR}/{safe}"
    try:
        r = requests.get(url, auth=_nc_auth(), stream=True, timeout=30)
        if r.status_code != 200:
            handler.send_error(502, f"Nextcloud returned {r.status_code}")
            return

        qs      = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
        dl_mode = qs.get("dl", ["0"])[0] == "1"

        content_length = r.headers.get("Content-Length", "")
        handler.send_response(200)
        handler.send_header("Content-Type", "video/mp4")
        handler.send_header("Accept-Ranges", "bytes")
        if dl_mode:
            handler.send_header("Content-Disposition",
                                f'attachment; filename="{safe}"')
        if content_length:
            handler.send_header("Content-Length", content_length)
        handler.end_headers()

        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                handler.wfile.write(chunk)

    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as exc:
        print(f"Video proxy error: {exc}", flush=True)


# ─────────────────────────────────────────────────────────────
# Log thread — tails journald for sound-recorder output
# ─────────────────────────────────────────────────────────────

def _journald_thread():
    proc = subprocess.Popen(
        ["journalctl", "-u", "sound-recorder", "-f", "--no-pager", "-o", "cat"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        ts  = datetime.datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {line}"
        with _log_lock:
            dead = []
            for q in _log_queues:
                try:
                    q.put_nowait(msg)
                except Exception:
                    dead.append(q)
            for q in dead:
                _log_queues.remove(q)


# ─────────────────────────────────────────────────────────────
# Threaded HTTP server
# ─────────────────────────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Each request handled in its own thread — prevents slow Nextcloud
    calls from blocking the snapshot or log endpoints."""
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass   # silence per-request access logs

    # ── Routing ──────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?")[0]
        qs   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if   path == "/":             self._serve_file(HTML_FILE, "text/html; charset=utf-8")
        elif path == "/snapshot":     self._serve_snapshot()
        elif path == "/videos":       self._serve_videos()
        elif path == "/video-proxy":  proxy_nc_video(qs.get("file", [""])[0], self)
        elif path == "/log":          self._serve_log_sse()
        elif path == "/config":       self._serve_config()
        elif path == "/sysinfo":      self._serve_sysinfo()
        else:                         self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if   path == "/auth":     self._check_auth()
        elif path == "/config":   self._save_config()
        elif path == "/restart":  self._restart_service()
        elif path == "/trigger":  self._manual_trigger()
        else:                     self.send_error(404)

    # ── Handlers ─────────────────────────────────────────────

    def _serve_file(self, path: Path, mime: str):
        if not path.exists():
            self.send_error(404, f"File not found: {path}")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _check_auth(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            data = json.loads(body)
            ok   = data.get("password", "") == DASHBOARD_PASS
            resp = json.dumps({"ok": ok}).encode()
            self.send_response(200)
        except Exception:
            resp = json.dumps({"ok": False}).encode()
            self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def _serve_sysinfo(self):
        import shutil
        try:
            # Memory — read from /proc/meminfo for accuracy
            meminfo = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    key, val = line.split(":")
                    meminfo[key.strip()] = int(val.strip().split()[0]) * 1024

            mem_total     = meminfo.get("MemTotal",     0)
            mem_available = meminfo.get("MemAvailable", 0)
            mem_used      = mem_total - mem_available
            swap_total    = meminfo.get("SwapTotal",    0)
            swap_free     = meminfo.get("SwapFree",     0)
            swap_used     = swap_total - swap_free

            # Disk — SD card root partition
            disk = shutil.disk_usage("/")

            # CPU temperature
            try:
                with open("/sys/class/thermal/thermal_zone0/temp") as fh:
                    cpu_temp = int(fh.read().strip()) / 1000.0
            except Exception:
                cpu_temp = None

            data = {
                "mem_total":     mem_total,
                "mem_used":      mem_used,
                "mem_available": mem_available,
                "mem_pct":       round(mem_used / max(mem_total, 1) * 100, 1),
                "swap_total":    swap_total,
                "swap_used":     swap_used,
                "swap_free":     swap_free,
                "disk_total":    disk.total,
                "disk_used":     disk.used,
                "disk_free":     disk.free,
                "disk_pct":      round(disk.used / max(disk.total, 1) * 100, 1),
                "cpu_temp":      cpu_temp,
            }
            body = json.dumps(data).encode()
            self.send_response(200)
        except Exception as exc:
            body = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_snapshot(self):
        snap = Path(SNAPSHOT_PATH)
        if not snap.exists():
            self.send_error(503, "No snapshot yet — recorder still starting up")
            return
        data  = snap.read_bytes()
        mtime = snap.stat().st_mtime
        ts    = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("X-Snapshot-Time", ts)
        self.end_headers()
        self.wfile.write(data)

    def _serve_videos(self):
        body = json.dumps(list_nc_videos()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_log_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q: queue.Queue = queue.Queue(maxsize=200)
        with _log_lock:
            _log_queues.append(q)
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(line)}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _log_lock:
                try:
                    _log_queues.remove(q)
                except ValueError:
                    pass

    def _serve_config(self):
        body = json.dumps(read_conf()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _save_config(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            data   = json.loads(body)
            merged = dict(DEFAULT_CONF)
            for k in ["threshold"]:
                if k in data:
                    merged[k] = str(float(data[k]))
            for k in ["pre_trigger_secs", "post_trigger_secs",
                      "log_rms_interval", "video_fps",
                      "frame_width", "frame_height"]:
                if k in data:
                    merged[k] = str(int(data[k]))
            write_conf(merged)
            resp = json.dumps({"ok": True,
                "message": "Settings saved. Restart the recorder to apply."}).encode()
            code = 200
        except Exception as exc:
            resp = json.dumps({"ok": False, "message": str(exc)}).encode()
            code = 400
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def _manual_trigger(self):
        try:
            # Write a trigger file that sound_recorder.py watches for
            Path("/tmp/sc_manual_trigger").touch()
            resp = json.dumps({"ok": True,
                "message": "Manual trigger sent — recording started."}).encode()
            code = 200
        except Exception as exc:
            resp = json.dumps({"ok": False, "message": str(exc)}).encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def _restart_service(self):
        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", "sound-recorder"],
                timeout=15, check=True,
            )
            resp = json.dumps({"ok": True, "message": "Recorder restarted."}).encode()
            code = 200
        except Exception as exc:
            resp = json.dumps({"ok": False, "message": str(exc)}).encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Security camera dashboard server")
    parser.add_argument("--port", type=int, default=PORT,
                        help=f"HTTP port (default: {PORT})")
    args = parser.parse_args()

    threading.Thread(target=_journald_thread, daemon=True).start()

    server = ThreadedHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"INFO: Dashboard running at http://cameradevice.local:{args.port}", flush=True)
    print(f"INFO: Snapshot source: {SNAPSHOT_PATH} (written by sound_recorder.py)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
