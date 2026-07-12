# Raspberry Pi OLED Dashboard

**SSD1306 128×64 status dashboard, room security, and video animations for Raspberry Pi.**

A Flask web portal plus a live monochrome OLED UI: system stats, DHT22 environment, PIR motion alerts with camera clips, and Otsu black‑and‑white video playback (local files or YouTube).

![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi-c51a4a?logo=raspberrypi&logoColor=white)
![Display](https://img.shields.io/badge/display-SSD1306%20128×64-lightgrey)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Keywords:** Raspberry Pi OLED, SSD1306 dashboard, Pi I2C display, room security PIR, Pi video on OLED, YouTube on SSD1306, DHT22 Pi dashboard.

---

## Features

| Area | Description |
|------|-------------|
| **OLED UI** | Rotating slides: room guard, environment, CPU/RAM/temp, fun sprite, system overview |
| **Web portal** | Live OLED mirror, camera view, arm/disarm, event history (`http://<pi-ip>:5000`) |
| **Security** | PIR-triggered alerts, snapshots, short H.264/MP4 recordings |
| **Animations** | Play clips on the OLED with **Otsu** 1-bit conversion |
| **Upload** | Browser upload (mp4/avi/mov/mkv/webm) |
| **YouTube** | Stream to OLED with real-time pacing + frame skip, or save offline |
| **Sensors** | DHT22, Pi thermal zone, psutil CPU/RAM, optional Docker count |

---

## Hardware

### SSD1306 0.96″ OLED (4-pin I2C)

| OLED | Raspberry Pi | Physical pin |
|------|--------------|--------------|
| VCC | 3.3 V | 1 |
| GND | GND | 6 |
| SCL | GPIO3 (SCL1) | 5 |
| SDA | GPIO2 (SDA1) | 3 |

- Default I2C address: **0x3C**
- Enable I2C: `sudo raspi-config` → Interface Options → I2C  
- Verify: `i2cdetect -y 1`

### Optional

| Device | GPIO (BCM) |
|--------|------------|
| PIR motion | 17 |
| DHT22 | 4 |
| CSI camera | Camera port (`rpicam-*` / libcamera) |

---

## Install

```bash
git clone https://github.com/ksanjeev284/raspberry-pi-oled-dashboard.git
cd raspberry-pi-oled-dashboard

sudo apt update
sudo apt install -y python3-venv python3-dev i2c-tools libgpiod-dev \
  libcamera-apps ffmpeg

python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt

python oled_dashboard.py
```

Open **http://\<pi-ip\>:5000**

Data directories (created automatically under your home folder):

- `~/oled_animations` — animation library  
- `~/security/` — recordings, snapshots, event log  

---

## systemd

```bash
# Edit User= and paths in the unit first
sudo cp systemd/raspberry-pi-oled-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now raspberry-pi-oled-dashboard.service
```

---

## Configuration

Edit the top of `oled_dashboard.py` (or rely on defaults):

| Setting | Default | Purpose |
|---------|---------|---------|
| `OLED_I2C_PORT` | `1` | I2C bus |
| `OLED_I2C_ADDR` | `0x3C` | Display address |
| `ANIMATION_DIR` | `~/oled_animations` | Local clips |
| `ANIMATION_MAX_UPLOAD_MB` | `80` | Upload size limit |
| `SECURITY_ROOT` | `~/security` | Media + logs |

---

## Project layout

```text
raspberry-pi-oled-dashboard/
├── oled_dashboard.py
├── requirements.txt
├── systemd/raspberry-pi-oled-dashboard.service
├── tools/video_to_frames.py
├── animations/                 # optional media (gitignored)
├── LICENSE
└── README.md
```

---

## Optional: offline frame export

```bash
python tools/video_to_frames.py clip.mp4 -o frames_data.h
```

Exports packed 128×64 frames for other firmware. On Raspberry Pi, play video directly in the dashboard.

---

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| Blank OLED | Wiring, 3.3 V, `i2cdetect -y 1` shows `3c` |
| I2C/GPIO permission | Add user to `i2c` and `gpio` groups; reboot |
| YouTube errors | `pip install -U yt-dlp` |
| Slow video | Short/low-res sources; playback skips frames to stay real-time |
| Camera busy | Leave camera view; wait for security capture to finish |

---

## License

MIT — see [LICENSE](LICENSE).
