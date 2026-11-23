# CamXposed MCP — Native LAN Camera Control, Vision & PTZ

CamXposed MCP exposes local ONVIF/RTSP camera discovery, snapshots, PTZ, audio capture, and speaker playback as MCP tools. It can run on Linux or Android through Termux and reach a remote MCP client through ngrok.

> **Important:** The MCP server and ngrok tunnel are two separate long-running processes. Start the Python MCP server first, then start the ngrok tunnel in a second terminal/session. Keep both running.

## Linux deployment (Ubuntu / Debian)

Run the server on a Linux machine connected to the same reachable LAN as the ONVIF camera. The MCP server and ngrok tunnel run in separate terminals.

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip ffmpeg
```

Install the ngrok agent using the official instructions for your Linux distribution: https://ngrok.com/download. Then authenticate it with your own token:

```bash
ngrok config add-authtoken "YOUR_NGROK_AUTHTOKEN"
```

Do not commit the token or put it in the repository.

### 2. Clone the repository

```bash
git clone https://github.com/SubhranshuSharma/CamXposed-MCP.git
cd CamXposed-MCP
```

For an existing checkout, update the default branch:

```bash
cd ~/CamXposed-MCP
git switch main
git pull origin main
```

### 3. Create the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure ONVIF credentials (optional)

The default is username `admin` with an empty password. Override these environment variables if your camera uses different credentials:

```bash
export CAMXPOSED_ONVIF_USER="admin"
export CAMXPOSED_ONVIF_PASS=""
```

Set these in the same terminal before starting the server.

### 5. Start the MCP server — terminal 1

```bash
source .venv/bin/activate
python server.py --transport streamable-http --host 127.0.0.1 --port 8000
```

Keep this process running. The local MCP endpoint is:

```text
http://127.0.0.1:8000/mcp
```

Bind to `127.0.0.1` when using ngrok; do not expose the unauthenticated MCP server directly to the LAN or public internet.

### 6. Start ngrok — terminal 2

For the reserved domain configured for this project:

```bash
ngrok http --url=your-subdomain.ngrok-free.app 8000
```

The remote MCP endpoint is:

```text
https://your-subdomain.ngrok-free.app/mcp
```

Configure the MCP client for **Streamable HTTP** using that URL. Keep both terminal processes running. The ngrok URL provides reachability, not application-level authorization; only share it with trusted clients.

### 7. Linux troubleshooting

- If the server cannot find FFmpeg, verify `ffmpeg -version` and that `ffmpeg` is on `PATH`.
- If Python package installation fails, confirm the virtual environment is active and retry `pip install -r requirements.txt`.
- If discovery returns no cameras, confirm the Linux host and camera are on the same reachable LAN/VLAN and that UDP multicast WS-Discovery traffic on port 3702 is not blocked.
- Camera discovery uses LAN multicast; running the server on a remote VPS will not discover a camera behind your home router unless you provide a routed network path.

## Termux deployment (Android / ARM64)

These are the deployment commands used by the project maintainer. Run them in **two separate Termux sessions**.

### 1. Update the checkout

```bash
cd ~/camxposed-mcp
git switch main
git pull origin main
```

If this is a fresh installation, clone the repository first:

```bash
git clone https://github.com/SubhranshuSharma/CamXposed-MCP.git ~/camxposed-mcp
cd ~/camxposed-mcp
```

### 2. Start the MCP server — Termux session 1

The Termux Python build uses Android's Bionic libc. On this setup, preloading the matching libpython shared library is required for native Python extensions such as `cryptography` to load reliably.

```bash
cd ~/camxposed-mcp

PYVER=$(python -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
LD_PRELOAD="$PREFIX/lib/libpython${PYVER}.so${LD_PRELOAD:+:$LD_PRELOAD}" \
  python server.py --transport streamable-http --host 127.0.0.1 --port 8000
```

Keep this process running. The MCP endpoint is local at:

```text
http://127.0.0.1:8000/mcp
```

Bind the server to **127.0.0.1**, not `0.0.0.0`: ngrok is the intended public ingress, and the local MCP server should not be exposed directly to the Wi-Fi network.

### 3. Start ngrok — Termux session 2

The Android ARM64 deployment uses the Node ngrok SDK and its Android ARM64 native package. The native package is important on Termux because the standard Linux ARM64 ngrok executable targets glibc, while Android uses Bionic.

The packages should already be installed in the project environment:

```bash
cd ~/camxposed-mcp
npm install @ngrok/ngrok @ngrok/ngrok-android-arm64
```

Set your own ngrok token in the current Termux session. Do not commit it, paste it into source files, or put it in README files:

```bash
export NGROK_AUTHTOKEN="YOUR_NGROK_AUTHTOKEN"
```

Start the reserved-domain tunnel:

```bash
cd ~/camxposed-mcp

node - <<'NODE'
const ngrok = require('@ngrok/ngrok');

(async () => {
    const listener = await ngrok.forward({
        addr: "127.0.0.1:8000",
        authtoken_from_env: true,
        domain: "your-subdomain.ngrok-free.app"
    });

    console.log("NGROK CONNECTED:", listener.url());
    console.log("MCP ENDPOINT:", listener.url() + "/mcp");
    console.log("Tunnel running. Press Ctrl+C to stop.");

    setInterval(() => {}, 60000);
})().catch(err => {
    console.error("NGROK FAILED:", err);
    process.exit(1);
});

process.on('SIGINT', async () => {
    await ngrok.disconnect();
    process.exit(0);
});
NODE
```

The remote MCP URL is:

```text
https://your-subdomain.ngrok-free.app/mcp
```

Configure the MCP client to use **Streamable HTTP** and this URL. The server does not add a separate MCP authentication layer in this deployment; the ngrok tunnel is the public ingress. Treat the endpoint as sensitive and only share it with trusted clients.

### 4. Restarting

- If Python exits, restart the Python command in session 1.
- If ngrok exits, restart the Node command in session 2.
- If the phone sleeps or Android kills Termux, either process may stop. Keep Termux allowed to run in the background and disable battery optimization for it if reliable uptime is required.
- Do not start a second server on port 8000 while the first one is still running.

## Camera discovery, selection, and NVR filtering

### ONVIF login defaults

Discovery uses these credentials for ONVIF SOAP requests:

- If no credentials are configured: username `admin`, password empty (`""`).
- If only a username is configured: that username with an empty password.
- If both are configured: use the supplied username and password.

Configure credentials in the Linux or Termux shell **before starting the Python server**:

```bash
export CAMXPOSED_ONVIF_USER="admin"
export CAMXPOSED_ONVIF_PASS=""
```

For the common default `admin` / blank-password camera, these exports are optional. The server does not brute-force passwords or cycle through guessed credentials. If authentication fails, provide the camera's actual credentials. Do not put passwords in the README, shell history, source code, or Git commits.

### Discovery is not the same as camera selection

ONVIF WS-Discovery can return multiple devices on the LAN: cameras, NVRs/DVRs, and other ONVIF endpoints. A device answering WS-Discovery is not automatically a usable camera.

The discovery layer should register only actual camera endpoints that expose usable ONVIF media capability/profile information. Recorder/NVR/DVR devices must be excluded from the camera inventory. Do not rely only on a hardcoded IP address to exclude a recorder: DHCP can change addresses. Use the device identity and advertised capabilities, and validate that a usable media profile exists.

The known LAN layout used during development included:
- `192.168.1.12` — CP Plus EZ-P31 camera.
- `192.168.1.2` — NVR/recorder; this must not be presented as a camera.

These addresses are examples from the development LAN, not static configuration. Never hardcode them as the default camera or gateway.

### Default camera contract

Camera-targeted tools should be usable without making the model perform setup choreography:

1. If the caller supplies an explicit `camera_id`, use that camera.
2. Otherwise, use the currently selected camera if it is still present and usable.
3. If there is no valid selection, discover cameras and automatically select the first usable camera.
4. If a remembered camera ID or IP is stale, refresh discovery and resolve against the current inventory rather than sending a request to an old address.
5. If discovery finds no usable camera, return a structured, actionable error. Do not silently fall back to an old IP, gateway address, or NVR.

The first discovered camera is a convenience default, not a claim that it is always the user's preferred camera. Keep explicit camera selection available for multi-camera installations.

### Self-describing MCP tools

Tool descriptions are part of the agent-facing interface. Every camera-targeted tool should explain, in plain language:

- what the tool does and what hardware it targets;
- whether it needs a camera ID (normally it should not);
- that it resolves the selected/default camera automatically;
- that it discovers and selects the first usable camera when no selection exists;
- whether the operation is read-only or causes a physical action;
- its important arguments, limits, and expected result;
- what to do when a prerequisite is missing.

Avoid instructions that force the agent to call `camera_select` or `onvif_discover` before every operation. Those tools remain available when the user explicitly wants to inspect or choose cameras; normal operations should resolve their own prerequisites.

### Self-correcting tool behaviour

Descriptions guide the model, but correctness must live in server code. Centralize camera resolution and reuse it across every camera-targeted operation, including status, observe, listen, speak, stop, PTZ, presets, and capabilities.

For recoverable failures, the server should:
- refresh discovery when the selected device has disappeared or its address changed;
- re-resolve the camera and retry only when the operation is safe to retry;
- avoid duplicate physical actions (especially speaker playback and PTZ movement) when the first request may already have succeeded;
- return a clear error code, a concise explanation, a recovery hint, and a `retryable` indication where appropriate.

Example error shape:

```json
{
  "ok": false,
  "error": "NO_CAMERA_AVAILABLE",
  "message": "No usable ONVIF camera was discovered.",
  "recovery": "Check that the camera is powered on and connected to the same LAN, then retry discovery.",
  "retryable": true
}
```

Do not return a generic `Camera not found` if the server can identify the missing prerequisite or the recovery action.

### Agent operating guidance

The intended agent behaviour is:

- For a request such as “take a picture”, call `camera_observe` directly. Do not ask the user to select a camera first.
- For “make the camera say hello”, call `camera_speak` directly. Resolve the default camera inside the tool.
- For “move right”, call `camera_move` directly, respecting its duration/speed limits.
- Use `camera_list` or `onvif_discover` when the user asks to inspect available devices, troubleshoot discovery, or choose a specific camera—not as mandatory preflight for every operation.
- If a tool returns a structured recovery instruction, follow it when safe. Do not repeatedly call a failing tool with identical arguments without acting on its error.
- Never invent a camera ID, IP address, profile token, or gateway address. Obtain these from the current discovery/inventory result.
- Do not claim a camera action succeeded unless the tool confirms it.

## Architecture and protocol notes

- **Discovery:** WS-Discovery uses UDP multicast `239.255.255.250:3702`. Devices may respond from a different source port; parse the returned ONVIF XAddr rather than assuming port 3702 is the service port.
- **ONVIF:** Use the device's discovered service endpoint, media profiles, and snapshot/stream URIs. Do not substitute stale hardcoded addresses.
- **Snapshots (`camera_observe`):** Prefer the camera's current snapshot URI or ONVIF `GetSnapshotUri`. Respect camera settling time after PTZ movement.
- **PTZ:** Continuous movement must be time-bounded and have a host-enforced auto-stop. Do not leave a continuous move running indefinitely.
- **Speaker (`camera_speak`):** Prefer the native LAN speaker path when supported, with the configured bridge fallback only when appropriate. Avoid retrying playback blindly if delivery status is ambiguous.
- **Audio capture (`camera_listen`):** RTSP audio capture depends on the camera's advertised stream/profile and supported codecs. Return a specific error when audio is unavailable rather than implying that camera discovery itself failed.
- **NVR filtering:** An ONVIF recorder may expose media profiles too. Combine device identity/capability checks with the project's camera eligibility rules; do not treat “has a media profile” as the sole proof that a device is a standalone camera.

## Troubleshooting on Termux

### Python fails to load cryptography or another native extension

Run the server with the matching `libpython` preload shown above. Recompute `PYVER` from the active Python interpreter; do not hardcode a Python minor version after upgrading Python.

Check that the library exists:

```bash
ls -l "$PREFIX/lib/libpython$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")').so"
```

### Server starts but the public MCP endpoint is unavailable

1. Confirm the Python process is still running and listening on port 8000.
2. Confirm ngrok prints `NGROK CONNECTED` and the expected reserved domain.
3. Confirm the client URL ends in `/mcp`.
4. Confirm the server uses Streamable HTTP, not stdio.
5. Check that both Termux sessions are still alive.

### Discovery returns zero cameras

1. Confirm the Android device and camera are on the same reachable LAN/VLAN.
2. Check that Android has local-network/Wi-Fi access and that the router does not isolate wireless clients.
3. Check that UDP multicast discovery is not blocked by the access point.
4. Run discovery again after the camera has finished booting.
5. Inspect the actual discovery responses and XAddr values. Do not replace a failed scan with an old remembered camera IP.
6. Remember that NVRs/recorders are intentionally excluded from the camera inventory.

### Camera appears in discovery but an operation fails

Check that the discovered device has a usable media profile and that the operation's required capability is advertised. PTZ, snapshots, RTSP audio, and speaker playback are distinct capabilities; success in one does not guarantee the others.

## Security

- Never commit ngrok tokens, camera passwords, API keys, or private tunnel credentials.
- Use environment variables or a local untracked configuration file for secrets.
- If a token is accidentally exposed in chat, logs, screenshots, or Git history, revoke/rotate it immediately.
- Keep the MCP server bound to `127.0.0.1` when using ngrok as the ingress.
- Do not log credentials or full authenticated RTSP/HTTP URLs.
- The public ngrok URL provides network reachability, not authorization by itself. Restrict access through the MCP client/account and add an authentication layer before exposing the server to untrusted users.
