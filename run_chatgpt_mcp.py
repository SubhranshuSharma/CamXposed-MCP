#!/usr/bin/env python3
"""Run CamXposed MCP Server with a public HTTPS tunnel for ChatGPT.

This exposes the local CamXposed MCP server (which accesses the camera over LAN)
to ChatGPT via a secure Cloudflare Tunnel using the standard Streamable HTTP MCP transport.
"""

import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import shutil

BASE_DIR = Path(__file__).resolve().parent
NGROK = shutil.which("ngrok") or os.path.expanduser("~/.local/bin/ngrok")
PORT = int(os.getenv("CAMXPOSED_PORT", "8000"))
STATIC_DOMAIN = os.getenv("CAMXPOSED_STATIC_DOMAIN", "").strip()
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

SERVER_LOG = LOG_DIR / "server.log"
TUNNEL_LOG = LOG_DIR / "tunnel.log"

def check_ngrok():
    if not (os.path.exists(NGROK) or shutil.which(NGROK)):
        raise FileNotFoundError(f"ngrok binary not found. Please install ngrok or ensure it is in your PATH.")
    print(f"✓ Using ngrok: {NGROK}")

def start_mcp_server(public_url=""):
    print(f"Starting CamXposed MCP server on 127.0.0.1:{PORT} (transport=streamable-http)...")
    cmd = [
        sys.executable, str(BASE_DIR / "server.py"),
        "--transport", "streamable-http",
        "--host", "127.0.0.1",
        "--port", str(PORT)
    ]
    env = dict(os.environ)
    if public_url:
        env["CAMXPOSED_PUBLIC_URL"] = public_url
    log_f = open(SERVER_LOG, "a")
    proc = subprocess.Popen(cmd, env=env, stdout=log_f, stderr=subprocess.STDOUT)
    return proc, log_f

def wait_for_mcp_server(timeout=10):
    start = time.time()
    url = f"http://127.0.0.1:{PORT}/mcp"
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream, application/json"})
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status in (200, 406):
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (200, 400, 405, 406):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False

def start_tunnel():
    domain = STATIC_DOMAIN or "crepe-cassette-engaged.ngrok-free.dev"
    print(f"Establishing ngrok tunnel to http://127.0.0.1:{PORT} on {domain}...")
    log_f = open(TUNNEL_LOG, "w")
    cmd = [NGROK, "http", str(PORT), f"--url=https://{domain}", "--log=stdout"]
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
    
    tunnel_url = f"https://{domain}"
    time.sleep(2)
    if proc.poll() is not None:
        raise RuntimeError(f"ngrok exited with code {proc.poll()}")

    return proc, log_f, tunnel_url

def verify_public_endpoint(mcp_endpoint, max_retries=15):
    """Wait for Cloudflare edge routing to finish propagating."""
    sys.stdout.write("Waiting for Cloudflare edge routing to propagate...")
    sys.stdout.flush()
    init_payload = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"healthcheck","version":"1.0"}}}'
    req = urllib.request.Request(
        mcp_endpoint,
        data=init_payload,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    )
    for _ in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    sys.stdout.write(" Ready!\n")
                    sys.stdout.flush()
                    return True
        except urllib.error.HTTPError as e:
            if e.code == 200:
                sys.stdout.write(" Ready!\n")
                sys.stdout.flush()
                return True
        except Exception:
            pass
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(1.5)
    sys.stdout.write(" Timeout waiting for edge check, but tunnel is active.\n")
    return False

def main():
    check_ngrok()
    
    tunnel_proc, tunnel_log_f, tunnel_url = start_tunnel()
    mcp_endpoint = f"{tunnel_url}/mcp"

    server_proc, server_log_f = start_mcp_server(public_url=tunnel_url)
    if not wait_for_mcp_server():
        tunnel_proc.terminate()
        server_proc.terminate()
        server_log_f.close()
        tunnel_log_f.close()
        print("Error: MCP server failed to start within timeout", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Local MCP server is running on port {PORT}")

    verify_public_endpoint(mcp_endpoint)

    print("\n" + "="*70)
    print("🚀 CAMXPOSED MCP SERVER READY FOR CHATGPT")
    print("="*70)
    print(f"\nPublic MCP Endpoint URL:")
    print(f"  👉  {mcp_endpoint}")
    print("\nHow to configure in ChatGPT (Plugins -> Add custom MCP server):")
    print("  1. Connection:     Select 'Server URL'")
    print(f"  2. URL:            {mcp_endpoint}")
    print("  3. Authentication: Select 'No authentication' (or 'None')")
    print("  4. Check:          'I understand and want to continue'")
    print("  5. Click:          'Create as a plugin'")
    print("\nLogs:")
    print(f"  Server log: {SERVER_LOG}")
    print(f"  Tunnel log: {TUNNEL_LOG}")
    print("\nKeep this process running while using the plugin in ChatGPT.")
    print("="*70 + "\n")

    def shutdown(signum, frame):
        print("\nStopping services...")
        tunnel_proc.terminate()
        server_proc.terminate()
        tunnel_proc.wait()
        server_proc.wait()
        tunnel_log_f.close()
        server_log_f.close()
        print("Shutdown complete.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while True:
        if server_proc.poll() is not None:
            print("MCP server terminated unexpectedly.")
            tunnel_proc.terminate()
            break
        if tunnel_proc.poll() is not None:
            print("Tunnel terminated unexpectedly.")
            server_proc.terminate()
            break
        time.sleep(1)

if __name__ == "__main__":
    main()
