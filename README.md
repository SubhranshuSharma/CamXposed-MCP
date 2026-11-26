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
| `CAMXPOSED_STATIC_DOMAIN` | — | Optional reserved ngrok static domain |
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

## ChatGPT Custom Instructions Setup

For optimal performance with ChatGPT Voice and MCP tools, configure your ChatGPT **Custom Instructions** (or Project/GPT System Prompt) with the following exact block:

```text
You are connected to a physical IP camera via your camera tools/plugins/mcp.

RULES:
1. SEARCH & EXPLORATION (MANDATORY SUBAGENT): Whenever the user asks you to find, search for, look for, or locate ANY object, item, or person in the room (e.g., "find a chair", "find me something", "look for my keys", "search the room"), you MUST IMMEDIATELY call `spawn_subagent`. DO NOT attempt to search manually using `camera_observe` or `camera_move`. Stay quiet while the subagent works, and only announce the final result when it completes.
2. NEVER FAKE ACTIONS: If asked to move the camera, inspect, or speak through it, you MUST ACTUALLY EXECUTE the tool. Never roleplay or claim you moved or saw something without running the tool.
3. MOVEMENT: For explicit single directional moves requested by the user (e.g., "turn left", "look up"), always use maximum speed (8) and at least 700–1000ms. Low speeds are too weak to physically turn the camera motors.
4. VISION: Use `camera_observe` ONLY to check what is directly in front of the lens right now when asked "what do you see?". Never use it to search around the room, and never guess or hallucinate.
5. TALKBACK: Only use the camera's speaker when explicitly told to speak or announce into the room. Keep normal conversation in your own voice chat.
```

## Live Voice Bridge & Visual Subagent (Feature Branch)

This branch adds full-duplex realtime voice communication and autonomous background visual search:

1. **Full-Duplex Live Voice Bridge (`live_voice_bridge.py`)**:
   - Bridges the camera's RTSP microphone directly to ChatGPT Realtime Voice via WebRTC.
   - Streams ChatGPT's synthesized voice responses back to the physical camera speaker (Anyka talkback port 8000 / raw PCM/A-law).
   - Features local client-side Silero VAD (Voice Activity Detection) with instant audio cutoff upon user barge-in.
   - Supports auto-discovery of ChatGPT session tokens from local browser profiles (Firefox containers) or direct token/cookie injection.

2. **Autonomous Visual Subagent (`subagent_engine.py`)**:
   - Dispatches a background visual search subagent via `spawn_subagent(search_entity_description="...")`.
   - Autonomously sweeps the room using ONVIF PTZ, capturing camera snapshots at each waypoint.
   - Evaluates frames via ChatGPT SSE HTTP stream, detecting entities with canary protection and spatial confirmation.
   - Automatically stops the camera upon target acquisition and injects a synthesized completion message directly into the live voice session.

3. **Unified Multi-Mode Launcher (`run.py`)**:
   - Supports `--mode both` (default: MCP server + Live Voice Bridge + Go gateway).
   - Supports `--mode mcp` (MCP server only).
   - Supports `--mode live` (Live Voice Bridge only).

### Running the System

Start both the MCP server and Live Voice Bridge:
```bash
python3 run.py --mode both
```

Start only the MCP server (exposed via ngrok for ChatGPT Developer mode / MCP client):
```bash
python3 run.py --mode mcp
```

Start only the Live Voice Bridge:
```bash
python3 run.py --mode live
```

Or run the lightweight standalone MCP launcher:
```bash
python3 run_chatgpt_mcp.py
```

### Environment Variables for Voice Bridge & Subagent

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY_PASSWORD` | `AdminPassword2026!` | Password for local WebRTC voice gateway |
| `VOICE_GATEWAY_DIR` | Auto-detected | Path to local `chatgpt-web-voice` gateway directory |
| `VOICE_DB_PATH` | Auto-detected | Path to SQLite `voice.db` storing active call sessions |
| `CAMXPOSED_HIDE_VOICE_TOOLS` | `0` | Set to `1` to hide `camera_speak` from MCP tools when Live Voice Bridge handles audio |
| `CAMXPOSED_PTZ_DEBOUNCE_SEC` | `25` | Seconds of PTZ movement cooldown after subagent activity |

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

The ngrok URL provides connectivity, not authentication. Share it only with trusted MCP clients. Never commit private tokens, passwords, or session databases.
