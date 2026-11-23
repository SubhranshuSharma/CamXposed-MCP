# CamXposed MCP

MCP server for ONVIF cameras: discovery, snapshots, PTZ, audio capture and speaker playback. The launcher starts the local MCP server, checks that it is healthy, creates the ngrok HTTPS tunnel, prints the remote MCP URL, and shuts both processes down together.

## One-command setup

Clone once:

```bash
git clone https://github.com/SubhranshuSharma/CamXposed-MCP.git
cd CamXposed-MCP
```

### Linux (Ubuntu/Debian)

Install the small set of OS tools:

```bash
sudo apt update
sudo apt install -y python3 python3-pip ffmpeg nodejs npm
```

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended), then run:

```bash
uv run run.py
```

Or use Python directly; the launcher installs missing Python requirements automatically:

```bash
python3 run.py
```

If ngrok CLI is installed and authenticated with `ngrok config add-authtoken YOUR_TOKEN`, the launcher reuses that configuration. Otherwise it automatically prepares the Node SDK and prompts for an ngrok token (or reads `NGROK_AUTHTOKEN`).

### Android (Termux — Play Store or F-Droid)

The launcher detects the active Termux prefix at runtime, so it does not assume the Play Store or F-Droid installation path.

```bash
pkg update
pkg install -y git python nodejs ffmpeg
git clone https://github.com/SubhranshuSharma/CamXposed-MCP.git ~/camxposed-mcp
cd ~/camxposed-mcp
```

Set your ngrok token once for the current shell, then launch:

```bash
export NGROK_AUTHTOKEN="YOUR_NGROK_AUTHTOKEN"
python run.py
```

The first run installs the Android ARM64 ngrok Node SDK in Termux's user cache. It also applies the Termux `libpython` preload automatically when the matching shared library exists. No manual `LD_PRELOAD`, separate server session, separate tunnel session, or global npm install is needed.

You can also use `uv run run.py` if uv is installed and working in your Termux environment. Plain `python run.py` is the supported fallback.

## Configuration

Optional environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `NGROK_AUTHTOKEN` | — | ngrok token for the Node SDK / Termux |
| `CAMXPOSED_STATIC_DOMAIN` | `your-subdomain.ngrok-free.app` | Reserved ngrok domain |
| `CAMXPOSED_PORT` | `8000` | Local MCP server port |
| `CAMXPOSED_ONVIF_USER` | `admin` | Camera username |
| `CAMXPOSED_ONVIF_PASS` | empty | Camera password |
| `CAMXPOSED_LOG_DIR` | system temporary directory | Runtime logs |

If no ngrok token is set for the Node SDK, the launcher prompts for it without echoing the token. Do not commit tokens or camera passwords.

Optional ONVIF credentials (default: `admin` with a blank password):

```bash
export CAMXPOSED_ONVIF_USER="admin"
export CAMXPOSED_ONVIF_PASS=""
```

## Camera behaviour

- Camera tools automatically use the selected camera.
- If none is selected, the server discovers cameras and selects the first usable one.
- Stale camera addresses are refreshed through discovery.
- NVR/DVR devices are excluded from the camera list.
- Default ONVIF login is `admin` with a blank password. Set `CAMXPOSED_ONVIF_USER` and `CAMXPOSED_ONVIF_PASS` to override it.

## Network requirements

- The host running this launcher must have LAN access to the camera.
- ONVIF discovery uses WS-Discovery multicast: UDP `239.255.255.250:3702`.
- A remote VPS cannot discover cameras on a private LAN without a routed connection.
- The local server binds to `127.0.0.1`; only ngrok exposes the MCP endpoint.
- Keep the launcher running while the remote MCP client is connected.

## Security

The ngrok URL provides connectivity, not authentication. Share it only with trusted MCP clients.
