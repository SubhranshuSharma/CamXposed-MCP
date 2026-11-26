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

1. **Google Gemini Live Voice Bridge & Vision Subagent (`gemini.py`)**:
   - **Gemini Live API (`gemini-3.8-live`)**: Full-duplex bidirectional streaming voice over WebSockets with official function calling.
   - **Pure Chat Vision Subagent (`gemini-3.8-flash`)**: Autonomous visual room sweeping using native multimodal vision on physical camera snapshots.
   - **Official Tool Routing**: `spawn_subagent` is officially declared for the front-line voice agent and never exposed to the subagent, eliminating recursion naturally without canary tokens or entrapment hacks.
   - **Non-Blocking Background Concurrency**: The live conversational session remains fully active while the subagent sweeps the room. The user can chat freely while the camera moves, and the final outcome is announced conversationally upon completion.
   - **Instant Barge-In**: Silero VAD + Gemini server interruption cut off camera speaker output immediately when the user speaks.

2. **ChatGPT Live Voice Bridge & Visual Subagent (`live_voice_bridge.py`, `subagent_engine.py`)**:
   - Bridges the camera's RTSP microphone to ChatGPT Realtime Voice via WebRTC gateway with Firefox token auto-refresh.
   - Autonomous visual subagent sweeps the room via ONVIF PTZ with canary protection and spatial confirmation.

3. **Unified Multi-Mode & Multi-Provider Launcher (`run.py`)**:
   - Providers: `--provider gemini` (Google Gemini Live & Chat) or `--provider chatgpt` (ChatGPT Web Voice). Default: `auto` (uses Gemini if `GEMINI_API_KEY` is available).
   - Modes: `--mode both` (default: MCP server + Live Voice Bridge), `--mode mcp`, `--mode live`.

### Quick Start (Linux & Android Termux)

#### Option 1: One-Command Self-Install & Run (Fresh Machine or Termux)
Run this single command in a terminal (works on Android Termux, Ubuntu, Debian, Fedora, Arch):

```bash
curl -fsSL https://raw.githubusercontent.com/SubhranshuSharma/CamXposed-MCP/feature/live-voice-bridge-and-visual-subagent/start.sh | bash
```

To pass custom flags directly to the one-command runner (e.g. `--mode live` or `--mode mcp`):
```bash
curl -fsSL https://raw.githubusercontent.com/SubhranshuSharma/CamXposed-MCP/feature/live-voice-bridge-and-visual-subagent/start.sh | bash -s -- --mode live
```

#### Option 2: Running Inside the Cloned Repository
If you already cloned this branch, simply run the self-installing launcher:

```bash
./start.sh
```

Or pass any runtime mode:
```bash
./start.sh --mode live
./start.sh --mode mcp
./start.sh --mode both
```

#### Option 3: Manual Execution
Start both the MCP server and Live Voice Bridge:
```bash
python3 run.py --mode both   # On Termux: python run.py --mode both
```

Start only the MCP server (exposed via ngrok for ChatGPT Developer mode / MCP client):
```bash
python3 run.py --mode mcp    # On Termux: python run.py --mode mcp
```

Start only the Live Voice Bridge:
```bash
python3 run.py --mode live   # On Termux: python run.py --mode live
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
