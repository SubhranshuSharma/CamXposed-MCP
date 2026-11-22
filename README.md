# कैमXposed MCP — Native LAN Smart IP Camera Control, Vision & PTZ

कैमXposed MCP enables direct, low-latency control of smart IP cameras over the local area network (LAN) natively via ONVIF, RTSP, and direct Anyka speaker streaming, with automated fallback to the कैमXposed Android bridge.

> **Python Requirement**: Python `>= 3.10` with no upper version restriction (runs seamlessly on the latest Python 3.11, 3.12, 3.13+).

---

## ⚡ Quick Start: Running on Linux or Termux (Android)

### Option A: Running on Linux (Desktop / Server)

#### 1. System Requirements & Setup
Ensure `ffmpeg` and `python` (>=3.10) are installed:
```bash
sudo apt update && sudo apt install -y ffmpeg python3 python3-pip
```

#### 2. Install Dependencies
```bash
git clone https://github.com/SubhranshuSharma/camxposed-mcp.git
cd camxposed-mcp
pip install -r requirements.txt
# Or using uv:
# uv sync
```

#### 3. Run Locally (Stdio MCP Transport)
```bash
python3 server.py
# Or with uv:
# uv run server.py
```

#### 4. Run with Ngrok for ChatGPT / Remote MCP Clients
```bash
# Configure your ngrok auth token once:
ngrok config add-authtoken <YOUR_NGROK_AUTHTOKEN>

# Start MCP server and tunnel:
python3 run_chatgpt_mcp.py
```

---

### Option B: Running on Android via Termux

Termux runs natively on Android without root and communicates directly with cameras on the local Wi-Fi.

#### 1. Install Packages in Termux
```bash
pkg update -y
pkg install python python-pip ffmpeg rust binutils libxml2 libxslt git curl -y
```

#### 2. Clone and Install Dependencies
```bash
git clone https://github.com/SubhranshuSharma/camxposed-mcp.git
cd camxposed-mcp
pip install -r requirements.txt
```

#### 3. Install Ngrok (ARM64)
```bash
curl -s https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz -o ngrok.tgz
tar -xvzf ngrok.tgz -C $PREFIX/bin/
rm ngrok.tgz

# Authenticate ngrok:
ngrok config add-authtoken <YOUR_NGROK_AUTHTOKEN>
```

#### 4. Run Server & Tunnel
```bash
python3 run_chatgpt_mcp.py
```

---

## 🌐 Connecting to ChatGPT / Claude Custom MCP

When running `run_chatgpt_mcp.py`:
1. **Public MCP URL**: `https://<your-domain>.ngrok-free.dev/mcp`
2. **Authentication**: Select **"No authentication"** (or `None`).
3. **Transport**: Streamable HTTP (`POST /mcp`).

---

## 🛠️ Architecture & Priorities

The MCP server enforces direct local LAN communication first:

1. **Speaker Talkback (`camera_speak`)**:
   - **Priority 1 (Native LAN)**: Streams G.711 A-law 8 kHz mono in 160-byte (20 ms) frames directly to the camera's Anyka TCP port `28772`.
   - **Instant Barge-In (`camera_speak_stop`)**: Abortive TCP close (`TCP RST`) instantly halts playback on the physical camera without waiting for decoder buffer drain.
   - **Speaker Volume (`camera_speaker_volume`)**: Scaled via FFmpeg filter before transmission; defaults to **50% (half volume)** and is persisted across restarts.
   - **Priority 2 (Fallback)**: Streams via the कैमXposed Android Gateway `/v1/cameras/{cid}/speak-stream`.

2. **Pan-Tilt-Zoom (PTZ)**:
   - **Time-Limited Continuous Move (`camera_move`)**: Directional moves (`left`, `right`, `up`, `down`) clamped to 100–2000 ms with host-enforced background auto-stop timers to prevent runaway camera rotation.
   - **Relative PTZ (`camera_ptz_relative_move`)**: Precise distance offset translation (-1.0 to 1.0) along X/Y axes.
   - **Presets (`camera_presets`)**: List, save, goto, and remove PTZ positions.

3. **Vision & Audio Capture**:
   - **Snapshot (`camera_observe`)**: Direct HTTP Digest snapshot (`/snapshot`) or ONVIF `GetSnapshotUri` with mechanical settling delay.
   - **Microphone (`camera_listen`)**: Direct RTSP audio capture via FFmpeg converting to standard PCM WAV format.

---

## 🔒 Security
No credentials, camera passwords, or ngrok authtokens are stored in this repository. All credentials should be provided via environment variables (`CAMXPOSED_ONVIF_USER`, `CAMXPOSED_ONVIF_PASS`, `CAMXPOSED_STATIC_DOMAIN`, etc.) or local configuration files.
