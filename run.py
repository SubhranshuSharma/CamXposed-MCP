#!/usr/bin/env python3
"""One-command CamXposed MCP + ngrok launcher for Linux and Android Termux."""
from __future__ import annotations

import getpass
import importlib.util
import json
import os
import platform
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREFERRED_PORT = int(os.getenv("CAMXPOSED_PORT", "8000"))
PORT_SCAN_COUNT = max(1, int(os.getenv("CAMXPOSED_PORT_SCAN_COUNT", "11")))
HOST = "127.0.0.1"
DOMAIN = os.getenv("CAMXPOSED_STATIC_DOMAIN", "your-subdomain.ngrok-free.app").strip()
LOG_DIR = Path(os.getenv("CAMXPOSED_LOG_DIR", str(Path(tempfile.gettempdir()) / "camxposed-mcp")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
SERVER_LOG = LOG_DIR / "server.log"
TUNNEL_LOG = LOG_DIR / "tunnel.log"
REQUIRED = {
    "mcp": "mcp",
    "edge_tts": "edge-tts",
    "pydantic": "pydantic",
    "wsdiscovery": "wsdiscovery",
    "zeroconf": "zeroconf",
}


def is_termux() -> bool:
    prefix = os.environ.get("PREFIX", "")
    return "com.termux" in prefix or bool(os.environ.get("TERMUX_VERSION"))


def say(message: str) -> None:
    print(message, flush=True)


def run_checked(cmd: list[str], *, env=None) -> None:
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def ensure_python_dependencies() -> None:
    missing = [package for module, package in REQUIRED.items()
               if importlib.util.find_spec(module) is None]
    if not missing:
        say("✓ Python dependencies are ready")
        return

    say("Installing Python dependencies: " + ", ".join(missing))
    uv = shutil.which("uv")
    if uv:
        command = [uv, "pip", "install", "--python", sys.executable]
        if not is_termux():
            command.append("--system")
        command += ["-r", str(ROOT / "requirements.txt")]
        try:
            run_checked(command)
            return
        except subprocess.CalledProcessError:
            say("uv install failed; trying the active Python's pip")

    command = [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
    if not is_termux() and sys.version_info >= (3, 11):
        command.append("--break-system-packages")
    run_checked(command)


def termux_native_env() -> dict[str, str]:
    """Expose Termux's active libpython to native extensions loaded via dlopen.

    Termux Python may not export interpreter symbols globally from the
    executable. Rust/PyO3 wheels such as cryptography then fail to resolve
    PyType_Type unless the matching libpython is preloaded.
    """
    env = os.environ.copy()
    if not is_termux():
        return env

    prefix = Path(env.get("PREFIX", "/data/data/com.termux/files/usr"))
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        prefix / "lib" / f"libpython{pyver}.so",
        prefix / "lib" / f"libpython{sys.version_info.major}.so",
    ]
    library = next((p for p in candidates if p.is_file()), None)
    if library:
        current = env.get("LD_PRELOAD", "")
        paths = [p for p in current.split(":") if p]
        if str(library) not in paths:
            env["LD_PRELOAD"] = ":".join([str(library), *paths])
    return env


def python_env() -> dict[str, str]:
    env = termux_native_env()
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def node_env() -> dict[str, str]:
    # Keep the same native runtime environment for both Python and Node
    # children; this supports Termux installations from either distribution.
    return termux_native_env()


TOKEN_FILE = Path(os.getenv(
    "CAMXPOSED_NGROK_TOKEN_FILE",
    str(Path.home() / ".config" / "camxposed-mcp" / "ngrok_token"),
))


def get_ngrok_token(*, change: bool = False) -> str:
    # Explicit environment value wins and is saved for subsequent launches.
    token = (os.getenv("NGROK_AUTHTOKEN") or os.getenv("NGROK_AUTH_TOKEN") or "").strip()
    if not token and not change:
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise RuntimeError(f"Cannot read saved ngrok token at {TOKEN_FILE}: {error}") from error

    if not token or change:
        if not sys.stdin.isatty():
            raise RuntimeError(
                "ngrok authentication is missing. Run interactively once to save it, "
                "or set NGROK_AUTHTOKEN."
            )
        prompt = "Enter new ngrok authtoken (input hidden): " if change else "ngrok authtoken (input hidden): "
        token = getpass.getpass(prompt).strip()
        if not token:
            raise RuntimeError("No ngrok authtoken entered.")

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)
        TOKEN_FILE.parent.chmod(0o700)
    except OSError:
        # Android/Windows filesystems may not implement POSIX permission bits.
        pass
    os.environ["NGROK_AUTHTOKEN"] = token
    say(f"✓ ngrok token saved securely at {TOKEN_FILE}")
    return token


def ensure_termux_ngrok_sdk() -> tuple[str, dict[str, str]]:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        raise RuntimeError("Termux needs Node.js for its native ngrok tunnel. Run: pkg install nodejs")

    arch = platform.machine().lower()
    if arch not in ("aarch64", "arm64"):
        raise RuntimeError(f"Termux ngrok native SDK currently expects ARM64; detected {arch}")

    modules = Path.home() / ".cache" / "camxposed-mcp-node"
    sdk_entry = modules / "node_modules" / "@ngrok" / "ngrok"
    android_binary = modules / "node_modules" / "@ngrok" / "ngrok-android-arm64"
    if not sdk_entry.exists() or not android_binary.exists():
        say("Preparing the Android ngrok runtime (one-time install)...")
        run_checked([
            npm, "install", "--prefix", str(modules), "--no-audit", "--no-fund",
            "@ngrok/ngrok", "@ngrok/ngrok-android-arm64",
        ])

    env = node_env()
    env["NODE_PATH"] = str(modules / "node_modules")
    return node, env


def child_parent_death_signal():
    """Ask Linux/Android to terminate this child if the launcher dies abruptly."""
    parent_pid = os.getpid()

    def configure():
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            # PR_SET_PDEATHSIG: deliver SIGTERM when the creating launcher exits.
            if libc.prctl(1, int(signal.SIGTERM), 0, 0, 0) != 0:
                return
            # Close the fork-to-prctl race: parent may have exited before setup.
            if os.getppid() != parent_pid:
                os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            # Normal finally/process-group cleanup remains the fallback.
            pass

    return configure


def start_server(port: int, hide_voice_tools: bool = False):
    env = python_env()
    if hide_voice_tools:
        env["CAMXPOSED_HIDE_VOICE_TOOLS"] = "1"
    log = open(SERVER_LOG, "a", encoding="utf-8")
    command = [
        sys.executable, str(ROOT / "server.py"),
        "--transport", "streamable-http", "--host", HOST, "--port", str(port),
    ]
    proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True, preexec_fn=child_parent_death_signal())
    return proc, log


def wait_for_server(proc, port: int, timeout: float = 25.0) -> bool:
    url = f"http://{HOST}:{port}/mcp"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json, text/event-stream"})
            with urllib.request.urlopen(request, timeout=1):
                return True
        except urllib.error.HTTPError as error:
            if error.code in (400, 405, 406):
                return True
        except Exception:
            pass
        time.sleep(0.35)
    return False


def is_gateway_running(gateway_url: str = "http://127.0.0.1:8090") -> bool:
    """Checks if the local ChatGPT Web Voice Go gateway is alive and responding."""
    try:
        req = urllib.request.Request(f"{gateway_url.rstrip('/')}/api/auth/login", method="POST")
        with urllib.request.urlopen(req, timeout=1.5):
            return True
    except urllib.error.HTTPError as err:
        # 400 Bad Request or 401 Unauthorized confirms the Go gateway is alive!
        if err.code in (400, 401, 405, 422):
            return True
    except Exception:
        pass
    return False


def start_voice_gateway():
    """Locates and starts the Go voice gateway binary if not already running."""
    if is_gateway_running():
        say("✓ Local voice gateway is already running on http://127.0.0.1:8090")
        return None, None

    # Search candidates for the chatgpt-web-voice repository
    gw_env_dir = os.getenv("VOICE_GATEWAY_DIR", "").strip()
    candidates = []
    if gw_env_dir:
        candidates.append(Path(gw_env_dir))
    candidates.extend([
        ROOT.parent / "chatgpt-web-voice",
        Path.home() / "chatgpt-web-voice",
        Path.home() / "Documents/chatgpt-web-voice",
        Path.home() / "Documents/antigravity/lively-bardeen/chatgpt-web-voice",
        Path.home() / "Downloads/chatgpt-web-voice",
    ])
    gw_dir = next((d for d in candidates if (d / "bin/server").is_file()), None)

    if not gw_dir:
        say("! Voice gateway binary not found in standard paths; skipping gateway auto-start")
        return None, None

    say(f"--- Starting Local Voice Gateway ({gw_dir.name}) ---")
    log_path = LOG_DIR / "gateway.log"
    log = open(log_path, "a", encoding="utf-8")

    # Load environment from .env if present
    gw_env = os.environ.copy()
    env_file = gw_dir / ".env"
    if env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    gw_env[k.strip()] = v.strip().strip("'\"")
        except Exception as e:
            say(f"! Warning reading {env_file}: {e}")

    cmd = [str(gw_dir / "bin/server")]
    proc = subprocess.Popen(
        cmd,
        cwd=str(gw_dir),
        env=gw_env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        preexec_fn=child_parent_death_signal()
    )

    # Wait up to 10s for the gateway to become responsive
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            say(f"! Voice gateway failed to start; inspect {log_path}")
            return None, log
        if is_gateway_running():
            say("✓ Voice gateway is healthy on http://127.0.0.1:8090")
            return proc, log
        time.sleep(0.3)

    say(f"! Voice gateway startup timed out; inspect {log_path}")
    return proc, log


def start_ngrok(port: int, *, change_token: bool = False):
    token = get_ngrok_token(change=change_token)
    ngrok = shutil.which("ngrok")

    # Terminate any lingering or orphaned ngrok tunnel processes to avoid ERR_NGROK_334
    try:
        subprocess.run(["pkill", "-f", "ngrok http"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.6)
    except Exception:
        pass

    # Native ngrok CLI can reuse an existing `ngrok config add-authtoken`
    # configuration, so do not force a second token prompt on desktop Linux.
    if ngrok and not is_termux():
        command = [ngrok, "http", str(port)]
        if DOMAIN:
            command.append(f"--url=https://{DOMAIN}")
        log = open(TUNNEL_LOG, "a", encoding="utf-8")
        env = os.environ.copy()
        env["NGROK_AUTHTOKEN"] = token
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        time.sleep(1.5)
        if proc.poll() is not None:
            log.close()
            raise RuntimeError(f"ngrok exited during startup; inspect {TUNNEL_LOG}")
        return proc, log, f"https://{DOMAIN}" if DOMAIN else ""

    node, env = ensure_termux_ngrok_sdk() if is_termux() else (shutil.which("node"), os.environ.copy())
    if not node:
        raise RuntimeError("Install ngrok CLI on Linux, or install Node.js to use the ngrok SDK fallback.")
    if not is_termux():
        modules = Path.home() / ".cache" / "camxposed-mcp-node"
        if not (modules / "node_modules" / "@ngrok" / "ngrok").exists():
            npm = shutil.which("npm")
            if not npm:
                raise RuntimeError("Neither ngrok CLI nor Node.js/npm is available.")
            say("Preparing the ngrok Node SDK...")
            run_checked([npm, "install", "--prefix", str(modules), "--no-audit", "--no-fund", "@ngrok/ngrok"])
        env["NODE_PATH"] = str(modules / "node_modules")

    env["NGROK_AUTHTOKEN"] = token
    js = (
        'const ngrok=require("@ngrok/ngrok");'
        '(async()=>{const opts={addr:"127.0.0.1:' + str(port) + '",authtoken_from_env:true'
        + (',domain:' + repr(DOMAIN) if DOMAIN else '')
        + '};const listener=await ngrok.forward(opts);'
        'console.log("CAMXPOSED_TUNNEL_URL="+listener.url());'
        'setInterval(()=>{},60000);})().catch(e=>{console.error(e);process.exit(1)});'
        'const shutdown=async()=>{try{await ngrok.disconnect()}finally{process.exit(0)}};'
        'process.on("SIGINT",shutdown);process.on("SIGTERM",shutdown);'
    )
    log = open(TUNNEL_LOG, "a", encoding="utf-8")
    proc = subprocess.Popen([node, "-e", js], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1,
                            start_new_session=True)
    # SDK emits the public URL as soon as the tunnel is established.
    import selectors
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 35
    url = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if selector.select(timeout=0.5):
            line = proc.stdout.readline().strip()
            if line.startswith("CAMXPOSED_TUNNEL_URL="):
                url = line.split("=", 1)[1]
                break
            if line:
                say(line)
    selector.close()
    if not url:
        proc.terminate()
        proc.wait(timeout=5)
        log.close()
        raise RuntimeError(f"ngrok SDK did not establish a tunnel; inspect {TUNNEL_LOG}")
    return proc, log, url.rstrip("/")


def verify_remote_endpoint(endpoint: str, attempts: int = 12) -> bool:
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "camxposed-launcher-healthcheck", "version": "1.0"},
        },
    }).encode()
    for _ in range(attempts):
        request = urllib.request.Request(
            endpoint, data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return True
        except urllib.error.HTTPError as error:
            if error.code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def stop_process(proc) -> None:
    """Stop the complete child process group, then reap the direct child."""
    if not proc:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)


def acquire_instance_lock():
    """Prevent two launchers from managing competing server/tunnel pairs."""
    import fcntl
    handle = open(LOG_DIR / "launcher.lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("Another CamXposed MCP launcher is already running.")
    return handle


def select_camera_via_ws_discovery() -> str:
    """Discovers ONVIF cameras via WS-Discovery, filters for actual cameras, and prompts user in CLI."""
    from lan_engine import LanEngine
    say("\n--- Discovering ONVIF Cameras via WS-Discovery ---")
    say("Scanning LAN (UDP 3702) for verified ONVIF cameras...")
    engine = LanEngine()
    res = engine.discover_all(timeout=2.0)
    cams = [c for c in res.get("cameras", []) if isinstance(c, dict) and c.get("ip")]

    if not cams:
        fallback_ip = os.getenv("CAMXPOSED_CAMERA_IP", "10.195.38.213")
        say(f"! No verified ONVIF cameras found via WS-Discovery. Falling back to: {fallback_ip}")
        return fallback_ip

    say(f"Found {len(cams)} verified ONVIF camera(s):")
    for idx, cam in enumerate(cams, 1):
        ptz_info = " [PTZ Supported]" if cam.get("ptzSupported") else ""
        say(f"  [{idx}] {cam.get('name', 'Camera')} - IP: {cam.get('ip')}{ptz_info}")

    default_cam = cams[0]
    default_ip = default_cam["ip"]

    if len(cams) == 1:
        say(f"Default camera: [{1}] {default_cam.get('name')} ({default_ip})")

    if sys.stdin.isatty():
        try:
            prompt = f"\nSelect camera [1-{len(cams)}] or enter custom IP (press Enter or wait 5s for default: 1 [ {default_ip} ]): "
            user_choice = None

            def _get_input():
                nonlocal user_choice
                try:
                    user_choice = input(prompt).strip()
                except (EOFError, KeyboardInterrupt):
                    user_choice = ""

            t = threading.Thread(target=_get_input, daemon=True)
            t.start()
            t.join(timeout=5.0)

            if user_choice is None:
                sys.stdout.write(f"\n[Timeout 5s] Auto-selecting default camera [{default_ip}]\n")
                sys.stdout.flush()
                user_choice = ""

            if not user_choice:
                chosen_ip = default_ip
            elif user_choice.isdigit() and 1 <= int(user_choice) <= len(cams):
                chosen_ip = cams[int(user_choice) - 1]["ip"]
            elif re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", user_choice):
                chosen_ip = user_choice
            else:
                say(f"Invalid input '{user_choice}'; using default camera [{default_ip}]")
                chosen_ip = default_ip
        except (EOFError, KeyboardInterrupt):
            say(f"\nUsing default camera [{default_ip}]")
            chosen_ip = default_ip
    else:
        say(f"Auto-selected default camera: [{default_ip}]")
        chosen_ip = default_ip

    say(f"✓ Target Camera: {chosen_ip}\n")
    return chosen_ip


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="CamXposed MCP Launcher with ChatGPT Live Voice Bridge")
    parser.add_argument("--mode", choices=["mcp", "live", "both"], default=os.getenv("CAMXPOSED_MODE", "both"),
                        help="Operating mode: 'mcp' (standard MCP server only), 'live' (direct ChatGPT Live voice bridge only), or 'both' (default: full live voice experience with camera MCP tools).")
    parser.add_argument("--change-token", action="store_true", help="Prompt to change saved ngrok authtoken")
    parser.add_argument("--camera-ip", default=os.getenv("CAMXPOSED_CAMERA_IP", None),
                        help="Target camera LAN IP address (auto-discovered via WS-Discovery if omitted)")
    parser.add_argument("--token", default=os.getenv("CAMXPOSED_CHATGPT_TOKEN"),
                        help="ChatGPT access token / session token (auto-extracted from Firefox if omitted)")
    parser.add_argument("--account-email", default=os.getenv("CAMXPOSED_ACCOUNT_EMAIL"),
                        help="ChatGPT account email to use (prompts user to select from discovered Firefox accounts if omitted)")
    parser.add_argument("--voice", default=os.getenv("CAMXPOSED_VOICE", "cove"),
                        help="ChatGPT voice name: cove, breeze, ember, juniper, vale (default: cove)")
    parser.add_argument("--motion-cooldown", type=float, default=float(os.getenv("CAMXPOSED_MOTION_COOLDOWN_S", "4.0")),
                        help="Cooldown in seconds after camera movement to suppress false-positive motion alarms (default: 4.0)")
    args, _ = parser.parse_known_args()
    os.environ["CAMXPOSED_MOTION_COOLDOWN_S"] = str(args.motion_cooldown)

    change_token = args.change_token
    mode = args.mode

    if sys.version_info < (3, 10):
        raise RuntimeError("CamXposed MCP requires Python 3.10 or newer.")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is missing. Install it first (Linux: apt install ffmpeg; Termux: pkg install ffmpeg).")

    platform_name = "Termux" if is_termux() else "Linux"
    say(f"CamXposed MCP launcher — {platform_name} [Mode: {mode.upper()}]")
    lock_handle = acquire_instance_lock()

    # Convert SIGTERM into normal Python unwinding so finally always cleans
    # up the server and tunnel process groups. SIGINT already raises this.
    def handle_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_sigterm)
    ensure_python_dependencies()

    target_camera_ip = args.camera_ip
    if not target_camera_ip:
        target_camera_ip = select_camera_via_ws_discovery()
    os.environ["CAMXPOSED_CAMERA_IP"] = target_camera_ip

    target_token = args.token
    target_account_email = args.account_email
    if mode in ("live", "both") and not target_token:
        try:
            from live_voice_bridge import select_chatgpt_account
            chosen = select_chatgpt_account()
            if chosen:
                target_token = chosen.get("access_token")
                target_account_email = chosen.get("email")
        except Exception as e:
            say(f"! Account selection error: {e}; falling back to default discovery")

    hide_voice_tools = (mode == "live")
    gateway_proc = None
    gateway_log = None
    bridge_proc = None
    tunnel_proc = None
    server_proc = None
    bridge_log = None
    tunnel_log = None
    server_log = None

    try:
        # 1. Start Go Voice Gateway if mode is 'live' or 'both'
        if mode in ("live", "both"):
            gateway_proc, gateway_log = start_voice_gateway()

        # 2. Start Live Voice Bridge if mode is 'live' or 'both'
        if mode in ("live", "both"):
            say("\n--- Starting ChatGPT Live Voice Bridge ---")
            say(f"Target Camera IP: {target_camera_ip}")
            say(f"ChatGPT Voice:    {args.voice}")
            if target_account_email:
                say(f"ChatGPT Account:  {target_account_email}")
            say("Direct connection: Camera RTSP Mic -> ChatGPT WebRTC -> Camera TCP 28772 Speaker")
            
            bridge_log_path = LOG_DIR / "bridge.log"
            bridge_log = open(bridge_log_path, "a", encoding="utf-8")
            bridge_cmd = [
                sys.executable, str(ROOT / "live_voice_bridge.py"),
                "--camera-ip", target_camera_ip,
                "--voice", args.voice,
            ]
            if target_token:
                bridge_cmd.extend(["--token", target_token])
            if target_account_email:
                bridge_cmd.extend(["--account-email", target_account_email])
            if args.motion_cooldown is not None:
                bridge_cmd.extend(["--motion-cooldown", str(args.motion_cooldown)])

            env = python_env()
            bridge_proc = subprocess.Popen(bridge_cmd, cwd=ROOT, env=env, stdout=bridge_log, stderr=subprocess.STDOUT,
                                           start_new_session=True, preexec_fn=child_parent_death_signal())
            say(f"✓ Live Voice Bridge process running (logs: {bridge_log_path})")

        # 2. Start MCP server and tunnel if mode is 'mcp' or 'both'
        if mode in ("mcp", "both"):
            say(f"\n--- Starting CamXposed MCP Server (Hide voice tools: {hide_voice_tools}) ---")
            port = None
            for candidate in range(PREFERRED_PORT, PREFERRED_PORT + PORT_SCAN_COUNT):
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    probe.bind((HOST, candidate))
                except OSError:
                    continue
                finally:
                    probe.close()
                say(f"Starting local MCP server at http://{HOST}:{candidate}/mcp ...")
                server_proc, server_log = start_server(candidate, hide_voice_tools=hide_voice_tools)
                if wait_for_server(server_proc, candidate):
                    port = candidate
                    break
                stop_process(server_proc)
                server_proc = None
            if port is None:
                raise RuntimeError(
                    f"No available MCP port in {PREFERRED_PORT}-{PREFERRED_PORT + PORT_SCAN_COUNT - 1}. "
                    f"Check {SERVER_LOG}"
                )
            say(f"✓ Local MCP server is healthy on port {port}")

            say("Connecting ngrok remote tunnel...")
            tunnel_proc, tunnel_log, public_url = start_ngrok(port, change_token=change_token)
            endpoint = public_url + "/mcp"
            say("Checking the public MCP endpoint...")
            if verify_remote_endpoint(endpoint):
                say("✓ Remote MCP handshake succeeded")
            else:
                say("! Tunnel is active, but the remote MCP handshake timed out; check the logs")
            say("")
            say("CAMXPOSED MCP IS READY")
            say(f"Remote MCP endpoint: {endpoint}")
            say(f"Local endpoint:      http://{HOST}:{port}/mcp")
            say(f"Server log:          {SERVER_LOG}")
            say(f"Tunnel log:          {TUNNEL_LOG}")

        say("\nAll services started. Keep this process running. Press Ctrl+C to stop.")

        while True:
            if server_proc and server_proc.poll() is not None:
                raise RuntimeError(f"MCP server exited; inspect {SERVER_LOG}")
            if tunnel_proc and tunnel_proc.poll() is not None:
                raise RuntimeError(f"ngrok tunnel exited; inspect {TUNNEL_LOG}")
            if bridge_proc and bridge_proc.poll() is not None:
                raise RuntimeError(f"Live voice bridge exited; inspect {LOG_DIR / 'bridge.log'}")
            if gateway_proc and gateway_proc.poll() is not None:
                raise RuntimeError(f"Voice gateway exited; inspect {LOG_DIR / 'gateway.log'}")
            time.sleep(1)
    except KeyboardInterrupt:
        say("\nStopping all services...")
        return 0
    finally:
        stop_process(bridge_proc)
        stop_process(tunnel_proc)
        stop_process(server_proc)
        stop_process(gateway_proc)
        if bridge_log:
            bridge_log.close()
        if tunnel_log:
            tunnel_log.close()
        if server_log:
            server_log.close()
        if gateway_log:
            gateway_log.close()
        lock_handle.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"\nCamXposed MCP launcher error: {error}", file=sys.stderr)
        raise SystemExit(1)
