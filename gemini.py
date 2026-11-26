#!/usr/bin/env python3
"""CamXposed Gemini Live Voice Bridge & Autonomous Visual Subagent.

Uses official Google Gemini Live API (gemini-3.8-live) for full-duplex realtime voice
over WebSockets with official function calling, and Gemini Chat API (gemini-3.8-flash)
with native multimodal vision for autonomous persistent visual search.
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import getpass
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import av
import numpy as np

try:
    from google import genai
    from google.genai import types
except ImportError:
    raise ImportError(
        "google-genai package is required. Install it with: pip install google-genai"
    )

from lan_engine import lan_engine, LanCamera
from live_voice_bridge import SileroVAD
from ptz_stall_detector import PtzFeatureStallDetector, Result as PtzStallResult, Match as PtzStallMatch

class PrettyColoredFormatter(logging.Formatter):
    """Rich ANSI color formatting with badges and clean timestamps for beautiful CLI output."""

    # ANSI Codes
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Colors
    GRAY = "\033[90m"
    CYAN = "\033[36m"
    BRIGHT_CYAN = "\033[1;36m"
    GREEN = "\033[32m"
    BRIGHT_GREEN = "\033[1;32m"
    YELLOW = "\033[33m"
    BRIGHT_YELLOW = "\033[1;33m"
    RED = "\033[31m"
    BRIGHT_RED = "\033[1;31m"
    MAGENTA = "\033[35m"
    BRIGHT_MAGENTA = "\033[1;35m"
    BLUE = "\033[34m"
    BRIGHT_BLUE = "\033[1;34m"
    ORANGE = "\033[38;5;208m"

    LEVEL_BADGES = {
        logging.DEBUG: f"\033[90mDEBUG\033[0m",
        logging.INFO: f"\033[1;36mINFO\033[0m ",
        logging.WARNING: f"\033[1;33mWARN\033[0m ",
        logging.ERROR: f"\033[1;31mFAIL\033[0m",
        logging.CRITICAL: f"\033[1;41mCRIT\033[0m",
    }

    TAG_STYLES = [
        ("[Subagent]", f"\033[1;35m🤖 [Subagent]\033[0m"),
        ("[Tool Call]", f"\033[1;32m⚙️  [Tool Call]\033[0m"),
        ("[Gemini Live]", f"\033[1;34m⚡ [Gemini Live]\033[0m"),
        ("[Server Event]", f"\033[1;36m📡 [Server Event]\033[0m"),
        ("[WS-Discovery]", f"\033[38;5;208m📡 [WS-Discovery]\033[0m"),
        ("[PTZ]", f"\033[1;33m🔄 [PTZ]\033[0m"),
        ("[Mic]", f"\033[36m🎙️  [Mic]\033[0m"),
        ("[Speaker]", f"\033[32m🔊 [Speaker]\033[0m"),
        ("✓", f"\033[1;32m✓\033[0m"),
    ]

    def __init__(self, use_color: Optional[bool] = None):
        super().__init__()
        if use_color is None:
            self.use_color = sys.stdout.isatty() or os.getenv("FORCE_COLOR") == "1"
        else:
            self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        timestamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        ms = int(record.msecs)
        message = record.getMessage()

        if not self.use_color:
            return f"{timestamp}.{ms:03d} [{record.levelname}] camxposed.gemini: {message}"

        time_str = f"{self.GRAY}{timestamp}.{ms:03d}{self.RESET}"
        level_str = self.LEVEL_BADGES.get(record.levelno, record.levelname)
        mod_badge = f"{self.BOLD}{self.CYAN}gemini{self.RESET}"

        for tag, styled in self.TAG_STYLES:
            if tag in message:
                message = message.replace(tag, styled)

        return f"{time_str} {level_str} {mod_badge} ❯ {message}"


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(PrettyColoredFormatter())
logger = logging.getLogger("camxposed.gemini")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.addHandler(_handler)
logger.propagate = False

CONFIG_DIR = Path(os.path.expanduser("~/.config/camxposed-mcp"))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
KEY_FILE = CONFIG_DIR / "gemini_api_key"

GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
GEMINI_CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-3-flash-preview")
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Charon")
INACTIVITY_TIMEOUT_S = float(os.getenv("CAMXPOSED_INACTIVITY_TIMEOUT_S", "60.0"))

LIVE_SYSTEM_INSTRUCTION = """
You are the voice assistant for an ONVIF smart security camera equipped with physical motorized PTZ and two-way audio.

LANGUAGE MANDATE:
- DEFAULT LANGUAGE IS HINDI (हिन्दी).
- You MUST ALWAYS speak and respond in natural, polite, and fluent conversational Hindi with clear pronunciation.
- Only switch to English if the user explicitly addresses you in English.
- Your voice is Charon. Keep spoken answers concise, friendly, and natural.

OFFICIAL TOOL ROUTING RULES:
1. SEARCH & EXPLORATION (MANDATORY SUBAGENT):
   Whenever the user asks you to find, locate, search for, or check if ANY object, item, or person is in the room
   (e.g., "find my keys", "चश्मा कहाँ रखा है?", "कमरे में बोतल ढूँढो", "search for my bag", "look around for a phone"),
   you MUST IMMEDIATELY call the `spawn_subagent` tool.
   DO NOT attempt to search the room using small camera movements yourself.
   After calling `spawn_subagent`, briefly let the user know in Hindi that you have started searching
   (e.g., "जी, मैं कमरे में ढूँढना शुरू कर रहा हूँ"), and continue chatting normally.
   You will automatically receive an update as soon as the autonomous visual sweep completes.

2. IMMEDIATE DIRECT MOVEMENTS:
   For explicit manual movement commands requested by the user (e.g., "turn left", "थोड़ा ऊपर देखो",
   "move right", "tilt down", "नीचे करो"), call `camera_move`. Use this ONLY for direct user movement requests,
   NOT for searching.

3. DIRECT VISUAL OBSERVATION:
   If the user asks what is directly in front of the lens right now (e.g., "what do you see?", "सामने क्या दिख रहा है?"),
   call `camera_observe`.

4. TALKBACK / ANNOUNCEMENTS:
   If explicitly instructed to speak or broadcast something through the camera loudspeaker, call `camera_speak`.

5. CONVERSATION DURING BACKGROUND SEARCH:
   While the autonomous visual subagent is sweeping the room in the background, you remain completely
   available to converse with the user on any topic. When the search finishes, you will receive a search outcome
   update—announce the final result clearly to the user in Hindi.
"""

SUBAGENT_SYSTEM_INSTRUCTION = """
You are an autonomous visual search subagent controlling an ONVIF PTZ camera to locate an object in a room.
Target entity: "{target}".

CRITICAL INSTRUCTIONS:
1. FULL SPAN COVERAGE & PERSISTENCE:
   You must be extremely persistent, patient, and thorough. Do NOT give up early.
   You must exhaustively sweep the entire room: covering the complete right span all the way to the far right boundary,
   and the complete left span all the way to the far left boundary before stopping.
2. MULTIMODAL VERIFICATION:
   Carefully examine every camera snapshot provided. Look for the target object in every corner, on surfaces,
   desks, tables, shelves, chairs, and floors.
3. CLEAR CONCLUSION:
   - If the target is clearly visible: declare FOUND with exact spatial location (e.g., "FOUND: Keys located on the desk to the right").
   - If not found in current frame: continue the sweep across the remaining span.
   - ONLY after exhausting the entire left and right span without seeing the target: declare NOT FOUND.
"""


def get_gemini_api_key() -> str:
    """Retrieves Gemini API key from environment, saved config, or user prompt."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if api_key:
        return api_key

    if KEY_FILE.is_file():
        try:
            saved = KEY_FILE.read_text(encoding="utf-8").strip()
            if saved:
                return saved
        except Exception as e:
            logger.warning(f"Failed to read saved Gemini API key from {KEY_FILE}: {e}")

    if sys.stdin.isatty():
        print("\n--- Google Gemini API Key Setup ---")
        print("A Gemini API key is required for Gemini Live & Vision subagent.")
        print("Get your API key at: https://aistudio.google.com/apikey")
        try:
            entered = getpass.getpass("Enter your GEMINI_API_KEY (hidden): ").strip()
            if not entered:
                entered = input("Enter your GEMINI_API_KEY: ").strip()
        except Exception:
            entered = input("Enter your GEMINI_API_KEY: ").strip()

        if entered:
            try:
                KEY_FILE.write_text(entered, encoding="utf-8")
                KEY_FILE.chmod(0o600)
                logger.info(f"Saved Gemini API key securely to {KEY_FILE}")
            except Exception as e:
                logger.warning(f"Could not save API key to file: {e}")
            return entered

    raise RuntimeError(
        "GEMINI_API_KEY environment variable is not set and no key file was found. "
        "Set GEMINI_API_KEY or save it in ~/.config/camxposed-mcp/gemini_api_key"
    )


def select_camera_via_ws_discovery(interactive: bool = True) -> str:
    """Discovers ONVIF cameras via WS-Discovery on LAN and returns verified camera IP."""
    logger.info("Scanning LAN for verified ONVIF cameras via WS-Discovery (UDP 3702)...")
    res = lan_engine.discover_all(timeout=2.0)
    cams = [c for c in res.get("cameras", []) if isinstance(c, dict) and c.get("ip")]

    if not cams:
        env_ip = os.getenv("CAMXPOSED_CAMERA_IP", "").strip()
        if env_ip:
            logger.info(f"WS-Discovery found 0 cameras; using CAMXPOSED_CAMERA_IP: {env_ip}")
            return env_ip
        with lan_engine.lock:
            if lan_engine.discovered_cameras:
                first = next(iter(lan_engine.discovered_cameras.values()))
                return first.ip
        if interactive and sys.stdin.isatty():
            try:
                entered = input("No ONVIF cameras found via WS-Discovery. Enter camera IP: ").strip()
                if entered:
                    return entered
            except (EOFError, KeyboardInterrupt):
                pass
        raise RuntimeError("No ONVIF cameras discovered via WS-Discovery (UDP 3702) and no CAMXPOSED_CAMERA_IP specified.")

    logger.info(f"Found {len(cams)} verified ONVIF camera(s) via WS-Discovery:")
    for idx, c in enumerate(cams, 1):
        ptz = " [PTZ Supported]" if c.get("ptzSupported") else ""
        logger.info(f"  [{idx}] {c.get('name', 'Camera')} - IP: {c.get('ip')}{ptz}")

    default_cam = cams[0]
    default_ip = default_cam["ip"]

    if len(cams) == 1 or not (interactive and sys.stdin.isatty()):
        logger.info(f"Target Camera auto-selected: {default_ip}")
        os.environ["CAMXPOSED_CAMERA_IP"] = default_ip
        return default_ip

    try:
        prompt = f"\nSelect camera [1-{len(cams)}] (press Enter or wait 5s for default: 1 [ {default_ip} ]): "
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

        if not user_choice:
            chosen_ip = default_ip
        elif user_choice.isdigit() and 1 <= int(user_choice) <= len(cams):
            chosen_ip = cams[int(user_choice) - 1]["ip"]
        elif re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", user_choice):
            chosen_ip = user_choice
        else:
            chosen_ip = default_ip
    except (EOFError, KeyboardInterrupt):
        chosen_ip = default_ip

    logger.info(f"Target Camera selected: {chosen_ip}")
    os.environ["CAMXPOSED_CAMERA_IP"] = chosen_ip
    return chosen_ip


def resolve_camera(camera_ip: Optional[str] = None) -> LanCamera:
    """Finds or discovers a LanCamera instance via WS-Discovery or given IP."""
    if not camera_ip:
        camera_ip = os.getenv("CAMXPOSED_CAMERA_IP", "").strip()

    with lan_engine.lock:
        if camera_ip:
            if camera_ip in lan_engine.discovered_cameras:
                return lan_engine.discovered_cameras[camera_ip]
            for c in lan_engine.discovered_cameras.values():
                if c.ip == camera_ip or c.id == camera_ip:
                    return c
        elif lan_engine.discovered_cameras:
            return next(iter(lan_engine.discovered_cameras.values()))

    if not camera_ip:
        camera_ip = select_camera_via_ws_discovery(interactive=False)

    with lan_engine.lock:
        if camera_ip in lan_engine.discovered_cameras:
            return lan_engine.discovered_cameras[camera_ip]
        for c in lan_engine.discovered_cameras.values():
            if c.ip == camera_ip or c.id == camera_ip:
                return c

    user = os.getenv("CAMXPOSED_ONVIF_USER", "admin")
    pwd = os.getenv("CAMXPOSED_ONVIF_PASS", "")
    try:
        cam = lan_engine.interrogate_onvif(camera_ip, username=user, password=pwd)
        if cam:
            with lan_engine.lock:
                lan_engine.discovered_cameras[camera_ip] = cam
            return cam
    except Exception as e:
        logger.debug(f"ONVIF interrogation failed for {camera_ip}: {e}")

    stream_uri = f"rtsp://{camera_ip}:554/ch0_0.264"
    cam = LanCamera(
        id=f"lan_{camera_ip.replace('.', '_')}",
        ip=camera_ip,
        username=user,
        password=pwd,
        stream_uri=stream_uri,
        snapshot_url=f"http://{camera_ip}:8888/snapshot"
    )
    with lan_engine.lock:
        lan_engine.discovered_cameras[camera_ip] = cam
    return cam


def move_camera(cam: LanCamera, direction: str, duration_ms: int = 800, speed: int = 8) -> dict[str, Any]:
    return lan_engine.ptz_move(cam, direction=direction, speed=speed, duration_ms=duration_ms)


def capture_camera_snapshot(cam: LanCamera) -> Optional[bytes]:
    return lan_engine.capture_snapshot(cam)


def frames_differ(img_bytes1: Optional[bytes], img_bytes2: Optional[bytes]) -> bool:
    """Exact dual-detector feature stall tracker ported from CamXposed-master (PtzFeatureStallDetector.java).
    Uses Harris corners + 128-bit BRIEF descriptors + RANSAC translation consensus,
    falling back to 12-cell normalized cross-correlation on Sobel gradient magnitudes.
    Returns True if camera view changed (displacement >= 1.8px), False if view unchanged / mechanical stall.
    """
    if not img_bytes1 or not img_bytes2:
        return True
    try:
        diagnosis = PtzFeatureStallDetector.compare(img_bytes1, img_bytes2)
        logger.debug(f"[PtzFeatureStallDetector] {diagnosis.summary()}")
        return bool(diagnosis.view_changed)
    except Exception as e:
        logger.warning(f"[PtzFeatureStallDetector] Frame comparison error: {e}")
        return True


class GeminiCameraMicCapture:
    """Captures RTSP microphone audio, downsamples to 16kHz PCM mono, and runs Silero VAD."""

    def __init__(self, rtsp_url: str, on_speech=None, on_speech_ended=None):
        self.rtsp_url = rtsp_url
        self.on_speech = on_speech
        self.on_speech_ended = on_speech_ended
        self.running = True
        self.lock = threading.Lock()
        self.pcm_16k_queue = bytearray()
        self.vad = SileroVAD()
        self.speech_active = False
        self.last_speech_time = 0.0
        self.vad_silence_s = float(os.getenv("CAMXPOSED_VAD_SILENCE_S", "1.8"))
        self.vad_threshold = float(os.getenv("CAMXPOSED_VAD_THRESHOLD", "0.35"))
        self.reader_thread = threading.Thread(
            target=self._rtsp_worker, daemon=True, name="gemini-mic-capture"
        )
        self.reader_thread.start()

    def _rtsp_worker(self):
        logger.info(f"Connecting to RTSP mic stream: {self.rtsp_url}...")
        while self.running:
            try:
                container = av.open(self.rtsp_url, options={
                    "rtsp_transport": "tcp",
                    "allowed_media_types": "audio",
                    "buffer_size": "102400",
                    "fflags": "nobuffer",
                    "flags": "low_delay"
                })
                audio_stream = next((s for s in container.streams if s.type == "audio"), None)
                if not audio_stream:
                    logger.warning("No audio stream in RTSP; sleeping 3s...")
                    time.sleep(3)
                    continue

                resampler_state = None
                vad_buf = bytearray()
                consecutive_speech = 0

                logger.info("RTSP microphone capture active (16kHz PCM streaming).")
                for frame in container.decode(audio_stream):
                    if not self.running:
                        break
                    raw_s16 = frame.to_ndarray().tobytes()
                    if not raw_s16:
                        continue

                    # Preamp boost 2.8x
                    try:
                        raw_s16 = audioop.mul(raw_s16, 2, 2.8)
                    except Exception:
                        pass

                    # Resample 8000Hz (camera mic) -> 16000Hz (Gemini Live native input)
                    pcm_16k, resampler_state = audioop.ratecv(
                        raw_s16, 2, 1, 8000, 16000, resampler_state
                    )

                    # Feed Silero VAD (512 samples = 1024 bytes at 16kHz)
                    vad_buf.extend(pcm_16k)
                    is_speech_chunk = False
                    speech_prob = 0.0
                    while len(vad_buf) >= 1024:
                        chunk_bytes = bytes(vad_buf[:1024])
                        del vad_buf[:1024]
                        samples = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                        speech_prob = self.vad.speech_probability(samples)
                        if speech_prob >= self.vad_threshold:
                            is_speech_chunk = True

                    now = time.monotonic()
                    if is_speech_chunk:
                        consecutive_speech += 1
                        self.last_speech_time = now
                        if not self.speech_active and consecutive_speech >= 1:
                            self.speech_active = True
                            logger.info(f"[VAD] User speech started (prob={speech_prob:.2f})")
                            if self.on_speech:
                                self.on_speech(speech_prob)
                    else:
                        consecutive_speech = 0
                        if self.speech_active and (now - self.last_speech_time) > self.vad_silence_s:
                            self.speech_active = False
                            logger.info(f"[VAD] User speech turn ended ({self.vad_silence_s:.1f}s gap)")
                            if self.on_speech_ended:
                                self.on_speech_ended()

                    # Enqueue 16kHz audio for Gemini Live
                    with self.lock:
                        self.pcm_16k_queue.extend(pcm_16k)
                        max_bytes = 32000 * 4  # 4 seconds max
                        if len(self.pcm_16k_queue) > max_bytes:
                            del self.pcm_16k_queue[:(len(self.pcm_16k_queue) - 32000 * 2)]

                container.close()
            except Exception as e:
                logger.warning(f"RTSP stream error: {e}; retrying in 2s...")
                time.sleep(2)

    def read_chunk(self, chunk_bytes: int = 3200) -> Optional[bytes]:
        """Reads a chunk of 16kHz 16-bit mono PCM (default 3200 bytes = 100ms)."""
        with self.lock:
            if len(self.pcm_16k_queue) >= chunk_bytes:
                chunk = bytes(self.pcm_16k_queue[:chunk_bytes])
                del self.pcm_16k_queue[:chunk_bytes]
                return chunk
        return None

    def clear(self):
        with self.lock:
            self.pcm_16k_queue.clear()

    def close(self):
        self.running = False


class GeminiCameraSpeaker:
    """Streams 24kHz PCM from Gemini Live model to camera speaker TCP 28772 as 8kHz G.711 A-law."""

    PORT = 28772

    def __init__(self, camera_ip: str):
        self.camera_ip = camera_ip
        self.socket: Optional[socket.socket] = None
        self.running = True
        self.lock = threading.Lock()
        self.queue = bytearray()
        self.resampler_state = None
        self.is_playing = False
        self.stream_task: Optional[asyncio.Task] = None

    def _ensure_socket(self) -> bool:
        if self.socket:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect((self.camera_ip, self.PORT))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket = sock
            self.is_playing = True
            logger.info(f"Connected to camera speaker TCP {self.PORT} on {self.camera_ip}")
            return True
        except Exception as e:
            logger.warning(f"Could not connect to camera speaker TCP {self.PORT}: {e}")
            self.socket = None
            self.is_playing = False
            return False

    def stop_playback(self, reason: str = "barge_in"):
        """Instant audio cut-off and TCP socket release for user barge-in."""
        with self.lock:
            self.is_playing = False
            self.queue.clear()
            if self.socket:
                try:
                    self.socket.close()
                except Exception:
                    pass
                self.socket = None
                logger.info(f"[Barge-in] Cut off speaker output and released TCP {self.PORT} ({reason})")

    def enqueue_pcm_24k(self, pcm_24k: bytes):
        """Converts 24kHz 16-bit PCM to 8kHz A-law and queues for transmission."""
        if not pcm_24k:
            return
        try:
            pcm_8k, self.resampler_state = audioop.ratecv(
                pcm_24k, 2, 1, 24000, 8000, self.resampler_state
            )
            alaw_8k = audioop.lin2alaw(pcm_8k, 2)
            with self.lock:
                self.queue.extend(alaw_8k)
        except Exception as e:
            logger.warning(f"Audio conversion error: {e}")

    async def playback_loop(self):
        """Sends queued A-law audio in 20ms chunks (160 bytes) to maintain real-time pacing."""
        while self.running:
            chunk = None
            with self.lock:
                if len(self.queue) >= 160:
                    chunk = bytes(self.queue[:160])
                    del self.queue[:160]

            if chunk:
                if not self.socket:
                    loop = asyncio.get_running_loop()
                    connected = await loop.run_in_executor(None, self._ensure_socket)
                    if not connected:
                        await asyncio.sleep(0.05)
                        continue

                try:
                    self.socket.sendall(chunk)
                except Exception as e:
                    logger.warning(f"Speaker send error: {e}; releasing socket")
                    self.stop_playback("send_error")

                await asyncio.sleep(0.019)  # Paced for 20ms playback
            else:
                # If buffer empty and was playing, check if we should release socket
                if self.socket and not self.is_playing:
                    self.stop_playback("silence")
                await asyncio.sleep(0.02)


class GeminiSubagent:
    """Autonomous visual search subagent powered by Gemini Chat API with native vision.
    
    Persistently sweeps camera PTZ across room angles, captures snapshots, and inspects
    each frame using gemini-3.8-flash. DOES NOT expose spawn_subagent to eliminate recursion.
    """

    def __init__(self, client: genai.Client, camera_ip: str, model_name: str = GEMINI_CHAT_MODEL):
        self.client = client
        self.camera_ip = camera_ip
        self.cam = resolve_camera(camera_ip)
        self.model_name = model_name
        self._is_busy = False
        self._current_target = ""
        self._search_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def is_busy(self) -> bool:
        with self._lock:
            return self._is_busy

    def get_target(self) -> str:
        with self._lock:
            return self._current_target

    def start_search(
        self,
        target_description: str,
        on_finish: Optional[Callable[[str, str, bool], None]] = None
    ) -> bool:
        with self._lock:
            if self._is_busy:
                logger.warning(f"Subagent already busy searching for: '{self._current_target}'")
                return False
            self._is_busy = True
            self._current_target = target_description

        logger.info(f"Starting autonomous visual search subagent for: '{target_description}'")
        self._search_thread = threading.Thread(
            target=self._search_worker,
            args=(target_description, on_finish),
            daemon=True,
            name="gemini-subagent-worker"
        )
        self._search_thread.start()
        return True

    def _search_worker(
        self,
        target: str,
        on_finish: Optional[Callable[[str, str, bool], None]] = None
    ):
        found = False
        found_details = ""
        step_count = 0

        try:
            lan_engine.record_agent_working(True, "gemini_subagent")
            logger.info(f"[Subagent] Starting dynamic span search for target '{target}'...")

            def is_view_changed(before_bytes: bytes, after_bytes: bytes) -> tuple[bool, str]:
                """Checks if camera viewpoint actually moved using PtzFeatureStallDetector and pixel difference."""
                # 1. PtzFeatureStallDetector (ORB-like BRIEF + Harris + NCC fallback)
                diag = PtzFeatureStallDetector.compare(before_bytes, after_bytes)
                if diag.view_changed:
                    return True, f"ptz_feature_detector(disp={diag.displacement:.1f}px, inliers={diag.inliers})"
                
                # 2. Robust MAE difference check: large moves (>30-150px) where template search (±16px) 
                # cannot find consensus must not be falsely treated as stalled.
                try:
                    im1 = np.array(Image.open(io.BytesIO(before_bytes)).convert("L").resize((320, 240)), dtype=np.float32)
                    im2 = np.array(Image.open(io.BytesIO(after_bytes)).convert("L").resize((320, 240)), dtype=np.float32)
                    mae = float(np.mean(np.abs(im1 - im2)))
                    if mae >= 6.0:
                        return True, f"frame_diff_mae({mae:.1f} >= 6.0)"
                except Exception as ex:
                    logger.debug(f"[Subagent] Frame difference check error: {ex}")
                
                return False, f"stalled(disp={diag.displacement:.1f}px, diag={diag.summary()[:60]}...)"

            def check_frame_for_target(snapshot_bytes: bytes, context_desc: str) -> tuple[bool, str]:
                nonlocal step_count
                step_count += 1
                prompt = (
                    f"You are an autonomous persistent visual search agent searching for '{target}' in this physical room using an ONVIF PTZ camera ({context_desc}). "
                    f"Be persistent and thorough. Examine this camera snapshot very carefully. Inspect every surface, desk, table, floor, chair, shelf, and background item. "
                    f"Is '{target}' (or any clear representation/match of it) visible anywhere in this frame? "
                    f"Respond strictly in JSON format with two fields: "
                    f"\"status\": either \"FOUND\" (only if target is clearly visible) or \"NOT_FOUND\" (if target is absent), "
                    f"\"details\": \"concise description of location if found, or what is visible in frame if not found\". "
                    f"Do not guess or hallucinate."
                )
                candidate_models = ["gemini-3-flash-preview", self.model_name, "gemini-3.8-flash"]
                # De-duplicate while preserving order
                models_to_try = list(dict.fromkeys(candidate_models))

                import concurrent.futures
                for model in models_to_try:
                    try:
                        def _do_generate():
                            return self.client.models.generate_content(
                                model=model,
                                contents=[
                                    types.Part.from_bytes(data=snapshot_bytes, mime_type="image/jpeg"),
                                    prompt
                                ],
                                config=types.GenerateContentConfig(
                                    response_mime_type="application/json",
                                    temperature=0.1,
                                    max_output_tokens=256
                                )
                            )

                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(_do_generate)
                            try:
                                response = future.result(timeout=12.0)
                            except concurrent.futures.TimeoutError:
                                logger.warning(f"[Subagent] Model {model} timed out after 12s, falling back...")
                                continue

                        raw_text = (response.text or "").strip()
                        try:
                            parsed = json.loads(raw_text)
                            status = str(parsed.get("status", "")).strip().upper()
                            details = str(parsed.get("details", "")).strip()
                        except Exception:
                            upper_raw = raw_text.upper()
                            status = "NOT_FOUND" if ("NOT_FOUND" in upper_raw or "NOT FOUND" in upper_raw) else ("FOUND" if "FOUND" in upper_raw else "NOT_FOUND")
                            details = raw_text

                        logger.info(f"[Subagent] Step {step_count} ({context_desc}, {model}): status={status} - {details[:90]}...")
                        if status == "FOUND":
                            return True, details
                        return False, details
                    except Exception as e:
                        err_str = str(e)
                        if "503" in err_str or "404" in err_str or "UNAVAILABLE" in err_str or "NOT_FOUND" in err_str or "429" in err_str:
                            logger.warning(f"[Subagent] Model {model} unavailable ({err_str[:60]}), trying next candidate...")
                            continue
                        logger.warning(f"[Subagent] Error querying Gemini vision with {model}: {e}")
                return False, ""

            # 1. Check initial starting view
            initial_snap = capture_camera_snapshot(self.cam)
            if initial_snap:
                is_hit, details = check_frame_for_target(initial_snap, "initial view")
                if is_hit:
                    found = True
                    found_details = details
                    logger.info(f"[Subagent] TARGET ACQUIRED immediately! {found_details}")

            # 2. Span Direction 1: Go RIGHT until physical mechanical endstop (view truly stops changing)
            if not found:
                logger.info("[Subagent] Panning RIGHT across the room until physical endstop is reached...")
                prev_snap = initial_snap
                right_steps = 0
                right_stall_streak = 0

                while self._is_busy:
                    right_steps += 1
                    logger.info(f"[Subagent] Moving right (step {right_steps})...")
                    move_camera(self.cam, direction="right", duration_ms=1000, speed=8)
                    time.sleep(1.3)

                    curr_snap = capture_camera_snapshot(self.cam)
                    if not curr_snap:
                        continue

                    if prev_snap:
                        changed, reason = is_view_changed(prev_snap, curr_snap)
                        if not changed:
                            right_stall_streak += 1
                            logger.info(f"[Subagent] Right stall observation streak={right_stall_streak}/3 ({reason})")
                            if right_stall_streak >= 3:
                                logger.info(f"[Subagent] Right physical endstop reached after {right_steps} moves. Now reversing to explore complete opposite span to the left.")
                                break
                        else:
                            right_stall_streak = 0
                            logger.debug(f"[Subagent] Right step {right_steps} confirmed move ({reason})")

                    prev_snap = curr_snap
                    is_hit, details = check_frame_for_target(curr_snap, f"panning right step {right_steps}")
                    if is_hit:
                        found = True
                        found_details = details
                        logger.info(f"[Subagent] TARGET ACQUIRED while panning right! {found_details}")
                        break

            # 3. Span Direction 2: Reverse and pan LEFT until physical mechanical endstop on the other side
            if not found:
                logger.info("[Subagent] Reversing direction: panning LEFT across entire room span until physical endstop...")
                prev_snap = capture_camera_snapshot(self.cam)
                left_steps = 0
                left_stall_streak = 0

                while self._is_busy:
                    left_steps += 1
                    logger.info(f"[Subagent] Moving left (step {left_steps})...")
                    move_camera(self.cam, direction="left", duration_ms=1000, speed=8)
                    time.sleep(1.3)

                    curr_snap = capture_camera_snapshot(self.cam)
                    if not curr_snap:
                        continue

                    if prev_snap:
                        changed, reason = is_view_changed(prev_snap, curr_snap)
                        if not changed:
                            left_stall_streak += 1
                            logger.info(f"[Subagent] Left stall observation streak={left_stall_streak}/3 ({reason})")
                            if left_stall_streak >= 3:
                                logger.info(f"[Subagent] Left physical endstop reached after {left_steps} moves. Complete room span exhausted.")
                                break
                        else:
                            left_stall_streak = 0
                            logger.debug(f"[Subagent] Left step {left_steps} confirmed move ({reason})")

                    prev_snap = curr_snap
                    is_hit, details = check_frame_for_target(curr_snap, f"panning left step {left_steps}")
                    if is_hit:
                        found = True
                        found_details = details
                        logger.info(f"[Subagent] TARGET ACQUIRED while panning left! {found_details}")
                        break

            # Revert to clean outcome messages
            if found:
                reverted_msg = f"FOUND: '{target}' was found in the room. Details: {found_details}"
                spoken = f"{target} mil gayi! Room me target mil gaya hai."
            else:
                reverted_msg = f"NOT_FOUND: '{target}' was not found after scanning the entire physical span from end to end."
                spoken = f"Pure kamre me dono taraf dekhne par bhi {target} nahi mili."

        except Exception as e:
            logger.error(f"[Subagent] Fatal error during search sweep: {e}")
            reverted_msg = f"ERROR: Search encountered an issue: {e}"
            spoken = f"Khuj me samasya aayi: {e}"
        finally:
            with self._lock:
                self._is_busy = False
            lan_engine.record_agent_working(False, "gemini_subagent")
            logger.info(f"[Subagent] Search finished. Outcome: {reverted_msg}")
            if on_finish:
                try:
                    on_finish(reverted_msg, spoken, found)
                except Exception as e:
                    logger.warning(f"[Subagent] Error calling on_finish: {e}")


class GeminiLiveBridge:
    """Full-duplex real-time voice bridge connecting camera mic & speaker to Gemini Live API."""

    def __init__(
        self,
        camera_ip: str,
        api_key: str,
        live_model: str = GEMINI_LIVE_MODEL,
        voice_name: str = GEMINI_VOICE,
        inactivity_timeout_s: float = INACTIVITY_TIMEOUT_S
    ):
        self.camera_ip = camera_ip
        self.cam = resolve_camera(camera_ip)
        self.api_key = api_key
        self.live_model = live_model
        self.voice_name = voice_name
        self.inactivity_timeout_s = inactivity_timeout_s

        self.client = genai.Client(api_key=self.api_key)
        self.mic: Optional[GeminiCameraMicCapture] = None
        self.speaker: Optional[GeminiCameraSpeaker] = None
        self.subagent = GeminiSubagent(client=self.client, camera_ip=self.camera_ip)

        self.running = True
        self.session: Optional[Any] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.last_user_activity = time.time()
        self.is_session_active = False

    def _setup_tool_declarations(self) -> list[types.Tool]:
        """Official tool routing: declares tools for Gemini Live front-line voice agent."""
        spawn_subagent_decl = types.FunctionDeclaration(
            name="spawn_subagent",
            description="MANDATORY for all search/find tasks. Starts an autonomous persistent visual search to find, locate, or track an object or person in the room. Always use this instead of manual moves when asked to search or find.",
            behavior="NON_BLOCKING",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "search_entity_description": {
                        "type": "STRING",
                        "description": "The exact object, person, or entity to search for in the room."
                    }
                },
                "required": ["search_entity_description"]
            }
        )

        camera_move_decl = types.FunctionDeclaration(
            name="camera_move",
            description="Move or rotate the physical camera lens for small direct manual movements (left, right, up, down) when explicitly asked by user. DO NOT use this to search for objects.",
            behavior="NON_BLOCKING",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "direction": {
                        "type": "STRING",
                        "description": "Direction to move: left, right, up, down.",
                        "enum": ["left", "right", "up", "down"]
                    },
                    "duration_ms": {
                        "type": "INTEGER",
                        "description": "Duration of movement in milliseconds (default: 800 for small nudge)."
                    },
                    "speed": {
                        "type": "INTEGER",
                        "description": "Motor speed from 1 to 8 (default: 8)."
                    }
                },
                "required": ["direction"]
            }
        )

        camera_observe_decl = types.FunctionDeclaration(
            name="camera_observe",
            description="Inspect what the camera sees directly in front of the lens right now for single-glance user questions.",
            behavior="NON_BLOCKING",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "quality": {
                        "type": "INTEGER",
                        "description": "Snapshot JPEG quality (1-100, default 80)."
                    }
                }
            }
        )

        camera_speak_decl = types.FunctionDeclaration(
            name="camera_speak",
            description="Speak or announce a message directly into the room using the camera's physical loudspeaker.",
            behavior="NON_BLOCKING",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "text": {
                        "type": "STRING",
                        "description": "The text to speak through the camera speaker in Hindi or English."
                    }
                },
                "required": ["text"]
            }
        )

        return [types.Tool(function_declarations=[
            spawn_subagent_decl,
            camera_move_decl,
            camera_observe_decl,
            camera_speak_decl,
        ])]

    def _on_speech_onset(self, prob: float):
        """User started speaking on camera microphone."""
        self.last_user_activity = time.time()
        # Instant Barge-In: cut off speaker output immediately
        if self.speaker:
            self.speaker.stop_playback("user_speech_onset")

    def _on_speech_ended(self):
        """User finished an utterance."""
        self.last_user_activity = time.time()

    def _on_subagent_finish(self, reverted_msg: str, spoken: str, found: bool):
        """Callback from background subagent when room search finishes."""
        logger.info(f"Subagent finished -> Injecting outcome into Live session: {reverted_msg}")
        if self.loop and self.session and self.is_session_active:
            asyncio.run_coroutine_threadsafe(
                self._inject_subagent_update(reverted_msg, spoken),
                self.loop
            )

    async def _inject_subagent_update(self, reverted_msg: str, spoken: str):
        """Injects subagent search results directly into the live conversation turn."""
        try:
            if self.session:
                update_text = (
                    f"[SUBAGENT VISUAL SEARCH COMPLETED]: {reverted_msg}. "
                    f"Please announce this search outcome clearly to the user in 1-2 friendly Hindi sentences (e.g. '{spoken}')."
                )
                await self.session.send_realtime_input(text=update_text)
                logger.info("Successfully sent search outcome update to Gemini Live session.")
        except Exception as e:
            logger.warning(f"Failed to inject subagent result into live session: {e}")

    async def _handle_tool_call(self, call: Any) -> types.FunctionResponse:
        """Executes official tool call from Gemini Live and returns FunctionResponse."""
        name = call.name
        args = call.args or {}
        call_id = call.id
        logger.info(f"[Tool Call] {name}({args})")

        response_dict: dict[str, Any] = {"status": "ok"}

        if name == "spawn_subagent":
            target = args.get("search_entity_description", "target")
            started = self.subagent.start_search(
                target_description=target,
                on_finish=self._on_subagent_finish
            )
            if started:
                response_dict = {
                    "status": "search_started",
                    "target": target,
                    "message": f"खोज शुरू हो गई है। बैकग्राउंड एजेंट कमरे में '{target}' को ढूँढ रहा है।"
                }
            else:
                response_dict = {
                    "status": "busy",
                    "message": f"'{self.subagent.get_target()}' की खोज पहले से जारी है।"
                }

        elif name == "camera_move":
            direction = args.get("direction", "left")
            duration_ms = int(args.get("duration_ms", 800))
            speed = int(args.get("speed", 8))
            res = move_camera(
                self.cam,
                direction=direction,
                duration_ms=duration_ms,
                speed=speed
            )
            response_dict = {"status": "ok", "moved": direction, "duration_ms": duration_ms, "result": res}

        elif name == "camera_observe":
            snapshot = capture_camera_snapshot(self.cam)
            response_dict = {
                "status": "snapshot_captured",
                "bytes": len(snapshot) if snapshot else 0,
                "note": "Camera frame observed."
            }

        elif name == "camera_speak":
            text = args.get("text", "")
            response_dict = {"status": "spoken", "text": text}

        return types.FunctionResponse(id=call_id, name=name, response=response_dict)

    async def _mic_send_loop(self):
        """Continuously streams 16kHz PCM from RTSP camera mic to Gemini Live."""
        logger.info("Starting mic send loop (16kHz PCM streaming to Gemini Live)...")
        while self.running and self.is_session_active:
            chunk = self.mic.read_chunk(3200) if self.mic else None
            if chunk and self.session:
                try:
                    await self.session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                    )
                except Exception as e:
                    logger.warning(f"Error streaming mic audio to Gemini: {e}")
                    break
            else:
                await asyncio.sleep(0.02)

    async def _receive_loop(self):
        """Receives audio chunks, transcripts, tool calls, and interruption events from Gemini Live."""
        logger.info("Starting receive loop from Gemini Live...")
        while self.running and self.is_session_active:
            try:
                async for response in self.session.receive():
                    if not self.running or not self.is_session_active:
                        break

                    content = response.server_content
                    if content:
                        # 1. Barge-in / Interruption signal from server
                        if content.interrupted is True:
                            logger.info("[Server Event] Interruption detected -> cutting speaker audio.")
                            if self.speaker:
                                self.speaker.stop_playback("server_interrupted")

                        # 2. Audio chunks from model turn
                        if content.model_turn:
                            for part in content.model_turn.parts:
                                if part.inline_data and part.inline_data.data:
                                    # 24kHz PCM mono from Gemini Live
                                    if self.speaker:
                                        self.speaker.enqueue_pcm_24k(part.inline_data.data)

                    # 3. Official function calling
                    if response.tool_call:
                        responses = []
                        for call in response.tool_call.function_calls:
                            res = await self._handle_tool_call(call)
                            responses.append(res)
                        if responses and self.session:
                            await self.session.send_tool_response(function_responses=responses)

            except Exception as e:
                logger.warning(f"Receive loop error: {e}")
                break

    async def run_session(self):
        """Runs the live voice bridge session with auto-reconnect and inactivity timeout."""
        self.loop = asyncio.get_running_loop()
        self.cam = resolve_camera(self.camera_ip)
        user = self.cam.username or "admin"
        pwd = self.cam.password or ""
        rtsp_url = f"rtsp://{user}:{pwd}@{self.camera_ip}:554/ch0_0.264"

        self.mic = GeminiCameraMicCapture(
            rtsp_url=rtsp_url,
            on_speech=self._on_speech_onset,
            on_speech_ended=self._on_speech_ended
        )
        self.speaker = GeminiCameraSpeaker(camera_ip=self.camera_ip)
        asyncio.create_task(self.speaker.playback_loop())

        tools = self._setup_tool_declarations()
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=types.Content(
                parts=[types.Part(text=LIVE_SYSTEM_INSTRUCTION)]
            ),
            tools=tools,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice_name)
                )
            ) if self.voice_name else None,
        )

        logger.info(f"Connecting to Gemini Live API ({self.live_model}, voice={self.voice_name})...")
        lan_engine.set_live_session_state(True, camera_ip=self.camera_ip)

        while self.running:
            try:
                async with self.client.aio.live.connect(model=self.live_model, config=config) as session:
                    self.session = session
                    self.is_session_active = True
                    self.last_user_activity = time.time()
                    logger.info("✓ Gemini Live API connected and active! Listening for voice...")
                    mic_task = asyncio.create_task(self._mic_send_loop())
                    recv_task = asyncio.create_task(self._receive_loop())

                    # Initial greeting prompt in Hindi so the model speaks first in Hindi
                    try:
                        await session.send_realtime_input(
                            text="[SESSION_INITIALIZED] Greet the user in Hindi warmly and concisely in 1 short sentence: 'नमस्ते! मैं कैमरा असिस्टेंट हूँ, बताइए मैं आपकी क्या सहायता कर सकता हूँ?'"
                        )
                    except Exception as e:
                        logger.debug(f"Initial greeting trigger error: {e}")

                    # Monitor inactivity
                    while self.running and not recv_task.done():
                        await asyncio.sleep(2.0)
                        now = time.time()

                        # Inactivity timeout (paused if subagent is busy searching)
                        if not self.subagent.is_busy():
                            silence_elapsed = now - self.last_user_activity
                            if silence_elapsed > self.inactivity_timeout_s:
                                logger.info(
                                    f"Inactivity timeout reached ({self.inactivity_timeout_s:.0f}s silence). "
                                    "Disconnecting session to preserve API quota."
                                )
                                break

                    self.is_session_active = False
                    mic_task.cancel()
                    recv_task.cancel()

                # Standby: wait for user speech before reconnecting
                logger.info("Session in standby. Listening for user speech onset to resume...")
                while self.running and not self.mic.speech_active:
                    await asyncio.sleep(0.5)

                if self.running:
                    logger.info("User speech detected! Resuming Gemini Live session...")

            except Exception as e:
                logger.warning(f"Gemini Live session disconnected: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3.0)

        lan_engine.set_live_session_state(False)


def main():
    parser = argparse.ArgumentParser(description="CamXposed Gemini Live Voice Bridge")
    parser.add_argument("--camera-ip", default=os.getenv("CAMXPOSED_CAMERA_IP", None),
                        help="Target camera IP address (auto-discovered via WS-Discovery if omitted)")
    parser.add_argument("--api-key", default=None, help="Gemini API Key")
    parser.add_argument("--live-model", default=GEMINI_LIVE_MODEL, help="Gemini Live model name")
    parser.add_argument("--chat-model", default=GEMINI_CHAT_MODEL, help="Gemini Chat model name for subagent")
    parser.add_argument("--voice", default=GEMINI_VOICE, help="Gemini voice name (e.g. Puck, Charon, Aoede)")
    args = parser.parse_args()

    camera_ip = args.camera_ip
    if not camera_ip:
        camera_ip = select_camera_via_ws_discovery(interactive=True)

    api_key = args.api_key or get_gemini_api_key()
    bridge = GeminiLiveBridge(
        camera_ip=camera_ip,
        api_key=api_key,
        live_model=args.live_model,
        voice_name=args.voice
    )

    def handle_sig(sig, frame):
        logger.info("Shutting down Gemini Live Voice Bridge...")
        bridge.running = False

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    asyncio.run(bridge.run_session())


if __name__ == "__main__":
    main()
