#!/usr/bin/env python3
"""
lan_engine.py - Native LAN discovery and ONVIF engine for CamXposed MCP server.
Supports:
  1. Multi-protocol LAN discovery:
     - WS-Discovery (UDP 3702, probe dn:NetworkVideoTransmitter)
     - UPnP SSDP (UDP 1900, M-SEARCH)
     - mDNS / DNS-SD (_camxposed._tcp.local. and _onvif._tcp.local.)
  2. Native ONVIF camera control & media:
     - Direct HTTP Digest snapshot capture (/snapshot or GetSnapshotUri)
     - Direct ONVIF PTZ ContinuousMove, Stop, and RelativeMove
     - Direct RTSP audio capture via ffmpeg
     - WS-Security UsernameToken (digest and plaintext) authentication
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("camxposed.lan")

def _get_local_ipv4_addresses() -> list[str]:
    """Enumerates all non-loopback IPv4 addresses across network interfaces."""
    addrs: set[str] = set()
    try:
        import ifaddr
        for iface in ifaddr.get_adapters():
            for ip in iface.ips:
                if isinstance(ip.ip, str):
                    try:
                        parsed = ipaddress.ip_address(ip.ip)
                        if not parsed.is_loopback and not parsed.is_link_local:
                            addrs.add(ip.ip)
                    except Exception:
                        pass
    except Exception:
        pass

    if not addrs:
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    addrs.add(ip)
        except Exception:
            pass

    return list(addrs)

DEFAULT_MOTION_COOLDOWN_AFTER_MOVE_S = float(os.getenv("CAMXPOSED_MOTION_COOLDOWN_S", "4.0"))

def _movement_state_file() -> Path:
    log_dir = Path(os.getenv("CAMXPOSED_LOG_DIR", str(Path(tempfile.gettempdir()) / "camxposed-mcp")))
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "camera_movement.json"

def _session_state_file() -> Path:
    log_dir = Path(os.getenv("CAMXPOSED_LOG_DIR", str(Path(tempfile.gettempdir()) / "camxposed-mcp")))
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "session_state.json"

def _normalize_cam_keys(cam_key: str) -> list[str]:
    """Returns normalized representations (both IP and ID) for a camera identifier."""
    raw = str(cam_key).strip()
    keys = {raw}
    if raw.startswith("lan_"):
        ip_part = raw[4:].replace("_", ".")
        keys.add(ip_part)
    elif "." in raw:
        id_part = "lan_" + raw.replace(".", "_")
        keys.add(id_part)
    return list(keys)


WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)
SSDP_ADDR = ("239.255.255.250", 1900)

@dataclass
class LanCamera:
    id: str  # e.g. "lan_10_64_137_213"
    ip: str
    name: str = "ONVIF Camera"
    port: int = 8888
    device_service_url: str = ""
    media_service_url: str = ""
    ptz_service_url: str = ""
    events_service_url: str = ""
    snapshot_url: str = ""
    stream_uri: str = ""
    username: str = "admin"
    password: str = ""
    profile_token: str = "PROFILE_000"
    ptz_configuration_token: str = "PTZConfigurationToken"
    mac: str = ""
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""
    ptz_supported: bool = True
    last_seen: float = 0.0

def _xml_text(elem: ET.Element, tag_name: str) -> str:
    for child in elem.iter():
        if child.tag.rsplit("}", 1)[-1] == tag_name:
            return (child.text or "").strip()
    return ""

def _xml_attr(elem: ET.Element, tag_name: str, attr_name: str) -> str:
    for child in elem.iter():
        if child.tag.rsplit("}", 1)[-1] == tag_name:
            return child.attrib.get(attr_name, "")
    return ""

class LanEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.discovered_cameras: dict[str, LanCamera] = {}
        self.discovered_gateways: dict[str, dict[str, Any]] = {}
        self.ptz_move_states: dict[str, dict[str, Any]] = {}
        self._movement_records: dict[str, dict[str, Any]] = {}
        self._live_session_active: bool = False
        self._alerts_enabled: bool = False
        self._agent_working: bool = False
        self._last_agent_done_time: float = 0.0
        self.last_scan_time: float = 0.0

    def record_camera_movement(self, cam_key: str, duration_ms: int = 800, cooldown_s: Optional[float] = None) -> None:
        """Records camera movement start, end, and post-movement settling cooldown."""
        if cooldown_s is None:
            cooldown_s = DEFAULT_MOTION_COOLDOWN_AFTER_MOVE_S
        now = time.time()
        move_until = now + (max(0, duration_ms) / 1000.0)
        cooldown_until = move_until + max(0.0, cooldown_s)

        record = {
            "move_start": now,
            "move_until": move_until,
            "cooldown_until": cooldown_until,
            "cooldown_s": cooldown_s,
            "duration_ms": duration_ms,
        }

        with self.lock:
            for k in _normalize_cam_keys(cam_key):
                self._movement_records[k] = record

        # Persist to shared file so other processes (live bridge, server) share movement state
        try:
            filepath = _movement_state_file()
            state = {}
            if filepath.exists():
                try:
                    state = json.loads(filepath.read_text(encoding="utf-8"))
                except Exception:
                    state = {}
            for k in _normalize_cam_keys(cam_key):
                state[k] = record
            tmp_path = filepath.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
            tmp_path.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp_path, filepath)
            logger.debug(f"[record_camera_movement] Camera {cam_key} moving for {duration_ms}ms + {cooldown_s}s settling cooldown")
        except Exception as e:
            logger.debug(f"[record_camera_movement] Failed to persist movement state: {e}")

    def set_live_session_state(self, is_active: bool, alerts_enabled: bool = False, camera_ip: str = "") -> None:
        """Sets the live voice bridge session state and alert enablement across processes."""
        with self.lock:
            self._live_session_active = is_active
            self._alerts_enabled = alerts_enabled
        try:
            filepath = _session_state_file()
            state = {}
            if filepath.exists():
                try:
                    state = json.loads(filepath.read_text(encoding="utf-8"))
                except Exception:
                    state = {}
            state["is_live_active"] = is_active
            state["alerts_enabled"] = alerts_enabled
            if camera_ip:
                state["camera_ip"] = camera_ip
            state["updated_at"] = time.time()
            tmp_path = filepath.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
            tmp_path.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp_path, filepath)
            logger.info(f"[SessionState] Updated: live_active={is_active}, alerts_enabled={alerts_enabled}")
        except Exception as e:
            logger.debug(f"[set_live_session_state] Failed to persist session state: {e}")

    def is_live_session_active(self) -> bool:
        """Returns True if the camera is currently connected to ChatGPT live voice session."""
        try:
            filepath = _session_state_file()
            if filepath.exists():
                state = json.loads(filepath.read_text(encoding="utf-8"))
                if (time.time() - state.get("updated_at", 0.0)) < 120.0:
                    return bool(state.get("is_live_active", False))
        except Exception:
            pass
        with self.lock:
            return self._live_session_active

    def are_alerts_enabled(self) -> bool:
        """Returns True ONLY when alerts are explicitly enabled (e.g. in Standby mode).
        Returns False all the while camera is active and connection is idle.
        """
        try:
            filepath = _session_state_file()
            if filepath.exists():
                state = json.loads(filepath.read_text(encoding="utf-8"))
                if (time.time() - state.get("updated_at", 0.0)) < 120.0:
                    return bool(state.get("alerts_enabled", False))
        except Exception:
            pass
        with self.lock:
            return self._alerts_enabled

    def record_agent_working(self, is_working: bool, detail: str = "") -> None:
        """Records whether the AI agent / MCP tool is actively executing work."""
        now = time.time()
        with self.lock:
            self._agent_working = is_working
            if not is_working:
                self._last_agent_done_time = now
        try:
            filepath = _session_state_file()
            state = {}
            if filepath.exists():
                try:
                    state = json.loads(filepath.read_text(encoding="utf-8"))
                except Exception:
                    state = {}
            state["agent_working"] = is_working
            state["agent_detail"] = detail
            if is_working:
                state["last_agent_active_time"] = now
            else:
                state["last_agent_done_time"] = now
            state["updated_at"] = now
            tmp_path = filepath.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
            tmp_path.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp_path, filepath)
            logger.debug(f"[record_agent_working] Agent working={is_working} ({detail})")
        except Exception as e:
            logger.debug(f"[record_agent_working] Failed to persist agent state: {e}")

    def is_agent_working(self) -> bool:
        """Returns True if an agent MCP tool is currently running."""
        try:
            filepath = _session_state_file()
            if filepath.exists():
                state = json.loads(filepath.read_text(encoding="utf-8"))
                if bool(state.get("agent_working", False)):
                    # Guard against stale state if crashed: max tool timeout 60s
                    if (time.time() - state.get("last_agent_active_time", 0.0)) < 60.0:
                        return True
        except Exception:
            pass
        with self.lock:
            return self._agent_working

    def get_last_agent_done_time(self) -> float:
        """Returns the unix timestamp when the agent last finished executing."""
        try:
            filepath = _session_state_file()
            if filepath.exists():
                state = json.loads(filepath.read_text(encoding="utf-8"))
                return float(state.get("last_agent_done_time", 0.0))
        except Exception:
            pass
        with self.lock:
            return self._last_agent_done_time

    def is_camera_moving_or_cooling(self, cam_key: str, cam: Optional[LanCamera] = None, event_utc: Optional[str] = None) -> tuple[bool, float]:
        """Returns (True, remaining_cooldown_s) if camera is currently moving or settling post-movement (during move or within 4s cooldown).
        Checks current arrival time, hardware moving status, and event_utc timestamp.
        """
        now = time.time()
        keys = _normalize_cam_keys(cam_key)

        event_ts = None
        if event_utc:
            try:
                from datetime import datetime
                clean_utc = str(event_utc).strip().replace("Z", "+00:00")
                event_ts = datetime.fromisoformat(clean_utc).timestamp()
            except Exception:
                event_ts = None

        def _is_record_active(rec: Optional[dict[str, Any]]) -> tuple[bool, float]:
            if not rec:
                return False, 0.0
            move_start = rec.get("move_start", 0.0)
            cooldown_until = rec.get("cooldown_until", 0.0)
            # 1. Current clock check (event arrived while moving or within 4s cooldown)
            if now < cooldown_until:
                return True, max(0.0, cooldown_until - now)
            # 2. Event generation timestamp check (in case camera buffered event and delivered it slightly later)
            if event_ts is not None and abs(event_ts - now) < 86400:
                if (move_start - 0.5) <= event_ts <= (cooldown_until + 0.5):
                    return True, max(0.0, cooldown_until - now)
            return False, 0.0

        # 1. Check local cache first
        with self.lock:
            for k in keys:
                rec = self._movement_records.get(k)
                active, rem = _is_record_active(rec)
                if active:
                    return True, rem

        # 2. Check shared file (if written by another process, e.g. server.py)
        try:
            filepath = _movement_state_file()
            if filepath.exists():
                state = json.loads(filepath.read_text(encoding="utf-8"))
                for k in keys:
                    rec = state.get(k)
                    active, rem = _is_record_active(rec)
                    if active:
                        # Update local cache
                        with self.lock:
                            for sub_k in keys:
                                self._movement_records[sub_k] = rec
                        return True, rem
        except Exception:
            pass

        # 3. Hardware check if cam object provided and ptz supported
        if cam and getattr(cam, "ptz_supported", False):
            try:
                status = self.ptz_status(cam)
                move_stat = status.get("moveStatus", {})
                if move_stat.get("panTilt") == "MOVING" or move_stat.get("zoom") == "MOVING":
                    # Camera hardware reports active movement; record settling
                    self.record_camera_movement(cam.ip, duration_ms=1000)
                    return True, DEFAULT_MOTION_COOLDOWN_AFTER_MOVE_S
            except Exception:
                pass

        return False, 0.0

    @staticmethod
    def is_motion_event(topic: str, data: Optional[dict[str, Any]] = None) -> bool:
        """Determines if an ONVIF event topic or payload is a video motion/detector alarm."""
        t_low = (topic or "").lower()
        motion_keywords = ("motion", "cellmotion", "fielddetector", "linedetector", "motionalarm", "analytics", "ruledetector")
        if any(kw in t_low for kw in motion_keywords):
            return True
        if data:
            for k in data.keys():
                if any(kw in k.lower() for kw in ("motion", "detector")):
                    return True
        return False

    @staticmethod
    def is_active_alarm(data: dict[str, Any]) -> bool:
        """Returns True if the event payload indicates an active alarm, False if cleared/inactive."""
        if not data:
            return True
        for k, v in data.items():
            v_str = str(v).lower().strip()
            if v_str in ("false", "0", "idle", "cleared", "no_motion"):
                return False
        return True


    def onvif_soap(self, endpoint: str, action: str, body_xml: str, username: str = "admin", password: str = "", timeout: float = 5.0) -> tuple[int, str]:
        """Direct lightweight ONVIF SOAP caller with WS-Security UsernameToken Digest/Text support."""
        nonce = os.urandom(16)
        created = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        hasher = hashlib.sha1()
        hasher.update(nonce + created.encode('utf-8') + (password or "").encode('utf-8'))
        digest = base64.b64encode(hasher.digest()).decode('ascii')
        nonce_b64 = base64.b64encode(nonce).decode('ascii')
        
        wsse = ""
        if username or password:
            wsse = f'''<wsse:Security s:mustUnderstand="1" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
          <wsse:UsernameToken>
            <wsse:Username>{username or "admin"}</wsse:Username>
            <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>
            <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>
            <wsu:Created>{created}</wsu:Created>
          </wsse:UsernameToken>
        </wsse:Security>'''
        
        envelope = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
  <s:Header>{wsse}</s:Header>
  <s:Body>{body_xml}</s:Body>
</s:Envelope>'''
        headers = {
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
            "SOAPAction": action
        }
        req = urllib.request.Request(endpoint, data=envelope.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except Exception as e:
            return 0, str(e)

    def probe_ws_discovery(self, timeout: float = 2.0) -> list[dict[str, Any]]:
        """Perform ONVIF WS-Discovery across all local network interfaces.

        Sends probes across all local network interfaces via IP_MULTICAST_IF
        to ensure discovery across multi-homed systems (e.g. Ethernet + Wi-Fi).
        Falls back to the wsdiscovery library if available and no responses received.
        """
        message_id = f"urn:uuid:{uuid.uuid4()}"
        probe = f'''<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>{message_id}</w:MessageID>
  <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>'''.encode("utf-8")

        found: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", 0))
            sock.settimeout(0.25)

            # Send probe on default interface
            try:
                sock.sendto(probe, WS_DISCOVERY_ADDR)
            except Exception:
                pass

            # Send probe across each active local IPv4 interface
            local_ips = _get_local_ipv4_addresses()
            for lip in local_ips:
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lip))
                    sock.sendto(probe, WS_DISCOVERY_ADDR)
                except Exception as e:
                    logger.debug(f"[probe_ws_discovery] Failed to send on interface {lip}: {e}")

            deadline = time.monotonic() + max(0.5, float(timeout))
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                try:
                    root = ET.fromstring(data)
                    xaddrs = []
                    types = []
                    scopes = []
                    epr = ""
                    for node in root.iter():
                        tag = node.tag.rsplit("}", 1)[-1]
                        value = (node.text or "").strip()
                        if tag == "XAddrs" and value:
                            xaddrs.extend(value.split())
                        elif tag == "Types" and value:
                            types.extend(value.split())
                        elif tag == "Scopes" and value:
                            scopes.extend(value.split())
                        elif tag == "Address" and value and value.startswith("urn:"):
                            epr = value
                    if not xaddrs:
                        continue
                    key = (addr[0], tuple(sorted(xaddrs)))
                    found[key] = {
                        "epr": epr,
                        "xaddrs": xaddrs,
                        "types": types,
                        "scopes": scopes,
                        "isOnvif": True,
                        "source": "ws-discovery-udp",
                        "sourceIp": addr[0],
                    }
                except ET.ParseError:
                    logger.debug("[probe_ws_discovery] Ignoring non-XML UDP response from %s", addr[0])
        except Exception as e:
            logger.warning(f"[probe_ws_discovery] Native UDP probe failed: {e}")
        finally:
            sock.close()

        # Fallback to wsdiscovery library if 0 responses received
        if not found:
            try:
                from wsdiscovery.discovery import ThreadedWSDiscovery as WSDiscovery
                wsd = WSDiscovery()
                wsd.start()
                svcs = wsd.searchServices(timeout=max(1.5, timeout))
                for s in svcs:
                    xaddrs = s.getXAddrs() or []
                    if not xaddrs:
                        continue
                    scopes = [str(sc) for sc in (s.getScopes() or [])]
                    types = [str(t) for t in (s.getTypes() or [])]
                    source_ip = ""
                    try:
                        u = urllib.parse.urlparse(xaddrs[0])
                        source_ip = u.hostname or ""
                    except Exception:
                        pass
                    key = (source_ip, tuple(sorted(xaddrs)))
                    found[key] = {
                        "epr": str(s.getEPR() or ""),
                        "xaddrs": xaddrs,
                        "types": types,
                        "scopes": scopes,
                        "isOnvif": True,
                        "source": "wsdiscovery-lib",
                        "sourceIp": source_ip,
                    }
                wsd.stop()
            except Exception as e:
                logger.debug(f"[probe_ws_discovery] wsdiscovery library fallback error: {e}")

        results = list(found.values())
        logger.info("[probe_ws_discovery] WS-Discovery received %d ONVIF response(s)", len(results))
        return results

    def probe_ssdp(self, timeout: float = 1.5) -> list[dict[str, Any]]:
        """Active UPnP SSDP M-SEARCH over UDP 1900 across all local interfaces."""
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            "MAN: \"ssdp:discover\"\r\n"
            "MX: 2\r\n"
            "ST: ssdp:all\r\n"
            "\r\n"
        ).encode("utf-8")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(max(0.1, timeout))
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        results = []
        try:
            try:
                sock.sendto(msg, SSDP_ADDR)
            except Exception:
                pass
            for lip in _get_local_ipv4_addresses():
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lip))
                    sock.sendto(msg, SSDP_ADDR)
                except Exception:
                    pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                    text = data.decode("utf-8", errors="replace")
                    headers = {}
                    for line in text.split("\r\n"):
                        if ":" in line:
                            k, v = line.split(":", 1)
                            headers[k.strip().lower()] = v.strip()
                    results.append({"addr": addr, "headers": headers, "source": "ssdp"})
                except socket.timeout:
                    break
        except Exception as e:
            logger.warning(f"[probe_ssdp] Exception: {e}")
        finally:
            sock.close()
        return results
    def probe_mdns(self, timeout: float = 1.5) -> list[dict[str, Any]]:
        """Active mDNS browse for _camxposed._tcp.local. and _onvif._tcp.local."""
        results = []
        try:
            from zeroconf import Zeroconf, ServiceBrowser
            class Listener:
                def __init__(self):
                    self.services = []
                def remove_service(self, zc, type_, name):
                    pass
                def add_service(self, zc, type_, name):
                    info = zc.get_service_info(type_, name)
                    if info:
                        self.services.append({
                            "name": name,
                            "type": type_,
                            "addresses": info.parsed_addresses(),
                            "port": info.port,
                            "properties": {k.decode('utf-8', 'replace'): v.decode('utf-8', 'replace') if isinstance(v, bytes) else str(v) for k, v in info.properties.items()},
                            "source": "mdns"
                        })
                def update_service(self, zc, type_, name):
                    pass
            zc = Zeroconf()
            listener = Listener()
            b1 = ServiceBrowser(zc, "_camxposed._tcp.local.", listener)
            b2 = ServiceBrowser(zc, "_onvif._tcp.local.", listener)
            time.sleep(timeout)
            results = listener.services
            zc.close()
        except Exception as e:
            logger.warning(f"[probe_mdns] Exception: {e}")
        return results

    def interrogate_onvif(self, ip: str, port: int = 8888, username: str = "admin", password: str = "") -> Optional[LanCamera]:
        """Interrogates an ONVIF camera directly via SOAP to discover services, profiles, and capabilities."""
        device_url = f"http://{ip}:{port}/onvif/device_service"
        # 1. GetCapabilities
        st, cap_resp = self.onvif_soap(device_url, "http://www.onvif.org/ver10/device/wsdl/GetCapabilities",
                                      "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>",
                                      username, password)
        if st >= 300 or "<Fault>" in cap_resp or ":Fault>" in cap_resp:
            logger.warning(f"[interrogate_onvif] GetCapabilities failed on {ip}:{port} (HTTP {st})")
            return None

        # Extract Media and PTZ XAddrs
        media_url = f"http://{ip}:{port}/onvif/media"
        ptz_url = f"http://{ip}:{port}/onvif/ptz_service"
        events_url = f"http://{ip}:{port}/onvif/event_service"
        try:
            root = ET.fromstring(cap_resp)
            for m in root.iter():
                tag = m.tag.rsplit("}", 1)[-1]
                if tag == "Media":
                    x = _xml_text(m, "XAddr")
                    if x: media_url = x
                elif tag == "PTZ":
                    x = _xml_text(m, "XAddr")
                    if x: ptz_url = x
                elif tag == "Events":
                    x = _xml_text(m, "XAddr")
                    if x: events_url = x
        except Exception as e:
            logger.warning(f"[interrogate_onvif] Cap parse error: {e}")

        # 2. GetDeviceInformation
        mfr, model, fw = "ONVIF", "IP Camera", ""
        st_info, info_resp = self.onvif_soap(device_url, "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
                                             "<tds:GetDeviceInformation/>", username, password)
        if st_info < 300 and "<Fault>" not in info_resp:
            try:
                root_info = ET.fromstring(info_resp)
                mfr = _xml_text(root_info, "Manufacturer") or mfr
                model = _xml_text(root_info, "Model") or model
                fw = _xml_text(root_info, "FirmwareVersion") or fw
            except Exception:
                pass

        # Recorder/NVR endpoints may answer WS-Discovery but are not usable
        # room cameras. Exclude devices whose ONVIF identity explicitly says
        # recorder/NVR/DVR before attempting media profile registration.
        identity = " ".join((mfr, model, fw)).strip()
        if re.search(r"\b(?:nvr|dvr|recorder|network video recorder|digital video recorder)\b", identity, re.IGNORECASE):
            logger.info("[interrogate_onvif] Excluding recorder-class ONVIF device %s (%s)", ip, identity)
            return None

        # 3. GetProfiles. A valid media profile is required for a usable camera;
        # merely answering WS-Discovery is not enough (NVRs and other ONVIF
        # devices can advertise the same NetworkVideoTransmitter type).
        profile_token = ""
        ptz_config_token = "PTZConfigurationToken"
        stream_uri = f"rtsp://{ip}:554/ch0_0.264"
        snapshot_url = f"http://{ip}:{port}/snapshot"
        st_prof, prof_resp = self.onvif_soap(media_url, "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
                                             "<trt:GetProfiles/>", username, password)
        if st_prof < 300 and "<Fault>" not in prof_resp:
            try:
                root_prof = ET.fromstring(prof_resp)
                for p in root_prof.iter():
                    if p.tag.rsplit("}", 1)[-1] == "Profiles":
                        tok = p.attrib.get("token", "")
                        if tok and not profile_token:
                            profile_token = tok
                    elif p.tag.rsplit("}", 1)[-1] == "PTZConfiguration":
                        cfg_tok = p.attrib.get("token", "")
                        if cfg_tok:
                            ptz_config_token = cfg_tok
            except Exception as e:
                logger.warning("[interrogate_onvif] Invalid GetProfiles response from %s:%s: %s", ip, port, e)

        if not profile_token:
            logger.info("[interrogate_onvif] Excluding %s:%s: no usable ONVIF media profile (likely NVR/non-camera endpoint)", ip, port)
            return None

        # GetSnapshotUri
        st_snap, snap_resp = self.onvif_soap(media_url, "http://www.onvif.org/ver10/media/wsdl/GetSnapshotUri",
                                             f"<trt:GetSnapshotUri><trt:ProfileToken>{profile_token}</trt:ProfileToken></trt:GetSnapshotUri>",
                                             username, password)
        if st_snap < 300 and "<Fault>" not in snap_resp:
            try:
                root_snap = ET.fromstring(snap_resp)
                u = _xml_text(root_snap, "Uri")
                if u: snapshot_url = u
            except Exception:
                pass

        # GetStreamUri
        st_str, str_resp = self.onvif_soap(media_url, "http://www.onvif.org/ver10/media/wsdl/GetStreamUri",
                                           f"<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup><trt:ProfileToken>{profile_token}</trt:ProfileToken></trt:GetStreamUri>",
                                           username, password)
        if st_str < 300 and "<Fault>" not in str_resp:
            try:
                root_str = ET.fromstring(str_resp)
                u = _xml_text(root_str, "Uri")
                if u: stream_uri = u
            except Exception:
                pass

        cid = f"lan_{ip.replace('.', '_')}"
        cam = LanCamera(
            id=cid,
            ip=ip,
            name=f"{mfr} {model} ({ip})".strip(),
            port=port,
            device_service_url=device_url,
            media_service_url=media_url,
            ptz_service_url=ptz_url,
            events_service_url=events_url,
            snapshot_url=snapshot_url,
            stream_uri=stream_uri,
            username=username,
            password=password,
            profile_token=profile_token,
            ptz_configuration_token=ptz_config_token,
            manufacturer=mfr,
            model=model,
            firmware=fw,
            ptz_supported=bool(ptz_url),
            last_seen=time.time()
        )
        with self.lock:
            self.discovered_cameras[cid] = cam
        return cam

    def discover_all(self, timeout: float = 2.0) -> dict[str, Any]:
        """Runs all discovery mechanisms concurrently and interrogates any discovered ONVIF endpoints."""
        t0 = time.time()
        ws_res = self.probe_ws_discovery(timeout)
        ssdp_res = self.probe_ssdp(timeout * 0.75)
        mdns_res = self.probe_mdns(timeout * 0.75)

        discovered_ips = set()
        # Parse WS-Discovery
        for item in ws_res:
            for x in item.get("xaddrs", []):
                try:
                    u = urllib.parse.urlparse(x)
                    if u.hostname:
                        discovered_ips.add((u.hostname, u.port or 8888))
                except Exception:
                    pass

        # Parse SSDP
        for item in ssdp_res:
            loc = item.get("headers", {}).get("location", "")
            if loc:
                try:
                    u = urllib.parse.urlparse(loc)
                    if u.hostname:
                        # If port is 18766 it's a CamXposed gateway
                        if u.port == 18766:
                            with self.lock:
                                self.discovered_gateways[u.hostname] = {
                                    "host": u.hostname, "port": u.port, "location": loc, "source": "ssdp"
                                }
                        else:
                            discovered_ips.add((u.hostname, u.port or 80))
                except Exception:
                    pass

        # Parse mDNS
        for item in mdns_res:
            for addr in item.get("addresses", []):
                if ":" not in addr: # IPv4
                    port = item.get("port", 18766)
                    if port == 18766:
                        with self.lock:
                            self.discovered_gateways[addr] = {
                                "host": addr, "port": port, "properties": item.get("properties", {}), "source": "mdns"
                            }
                    else:
                        discovered_ips.add((addr, port))

        # A discovery result is a fresh snapshot, not an append-only cache.
        # Drop cameras from previous scans so DHCP changes cannot leave stale IPs selectable.
        with self.lock:
            self.discovered_cameras.clear()
        # Use explicitly configured ONVIF credentials when supplied. If the user
        # has not supplied credentials, default to the common camera login
        # admin with an empty password. Supplying only a username also keeps
        # the password empty; never invent or brute-force passwords.
        onvif_username = os.getenv("CAMXPOSED_ONVIF_USER", "admin").strip() or "admin"
        onvif_password = os.getenv("CAMXPOSED_ONVIF_PASS", "")

        # Interrogate only endpoints advertised by this live discovery pass.
        interrogated = []
        for ip, port in discovered_ips:
            try:
                cam = self.interrogate_onvif(ip, port, onvif_username, onvif_password)
                if cam:
                    interrogated.append(cam)
            except Exception as e:
                logger.warning(f"[discover_all] Interrogate failed for {ip}:{port}: {e}")

        self.last_scan_time = time.time()
        return {
            "ok": True,
            "scanDurationSec": round(time.time() - t0, 2),
            "discoveredCamerasCount": len(self.discovered_cameras),
            "cameras": [
                {
                    "id": c.id,
                    "ip": c.ip,
                    "name": c.name,
                    "port": c.port,
                    "profileToken": c.profile_token,
                    "snapshotUrl": c.snapshot_url,
                    "streamUri": c.stream_uri,
                    "ptzSupported": c.ptz_supported,
                    "manufacturer": c.manufacturer,
                    "model": c.model,
                }
                for c in self.discovered_cameras.values()
            ],
            "discoveredGateways": list(self.discovered_gateways.values()),
            "protocolsUsed": ["WS-Discovery (UDP 3702)", "UPnP SSDP (UDP 1900)", "mDNS (DNS-SD)"]
        }

    def capture_snapshot(self, cam: LanCamera) -> Optional[bytes]:
        """Captures a pristine JPEG snapshot directly from the camera using HTTP Digest auth."""
        url = cam.snapshot_url or f"http://{cam.ip}:{cam.port}/snapshot"
        try:
            # First try Digest auth
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, f"http://{cam.ip}:{cam.port}/", cam.username, cam.password)
            handler = urllib.request.HTTPDigestAuthHandler(mgr)
            opener = urllib.request.build_opener(handler)
            with opener.open(url, timeout=4.0) as r:
                data = r.read()
                if data.startswith(b'\xff\xd8'):
                    return data
        except Exception as e:
            logger.warning(f"[capture_snapshot] Direct HTTP Digest snapshot failed ({e}), trying unauthenticated...")
        
        try:
            # Fallback without auth
            with urllib.request.urlopen(url, timeout=3.0) as r:
                data = r.read()
                if data.startswith(b'\xff\xd8'):
                    return data
        except Exception as e:
            logger.warning(f"[capture_snapshot] Unauthenticated snapshot failed: {e}")

        # Fallback to ffmpeg RTSP single frame
        try:
            rtsp = cam.stream_uri
            if cam.username:
                u = urllib.parse.urlparse(rtsp)
                rtsp = f"{u.scheme}://{cam.username}:{cam.password}@{u.netloc}{u.path}"
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-rtsp_transport", "tcp",
                "-i", rtsp,
                "-frames:v", "1", "-q:v", "4",
                "-f", "image2", "pipe:1"
            ]
            p = subprocess.run(cmd, capture_output=True, timeout=5)
            if p.returncode == 0 and p.stdout.startswith(b'\xff\xd8'):
                return p.stdout
        except Exception as e:
            logger.error(f"[capture_snapshot] RTSP frame grab fallback failed: {e}")

        return None

    def ptz_move(self, cam: LanCamera, direction: str, speed: int = 8, duration_ms: int = 700) -> dict[str, Any]:
        """Performs PTZ movement directly using ONVIF ContinuousMove and Stop."""
        dir_clean = direction.strip().lower()
        endpoint = cam.ptz_service_url or f"http://{cam.ip}:{cam.port}/onvif/ptz_service"
        prof = cam.profile_token or "PROFILE_000"

        if dir_clean == "stop":
            self.stop_tracked_move(cam)
            self.record_camera_movement(cam.ip, duration_ms=0)
            body = f"<tptz:Stop><tptz:ProfileToken>{prof}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>"
            st, resp = self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/Stop", body, cam.username, cam.password)
            return {"ok": st < 300, "provider": "native_onvif", "action": "stop", "httpStatus": st}

        # Cancel any pending auto-stop for earlier movement on this camera
        self.stop_tracked_move(cam, send_hardware_stop=False)

        # Clamp duration to 100-2000 ms per CamXposed ONVIF auto-stop specification
        bounded_duration = max(100, min(2000, int(duration_ms)))
        speed_frac = max(1, min(8, int(speed))) / 8.0
        dur_s = bounded_duration / 1000.0
        timeout_str = f"PT{int(dur_s)}.{int((dur_s % 1) * 1000):03d}S"

        pan, tilt, zoom = 0.0, 0.0, 0.0
        if dir_clean == "left": pan = -speed_frac
        elif dir_clean == "right": pan = speed_frac
        elif dir_clean == "up": tilt = speed_frac
        elif dir_clean == "down": tilt = -speed_frac
        elif dir_clean == "zoom_in": zoom = speed_frac
        elif dir_clean == "zoom_out": zoom = -speed_frac

        body = (
            f"<tptz:ContinuousMove>"
            f"<tptz:ProfileToken>{prof}</tptz:ProfileToken>"
            f"<tptz:Velocity><tt:PanTilt x=\"{pan:.2f}\" y=\"{tilt:.2f}\"/><tt:Zoom x=\"{zoom:.2f}\"/></tptz:Velocity>"
            f"<tptz:Timeout>{timeout_str}</tptz:Timeout>"
            f"</tptz:ContinuousMove>"
        )
        st, resp = self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove", body, cam.username, cam.password)
        if st < 300:
            # Arm host-side independent auto-stop timer with retry to guarantee camera stops
            self._arm_auto_stop(cam, bounded_duration, speed)
            self.record_camera_movement(cam.ip, duration_ms=bounded_duration)

        return {
            "ok": st < 300,
            "provider": "native_onvif",
            "action": "continuous_move",
            "direction": dir_clean,
            "speed": speed,
            "durationMs": bounded_duration,
            "hostAutoStopScheduled": True,
            "hostAutoStopMs": bounded_duration,
            "cameraTimeoutAlsoRequested": True,
            "velocity": {"pan": pan, "tilt": tilt, "zoom": zoom},
            "httpStatus": st
        }

    def capture_audio(self, cam: LanCamera, seconds: float = 2.0) -> bytes:
        """Captures PCM 16-bit WAV audio directly from the ONVIF camera's RTSP stream."""
        rtsp = cam.stream_uri or f"rtsp://{cam.ip}:554/ch0_0.264"
        if cam.username:
            u = urllib.parse.urlparse(rtsp)
            rtsp = f"{u.scheme}://{cam.username}:{cam.password}@{u.netloc}{u.path}"
        
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-allowed_media_types", "audio",
            "-i", rtsp,
            "-vn", "-t", str(max(0.5, min(10.0, float(seconds)))),
            "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"
        ]
        p = subprocess.run(cmd, capture_output=True, timeout=int(seconds) + 5)
        if p.returncode == 0 and p.stdout.startswith(b"RIFF"):
            return p.stdout
    def ptz_capabilities(self, cam: LanCamera) -> dict[str, Any]:
        """Read advertised PTZ spaces (velocity, speed, translation, position, timeout)."""
        endpoint = cam.ptz_service_url or f"http://{cam.ip}:{cam.port}/onvif/ptz_service"
        cfg_token = cam.ptz_configuration_token or "PTZConfigurationToken"
        body = f"<tptz:GetConfigurationOptions><tptz:ConfigurationToken>{cfg_token}</tptz:ConfigurationToken></tptz:GetConfigurationOptions>"
        st, resp = self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/GetConfigurationOptions", body, cam.username, cam.password)
        if st >= 300 or "<Fault>" in resp:
            return {"ok": False, "error": f"GetConfigurationOptions HTTP {st}"}

        out = {"ok": True, "provider": "native_onvif", "configurationToken": cfg_token}
        try:
            root = ET.fromstring(resp)
            for child in root.iter():
                tag = child.tag.rsplit("}", 1)[-1]
                if tag.endswith("Space"):
                    space_info = {}
                    uri = _xml_text(child, "URI")
                    if uri: space_info["uri"] = uri
                    for sub in child:
                        subtag = sub.tag.rsplit("}", 1)[-1]
                        if subtag in ("XRange", "YRange"):
                            min_val = _xml_text(sub, "Min")
                            max_val = _xml_text(sub, "Max")
                            prefix = "x" if subtag == "XRange" else "y"
                            if min_val: space_info[f"{prefix}Min"] = float(min_val)
                            if max_val: space_info[f"{prefix}Max"] = float(max_val)
                    key = tag[0].lower() + tag[1:]
                    out[key] = space_info
                elif tag == "PTZTimeout":
                    timeout_info = {}
                    min_val = _xml_text(child, "Min")
                    max_val = _xml_text(child, "Max")
                    if min_val: timeout_info["minDuration"] = min_val
                    if max_val: timeout_info["maxDuration"] = max_val
                    out["timeout"] = timeout_info
        except Exception as e:
            logger.warning(f"[ptz_capabilities] Parsing error: {e}")
        return out

    def ptz_status(self, cam: LanCamera) -> dict[str, Any]:
        """Read current ONVIF PTZ position, move status, and UTC time."""
        endpoint = cam.ptz_service_url or f"http://{cam.ip}:{cam.port}/onvif/ptz_service"
        prof = cam.profile_token or "PROFILE_000"
        body = f"<tptz:GetStatus><tptz:ProfileToken>{prof}</tptz:ProfileToken></tptz:GetStatus>"
        st, resp = self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/GetStatus", body, cam.username, cam.password)
        if st >= 300 or "<Fault>" in resp:
            return {"ok": False, "error": f"GetStatus HTTP {st}"}

        out = {"ok": True, "provider": "native_onvif", "profileToken": prof}
        try:
            root = ET.fromstring(resp)
            for child in root.iter():
                tag = child.tag.rsplit("}", 1)[-1]
                if tag == "Position":
                    for sub in child:
                        subtag = sub.tag.rsplit("}", 1)[-1]
                        if subtag == "PanTilt":
                            out["pan"] = float(sub.attrib.get("x", 0.0))
                            out["tilt"] = float(sub.attrib.get("y", 0.0))
                        elif subtag == "Zoom":
                            out["zoom"] = float(sub.attrib.get("x", 0.0))
                elif tag == "MoveStatus":
                    pt = _xml_text(child, "PanTilt")
                    zm = _xml_text(child, "Zoom")
                    out["moveStatus"] = {"panTilt": pt or "IDLE", "zoom": zm or "IDLE"}
                elif tag == "UtcTime":
                    out["utcTime"] = (child.text or "").strip()
        except Exception as e:
            logger.warning(f"[ptz_status] Parsing error: {e}")
        return out

    def stop_tracked_move(self, cam: LanCamera, send_hardware_stop: bool = True) -> None:
        """Cancels any pending auto-stop timer and optionally sends a hardware ONVIF Stop."""
        with self.lock:
            state = self.ptz_move_states.get(cam.ip)
            if not state:                return
            state["generation"] += 1
            timer = state.get("timer")
            if timer:
                try: timer.cancel()
                except Exception: pass
                state["timer"] = None
            was_active = state.get("active", False)
            state["active"] = False

        if send_hardware_stop and was_active:
            self.record_camera_movement(cam.ip, duration_ms=0)
            try:
                endpoint = cam.ptz_service_url or f"http://{cam.ip}:{cam.port}/onvif/ptz_service"
                prof = cam.profile_token or "PROFILE_000"
                body = f"<tptz:Stop><tptz:ProfileToken>{prof}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>"
                self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/Stop", body, cam.username, cam.password)
            except Exception as e:
                logger.warning(f"[stop_tracked_move] Hardware stop failed for {cam.ip}: {e}")

    def _arm_auto_stop(self, cam: LanCamera, duration_ms: int, speed: int) -> None:
        """Schedules an independent host-side ONVIF Stop timer matching CamXposed v88."""
        with self.lock:
            state = self.ptz_move_states.setdefault(cam.ip, {"active": False, "generation": 0, "timer": None, "speed": speed})
            state["active"] = True
            state["speed"] = speed
            state["generation"] += 1
            gen = state["generation"]

            # Cancel existing timer if any
            if state["timer"]:
                try: state["timer"].cancel()
                except Exception: pass
                state["timer"] = None

            delay_s = max(0.1, duration_ms / 1000.0)
            t = threading.Timer(delay_s, self._execute_auto_stop, args=(cam, gen, 1))
            t.daemon = True
            state["timer"] = t
            t.start()
            logger.info(f"[ptz_move] Host-enforced PTZ auto-stop armed for {cam.ip} in {duration_ms}ms (gen={gen})")

    def _execute_auto_stop(self, cam: LanCamera, generation: int, attempt: int) -> None:
        """Executes the host-enforced ONVIF Stop when duration elapses, with retry on failure."""
        with self.lock:
            state = self.ptz_move_states.get(cam.ip)
            if not state or state["generation"] != generation or not state.get("active", False):
                return
            state["timer"] = None

        logger.info(f"[ptz_move] Executing host-enforced ONVIF Stop for {cam.ip} (gen={generation}, attempt={attempt})")
        endpoint = cam.ptz_service_url or f"http://{cam.ip}:{cam.port}/onvif/ptz_service"
        prof = cam.profile_token or "PROFILE_000"
        body = f"<tptz:Stop><tptz:ProfileToken>{prof}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>"
        st, _ = self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/Stop", body, cam.username, cam.password)

        with self.lock:
            state = self.ptz_move_states.get(cam.ip)
            if not state or state["generation"] != generation:
                return
            if st < 300:
                state["active"] = False
                self.record_camera_movement(cam.ip, duration_ms=0)
                logger.info(f"[ptz_move] Host-enforced ONVIF Stop succeeded for {cam.ip}")
            else:
                logger.warning(f"[ptz_move] Host-enforced ONVIF Stop failed with HTTP {st} for {cam.ip}")
                if attempt < 3:
                    # Retry in 250ms
                    retry_timer = threading.Timer(0.25, self._execute_auto_stop, args=(cam, generation, attempt + 1))
                    retry_timer.daemon = True
                    state["timer"] = retry_timer
                    retry_timer.start()

    def ptz_relative_move(self, cam: LanCamera, x: float = 0.0, y: float = 0.0, zoom: float = 0.0, speed: int = 4) -> dict[str, Any]:
        """Perform a finite ONVIF RelativeMove using normalized translation offsets (-1.0 to 1.0) and speed (1-8)."""
        # Stop any active continuous move before starting finite relative movement
        self.stop_tracked_move(cam, send_hardware_stop=True)

        endpoint = cam.ptz_service_url or f"http://{cam.ip}:{cam.port}/onvif/ptz_service"
        prof = cam.profile_token or "PROFILE_000"
        speed_frac = max(1, min(8, int(speed))) / 8.0
        
        # Clamp translation to [-1.0, 1.0]
        tx = max(-1.0, min(1.0, float(x)))
        ty = max(-1.0, min(1.0, float(y)))
        tz = max(-1.0, min(1.0, float(zoom)))

        body = (
            f"<tptz:RelativeMove>"
            f"<tptz:ProfileToken>{prof}</tptz:ProfileToken>"
            f"<tptz:Translation><tt:PanTilt x=\"{tx:.3f}\" y=\"{ty:.3f}\"/><tt:Zoom x=\"{tz:.3f}\"/></tptz:Translation>"
            f"<tptz:Speed><tt:PanTilt x=\"{speed_frac:.2f}\" y=\"{speed_frac:.2f}\"/><tt:Zoom x=\"{speed_frac:.2f}\"/></tptz:Speed>"
            f"</tptz:RelativeMove>"
        )
        st, resp = self.onvif_soap(endpoint, "http://www.onvif.org/ver20/ptz/wsdl/RelativeMove", body, cam.username, cam.password)
        if st < 300:
            self.record_camera_movement(cam.ip, duration_ms=800)
        return {
            "ok": st < 300,
            "provider": "native_onvif",
            "action": "relative_move",
            "translation": {"x": tx, "y": ty, "zoom": tz},
            "speed": speed,
            "httpStatus": st
        }

    def subscribe_events(self, cam: LanCamera, on_event: Callable[[dict[str, Any]], None]) -> "OnvifEventSubscriber":
        """Starts an asynchronous ONVIF event subscription for the camera."""
        return OnvifEventSubscriber(self, cam, on_event)


class AnykaLanSpeaker:
    """Streams G.711 A-law 8 kHz mono audio directly to Anyka camera's TCP port 28772.
    
    Matches CamXposed v92 AnykaSpeakerStream specification:
    - Target: camera_ip:28772
    - Format: G.711 A-law, 8000 Hz, 1 channel
    - Frame size: 160 bytes (20 ms)
    - Pacing: 20 ms per frame
    - Stop / Barge-in: Abortive TCP close (SO_LINGER = 1, 0 / TCP RST)
    - Backlog: Camera native server caps ADEC buffer at 500 ms (25 frames)
    """
    PORT = 28772
    FRAME_BYTES = 160
    FRAME_MS = 20

    def __init__(self, camera_ip: str, connect_timeout: float = 1.2):
        if not camera_ip or not camera_ip.strip():
            raise ValueError("Target camera IP is empty")
        self.camera_ip = camera_ip.strip()
        self.socket: Optional[socket.socket] = None
        self.stopped = threading.Event()
        self.write_attempted = False
        self.sent_bytes = 0
        self.write_count = 0

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(connect_timeout)
        try:
            sock.connect((self.camera_ip, self.PORT))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16 * 1024)
            self.socket = sock
            logger.info(f"[AnykaLanSpeaker] Connected directly to Anyka speaker TCP port {self.PORT} on {self.camera_ip}")
        except Exception as e:
            sock.close()
            raise ConnectionRefusedError(f"Failed to connect to Anyka speaker on {self.camera_ip}:{self.PORT}: {e}")

    def stream_alaw(self, alaw_bytes: bytes, playback_id: int = 0) -> bool:
        """Paces 160-byte G.711 A-law frames at 20 ms intervals to port 28772."""
        if not self.socket or self.stopped.is_set():
            return False

        sock = self.socket
        frame_len = self.FRAME_BYTES
        frame_interval = 0.020  # 20 ms

        try:
            total_len = len(alaw_bytes)
            offset = 0
            next_frame_time = time.monotonic()

            while offset < total_len and not self.stopped.is_set():
                chunk = alaw_bytes[offset:offset + frame_len]
                if len(chunk) < frame_len:
                    # Pad final sub-20ms frame with G.711 A-law silence (0xD5)
                    chunk = chunk + b"\xD5" * (frame_len - len(chunk))

                wait_s = next_frame_time - time.monotonic()
                if wait_s > 0:
                    time.sleep(wait_s)

                if self.stopped.is_set():
                    break

                self.write_attempted = True
                sock.sendall(chunk)
                self.sent_bytes += len(chunk)
                self.write_count += 1
                offset += frame_len
                next_frame_time = time.monotonic() + frame_interval

                if self.write_count == 1 or self.write_count % 50 == 0:
                    logger.debug(f"[AnykaLanSpeaker] Sent {self.write_count} frames ({self.sent_bytes} bytes) to {self.camera_ip}")

            # Graceful end
            if not self.stopped.is_set():
                logger.info(f"[AnykaLanSpeaker] Playback {playback_id} completed: sent {self.sent_bytes} bytes in {self.write_count} frames")
                self.close(graceful=True)
                return True
            return False
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning(f"[AnykaLanSpeaker] Socket write error during playback {playback_id}: {e}")
            self.abort(reason="socket_error")
            return False
        finally:
            self.close(graceful=not self.stopped.is_set())

    def abort(self, reason: str = "barge_in") -> None:
        """Instant barge-in cutoff using abortive TCP close (TCP RST) matching CamXposed v92."""
        self.stopped.set()
        sock = self.socket
        self.socket = None
        if sock:
            try:
                # SO_LINGER = struct.pack('ii', 1, 0) forces immediate TCP RST packet to be sent to camera
                import struct
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            except Exception:
                pass
            try:
                sock.close()
                logger.info(f"[AnykaLanSpeaker] Aborted playback on {self.camera_ip}:{self.PORT} (TCP RST sent, reason: {reason})")
            except Exception as e:
                logger.warning(f"[AnykaLanSpeaker] Error closing socket: {e}")

    def close(self, graceful: bool = True) -> None:
        """Closes speaker socket."""
        sock = self.socket
        self.socket = None
        if sock:
            try:
                if not graceful:
                    import struct
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                sock.close()
            except Exception:
                pass


class OnvifEventSubscriber:
    """Subscribes to an ONVIF camera's Events service via WS-PullPoint and dispatches notifications."""

    def __init__(self, engine: LanEngine, cam: LanCamera, on_event: Callable[[dict[str, Any]], None]):
        self.engine = engine
        self.cam = cam
        self.on_event = on_event
        self.running = True
        self.subscription_url = ""
        self.last_event_time = 0.0
        self.thread = threading.Thread(target=self._worker, daemon=True, name=f"onvif-events-{cam.ip}")
        self.thread.start()

    def stop(self):
        self.running = False

    def _create_subscription(self) -> bool:
        event_service = self.cam.events_service_url or f"http://{self.cam.ip}:{self.cam.port}/onvif/event_service"
        body = '''<tev:CreatePullPointSubscription xmlns:tev="http://www.onvif.org/ver10/events/wsdl">
  <tev:InitialTerminationTime>PT300S</tev:InitialTerminationTime>
</tev:CreatePullPointSubscription>'''
        try:
            st, resp = self.engine.onvif_soap(
                event_service,
                "http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionRequest",
                body,
                self.cam.username,
                self.cam.password,
                timeout=6.0
            )
            if st < 300 and "<Fault>" not in resp:
                root = ET.fromstring(resp)
                for node in root.iter():
                    if "Address" in node.tag and node.text and node.text.startswith("http"):
                        self.subscription_url = node.text.strip()
                        logger.info(f"[OnvifEvents] PullPoint subscription established for {self.cam.ip}: {self.subscription_url}")
                        return True
        except Exception as e:
            logger.warning(f"[OnvifEvents] CreatePullPointSubscription error on {self.cam.ip}: {e}")
        return False

    def _worker(self):
        pull_body = '''<tev:PullMessages xmlns:tev="http://www.onvif.org/ver10/events/wsdl">
  <tev:Timeout>PT5S</tev:Timeout>
  <tev:MessageLimit>10</tev:MessageLimit>
</tev:PullMessages>'''
        while self.running:
            # Alerts must be completely disabled while camera is active and connection is idle.
            # Alerts are only pulled and dispatched when explicitly enabled (in Standby mode).
            if not self.engine.are_alerts_enabled():
                time.sleep(0.5)
                continue

            if not self.subscription_url:
                if not self._create_subscription():
                    time.sleep(5.0)
                    continue

            try:
                st, resp = self.engine.onvif_soap(
                    self.subscription_url,
                    "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest",
                    pull_body,
                    self.cam.username,
                    self.cam.password,
                    timeout=10.0
                )
                if not self.engine.are_alerts_enabled():
                    continue

                if st >= 300 or "<Fault>" in resp:
                    logger.debug(f"[OnvifEvents] PullMessages fault/expired (HTTP {st}), renewing subscription...")
                    self.subscription_url = ""
                    time.sleep(1.0)
                    continue

                root = ET.fromstring(resp)
                for notif in root.iter():
                    if not self.engine.are_alerts_enabled():
                        break
                    if notif.tag.rsplit("}", 1)[-1] == "NotificationMessage":
                        topic = ""
                        utc_time = ""
                        data_items = {}
                        for child in notif.iter():
                            tag = child.tag.rsplit("}", 1)[-1]
                            if tag == "Topic":
                                topic = (child.text or "").strip()
                            elif tag == "Message":
                                utc_time = child.attrib.get("UtcTime", "")
                            elif tag == "SimpleItem":
                                name = child.attrib.get("Name", "")
                                val = child.attrib.get("Value", "")
                                if name:
                                    data_items[name] = val

                        is_active = False
                        for k, v in data_items.items():
                            if str(v).lower() in ("true", "1", "motion", "detected"):
                                is_active = True
                                break

                        # 1. Ignore inactive/cleared events (e.g. IsMotion: false)
                        if not LanEngine.is_active_alarm(data_items):
                            logger.debug(f"[OnvifEvents] Ignoring inactive/cleared event on {self.cam.ip}: {topic} {data_items}")
                            continue

                        # 2. Ignore motion alarms generated during camera movement or post-movement settling
                        is_motion = LanEngine.is_motion_event(topic, data_items)
                        if is_motion:
                            moving, remaining = self.engine.is_camera_moving_or_cooling(self.cam.ip, self.cam, event_utc=utc_time)
                            if moving:
                                logger.info(f"[OnvifEvents] Completely dropping and forgetting motion alarm on {self.cam.ip} during/after movement ({remaining:.1f}s cooldown remaining): {topic}")
                                continue

                        if is_active or is_motion or "Alarm" in topic or "Analytics" in topic:
                            now = time.monotonic()
                            if (now - self.last_event_time) > 2.0:
                                self.last_event_time = now
                                event_dict = {
                                    "camera_id": self.cam.id,
                                    "camera_ip": self.cam.ip,
                                    "topic": topic,
                                    "utc_time": utc_time or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "data": data_items,
                                    "is_active": is_active,
                                }
                                logger.info(f"[OnvifEvents] Event triggered on {self.cam.ip}: {topic} {data_items}")
                                try:
                                    self.on_event(event_dict)
                                except Exception as e:
                                    logger.warning(f"[OnvifEvents] on_event handler error: {e}")
            except Exception as e:
                logger.debug(f"[OnvifEvents] PullMessages exception: {e}")
                self.subscription_url = ""
                time.sleep(3.0)


lan_engine = LanEngine()