#!/usr/bin/env python3
"""
Sound-Triggered Video Recorder  (Nextcloud edition)
=====================================================
Continuously records from a webcam + microphone.
Saves the last 60 seconds BEFORE a loud sound, plus 2 minutes AFTER.
Finished clip is uploaded to Nextcloud via WebDAV, then deleted locally.

Requirements:
    pip install pyaudio opencv-python numpy requests

Also needs ffmpeg:
    sudo apt install ffmpeg

Credentials — set via environment variables or pass as CLI flags:

    export NC_URL="NEXTCLOUD-URL"
    export NC_USER="your_username"
    export NC_PASS="your_app_password"
    export NC_REMOTE_DIR="/CrashCam"

Usage:
    python3 sound_recorder.py --nc-user USERNAME --nc-pass YOUR_APP_PASSWORD
"""

import argparse
import collections
import contextlib
import datetime
import email.mime.multipart
import email.mime.text
import os
import queue
import smtplib
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from pathlib import Path

import cv2
import numpy as np
import pyaudio
import requests
from requests.auth import HTTPBasicAuth


# ─────────────────────────────────────────────────────────────
# TTY detection — drives which output mode we use
#   IS_TTY = True  → interactive terminal → live dashboard
#   IS_TTY = False → systemd / pipe      → plain log lines
# ─────────────────────────────────────────────────────────────
IS_TTY = sys.stdout.isatty()


# ─────────────────────────────────────────────────────────────
# Suppress ALSA/JACK spam — redirect stderr at OS level
# during PyAudio init only. No ctypes, no segfaults.
# ─────────────────────────────────────────────────────────────
@contextlib.contextmanager
def _suppress_alsa_stderr():
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_err = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(old_err, 2)
        os.close(old_err)


# ─────────────────────────────────────────────────────────────
# Configuration defaults (override via env-vars or edit here)
# ─────────────────────────────────────────────────────────────
SNAPSHOT_PATH      = "/tmp/sc_snapshot.jpg"
MANUAL_TRIGGER_PATH = "/tmp/sc_manual_trigger"
SNAPSHOT_INTERVAL  = 10   # seconds between snapshot writes
PRE_TRIGGER_SECS   = 5         # seconds of video to keep before trigger
POST_TRIGGER_SECS  = 60        # seconds to keep recording after trigger
THRESHOLD          = 0.10      # RMS amplitude threshold (0.0–1.0)
CHUNK_SECS         = 1         # size of each circular-buffer segment (seconds)
TEMP_DIR           = tempfile.gettempdir()   # /tmp — only used during encode

# Nextcloud / WebDAV — read from env, fall back to constants
NEXTCLOUD_URL        = os.environ.get("NC_URL",        "")
NEXTCLOUD_USER       = os.environ.get("NC_USER",       "")
NEXTCLOUD_PASS       = os.environ.get("NC_PASS",       "")
NEXTCLOUD_REMOTE_DIR = os.environ.get("NC_REMOTE_DIR", "/CrashCam")

# Email notifications — set via env-vars or edit here
# Generate an App Password at: https://account.microsoft.com/security
#   → Advanced security options → App passwords → Create
EMAIL_ENABLED  = os.environ.get("EMAIL_ENABLED",  "true").lower() == "true"
EMAIL_TO       = os.environ.get("EMAIL_TO",       "RECIPIENT_EMAIL_HERE")
EMAIL_FROM     = os.environ.get("EMAIL_FROM",     "SENDER_EMAIL_HERE")
EMAIL_PASS     = os.environ.get("EMAIL_PASS",     "")   # App Password
EMAIL_SMTP     = os.environ.get("EMAIL_SMTP",     "smtp-mail.outlook.com")
EMAIL_PORT     = int(os.environ.get("EMAIL_PORT", "587"))

# Audio settings
SAMPLE_RATE        = 16000   # USB webcam mic only supports 16 kHz
CHANNELS           = 1
AUDIO_FORMAT       = pyaudio.paInt16
CHUNK_FRAMES       = 2048
AUDIO_DEVICE_INDEX = 1       # [1] USB Device 0x46d:0x821

# Video settings — OV5647 Pi Camera v1
# 1296x972 uses the full sensor (2x2 binned) for maximum field of view
# Alternative: 2592x1944 @ 15fps for maximum resolution but slower
VIDEO_FPS     = 30
VIDEO_CODEC   = "mp4v"
FRAME_WIDTH   = 1296
FRAME_HEIGHT  = 972

# Camera colour tuning — adjust if image looks too warm/cool or vivid
# Saturation: 1.0 = normal, 0.8 = slightly muted, 1.2 = more vivid
# Sharpness:  1.0 = normal, 0.0 = no sharpening, 2.0 = aggressive
CAM_SATURATION = 1.0
CAM_SHARPNESS  = 1.0

# How often to print RMS log lines when running under systemd (seconds)
# Set higher to reduce journal spam — 30s is plenty for monitoring
LOG_RMS_INTERVAL = 30

# Set to False once you've tuned the smart trigger values — silences
# FILTER: lines which can flood the journal (thousands per day)
FILTER_LOGGING = False

# ─────────────────────────────────────────────────────────────


def rms(data: bytes) -> float:
    """Root-mean-square of raw PCM int16 buffer, normalised to 0–1."""
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2))) / 32768.0


def webdav_ensure_dir(base_url: str, remote_dir: str, auth: HTTPBasicAuth):
    """Create remote directory via WebDAV MKCOL. Safe if already exists."""
    parts = Path(remote_dir.strip("/")).parts
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        url = f"{base_url}/remote.php/dav/files/{auth.username}{current}"
        r = requests.request("MKCOL", url, auth=auth, timeout=15)
        if r.status_code not in (201, 301, 405):
            print(f"WARNING: MKCOL {current} returned HTTP {r.status_code}", flush=True)


def webdav_upload(local_path: str, remote_filename: str,
                  base_url: str, remote_dir: str, auth: HTTPBasicAuth,
                  content_type: str = "video/mp4") -> bool:
    """Upload a file to Nextcloud via WebDAV PUT. Returns True on success."""
    remote_path = f"{remote_dir.rstrip('/')}/{remote_filename}"
    url = f"{base_url}/remote.php/dav/files/{auth.username}{remote_path}"

    print(f"UPLOAD: {remote_path} ...", flush=True)
    try:
        with open(local_path, "rb") as fh:
            r = requests.put(
                url,
                data=fh,
                auth=auth,
                headers={"Content-Type": content_type},
                timeout=300,
            )
        if r.status_code in (200, 201, 204):
            print("UPLOAD: OK", flush=True)
            return True
        else:
            print(f"UPLOAD: FAILED HTTP {r.status_code}: {r.text[:200]}", flush=True)
            return False
    except requests.RequestException as exc:
        print(f"UPLOAD: ERROR {exc}", flush=True)
        return False


def upload_error_report(errors: list[str], base_url: str, remote_dir: str,
                        auth: HTTPBasicAuth):
    """Write a plain-text error report and upload it to Nextcloud."""
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"SELFTEST_FAILED_{ts}.txt"
    tmp_path = os.path.join(TEMP_DIR, filename)

    hostname = os.uname().nodename
    report   = "\n".join([
        "=" * 60,
        "SOUND-TRIGGERED CAMERA — SELF-TEST FAILURE REPORT",
        "=" * 60,
        f"Timestamp : {datetime.datetime.now().isoformat()}",
        f"Host      : {hostname}",
        f"Script    : {__file__}",
        "",
        "FAILED CHECKS:",
        "",
        *[f"  ✗  {e}" for e in errors],
        "",
        "The recorder did NOT start. Fix the issues above and restart the service.",
        "=" * 60,
    ])

    with open(tmp_path, "w") as fh:
        fh.write(report)

    print(f"SELFTEST: Uploading error report as {filename}", flush=True)
    try:
        webdav_upload(tmp_path, filename, base_url, remote_dir, auth,
                      content_type="text/plain")
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass


# ─────────────────────────────────────────────────────────────
# Device discovery
# ─────────────────────────────────────────────────────────────

def discover_best_video_device() -> int:
    """
    Detect the Pi camera via picamera2 and return its index.
    Falls back to 0 if detection fails.
    """
    print("DISCOVER: Scanning for Pi camera ...", flush=True)
    try:
        from picamera2 import Picamera2
        cameras = Picamera2.global_camera_info()
        if cameras:
            for cam in cameras:
                print(f"DISCOVER:   [{cam['Num']}] {cam.get('Model', 'unknown')}",
                      flush=True)
            best = cameras[0]['Num']
            print(f"DISCOVER: Best video device → Pi camera index {best}",
                  flush=True)
            return best
        else:
            print("DISCOVER: No Pi cameras found, defaulting to 0", flush=True)
            return 0
    except Exception as exc:
        print(f"DISCOVER: Pi camera probe failed ({exc}), defaulting to 0",
              flush=True)
        return 0


def discover_best_audio_device() -> tuple:
    """
    Probe all PyAudio input devices and return (index, sample_rate) of the
    best one. Prefers USB devices, then most channels. Probes supported
    sample rates per device rather than assuming 16000 Hz.
    Returns (AUDIO_DEVICE_INDEX, SAMPLE_RATE) as fallback if none found.
    """
    CANDIDATE_RATES = [48000, 44100, 32000, 22050, 16000, 8000]

    print("DISCOVER: Scanning audio devices ...", flush=True)

    best_index    = AUDIO_DEVICE_INDEX
    best_rate     = SAMPLE_RATE
    best_channels = 0
    best_is_usb   = False

    with _suppress_alsa_stderr():
        pa = pyaudio.PyAudio()

    for i in range(pa.get_device_count()):
        try:
            info     = pa.get_device_info_by_index(i)
            channels = int(info["maxInputChannels"])
            if channels == 0:
                continue

            name   = info["name"]
            is_usb = "usb" in name.lower() or "hw:" in name.lower()

            # Find the best supported sample rate for this device
            device_rate = None
            for rate in CANDIDATE_RATES:
                try:
                    pa.is_format_supported(
                        rate,
                        input_device=i,
                        input_channels=1,
                        input_format=AUDIO_FORMAT,
                    )
                    device_rate = rate
                    break   # take highest supported rate
                except Exception:
                    continue

            print(f"DISCOVER:   [{i}] {name}  "
                  f"ch={channels}  usb={is_usb}  "
                  f"best_rate={device_rate or 'none'}", flush=True)

            if device_rate is None:
                continue

            # Prefer USB; among equal preference pick more channels
            if (is_usb and not best_is_usb) or \
               (is_usb == best_is_usb and channels > best_channels):
                best_channels = channels
                best_is_usb   = is_usb
                best_index    = i
                best_rate     = device_rate

        except Exception:
            pass

    pa.terminate()
    print(f"DISCOVER: Best audio device → index {best_index} "
          f"@ {best_rate} Hz", flush=True)
    return best_index, best_rate


# ─────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────

def selftest(args, nc_auth: HTTPBasicAuth, nc_base_url: str,
             nc_remote_dir: str) -> bool:
    """
    Run startup checks for camera, microphone, and Nextcloud.
    Returns True if all pass. On failure, uploads an error report
    to Nextcloud and returns False.
    """
    errors = []
    print("SELFTEST: Running startup checks ...", flush=True)

    # ── 1. Nextcloud connectivity ─────────────────────────────
    print("SELFTEST: [1/3] Nextcloud connectivity ...", flush=True)
    try:
        url = f"{nc_base_url}/remote.php/dav/files/{nc_auth.username}/"
        r   = requests.request("PROPFIND", url, auth=nc_auth,
                               headers={"Depth": "0"}, timeout=15)
        if r.status_code == 207:
            print("SELFTEST:       Nextcloud OK", flush=True)
        else:
            errors.append(
                f"Nextcloud returned HTTP {r.status_code} for PROPFIND. "
                f"Check NC_URL, NC_USER, NC_PASS."
            )
            print(f"SELFTEST:       FAIL — HTTP {r.status_code}", flush=True)
    except requests.RequestException as exc:
        errors.append(f"Nextcloud unreachable: {exc}")
        print(f"SELFTEST:       FAIL — {exc}", flush=True)

    # ── 2. Camera ─────────────────────────────────────────────
    print(f"SELFTEST: [2/3] Pi camera (index {args.device}) ...", flush=True)
    try:
        from picamera2 import Picamera2
        cam = Picamera2(args.device)
        cfg = cam.create_video_configuration(
            main={"size": (FRAME_WIDTH, FRAME_HEIGHT), "format": "XBGR8888"})
        cam.configure(cfg)
        cam.start()
        time.sleep(1.0)
        frame = cam.capture_array("main")
        frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
        cam.stop()
        cam.close()
        time.sleep(0.5)
        if frame is None or frame.size == 0:
            errors.append("Pi camera opened but returned no frame.")
            print("SELFTEST:       FAIL — no frame captured", flush=True)
        else:
            h, w = frame.shape[:2]
            print(f"SELFTEST:       Camera OK ({w}x{h})", flush=True)
    except Exception as exc:
        errors.append(f"Pi camera error: {exc}\n{traceback.format_exc()}")
        print(f"SELFTEST:       FAIL — {exc}", flush=True)

    # ── 3. Microphone ─────────────────────────────────────────
    sample_rate = getattr(args, "sample_rate", SAMPLE_RATE)
    print(f"SELFTEST: [3/3] Microphone (device {args.audio_device} "
          f"@ {sample_rate} Hz) ...", flush=True)
    try:
        with _suppress_alsa_stderr():
            pa = pyaudio.PyAudio()

        # Check the device exists and supports our sample rate
        info = pa.get_device_info_by_index(args.audio_device)
        if info["maxInputChannels"] < 1:
            errors.append(
                f"Audio device {args.audio_device} ({info['name']}) "
                f"has no input channels."
            )
            print("SELFTEST:       FAIL — no input channels", flush=True)
        else:
            supported = pa.is_format_supported(
                sample_rate,
                input_device=args.audio_device,
                input_channels=CHANNELS,
                input_format=AUDIO_FORMAT,
            )
            if not supported:
                errors.append(
                    f"Audio device {args.audio_device} ({info['name']}) "
                    f"does not support {sample_rate} Hz / {CHANNELS}ch / int16. "
                    f"Default rate is {int(info['defaultSampleRate'])} Hz."
                )
                print(f"SELFTEST:       FAIL — {sample_rate} Hz not supported",
                      flush=True)
            else:
                # Try actually opening a short capture stream
                captured = []
                def _cb(in_data, fc, ti, st):
                    captured.append(in_data)
                    return (None, pyaudio.paContinue)

                with _suppress_alsa_stderr():
                    stream = pa.open(
                        format=AUDIO_FORMAT,
                        channels=CHANNELS,
                        rate=sample_rate,
                        input=True,
                        frames_per_buffer=CHUNK_FRAMES,
                        input_device_index=args.audio_device,
                        stream_callback=_cb,
                    )
                stream.start_stream()
                time.sleep(0.5)   # capture half a second
                stream.stop_stream()
                stream.close()

                if not captured:
                    errors.append(
                        f"Audio device {args.audio_device} ({info['name']}) "
                        f"opened but produced no audio data."
                    )
                    print("SELFTEST:       FAIL — no audio data received", flush=True)
                else:
                    level = rms(b"".join(captured))
                    print(f"SELFTEST:       Microphone OK "
                          f"(device: {info['name']}, RMS={level:.4f})", flush=True)

        pa.terminate()

    except OSError as exc:
        errors.append(
            f"Microphone error opening device {args.audio_device}: {exc}"
        )
        print(f"SELFTEST:       FAIL — {exc}", flush=True)
    except Exception as exc:
        errors.append(f"Microphone error: {exc}\n{traceback.format_exc()}")
        print(f"SELFTEST:       FAIL — {exc}", flush=True)

    # ── Result ────────────────────────────────────────────────
    if errors:
        print(f"SELFTEST: FAILED ({len(errors)} error(s)) — uploading report ...",
              flush=True)
        for e in errors:
            print(f"SELFTEST:   ✗ {e}", flush=True)
        # Best-effort upload — Nextcloud may itself have failed
        try:
            webdav_ensure_dir(nc_base_url, nc_remote_dir, nc_auth)
            upload_error_report(errors, nc_base_url, nc_remote_dir, nc_auth)
        except Exception as exc:
            print(f"SELFTEST: Could not upload error report: {exc}", flush=True)
        return False

    print("SELFTEST: All checks passed — starting recorder.", flush=True)
    return True


# ─────────────────────────────────────────────────────────────
# Email notification
# ─────────────────────────────────────────────────────────────

def send_notification(filename: str, duration: float, size_mb: float,
                      nc_base_url: str, nc_remote_dir: str):
    """Send an email notification after a successful upload. Non-fatal on error."""
    if not EMAIL_ENABLED:
        return
    if not EMAIL_PASS:
        print("EMAIL: Skipped — EMAIL_PASS not set.", flush=True)
        return

    try:
        ts_nice  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hostname = os.uname().nodename
        nc_link  = f"{nc_base_url}/index.php/apps/files/?dir={nc_remote_dir}"

        subject = f"[CrashCam] Event recorded — {ts_nice}"

        body_text = "\n".join([
            "A sound event was detected and recorded.",
            "",
            f"  Time     : {ts_nice}",
            f"  File     : {filename}",
            f"  Duration : {duration:.1f}s",
            f"  Size     : {size_mb:.1f} MB",
            "",
            f"View in Nextcloud: {nc_link}",
            "",
            "— CrashCam",
        ])

        body_html = f"""
<html><body style="font-family:sans-serif;color:#222;max-width:480px">
  <h2 style="color:#c0392b;margin-bottom:4px">⚠ Event recorded</h2>
  <p style="color:#666;margin-top:0">{ts_nice}</p>
  <table style="border-collapse:collapse;width:100%;margin:16px 0">
    <tr><td style="padding:6px 12px 6px 0;color:#888">File</td>
        <td style="padding:6px 0"><strong>{filename}</strong></td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#888">Duration</td>
        <td style="padding:6px 0">{duration:.1f}s</td></tr>
    <tr><td style="padding:6px 12px 6px 0;color:#888">Size</td>
        <td style="padding:6px 0">{size_mb:.1f} MB</td></tr>
  </table>
  <a href="{nc_link}"
     style="display:inline-block;padding:10px 20px;background:#3ddc84;
            color:#000;border-radius:5px;text-decoration:none;font-weight:600">
    Open in Nextcloud →
  </a>
  <p style="color:#aaa;font-size:11px;margin-top:24px">Sent by CrashCam</p>
</body></html>"""

        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(email.mime.text.MIMEText(body_text, "plain"))
        msg.attach(email.mime.text.MIMEText(body_html,  "html"))

        print(f"EMAIL: Sending notification to {EMAIL_TO} ...", flush=True)
        with smtplib.SMTP(EMAIL_SMTP, EMAIL_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(EMAIL_FROM, EMAIL_PASS)
            smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print("EMAIL: Sent OK", flush=True)

    except Exception as exc:
        # Never let an email failure crash the recorder
        print(f"EMAIL: Failed — {exc}", flush=True)


# ─────────────────────────────────────────────────────────────


class CircularChunkBuffer:
    """Thread-safe ring buffer of (video_frames, audio_bytes) chunks."""

    def __init__(self, max_secs: int, chunk_secs: float):
        self.max_chunks = int(max_secs / chunk_secs) + 2
        self._buf: collections.deque = collections.deque(maxlen=self.max_chunks)
        self._lock = threading.Lock()

    def push(self, video_frames: list, audio_bytes: bytes):
        with self._lock:
            self._buf.append((video_frames, audio_bytes))

    def snapshot(self) -> list:
        with self._lock:
            return list(self._buf)


class Recorder:
    def __init__(self, args):
        self.threshold    = args.threshold
        self.video_device = args.device
        self.audio_device = args.audio_device
        self.sample_rate  = getattr(args, "sample_rate", SAMPLE_RATE)
        self.pre_secs     = PRE_TRIGGER_SECS
        self.post_secs    = POST_TRIGGER_SECS

        # Nextcloud credentials
        nc_user = args.nc_user or NEXTCLOUD_USER
        nc_pass = args.nc_pass or NEXTCLOUD_PASS
        if not nc_user or not nc_pass:
            print("ERROR: Nextcloud credentials not set. "
                  "Set NC_USER/NC_PASS env-vars or use --nc-user/--nc-pass.",
                  file=sys.stderr, flush=True)
            sys.exit(1)
        self.nc_auth       = HTTPBasicAuth(nc_user, nc_pass)
        self.nc_base_url   = (args.nc_url or NEXTCLOUD_URL).rstrip("/")
        self.nc_remote_dir = args.nc_dir or NEXTCLOUD_REMOTE_DIR

        # Run self-test before doing anything else
        if not selftest(args, self.nc_auth, self.nc_base_url, self.nc_remote_dir):
            sys.exit(1)

        print(f"INFO: Checking Nextcloud folder '{self.nc_remote_dir}' ...", flush=True)
        webdav_ensure_dir(self.nc_base_url, self.nc_remote_dir, self.nc_auth)
        print(f"INFO: Nextcloud OK — {self.nc_base_url}{self.nc_remote_dir}", flush=True)

        self.buffer          = CircularChunkBuffer(self.pre_secs, CHUNK_SECS)
        self.audio_q: queue.Queue = queue.Queue()

        self._stop           = threading.Event()
        self._triggered      = threading.Event()
        self._trigger_time   = 0.0

        self._chunk_frames: list  = []   # list of (frame, timestamp)
        self._chunk_audio:  bytes = b""
        self._chunk_lock  = threading.Lock()
        self._chunk_start = time.monotonic()

        self._post_frames: list  = []   # list of (frame, timestamp)
        self._post_audio:  bytes = b""

        # Shared state for dashboard / log output
        self._last_rms:    float = 0.0
        self._status_msg:  str   = ""
        self._encode_pct:  int   = -1   # -1 = not encoding

        # RMS log — list of (wall_datetime, rms_value, phase)
        # phase: "pre" = before trigger, "post" = after trigger
        self._rms_log:      list  = []
        self._rms_log_lock        = threading.Lock()

    # ── Audio callback ────────────────────────────────────────

    def _audio_callback(self, in_data, frame_count, time_info, status):
        self.audio_q.put(in_data)
        return (None, pyaudio.paContinue)

    # ── Audio consumer thread ─────────────────────────────────

    def _audio_thread(self):
        last_log = time.monotonic()

        while not self._stop.is_set():
            try:
                data = self.audio_q.get(timeout=0.1)
            except queue.Empty:
                continue

            level = rms(data)
            self._last_rms = level
            now_wall = datetime.datetime.now()

            if self._triggered.is_set():
                self._post_audio += data
                elapsed   = time.monotonic() - self._trigger_time
                remaining = max(0, self.post_secs - elapsed)
                self._status_msg = (
                    f"POST-TRIGGER  {elapsed:.0f}s elapsed  "
                    f"{remaining:.0f}s remaining"
                )
                with self._rms_log_lock:
                    self._rms_log.append((now_wall, level, "post"))
            else:
                with self._chunk_lock:
                    self._chunk_audio += data
                buffered = min(len(self.buffer.snapshot()), self.pre_secs)
                self._status_msg = f"MONITORING  buffer {buffered}s / {self.pre_secs}s"
                with self._rms_log_lock:
                    self._rms_log.append((now_wall, level, "pre"))

            # Simple RMS threshold trigger
            if level >= self.threshold and not self._triggered.is_set():
                self._trigger_time = time.monotonic()
                self._triggered.set()
                print(f"TRIGGER: Sound detected RMS={level:.4f} "
                      f"(threshold={self.threshold})", flush=True)

            # Manual trigger — check for trigger file written by dashboard
            if not self._triggered.is_set() and \
                    os.path.exists(MANUAL_TRIGGER_PATH):
                try:
                    os.remove(MANUAL_TRIGGER_PATH)
                except FileNotFoundError:
                    pass
                self._trigger_time = time.monotonic()
                self._triggered.set()
                print("TRIGGER: Manual trigger activated from dashboard",
                      flush=True)

            # Plain-text RMS log line for systemd
            if not IS_TTY:
                now = time.monotonic()
                if now - last_log >= LOG_RMS_INTERVAL:
                    last_log = now
                    print(f"RMS: {level:.4f}  status: {self._status_msg}", flush=True)

    # ── Video capture thread ──────────────────────────────────

    def _video_thread(self):
        from picamera2 import Picamera2
        try:
            cam = Picamera2(self.video_device)
            cfg = cam.create_video_configuration(
                main={"size": (FRAME_WIDTH, FRAME_HEIGHT), "format": "XBGR8888"},
                controls={
                    "FrameRate":   float(VIDEO_FPS),
                    "AwbMode":     0,
                    "Saturation":  CAM_SATURATION,
                    "Sharpness":   CAM_SHARPNESS,
                },
            )
            cam.configure(cfg)
            cam.start()
        except Exception as exc:
            print(f"ERROR: Could not open Pi camera: {exc}",
                  file=sys.stderr, flush=True)
            self._stop.set()
            return

        print(f"INFO: Pi camera opened ({FRAME_WIDTH}x{FRAME_HEIGHT} "
              f"@ {VIDEO_FPS} fps)", flush=True)

        time.sleep(3.0)   # OV5647 needs time for AWB and AEC to settle

        last_snapshot = 0.0
        started_at    = time.monotonic()
        WARMUP_SECS   = 2.0

        while not self._stop.is_set():
            try:
                raw   = cam.capture_array("main")
                # OV5647 via picamera2 XBGR8888: BGRA2BGR gives swapped R/B
                # Use RGB2BGR instead to get correct colours
                frame = cv2.cvtColor(raw, cv2.COLOR_RGBA2BGR)
            except Exception as exc:
                print(f"WARNING: Frame capture failed: {exc}", flush=True)
                time.sleep(0.1)
                continue

            now      = time.monotonic()
            now_wall = datetime.datetime.now()

            # ── Write snapshot every SNAPSHOT_INTERVAL seconds ──
            if (now - started_at >= WARMUP_SECS and
                    now - last_snapshot >= SNAPSHOT_INTERVAL):
                mean_brightness = frame.mean()
                if mean_brightness >= 5.0:
                    last_snapshot = now
                    try:
                        ts_str = now_wall.strftime("%Y-%m-%d  %H:%M:%S")
                        snap   = frame.copy()
                        sh, sw = snap.shape[:2]
                        font   = cv2.FONT_HERSHEY_SIMPLEX
                        fscale, thick, pad = 0.5, 1, 6
                        (_, th), bl = cv2.getTextSize(ts_str, font, fscale, thick)
                        bar_h = th + bl + pad * 2
                        roi   = snap[sh - bar_h:sh, 0:sw]
                        black = roi.copy(); black[:] = (0, 0, 0)
                        cv2.addWeighted(black, 0.6, roi, 0.4, 0, roi)
                        snap[sh - bar_h:sh, 0:sw] = roi
                        cv2.putText(snap, ts_str, (pad, sh - pad - bl),
                                    font, fscale, (255, 255, 255), thick,
                                    cv2.LINE_AA)
                        tmp_snap = SNAPSHOT_PATH + ".tmp"
                        ok, buf  = cv2.imencode(".jpg", snap,
                                               [cv2.IMWRITE_JPEG_QUALITY, 80])
                        if ok:
                            with open(tmp_snap, "wb") as fh:
                                fh.write(buf.tobytes())
                            os.replace(tmp_snap, SNAPSHOT_PATH)
                    except Exception as exc:
                        print(f"WARNING: Snapshot write failed: {exc}",
                              flush=True)

            if self._triggered.is_set():
                # Compress frame to JPEG before storing — reduces RAM by ~20x
                # Raw 1280x720 BGR = 2.8MB, JPEG ~120KB
                ok, buf = cv2.imencode(".jpg", frame,
                                       [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    self._post_frames.append((buf.tobytes(), now, now_wall))

                if now - self._trigger_time >= self.post_secs:
                    with self._rms_log_lock:
                        rms_snapshot = list(self._rms_log)
                        self._rms_log = []
                    t = threading.Thread(
                        target=self._finalize_and_upload,
                        args=(list(self._post_frames), self._post_audio,
                              rms_snapshot),
                        daemon=True,
                    )
                    t.start()
                    self._triggered.clear()
                    self._post_frames = []
                    self._post_audio  = b""
            else:
                with self._chunk_lock:
                    ok, buf = cv2.imencode(".jpg", frame,
                                           [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if ok:
                        self._chunk_frames.append((buf.tobytes(), now, now_wall))

                if now - self._chunk_start >= CHUNK_SECS:
                    with self._chunk_lock:
                        frames = self._chunk_frames[:]
                        audio  = self._chunk_audio
                        self._chunk_frames = []
                        self._chunk_audio  = b""
                        self._chunk_start  = now
                    self.buffer.push(frames, audio)

        cam.stop()
        cam.close()

    # ── Encode + upload (runs in its own thread) ──────────────

    def _finalize_and_upload(self, post_frames: list, post_audio: bytes,
                             rms_log: list):
        ts          = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        vid_tmp     = os.path.join(TEMP_DIR, f"sc_{ts}_video.mp4")
        aud_tmp     = os.path.join(TEMP_DIR, f"sc_{ts}_audio.wav")
        merged_tmp  = os.path.join(TEMP_DIR, f"sc_{ts}.mp4")
        rms_tmp     = os.path.join(TEMP_DIR, f"sc_{ts}_rms.txt")
        wav_out_tmp = os.path.join(TEMP_DIR, f"sc_{ts}_event.wav")
        remote_name = f"event_{ts}.mp4"
        rms_name    = f"event_{ts}_rms.txt"
        wav_name    = f"event_{ts}.wav"

        try:
            pre_chunks = self.buffer.snapshot()
            all_items: list   = []   # list of (frame, monotonic_ts, wall_dt)
            all_audio:  bytes = b""
            for (fts_list, abytes) in pre_chunks:
                all_items.extend(fts_list)
                all_audio += abytes
            all_items.extend(post_frames)
            all_audio += post_audio

            if not all_items:
                print("WARNING: No frames captured, skipping.", flush=True)
                return

            # ── Compute actual FPS from wall-clock timestamps ──
            jpg_bufs    = [f  for f, _, _w in all_items]
            timestamps  = [t  for _f, t, _w in all_items]
            wall_times  = [w  for _f, _t, w in all_items]
            total_frames = len(jpg_bufs)

            if total_frames >= 2:
                elapsed_real = timestamps[-1] - timestamps[0]
                actual_fps   = (total_frames - 1) / elapsed_real if elapsed_real > 0 else VIDEO_FPS
            else:
                actual_fps   = VIDEO_FPS
            actual_fps = max(1.0, min(actual_fps, 60.0))
            duration   = total_frames / actual_fps

            print(f"ENCODE: Starting {duration:.1f}s clip "
                  f"({total_frames} frames @ {actual_fps:.1f} fps actual)",
                  flush=True)

            # Decode first frame to get dimensions
            first_frame = cv2.imdecode(
                np.frombuffer(jpg_bufs[0], dtype=np.uint8), cv2.IMREAD_COLOR)
            fh, fw = first_frame.shape[:2]

            # ── Write video — decode + stamp one frame at a time ──
            # Never holds more than one raw frame in RAM at once
            font       = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.55
            thickness  = 1
            pad        = 8

            fourcc = cv2.VideoWriter_fourcc(*VIDEO_CODEC)
            vw     = cv2.VideoWriter(vid_tmp, fourcc, actual_fps, (fw, fh))

            for jpg_buf, wall_dt in zip(jpg_bufs, wall_times):
                f = cv2.imdecode(
                    np.frombuffer(jpg_buf, dtype=np.uint8), cv2.IMREAD_COLOR)
                if f is None:
                    continue

                # Burn timestamp overlay
                ts_str = wall_dt.strftime("%Y-%m-%d  %H:%M:%S")
                (_, th), baseline = cv2.getTextSize(
                    ts_str, font, font_scale, thickness)
                bar_h = th + baseline + pad * 2
                roi   = f[fh - bar_h:fh, 0:fw]
                black = roi.copy(); black[:] = (0, 0, 0)
                cv2.addWeighted(black, 0.55, roi, 0.45, 0, roi)
                f[fh - bar_h:fh, 0:fw] = roi
                cv2.putText(f, ts_str, (pad, fh - pad - baseline),
                            font, font_scale, (255, 255, 255),
                            thickness, cv2.LINE_AA)
                vw.write(f)

            vw.release()

            # Write raw audio
            wf = wave.open(aud_tmp, "wb")
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(all_audio)
            wf.close()

            # Merge with ffmpeg — progress piped to stdout
            thumb_time = min(10.0, duration - 0.5)
            cmd = [
                "ffmpeg", "-y",
                "-i", vid_tmp,
                "-i", aud_tmp,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-c:a", "aac",
                "-shortest",
                "-progress", "pipe:1",
                "-nostats",
                merged_tmp,
            ]
            self._encode_pct = 0
            self._status_msg = "ENCODING 0%"
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
            encoded      = 0
            last_log_pct = -1
            for line in proc.stdout:
                if line.strip().startswith("frame="):
                    try:
                        encoded = int(line.strip().split("=")[1])
                    except ValueError:
                        pass
                    pct = min(int(encoded / max(total_frames, 1) * 100), 100)
                    self._encode_pct = pct
                    self._status_msg = f"ENCODING {pct}%"

                    # In systemd mode log every 25%
                    if not IS_TTY and pct - last_log_pct >= 25:
                        last_log_pct = pct
                        print(f"ENCODE: {pct}%", flush=True)

            proc.wait()
            self._encode_pct = 100
            self._status_msg = "ENCODING 100%"
            print("ENCODE: 100% complete", flush=True)

            if proc.returncode != 0:
                print("ERROR: ffmpeg failed", flush=True)
                return

            # ── Embed thumbnail at 10s into the clip ──────────
            thumb_tmp    = os.path.join(TEMP_DIR, f"sc_{ts}_thumb.jpg")
            thumbed_tmp  = os.path.join(TEMP_DIR, f"sc_{ts}_thumbed.mp4")
            thumb_time   = min(10.0, max(0.0, duration - 0.5))
            try:
                # Extract frame at thumb_time
                subprocess.run([
                    "ffmpeg", "-y",
                    "-ss", str(thumb_time),
                    "-i", merged_tmp,
                    "-vframes", "1",
                    "-q:v", "2",
                    thumb_tmp,
                ], capture_output=True, check=True)

                # Embed it as the video thumbnail
                subprocess.run([
                    "ffmpeg", "-y",
                    "-i", merged_tmp,
                    "-i", thumb_tmp,
                    "-map", "0",
                    "-map", "1",
                    "-c", "copy",
                    "-c:v:1", "mjpeg",
                    "-disposition:v:1", "attached_pic",
                    thumbed_tmp,
                ], capture_output=True, check=True)

                # Replace merged with thumbed version
                os.replace(thumbed_tmp, merged_tmp)
                print("ENCODE: Thumbnail embedded at "
                      f"{thumb_time:.0f}s", flush=True)
            except Exception as exc:
                print(f"WARNING: Thumbnail embed failed: {exc}", flush=True)
            finally:
                for p in (thumb_tmp, thumbed_tmp):
                    try:
                        os.remove(p)
                    except FileNotFoundError:
                        pass

            # Upload
            success = webdav_upload(
                merged_tmp, remote_name,
                self.nc_base_url, self.nc_remote_dir, self.nc_auth,
            )

            if success:
                size_mb = os.path.getsize(merged_tmp) / 1_048_576
                print(f"SAVED: {remote_name}  {size_mb:.1f} MB  {duration:.1f}s",
                      flush=True)
                self._status_msg = f"LAST UPLOAD: {remote_name}  {size_mb:.1f} MB"
                send_notification(remote_name, duration, size_mb,
                                  self.nc_base_url, self.nc_remote_dir)

                # ── Write and upload RMS log ───────────────────
                try:
                    # Find the trigger point — first "post" entry
                    trigger_idx  = next(
                        (i for i, (_, _, p) in enumerate(rms_log) if p == "post"),
                        len(rms_log)
                    )
                    trigger_rms  = rms_log[trigger_idx][1] if trigger_idx < len(rms_log) else 0
                    trigger_time = rms_log[trigger_idx][0] if trigger_idx < len(rms_log) else None

                    # Trim to 5 seconds before and after the trigger
                    WINDOW_SECS = 5.0
                    if trigger_time:
                        windowed = [
                            (wall_dt, val, phase)
                            for wall_dt, val, phase in rms_log
                            if abs((wall_dt - trigger_time).total_seconds()) <= WINDOW_SECS
                        ]
                    else:
                        windowed = rms_log

                    lines = [
                        "=" * 60,
                        f"RMS LOG — {remote_name}",
                        "=" * 60,
                        f"Threshold      : {self.threshold}",
                        f"Trigger RMS    : {trigger_rms:.4f}",
                        f"Trigger time   : {trigger_time.strftime('%Y-%m-%d %H:%M:%S') if trigger_time else 'unknown'}",
                        f"Window shown   : {WINDOW_SECS:.0f}s before and after trigger",
                        f"Readings shown : {len(windowed)} of {len(rms_log)} total",
                        "",
                        f"{'Time':<25} {'RMS':>8}  {'Phase':<6}  {'vs threshold':>14}",
                        "-" * 60,
                    ]
                    for wall_dt, val, phase in windowed:
                        marker = " ← TRIGGER" if (phase == "post" and
                                  rms_log.index((wall_dt, val, phase)) == trigger_idx) else ""
                        ratio  = val / self.threshold if self.threshold > 0 else 0
                        lines.append(
                            f"{wall_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]:<25} "
                            f"{val:>8.4f}  {phase:<6}  {ratio:>13.2f}x{marker}"
                        )
                    lines += [
                        "-" * 60,
                        f"Min RMS (full clip) : {min(v for _, v, _ in rms_log):.4f}",
                        f"Max RMS (full clip) : {max(v for _, v, _ in rms_log):.4f}",
                        f"Avg RMS (full clip) : {sum(v for _, v, _ in rms_log) / len(rms_log):.4f}",
                        f"Threshold           : {self.threshold}",
                        "=" * 60,
                    ]

                    with open(rms_tmp, "w") as fh:
                        fh.write("\n".join(lines) + "\n")

                    webdav_upload(rms_tmp, rms_name,
                                  self.nc_base_url, self.nc_remote_dir,
                                  self.nc_auth, content_type="text/plain")
                    print(f"SAVED: {rms_name}", flush=True)
                except Exception as exc:
                    print(f"WARNING: RMS log failed: {exc}", flush=True)
                finally:
                    try:
                        os.remove(rms_tmp)
                    except FileNotFoundError:
                        pass

                # ── Save and upload event waveform (.wav) ─────
                try:
                    # Write a clean wav with just the trigger window
                    # (1s before trigger + full post-trigger audio)
                    # This is used for sound profile analysis
                    wf2 = wave.open(wav_out_tmp, "wb")
                    wf2.setnchannels(CHANNELS)
                    wf2.setsampwidth(2)
                    wf2.setframerate(self.sample_rate)
                    wf2.writeframes(all_audio)
                    wf2.close()
                    webdav_upload(wav_out_tmp, wav_name,
                                  self.nc_base_url, self.nc_remote_dir,
                                  self.nc_auth, content_type="audio/wav")
                    print(f"SAVED: {wav_name}", flush=True)
                except Exception as exc:
                    print(f"WARNING: WAV upload failed: {exc}", flush=True)
                finally:
                    try:
                        os.remove(wav_out_tmp)
                    except FileNotFoundError:
                        pass
            else:
                fallback = os.path.join(TEMP_DIR, remote_name)
                os.rename(merged_tmp, fallback)
                print(f"WARNING: Upload failed — clip kept at {fallback}", flush=True)
                self._status_msg = f"UPLOAD FAILED — saved to {fallback}"
                merged_tmp = None

            self._encode_pct = -1  # encoding done

        finally:
            for path in (vid_tmp, aud_tmp, rms_tmp, wav_out_tmp):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            if merged_tmp and os.path.exists(merged_tmp):
                try:
                    os.remove(merged_tmp)
                except FileNotFoundError:
                    pass

    # ── Live dashboard (TTY mode only) ────────────────────────

    def _draw_dashboard(self):
        """Redraw 3-line live dashboard in place. Only called when IS_TTY."""
        METER_WIDTH = 30
        rms_val   = self._last_rms
        threshold = self.threshold

        filled = int(min(rms_val / max(threshold * 2, 0.0001), 1.0) * METER_WIDTH)
        bar = ""
        for i in range(METER_WIDTH):
            frac       = (i + 1) / METER_WIDTH
            level_at_i = frac * threshold * 2
            if i < filled:
                if level_at_i < threshold * 0.75:
                    bar += "█"
                elif level_at_i < threshold:
                    bar += "▓"
                else:
                    bar += "▒"
            else:
                bar += "░"

        marker          = int(0.5 * METER_WIDTH)
        bar_with_marker = bar[:marker] + "|" + bar[marker:]

        # Second line: encoding progress bar or status
        if self._encode_pct >= 0:
            done      = int(self._encode_pct / 100 * METER_WIDTH)
            enc_bar   = "█" * done + "░" * (METER_WIDTH - done)
            status_ln = f"⚙️  ENCODING [{enc_bar}] {self._encode_pct:3d}%"
        else:
            status_ln = self._status_msg or "🟢  MONITORING"

        now_str    = datetime.datetime.now().strftime("%H:%M:%S")
        rms_str    = f"{rms_val:.4f}"
        thresh_str = f"{threshold:.4f}"

        print(
            f"\033[3A"
            f"\033[K🔊  Level  [{bar_with_marker}]  "
            f"{rms_str}  (trigger ≥ {thresh_str})\n"
            f"\033[K{status_ln}\n"
            f"\033[K🕐  {now_str}",
            end="", flush=True,
        )

    # ── Main entry point ──────────────────────────────────────

    def run(self):
        with _suppress_alsa_stderr():
            pa = pyaudio.PyAudio()

        open_kwargs = dict(
            format=AUDIO_FORMAT,
            channels=CHANNELS,
            rate=self.sample_rate,
            input=True,
            frames_per_buffer=CHUNK_FRAMES,
            stream_callback=self._audio_callback,
        )
        if self.audio_device is not None:
            open_kwargs["input_device_index"] = self.audio_device

        with _suppress_alsa_stderr():
            stream = pa.open(**open_kwargs)
        stream.start_stream()

        audio_t = threading.Thread(target=self._audio_thread, daemon=True)
        video_t = threading.Thread(target=self._video_thread, daemon=True)
        audio_t.start()
        video_t.start()

        print(f"INFO: Recording started  threshold={self.threshold}  "
              f"pre={self.pre_secs}s  post={self.post_secs}s  "
              f"dashboard=http://cameradevice.local:6464", flush=True)

        if IS_TTY:
            print("    Press Ctrl+C to stop.")
            print()  # reserve 3 lines for dashboard
            print()
            print()
            try:
                while not self._stop.is_set():
                    self._draw_dashboard()
                    time.sleep(0.1)
            except KeyboardInterrupt:
                print("\n\n\n🛑  Stopping…")
                self._stop.set()
        else:
            # Systemd mode — just keep alive; output is handled by threads
            try:
                while not self._stop.is_set():
                    time.sleep(1)
            except KeyboardInterrupt:
                self._stop.set()

        stream.stop_stream()
        stream.close()
        pa.terminate()
        audio_t.join(timeout=3)
        video_t.join(timeout=3)
        print("INFO: Stopped.", flush=True)


# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sound-triggered video recorder — uploads clips to Nextcloud.")
    parser.add_argument(
        "--threshold", type=float, default=THRESHOLD,
        help=f"RMS trigger level 0.0–1.0 (default: {THRESHOLD})")
    parser.add_argument(
        "--device", type=int, default=0,
        help="OpenCV camera index (default: 0)")
    parser.add_argument(
        "--audio-device", type=int, default=AUDIO_DEVICE_INDEX,
        help=f"PyAudio input device index (default: {AUDIO_DEVICE_INDEX}). "
             "Run --list-audio to see options.")
    parser.add_argument(
        "--nc-url", default=None,
        help=f"Nextcloud base URL (default: {NEXTCLOUD_URL})")
    parser.add_argument(
        "--nc-user", default=None,
        help="Nextcloud username (or set NC_USER env-var)")
    parser.add_argument(
        "--nc-pass", default=None,
        help="Nextcloud app-password (or set NC_PASS env-var)")
    parser.add_argument(
        "--nc-dir", default=None,
        help=f"Remote folder in Nextcloud (default: {NEXTCLOUD_REMOTE_DIR})")
    parser.add_argument(
        "--list-audio", action="store_true",
        help="List audio input devices and exit")

    args = parser.parse_args()

    # Auto-discover best devices unless user explicitly overrode them
    if args.device == 0:   # 0 is the default — treat as "auto"
        args.device = discover_best_video_device()
    if args.audio_device == AUDIO_DEVICE_INDEX:
        args.audio_device, args.sample_rate = discover_best_audio_device()
    else:
        args.sample_rate = SAMPLE_RATE

    print(f"DISCOVER: Using video device {args.device}, "
          f"audio device {args.audio_device} @ {args.sample_rate} Hz",
          flush=True)

    if args.list_audio:
        with _suppress_alsa_stderr():
            pa = pyaudio.PyAudio()
        print("Available audio input devices:")
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                print(f"  [{i}] {info['name']}")
        pa.terminate()
        return

    Recorder(args).run()


if __name__ == "__main__":
    main()
