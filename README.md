# CamXposed MCP

MCP server for ONVIF cameras: discovery, snapshots, PTZ, audio capture and speaker playback.

## Linux (Ubuntu/Debian)

Requirements: Python 3, Git, FFmpeg, uv, and ngrok. The host must be on the camera's LAN.

```bash
sudo apt update
sudo apt install -y git python3 ffmpeg
git clone https://github.com/SubhranshuSharma/CamXposed-MCP.git
cd CamXposed-MCP

curl -LsSf https://astral.sh/uv/install.sh | sh
uv pip install --system --break-system-packages -r requirements.txt
```

Optional ONVIF credentials (default: `admin` with an empty password):

```bash
export CAMXPOSED_ONVIF_USER="admin"
export CAMXPOSED_ONVIF_PASS=""
```

### Start the server

Terminal 1:

```bash
python3 server.py --transport streamable-http --host 127.0.0.1 --port 8000
```

MCP endpoint: `http://127.0.0.1:8000/mcp`

### Start ngrok

Install ngrok using its [official Linux instructions](https://ngrok.com/download), then authenticate:

```bash
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
```

Terminal 2:

```bash
ngrok http --url=your-subdomain.ngrok-free.app 8000
```

Remote MCP endpoint: `https://your-subdomain.ngrok-free.app/mcp`

Configure your MCP client for **Streamable HTTP**. Keep both processes running.

## Android (Termux)

Run the server and tunnel in separate Termux sessions.

### Install

```bash
pkg update
pkg install -y git python nodejs ffmpeg
git clone https://github.com/SubhranshuSharma/CamXposed-MCP.git ~/camxposed-mcp
cd ~/camxposed-mcp
pip install -r requirements.txt
npm install @ngrok/ngrok @ngrok/ngrok-android-arm64
```

### Start the server

Session 1:

```bash
cd ~/camxposed-mcp
PYVER=$(python -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
LD_PRELOAD="$PREFIX/lib/libpython${PYVER}.so${LD_PRELOAD:+:$LD_PRELOAD}" \
  python server.py --transport streamable-http --host 127.0.0.1 --port 8000
```

### Start ngrok

Session 2:

```bash
cd ~/camxposed-mcp
export NGROK_AUTHTOKEN="YOUR_NGROK_AUTHTOKEN"
node - <<'NODE'
const ngrok = require('@ngrok/ngrok');
(async () => {
  const listener = await ngrok.forward({
    addr: "127.0.0.1:8000",
    authtoken_from_env: true,
    domain: "your-subdomain.ngrok-free.app"
  });
  console.log("MCP endpoint:", listener.url() + "/mcp");
  setInterval(() => {}, 60000);
})().catch(err => {
  console.error(err);
  process.exit(1);
});
process.on("SIGINT", async () => {
  await ngrok.disconnect();
  process.exit(0);
});
NODE
```

MCP endpoint: `https://your-subdomain.ngrok-free.app/mcp`

## Camera behaviour

- Camera tools automatically use the selected camera.
- If none is selected, the server discovers cameras and selects the first usable one.
- Stale camera addresses are refreshed through discovery.
- NVR/DVR devices are excluded from the camera list.
- Default ONVIF login is `admin` with a blank password. Set `CAMXPOSED_ONVIF_USER` and `CAMXPOSED_ONVIF_PASS` to override it.

## Network requirements

- The server must have LAN access to the camera.
- ONVIF discovery uses WS-Discovery multicast: UDP `239.255.255.250:3702`.
- A remote VPS cannot discover cameras on a private LAN without a routed connection.
- Keep the server bound to `127.0.0.1` when using ngrok.

## Security

Do not commit ngrok tokens, camera passwords or API keys. The ngrok URL provides connectivity, not authentication; share it only with trusted MCP clients.
