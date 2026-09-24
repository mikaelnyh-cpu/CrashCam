# CrashCam

A Raspberry Pi security camera that continuously buffers video and audio, and automatically saves a clip whenever a loud sound is detected. Each clip contains a short pre-trigger buffer and a configurable post-trigger recording. Finished clips — along with an RMS log and audio waveform — are uploaded directly to a Nextcloud instance via WebDAV. Nothing is stored permanently on the Pi.

---

## How it works

```
──── circular RAM buffer (compressed) ────►│◄──── post-trigger recording ────►
             (pre-trigger secs)           BANG!       (post-trigger secs)
                                            ↑
                               ffmpeg encodes to .mp4
                               thumbnail embedded at 10s
                                            ↓
                        uploads to Nextcloud, deletes locally
```

Each event produces three files in Nextcloud:
- `event_YYYYMMDD_HHMMSS.mp4` — video with burned-in timestamp
- `event_YYYYMMDD_HHMMSS.wav` — full audio waveform for analysis
- `event_YYYYMMDD_HHMMSS_rms.txt` — RMS log around the trigger point

---

## Hardware

- Raspberry Pi 4 
- Raspberry Pi Camera v1 (OV5647) via CSI connector
- USB microphone or USB webcam with built-in mic
- MicroSD card (32GB+ recommended)

---

## System dependencies

```bash
sudo apt update
sudo apt install -y ffmpeg portaudio19-dev python3-full python3-opencv \
                   libcap-dev screen v4l-utils
```

---

## Python environment

The venv must use `--system-site-packages` so it can access `libcamera` which cannot be pip-installed:

```bash
cd /home/pi   # or your home directory
python3 -m venv cam-env --system-site-packages
cam-env/bin/pip install pyaudio numpy opencv-python requests picamera2
```

Verify all imports:
```bash
cam-env/bin/python3 -c "import cv2, numpy, pyaudio, requests, picamera2; print('All imports OK')"
```

---

## Nextcloud setup

The system uploads via WebDAV. You need:
- A running Nextcloud instance
- An **App Password** (never use your real login password)
  - Settings → Security → Devices & sessions → Create new app password
- The upload folder created on first run automatically

---

## Credentials — environment file

Create a protected credentials file:

```bash
sudo nano /etc/crashcam.env
```

```ini
NC_URL=https://your.nextcloud.instance
NC_USER=your_username
NC_PASS=your_app_password
NC_REMOTE_DIR=/CrashCam
DASHBOARD_PASS=your_dashboard_password
```

Lock it down:
```bash
sudo chmod 600 /etc/crashcam.env
```

---

## Installation

Copy the three files to your home directory:
```
sound_recorder.py   — main recorder process
camera_server.py    — dashboard web server
dashboard.html      — dashboard UI
```

---

## Systemd services

### Recorder service

```bash
sudo nano /etc/systemd/system/sound-recorder.service
```

```ini
[Unit]
Description=CrashCam Sound-Triggered Video Recorder
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/crashcam.env
ExecStart=/home/pi/cam-env/bin/python3 /home/pi/sound_recorder.py
WorkingDirectory=/home/pi
Restart=always
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
```

### Dashboard service

```bash
sudo nano /etc/systemd/system/camera-dashboard.service
```

```ini
[Unit]
Description=CrashCam Dashboard
After=network-online.target sound-recorder.service
Wants=network-online.target

[Service]
EnvironmentFile=/etc/crashcam.env
ExecStart=/home/pi/cam-env/bin/python3 /home/pi/camera_server.py
WorkingDirectory=/home/pi
Restart=always
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
```

Enable and start both:
```bash
sudo systemctl daemon-reload
sudo systemctl enable sound-recorder camera-dashboard
sudo systemctl start sound-recorder camera-dashboard
```

---

## Dashboard

Access at **http://[pi-ip]:6464** from any browser on the same network.

Tabs:
- **Camera** — snapshot updated every 10 seconds, live RMS meter
- **Recordings** — browse, play and download clips from Nextcloud
- **Log** — live colour-coded event log
- **Settings** — password protected; edit threshold and timing, restart recorder

The **⏺ Manual Trigger** button in the top bar starts a recording immediately regardless of sound level.

You can also trigger from the command line:
```bash
touch /tmp/sc_manual_trigger
```

---

## Configuration

Key constants near the top of `sound_recorder.py`:

| Constant | Default | Description |
|---|---|---|
| `THRESHOLD` | `0.10` | RMS trigger level (0.0–1.0). Lower = more sensitive |
| `PRE_TRIGGER_SECS` | `5` | Seconds of buffer kept before trigger |
| `POST_TRIGGER_SECS` | `60` | Seconds recorded after trigger |
| `FRAME_WIDTH` | `1296` | Capture width in pixels |
| `FRAME_HEIGHT` | `972` | Capture height in pixels |
| `VIDEO_FPS` | `30` | Target frame rate |
| `LOG_RMS_INTERVAL` | `30` | Seconds between RMS log entries in systemd |
| `CAM_SATURATION` | `1.0` | Camera colour saturation (1.0 = normal) |
| `CAM_SHARPNESS` | `1.0` | Camera sharpness (1.0 = normal) |

---

## Self-test

On every start the recorder runs three checks:

1. **Nextcloud** — verifies connectivity and credentials
2. **Camera** — opens the Pi camera and captures a test frame
3. **Microphone** — opens the audio stream and confirms data flows

If any check fails, a `SELFTEST_FAILED_[timestamp].txt` report is uploaded to Nextcloud and the recorder exits. Check the log:

```bash
journalctl -u sound-recorder | grep SELFTEST
```

---

## Device discovery

On each start the recorder automatically:
- Detects the Pi camera via `picamera2`
- Scans all audio devices and picks the best USB mic (highest sample rate, most channels)

No manual device index configuration needed.

---

## Disk usage

| Phase | Location | Cleaned up? |
|---|---|---|
| Frame buffer (compressed JPEG) | RAM only | Auto-rotated |
| Audio buffer | RAM only | Cleared after encode |
| ffmpeg encode | `/tmp` | Deleted after upload |
| Finished clip + wav + rms | Nextcloud only | Stays on Nextcloud |

---

## Journal limits

To prevent log growth from consuming RAM:

```bash
sudo nano /etc/systemd/journald.conf
```

```ini
[Journal]
SystemMaxUse=50M
SystemKeepFree=200M
MaxRetentionSec=3day
RateLimitInterval=30s
RateLimitBurst=100
```

```bash
sudo systemctl restart systemd-journald
```

---

## Daily reboot (optional)

To keep memory clean, add a daily reboot at 04:00:

```bash
sudo crontab -e
```

```
0 4 * * * /sbin/reboot
```

---

## Common commands

```bash
# Service control
sudo systemctl restart sound-recorder
sudo systemctl restart camera-dashboard
sudo systemctl status sound-recorder

# Live log
journalctl -u sound-recorder -f

# Manual trigger
touch /tmp/sc_manual_trigger

# Free swap if memory pressure builds
sudo swapoff -a && sudo swapon -a

# Check memory
free -h

# Vacuum journal
sudo journalctl --vacuum-size=50M
```

---

## Supported cameras

The recorder is written for the **Raspberry Pi Camera v1 (OV5647)** using `picamera2`. The OV5647 supports:

| Mode | Resolution | FPS | Notes |
|---|---|---|---|
| Full sensor (binned) | 1296×972 | 46 | **Recommended** — widest FOV |
| 1080p crop | 1920×1080 | 32 | 16:9 crop, narrower FOV |
| Full resolution | 2592×1944 | 15 | Maximum detail, slow |

Set `FRAME_WIDTH` / `FRAME_HEIGHT` in `sound_recorder.py` to switch modes.

The colour conversion uses `cv2.COLOR_RGBA2BGR` which is correct for the OV5647's `XBGR8888` ISP output. If you swap to a different Pi camera module, this may need adjusting.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No module named 'libcamera'` | Rebuild venv with `--system-site-packages` |
| Camera shows wrong colours | Check colour conversion in video thread — OV5647 needs `COLOR_RGBA2BGR` |
| `Invalid sample rate` | Mic doesn't support the discovered rate — device discovery will find the right one on restart |
| Pi freezes after hours | Check `free -h` — frame buffer may be too large; ensure JPEG compression is active in video thread |
| Dashboard shows "No signal" | Wait 15s after restart for first snapshot; check `ls -lh /tmp/sc_snapshot.jpg` |
| Upload fails HTTP 503 | Nextcloud server-side encryption not ready — disable in Nextcloud admin settings |
| Recordings list empty | Check `NC_USER`/`NC_PASS`/`NC_URL` in environment file; ensure `EnvironmentFile=` is in service file |
| Service fails `217/USER` | `User=` in service file doesn't match your actual username |

---

## License

MIT
