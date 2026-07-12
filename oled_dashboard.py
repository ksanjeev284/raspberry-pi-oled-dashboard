"""
Raspberry Pi OLED Dashboard — SSD1306 status panel, room guard & video animations.

Features: rotating OLED UI, Flask portal, PIR security captures,
local video upload, YouTube stream/save with Otsu 1-bit playback.

Run:  python oled_dashboard.py
Web:  http://<pi-ip>:5000
"""

import time
import board
import adafruit_dht
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from PIL import ImageFont, Image, ImageDraw
from gpiozero import MotionSensor
import psutil
import socket
import subprocess
import threading
import io
import os
from pathlib import Path
from datetime import datetime
from collections import deque
from flask import (
    Flask,
    Response,
    render_template_string,
    request,
    stream_with_context,
    jsonify,
    send_file,
    abort,
)
import cv2
import yt_dlp
import re

# ==========================================
# HARDWARE — JMD 0.96" Blue OLED (I2C 4-pin)
# ==========================================
# Module: JMD-0.96 Inch I2C/IIC 4-Pin OLED (BLUE)
# Resolution: 128 x 64 pixels  |  Driver: SSD1306  |  Bus: I2C
# Voltage: VCC 3.3V–5V (Pi 3.3V preferred)
#
# Raspberry Pi wiring (BCM / physical):
#   OLED VCC  →  3.3V  (physical pin 1)
#   OLED GND  →  GND   (physical pin 6)
#   OLED SCL  →  GPIO3 / SCL1  (physical pin 5)
#   OLED SDA  →  GPIO2 / SDA1  (physical pin 3)
#
# I2C bus 1, default address 0x3C (confirmed via i2cdetect)
OLED_I2C_PORT = 1
OLED_I2C_ADDR = 0x3C
OLED_WIDTH = 128
OLED_HEIGHT = 64
# Blue OLEDs read cleaner at full contrast on this small 0.96" panel
OLED_CONTRAST = 255


def init_oled():
    """
    Init JMD 0.96\" 128x64 blue SSD1306 over I2C.
    Falls back to 0x3D / SH1106 only if needed (some clones mislabel chips).
    """
    last_err = None
    # Prefer SSD1306 — correct for JMD 0.96\" blue modules
    for addr in (OLED_I2C_ADDR, 0x3D):
        try:
            ser = i2c(port=OLED_I2C_PORT, address=addr)
            dev = ssd1306(
                ser,
                width=OLED_WIDTH,
                height=OLED_HEIGHT,
                rotate=0,
            )
            dev.contrast(OLED_CONTRAST)
            # Ensure panel is awake and blank before first frame
            try:
                dev.show()
            except Exception:
                pass
            dev.clear()
            print(f"OLED ready: SSD1306 128x64 blue @ I2C 0x{addr:02X} bus {OLED_I2C_PORT}")
            return dev
        except Exception as e:
            last_err = e
            print(f"SSD1306 @ 0x{addr:02X} failed: {e}")

    # Rare clones use SH1106 (same 4-pin package)
    try:
        from luma.oled.device import sh1106

        ser = i2c(port=OLED_I2C_PORT, address=OLED_I2C_ADDR)
        dev = sh1106(ser, width=OLED_WIDTH, height=OLED_HEIGHT, rotate=0)
        dev.contrast(OLED_CONTRAST)
        dev.clear()
        print("OLED ready: SH1106 fallback @ 0x3C")
        return dev
    except Exception as e:
        last_err = e

    raise RuntimeError(f"Could not init JMD 0.96 OLED: {last_err}")


device = init_oled()

pir = MotionSensor(17)  # GPIO17 = physical pin 11
dht_sensor = adafruit_dht.DHT22(board.D4, use_pulseio=False)

# Fonts tuned for 128x64 — large enough to read, small enough not to collide
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

tiny = ImageFont.truetype(FONT, 8)
small = ImageFont.truetype(FONT, 10)
bold = ImageFont.truetype(BOLD, 11)
large = ImageFont.truetype(BOLD, 14)
huge = ImageFont.truetype(BOLD, 16)


def text_h(font):
    """Approximate glyph height for layout spacing."""
    try:
        bbox = font.getbbox("Ag")
        return bbox[3] - bbox[1]
    except Exception:
        return 10


def fit_text(draw, xy, text, font, max_w=128):
    """Draw text clipped to max pixel width (no overrun into neighbors)."""
    s = str(text)
    while s and draw.textlength(s, font=font) > max_w:
        s = s[:-1]
    if s:
        draw.text(xy, s, font=font, fill="white")

# ==========================================
# GLOBAL STATE
# ==========================================
app = Flask(__name__)
current_mode = "DASHBOARD"  # Modes: DASHBOARD, CAMERA, YOUTUBE, ANIMATION
youtube_url = ""
animation_path = ""  # absolute path to local video/bin clip for ANIMATION mode
animation_youtube_url = ""  # YouTube URL played via ANIMATION (Otsu B/W)
animation_loop = True
latest_frame = None  # PIL image (OLED mirror) for dashboard MJPEG feed
lock = threading.Lock()

# Website camera: hardware H.264 via rpicam-vid (Pi 3B+ cannot soft-encode 720p@30)
WEB_W, WEB_H = 1280, 720
WEB_FPS = 30
WEB_BITRATE_BPS = 2_500_000  # rpicam-vid uses bits/sec
camera_stream_lock = threading.Lock()

# ==========================================
# OLED ANIMATIONS (from oled-animation project)
# ==========================================
# Drop MP4/AVI clips here (or .bin packs from convert_oled_animation.py).
# Playback uses 128x64 Otsu thresholding — same pipeline as video.py.
ANIMATION_DIR = Path.home() / "oled_animations"
ANIMATION_DIR.mkdir(parents=True, exist_ok=True)
ANIMATION_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".bin"}
# Default frame delay (ms) when FPS unknown — matches video.py FRAME_DELAY
ANIMATION_FRAME_DELAY_MS = 16
# Portal upload limit (browser + Flask)
ANIMATION_MAX_UPLOAD_MB = 80
ANIMATION_MAX_UPLOAD_BYTES = ANIMATION_MAX_UPLOAD_MB * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = ANIMATION_MAX_UPLOAD_BYTES


def list_animations():
    """Return sorted list of playable animation filenames in ANIMATION_DIR."""
    if not ANIMATION_DIR.is_dir():
        return []
    files = []
    for p in sorted(ANIMATION_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in ANIMATION_EXTS:
            files.append(
                {
                    "name": p.name,
                    "size_kb": round(p.stat().st_size / 1024, 1),
                    "kind": "bin" if p.suffix.lower() == ".bin" else "video",
                }
            )
    return files


def sanitize_animation_filename(name):
    """
    Turn an upload name into a safe basename under ANIMATION_DIR.
    Keeps only letters, digits, dot, dash, underscore; forces known extension.
    """
    if not name:
        return None
    base = Path(str(name)).name
    # strip path pieces / Windows drive noise
    base = base.replace("\\", "/").split("/")[-1]
    stem = Path(base).stem
    ext = Path(base).suffix.lower()
    if ext not in ANIMATION_EXTS:
        return None
    # ASCII-ish safe stem
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not cleaned:
        cleaned = "upload"
    # avoid huge names
    cleaned = cleaned[:80]
    return f"{cleaned}{ext}"


def unique_animation_path(filename):
    """If file exists, append _1, _2, ... before extension."""
    path = ANIMATION_DIR / filename
    if not path.exists():
        return path
    stem = path.stem
    ext = path.suffix
    for i in range(1, 1000):
        candidate = ANIMATION_DIR / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
    # last resort: timestamp
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ANIMATION_DIR / f"{stem}_{ts}{ext}"


def resolve_animation(name):
    """Resolve a safe basename under ANIMATION_DIR, or None."""
    if not name:
        return None
    base = Path(str(name)).name
    if base != str(name).replace("\\", "/").split("/")[-1]:
        return None
    if ".." in base or "/" in base or "\\" in base:
        return None
    path = ANIMATION_DIR / base
    if not path.is_file() or path.suffix.lower() not in ANIMATION_EXTS:
        return None
    return path


def is_youtube_url(url):
    """True if URL looks like a YouTube watch/share link."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    return bool(
        re.search(
            r"(youtube\.com/watch|youtube\.com/shorts|youtu\.be/|youtube\.com/embed/)",
            u,
            re.I,
        )
    )


def resolve_youtube_stream(url):
    """
    Resolve a direct media URL via yt-dlp (no download).
    Returns (stream_url, title, fps). Raises on failure.
    Prefer low resolution so Pi can decode + Otsu near real-time.
    """
    ydl_opts = {
        # 240p/360p is plenty for 128x64 and much faster to decode than 480p+
        "format": (
            "worst[height<=240][ext=mp4]/"
            "worst[height<=360][ext=mp4]/"
            "worst[ext=mp4]/worst"
        ),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        stream_url = info.get("url")
        if not stream_url:
            raise RuntimeError("No stream URL")
        title = (info.get("title") or "YouTube")[:80]
        fps = info.get("fps") or 30
        try:
            fps = float(fps)
        except (TypeError, ValueError):
            fps = 30.0
        if fps < 1:
            fps = 30.0
        return stream_url, title, fps


def animation_period(fps):
    """Source-frame period for wall-clock pacing (seconds)."""
    try:
        f = float(fps)
    except (TypeError, ValueError):
        f = 24.0
    if f < 1:
        f = 24.0
    # Cap absurd metadata (some streams report 1000+)
    f = min(f, 60.0)
    return f, 1.0 / f


def catch_up_video_frame(cap, frame_idx, t_start, fps):
    """
    Keep playback on wall-clock time by dropping frames when processing lags.
    Returns (ok, bgr_frame_or_None, new_frame_idx).
    """
    fps, _ = animation_period(fps)
    elapsed = time.monotonic() - t_start
    target = int(elapsed * fps)
    # Drop intermediate frames (grab is cheaper than full decode+display)
    skip = target - frame_idx
    if skip > 0:
        # Limit burst so we don't block too long on one loop
        for _ in range(min(skip, 45)):
            if not cap.grab():
                return False, None, frame_idx
            frame_idx += 1
    ret, cv_frame = cap.read()
    if ret and cv_frame is not None:
        frame_idx += 1
        return True, cv_frame, frame_idx
    return False, None, frame_idx


def sleep_for_playback(t_start, frame_idx, fps):
    """Sleep only if we are ahead of schedule (never double-count processing)."""
    fps, _ = animation_period(fps)
    deadline = t_start + (frame_idx / fps)
    remaining = deadline - time.monotonic()
    if remaining > 0.001:
        time.sleep(min(remaining, 0.1))


def download_youtube_animation(url):
    """
    Download a YouTube clip into ANIMATION_DIR for offline OLED playback.
    Returns Path to the saved file. Raises on failure.
    """
    # Temp template — sanitize final name after download
    outtmpl = str(ANIMATION_DIR / "yt_%(id)s.%(ext)s")
    ydl_opts = {
        "format": "worst[height<=360][ext=mp4]/worst[ext=mp4]/worst",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "max_filesize": ANIMATION_MAX_UPLOAD_BYTES,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("yt-dlp returned no info")
        # Prepare expected path
        vid = info.get("id") or "video"
        ext = (info.get("ext") or "mp4").lower()
        if f".{ext}" not in ANIMATION_EXTS and ext != "mp4":
            # prefer mp4 container name even if remuxed
            ext = "mp4"
        raw_path = ANIMATION_DIR / f"yt_{vid}.{ext}"
        # yt-dlp may write slightly different name — find newest yt_* file
        if not raw_path.is_file():
            candidates = sorted(
                ANIMATION_DIR.glob(f"yt_{vid}.*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                # fallback: any prepared filename from prepare_filename
                try:
                    prepared = Path(ydl.prepare_filename(info))
                    if prepared.is_file():
                        raw_path = prepared
                except Exception:
                    pass
            else:
                raw_path = candidates[0]
        if not raw_path.is_file():
            raise RuntimeError("Download finished but file missing")

        title = info.get("title") or vid
        safe = sanitize_animation_filename(f"{title}.mp4")
        if not safe:
            safe = f"youtube_{vid}.mp4"
        # force .mp4-ish if extension unsupported
        if Path(safe).suffix.lower() not in ANIMATION_EXTS:
            safe = f"{Path(safe).stem}.mp4"
        dest = unique_animation_path(safe)
        if raw_path.resolve() != dest.resolve():
            raw_path.replace(dest)
        if dest.stat().st_size > ANIMATION_MAX_UPLOAD_BYTES:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Downloaded file exceeds {ANIMATION_MAX_UPLOAD_MB} MB")
        return dest


# ==========================================
# ROOM SECURITY (extends existing sensors)
# ==========================================
# INMP441 mic is NOT wired yet → sound shows "NO MIC" until added.
SECURITY_ROOT = Path.home() / "security"
DIR_RECORDINGS = SECURITY_ROOT / "recordings"
DIR_SNAPSHOTS = SECURITY_ROOT / "snapshots"
DIR_LOGS = SECURITY_ROOT / "logs"
EVENT_LOG = DIR_LOGS / "security_events.log"

for _d in (DIR_RECORDINGS, DIR_SNAPSHOTS, DIR_LOGS):
    _d.mkdir(parents=True, exist_ok=True)

armed = False  # DISARMED by default
recording = False
alert_until = 0.0  # monotonic deadline for OLED alert screen
alert_title = ""
alert_detail = ""
sound_level = "NO MIC"  # QUIET / NOISE / LOUD when INMP441 is added
last_motion = False
shared_room_temp = None
shared_room_hum = None
recent_events = deque(maxlen=40)
EVENT_COOLDOWN_SEC = 45  # min gap between auto captures
RECORD_SECONDS = 10  # clip length on trigger
_last_event_time = 0.0

# ==========================================
# HELPER FUNCTIONS (From Original Script)
# ==========================================
def get_pi_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except Exception:
        return 0


def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "OFF"


def get_docker():
    try:
        out = subprocess.check_output(
            ["docker", "ps", "-q"], stderr=subprocess.DEVNULL
        ).decode()
        return len(out.splitlines())
    except Exception:
        return 0


def stamp():
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def log_security_event(kind, message, extra=None):
    """Append to disk log + in-memory list for the web dashboard."""
    ts_human = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "time": ts_human,
        "kind": kind,
        "message": message,
        "extra": extra or {},
    }
    line = f"{ts_human}  [{kind}]  {message}"
    if extra:
        line += f"  {extra}"
    try:
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"Event log write failed: {e}")
    with lock:
        recent_events.appendleft(entry)
    print("SECURITY:", line)
    return entry


def set_alert(title, detail, seconds=12):
    global alert_until, alert_title, alert_detail
    with lock:
        alert_title = title[:18]
        alert_detail = detail[:20]
        alert_until = time.monotonic() + seconds


def remux_h264_to_mp4(h264_path, mp4_path=None):
    """Browser-friendly MP4 (copy, no re-encode)."""
    h264_path = Path(h264_path)
    if mp4_path is None:
        mp4_path = h264_path.with_suffix(".mp4")
    mp4_path = Path(mp4_path)
    if mp4_path.exists() and mp4_path.stat().st_size > 0:
        return mp4_path
    if not h264_path.exists():
        return None
    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-framerate",
                "15",
                "-i",
                str(h264_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(mp4_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode == 0 and mp4_path.exists():
            return mp4_path
        print("remux fail:", r.stderr)
    except Exception as e:
        print(f"remux error: {e}")
    return None


def list_security_media():
    """
    Build timeline of alerts with playable video + snapshot when present.
    Groups by timestamp stamp in filenames.
    """
    items = {}
    # snapshots: intruder_YYYY-mm-dd_HH-MM-SS.jpg
    for p in sorted(DIR_SNAPSHOTS.glob("intruder_*.jpg"), reverse=True):
        m = re.match(r"intruder_(.+)\.jpg$", p.name)
        if not m:
            continue
        key = m.group(1)
        items.setdefault(key, {"ts": key, "snapshot": None, "video": None, "h264": None})
        items[key]["snapshot"] = p.name

    for p in sorted(DIR_RECORDINGS.glob("motion_*.*"), reverse=True):
        m = re.match(r"motion_(.+)\.(h264|mp4)$", p.name)
        if not m:
            continue
        key, ext = m.group(1), m.group(2)
        items.setdefault(key, {"ts": key, "snapshot": None, "video": None, "h264": None})
        if ext == "mp4":
            items[key]["video"] = p.name
        else:
            items[key]["h264"] = p.name
            # Prefer existing mp4 or create lazily on list
            mp4 = p.with_suffix(".mp4")
            if not mp4.exists():
                remux_h264_to_mp4(p, mp4)
            if mp4.exists():
                items[key]["video"] = mp4.name

    # newest first
    out = sorted(items.values(), key=lambda x: x["ts"], reverse=True)
    return out[:30]


def safe_media_name(name):
    """Prevent path traversal when serving files."""
    name = Path(name).name
    if not re.match(r"^[\w.\-]+$", name):
        return None
    return name


def capture_security_media(ts_label):
    """
    Snapshot + short H.264 clip using rpicam (HW), then remux to MP4 for browser play.
    Uses camera_stream_lock so live /camera.mp4 does not fight recording.
    """
    global recording
    snap_path = DIR_SNAPSHOTS / f"intruder_{ts_label}.jpg"
    h264_path = DIR_RECORDINGS / f"motion_{ts_label}.h264"
    mp4_path = DIR_RECORDINGS / f"motion_{ts_label}.mp4"
    got = camera_stream_lock.acquire(timeout=3)
    if not got:
        log_security_event("CAMERA_BUSY", "Camera in use — skip capture")
        return
    recording = True
    try:
        # Still image
        r1 = subprocess.run(
            [
                "rpicam-still",
                "-n",
                "-t",
                "400",
                "--width",
                "1280",
                "--height",
                "720",
                "-o",
                str(snap_path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r1.returncode == 0 and snap_path.exists():
            log_security_event(
                "SNAPSHOT",
                f"Saved {snap_path.name}",
                {"ts": ts_label, "file": snap_path.name},
            )
        else:
            log_security_event(
                "SNAPSHOT_FAIL",
                (r1.stderr or r1.stdout or "still failed")[:80],
            )

        # Short event video (not 24/7)
        r2 = subprocess.run(
            [
                "rpicam-vid",
                "-n",
                "-t",
                str(int(RECORD_SECONDS * 1000)),
                "--width",
                "1280",
                "--height",
                "720",
                "--framerate",
                "15",
                "--codec",
                "h264",
                "--inline",
                "-o",
                str(h264_path),
            ],
            capture_output=True,
            text=True,
            timeout=RECORD_SECONDS + 25,
        )
        if r2.returncode == 0 and h264_path.exists():
            log_security_event(
                "RECORDING",
                f"Saved {h264_path.name}",
                {"ts": ts_label, "file": h264_path.name},
            )
            mp4 = remux_h264_to_mp4(h264_path, mp4_path)
            if mp4:
                log_security_event(
                    "RECORDING_MP4",
                    f"Playable {mp4.name}",
                    {"ts": ts_label, "file": mp4.name},
                )
        else:
            log_security_event(
                "RECORD_FAIL",
                (r2.stderr or r2.stdout or "vid failed")[:80],
            )
    except Exception as e:
        log_security_event("CAPTURE_ERROR", str(e)[:100])
    finally:
        recording = False
        try:
            camera_stream_lock.release()
        except Exception:
            pass


def trigger_security_event(kind, message):
    """High-level: log, OLED alert, background capture."""
    global _last_event_time
    now = time.time()
    if now - _last_event_time < EVENT_COOLDOWN_SEC:
        log_security_event("COOLDOWN", f"Skipped {kind} (cooldown)")
        return
    _last_event_time = now
    ts = stamp()
    log_security_event(kind, message, {"ts": ts})
    set_alert("!!! ALERT !!!", message, seconds=14)
    threading.Thread(
        target=capture_security_media,
        args=(ts,),
        name="sec-capture",
        daemon=True,
    ).start()


def security_engine():
    """
    Watches PIR when ARMED. Mic levels reserved for INMP441 later.
    Does not change sensor wiring — reuses existing MotionSensor(17).
    """
    global last_motion, armed
    prev_motion = False
    while True:
        try:
            motion = bool(pir.motion_detected)
            with lock:
                last_motion = motion
                is_armed = armed

            # Rising edge while armed → security event
            if is_armed and motion and not prev_motion:
                trigger_security_event("MOTION", "PIR motion detected")

            prev_motion = motion
            time.sleep(0.15)
        except Exception as e:
            print(f"security_engine: {e}")
            time.sleep(1)


def draw_guard_slide(draw, r_temp, r_hum, motion, sound, is_armed, is_recording, frame=0):
    """ROOM GUARD status — local control screen."""
    kawaii_border(draw, frame)
    fit_text(draw, (4, 0), "ROOM GUARD", bold, max_w=100)
    if is_armed:
        fit_text(draw, (90, 0), "ARM", tiny, max_w=36)
    else:
        fit_text(draw, (84, 0), "OFF", tiny, max_w=40)
    draw.line((0, 12, 127, 12), fill="white")

    t = f"{r_temp:.1f}C" if r_temp is not None else "--.-C"
    h = f"{r_hum:.0f}%" if r_hum is not None else "--%"
    fit_text(draw, (2, 15), f"TEMP {t}", small, max_w=124)
    fit_text(draw, (2, 28), f"HUM  {h}", small, max_w=124)
    fit_text(
        draw,
        (2, 41),
        f"MOT  {'ACTIVE' if motion else 'CLEAR'}",
        small,
        max_w=124,
    )
    fit_text(draw, (2, 54), f"SND  {sound}", tiny, max_w=80)
    if is_recording:
        fit_text(draw, (88, 54), "REC", tiny, max_w=36)
    elif is_armed and frame % 8 < 4:
        star(draw, 118, 56)


def draw_alert_slide(draw, title, detail, frame=0):
    """Full-screen alert when ARMED event fires."""
    # flashing border
    if frame % 4 < 2:
        draw.rectangle((0, 0, 127, 63), outline="white")
    fit_text(draw, (8, 4), title or "ALERT", bold, max_w=112)
    draw.line((8, 18, 119, 18), fill="white")
    fit_text(draw, (8, 24), detail or "EVENT", small, max_w=112)
    fit_text(draw, (8, 42), "RECORDING..." if recording else "CHECK DASH", small, max_w=112)
    if frame % 6 < 3:
        heart(draw, 110, 50)


def bar(draw, x, y, value, width=30, height=8):
    """Thicker progress bar for better visibility on 128x64 OLED."""
    draw.rectangle((x, y, x + width, y + height), outline="white")
    fill = int((width - 2) * min(max(value, 0), 100) / 100)
    if fill > 0:
        draw.rectangle((x + 1, y + 1, x + 1 + fill, y + height - 1), fill="white")


def heart(draw, x, y):
    draw.point((x + 1, y), fill="white")
    draw.point((x + 4, y), fill="white")
    draw.line((x, y + 1, x + 5, y + 1), fill="white")
    draw.line((x + 1, y + 2, x + 4, y + 2), fill="white")
    draw.line((x + 2, y + 3, x + 3, y + 3), fill="white")


# ==========================================
# KAWAII PIXEL DECORATIONS (monochrome OLED)
# ==========================================
def star(draw, x, y, big=False):
    """Tiny sparkle / star."""
    if big:
        draw.point((x + 2, y), fill="white")
        draw.point((x + 2, y + 4), fill="white")
        draw.line((x, y + 2, x + 4, y + 2), fill="white")
        draw.point((x + 1, y + 1), fill="white")
        draw.point((x + 3, y + 1), fill="white")
        draw.point((x + 1, y + 3), fill="white")
        draw.point((x + 3, y + 3), fill="white")
    else:
        draw.point((x + 1, y), fill="white")
        draw.line((x, y + 1, x + 2, y + 1), fill="white")
        draw.point((x + 1, y + 2), fill="white")


def sparkle_field(draw, frame, positions=None):
    """Sparse twinkles only in safe empty zones (avoid text)."""
    if positions is None:
        positions = [(122, 1), (2, 60)]
    for i, (sx, sy) in enumerate(positions):
        if 0 <= sx <= 124 and 0 <= sy <= 61 and (frame + i * 3) % 14 < 6:
            star(draw, sx, sy, big=False)


def flower(draw, x, y, frame=0):
    """5px flower — keep clear of text."""
    r = 1 if (frame // 6) % 2 == 0 else 0
    draw.point((x + 2, y), fill="white")
    draw.point((x, y + 2), fill="white")
    draw.point((x + 4, y + 2), fill="white")
    draw.point((x + 2, y + 4), fill="white")
    draw.point((x + 2 + r, y + 2), fill="white")


def bunny_face(draw, x, y, frame=0):
    """Compact bunny (~12x16) for free space only."""
    bob = 1 if (frame // 5) % 2 == 0 else 0
    draw.line((x + 2, y + 6, x + 2, y + bob), fill="white")
    draw.line((x + 9, y + 6, x + 9, y + bob), fill="white")
    draw.ellipse((x + 1, y + 5, x + 11, y + 15), outline="white")
    blink = (frame % 20) in (18, 19)
    if blink:
        draw.line((x + 3, y + 9, x + 5, y + 9), fill="white")
        draw.line((x + 7, y + 9, x + 9, y + 9), fill="white")
    else:
        draw.point((x + 4, y + 9), fill="white")
        draw.point((x + 8, y + 9), fill="white")
    draw.point((x + 6, y + 11), fill="white")


def bow(draw, x, y):
    draw.polygon([(x, y + 2), (x + 3, y), (x + 3, y + 4)], outline="white")
    draw.polygon([(x + 6, y + 2), (x + 3, y), (x + 3, y + 4)], outline="white")


def kawaii_border(draw, frame=0):
    """Corner ticks only — does not cross text rows."""
    draw.line((0, 0, 4, 0), fill="white")
    draw.line((0, 0, 0, 4), fill="white")
    draw.line((123, 0, 127, 0), fill="white")
    draw.line((127, 0, 127, 4), fill="white")
    draw.line((0, 63, 4, 63), fill="white")
    draw.line((0, 59, 0, 63), fill="white")
    draw.line((123, 63, 127, 63), fill="white")
    draw.line((127, 59, 127, 63), fill="white")


def cheek_blush(draw, x, y):
    draw.point((x, y), fill="white")
    draw.point((x + 2, y), fill="white")


def kawaii_mood(cpu, temp, motion, just_left):
    if motion:
        return "hi!"
    if just_left:
        return "bye?"
    if temp >= 65:
        return "hot!"
    if cpu >= 85:
        return "busy"
    if cpu < 20 and temp < 50:
        return "cozy"
    return "ok"


def draw_cat(draw, frame, cpu, temp, motion, just_left, x=91, y=2, show_status=True):
    """
    Pet sprite. Bounding box ~ 36w x 52h (status included).
    Keep x in 0..92 and y in 0..10 so nothing clips the 128x64 panel.
    """
    # Clamp so whiskers/tail never leave the screen (stops wrap/overlap)
    x = max(0, min(int(x), 92))
    y = max(0, min(int(y), 8))

    sleeping = (frame % 100 in range(75, 92) and not motion and not just_left)
    meowing = (frame % 65 in range(52, 57) and not motion and not just_left)
    loving = (frame % 80 in range(60, 65) and not motion and not just_left)
    blink = (frame % 18 in (16, 17) and not motion)

    if motion:
        left_twitch, right_twitch = 2, 1
    else:
        left_twitch = 1 if frame % 30 == 0 else 0
        right_twitch = 0

    # ears + head (no bounce — bounce caused vertical overlap)
    draw.polygon(
        [(x + 4, y + 8), (x + 8, y + 1 - left_twitch), (x + 12, y + 8)], outline="white"
    )
    draw.polygon(
        [(x + 18, y + 8), (x + 22, y + 1 - right_twitch), (x + 26, y + 8)],
        outline="white",
    )
    draw.rounded_rectangle((x + 4, y + 7, x + 26, y + 26), radius=4, outline="white")
    cheek_blush(draw, x + 6, y + 18)
    cheek_blush(draw, x + 20, y + 18)

    if motion:
        draw.ellipse((x + 8, y + 11, x + 13, y + 17), outline="white")
        draw.ellipse((x + 17, y + 11, x + 22, y + 17), outline="white")
        draw.point((x + 10, y + 14), fill="white")
        draw.point((x + 19, y + 14), fill="white")
    elif just_left:
        look = -1 if frame % 8 < 4 else 1
        draw.ellipse((x + 9 + look, y + 13, x + 11 + look, y + 16), fill="white")
        draw.ellipse((x + 18 + look, y + 13, x + 20 + look, y + 16), fill="white")
    elif sleeping or blink:
        draw.arc((x + 8, y + 12, x + 13, y + 16), 0, 180, fill="white")
        draw.arc((x + 17, y + 12, x + 22, y + 16), 0, 180, fill="white")
    elif cpu > 90:
        draw.line((x + 8, y + 12, x + 12, y + 16), fill="white")
        draw.line((x + 12, y + 12, x + 8, y + 16), fill="white")
        draw.line((x + 17, y + 12, x + 21, y + 16), fill="white")
        draw.line((x + 21, y + 12, x + 17, y + 16), fill="white")
    else:
        look = (frame // 8) % 3 - 1
        draw.ellipse((x + 9 + look, y + 13, x + 11 + look, y + 16), fill="white")
        draw.ellipse((x + 18 + look, y + 13, x + 20 + look, y + 16), fill="white")

    draw.point((x + 15, y + 18), fill="white")

    if motion or meowing:
        draw.ellipse((x + 12, y + 19, x + 18, y + 24), outline="white")
    elif just_left:
        draw.line((x + 12, y + 22, x + 18, y + 22), fill="white")
    else:
        draw.arc((x + 11, y + 19, x + 19, y + 25), 20, 160, fill="white")

    # short whiskers (stay inside body width)
    draw.line((x + 6, y + 19, x + 1, y + 18), fill="white")
    draw.line((x + 6, y + 21, x + 1, y + 22), fill="white")
    draw.line((x + 24, y + 19, x + 29, y + 18), fill="white")
    draw.line((x + 24, y + 21, x + 29, y + 22), fill="white")

    # body (shorter so status fits under)
    draw.arc((x + 7, y + 25, x + 23, y + 42), 180, 360, fill="white")
    draw.line((x + 7, y + 33, x + 7, y + 40), fill="white")
    draw.line((x + 23, y + 33, x + 23, y + 40), fill="white")
    draw.line((x + 7, y + 40, x + 23, y + 40), fill="white")

    if loving or (frame % 50 < 20 and not motion):
        bow(draw, x + 12, y + 3)

    # compact tail inside right edge
    if (frame if not motion else frame * 2) % 4 < 2:
        draw.arc((x + 20, y + 28, x + 32, y + 40), 270, 90, fill="white")
    else:
        draw.arc((x + 19, y + 30, x + 31, y + 42), 270, 80, fill="white")

    if motion:
        status = "I SEE U"
        heart(draw, x + 26, y + 2)
    elif just_left:
        status = "WHERE?"
    elif sleeping:
        status = "Zzz"
    elif meowing:
        status = "NYA"
    elif temp >= 65:
        status = "HOT"
    elif cpu >= 85:
        status = "BUSY"
    else:
        status = "HAPPY"

    if loving:
        heart(draw, x + 26, y + 2)

    if show_status:
        # status always under body, clipped to sprite width
        fit_text(draw, (x + 2, y + 43), status, tiny, max_w=30)


def draw_env_slide(draw, r_temp, r_hum, frame=0):
    """
    Layout zones (no overlap):
      title  y=0..11
      line   y=12
      temp   y=16..34
      hum    y=38..52
      tag    y=54..62  right side only
    """
    kawaii_border(draw, frame)
    fit_text(draw, (4, 0), "ROOM", bold, max_w=50)
    if frame % 10 < 5:
        star(draw, 48, 2)
    draw.line((0, 12, 127, 12), fill="white")

    if r_temp is not None and r_hum is not None:
        fit_text(draw, (4, 18), f"{r_temp:.1f}C", huge, max_w=90)
        fit_text(draw, (4, 40), f"{r_hum:.0f}% RH", large, max_w=90)
        if 20 <= r_temp <= 26 and 35 <= r_hum <= 60:
            fit_text(draw, (90, 54), "cozy", tiny, max_w=36)
            heart(draw, 118, 56)
        elif r_temp >= 28:
            fit_text(draw, (90, 54), "warm", tiny, max_w=36)
        else:
            fit_text(draw, (90, 54), "ok", tiny, max_w=36)
        flower(draw, 112, 20, frame)
    else:
        fit_text(draw, (8, 28), "Reading...", small, max_w=110)


def draw_sys_slide(draw, cpu, ram, temp, ip, docker, frame=0):
    """
    Fixed rows — no overlapping mood/IP:
      y0-11  title
      y14-26 CPU + bar
      y28-40 RAM + bar
      y44-55 temp | IP | docker
    """
    kawaii_border(draw, frame)
    fit_text(draw, (4, 0), "PI STATUS", bold, max_w=90)
    if frame % 8 < 4:
        star(draw, 100, 2)
    else:
        heart(draw, 100, 2)
    draw.line((0, 12, 127, 12), fill="white")

    # CPU row
    fit_text(draw, (2, 15), "CPU", small, max_w=30)
    fit_text(draw, (36, 15), f"{cpu:3.0f}%", small, max_w=40)
    bar(draw, 78, 17, cpu, width=46, height=8)

    # RAM row
    fit_text(draw, (2, 30), "RAM", small, max_w=30)
    fit_text(draw, (36, 30), f"{ram:3.0f}%", small, max_w=40)
    bar(draw, 78, 32, ram, width=46, height=8)

    # Footer columns: [temp 0-40] [IP 44-96] [docker 100-126]
    fit_text(draw, (2, 48), f"{temp:.0f}C", small, max_w=36)
    short_ip = ip.split(".")[-1] if "." in ip else ip[:8]
    fit_text(draw, (44, 50), f".{short_ip}", tiny, max_w=48)
    if docker:
        fit_text(draw, (100, 50), f"D{docker}", tiny, max_w=26)
    else:
        # free right slot — optional tiny sparkle only
        if frame % 12 < 5:
            star(draw, 118, 52)


# Prefer 720p @ 30fps; appsink keeps a few frames so capture isn't starved
_APPSINK = "appsink drop=true max-buffers=4 sync=false"
CAMERA_PIPELINES = [
    (
        "libcamerasrc ! video/x-raw,width=1280,height=720,framerate=30/1 ! "
        f"videoconvert ! video/x-raw,format=BGR ! {_APPSINK}"
    ),
    (
        "libcamerasrc ! video/x-raw,width=1296,height=972,framerate=30/1 ! "
        f"videoconvert ! video/x-raw,format=BGR ! {_APPSINK}"
    ),
    (
        "libcamerasrc ! video/x-raw,width=1920,height=1080,framerate=30/1 ! "
        f"videoconvert ! video/x-raw,format=BGR ! {_APPSINK}"
    ),
    (
        "libcamerasrc ! video/x-raw,width=640,height=480,framerate=30/1 ! "
        f"videoconvert ! video/x-raw,format=BGR ! {_APPSINK}"
    ),
    (
        "libcamerasrc ! videoconvert ! video/x-raw,format=BGR ! "
        f"{_APPSINK}"
    ),
]


def show_oled_text(line1, line2=""):
    """Draw a simple status screen and publish it to the web feed."""
    global latest_frame
    image = Image.new("1", (device.width, device.height))
    draw = ImageDraw.Draw(image)
    draw.text((2, 18), line1[:20], font=bold, fill="white")
    if line2:
        draw.text((2, 36), line2[:28], font=small, fill="white")
    device.display(image)
    with lock:
        latest_frame = image.copy()


def release_capture(cap):
    if cap is not None:
        try:
            cap.release()
        except Exception:
            pass
    return None


def open_camera_capture():
    """Open OV5647 through libcamerasrc (prefer 720p @ 30fps)."""
    for pipe in CAMERA_PIPELINES:
        print(f"Trying camera pipeline: {pipe[:80]}...")
        cap = cv2.VideoCapture(pipe, cv2.CAP_GSTREAMER)
        if cap is not None and cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                print(f"Camera opened at shape={frame.shape}")
                return cap
        release_capture(cap)
    return None


def cv_frame_to_oled(cv_frame, otsu=False):
    """Convert BGR OpenCV frame to 128x64 1-bit PIL image for the OLED."""
    # Resize first (less work) — AREA is good for downscale to tiny panel
    frame_res = cv2.resize(
        cv_frame, (OLED_WIDTH, OLED_HEIGHT), interpolation=cv2.INTER_AREA
    )
    gray = cv2.cvtColor(frame_res, cv2.COLOR_BGR2GRAY)
    if otsu:
        # Same as oled-animation video.py — per-frame Otsu B/W
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        # No dither — faster and sharper for 1-bit OLED
        return Image.fromarray(binary, mode="L").convert(
            "1", dither=Image.Dither.NONE
        )
    return Image.fromarray(gray, mode="L").convert("1", dither=Image.Dither.NONE)


def load_bin_animation(path):
    """
    Load packed 128x64 frames from a .bin file.
    Format: [uint32 little-endian frame_count][frame0 1024 bytes]...
    Each frame is row-major 1-bit MSB-first (same packing as video.py).
    Returns list of PIL Image mode '1'.
    """
    data = Path(path).read_bytes()
    if len(data) < 4:
        raise ValueError("bin too short")
    count = int.from_bytes(data[:4], "little")
    bpf = (OLED_WIDTH * OLED_HEIGHT) // 8  # 1024
    expected = 4 + count * bpf
    if count <= 0 or len(data) < expected:
        raise ValueError(f"bad bin header count={count} size={len(data)}")
    frames = []
    for i in range(count):
        chunk = data[4 + i * bpf : 4 + (i + 1) * bpf]
        # Unpack bits to 128*64 grayscale 0/255
        pixels = bytearray(OLED_WIDTH * OLED_HEIGHT)
        for bi, b in enumerate(chunk):
            base = bi * 8
            for bit in range(8):
                pixels[base + bit] = 255 if (b & (1 << (7 - bit))) else 0
        frames.append(Image.frombytes("L", (OLED_WIDTH, OLED_HEIGHT), bytes(pixels)).convert("1"))
    return frames


# ==========================================
# CAMERA WEB NOTE
# Website stream is started on-demand in /camera.mp4 via rpicam-vid (HW H.264).
# Security capture also uses rpicam-* under camera_stream_lock.
# ==========================================


# ==========================================
# OLED RENDER THREAD (always dashboard UI)
# ==========================================
def update_display():
    global current_mode, latest_frame, youtube_url, animation_path, animation_loop
    global animation_youtube_url
    global shared_room_temp, shared_room_hum

    # Main loop variables for dashboard
    frame = 0
    motion_was_active = False
    last_motion_end = 0
    room_temp = None
    room_hum = None
    last_dht_read = 0
    cap = None  # YouTube stream or local animation video
    last_mode = None
    anim_bin_frames = None  # list[Image] when playing .bin pack
    anim_bin_idx = 0
    anim_delay_s = ANIMATION_FRAME_DELAY_MS / 1000.0
    last_anim_path = None  # reopen capture when clip changes
    anim_yt_title = ""
    anim_fps = 24.0  # source fps for wall-clock pacing
    anim_t_start = 0.0  # monotonic start of current clip
    anim_frame_idx = 0  # frames consumed from source (incl. skipped)

    # GUARD slide added for security status; rest unchanged
    SLIDES = [
        ("GUARD", 8),
        ("ENV", 5),
        ("SYS", 8),
        ("CAT", 6),
        ("DASH", 8),
        ("KAWAII", 4),
    ]
    current_slide_idx = 0
    slide_start_time = time.monotonic()

    while True:
        try:
            now = time.monotonic()
            mode = current_mode

            # CAMERA mode: OLED still shows dashboard (camera is web-only)
            oled_mode = "DASHBOARD" if mode == "CAMERA" else mode

            if mode != last_mode:
                print(f"Mode change: {last_mode} -> {mode} (OLED={oled_mode})")
                # Release video capture when leaving stream modes
                if last_mode in ("YOUTUBE", "ANIMATION") and mode not in (
                    "YOUTUBE",
                    "ANIMATION",
                ):
                    cap = release_capture(cap)
                    anim_bin_frames = None
                    anim_bin_idx = 0
                elif last_mode == "YOUTUBE" and mode == "ANIMATION":
                    cap = release_capture(cap)
                elif last_mode == "ANIMATION" and mode == "YOUTUBE":
                    cap = release_capture(cap)
                    anim_bin_frames = None
                    anim_bin_idx = 0
                last_mode = mode
                if mode == "YOUTUBE":
                    show_oled_text("YOUTUBE", "Loading...")
                elif mode == "ANIMATION":
                    show_oled_text("ANIMATION", "Loading...")

            if oled_mode == "DASHBOARD":
                # Sensor polling
                if now - last_dht_read > 2.0:
                    try:
                        room_temp = dht_sensor.temperature
                        room_hum = dht_sensor.humidity
                    except RuntimeError:
                        pass
                    last_dht_read = now
                    with lock:
                        shared_room_temp = room_temp
                        shared_room_hum = room_hum

                cpu = psutil.cpu_percent(interval=0.1)
                ram = psutil.virtual_memory().percent
                pi_temp = get_pi_temp()
                ip = get_ip()
                docker = get_docker()

                motion = pir.motion_detected
                if motion_was_active and not motion:
                    last_motion_end = now
                just_left = (not motion and now - last_motion_end < 4)
                motion_was_active = motion

                # Priority: security alert screen
                image = Image.new("1", (device.width, device.height))
                draw = ImageDraw.Draw(image)

                with lock:
                    a_until = alert_until
                    a_title = alert_title
                    a_detail = alert_detail
                    is_armed = armed
                    is_rec = recording
                    snd = sound_level

                if now < a_until:
                    draw_alert_slide(draw, a_title, a_detail, frame)
                    device.display(image)
                    with lock:
                        latest_frame = image.copy()
                    frame += 1
                    time.sleep(0.25)
                    continue

                if now - slide_start_time > SLIDES[current_slide_idx][1]:
                    current_slide_idx = (current_slide_idx + 1) % len(SLIDES)
                    slide_start_time = now

                active_slide = SLIDES[current_slide_idx][0]

                if active_slide == "GUARD":
                    draw_guard_slide(
                        draw,
                        room_temp,
                        room_hum,
                        motion,
                        snd,
                        is_armed,
                        is_rec,
                        frame,
                    )
                elif active_slide == "ENV":
                    draw_env_slide(draw, room_temp, room_hum, frame)
                elif active_slide == "SYS":
                    draw_sys_slide(draw, cpu, ram, pi_temp, ip, docker, frame)
                elif active_slide == "CAT":
                    # Zones: title top | cat center-right | bunny bottom-left
                    kawaii_border(draw, frame)
                    fit_text(draw, (4, 0), "PET", bold, max_w=48)
                    heart(draw, 56, 2)
                    sparkle_field(draw, frame, [(120, 2)])
                    # cat centered in free area x=48..84
                    draw_cat(
                        draw,
                        frame,
                        cpu,
                        pi_temp,
                        motion,
                        just_left,
                        x=52,
                        y=4,
                        show_status=True,
                    )
                    # bunny only in left free strip (x=4..20, y=28..48)
                    bunny_face(draw, 4, 28, frame)
                    fit_text(
                        draw,
                        (4, 52),
                        kawaii_mood(cpu, pi_temp, motion, just_left),
                        tiny,
                        max_w=40,
                    )
                elif active_slide == "KAWAII":
                    # Clean vibes: title | centered cat | short msg (no cloud pile-up)
                    kawaii_border(draw, frame)
                    fit_text(draw, (28, 0), "PET", bold, max_w=60)
                    heart(draw, 8, 2)
                    heart(draw, 112, 2)
                    draw_cat(
                        draw,
                        frame,
                        cpu,
                        pi_temp,
                        motion,
                        just_left,
                        x=46,
                        y=6,
                        show_status=False,
                    )
                    msgs = ["all good", "you got this", "stay cool", "nice", "good job"]
                    msg = msgs[(frame // 10) % len(msgs)]
                    fit_text(draw, (4, 54), msg, tiny, max_w=90)
                    if frame % 8 < 4:
                        star(draw, 110, 56)
                elif active_slide == "DASH":
                    # Left 0..82 stats | divider 84 | cat 88..124
                    kawaii_border(draw, frame)
                    fit_text(draw, (2, 0), "PI", bold, max_w=20)
                    if motion:
                        if frame % 2 == 0:
                            draw.ellipse((24, 2, 30, 8), fill="white")
                        fit_text(draw, (34, 0), "MOT", tiny, max_w=28)
                    else:
                        draw.ellipse((24, 2, 30, 8), fill="white")
                        fit_text(draw, (34, 0), "OK", tiny, max_w=20)

                    fit_text(draw, (2, 14), f"C{cpu:3.0f}%", small, max_w=48)
                    bar(draw, 50, 16, cpu, width=30, height=7)
                    fit_text(draw, (2, 28), f"R{ram:3.0f}%", small, max_w=48)
                    bar(draw, 50, 30, ram, width=30, height=7)
                    fit_text(draw, (2, 42), f"{pi_temp:.0f}C", small, max_w=40)
                    bar(draw, 50, 44, min(pi_temp, 100), width=30, height=7)
                    short_ip = ip.split(".")[-1] if "." in ip else ip
                    fit_text(draw, (2, 54), f".{short_ip}", tiny, max_w=40)

                    draw.line((84, 0, 84, 63), fill="white")
                    # cat fully in right pane (x>=88), status under cat only
                    draw_cat(
                        draw,
                        frame,
                        cpu,
                        pi_temp,
                        motion,
                        just_left,
                        x=88,
                        y=2,
                        show_status=True,
                    )

                # Physical OLED + optional web OLED mirror
                device.display(image)
                with lock:
                    latest_frame = image.copy()
                frame += 1
                time.sleep(0.3)

            elif oled_mode == "YOUTUBE":
                if not youtube_url:
                    show_oled_text("YOUTUBE", "No URL set")
                    time.sleep(1)
                    current_mode = "DASHBOARD"
                    continue

                if cap is None or not cap.isOpened():
                    cap = release_capture(cap)
                    try:
                        show_oled_text("YOUTUBE", "Resolving...")
                        ydl_opts = {
                            "format": "worst[ext=mp4]/worst",
                            "quiet": True,
                            "no_warnings": True,
                            "noplaylist": True,
                        }
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(youtube_url, download=False)
                            stream_url = info.get("url")
                            if not stream_url:
                                raise RuntimeError("No stream URL")
                        cap = cv2.VideoCapture(stream_url)
                        if not cap.isOpened():
                            raise RuntimeError("Open stream failed")
                    except Exception as e:
                        print(f"YouTube error: {e}")
                        show_oled_text("YT ERROR", str(e)[:28])
                        cap = release_capture(cap)
                        time.sleep(2)
                        current_mode = "DASHBOARD"
                        continue

                ret, cv_frame = cap.read()
                if ret and cv_frame is not None:
                    img = cv_frame_to_oled(cv_frame, otsu=False)
                    device.display(img)
                    with lock:
                        latest_frame = img.copy()
                else:
                    cap = release_capture(cap)
                    current_mode = "DASHBOARD"
                time.sleep(0.03)

            elif oled_mode == "ANIMATION":
                # Local clip (MP4/.bin) or YouTube stream — all with Otsu B/W
                with lock:
                    path = animation_path
                    yt_url = animation_youtube_url
                    do_loop = animation_loop

                # Prefer local file; otherwise YouTube stream
                if path and Path(path).is_file():
                    source_key = path
                    yt_url = ""
                elif yt_url and is_youtube_url(yt_url):
                    source_key = f"yt:{yt_url}"
                    path = ""
                else:
                    show_oled_text("ANIMATION", "No clip set")
                    time.sleep(1)
                    current_mode = "DASHBOARD"
                    continue

                # Source changed while already in ANIMATION — reset decoder
                if source_key != last_anim_path:
                    cap = release_capture(cap)
                    anim_bin_frames = None
                    anim_bin_idx = 0
                    anim_yt_title = ""
                    anim_fps = 24.0
                    anim_t_start = 0.0
                    anim_frame_idx = 0
                    last_anim_path = source_key

                # ---- YouTube stream (Otsu, wall-clock pace + frame skip) ----
                if yt_url:
                    if cap is None or not cap.isOpened():
                        cap = release_capture(cap)
                        try:
                            show_oled_text("YT ANIM", "Resolving...")
                            stream_url, anim_yt_title, yt_fps = resolve_youtube_stream(
                                yt_url
                            )
                            show_oled_text("YT ANIM", (anim_yt_title or "YouTube")[:18])
                            cap = cv2.VideoCapture(stream_url)
                            if not cap.isOpened():
                                raise RuntimeError("Open YT stream failed")
                            # Prefer yt-dlp fps; CAP_PROP often wrong on streams
                            cap_fps = cap.get(cv2.CAP_PROP_FPS) or 0
                            anim_fps, anim_delay_s = animation_period(
                                yt_fps if yt_fps > 1 else cap_fps
                            )
                            anim_t_start = time.monotonic()
                            anim_frame_idx = 0
                            print(
                                f"Animation YouTube {anim_yt_title!r} "
                                f"fps={anim_fps:.1f} (yt={yt_fps} cap={cap_fps}) "
                                f"period={anim_delay_s:.3f}s [realtime+skip]"
                            )
                        except Exception as e:
                            print(f"Animation YouTube error: {e}")
                            show_oled_text("YT ERR", str(e)[:28])
                            cap = release_capture(cap)
                            time.sleep(2)
                            current_mode = "DASHBOARD"
                            continue

                    ok, cv_frame, anim_frame_idx = catch_up_video_frame(
                        cap, anim_frame_idx, anim_t_start, anim_fps
                    )
                    if ok and cv_frame is not None:
                        img = cv_frame_to_oled(cv_frame, otsu=True)
                        device.display(img)
                        with lock:
                            latest_frame = img.copy()
                        sleep_for_playback(anim_t_start, anim_frame_idx, anim_fps)
                    else:
                        cap = release_capture(cap)
                        if do_loop:
                            # Re-resolve stream next iteration (URLs expire)
                            anim_t_start = 0.0
                            anim_frame_idx = 0
                        else:
                            current_mode = "DASHBOARD"
                    continue

                # ---- Local file / bin pack ----
                path_obj = Path(path)

                # Pre-packed binary frames (fast, low CPU)
                if path_obj.suffix.lower() == ".bin":
                    if anim_bin_frames is None:
                        try:
                            show_oled_text("ANIMATION", path_obj.stem[:18])
                            anim_bin_frames = load_bin_animation(path_obj)
                            anim_bin_idx = 0
                            anim_fps, anim_delay_s = animation_period(
                                1000.0 / max(ANIMATION_FRAME_DELAY_MS, 1)
                            )
                            anim_t_start = time.monotonic()
                            print(
                                f"Loaded bin animation {path_obj.name}: "
                                f"{len(anim_bin_frames)} frames"
                            )
                        except Exception as e:
                            print(f"Animation bin error: {e}")
                            show_oled_text("ANIM ERR", str(e)[:28])
                            anim_bin_frames = None
                            time.sleep(2)
                            current_mode = "DASHBOARD"
                            continue

                    img = anim_bin_frames[anim_bin_idx]
                    device.display(img)
                    with lock:
                        latest_frame = img.copy()
                    anim_bin_idx += 1
                    if anim_bin_idx >= len(anim_bin_frames):
                        if do_loop:
                            anim_bin_idx = 0
                            anim_t_start = time.monotonic()
                        else:
                            anim_bin_frames = None
                            anim_bin_idx = 0
                            current_mode = "DASHBOARD"
                    else:
                        sleep_for_playback(anim_t_start, anim_bin_idx, anim_fps)
                    continue

                # Video file — Otsu B/W, wall-clock pace + frame skip
                if cap is None or not cap.isOpened():
                    cap = release_capture(cap)
                    try:
                        show_oled_text("ANIMATION", path_obj.stem[:18])
                        cap = cv2.VideoCapture(str(path_obj))
                        if not cap.isOpened():
                            raise RuntimeError("Open video failed")
                        cap_fps = cap.get(cv2.CAP_PROP_FPS) or 0
                        anim_fps, anim_delay_s = animation_period(cap_fps)
                        anim_t_start = time.monotonic()
                        anim_frame_idx = 0
                        print(
                            f"Animation video {path_obj.name} fps={anim_fps:.1f} "
                            f"period={anim_delay_s:.3f}s [realtime+skip]"
                        )
                    except Exception as e:
                        print(f"Animation open error: {e}")
                        show_oled_text("ANIM ERR", str(e)[:28])
                        cap = release_capture(cap)
                        time.sleep(2)
                        current_mode = "DASHBOARD"
                        continue

                ok, cv_frame, anim_frame_idx = catch_up_video_frame(
                    cap, anim_frame_idx, anim_t_start, anim_fps
                )
                if ok and cv_frame is not None:
                    img = cv_frame_to_oled(cv_frame, otsu=True)
                    device.display(img)
                    with lock:
                        latest_frame = img.copy()
                    sleep_for_playback(anim_t_start, anim_frame_idx, anim_fps)
                else:
                    if do_loop:
                        cap = release_capture(cap)
                        anim_t_start = 0.0
                        anim_frame_idx = 0
                        # reopen next loop iteration
                    else:
                        cap = release_capture(cap)
                        current_mode = "DASHBOARD"

            else:
                show_oled_text("UNKNOWN", str(mode)[:20])
                time.sleep(1)

        except Exception as e:
            print(f"Render Error: {e}")
            try:
                show_oled_text("ERROR", str(e)[:28])
            except Exception:
                pass
            cap = release_capture(cap)
            time.sleep(1)


# ==========================================
# FLASK WEB SERVER
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Raspberry Pi OLED Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Nunito:wght@600;800&display=swap');
        body {
            font-family: 'Nunito', system-ui, sans-serif;
            background: linear-gradient(160deg, #1a1025 0%, #2a1840 40%, #1a2035 100%);
            color: #ffe6f2; text-align: center; padding: 28px 16px; min-height: 100vh;
            margin: 0;
        }
        h1 { font-size: 1.6rem; margin: 0 0 6px; text-shadow: 0 0 18px #ff8dc7; }
        .subtitle { color: #c9b0ff; margin-bottom: 14px; font-size: 0.9rem; }
        .pill {
            display: inline-block; padding: 8px 16px; border-radius: 999px; margin: 4px;
            border: 1px solid #ff8dc7; background: #3a2458; color: #ffc2e8; font-size: 0.9rem;
        }
        .pill.armed { background: #5a1a2a; border-color: #ff6b6b; color: #ffc9c9; }
        .pill.ok { background: #1a3a2a; border-color: #6bffb0; color: #c9ffe0; }
        .card {
            background: rgba(40, 24, 60, 0.9); border: 2px solid #ff8dc7;
            border-radius: 22px; padding: 18px; max-width: 960px; margin: 0 auto 18px;
            box-shadow: 0 8px 32px rgba(255, 120, 180, 0.12); text-align: left;
        }
        .card h3 { margin: 0 0 12px; text-align: center; color: #ffd6ef; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
        .stat {
            background: #241636; border-radius: 14px; padding: 12px; text-align: center;
            border: 1px solid #5a3a7a;
        }
        .stat .v { font-size: 1.25rem; font-weight: 800; color: #fff; }
        .stat .l { font-size: 0.75rem; color: #c9b0ff; margin-top: 4px; }
        .btn {
            padding: 12px 20px; margin: 6px; font-size: 14px; cursor: pointer;
            border: none; border-radius: 999px; font-family: inherit; font-weight: 700;
            background: linear-gradient(135deg, #ff8dc7, #c48bff); color: #1a1025;
        }
        .btn.danger { background: linear-gradient(135deg, #ff6b6b, #c44b6b); color: #fff; }
        .btn.safe { background: linear-gradient(135deg, #6bffb0, #4bc4a0); color: #10251a; }
        .btn.secondary { background: #3a2458; color: #ffc2e8; border: 1px solid #ff8dc7; }
        .center { text-align: center; }
        img, video {
            border: 3px solid #ffb6de; max-width: 100%; width: 100%; height: auto;
            background: #000; display: block; margin: 10px auto 0; border-radius: 14px;
        }
        input {
            padding: 10px 14px; width: min(300px, 75vw); border-radius: 999px;
            border: 2px solid #c48bff; background: #241636; color: #ffe6f2; font-family: inherit;
        }
        ul.events { list-style: none; padding: 0; margin: 0; max-height: 280px; overflow-y: auto; }
        ul.events li {
            padding: 10px 12px; margin-bottom: 8px; border-radius: 12px;
            background: #241636; border-left: 4px solid #ff8dc7; font-size: 0.88rem;
        }
        ul.events li .t { color: #c9b0ff; font-size: 0.75rem; }
        ul.events li .k { color: #ff8dc7; font-weight: 800; }
        ul.events li.alert-kind { border-left-color: #ff6b6b; background: #3a1520; }
        .note { color: #a898c8; font-size: 0.8rem; text-align: center; margin-top: 8px; }
        .alert-banner {
            max-width: 960px; margin: 0 auto 18px; padding: 14px 18px;
            border-radius: 18px; background: #5a1a2a; border: 2px solid #ff6b6b;
            color: #ffd0d0; text-align: left; display: none;
        }
        .alert-banner.show { display: block; animation: pulse 1.2s ease infinite alternate; }
        @keyframes pulse { from { box-shadow: 0 0 0 rgba(255,100,100,0.2); } to { box-shadow: 0 0 22px rgba(255,100,100,0.45); } }
        .media-grid { display: grid; gap: 16px; }
        .media-card {
            background: #241636; border-radius: 16px; padding: 12px;
            border: 1px solid #5a3a7a;
        }
        .media-card h4 { margin: 0 0 8px; color: #ffd6ef; font-size: 0.95rem; }
        .media-card video, .media-card img {
            max-height: 320px; object-fit: contain; margin-top: 8px;
        }
        .tabs { text-align: center; margin-bottom: 10px; }
        .tabs a {
            display: inline-block; margin: 4px; padding: 8px 14px; border-radius: 999px;
            color: #ffc2e8; text-decoration: none; border: 1px solid #ff8dc7; font-size: 0.85rem;
        }
        .row-actions a {
            color: #ffb6de; margin-right: 10px; font-size: 0.8rem;
        }
    </style>
</head>
<body>
    <h1>&#128737; Pi OLED Dashboard</h1>
    <div class="subtitle">Room monitor + armable security · OLED + camera</div>
    <div class="center">
        <span class="pill {% if armed %}armed{% else %}ok{% endif %}">
            {% if armed %}● ARMED{% else %}○ DISARMED{% endif %}
        </span>
        <span class="pill">UI: <b>{{ mode }}</b></span>
        {% if recording %}<span class="pill armed">REC</span>{% endif %}
    </div>

    <div id="alertBanner" class="alert-banner {% if live_alert %}show{% endif %}">
        <strong>⚠ ALERT</strong>
        <div id="alertBannerText">
            {% if live_alert %}{{ live_alert.title }} — {{ live_alert.detail }}{% endif %}
        </div>
        <div class="note" style="text-align:left;margin:6px 0 0">Scroll to Recordings / Events below</div>
    </div>

    <div class="card">
        <h3>System status</h3>
        <div class="grid">
            <div class="stat"><div class="v">{{ temp }}</div><div class="l">Temperature</div></div>
            <div class="stat"><div class="v">{{ hum }}</div><div class="l">Humidity</div></div>
            <div class="stat"><div class="v">{{ motion }}</div><div class="l">Motion</div></div>
            <div class="stat"><div class="v">{{ sound }}</div><div class="l">Sound (mic soon)</div></div>
        </div>
        <div class="center" style="margin-top:14px">
            <form action="/set_armed" method="post" style="display:inline">
                <input type="hidden" name="armed" value="1"/>
                <button class="btn danger" type="submit">ARM system</button>
            </form>
            <form action="/set_armed" method="post" style="display:inline">
                <input type="hidden" name="armed" value="0"/>
                <button class="btn safe" type="submit">DISARM</button>
            </form>
        </div>
        <p class="note">When ARMED: PIR motion → OLED alert + snapshot + {{ rec_sec }}s video clip</p>
    </div>

    <div class="card">
        <h3>{% if mode == 'CAMERA' %}Live camera{% else %}Live OLED{% endif %}</h3>
        {% if mode == 'CAMERA' %}
        <video id="cam" autoplay muted playsinline controls preload="auto"
               src="/camera.mp4?t={{ ts }}" type="video/mp4"></video>
        <script>
        (function () {
            var v = document.getElementById('cam');
            if (!v) return;
            ['loadeddata','stalled','waiting'].forEach(function (ev) {
                v.addEventListener(ev, function () { try { v.play(); } catch (e) {} });
            });
            v.addEventListener('error', function () {
                setTimeout(function () { v.src = '/camera.mp4?t=' + Date.now(); try { v.play(); } catch (e) {} }, 3000);
            });
        })();
        </script>
        {% else %}
        <img src="/video_feed" alt="OLED Feed"/>
        {% endif %}
        <div class="center" style="margin-top:12px">
            <form action="/set_mode" method="post" style="display:inline">
                <button class="btn secondary" name="mode" value="DASHBOARD">OLED view</button>
                <button class="btn secondary" name="mode" value="CAMERA">Camera view</button>
            </form>
        </div>
    </div>

    <div class="card" id="recordings">
        <h3>Alert recordings &amp; snapshots</h3>
        <p class="note">Play clips from security events (browser MP4). Newest first.</p>
        <div class="media-grid">
        {% if media %}
            {% for m in media %}
            <div class="media-card" id="clip-{{ m.ts }}">
                <h4>🔔 {{ m.ts.replace('_', ' ') }}</h4>
                {% if m.video %}
                <video controls preload="metadata" playsinline
                       src="/media/recording/{{ m.video }}"></video>
                <div class="row-actions center" style="margin-top:8px">
                    <a href="/media/recording/{{ m.video }}" download>Download video</a>
                    {% if m.snapshot %}
                    <a href="/media/snapshot/{{ m.snapshot }}" target="_blank">Open snapshot</a>
                    {% endif %}
                </div>
                {% elif m.snapshot %}
                <img src="/media/snapshot/{{ m.snapshot }}" alt="snapshot {{ m.ts }}"/>
                <div class="row-actions center"><a href="/media/snapshot/{{ m.snapshot }}" download>Download photo</a></div>
                {% else %}
                <p class="note">Media missing for this stamp</p>
                {% endif %}
            </div>
            {% endfor %}
        {% else %}
            <p class="note">No recordings yet. ARM the system and trigger motion to capture.</p>
        {% endif %}
        </div>
    </div>

    <div class="card" id="events">
        <h3>Alert &amp; event history</h3>
        <ul class="events">
        {% if events %}
            {% for e in events %}
            <li class="{% if e.kind in ['MOTION','ALERT','SNAPSHOT','RECORDING','RECORDING_MP4','TEST'] %}alert-kind{% endif %}">
                <div class="t">{{ e.time }}</div>
                <span class="k">{{ e.kind }}</span> — {{ e.message }}
                {% if e.extra and e.extra.ts %}
                <div class="row-actions">
                    <a href="#clip-{{ e.extra.ts }}">View media</a>
                    <a href="/media/recording/motion_{{ e.extra.ts }}.mp4">Play video</a>
                    <a href="/media/snapshot/intruder_{{ e.extra.ts }}.jpg" target="_blank">Snapshot</a>
                </div>
                {% endif %}
            </li>
            {% endfor %}
        {% else %}
            <li><span class="t">No events yet</span><br/>Arm the system and walk in front of the PIR to test.</li>
        {% endif %}
        </ul>
        <p class="note">Log file: ~/security/logs/security_events.log</p>
    </div>

    <div class="card center" id="animations">
        <h3>OLED animations</h3>
        <p class="note">Plays on the physical 128×64 OLED (Otsu B/W, same as oled-animation). Upload from this portal or place files in <code>~/oled_animations</code>.</p>
        {% if anim_flash %}
        <p class="note" style="color:#7dffa3;font-weight:600">{{ anim_flash }}</p>
        {% endif %}
        {% if anim_error %}
        <p class="note" style="color:#ff8a8a;font-weight:600">{{ anim_error }}</p>
        {% endif %}

        <form action="/upload_animation" method="post" enctype="multipart/form-data"
              style="margin:14px 0 18px;text-align:left;max-width:520px;margin-left:auto;margin-right:auto;padding:12px;border:1px solid rgba(255,255,255,0.12);border-radius:10px">
            <h4 style="margin:0 0 8px">Upload video</h4>
            <p class="note" style="margin-top:0">Formats: mp4, avi, mov, mkv, webm · max {{ anim_max_mb }} MB · short clips work best on OLED</p>
            <input type="file" name="video" accept="video/*,.mp4,.avi,.mov,.mkv,.webm" required
                   style="width:100%;margin:8px 0;color:inherit"/>
            <div class="row-actions" style="margin-top:8px;flex-wrap:wrap;gap:10px">
                <label style="font-size:0.85rem">
                    <input type="checkbox" name="loop" value="1" checked/> Loop when playing
                </label>
                <label style="font-size:0.85rem">
                    <input type="checkbox" name="play" value="1" checked/> Play on OLED after upload
                </label>
            </div>
            <button class="btn" type="submit" style="margin-top:12px">Upload</button>
        </form>

        <form action="/youtube_animation" method="post"
              style="margin:14px 0 18px;text-align:left;max-width:520px;margin-left:auto;margin-right:auto;padding:12px;border:1px solid rgba(255,255,255,0.12);border-radius:10px">
            <h4 style="margin:0 0 8px">YouTube video</h4>
            <p class="note" style="margin-top:0">Stream with Otsu B/W (same as local animations), and/or save a low-res copy into the library for offline play.</p>
            <input type="url" name="url" placeholder="https://www.youtube.com/watch?v=..." required
                   style="width:100%;margin:8px 0;padding:8px;border-radius:6px;border:1px solid rgba(255,255,255,0.2);background:rgba(0,0,0,0.25);color:inherit"/>
            <div class="row-actions" style="margin-top:8px;flex-wrap:wrap;gap:10px">
                <label style="font-size:0.85rem">
                    <input type="checkbox" name="play" value="1" checked/> Play on OLED
                </label>
                <label style="font-size:0.85rem">
                    <input type="checkbox" name="save" value="1"/> Save to library
                </label>
                <label style="font-size:0.85rem">
                    <input type="checkbox" name="loop" value="1" checked/> Loop
                </label>
            </div>
            <button class="btn" type="submit" style="margin-top:12px">Use YouTube</button>
        </form>

        {% if animations %}
        <div class="media-grid" style="text-align:left">
            {% for a in animations %}
            <div class="media-card">
                <h4>{{ a.name }}</h4>
                <p class="note">{{ a.kind }} · {{ a.size_kb }} KB</p>
                <form action="/play_animation" method="post" style="display:inline">
                    <input type="hidden" name="name" value="{{ a.name }}"/>
                    <label style="font-size:0.85rem;margin-right:8px">
                        <input type="checkbox" name="loop" value="1" checked/> Loop
                    </label>
                    <button class="btn" type="submit">Play on OLED</button>
                </form>
                <form action="/delete_animation" method="post" style="display:inline;margin-left:6px"
                      onsubmit="return confirm('Delete {{ a.name }}?');">
                    <input type="hidden" name="name" value="{{ a.name }}"/>
                    <button class="btn secondary" type="submit">Delete</button>
                </form>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <p class="note">No animations yet — upload a video or use YouTube above.</p>
        {% endif %}
        {% if mode == 'ANIMATION' %}
        <p class="note" style="margin-top:10px">
            Now playing:
            {% if animation_playing %}{{ animation_playing }}
            {% elif animation_yt %}YouTube stream
            {% else %}animation{% endif %}
        </p>
        <form action="/stop_animation" method="post" style="margin-top:12px">
            <button class="btn secondary" type="submit">Stop animation → dashboard</button>
        </form>
        {% endif %}
    </div>

    <div class="card center">
        <h3>Extras</h3>
        <form action="/play_youtube" method="post">
            <input type="text" name="url" placeholder="YouTube URL (legacy OLED play, no Otsu)" required/>
            <button class="btn" type="submit">Play on OLED</button>
        </form>
        <p class="note">Prefer <strong>OLED animations → YouTube video</strong> for Otsu B/W quality.</p>
        <form action="/test_alert" method="post" style="margin-top:10px">
            <button class="btn secondary" type="submit">Test alert (no force record)</button>
        </form>
    </div>
    <script>
    // Poll status so alerts appear live without full refresh
    (function () {
        var banner = document.getElementById('alertBanner');
        var text = document.getElementById('alertBannerText');
        var lastSig = '';
        function tick() {
            fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
                if (s.live_alert) {
                    banner.classList.add('show');
                    var sig = s.live_alert.title + '|' + s.live_alert.detail;
                    if (sig !== lastSig) {
                        lastSig = sig;
                        text.textContent = s.live_alert.title + ' — ' + s.live_alert.detail;
                    }
                } else {
                    banner.classList.remove('show');
                }
            }).catch(function () {});
        }
        setInterval(tick, 2500);
        tick();
    })();
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    with lock:
        events = list(recent_events)[:40]
        t = shared_room_temp
        h = shared_room_hum
        mot = last_motion
        arm = armed
        rec = recording
        snd = sound_level
        a_until = alert_until
        a_title = alert_title
        a_detail = alert_detail
    live = None
    if time.monotonic() < a_until:
        live = {"title": a_title, "detail": a_detail}
    temp_s = f"{t:.1f}°C" if t is not None else "--"
    hum_s = f"{h:.0f}%" if h is not None else "--"
    media = list_security_media()
    animations = list_animations()
    return render_template_string(
        HTML_TEMPLATE,
        mode=current_mode,
        ts=int(time.time()),
        armed=arm,
        recording=rec,
        temp=temp_s,
        hum=hum_s,
        motion="ACTIVE" if mot else "CLEAR",
        sound=snd,
        events=events,
        rec_sec=RECORD_SECONDS,
        media=media,
        live_alert=live,
        animations=animations,
        animation_playing=Path(animation_path).name if animation_path else "",
        animation_yt=bool(animation_youtube_url),
        anim_flash=request.args.get("anim_ok") or "",
        anim_error=request.args.get("anim_err") or "",
        anim_max_mb=ANIMATION_MAX_UPLOAD_MB,
    )


def generate_mjpeg():
    """Dashboard / non-camera: stream OLED mirror as MJPEG."""
    global latest_frame
    while True:
        with lock:
            oled = latest_frame
        if oled is None:
            time.sleep(0.1)
            continue
        img_byte_arr = io.BytesIO()
        oled.convert("RGB").resize((512, 256), Image.NEAREST).save(
            img_byte_arr, format="JPEG"
        )
        frame_bytes = img_byte_arr.getvalue()
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
        )
        time.sleep(0.1)


@app.route("/video_feed")
def video_feed():
    return Response(
        generate_mjpeg(), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


# Live web stream process tracking (prevents orphan rpicam/ffmpeg holding the CSI cam)
_live_stream_procs = []  # list[subprocess.Popen]
_live_stream_procs_lock = threading.Lock()


def _kill_proc(proc, timeout=2):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        pass


def _force_stop_live_stream():
    """Kill any tracked live-stream helpers and clear the list."""
    with _live_stream_procs_lock:
        procs = list(_live_stream_procs)
        _live_stream_procs.clear()
    for proc in procs:
        _kill_proc(proc)
    # Safety net: orphaned helpers from a previous crash/client disconnect
    try:
        subprocess.run(
            ["pkill", "-f", r"rpicam-vid -t 0 -n --width"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def generate_mp4_stream():
    """
    Smooth live MP4 using Raspberry Pi *hardware* H.264 encode.

    Pipeline:
      rpicam-vid (HW encode) -> annex-B H.264 -> ffmpeg -c copy -> fMP4

    Hardened against:
      - orphan rpicam holding the exclusive libcamera device
      - stderr PIPE deadlocks
      - client disconnect leaving a blocked read forever
    """
    import select

    if current_mode != "CAMERA":
        return

    # Serialize camera access (libcamera is exclusive). If a previous client
    # stalled, force-kill its pipeline and take over so the UI recovers.
    got_lock = camera_stream_lock.acquire(blocking=False)
    if not got_lock:
        print("Camera stream busy — taking over previous client")
        _force_stop_live_stream()
        got_lock = camera_stream_lock.acquire(timeout=5)
        if not got_lock:
            print("Camera stream lock still held — giving up")
            return

    rpicam = None
    ffmpeg = None
    try:
        rpicam_cmd = [
            "rpicam-vid",
            "-t",
            "0",
            "-n",
            "--width",
            str(WEB_W),
            "--height",
            str(WEB_H),
            "--framerate",
            str(WEB_FPS),
            "--codec",
            "h264",
            "--profile",
            "baseline",
            "--inline",
            "--flush",
            "--bitrate",
            str(WEB_BITRATE_BPS),
            "-o",
            "-",
        ]
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-f",
            "h264",
            "-r",
            str(WEB_FPS),
            "-i",
            "pipe:0",
            "-c:v",
            "copy",
            "-an",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
        ]

        print("Starting hardware camera stream:", " ".join(rpicam_cmd))
        # DEVNULL stderr: PIPE without a reader can fill and freeze rpicam/ffmpeg
        rpicam = subprocess.Popen(
            rpicam_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            start_new_session=True,
        )
        ffmpeg = subprocess.Popen(
            ffmpeg_cmd,
            stdin=rpicam.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            start_new_session=True,
        )
        if rpicam.stdout:
            rpicam.stdout.close()

        with _live_stream_procs_lock:
            _live_stream_procs[:] = [rpicam, ffmpeg]

        assert ffmpeg.stdout is not None
        fd = ffmpeg.stdout.fileno()
        got_data = False
        idle_s = 0.0
        while current_mode == "CAMERA":
            ready, _, _ = select.select([fd], [], [], 2.0)
            if not ready:
                idle_s += 2.0
                # Fail fast if encoder never produces bytes (cam error / busy)
                if not got_data and idle_s >= 8.0:
                    print("Hardware stream: no data within 8s — aborting")
                    break
                # Client still connected but stream stalled
                if got_data and idle_s >= 20.0:
                    print("Hardware stream: stalled 20s — aborting")
                    break
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            got_data = True
            idle_s = 0.0
            yield chunk
    except GeneratorExit:
        pass
    except Exception as e:
        print(f"Hardware stream error: {e}")
    finally:
        with _live_stream_procs_lock:
            _live_stream_procs[:] = []
        _kill_proc(ffmpeg)
        _kill_proc(rpicam)
        try:
            camera_stream_lock.release()
        except Exception:
            pass
        print("Hardware camera stream stopped")


@app.route("/camera.mp4")
def camera_mp4():
    """720p hardware-encoded live MP4 for camera mode."""
    if current_mode != "CAMERA":
        return "Switch to Camera Mode first", 409
    if recording:
        return "Camera busy recording security clip", 409
    return Response(
        stream_with_context(generate_mp4_stream()),
        mimetype="video/mp4",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.route("/set_mode", methods=["POST"])
def set_mode():
    global current_mode
    mode = (request.form.get("mode") or "DASHBOARD").upper()
    if mode in ("DASHBOARD", "CAMERA", "YOUTUBE", "ANIMATION"):
        prev = current_mode
        current_mode = mode
        # Leaving camera view must free the CSI camera for security captures
        if prev == "CAMERA" and mode != "CAMERA":
            _force_stop_live_stream()
    return "<script>window.location.href='/';</script>"


@app.route("/set_armed", methods=["POST"])
def set_armed():
    global armed
    val = request.form.get("armed", "0")
    armed = val in ("1", "true", "on", "yes", "ARMED")
    log_security_event(
        "ARMED" if armed else "DISARMED",
        "System armed" if armed else "System disarmed",
    )
    set_alert(
        "ARMED" if armed else "DISARMED",
        "Watching..." if armed else "Standby",
        seconds=4,
    )
    return "<script>window.location.href='/';</script>"


@app.route("/test_alert", methods=["POST"])
def test_alert():
    """OLED + log only — does not force camera capture."""
    log_security_event("TEST", "Manual test alert from dashboard")
    set_alert("TEST ALERT", "Dashboard ping", seconds=6)
    return "<script>window.location.href='/';</script>"


@app.route("/play_youtube", methods=["POST"])
def play_youtube():
    global current_mode, youtube_url
    youtube_url = request.form["url"]
    current_mode = "YOUTUBE"
    return "<script>window.location.href='/';</script>"


@app.route("/play_animation", methods=["POST"])
def play_animation():
    """Start local oled-animation clip on the physical OLED."""
    global current_mode, animation_path, animation_loop, animation_youtube_url
    name = request.form.get("name") or ""
    path = resolve_animation(name)
    if not path:
        return (
            "<script>alert('Unknown animation');window.location.href='/#animations';</script>",
            400,
        )
    animation_path = str(path)
    animation_youtube_url = ""
    animation_loop = request.form.get("loop", "1") in ("1", "true", "on", "yes")
    # Leaving camera frees CSI for security; animation only uses OLED
    if current_mode == "CAMERA":
        _force_stop_live_stream()
    current_mode = "ANIMATION"
    log_security_event("ANIMATION", f"Playing {path.name} loop={animation_loop}")
    return "<script>window.location.href='/#animations';</script>"


@app.route("/stop_animation", methods=["POST"])
def stop_animation():
    global current_mode, animation_path, animation_youtube_url
    current_mode = "DASHBOARD"
    animation_path = ""
    animation_youtube_url = ""
    return "<script>window.location.href='/#animations';</script>"


@app.route("/youtube_animation", methods=["POST"])
def youtube_animation():
    """
    Use a YouTube URL with the OLED animation feature.
    Options: stream (Otsu play), save to library, or both.
    """
    global current_mode, animation_path, animation_youtube_url, animation_loop

    url = (request.form.get("url") or "").strip()
    if not is_youtube_url(url):
        return _anim_redirect(err="Enter a valid YouTube URL"), 400

    play_now = request.form.get("play", "1") in ("1", "true", "on", "yes")
    save = request.form.get("save") in ("1", "true", "on", "yes")
    animation_loop = request.form.get("loop", "1") in ("1", "true", "on", "yes")

    if not play_now and not save:
        return _anim_redirect(err="Choose Play and/or Save"), 400

    saved_path = None
    if save:
        try:
            print(f"YouTube animation download: {url}")
            saved_path = download_youtube_animation(url)
            log_security_event(
                "ANIMATION_YT_SAVE",
                f"Saved {saved_path.name} from YouTube",
            )
        except Exception as e:
            print(f"YouTube animation download error: {e}")
            return _anim_redirect(err=f"YouTube save failed: {e}"), 500

    if play_now:
        if current_mode == "CAMERA":
            _force_stop_live_stream()
        if saved_path is not None:
            # Prefer offline file after save (stable loops)
            animation_path = str(saved_path)
            animation_youtube_url = ""
            current_mode = "ANIMATION"
            log_security_event(
                "ANIMATION",
                f"Playing saved YT {saved_path.name} loop={animation_loop}",
            )
            return _anim_redirect(
                ok=f"Saved and playing {saved_path.name}"
            )
        # Stream live with Otsu
        animation_path = ""
        animation_youtube_url = url
        current_mode = "ANIMATION"
        log_security_event(
            "ANIMATION", f"Streaming YouTube loop={animation_loop}"
        )
        return _anim_redirect(ok="Playing YouTube on OLED (Otsu)")

    return _anim_redirect(ok=f"Saved {saved_path.name}")


def _anim_redirect(ok=None, err=None):
    """Redirect back to animations card with a status message."""
    from urllib.parse import quote

    if err:
        return f"<script>window.location.href='/?anim_err={quote(err)}#animations';</script>"
    return f"<script>window.location.href='/?anim_ok={quote(ok or 'OK')}#animations';</script>"


@app.route("/upload_animation", methods=["POST"])
def upload_animation():
    """Accept a video from the portal and store it in ANIMATION_DIR."""
    global current_mode, animation_path, animation_loop, animation_youtube_url

    f = request.files.get("video")
    if f is None or not f.filename:
        return _anim_redirect(err="No file selected"), 400

    safe = sanitize_animation_filename(f.filename)
    if not safe:
        return (
            _anim_redirect(err="Unsupported type. Use mp4/avi/mov/mkv/webm"),
            400,
        )

    dest = unique_animation_path(safe)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        f.save(str(tmp))
        size = tmp.stat().st_size
        if size <= 0:
            tmp.unlink(missing_ok=True)
            return _anim_redirect(err="Empty file"), 400
        if size > ANIMATION_MAX_UPLOAD_BYTES:
            tmp.unlink(missing_ok=True)
            return (
                _anim_redirect(
                    err=f"File too large (max {ANIMATION_MAX_UPLOAD_MB} MB)"
                ),
                400,
            )
        tmp.replace(dest)
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        print(f"Upload animation error: {e}")
        return _anim_redirect(err="Upload failed"), 500

    log_security_event(
        "ANIMATION_UPLOAD",
        f"Uploaded {dest.name} ({round(size / 1024, 1)} KB)",
    )

    play_now = request.form.get("play", "1") in ("1", "true", "on", "yes")
    animation_loop = request.form.get("loop", "1") in ("1", "true", "on", "yes")
    if play_now:
        if current_mode == "CAMERA":
            _force_stop_live_stream()
        animation_path = str(dest)
        animation_youtube_url = ""
        current_mode = "ANIMATION"
        log_security_event(
            "ANIMATION", f"Playing {dest.name} loop={animation_loop}"
        )
        return _anim_redirect(ok=f"Uploaded and playing {dest.name}")
    return _anim_redirect(ok=f"Uploaded {dest.name}")


@app.route("/delete_animation", methods=["POST"])
def delete_animation():
    """Remove a clip from ANIMATION_DIR (stops playback if it was active)."""
    global current_mode, animation_path, animation_youtube_url
    name = request.form.get("name") or ""
    path = resolve_animation(name)
    if not path:
        return _anim_redirect(err="Unknown file"), 400
    try:
        # Stop if this clip is currently playing
        if animation_path and Path(animation_path).resolve() == path.resolve():
            current_mode = "DASHBOARD"
            animation_path = ""
            animation_youtube_url = ""
        path.unlink()
        log_security_event("ANIMATION_DELETE", f"Deleted {path.name}")
        return _anim_redirect(ok=f"Deleted {path.name}")
    except Exception as e:
        print(f"Delete animation error: {e}")
        return _anim_redirect(err="Delete failed"), 500


@app.errorhandler(413)
def upload_too_large(_e):
    return (
        _anim_redirect(err=f"File too large (max {ANIMATION_MAX_UPLOAD_MB} MB)"),
        413,
    )


@app.route("/api/status")
def api_status():
    with lock:
        live = None
        if time.monotonic() < alert_until:
            live = {"title": alert_title, "detail": alert_detail}
        return jsonify(
            {
                "armed": armed,
                "recording": recording,
                "motion": last_motion,
                "sound": sound_level,
                "temp": shared_room_temp,
                "humidity": shared_room_hum,
                "mode": current_mode,
                "animation": Path(animation_path).name if animation_path else None,
                "animation_youtube": bool(animation_youtube_url),
                "ip": get_ip(),
                "events": list(recent_events)[:20],
                "mic_ready": False,  # INMP441 not connected yet
                "live_alert": live,
            }
        )


@app.route("/api/events")
def api_events():
    with lock:
        return jsonify(list(recent_events)[:40])


@app.route("/api/media")
def api_media():
    return jsonify(list_security_media())


@app.route("/media/snapshot/<name>")
def media_snapshot(name):
    name = safe_media_name(name)
    if not name:
        abort(400)
    path = DIR_SNAPSHOTS / name
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="image/jpeg")


@app.route("/media/recording/<name>")
def media_recording(name):
    """Serve MP4 for browser playback; remux .h264 on demand if needed."""
    name = safe_media_name(name)
    if not name:
        abort(400)
    path = DIR_RECORDINGS / name
    if name.endswith(".mp4"):
        if not path.is_file():
            # try remux from sibling h264
            h264 = path.with_suffix(".h264")
            remux_h264_to_mp4(h264, path)
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype="video/mp4", conditional=True)
    if name.endswith(".h264"):
        if not path.is_file():
            abort(404)
        mp4 = remux_h264_to_mp4(path)
        if mp4 and mp4.is_file():
            return send_file(mp4, mimetype="video/mp4", conditional=True)
        return send_file(path, mimetype="application/octet-stream", as_attachment=True)
    abort(404)


if __name__ == "__main__":
    log_security_event("BOOT", "Pi OLED dashboard started")
    t_oled = threading.Thread(target=update_display, name="oled-dashboard", daemon=True)
    t_sec = threading.Thread(target=security_engine, name="security-engine", daemon=True)
    t_oled.start()
    t_sec.start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
