#!/usr/bin/env python3
"""One-command CamXposed MCP + ngrok launcher for Linux and Android Termux."""
from __future__ import annotations

import getpass
import importlib.util
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("CAMXPOSED_PORT", "8000"))
HOST = "127.0.0.1"
DOMAIN = os.getenv("CAMXPOSED_STATIC_DOMAIN", "crepe-cassette-engaged.ngrok-free.dev").strip()
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


def python_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def node_env() -> dict[str, str]:
    env = os.environ.copy()
    if not is_termux():
        return env

    # Termux's Node native addons may need libpython preloaded. Resolve the
    # actual Termux prefix so both Play Store and F-Droid installs work.
    prefix = Path(env.get("PREFIX", "/data/data/com.termux/files/usr"))
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        prefix / "lib" / f"libpython{pyver}.so",
        prefix / "lib" / f"libpython{sys.version_info.major}.so",
    ]
    library = next((p for p in candidates if p.exists()), None)
    if library:
        current = env.get("LD_PRELOAD", "")
        paths = [p for p in current.split(":") if p]
        if str(library) not in paths:
            env["LD_PRELOAD"] = ":".join([str(library), *paths])
    return env


def get_ngrok_token() -> str:
    token = (os.getenv("NGROK_AUTHTOKEN") or os.getenv("NGROK_AUTH_TOKEN") or "").strip()
    if token:
        return token
    if sys.stdin.isatty():
        token = getpass.getpass("ngrok authtoken (input hidden): ").strip()
        if token:
            os.environ["NGROK_AUTHTOKEN"] = token
            return token
    raise RuntimeError(
        "ngrok authentication is missing. Set NGROK_AUTHTOKEN, or run this in an "
        "interactive terminal and enter your token when prompted."
    )


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


def start_server():
    env = python_env()
    log = open(SERVER_LOG, "a", encoding="utf-8")
    command = [
        sys.executable, str(ROOT / "server.py"),
        "--transport", "streamable-http", "--host", HOST, "--port", str(PORT),
    ]
    proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def wait_for_server(proc, timeout: float = 25.0) -> bool:
    url = f"http://{HOST}:{PORT}/mcp"
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


def start_ngrok():
    token = get_ngrok_token()
    ngrok = shutil.which("ngrok")

    # Native ngrok CLI is the leanest option on desktop Linux.
    if ngrok and not is_termux():
        command = [ngrok, "http", str(PORT)]
        if DOMAIN:
            command.append(f"--url=https://{DOMAIN}")
        log = open(TUNNEL_LOG, "a", encoding="utf-8")
        env = os.environ.copy()
        env["NGROK_AUTHTOKEN"] = token
        proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
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
        '(async()=>{const opts={addr:"127.0.0.1:' + str(PORT) + '",authtoken_from_env:true'
        + (',domain:' + repr(DOMAIN) if DOMAIN else '')
        + '};const listener=await ngrok.forward(opts);'
        'console.log("CAMXPOSED_TUNNEL_URL="+listener.url());'
        'setInterval(()=>{},60000);})().catch(e=>{console.error(e);process.exit(1)});'
        'process.on("SIGINT",async()=>{await ngrok.disconnect();process.exit(0)});'
    )
    log = open(TUNNEL_LOG, "a", encoding="utf-8")
    proc = subprocess.Popen([node, "-e", js], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1)
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


def stop_process(proc) -> None:
    if not proc or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def main() -> int:
    if sys.version_info < (3, 10):
        raise RuntimeError("CamXposed MCP requires Python 3.10 or newer.")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is missing. Install it first (Linux: apt install ffmpeg; Termux: pkg install ffmpeg).")

    platform_name = "Termux" if is_termux() else "Linux"
    say(f"CamXposed MCP launcher — {platform_name}")
    ensure_python_dependencies()

    server_proc = tunnel_proc = None
    server_log = tunnel_log = None
    try:
        say(f"Starting local MCP server at http://{HOST}:{PORT}/mcp ...")
        server_proc, server_log = start_server()
        if not wait_for_server(server_proc):
            raise RuntimeError(f"MCP server did not become ready. Check {SERVER_LOG}")
        say("✓ Local MCP server is healthy")

        say("Connecting ngrok remote tunnel...")
        tunnel_proc, tunnel_log, public_url = start_ngrok()
        endpoint = public_url + "/mcp"
        say("")
        say("CAMXPOSED MCP IS READY")
        say(f"Remote MCP endpoint: {endpoint}")
        say(f"Local endpoint:      http://{HOST}:{PORT}/mcp")
        say(f"Server log:          {SERVER_LOG}")
        say(f"Tunnel log:          {TUNNEL_LOG}")
        say("Keep this process running. Press Ctrl+C to stop both services.")

        while True:
            if server_proc.poll() is not None:
                raise RuntimeError(f"MCP server exited; inspect {SERVER_LOG}")
            if tunnel_proc.poll() is not None:
                raise RuntimeError(f"ngrok tunnel exited; inspect {TUNNEL_LOG}")
            time.sleep(1)
    except KeyboardInterrupt:
        say("\nStopping MCP server and tunnel...")
        return 0
    finally:
        stop_process(tunnel_proc)
        stop_process(server_proc)
        if tunnel_log:
            tunnel_log.close()
        if server_log:
            server_log.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"\nCamXposed MCP launcher error: {error}", file=sys.stderr)
        raise SystemExit(1)
