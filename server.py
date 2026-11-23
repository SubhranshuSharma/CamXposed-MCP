#!/usr/bin/env python3
from __future__ import annotations
import asyncio, base64, json, logging, os, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request, re, uuid, xml.etree.ElementTree as ET
import edge_tts
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from lan_engine import lan_engine, LanCamera, AnykaLanSpeaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("camxposed")

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.types import AudioContent, TextContent

# MCP tool registry
mcp = MCPServer(
    name="कैमXposed",
    title="कैमXposed",
    description="CamXposed MCP Bridge - Smart IP Camera Control, Vision & PTZ",
    version="1.0.0",
)
# Discovery uses native WS-Discovery over UDP 3702. No Avahi, zeroconf, or UPnP dependency.

SERVICE_TYPE="_camxposed._tcp.local."
EXPECTED_BRAND="कैमXposed"
EXPECTED_UUID="03597b99-b818-4576-b345-8737bb91885f"
WS_DISCOVERY_ADDR=("239.255.255.250", 3702)
WS_DISCOVERY_TIMEOUT=float(os.getenv("CAMXPOSED_WS_DISCOVERY_TIMEOUT","1.5"))
WS_DISCOVERY_RETRIES=int(os.getenv("CAMXPOSED_WS_DISCOVERY_RETRIES","1"))
DISCOVERY_TIMEOUT=float(os.getenv("CAMXPOSED_DISCOVERY_TIMEOUT","1.0"))
DISCOVERY_RETRIES=int(os.getenv("CAMXPOSED_DISCOVERY_RETRIES","1"))
HTTP_TIMEOUT=float(os.getenv("CAMXPOSED_HTTP_TIMEOUT","10"))
FFMPEG=os.getenv("CAMXPOSED_FFMPEG","ffmpeg")
@dataclass
class Camera:
    id:str; name:str; channel:int=0; online:bool=True; capabilities:dict[str,Any]=field(default_factory=dict)
@dataclass
class Gateway:
    uuid:str; host:str; port:int; name:str; txt:dict[str,str]; gateway:dict[str,Any]=field(default_factory=dict)
    @property
    def base(self): return f"http://{self.host}:{self.port}"


def _xml_text(root: ET.Element, local_name: str) -> list[str]:
    return [
        (el.text or "").strip()
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == local_name and (el.text or "").strip()
    ]


def _parse_ws_discovery(data: bytes, addr: tuple[str, int]) -> Optional[Gateway]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    xaddrs = _xml_text(root, "XAddrs")
    addresses = _xml_text(root, "Address")
    types = _xml_text(root, "Types")
    if not xaddrs:
        return None
    from urllib.parse import urlparse
    for xaddr_string in xaddrs:
        for location in xaddr_string.split():
            try:
                u = urlparse(location)
            except ValueError:
                continue
            if u.scheme.lower() != "http" or not u.hostname:
                continue
            port = u.port or 80
            epr = addresses[0] if addresses else ""
            uuid_value = epr
            if uuid_value.lower().startswith("urn:uuid:"):
                uuid_value = uuid_value[9:]
            elif uuid_value.lower().startswith("uuid:"):
                uuid_value = uuid_value[5:]
            if not uuid_value:
                uuid_value = EXPECTED_UUID
            return Gateway(uuid=uuid_value, host=u.hostname, port=port, name="CamXposed", txt={"xaddr":location,"epr":epr,"types":types[0] if types else "","source":f"{addr[0]}:{addr[1]}"})
    return None


class WSDiscovery:
    """Native CamXposed WS-Discovery over UDP 3702.

    The discovery window is intentionally long and sends fresh WS-Discovery
    probes repeatedly. This makes discovery resilient to occasional Wi-Fi
    multicast packet loss while collecting replies continuously.
    """

    PROBE_INTERVAL = float(os.getenv("CAMXPOSED_WS_DISCOVERY_PROBE_INTERVAL", "0.35"))

    @staticmethod
    def _probe() -> bytes:
        message_id = f"urn:uuid:{uuid.uuid4()}"
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://www.w3.org/2005/08/addressing" xmlns:d="http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01">
 <s:Header>
  <a:Action s:mustUnderstand="1">http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01/Probe</a:Action>
  <a:MessageID>{message_id}</a:MessageID>
  <a:To s:mustUnderstand="1">urn:docs-oasis-open-org:ws-dd:ns:discovery:2009:01</a:To>
 </s:Header>
 <s:Body><d:Probe /></s:Body>
</s:Envelope>'''.encode("utf-8")

    @staticmethod
    def _interfaces() -> list[tuple[str, str]]:
        """Return usable IPv4 interfaces as (name, address)."""
        wanted = os.getenv("CAMXPOSED_WS_DISCOVERY_INTERFACE", "").strip()
        rows: list[tuple[str, str]] = []
        try:
            proc = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "up", "scope", "global"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            for line in proc.stdout.splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                name = parts[1]
                if wanted and name != wanted:
                    continue
                value = parts[3].split("/", 1)[0]
                if value and not value.startswith("127."):
                    rows.append((name, value))
        except (OSError, subprocess.SubprocessError):
            pass
        if rows:
            return rows
        try:
            host = socket.gethostname()
            seen = set()
            for item in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM):
                value = item[4][0]
                if value and not value.startswith("127.") and value not in seen:
                    rows.append(("auto", value)); seen.add(value)
        except OSError:
            pass
        return rows

    @staticmethod
    def discover(timeout: float = WS_DISCOVERY_TIMEOUT) -> list[Gateway]:
        total = max(0.5, float(timeout))
        interval = max(0.1, float(WSDiscovery.PROBE_INTERVAL))
        found: dict[str, Gateway] = {}
        interfaces = WSDiscovery._interfaces() or [("default", "0.0.0.0")]

        for _, local_ip in interfaces:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try: sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                except OSError: pass
                sock.bind((local_ip, 0))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
                if local_ip != "0.0.0.0":
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
                    try:
                        membership = socket.inet_aton(WS_DISCOVERY_ADDR[0]) + socket.inet_aton(local_ip)
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                    except OSError: pass

                start = time.monotonic(); deadline = start + total; next_probe = start
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if now >= next_probe:
                        sock.sendto(WSDiscovery._probe(), WS_DISCOVERY_ADDR)
                        next_probe = now + interval
                    sock.settimeout(max(0.02, min(deadline, next_probe) - time.monotonic()))
                    try: data, addr = sock.recvfrom(65535)
                    except socket.timeout: continue
                    gateway = _parse_ws_discovery(data, addr)
                    if gateway:
                        found[gateway.uuid] = gateway
                        break
            finally:
                sock.close()
            if found:
                break
        return list(found.values())


class DiscoveryError(RuntimeError):
    pass


class Client:
    def __init__(self):
        self.gateways={}; self.selected=None; self.camera=None; self.lock=threading.RLock(); self.last_discovery_error=None; self.alert_cursor=0
        self.alert_watch_enabled=False
        self.alert_watch_camera_ids: Optional[set[str]]=None
        self.alert_watch_since_id=0
        # Per-camera monotonic deadline for the most recent timed PTZ movement.
        self.ptz_move_until = {}
        self.ptz_state_lock = threading.Lock()
        self.playback_seq = int(time.time() * 1000)
        self.speak_lock = threading.Lock()
        self.stop_requested = threading.Event()
        self.active_speak_proc: Optional[subprocess.Popen] = None
        self.active_speak_conn: Optional[Any] = None
        self.active_lan_speaker: Optional[AnykaLanSpeaker] = None
        self.active_playback_id: Optional[int] = None
        self.last_snapshot_bytes: Optional[bytes] = None
        self.last_snapshot_time: float = 0.0
        self.camera_snapshots: dict[str, bytes] = {}
        self.connected_cameras: set[str] = set()
        self.cached_cameras: list[Camera] = []
        self.cached_cameras_time: float = 0.0
        # Local speaker volume percentage (0-100), persisted to config file, defaults to 50%
        self.config_dir = Path(os.getenv("CAMXPOSED_CONFIG_DIR", Path.home() / ".config" / "camxposed"))
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "speaker_settings.json"
        self.local_speaker_volume_percent: int = self._load_speaker_volume()

    def _load_speaker_volume(self) -> int:
        # Check environment variable first
        env_val = os.getenv("CAMXPOSED_LOCAL_SPEAKER_VOLUME")
        if env_val is not None:
            try: return max(0, min(100, int(env_val)))
            except ValueError: pass
        if self.config_file.exists():
            try:
                data = json.loads(self.config_file.read_text())
                if "volume_percent" in data:
                    return max(0, min(100, int(data["volume_percent"])))
            except Exception:
                pass
        return 50  # Default half volume

    def save_speaker_volume(self, volume_percent: int) -> None:
        try:
            clamped = max(0, min(100, int(volume_percent)))
            self.local_speaker_volume_percent = clamped
            data = {"volume_percent": clamped}
            self.config_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"Could not persist speaker volume to {self.config_file}: {e}")

    def http(self,m,url,body=None,headers=None,timeout=HTTP_TIMEOUT):
        try:
            r=urllib.request.urlopen(urllib.request.Request(url,data=body,method=m,headers=headers or {}),timeout=timeout)
            return r.status,r.read(),dict(r.headers)
        except urllib.error.HTTPError as e: return e.code,e.read(),dict(e.headers)

    def live(self,g):
        try:
            c,b,_=self.http("GET",g.base+"/v1/gateway")
            d=json.loads(b.decode())
            if c!=200 or not d.get("gatewayRunning") or not d.get("lanListening"): return False
            g.gateway=d; return True
        except Exception: return False

    def discover(self):
        """Discover live CamXposed gateways using WS-Discovery UDP 3702."""
        override = os.getenv("CAMXPOSED_GATEWAY_URL", "").strip()
        if override:
            from urllib.parse import urlparse
            parsed = urlparse(override)
            g = Gateway(EXPECTED_UUID, parsed.hostname or "", parsed.port or 80, "env-override", {})
            if self.live(g):
                self.gateways[g.uuid] = g
                self.last_discovery_error = None
                return [g]
            self.last_discovery_error = {"method":"environment_override","message":f"CAMXPOSED_GATEWAY_URL is set but gateway is not reachable: {override}"}
            return []

        attempts = max(1, int(os.getenv("CAMXPOSED_WS_DISCOVERY_RETRIES", str(WS_DISCOVERY_RETRIES))))
        timeout = float(os.getenv("CAMXPOSED_WS_DISCOVERY_TIMEOUT", str(WS_DISCOVERY_TIMEOUT)))
        found: list[Gateway] = []
        for attempt in range(attempts):
            try:
                found = WSDiscovery.discover(timeout)
            except OSError as e:
                self.last_discovery_error = {
                    "method":"ws-discovery", "stage":"socket", "message":str(e),
                    "possibleCause":"Local firewall/socket/network configuration may be preventing UDP 3702 multicast.",
                    "firewallCommands": {"ufwAllowDiscovery":"sudo ufw allow 3702/udp", "ufwDisableTemporary":"sudo ufw disable", "ufwStatus":"sudo ufw status verbose"},
                }
                return []
            if found or attempt + 1 >= attempts:
                break
            time.sleep(0.25)

        live=[]
        for g in found:
            if self.live(g):
                with self.lock: self.gateways[g.uuid]=g
                live.append(g)
        if live:
            self.last_discovery_error=None
            return live

        self.last_discovery_error = {
            "method":"ws-discovery", "stage":"no_gateway",
            "message":"No live CamXposed gateway was discovered via WS-Discovery on UDP 3702.",
            "possibleCauses":[
                "CamXposed/LSPosed bridge is not running or WS-Discovery is not listening on the Android device.",
                "PC firewall or network firewall is blocking UDP 3702 multicast/return traffic.",
                "Phone and PC are not on the same LAN/subnet or multicast is filtered by the access point.",
                "The gateway answered WS-Discovery but its HTTP API on TCP 18766 was not reachable.",
            ],
            "recommendedChecks":[
                "Verify the CamXposed/LSPosed bridge is actually running on the phone.",
                "Temporarily disable the PC firewall and retry discovery.",
                "If using UFW, allow WS-Discovery with: sudo ufw allow 3702/udp",
                "For a temporary UFW test use: sudo ufw disable",
                "Then verify with: sudo ufw status verbose",
                "Once discovery succeeds, TCP 18766 must also be reachable for the HTTP gateway.",
            ],
            "firewallCommands":{
                "ufwAllowDiscovery":"sudo ufw allow 3702/udp",
                "ufwAllowGateway":"sudo ufw allow 18766/tcp",
                "ufwDisableTemporary":"sudo ufw disable",
                "ufwReenable":"sudo ufw enable",
                "ufwStatus":"sudo ufw status verbose",
            },
        }
        return []

    def gateway(self):
        with self.lock:
            if self.selected and self.selected in self.gateways:
                g = self.gateways[self.selected]
                if self.live(g):
                    return g

        retries = max(1, int(os.getenv("CAMXPOSED_WS_DISCOVERY_RETRIES", str(WS_DISCOVERY_RETRIES))))
        for attempt in range(retries):
            live = self.discover()
            if live:
                g = next((x for x in live if x.uuid == EXPECTED_UUID), live[0])
                with self.lock:
                    self.selected = g.uuid
                return g
            if attempt + 1 < retries:
                time.sleep(0.25)

        detail = self.last_discovery_error or {"message": "No WS-Discovery result."}
        raise DiscoveryError(json.dumps(detail, ensure_ascii=False))

    def cameras(self,g=None,force_refresh=False):
        now = time.monotonic()
        with self.lock:
            if not force_refresh and self.cached_cameras and (now - self.cached_cameras_time) < 60.0:
                return self.cached_cameras

        cams = []
        # 1. Native LAN discovered cameras
        with lan_engine.lock:
            for lcam in lan_engine.discovered_cameras.values():
                cams.append(Camera(
                    id=lcam.id,
                    name=lcam.name,
                    channel=0,
                    online=True,
                    capabilities={
                        "ptz": lcam.ptz_supported,
                        "snapshot": bool(lcam.snapshot_url),
                        "stream": bool(lcam.stream_uri),
                        "presets": len(lcam.presets),
                        "lanNative": True,
                        "ip": lcam.ip,
                        "manufacturer": lcam.manufacturer,
                        "model": lcam.model,
                    }
                ))

        # 2. Bridge gateway cameras if available
        if g is not None:
            try:
                c,b,_=self.http("GET",g.base+"/v1/cameras",timeout=HTTP_TIMEOUT)
                if c < 300:
                    d=json.loads(b.decode())
                    items=d.get("cameras",d if isinstance(d,list) else [])
                    for x in items:
                        # Avoid duplicates if already discovered via LAN
                        cid = str(x["id"])
                        if not any(c.id == cid for c in cams):
                            cams.append(Camera(cid,str(x.get("name",cid)),int(x.get("channel",0)),bool(x.get("online",True)),x.get("capabilities",{})))
            except Exception as e:
                logger.warning(f"[cameras] Bridge /v1/cameras failed: {e}")

        with self.lock:
            self.cached_cameras = cams
            self.cached_cameras_time = now
        return cams

    def find_lan_camera(self, cid: str) -> Optional[LanCamera]:
        """Lookup a camera in the native LAN engine by ID or IP."""
        with lan_engine.lock:
            if cid in lan_engine.discovered_cameras:
                return lan_engine.discovered_cameras[cid]
            if cid.startswith("lan_"):
                ip = cid[4:].replace("_", ".")
                for cam in lan_engine.discovered_cameras.values():
                    if cam.ip == ip: return cam
            if re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", cid):
                for cam in lan_engine.discovered_cameras.values():
                    if cam.ip == cid: return cam
                # Auto-register on the fly for raw IP
                return lan_engine.interrogate_onvif(cid)
        return None

    def resolve_camera(self, camera_id: Optional[str] = None):
        target = str(camera_id).strip() if camera_id and str(camera_id).strip() else None
        if target:
            lan_cam = self.find_lan_camera(target)
            if lan_cam:
                return None, lan_cam.id
            # An explicit native LAN ID/IP may have changed after DHCP renewal.
            # Refresh once before treating it as a bridge camera identifier.
            if target.startswith("lan_") or re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", target):
                try:
                    lan_engine.discover_all(timeout=3.0)
                    lan_cam = self.find_lan_camera(target)
                    if lan_cam:
                        return None, lan_cam.id
                except Exception as e:
                    logger.warning(f"[resolve_camera] Native LAN refresh failed: {e}")
            try:
                g = self.gateway()
                return g, target
            except Exception:
                return None, target

        # Reuse a selected camera only while it is still present. If its DHCP
        # address changed, discard the stale ID and run a fresh LAN discovery.
        with self.lock:
            selected = self.camera
        if selected:
            lan_cam = self.find_lan_camera(selected)
            if lan_cam:
                return None, selected
            if selected.startswith("lan_") or re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", selected):
                with self.lock:
                    if self.camera == selected:
                        self.camera = None
            else:
                try:
                    return self.gateway(), selected
                except Exception as e:
                    logger.warning(f"[resolve_camera] Previously selected bridge camera unavailable: {e}")
                    with self.lock:
                        if self.camera == selected:
                            self.camera = None

        # Auto-discover before failing. The scan populates this process's own
        # inventory, so callers never need to invoke discovery tools manually.
        try:
            if not lan_engine.discovered_cameras or (time.time() - lan_engine.last_scan_time) > 30.0:
                logger.info("[resolve_camera] No fresh native camera inventory; running ONVIF discovery automatically")
                scan = lan_engine.discover_all(timeout=3.0)
                logger.info("[resolve_camera] Automatic ONVIF discovery found %s camera(s)",
                            scan.get("discoveredCamerasCount", 0))
        except Exception as e:
            logger.warning(f"[resolve_camera] Automatic ONVIF discovery failed: {e}")

        with lan_engine.lock:
            if lan_engine.discovered_cameras:
                first_lan = next(iter(lan_engine.discovered_cameras.values()))
                with self.lock:
                    self.camera = first_lan.id
                return None, first_lan.id

        try:
            g = self.gateway()
            cs = self.cameras(g, force_refresh=True)
            if cs:
                cid = cs[0].id
                with self.lock:
                    self.camera = cid
                return g, cid
        except Exception as e:
            logger.warning(f"[resolve_camera] Could not list cameras after automatic discovery ({e})")

        fallback_cid = os.getenv("CAMXPOSED_DEFAULT_CAMERA_ID", "").strip()
        if fallback_cid:
            return None, fallback_cid
        raise RuntimeError("No camera discovered. Automatic ONVIF discovery and CamXposed gateway discovery both failed.")

    def connect(self,g,cid):
        cid_str = str(cid)
        lan_cam = self.find_lan_camera(cid_str)
        if lan_cam:
            return {"ok": True, "cameraId": cid_str, "provider": "native_onvif", "ip": lan_cam.ip}
        if not g:
            g = self.gateway()
        with self.lock:
            if cid_str in self.connected_cameras:
                return {"ok":True,"cameraId":cid_str,"alreadyConnected":True}
        c,b,_=self.http("POST",f"{g.base}/v1/cameras/{cid_str}/connect",b"",{"Content-Type":"application/json"},8)
        if c>=300: raise RuntimeError(f"connect HTTP {c}: {b.decode(errors='replace')}")
        with self.lock:
            self.connected_cameras.add(cid_str)
        try:return json.loads(b.decode())
        except:return {"raw":b.decode(errors="replace")}

    def move(self,g,cid,direction,speed=8,duration=500):
        lan_cam = self.find_lan_camera(cid)
        if lan_cam:
            return lan_engine.ptz_move(lan_cam, direction, speed, duration)
        if not g:
            g = self.gateway()
        body=json.dumps({"direction":direction,"speed":speed,"durationMs":duration}).encode()
        c,b,_=self.http("POST",f"{g.base}/v1/cameras/{cid}/ptz",body,{"Content-Type":"application/json"})
        if c>=300: raise RuntimeError(f"PTZ HTTP {c}: {b.decode(errors='replace')}")
        try:return json.loads(b.decode())
        except:return {"raw":b.decode(errors="replace")}

    def observe(self, g, cid, width=800):
        t0 = time.time()
        lan_cam = self.find_lan_camera(cid)
        if lan_cam:
            logger.info(f"[observe] Fetching pristine snapshot directly via native LAN ONVIF from {lan_cam.ip}...")
            raw = lan_engine.capture_snapshot(lan_cam)
            if raw:
                logger.info(f"[observe] Native LAN snapshot captured for camera {cid}: {len(raw)} bytes in {time.time() - t0:.2f}s")
                with self.lock:
                    self.last_snapshot_bytes = raw
                    self.last_snapshot_time = time.time()
                    self.camera_snapshots[str(cid)] = raw
                return raw
            logger.warning(f"[observe] Native LAN snapshot failed for {lan_cam.ip}, falling back...")

        if not g:
            try:
                g = self.gateway()
            except Exception:
                g = None

        out = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
        raw = None
        if g:
            try:
                # Primary: pristine keyframe capture with tuned probesize for instant decode
                cmd = [
                    FFMPEG, "-hide_banner", "-loglevel", "error",
                    "-fflags", "nobuffer+discardcorrupt",
                    "-probesize", "64k", "-analyzeduration", "500k",
                    "-f", "hevc", "-i", f"{g.base}/v1/cameras/{cid}/video",
                    "-vf", f"select=eq(pict_type\\,I),scale={width}:-1",
                    "-vsync", "0", "-frames:v", "1", "-q:v", "5", "-y", out
                ]
                p = subprocess.run(cmd, capture_output=True, timeout=8)
                if p.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000:
                    raw = Path(out).read_bytes()
                    logger.info(f"[observe] Pristine I-frame captured for camera {cid}: {len(raw)} bytes in {time.time() - t0:.2f}s")
                else:
                    logger.warning(f"[observe] Primary I-frame capture returned rc={p.returncode}, attempting fast fallback...")
            except subprocess.TimeoutExpired:
                logger.warning(f"[observe] Primary I-frame capture timed out after 8s, attempting fast fallback...")
            except Exception as e:
                logger.warning(f"[observe] Primary I-frame capture exception: {e}, attempting fast fallback...")
            finally:
                try: os.unlink(out)
                except OSError: pass

            if raw is None:
                # Fast fallback: grab 1 immediate frame with minimal probe
                out_fb = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
                try:
                    cmd_fb = [
                        FFMPEG, "-hide_banner", "-loglevel", "error",
                        "-fflags", "nobuffer+discardcorrupt",
                        "-probesize", "32k", "-analyzeduration", "0",
                        "-f", "hevc", "-i", f"{g.base}/v1/cameras/{cid}/video",
                        "-frames:v", "1", "-vf", f"scale={width}:-1",
                        "-q:v", "5", "-y", out_fb
                    ]
                    p_fb = subprocess.run(cmd_fb, capture_output=True, timeout=4)
                    if p_fb.returncode == 0 and os.path.exists(out_fb) and os.path.getsize(out_fb) > 1000:
                        raw = Path(out_fb).read_bytes()
                        logger.info(f"[observe] Fast fallback frame captured for camera {cid}: {len(raw)} bytes in {time.time() - t0:.2f}s")
                except Exception as e:
                    logger.error(f"[observe] Fast fallback also failed: {e}")
                finally:
                    try: os.unlink(out_fb)
                    except OSError: pass

        if raw is None:
            # Last resort fallback: cached recent snapshot
            with self.lock:
                raw = self.camera_snapshots.get(str(cid)) or self.last_snapshot_bytes
            if raw:
                logger.warning(f"[observe] Using cached recent frame for camera {cid}: {len(raw)} bytes")
            else:
                raise RuntimeError(f"Failed to capture frame from camera {cid} and no cached frame available.")

        with self.lock:
            self.last_snapshot_bytes = raw
            self.last_snapshot_time = time.time()
            self.camera_snapshots[str(cid)] = raw
        return raw

    def listen(self,g,cid,seconds=2):
        lan_cam = self.find_lan_camera(cid)
        if lan_cam:
            logger.info(f"[listen] Capturing audio directly from native LAN ONVIF/RTSP ({lan_cam.ip})...")
            wav_bytes = lan_engine.capture_audio(lan_cam, seconds)
            metadata = {
                "cameraId": str(cid),
                "sourceCodec": "g711a_pcm",
                "sampleRate": 8000,
                "channels": 1,
                "durationSeconds": round(seconds, 3),
                "provider": "native_onvif",
                "format": "audio/wav; PCM signed 16-bit little-endian"
            }
            return [
                TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
                AudioContent(type="audio", data=base64.b64encode(wav_bytes).decode("ascii"),
                             mimeType="audio/wav")
            ]
        if not g:
            g = self.gateway()
        """Capture G.711 A-law from the bridge and return a native MCP audio content block.

        MCP audio content carries base64-encoded audio bytes on the wire, but unlike
        a JSON dataBase64 field it is typed as audio and can be handled as audio by
        clients that support MCP audio content. Convert the camera's raw A-law stream
        into a standard PCM WAV container before returning it.
        """
        data=bytearray(); start=time.monotonic()
        with urllib.request.urlopen(f"{g.base}/v1/cameras/{cid}/audio",timeout=max(6,seconds+2)) as r:
            while time.monotonic()-start<seconds:
                x=r.read(1600)
                if not x:break
                data.extend(x)
        if not data:
            raise RuntimeError("Camera audio capture returned no bytes")
        ffmpeg=shutil.which(FFMPEG)
        if not ffmpeg:
            raise RuntimeError(f"FFmpeg executable not found: {FFMPEG}; required to convert camera G.711 A-law to WAV")
        proc=subprocess.run(
            [ffmpeg,"-hide_banner","-loglevel","error",
             "-f","alaw","-ar","8000","-ac","1","-i","pipe:0",
             "-c:a","pcm_s16le","-f","wav","pipe:1"],
            input=bytes(data),capture_output=True,timeout=15
        )
        if proc.returncode or not proc.stdout.startswith(b"RIFF"):
            detail=proc.stderr.decode(errors="replace")[-1000:]
            raise RuntimeError("Failed to convert camera audio to PCM WAV"+(f": {detail}" if detail else ""))
        duration=len(data)/8000.0
        metadata={"cameraId":str(cid),"sourceCodec":"g711a","sampleRate":8000,
                  "channels":1,"durationSeconds":round(duration,3),
                  "format":"audio/wav; PCM signed 16-bit little-endian"}
        return [
            TextContent(type="text", text=json.dumps(metadata,ensure_ascii=False)),
            AudioContent(type="audio", data=base64.b64encode(proc.stdout).decode("ascii"),
                         mimeType="audio/wav")
        ]
    def speak_stop(self, g, cid, reason="user_barge_in"):
        self.stop_requested.set()
        with self.lock:
            pid = self.active_playback_id or self.playback_seq
            conn = self.active_speak_conn
            lan_speaker = self.active_lan_speaker
            self.active_speak_conn = None
            self.active_lan_speaker = None
            self.active_playback_id = None

        # 1. Immediate LAN abort: send TCP RST to port 28772 to instantly cut off ADEC session
        if lan_speaker:
            try:
                lan_speaker.abort(reason=reason)
            except Exception as e:
                logger.warning(f"[speak_stop] LAN speaker abort error: {e}")

        # 2. Immediate local termination: abort active HTTP socket connection instantly
        if conn:
            try:
                conn.close()
            except Exception:
                pass

        # 3. Out-of-band socket abort & vendor stopTalk trigger on the bridge if bridge exists
        if g:
            body = json.dumps({"playbackId": pid, "reason": reason}).encode()
            def _send_oob_stop():
                try:
                    self.http("POST", f"{g.base}/v1/cameras/{cid}/speak-stop", body, {"Content-Type": "application/json"}, timeout=2)
                except Exception as e:
                    logger.warning(f"[speak_stop] Out-of-band bridge stop notice error: {e}")

            # Dispatch out-of-band in background daemon thread for <2ms instant tool response
            threading.Thread(target=_send_oob_stop, daemon=True, name=f"speak-stop-{pid}").start()

        return {"ok": True, "stoppedPlaybackId": pid, "reason": reason, "immediate": True}

    def _stream_lan_speaker_worker(self, speaker: AnykaLanSpeaker, alaw: bytes, playback_id: int):
        """Worker thread to stream paced G.711 A-law frames directly to Anyka camera's TCP port 28772."""
        try:
            speaker.stream_alaw(alaw, playback_id=playback_id)
        except Exception as e:
            logger.warning(f"[speak] LAN speaker worker error for playback {playback_id}: {e}")
        finally:
            with self.lock:
                if self.active_playback_id == playback_id:
                    self.active_lan_speaker = None
                    self.active_playback_id = None

    def _stream_audio_worker(self, conn, alaw, playback_id, timeout):
        """Streams audio frames in 2048-byte HTTP chunks matching Vector's VectorSpeakerStream.

        Chunk size 2048 bytes = 256 ms of audio.
        Streams at the camera's playback rate without huge downstream buffer bloat,
        starting instantly on the camera without waiting for the whole utterance to finish.
        """
        CHUNK_BYTES = 2048
        try:
            total_len = len(alaw)
            offset = 0
            while offset < total_len and not self.stop_requested.is_set():
                chunk = alaw[offset:offset + CHUNK_BYTES]
                chunk_len = len(chunk)
                if chunk_len < CHUNK_BYTES:
                    # Pad tail with G.711 A-law silence (0xD5) to 160-byte frame boundary
                    rem = chunk_len % 160
                    if rem != 0:
                        chunk = chunk + b"\xD5" * (160 - rem)
                        chunk_len = len(chunk)

                # Send HTTP chunk: <hex_size>\r\n<data>\r\n
                header = f"{chunk_len:X}\r\n".encode("ascii")
                try:
                    conn.send(header + chunk + b"\r\n")
                except (BrokenPipeError, OSError, ConnectionResetError):
                    logger.info(f"[speak] Socket connection closed during playback {playback_id}")
                    break

                offset += chunk_len
                # Sleep for the playback duration of this chunk
                sleep_sec = chunk_len / 8000.0
                time.sleep(sleep_sec)

            if not self.stop_requested.is_set():
                try:
                    # End chunked stream with terminating 0-size chunk
                    conn.send(b"0\r\n\r\n")
                    resp = conn.getresponse()
                    logger.info(f"[speak] Stream {playback_id} finished: HTTP {resp.status}")
                except Exception as e:
                    logger.info(f"[speak] Stream {playback_id} end response: {e}")
        except Exception as e:
            logger.warning(f"[speak] Stream {playback_id} error: {e}")
        finally:
            try: conn.close()
            except Exception: pass
            with self.lock:
                if self.active_playback_id == playback_id:
                    self.active_speak_conn = None
                    self.active_playback_id = None

    def speak(self, g, cid, alaw, wait: bool = False):
        if not alaw:
            raise ValueError("No audio data provided to speak")
        
        # If a previous speech is still playing, abort/close it
        with self.lock:
            prev_conn = self.active_speak_conn
            prev_speaker = self.active_lan_speaker
        if prev_speaker:
            try: prev_speaker.abort(reason="new_utterance")
            except Exception: pass
        if prev_conn:
            try: prev_conn.close()
            except Exception: pass

        self.stop_requested.clear()
        with self.lock:
            self.playback_seq += 1
            playback_id = self.playback_seq

        timeout = max(30, int(len(alaw) / 8000) + 15)
        duration_ms = round((len(alaw) / 8000) * 1000)

        # 1. First priority: Check if target camera has an IP reachable on Anyka TCP port 28772
        target_ip = ""
        lan_cam = self.find_lan_camera(cid)
        if lan_cam and lan_cam.ip:
            target_ip = lan_cam.ip
        elif g and isinstance(cid, str) and cid.startswith("lan_"):
            target_ip = cid.removeprefix("lan_").replace("_", ".")

        if target_ip:
            try:
                logger.info(f"[speak] Attempting native Anyka TCP port 28772 speaker stream to {target_ip}...")
                lan_speaker = AnykaLanSpeaker(target_ip, connect_timeout=1.2)
                with self.lock:
                    self.active_lan_speaker = lan_speaker
                    self.active_playback_id = playback_id

                worker = threading.Thread(
                    target=self._stream_lan_speaker_worker,
                    args=(lan_speaker, alaw, playback_id),
                    daemon=True,
                    name=f"anyka-lan-speak-{playback_id}"
                )
                worker.start()

                if wait:
                    worker.join(timeout=timeout)
                    return {
                        "ok": True,
                        "status": "completed",
                        "playbackId": playback_id,
                        "route": "native_anyka_tcp",
                        "target": f"{target_ip}:28772",
                        "durationMs": duration_ms
                    }

                # Brief settling wait before returning non-blocking response
                time.sleep(0.10)
                return {
                    "ok": True,
                    "status": "speaking",
                    "route": "native_anyka_tcp",
                    "target": f"{target_ip}:28772",
                    "playbackId": playback_id,
                    "durationMs": duration_ms,
                    "message": f"Camera speaker is broadcasting over LAN TCP 28772 ({duration_ms}ms estimated). Call camera_speak_stop to interrupt."
                }
            except Exception as e:
                logger.warning(f"[speak] Native Anyka TCP speaker to {target_ip}:28772 failed ({e}). Checking gateway fallback...")

        # 2. Second priority: Fall back to Android CamXposed/Vector HTTP bridge if available
        if not g:
            try:
                g = self.gateway()
            except Exception:
                pass

        if not g:
            raise RuntimeError(f"Could not connect to camera speaker: Anyka LAN TCP connection failed and no CamXposed bridge gateway is available.")

        logger.info(f"[speak] Streaming audio via CamXposed bridge gateway {g.base}...")
        import http.client
        conn = http.client.HTTPConnection(g.host, g.port, timeout=timeout)
        headers = {
            "Content-Type": "audio/x-alaw",
            "Transfer-Encoding": "chunked",
            "X-Playback-Id": str(playback_id),
            "Connection": "keep-alive"
        }
        conn.putrequest("POST", f"/v1/cameras/{cid}/speak-stream")
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()

        with self.lock:
            self.active_speak_conn = conn
            self.active_playback_id = playback_id

        # Launch background audio streaming worker
        worker = threading.Thread(
            target=self._stream_audio_worker,
            args=(conn, alaw, playback_id, timeout),
            daemon=True,
            name=f"speak-stream-{playback_id}"
        )
        worker.start()

        if wait:
            worker.join(timeout=timeout)
            return {"ok": True, "status": "completed", "route": "gateway_bridge", "playbackId": playback_id, "durationMs": duration_ms}

        # Return immediately so the agent is not blocked and can issue camera_speak_stop mid-playback
        time.sleep(0.15)
        return {
            "ok": True,
            "status": "speaking",
            "route": "gateway_bridge",
            "playbackId": playback_id,
            "durationMs": duration_ms,
            "message": f"Camera is now speaking via gateway ({duration_ms}ms estimated). Call camera_speak_stop to interrupt."
        }
    def alerts(self,g,since_id=0,limit=50):
        limit=max(1,min(200,int(limit)))
        url=f"{g.base}/v1/alerts?sinceId={max(0,int(since_id))}&limit={limit}"
        c,b,_=self.http("GET",url,timeout=HTTP_TIMEOUT)
        if c>=300: raise RuntimeError(f"/v1/alerts HTTP {c}: {b.decode(errors='replace')}")
        data=json.loads(b.decode())
        if not data.get("ok"): raise RuntimeError("Bridge returned unsuccessful alert response")
        # Canonical camera identity is always a string. Never infer identity from nickname/index.
        for event in data.get("events", []):
            raw_id = event.get("cameraId")
            event["cameraId"] = str(raw_id).strip() if raw_id not in (None, "") else ""
            if event.get("rawCameraId") is not None:
                event["rawCameraId"] = str(event["rawCameraId"]).strip()
            event.setdefault("imageAvailable", False)
            event.setdefault("imagePath", f"/v1/alerts/{int(event.get('id', 0))}/image")
        return data

    def onvif_discover(self, g):
        c, b, _ = self.http("POST", f"{g.base}/v1/onvif/discover", b"", {"Content-Type": "application/json"}, timeout=12)
        if c >= 300: raise RuntimeError(f"/v1/onvif/discover HTTP {c}: {b.decode(errors='replace')}")
        return json.loads(b.decode())

    def onvif_cameras(self, g):
        c, b, _ = self.http("GET", f"{g.base}/v1/onvif/cameras", timeout=HTTP_TIMEOUT)
        if c >= 300: raise RuntimeError(f"/v1/onvif/cameras HTTP {c}: {b.decode(errors='replace')}")
        return json.loads(b.decode())

    def onvif_inspect(self, g, ip_address, device_service_url=None, username=None, password=None):
        payload = {"ipAddress": str(ip_address).strip()}
        if device_service_url: payload["deviceServiceUrl"] = str(device_service_url).strip()
        if username is not None: payload["username"] = str(username).strip()
        if password is not None: payload["password"] = str(password).strip()
        body = json.dumps(payload).encode()
        c, b, _ = self.http("POST", f"{g.base}/v1/onvif/inspect", body, {"Content-Type": "application/json"}, timeout=15)
        if c >= 300: raise RuntimeError(f"/v1/onvif/inspect HTTP {c}: {b.decode(errors='replace')}")
        return json.loads(b.decode())

    def onvif_soap(self, endpoint, action, body_xml, username="admin", password=""):
        """Direct lightweight ONVIF SOAP caller with WS-Security UsernameToken Digest/Text support."""
        nonce = os.urandom(16)
        created = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        import hashlib
        hasher = hashlib.sha1()
        hasher.update(nonce + created.encode('utf-8') + (password or "").encode('utf-8'))
        digest = base64.b64encode(hasher.digest()).decode('ascii')
        nonce_b64 = base64.b64encode(nonce).decode('ascii')
        wsse = f'''<wsse:Security s:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
          <wsse:UsernameToken>
            <wsse:Username>{username or "admin"}</wsse:Username>
            <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>
            <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>
            <wsu:Created>{created}</wsu:Created>
          </wsse:UsernameToken>
        </wsse:Security>'''
        envelope = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
  <s:Header>{wsse}</s:Header>
  <s:Body>{body_xml}</s:Body>
</s:Envelope>'''
        headers = {"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'}
        req = urllib.request.Request(endpoint, data=envelope.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")

    def onvif_presets(self, camera_ip, action="list", preset_token=None, preset_name=None, username="admin", password="", profile_token="PROFILE_000", port=8888):
        """Manage or execute ONVIF PTZ Presets (list, goto, set, remove)."""
        endpoint = f"http://{camera_ip}:{port}/onvif/ptz_service"
        op = (action or "list").strip().lower()
        if op == "list":
            status, resp = self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/GetPresets", f"<tptz:GetPresets><tptz:ProfileToken>{profile_token}</tptz:ProfileToken></tptz:GetPresets>", username, password)
            if status >= 300: raise RuntimeError(f"GetPresets HTTP {status}: {resp[:300]}")
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp)
            presets = []
            for p in root.iter():
                if p.tag.endswith("Preset"):
                    token = p.attrib.get("token", "")
                    name = ""
                    for c in p:
                        if c.tag.endswith("Name"): name = c.text or ""
                    if token: presets.append({"token": token, "name": name or token})
            return {"ok": True, "action": "list", "cameraIp": camera_ip, "profileToken": profile_token, "count": len(presets), "presets": presets}
        elif op == "goto":
            if not preset_token: raise ValueError("preset_token is required for 'goto' action")
            status, resp = self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/GotoPreset", f"<tptz:GotoPreset><tptz:ProfileToken>{profile_token}</tptz:ProfileToken><tptz:PresetToken>{preset_token}</tptz:PresetToken></tptz:GotoPreset>", username, password)
            if status >= 300: raise RuntimeError(f"GotoPreset HTTP {status}: {resp[:300]}")
            return {"ok": True, "action": "goto", "cameraIp": camera_ip, "presetToken": preset_token, "status": "Preset movement dispatched"}
        elif op == "set":
            if not preset_name: raise ValueError("preset_name is required for 'set' action")
            status, resp = self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/SetPreset", f"<tptz:SetPreset><tptz:ProfileToken>{profile_token}</tptz:ProfileToken><tptz:PresetName>{preset_name}</tptz:PresetName></tptz:SetPreset>", username, password)
            if status >= 300: raise RuntimeError(f"SetPreset HTTP {status}: {resp[:300]}")
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp)
            created_token = ""
            for p in root.iter():
                if p.tag.endswith("PresetToken"): created_token = p.text or ""
            return {"ok": True, "action": "set", "cameraIp": camera_ip, "presetToken": created_token, "name": preset_name}
        elif op == "remove":
            if not preset_token: raise ValueError("preset_token is required for 'remove' action")
            status, resp = self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/RemovePreset", f"<tptz:RemovePreset><tptz:ProfileToken>{profile_token}</tptz:ProfileToken><tptz:PresetToken>{preset_token}</tptz:PresetToken></tptz:RemovePreset>", username, password)
            if status >= 300: raise RuntimeError(f"RemovePreset HTTP {status}: {resp[:300]}")
            return {"ok": True, "action": "remove", "cameraIp": camera_ip, "presetToken": preset_token}
        else:
            raise ValueError("action must be 'list', 'goto', 'set', or 'remove'")

client=Client()
def gj(g):return {"uuid":g.uuid,"host":g.host,"port":g.port,"name":g.name,"service":SERVICE_TYPE,"txt":g.txt,"gateway":g.gateway}
def cj(c):return {"id":c.id,"name":c.name,"channel":c.channel,"online":c.online,"capabilities":c.capabilities}

@mcp.custom_route("/snapshot.jpg", methods=["GET"])
async def snapshot_route(request):
    from starlette.responses import Response
    cid = request.query_params.get("camera_id")
    with client.lock:
        if cid and str(cid) in client.camera_snapshots:
            data = client.camera_snapshots[str(cid)]
        else:
            data = client.last_snapshot_bytes
    if data:
        return Response(data, media_type="image/jpeg")
    return Response(b"No snapshot available yet. Call camera_observe first.", status_code=404, media_type="text/plain")

@mcp.custom_route("/alert_image.jpg", methods=["GET"])
async def alert_image_route(request):
    from starlette.responses import Response
    alert_id = request.query_params.get("id")
    if not alert_id:
        return Response(b"Missing 'id' query parameter", status_code=400, media_type="text/plain")
    try:
        g = client.gateway()
        status, data, headers = client.http("GET", f"{g.base}/v1/alerts/{int(alert_id)}/image", timeout=HTTP_TIMEOUT)
        if status == 200:
            content_type = headers.get("Content-Type", "image/jpeg")
            return Response(data, media_type=content_type)
        return Response(b"Image not found", status_code=status, media_type="text/plain")
    except Exception as e:
        return Response(str(e).encode(), status_code=500, media_type="text/plain")

@mcp.tool()
def camera_discover():
    """Discover CamXposed gateways and ONVIF cameras on LAN using native multi-protocol discovery (WS-Discovery UDP 3702, UPnP SSDP UDP 1900, mDNS DNS-SD)."""
    lan_res = lan_engine.discover_all(timeout=2.0)
    # Refresh the combined camera list immediately after the live LAN scan.
    with client.lock:
        client.cached_cameras = []
        client.cached_cameras_time = 0.0
        if client.camera and (client.camera.startswith("lan_") or re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", client.camera)):
            with lan_engine.lock:
                if client.camera not in lan_engine.discovered_cameras:
                    client.camera = None
    gs = client.discover()
    gateways = [gj(g) for g in gs]
    for gw in lan_res.get("discoveredGateways", []):
        if not any(g["host"] == gw["host"] for g in gateways):
            gateways.append(gw)
    return {
        "ok": True,
        "protocols": lan_res.get("protocolsUsed", []),
        "gateways": gateways,
        "selectedGateway": client.selected,
        "lanCameras": lan_res.get("cameras", []),
        "scanDurationSec": lan_res.get("scanDurationSec", 0.0)
    }

@mcp.tool()
def camera_list():
    """List available cameras (both native LAN ONVIF cameras and bridge-managed cameras). Default camera is automatically targeted by move/observe/speak."""
    g = None
    try:
        g = client.gateway()
    except Exception:
        pass
    cs = client.cameras(g)
    return {
        "ok": True,
        "gateway": gj(g) if g else None,
        "cameraCount": len(cs),
        "cameras": [cj(c) for c in cs]
    }

@mcp.tool()
def onvif_discover(timeout_seconds: float = 2.0):
    """Trigger native active ONVIF discovery directly from MCP across WS-Discovery, UPnP SSDP, and mDNS.
    
    Discovers all LAN ONVIF cameras, inspects device capabilities, and registers them directly into MCP.
    """
    result = lan_engine.discover_all(timeout=max(1.0, min(10.0, float(timeout_seconds))))
    with client.lock:
        client.cached_cameras = []
        client.cached_cameras_time = 0.0
        if client.camera and (client.camera.startswith("lan_") or re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", client.camera)):
            with lan_engine.lock:
                if client.camera not in lan_engine.discovered_cameras:
                    client.camera = None
    return result

@mcp.tool()
def onvif_cameras():
    """Return the currently discovered native ONVIF camera inventory from MCP memory."""
    with lan_engine.lock:
        cams = [
            {
                "id": c.id,
                "ip": c.ip,
                "name": c.name,
                "port": c.port,
                "profileToken": c.profile_token,
                "snapshotUrl": c.snapshot_url,
                "streamUri": c.stream_uri,
                "ptzSupported": c.ptz_supported,
                "presetCount": len(c.presets),
                "presets": c.presets,
                "manufacturer": c.manufacturer,
                "model": c.model,
                "firmware": c.firmware,
            }
            for c in lan_engine.discovered_cameras.values()
        ]
    return {"ok": True, "provider": "native_lan", "count": len(cams), "cameras": cams}

@mcp.tool()
def onvif_inspect(ip_address: str, device_service_url: Optional[str] = None, username: Optional[str] = "admin", password: Optional[str] = ""):
    """Interrogate an ONVIF camera at the given IP natively via ONVIF SOAP.
    
    Enumerates ONVIF capabilities, device information, PTZ services, presets, and media profiles (RTSP stream and JPEG snapshot URIs).
    """
    cam = lan_engine.interrogate_onvif(str(ip_address).strip(), username=username or "admin", password=password or "")
    if cam:
        return {
            "ok": True,
            "provider": "native_onvif",
            "camera": {
                "id": cam.id,
                "ip": cam.ip,
                "name": cam.name,
                "deviceServiceUrl": cam.device_service_url,
                "mediaServiceUrl": cam.media_service_url,
                "ptzServiceUrl": cam.ptz_service_url,
                "snapshotUrl": cam.snapshot_url,
                "streamUri": cam.stream_uri,
                "profileToken": cam.profile_token,
                "manufacturer": cam.manufacturer,
                "model": cam.model,
                "firmware": cam.firmware,
                "presetCount": len(cam.presets),
                "presets": cam.presets
            }
        }
    return {"ok": False, "error": f"Failed to interrogate ONVIF camera at {ip_address}"}

@mcp.tool()
def camera_presets(camera_id: Optional[str] = None, action: str = "list", preset_token: Optional[str] = None, name: Optional[str] = None, username: Optional[str] = "admin", password: Optional[str] = ""):
    """Manage and navigate PTZ presets on an ONVIF-capable camera.
    
    Parameters:
    - camera_id: Target camera ID, e.g. "lan_10_64_137_213" or canonical UID or IP address. If omitted, uses default camera.
    - action: "list" (enumerate presets), "goto" (move to preset_token), "set" (save current position as preset with name), or "remove" (delete preset_token).
    - preset_token: Token of the preset (e.g. "PRESET_1"), required for "goto" and "remove".
    - name: Friendly name when setting a preset (e.g. "Door", "Desk"), required for "set".
    """
    g, cid = client.resolve_camera(camera_id)
    ip = None
    if cid.startswith("lan_"):
        ip = cid[4:].replace("_", ".")
    elif re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", cid):
        ip = cid
    else:
        lan_cam = client.find_lan_camera(cid)
        if lan_cam:
            ip = lan_cam.ip
        elif g:
            try:
                cs = client.onvif_cameras(g)
                for c in cs.get("cameras", []):
                    if c.get("cameraId") == cid and c.get("ipAddress"):
                        ip = c["ipAddress"]
                        break
            except Exception:
                pass
    if not ip:
        ip = os.getenv("CAMXPOSED_DEFAULT_ONVIF_IP", "10.64.137.213")

    return client.onvif_presets(
        camera_ip=ip,
        action=action,
        preset_token=preset_token,
        preset_name=name,
        username=username or "admin",
        password=password or "",
    )

@mcp.tool()
def camera_ptz_capabilities(camera_id: Optional[str] = None):
    """Read the selected ONVIF camera's advertised PTZ capabilities, including velocity ranges, speed spaces, translation spaces, position spaces, and timeout range."""
    g, cid = client.resolve_camera(camera_id)
    lan_cam = client.find_lan_camera(cid)
    if lan_cam:
        return lan_engine.ptz_capabilities(lan_cam)
    # Check gateway fallback
    if g:
        status, data, _ = client.http("GET", f"{g.base}/v1/cameras/{cid}/ptz/capabilities")
        if status < 300:
            return json.loads(data.decode())
    return {"ok": False, "error": f"Camera {cid} does not expose ONVIF PTZ capabilities"}

@mcp.tool()
def camera_ptz_status(camera_id: Optional[str] = None):
    """Read current ONVIF pan, tilt, zoom position coordinates and move status (IDLE/MOVING)."""
    g, cid = client.resolve_camera(camera_id)
    lan_cam = client.find_lan_camera(cid)
    if lan_cam:
        return lan_engine.ptz_status(lan_cam)
    if g:
        status, data, _ = client.http("GET", f"{g.base}/v1/cameras/{cid}/ptz/status")
        if status < 300:
            return json.loads(data.decode())
    return {"ok": False, "error": f"Camera {cid} does not expose ONVIF PTZ status"}

@mcp.tool()
def camera_ptz_relative_move(camera_id: Optional[str] = None, x: float = 0.0, y: float = 0.0, zoom: float = 0.0, speed: int = 4, wait: bool = True):
    """Perform a finite ONVIF RelativeMove using normalized translation offsets and speed.
    
    Parameters:
    - camera_id: Target camera ID. If omitted, uses default active camera.
    - x: Normalized relative pan offset (-1.0 to 1.0, where negative is left, positive is right).
    - y: Normalized relative tilt offset (-1.0 to 1.0, where negative is down, positive is up).
    - zoom: Normalized relative zoom offset (-1.0 to 1.0).
    - speed: Integer 1-8 controlling the movement speed. Default is 4.
    - wait: Boolean. Default is True. Waits for mechanical settling before returning.
    """
    g, cid = client.resolve_camera(camera_id)
    lan_cam = client.find_lan_camera(cid)
    if not lan_cam:
        if g:
            body = json.dumps({"x": x, "y": y, "zoom": zoom, "speed": speed}).encode()
            st, data, _ = client.http("POST", f"{g.base}/v1/cameras/{cid}/ptz/relative", body, {"Content-Type": "application/json"})
            if st < 300:
                res = json.loads(data.decode())
            else:
                return {"ok": False, "error": f"Relative PTZ HTTP {st}"}
        else:
            return {"ok": False, "error": f"Camera {cid} does not support ONVIF RelativeMove"}
    else:
        res = lan_engine.ptz_relative_move(lan_cam, x=x, y=y, zoom=zoom, speed=speed)

    with client.ptz_state_lock:
        client.ptz_move_until[cid] = time.monotonic() + 0.8

    if wait and (x != 0 or y != 0 or zoom != 0):
        time.sleep(0.8)
        res["settled"] = True
    return res

@mcp.tool()
def camera_select(camera_id:str):
    """Select/connect one camera for default operations. Input is an ID from camera_list(). Returns the selected camera."""
    cs=client.cameras()
    c=next((x for x in cs if x.id==camera_id),None)
    if not c:
        lan_cam = client.find_lan_camera(camera_id)
        if lan_cam:
            client.camera = lan_cam.id
            return {"ok": True, "camera": {"id": lan_cam.id, "name": lan_cam.name, "provider": "native_onvif"}}
        raise ValueError("Unknown camera: "+camera_id)
    client.camera=camera_id
    g = None
    try:
        g = client.gateway()
        client.connect(g, camera_id)
    except Exception:
        pass
    return {"ok":True,"camera":cj(c),"gateway":gj(g) if g else None}

@mcp.tool()
def camera_status(camera_id:Optional[str]=None):
    """Return camera liveness and capabilities. Omit camera_id for the current/default camera."""
    cs=client.cameras()
    cid=camera_id or client.camera or (cs[0].id if cs else None)
    if not cid:
        raise RuntimeError("No camera available")
    lan_cam = client.find_lan_camera(cid)
    if lan_cam:
        return {
            "ok": True,
            "provider": "native_onvif",
            "camera": {
                "id": lan_cam.id,
                "name": lan_cam.name,
                "ip": lan_cam.ip,
                "online": True,
                "ptzSupported": lan_cam.ptz_supported,
                "presetCount": len(lan_cam.presets),
                "snapshotUrl": lan_cam.snapshot_url,
                "streamUri": lan_cam.stream_uri
            }
        }
    g=None
    try: g=client.gateway()
    except Exception: pass
    c=next((c for c in cs if c.id==cid), None)
    return {"ok":True,"gateway":gj(g) if g else None,"camera":cj(c) if c else {"id": cid}}
@mcp.tool()
def camera_alerts(since_id:Optional[int]=None, limit:int=50, camera_id:Optional[str]=None):
    """Read queued FCM human-body detection events. Camera IDs are canonical UID strings (or LAN format "lan_192_168_1_x"). Use since_id to resume. Image metadata is included when available; call camera_alert_image(event_id) for the image."""
    g=client.gateway()
    cursor=client.alert_cursor if since_id is None else max(0,int(since_id))
    data=client.alerts(g,cursor,max(1,min(int(limit),200)))
    events=data.get("events",[])
    wanted=str(camera_id).strip() if camera_id is not None else None
    if wanted is not None:
        events=[e for e in events if str(e.get("cameraId", "")) == wanted]
    # Advance past all scanned events even when a camera filter excludes them;
    # otherwise the same unrelated events would be returned forever.
    scanned_latest=max((int(e.get("id",0)) for e in data.get("events",[])), default=0)
    if since_id is None:
        client.alert_cursor=max(client.alert_cursor, scanned_latest, int(data.get("latestId",cursor)) if not data.get("events") else 0)
    return {"ok":True,"gateway":gj(g),"alerts":events,"count":len(events),
            "latestId":int(data.get("latestId",0)),"oldestId":int(data.get("oldestId",0)),
            "droppedThroughId":int(data.get("droppedThroughId",0)),"cursor":client.alert_cursor,
            "cameraIdFilter":wanted}

@mcp.tool()
def camera_watch_human_alerts(camera_ids:Optional[list[str]]=None, enabled:bool=True, since_id:Optional[int]=None):
    """Arm or disarm the MCP server's persistent-in-process human-alert watch configuration. Camera IDs can be canonical numeric UIDs or "lan_192_168_1_x". This config tells the agent/runtime what events to wait for; it does not itself wake an idle host. A host automation must keep camera_wait_for_human_alert active or use its own supported wake trigger. Omit camera_ids to watch all cameras."""
    with client.lock:
        client.alert_watch_enabled=bool(enabled)
        client.alert_watch_camera_ids=(None if camera_ids is None else {str(x).strip() for x in camera_ids if str(x).strip()})
        if since_id is not None:
            client.alert_watch_since_id=max(0,int(since_id))
            client.alert_cursor=client.alert_watch_since_id
        result={"ok":True,"enabled":client.alert_watch_enabled,
                "cameraIds":None if client.alert_watch_camera_ids is None else sorted(client.alert_watch_camera_ids),
                "sinceId":client.alert_watch_since_id,
                "delivery":"blocking-tool-wait; host wake-up requires runtime support"}
    return result

@mcp.tool()
def camera_alert_image(alert_id:int):
    """Fetch the full notification image saved for a human-body alert and return it as a native MCP image. Use the event's id from camera_alerts or camera_wait_for_human_alert. Returns an error if that alert has no saved image."""
    g=client.gateway()
    event_id=max(1,int(alert_id))
    status,data,headers=client.http("GET",f"{g.base}/v1/alerts/{event_id}/image",timeout=HTTP_TIMEOUT)
    if status>=300:
        try: detail=json.loads(data.decode()).get("error",data.decode(errors="replace"))
        except Exception: detail=data.decode(errors="replace")
        return {"ok":False,"alertId":event_id,"error":f"image fetch HTTP {status}: {detail}"}
    content_type=headers.get("Content-Type",headers.get("content-type","image/jpeg")).split(";",1)[0].strip().lower()
    image_format="png" if content_type=="image/png" else "jpeg"
    return Image(data=data,format=image_format)

@mcp.tool()
def camera_wait_for_human_alert(timeout_seconds:int=30, since_id:Optional[int]=None, camera_id:Optional[str]=None):
    """Block until a new human-body FCM alert arrives or timeout expires. Intended for an agent/runtime that explicitly arms a watch and keeps this wait active. The bridge currently exposes a bounded queue, so this MCP server checks that queue internally; it does not create an unsolicited agent turn by itself. camera_id can be canonical UID string or "lan_192_168_1_x"."""
    timeout=max(1,min(int(timeout_seconds),120))
    g=client.gateway()
    with client.lock:
        if not client.alert_watch_enabled:
            # Backwards compatible: direct calls still work, but the tool tells the agent
            # the persistent watch wasn't explicitly armed.
            watch_ids=None
        else:
            watch_ids=client.alert_watch_camera_ids
    wanted=str(camera_id).strip() if camera_id is not None else None
    if wanted is None and watch_ids is not None and len(watch_ids)==1:
        wanted=next(iter(watch_ids))
    cursor=client.alert_cursor if since_id is None else max(0,int(since_id))
    deadline=time.monotonic()+timeout
    last=None
    while True:
        data=client.alerts(g,cursor,50)
        events=data.get("events",[])
        for event in events:
            event_id=int(event.get("id",0))
            if event_id <= cursor: continue
            event_camera=str(event.get("cameraId", ""))
            if wanted is not None and event_camera != wanted: continue
            if watch_ids is not None and event_camera not in watch_ids: continue
            client.alert_cursor=max(client.alert_cursor,event_id)
            client.alert_watch_since_id=max(client.alert_watch_since_id,event_id)
            return {"ok":True,"detected":True,"event":event,"cursor":client.alert_cursor,
                    "imageAvailable":bool(event.get("imageAvailable",False)),
                    "imageTool":"camera_alert_image","imageAlertId":event_id,
                    "cameraId":event_camera,"remainingQueued":max(0,len(events)-1),
                    "latestId":int(data.get("latestId",0)),
                    "agentInstruction":"Evaluate this event and inspect camera_alert_image(imageAlertId) when an image is available."}
        last=data
        # Advance the local scan cursor past non-matching events so a camera filter
        # cannot cause the same bounded page to be scanned repeatedly.
        if events:
            cursor=max(cursor,max(int(e.get("id",0)) for e in events))
        elif data.get("latestId") is not None:
            cursor=max(cursor,int(data.get("latestId",cursor)))
        if time.monotonic()>=deadline:
            return {"ok":True,"detected":False,"timeoutSeconds":timeout,
                    "latestId":int(last.get("latestId",cursor)),"cursor":client.alert_cursor,
                    "watchEnabled":client.alert_watch_enabled}
        time.sleep(0.75)

@mcp.tool()
def camera_observe(camera_id:Optional[str]=None,width:int=800,latest_frame:bool=False):
    """Capture a frame from one camera and return a native MCP JPEG image.

    By default, waits until the most recent timed PTZ move for this camera has
    finished, then waits an additional 400 ms for the view to settle before
    capturing. This avoids immediately returning a frame from before/during a
    just-requested movement.

    WARNING: latest_frame=True bypasses this wait and may return a stale or
    mid-movement frame. Use only when you intentionally want an immediate grab.
    """
    g, cid = client.resolve_camera(camera_id)
    try: client.connect(g, cid)
    except Exception: pass
    waited_ms = 0
    if not latest_frame:
        with client.ptz_state_lock:
            move_until = client.ptz_move_until.get(cid, 0.0)
        delay = max(0.0, move_until - time.monotonic()) + 0.4
        if delay > 0:
            logger.info(f"[camera_observe] Camera {cid} is in motion/settling; waiting {delay:.2f}s before capture...")
            time.sleep(delay)
            waited_ms = int(delay * 1000)
    # Allow lightweight downscaling (down to 120px) to conserve LLM context tokens and bandwidth
    target_width = max(120, min(int(width), 1280)) if width else 640
    logger.info(f"[camera_observe] Capturing frame for camera {cid} (waited_ms={waited_ms}, width={target_width})...")
    frame_bytes = client.observe(g, cid, target_width)
    logger.info(f"[camera_observe] Returning native JPEG image ({len(frame_bytes)} bytes)")
    return Image(data=frame_bytes, format="jpeg")

@mcp.tool()
def camera_listen(camera_id:Optional[str]=None,seconds:float=2):
    """Capture a short audio sample for inspection. seconds is 0.25-5."""
    g, cid = client.resolve_camera(camera_id)
    try: client.connect(g, cid)
    except Exception: pass
    logger.info(f"[camera_listen] Listening to camera {cid} for {seconds}s...")
    res = client.listen(g,cid,max(.25,min(seconds,5)))
    logger.info(f"[camera_listen] Done listening to camera {cid}")
    return res

@mcp.tool()
def camera_move(camera_id:Optional[str]=None,direction:str="right",speed:int=8,duration_ms:int=700,wait:bool=True):
    """Perform one manual PTZ move on the camera.

    SPEED & DURATION WARNING:
    - Default speed is 8 (maximum) and duration_ms is 700 ms.
    - CRITICAL: A low speed like 3 for half a second (500-600ms) is WAY too low! The camera stepper motors have mechanical gear deadband/slack, so speed 3 for half a second barely nudges 1-2 degrees and appears completely frozen to the user.
    - ALWAYS use speed 7 or 8 with duration 600-1200ms for standard visible framing adjustments. Do NOT choose low speeds like 1-4 unless the user explicitly asks for ultra-fine millimeter micro-adjustments. Run carefully and avoid weak half-second moves at low speeds!

    Parameters:
    - direction: 'left', 'right', 'up', 'down', or 'stop'.
    - speed: Integer 1-8. Default is 8. (Always prefer 7 or 8 for real, visible motion).
    - duration_ms: Movement duration in milliseconds (100-2000). Default is 700 ms. (600-1200 ms recommended).
    - wait: Boolean. Default is True. Waits for the physical movement to complete and settle before returning, ensuring the camera is completely still before any subsequent camera_observe frame capture.
    """
    dir_clean = str(direction).strip().lower()
    if dir_clean not in {"left", "right", "up", "down", "stop"}:
        logger.warning(f"[camera_move] Unknown direction '{direction}', defaulting to 'stop'")
        dir_clean = "stop"
    direction = dir_clean

    try:
        speed_int = int(speed)
    except (ValueError, TypeError):
        speed_int = 8
    speed = max(1, min(speed_int, 8))

    try:
        duration_int = int(duration_ms)
    except (ValueError, TypeError):
        duration_int = 700
    duration_ms = max(100, min(duration_int, 4000))

    g, cid = client.resolve_camera(camera_id)
    try: client.connect(g, cid)
    except Exception: pass

    logger.info(f"[camera_move] Moving camera {cid}: direction={direction}, speed={speed}, duration_ms={duration_ms}")
    move_started = time.monotonic()
    result = client.move(g, cid, direction, speed, duration_ms)
    # Record deadline from request start so a bridge that already waits for the
    # movement duration does not cause a second full-duration wait.
    with client.ptz_state_lock:
        if direction == "stop":
            client.ptz_move_until[cid] = time.monotonic()
        else:
            client.ptz_move_until[cid] = move_started + duration_ms / 1000.0

    if wait and direction != "stop":
        # Sleep for duration + 300ms mechanical settling so camera physically finishes moving
        settle_time = (duration_ms / 1000.0) + 0.3
        time.sleep(settle_time)
        result["settled"] = True

    logger.info(f"[camera_move] PTZ completed: {result}. Settled.")
    return result

def tts(text: str, voice: Optional[str] = None, volume_percent: Optional[int] = None):
    """Generate speech or singing/melodic audio with Edge TTS, then encode it for camera talkback.

    Automatically selects the best neural voice:
    - If text contains Devanagari (Hindi) script -> hi-IN-MadhurNeural
    - If text is English / Latin script -> en-IN-PrabhatNeural (crystal clear English without phonetic distortion)
    - Or uses custom requested voice if specified.
    """
    clean_text = text.strip()
    # Check if text is full SSML
    is_ssml = clean_text.startswith("<speak") and clean_text.endswith("</speak>")

    if not voice:
        env_voice = os.getenv("CAMXPOSED_TTS_VOICE")
        if env_voice:
            voice = env_voice
        elif not is_ssml:
            has_devanagari = bool(re.search(r"[\u0900-\u097F]", clean_text))
            voice = "hi-IN-MadhurNeural" if has_devanagari else "en-IN-PrabhatNeural"
        else:
            voice = "hi-IN-MadhurNeural"

    # Volume scaling: client default is 50% (half volume), clamped between 0 and 100
    vol = client.local_speaker_volume_percent if volume_percent is None else volume_percent
    vol = max(0, min(100, int(vol)))
    vol_scale = vol / 100.0

    mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
    try:
        async def generate():
            if is_ssml:
                communicate = edge_tts.Communicate(clean_text, voice)
            else:
                communicate = edge_tts.Communicate(text=clean_text, voice=voice)
            await communicate.save(mp3)
        try:
            asyncio.run(generate())
        except Exception as exc:
            raise RuntimeError(f"Edge TTS failed for voice {voice}: {exc}") from exc
        try:
            # Use soxr resampling with high quality filter for clear, resonant audio playback,
            # and scale volume using ffmpeg's volume filter
            proc = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", mp3,
                 "-af", f"aresample=resampler=soxr:precision=28:osf=s16:cutoff=0.99,volume={vol_scale:.2f}",
                 "-ac", "1", "-ar", "8000", "-c:a", "pcm_alaw", "-f", "alaw", "pipe:1"],
                capture_output=True, timeout=30
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"FFmpeg executable not found: {FFMPEG}. Install ffmpeg or set CAMXPOSED_FFMPEG.") from exc
        if proc.returncode or not proc.stdout:
            detail = proc.stderr.decode(errors="replace")[-1000:]
            raise RuntimeError("Failed to convert Edge TTS audio to G.711 A-law" + (f": {detail}" if detail else ""))
        return proc.stdout
    finally:
        try: os.unlink(mp3)
        except OSError: pass

@mcp.tool()
def camera_speaker_volume(volume_percent: Optional[int] = None) -> dict[str, Any]:
    """Get or set the physical camera speaker talkback volume percentage (0-100%).
    
    Defaults to 50% (half volume) for comfortable indoor volume that does not clip or distort the camera speaker.
    - If volume_percent is provided: updates the speaker volume setting for all subsequent announcements and speech.
    - If volume_percent is omitted: returns the current speaker volume percentage.
    """
    if volume_percent is not None:
        clamped = max(0, min(100, int(volume_percent)))
        client.save_speaker_volume(clamped)
        logger.info(f"[camera_speaker_volume] Updated and persisted speaker volume to {clamped}%")
        return {
            "ok": True,
            "speakerVolumePercent": clamped,
            "message": f"Camera speaker volume set to {clamped}% (persisted across restarts)"
        }
    return {
        "ok": True,
        "speakerVolumePercent": client.local_speaker_volume_percent,
        "message": f"Current camera speaker volume is {client.local_speaker_volume_percent}%"
    }

@mcp.tool()
def camera_speak(text:str, camera_id:Optional[str]=None, wait:bool=False, voice:Optional[str]=None, volume_percent:Optional[int]=None):
    """Broadcast spoken announcements, singing, or melodies out loud into the physical room through the camera speaker.

    CRITICAL USAGE INSTRUCTIONS:
    - ONLY call this tool when the user EXPLICITLY asks to broadcast, announce, speak, or sing THROUGH THE CAMERA (e.g. "tell whoever is in the room that dinner is ready", "announce via the camera", "sing a song through the camera speaker").
    - NEVER call this tool to respond to normal conversational queries, status updates, or to acknowledge commands. Your normal voice chat responses are already delivered directly to the user through your own conversation channel!
    - Do NOT narrate your actions through the camera (e.g., do NOT call camera_speak to say "I am checking the camera" or "Camera moved").

    VOLUME:
    - volume_percent: Integer 0-100 (defaults to 50% half volume). Can be adjusted per-utterance or persistently via camera_speaker_volume.

    VOICE & LANGUAGE:
    - Hindi text automatically uses hi-IN-MadhurNeural.
    - English text automatically uses en-IN-PrabhatNeural (crystal clear English without accent distortion).
    - Custom voice can be provided in the voice parameter.

    SINGING / MELODY / CHANTING (SSML):
    - When asked to sing, chant, or hum a tune, provide SSML with <prosody pitch="+XHz" rate="Y%"> or semitones (pitch="+5st").
      Example:
        <speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="hi-IN">
          <voice name="hi-IN-MadhurNeural">
            <prosody pitch="+40Hz" rate="-10%">हैप्पी </prosody>
            <prosody pitch="+70Hz" rate="-10%">बर्थडे </prosody>
            <prosody pitch="+100Hz" rate="-20%">टू यू! </prosody>
          </voice>
        </speak>

    Playback streams in real-time. Can be interrupted at any moment using camera_speak_stop.
    """
    g, cid = client.resolve_camera(camera_id)
    try: client.connect(g, cid)
    except Exception: pass
    vol = client.local_speaker_volume_percent if volume_percent is None else max(0, min(100, int(volume_percent)))
    logger.info(f"[camera_speak] Speaking through camera {cid} (wait={wait}, voice={voice}, volume={vol}%): '{text[:80]}...'")
    audio = tts(text, voice=voice, volume_percent=vol)
    logger.info(f"[camera_speak] Generated {len(audio)} bytes G.711 A-law audio from Edge TTS. Launching stream...")
    res = client.speak(g, cid, audio, wait=wait)
    res["speakerVolumePercent"] = vol
    logger.info(f"[camera_speak] Result: {res}")
    return res

@mcp.tool()
def camera_speak_stop(camera_id:Optional[str]=None, reason:str="user_barge_in"):
    """Immediately stop and cut off any active camera speech/talkback playback. Use for instant barge-in response when user speaks or interrupts."""
    g, cid = client.resolve_camera(camera_id)
    logger.info(f"[camera_speak_stop] Stopping speech on camera {cid} (reason: {reason})")
    res = client.speak_stop(g, cid, reason)
    logger.info(f"[camera_speak_stop] Result: {res}")
    return res

def main():
    import argparse
    parser = argparse.ArgumentParser(description="CamXposed MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default=os.getenv("CAMXPOSED_TRANSPORT", "stdio"), help="Transport type")
    parser.add_argument("--host", default=os.getenv("CAMXPOSED_HOST", "0.0.0.0"), help="Host to bind for HTTP/SSE")
    parser.add_argument("--port", type=int, default=int(os.getenv("CAMXPOSED_PORT", "8000")), help="Port to bind for HTTP/SSE")
    args, _ = parser.parse_known_args()

    if args.transport == "streamable-http":
        from mcp.server.transport_security import TransportSecuritySettings
        sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        mcp.run(transport="streamable-http", host=args.host, port=args.port, transport_security=sec)
    elif args.transport == "sse":
        from mcp.server.transport_security import TransportSecuritySettings
        sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        mcp.run(transport="sse", host=args.host, port=args.port, transport_security=sec)
    else:
        mcp.run(transport="stdio")

if __name__=="__main__":
    main()
