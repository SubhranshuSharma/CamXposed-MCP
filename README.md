# CamXposed MCP

MCP server for ONVIF cameras: discovery, snapshots, PTZ, audio capture and speaker playback. The launcher starts the local MCP server, checks that it is healthy, creates the ngrok HTTPS tunnel, prints the remote MCP URL, and shuts both processes down together.

## One-command setup

Run this once on Linux or Android Termux (Play Store or F-Droid):

```bash
curl -fsSL https://raw.githubusercontent.com/SubhranshuSharma/CamXposed-MCP/main/install.sh | sh
```

The installer:
- Installs the required OS packages (Python, Git, FFmpeg, Node.js/npm).
- Detects Termux versus Linux and supports Debian/Ubuntu, Fedora/RHEL, and Arch Linux.
- Clones the repository to `~/camxposed-mcp`, or fast-forward updates that checkout if it is clean.
- Does not overwrite a checkout containing local modifications. Set `CAMXPOSED_INSTALL_DIR` to choose another location.

After setup, start everything with one command:

Linux:
```bash
cd ~/camxposed-mcp
python3 run.py
```

Termux:
```bash
cd ~/camxposed-mcp
python run.py
```

The launcher installs missing Python packages and the ngrok Node SDK automatically. On Termux it also applies the matching `libpython` preload when available. The first launch asks for your ngrok token once (hidden input) and saves it under `~/.config/camxposed-mcp/ngrok_token`; later launches reuse it. No separate server or tunnel command, manual `LD_PRELOAD`, or global npm install is needed.

To change the saved token later:

```bash
python run.py --change-token
```

## Configuration

Optional environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `NGROK_AUTHTOKEN` | — | ngrok token for the Node SDK / Termux |
| `CAMXPOSED_STATIC_DOMAIN` | `crepe-cassette-engaged.ngrok-free.dev` | Reserved ngrok domain |
| `CAMXPOSED_PORT` | `8000` | Local MCP server port |
| `CAMXPOSED_ONVIF_USER` | `admin` | Camera username |
| `CAMXPOSED_ONVIF_PASS` | empty | Camera password |
| `CAMXPOSED_LOG_DIR` | system temporary directory | Runtime logs |

The launcher prompts for a token if none is saved. An explicitly supplied `NGROK_AUTHTOKEN` environment variable takes precedence and updates the saved value. You can change the token-file location with `CAMXPOSED_NGROK_TOKEN_FILE`. Do not commit tokens or camera passwords.

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
