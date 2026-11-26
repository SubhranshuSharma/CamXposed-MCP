#!/usr/bin/env python3
"""
live_voice_bridge.py - Real-time full-duplex bridge connecting camera hardware audio to ChatGPT Web Voice.

Architecture:
1. Camera Mic -> ChatGPT Voice:
   - Reads RTSP audio (8kHz G.711 A-law / PCM) directly from camera (e.g. 192.168.1.12:554)
   - Evaluates Silero VAD v5 locally on 16kHz audio chunks for voice onset and turn detection
   - Resamples to 48kHz mono PCM frames (AudioFrame) with strictly monotonic PTS timestamps
   - Feeds frames into aiortc AudioStreamTrack sent over WebRTC to ChatGPT Web Voice
   - Sends silence frames when inactive to maintain the RTP connection without false turns

2. ChatGPT Voice -> Camera Speaker:
   - Receives remote audio track (48kHz Opus/PCM) from ChatGPT WebRTC
   - Downsamples to 8kHz mono PCM and encodes to G.711 A-law
   - Streams 160-byte frames (20ms) over native TCP 28772 directly to Anyka camera speaker
   - Auto-releases TCP port 28772 during pauses/silence so other tools have instant access

3. Real-Time Interruption (Barge-In):
   - When the user speaks while ChatGPT is speaking:
     a) Immediately closes the camera TCP speaker socket
     b) Sends action_request: stop_speaking on the negotiated oai-events DataChannel

4. Authentication & Gateway Sync:
   - Auto-discovers active ChatGPT session token from Firefox cookies/containers
   - Authenticates with local voice gateway (http://127.0.0.1:8090) and exchanges SDP
"""
from __future__ import annotations

import asyncio
import audioop
import collections
import concurrent.futures
import datetime
import fractions
import hashlib
import json
import logging
import os
import re
import select
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Optional

from lan_engine import LanEngine, LanCamera

import av
import numpy as np
import onnxruntime as ort
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
from aiortc.mediastreams import MediaStreamError

import aioice.ice

# Monkey-patch aioice consent freshness checks (RFC 7675).
# OpenAI's Realtime WebRTC endpoints do not answer periodical STUN binding consent checks on the media port.
# Without this patch, aioice forcibly drops the session after 30s with 'Consent to send expired'.
async def _noop_query_consent(self):
    while True:
        await asyncio.sleep(3600)

aioice.ice.Connection.query_consent = _noop_query_consent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [LiveVoiceBridge] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("camxposed.live_bridge")


ACCOUNT_STATUS_CACHE = os.path.expanduser("~/.config/camxposed-mcp/account_status.json")


def load_account_statuses() -> dict:
    if os.path.isfile(ACCOUNT_STATUS_CACHE):
        try:
            with open(ACCOUNT_STATUS_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def is_account_rate_limited(status_entry: dict) -> tuple[bool, str]:
    """Checks if an account's recorded voice limit is currently active or has expired.
    Returns (is_limited, reset_display_string).
    If the reset time has arrived, returns (False, '').
    """
    if not status_entry or not status_entry.get("limited"):
        return False, ""

    updated_at = status_entry.get("updated_at", 0)
    # If limit was recorded more than 24 hours ago, treat it as expired
    if time.time() - updated_at >= 86400:
        return False, ""

    resets_at = status_entry.get("resets_at")
    if resets_at and isinstance(resets_at, (int, float)):
        if time.time() >= resets_at:
            return False, ""
        dt = datetime.datetime.fromtimestamp(resets_at)
        return True, dt.strftime("%-I:%M %p")

    reset_str = status_entry.get("reset_str", "")
    m = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)", reset_str, re.IGNORECASE)
    if m:
        h = int(m.group(1))
        minute = int(m.group(2))
        ampm = m.group(3).lower()
        if ampm == "pm" and h != 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0

        up_dt = datetime.datetime.fromtimestamp(updated_at)
        cand = up_dt.replace(hour=h, minute=minute, second=0, microsecond=0)
        if cand < up_dt:
            cand += datetime.timedelta(days=1)

        if datetime.datetime.now() >= cand:
            return False, ""
        return True, cand.strftime("%-I:%M %p")

    return True, reset_str


def save_account_status(email: str, limited: bool, reset_str: str = "", resets_at: Optional[float] = None):
    if not email:
        return
    try:
        os.makedirs(os.path.dirname(ACCOUNT_STATUS_CACHE), exist_ok=True)
        statuses = load_account_statuses()
        entry = {
            "limited": limited,
            "reset_str": reset_str,
            "updated_at": time.time()
        }
        if resets_at is not None:
            entry["resets_at"] = resets_at
        statuses[email] = entry
        with open(ACCOUNT_STATUS_CACHE, "w", encoding="utf-8") as f:
            json.dump(statuses, f, indent=2)
    except Exception as e:
        logger.debug(f"Failed to save account status: {e}")


def discover_firefox_accounts() -> list[dict]:
    """Finds all distinct ChatGPT accounts in Firefox cookies/containers.
    Returns list of dicts with email, name, session_token, access_token, origin, and status.
    Deprioritizes rate-limited accounts (putting them at the bottom with reset time)
    and prioritizes ready accounts at the top.
    """
    import glob
    accounts = []
    seen_tokens = set()
    statuses = load_account_statuses()
    profiles = glob.glob(os.path.expanduser("~/.mozilla/firefox/*.default*"))

    for prof in profiles:
        cookies_db = os.path.join(prof, "cookies.sqlite")
        if not os.path.exists(cookies_db):
            continue
        try:
            conn = sqlite3.connect(f"file:{cookies_db}?immutable=1", uri=True)
            cur = conn.cursor()
            cur.execute("""
                SELECT originAttributes, name, value 
                FROM moz_cookies 
                WHERE (host LIKE "%chatgpt%" OR host LIKE "%openai%")
                  AND name LIKE "%session-token%"
                ORDER BY originAttributes, name
            """)
            rows = cur.fetchall()
            tokens_map: dict[str, dict[str, str]] = {}
            for oa, name, val in rows:
                tokens_map.setdefault(oa, {})[name] = val

            for oa, parts in tokens_map.items():
                sorted_keys = sorted(parts.keys())
                combined = "".join(parts[k] for k in sorted_keys)
                if not combined.startswith("eyJ") or combined in seen_tokens:
                    continue
                seen_tokens.add(combined)

                email = ""
                name = ""
                access_token = None

                prof_name = os.path.basename(prof)
                cache_key = f"{prof_name}:{oa or 'default'}"
                identities = statuses.get("__identities__", {})
                cached_id = identities.get(cache_key, {})

                # Query https://chatgpt.com/api/auth/session to get verified email and fresh accessToken
                try:
                    req = urllib.request.Request("https://chatgpt.com/api/auth/session", headers={
                        "Cookie": f"__Secure-next-auth.session-token={combined}",
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0"
                    })
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        data = json.loads(resp.read().decode())
                        u = data.get("user", {})
                        email = u.get("email", "")
                        name = u.get("name", "")
                        access_token = data.get("accessToken")
                        if email:
                            # Update cached identity
                            statuses.setdefault("__identities__", {})[cache_key] = {
                                "email": email,
                                "name": name,
                                "access_token": access_token
                            }
                            try:
                                with open(ACCOUNT_STATUS_CACHE, "w", encoding="utf-8") as f:
                                    json.dump(statuses, f, indent=2)
                            except Exception:
                                pass
                except Exception as e:
                    logger.debug(f"Failed to fetch session metadata for {oa}: {e}")
                    if cached_id:
                        email = cached_id.get("email", "")
                        name = cached_id.get("name", "")
                        access_token = cached_id.get("access_token")

                if not email:
                    email = f"Account ({oa or 'default'})"

                st = statuses.get(email, {})
                is_limited, reset_str = is_account_rate_limited(st)
                if st.get("limited") and not is_limited:
                    save_account_status(email, limited=False)

                accounts.append({
                    "email": email,
                    "name": name,
                    "session_token": combined,
                    "access_token": access_token or combined,
                    "origin": oa or "default",
                    "profile": os.path.basename(prof),
                    "is_limited": is_limited,
                    "reset_str": reset_str
                })
        except Exception as e:
            logger.debug(f"Error reading Firefox cookies from {prof}: {e}")

    # Deprioritize accounts that reached voice usage limit; sort ready accounts to the top
    accounts.sort(key=lambda a: (1 if a["is_limited"] else 0, a["email"].lower()))
    return accounts


def select_chatgpt_account(accounts: Optional[list[dict]] = None, timeout: float = 5.0) -> Optional[dict]:
    """Prompts the user in CLI to choose from discovered ChatGPT accounts.
    Presents uncapped accounts first and rate-limited accounts at the bottom with reset time.
    Provides a 5s countdown defaulting to the top uncapped account.
    """
    if accounts is None:
        accounts = discover_firefox_accounts()

    if not accounts:
        print("! No ChatGPT accounts found in Firefox.", flush=True)
        return None

    print("\n--- ChatGPT Account Selection ---", flush=True)
    print(f"Discovered {len(accounts)} account(s) from Firefox & containers:", flush=True)
    for idx, acc in enumerate(accounts, 1):
        email = acc.get("email", "Unknown")
        name = acc.get("name")
        name_str = f" ({name})" if name else ""
        origin = acc.get("origin", "")
        if "userContextId=" in origin:
            cid = origin.split("userContextId=")[1].split("^")[0]
            origin_str = f" [Container: {cid}]"
        elif origin == "default":
            origin_str = " [Default Profile]"
        else:
            origin_str = f" [{origin}]"

        if acc.get("is_limited"):
            reset_info = f" - Resets at {acc['reset_str']}" if acc.get("reset_str") else ""
            status_str = f" [Status: Daily Voice Limit Reached{reset_info}]"
        else:
            status_str = " [Status: Ready]"

        print(f"  [{idx}] {email}{name_str}{origin_str}{status_str}", flush=True)

    default_acc = accounts[0]
    default_email = default_acc.get("email", "")

    if len(accounts) == 1:
        print(f"Auto-selected account: [{1}] {default_email}\n", flush=True)
        return default_acc

    user_choice = ""
    if sys.stdin.isatty():
        try:
            prompt = f"\nSelect ChatGPT account [1-{len(accounts)}] (press Enter or wait {int(timeout)}s for default: 1 [ {default_email} ]): "
            user_choice = None

            def _get_input():
                nonlocal user_choice
                try:
                    user_choice = input(prompt).strip()
                except (EOFError, KeyboardInterrupt):
                    user_choice = ""

            t = threading.Thread(target=_get_input, daemon=True)
            t.start()
            t.join(timeout=timeout)

            if user_choice is None:
                sys.stdout.write(f"\n[Timeout {int(timeout)}s] Auto-selecting top account [{default_email}]\n")
                sys.stdout.flush()
                user_choice = ""
        except (EOFError, KeyboardInterrupt):
            print(f"\nUsing top account [{default_email}]", flush=True)
            user_choice = ""
    else:
        # Non-tty / piped input check (immediate, non-blocking)
        try:
            rlist, _, _ = select.select([sys.stdin], [], [], 0.0)
            if rlist:
                user_choice = sys.stdin.readline().strip()
        except Exception:
            pass
        if not user_choice:
            print(f"Auto-selected top account: [{default_email}]", flush=True)

    if not user_choice:
        chosen = default_acc
    elif user_choice.isdigit() and 1 <= int(user_choice) <= len(accounts):
        chosen = accounts[int(user_choice) - 1]
    else:
        matched = next((a for a in accounts if a.get("email", "").lower() == user_choice.lower()), None)
        if matched:
            chosen = matched
        else:
            print(f"Invalid input '{user_choice}'; using top account [{default_email}]", flush=True)
            chosen = default_acc

    print(f"✓ Target ChatGPT Account: {chosen.get('email')}\n", flush=True)
    return chosen


def get_token_from_firefox() -> Optional[str]:
    """Finds top available ChatGPT token from local Firefox cookies."""
    accs = discover_firefox_accounts()
    if accs:
        return accs[0]["access_token"]
    return None


MODEL_URL = "https://huggingface.co/runanywhere/silero-vad-v5/resolve/main/silero_vad.onnx"
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
CACHE_DIR = os.path.expanduser("~/.cache/camxposed")
MODEL_PATH = os.path.join(CACHE_DIR, "silero_vad_v5.onnx")


class SileroVAD:
    """Silero VAD v5 local ONNX model inference for precise camera mic voice activity detection."""

    def __init__(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        if not os.path.isfile(MODEL_PATH) or self._sha256(MODEL_PATH) != MODEL_SHA256:
            logger.info("Downloading Silero VAD v5 ONNX model from Hugging Face...")
            urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
            digest = self._sha256(MODEL_PATH)
            if digest != MODEL_SHA256:
                raise ValueError(f"Silero VAD SHA256 mismatch for {MODEL_PATH}: {digest}")
            logger.info("Silero VAD v5 model verified and cached.")

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(MODEL_PATH, sess_options=opts, providers=["CPUExecutionProvider"])
        self.reset()
        logger.info("Silero VAD v5 initialized successfully (sampleRate=16000, frameSamples=512, CPU).")

    def _sha256(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    def reset(self):
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros(64, dtype=np.float32)
        self.sr = np.array(16000, dtype=np.int64)

    def speech_probability(self, chunk_f32: np.ndarray) -> float:
        """Evaluates speech probability on 512 samples of 16kHz float32 audio [-1.0, 1.0]."""
        inp = np.concatenate([self.context, chunk_f32])[np.newaxis, :]
        outputs = self.session.run(None, {"input": inp, "state": self.state, "sr": self.sr})
        prob = float(outputs[0][0][0])
        self.state = outputs[1]
        self.context = inp[0, -64:]
        return prob


class CameraMicCapture:
    """Continuously captures camera RTSP microphone audio, runs Silero VAD,
    and maintains a thread-safe circular 48kHz audio buffer.
    Persists across sessions so VAD can continuously listen and wake up ChatGPT from Standby.
    """

    def __init__(self, rtsp_url: str, on_speech=None):
        self.rtsp_url = rtsp_url
        self.on_speech = on_speech
        self.running = True
        self.lock = threading.Lock()
        self.mic_pcm_48k = bytearray()
        self.vad = SileroVAD()
        self.speech_active = False
        self.last_speech_time = 0.0
        self.vad_silence_s = float(os.getenv("CAMXPOSED_VAD_SILENCE_S", "1.8"))
        self.vad_prob_threshold = float(os.getenv("CAMXPOSED_VAD_THRESHOLD", "0.35"))
        self.reader_thread = threading.Thread(target=self._rtsp_worker, daemon=True, name="camera-mic-capture")
        self.reader_thread.start()

    def _rtsp_worker(self):
        logger.info(f"Connecting to persistent camera RTSP mic stream: {self.rtsp_url}...")
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
                    logger.error("No audio stream found in RTSP stream! Sleeping 3s...")
                    time.sleep(3)
                    continue

                pcm_16k_buf = bytearray()
                resampler_state_16k = None
                resampler_state_48k = None
                consecutive_speech = 0

                logger.info("Persistent RTSP microphone capture active (continuous Silero VAD monitoring).")

                for frame in container.decode(audio_stream):
                    if not self.running:
                        break

                    raw_s16 = frame.to_ndarray().tobytes()
                    if not raw_s16:
                        continue

                    # Preamp boost 2.8x for camera mic hardware
                    try:
                        raw_s16 = audioop.mul(raw_s16, 2, 2.8)
                    except Exception:
                        pass

                    # 1. Resample 8000Hz -> 48000Hz for WebRTC Opus
                    raw_48k, resampler_state_48k = audioop.ratecv(raw_s16, 2, 1, 8000, 48000, resampler_state_48k)

                    # 2. Resample 8000Hz -> 16000Hz for Silero VAD
                    pcm_16k, resampler_state_16k = audioop.ratecv(raw_s16, 2, 1, 8000, 16000, resampler_state_16k)
                    pcm_16k_buf.extend(pcm_16k)

                    # 3. Evaluate Silero VAD on 512-sample chunks (32ms = 1024 bytes)
                    is_speech_chunk = False
                    speech_prob = 0.0
                    while len(pcm_16k_buf) >= 1024:
                        chunk_bytes = bytes(pcm_16k_buf[:1024])
                        del pcm_16k_buf[:1024]
                        samples = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                        speech_prob = self.vad.speech_probability(samples)
                        if speech_prob >= self.vad_prob_threshold:
                            is_speech_chunk = True

                    now = time.monotonic()
                    if is_speech_chunk:
                        consecutive_speech += 1
                        self.last_speech_time = now
                        if not self.speech_active and consecutive_speech >= 1:
                            self.speech_active = True
                            logger.info(f"[Silero VAD] Speech onset detected! (prob={speech_prob:.2f})")
                            if self.on_speech:
                                self.on_speech(speech_prob)
                    else:
                        consecutive_speech = 0
                        if self.speech_active and (now - self.last_speech_time) > self.vad_silence_s:
                            self.speech_active = False
                            logger.info(f"[Silero VAD] Speech turn ended ({self.vad_silence_s:.1f}s silence gap).")

                    # 4. Enqueue 48kHz audio into real-time buffer
                    with self.lock:
                        self.mic_pcm_48k.extend(raw_48k)
                        # Keep a healthy jitter buffer (up to ~800ms) to prevent audio chopping / frame drops.
                        # Only trim if consumer falls behind (> 1000ms), safely retaining latest 240ms.
                        max_buf_bytes = 1920 * 50  # 1000ms
                        if len(self.mic_pcm_48k) > max_buf_bytes:
                            trim_target = 1920 * 12  # 240ms
                            del self.mic_pcm_48k[:(len(self.mic_pcm_48k) - trim_target)]

                container.close()
            except Exception as e:
                logger.warning(f"RTSP stream error: {e}. Reconnecting in 2s...")
                time.sleep(2)

    def read_chunk_48k(self, frame_bytes: int) -> Optional[bytes]:
        with self.lock:
            if len(self.mic_pcm_48k) >= frame_bytes:
                chunk = bytes(self.mic_pcm_48k[:frame_bytes])
                del self.mic_pcm_48k[:frame_bytes]
                return chunk
        return None

    def clear_buffer(self):
        with self.lock:
            self.mic_pcm_48k.clear()

    def close(self):
        self.running = False


class CameraMicTrack(AudioStreamTrack):
    """Feeds camera audio from persistent CameraMicCapture into a WebRTC peer connection."""
    kind = "audio"

    def __init__(self, capture: CameraMicCapture):
        super().__init__()
        self.capture = capture
        self.running = True
        self._start_time: Optional[float] = None
        self._timestamp: int = 0
        self._last_speech_log = 0.0
        self._last_idle_log = 0.0

    async def recv(self) -> av.AudioFrame:
        if not self.running:
            raise MediaStreamError

        samples = 960  # 20ms of 48000Hz
        sample_rate = 48000
        frame_bytes = samples * 2  # 1920 bytes (s16 mono)

        now = time.monotonic()
        if self._start_time is None:
            self._start_time = now
            self._timestamp = 0
            self._last_speech_log = 0.0
            self._last_idle_log = 0.0
        else:
            self._timestamp += samples
            expected_time = self._start_time + (self._timestamp / sample_rate)
            wait = expected_time - now
            if wait > 0:
                await asyncio.sleep(wait)
            elif wait < -0.1:
                self._start_time = now - (self._timestamp / sample_rate)

        chunk = self.capture.read_chunk_48k(frame_bytes)
        if not chunk:
            chunk = b"\x00" * frame_bytes

        # Telemetry logging
        rms = audioop.rms(chunk, 2)
        if self.capture.speech_active:
            if now - self._last_speech_log >= 1.0:
                self._last_speech_log = now
                logger.info(f"[Camera Mic -> WebRTC] Live speech streaming (RMS={rms})")
        else:
            if now - self._last_idle_log >= 10.0:
                self._last_idle_log = now
                logger.debug(f"[Camera Mic -> WebRTC] Ambient room floor (RMS={rms})")

        data = np.frombuffer(chunk, dtype=np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(data, format="s16", layout="mono")
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, sample_rate)
        frame.sample_rate = sample_rate
        return frame

    def stop(self):
        super().stop()
        self.running = False


class CameraSpeakerStreamer:
    """Consumes 48kHz WebRTC audio track from ChatGPT and streams 8kHz G.711 A-law to camera TCP 28772.
    
    Includes Voice Activity Detection (VAD) and auto-releases TCP port 28772 during silence
    so that discrete camera_speak announcements and talkback tools have free access to the hardware.
    """

    PORT = 28772
    FRAME_BYTES = 160  # 20ms of 8000Hz A-law
    SILENCE_THRESHOLD = 30  # RMS threshold for active speech
    IDLE_RELEASE_SEC = 0.5  # Release TCP socket after 500ms of silence

    def __init__(self, camera_ip: str, on_speech_active=None):
        self.camera_ip = camera_ip
        self.on_speech_active = on_speech_active
        self.socket: Optional[socket.socket] = None
        self.running = True
        self.pcm_buffer = bytearray()
        self.resampler_state = None
        self.last_active_time = 0.0
        self.next_frame_time = 0.0
        self.is_playing = False

    async def _ensure_socket_async(self) -> bool:
        if self.socket:
            return True
        try:
            loop = asyncio.get_running_loop()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            await loop.run_in_executor(None, sock.connect, (self.camera_ip, self.PORT))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket = sock
            self.next_frame_time = time.monotonic()
            self.is_playing = True
            logger.info(f"Connected to camera speaker TCP port {self.PORT} on {self.camera_ip}")
            return True
        except Exception as e:
            logger.warning(f"Could not connect to camera speaker port {self.PORT}: {e}")
            self.socket = None
            self.is_playing = False
            return False

    def _release_socket(self, reason: str = "idle"):
        self.is_playing = False
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None
            self.pcm_buffer.clear()
            logger.info(f"Released camera speaker TCP port {self.PORT} ({reason})")

    async def consume_track(self, track: AudioStreamTrack):
        logger.info("Beginning to receive ChatGPT audio track -> streaming to camera speaker...")
        while self.running:
            try:
                frame = await track.recv()
                raw_bytes = frame.to_ndarray().tobytes()
                
                in_rate = frame.sample_rate or 48000
                channels = len(frame.layout.channels) if hasattr(frame, "layout") and frame.layout else 1
                
                pcm_mono = raw_bytes
                if channels > 1:
                    pcm_mono = audioop.tomono(raw_bytes, 2, 0.5, 0.5)

                rms = audioop.rms(pcm_mono, 2)
                now = time.monotonic()

                if not hasattr(self, "_last_recv_log") or (now - self._last_recv_log) >= 2.0:
                    self._last_recv_log = now
                    logger.debug(f"[ChatGPT Audio Track] Recv frame: in_rate={in_rate}, channels={channels}, RMS={rms}")

                if rms > self.SILENCE_THRESHOLD:
                    if not self.is_playing:
                        logger.info(f"[ChatGPT Audio Track] Active speech received from ChatGPT (RMS={rms}) -> streaming to speaker")
                    self.last_active_time = now
                    if self.on_speech_active:
                        self.on_speech_active()
                    pcm_8k, self.resampler_state = audioop.ratecv(pcm_mono, 2, 1, in_rate, 8000, self.resampler_state)
                    alaw_chunk = audioop.lin2alaw(pcm_8k, 2)
                    self.pcm_buffer.extend(alaw_chunk)

                    # Cap buffer to 500ms max to prevent latency buildup
                    if len(self.pcm_buffer) > self.FRAME_BYTES * 25:
                        del self.pcm_buffer[:-self.FRAME_BYTES * 25]

                    # Pace and send 160-byte packets (20ms each)
                    while len(self.pcm_buffer) >= self.FRAME_BYTES and self.running:
                        packet = bytes(self.pcm_buffer[:self.FRAME_BYTES])
                        del self.pcm_buffer[:self.FRAME_BYTES]
                        if await self._ensure_socket_async():
                            wait_s = self.next_frame_time - time.monotonic()
                            if wait_s > 0:
                                await asyncio.sleep(wait_s)
                            try:
                                sock = self.socket
                                if sock:
                                    sock.sendall(packet)
                                    self.next_frame_time = max(time.monotonic(), self.next_frame_time + 0.020)
                            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                                logger.warning(f"Speaker socket write error: {e}")
                                self._release_socket(reason="write_error")
                                break
                else:
                    # If remaining packets in buffer, drain them
                    while len(self.pcm_buffer) >= self.FRAME_BYTES and self.socket and self.running:
                        packet = bytes(self.pcm_buffer[:self.FRAME_BYTES])
                        del self.pcm_buffer[:self.FRAME_BYTES]
                        wait_s = self.next_frame_time - time.monotonic()
                        if wait_s > 0:
                            await asyncio.sleep(wait_s)
                        try:
                            sock = self.socket
                            if sock:
                                sock.sendall(packet)
                                self.next_frame_time = max(time.monotonic(), self.next_frame_time + 0.020)
                        except Exception:
                            self._release_socket(reason="drain_error")
                            break

                    # Inactive/silent frame: if idle for > IDLE_RELEASE_SEC, release TCP port
                    if self.socket and (now - self.last_active_time) > self.IDLE_RELEASE_SEC:
                        self._release_socket(reason="silence_timeout")

            except Exception as e:
                if "MediaStreamError" in type(e).__name__ or not self.running:
                    logger.info("ChatGPT audio track finished or closed.")
                    break
                logger.warning(f"Error processing ChatGPT audio frame: {e}")
                await asyncio.sleep(0.01)

        self._release_socket(reason="track_ended")

    def abort(self, reason: str = "barge_in"):
        self._release_socket(reason=reason)

    def close(self):
        self.running = False
        self._release_socket(reason="close")


def get_active_voice_session_ids() -> tuple[str, str, str]:
    """Returns (voice_session_id, upstream_conversation_id, upstream_parent_message_id) from voice.db."""
    custom_db = os.getenv("VOICE_DB_PATH", "").strip()
    db_paths = [
        custom_db,
        os.path.expanduser("~/chatgpt-web-voice/data/voice.db"),
        os.path.expanduser("~/Documents/chatgpt-web-voice/data/voice.db"),
        os.path.expanduser("~/Documents/antigravity/lively-bardeen/chatgpt-web-voice/data/voice.db"),
        os.path.abspath("./data/voice.db"),
    ]
    for p in db_paths:
        if p and os.path.exists(p):
            try:
                import sqlite3
                conn = sqlite3.connect(p, timeout=2.0)
                cur = conn.cursor()
                cur.execute(
                    "SELECT voice_session_id, upstream_conversation_id, upstream_parent_message_id "
                    "FROM call_sessions WHERE status = 'active' ORDER BY updated_at DESC LIMIT 1;"
                )
                row = cur.fetchone()
                conn.close()
                if row:
                    return str(row[0] or ""), str(row[1] or ""), str(row[2] or "")
            except Exception:
                pass
    return "", "", ""


def _clean_subagent_conclusion(text: str) -> str:
    """Strips XML/HTML tags and extraneous formatting, returning a clean concise summary."""
    if not text:
        return ""
    # Strip XML/HTML tags like <AsyncImage .../>, <badge...>...</badge>
    t = re.sub(r"<[^>]+>", " ", text)
    # Strip markdown code blocks
    t = re.sub(r"```[\s\S]*?```", " ", t)
    # Strip markdown bold/italic asterisks
    t = re.sub(r"\*{1,3}", "", t)
    lines = []
    for line in t.splitlines():
        line = line.strip().lstrip("-*• ")
        if not line:
            continue
        # Drop repetitive status lines already communicated by the system event
        if any(line.lower().startswith(h) for h in ["result:", "camera status:", "status:", "framing:", "camera:"]):
            continue
        lines.append(line)
    cleaned = " ".join(lines)
    cleaned = re.sub(r"^\s*FOUND\s*—\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 220:
        cleaned = cleaned[:217] + "..."
    return cleaned


class LiveVoiceBridge:
    """Manages the full-duplex WebRTC connection between local camera hardware and ChatGPT Web Voice."""

    def __init__(self, camera_ip: str, gateway_url: str = "http://127.0.0.1:8090", voice: str = "cove", token: Optional[str] = None, account_email: Optional[str] = None):
        self.camera_ip = camera_ip.strip()
        self.gateway_url = gateway_url.rstrip("/")
        self.voice = voice
        self.token = token
        self.account_email = account_email
        self.account_id = 1
        self.running = False
        self.pc: Optional[RTCPeerConnection] = None
        self.dc: Optional[object] = None
        self.mic_track: Optional[CameraMicTrack] = None
        self.speaker: Optional[CameraSpeakerStreamer] = None
        self.chatgpt_state = "listening"
        self.session_cookie = ""
        self.csrf_token = ""
        self.voice_session_id = ""
        self.upstream_conversation_id = ""
        self.upstream_parent_message_id = ""
        self.upstream_voice_session_id = ""
        self.last_subagent_event_time = time.time()
        self.engine = LanEngine()
        self.event_subscriber: Optional[object] = None
        self.motion_active = False
        self.last_motion_alert_time = 0.0
        self.last_critical_alert_time = 0.0
        self.session_active = False
        self.standby_mode = False
        self.last_talk_time = time.monotonic()
        self.last_vad_speech_time = time.monotonic()
        self.last_agent_busy_time = 0.0
        self.last_agent_done_time = time.monotonic()
        self.last_agent_stream_time = 0.0
        self.wake_event: Optional[asyncio.Event] = None
        self.wake_reason: str = ""
        self.wake_detail: str = ""
        self.pending_wake_alert: Optional[dict] = None
        self.mic_capture: Optional[CameraMicCapture] = None
        self.disconnect_due_to_inactivity = False

    def mark_talk_activity(self, source: str = "general"):
        """Marks speech activity to reset talk activity timers."""
        now = time.monotonic()
        self.last_talk_time = now
        if source == "vad":
            self.last_vad_speech_time = now
        elif source in ("agent", "speaker", "delta"):
            self.last_agent_busy_time = now
            self.last_agent_stream_time = now

    def is_agent_turn(self) -> tuple[bool, str]:
        """Returns (True, reason) if it is currently the AI agent's turn.
        While it is the agent's turn (thinking, generating, speaking, executing MCP tools):
        All that time DOES NOT count towards the inactivity timeout.
        """
        now = time.monotonic()
        # 1. ChatGPT state indicates agent is active
        # Realtime states: "speaking", "answering", "generating", "responding", "thinking"
        if self.chatgpt_state in ("speaking", "answering", "generating", "responding", "thinking"):
            return True, f"chatgpt_state={self.chatgpt_state}"

        # 2. Speaker is actively playing or recently played audio (< 1.5s)
        if self.speaker and (self.speaker.is_playing or (now - getattr(self.speaker, "last_active_time", 0.0) < 1.5)):
            return True, "speaker_streaming_audio"

        # 3. Agent recently sent spoken audio or deltas (< 2.0s)
        if (now - getattr(self, "last_agent_stream_time", 0.0)) < 2.0:
            return True, "recent_spoken_delta"

        # 4. Agent MCP tool is actively executing (e.g. camera_move, camera_observe)
        if self.engine and self.engine.is_agent_working():
            return True, "mcp_tool_executing"

        # 5. Background visual search subagent is actively exploring the room
        try:
            from subagent_engine import subagent_manager
            if subagent_manager.is_busy():
                return True, "subagent_searching"
        except Exception:
            pass

        # 6. MCP tool recently completed (< 2.5s settling)
        if self.engine and (time.time() - self.engine.get_last_agent_done_time()) < 2.5:
            return True, "mcp_tool_settling"

        return False, "idle"

    def start_event_subscription(self):
        """Starts ONVIF event subscriber. Only called when in Standby mode."""
        if self.event_subscriber:
            return
        try:
            if not getattr(self, "cam", None):
                self.cam = self.engine.interrogate_onvif(self.camera_ip, 8888)
            if self.cam and self.cam.events_service_url:
                self.event_subscriber = self.engine.subscribe_events(self.cam, self.on_camera_event)
                logger.info(f"[LiveVoiceBridge] Started ONVIF alert listener for {self.camera_ip} (Standby mode armed)")
        except Exception as e:
            logger.warning(f"Could not start ONVIF event subscriber: {e}")

    def stop_event_subscription(self):
        """Stops ONVIF event subscriber completely. Alerts disabled."""
        if self.event_subscriber:
            try:
                self.event_subscriber.stop()
            except Exception:
                pass
            self.event_subscriber = None
            logger.info(f"[LiveVoiceBridge] Stopped ONVIF alert listener. Alerts are disabled.")

    def on_speech_detected(self, prob: float):
        """Callback from persistent CameraMicCapture when Silero VAD evaluates speech."""
        now = time.monotonic()
        if self.session_active:
            self.mark_talk_activity(source="vad")
            self.on_barge_in()
        elif self.standby_mode:
            logger.info(f"[VAD Wake-up] Speech detected on camera mic (prob={prob:.2f}) while in Standby! Starting fresh session...")
            self.trigger_wake_up(reason="vad", detail=f"User voice detected on camera mic (prob={prob:.2f})")

    def trigger_wake_up(self, reason: str, detail: str, alert_event: Optional[dict] = None):
        """Wakes up the bridge from Standby mode to initiate a fresh ChatGPT session."""
        if not self.standby_mode or self.session_active:
            return
        self.wake_reason = reason
        self.wake_detail = detail
        self.pending_wake_alert = None  # Do NOT inject alert notice into fresh conversation!
        self.standby_mode = False
        # Disarm and stop alerts immediately on wake-up
        self.stop_event_subscription()
        self.engine.set_live_session_state(is_active=True, alerts_enabled=False, camera_ip=self.camera_ip)
        if hasattr(self, "loop") and self.loop and self.loop.is_running() and self.wake_event:
            self.loop.call_soon_threadsafe(self.wake_event.set)
        elif self.wake_event:
            self.wake_event.set()

    def login_gateway(self):
        """Authenticates with local Go gateway."""
        gw_user = os.getenv("GATEWAY_USER", "admin")
        gw_pass = os.getenv("GATEWAY_PASSWORD", "AdminPassword2026!")
        data = json.dumps({"username": gw_user, "password": gw_pass}).encode("utf-8")
        login_url = f"{self.gateway_url}/api/auth/login"
        req = urllib.request.Request(login_url, data=data, headers={"Content-Type": "application/json"})
        cookie_header = []
        with urllib.request.urlopen(req) as resp:
            for k, v in resp.headers.items():
                if k.lower() == "set-cookie":
                    cookie_header.append(v.split(";")[0])
                    if "voice_gateway_csrf=" in v:
                        self.csrf_token = v.split("voice_gateway_csrf=")[1].split(";")[0]
        self.session_cookie = "; ".join(cookie_header)
        logger.info(f"Authenticated with local voice gateway at {self.gateway_url}")

    def sync_token_if_needed(self):
        """Ensures the gateway has the selected/active ChatGPT session token loaded."""
        token_to_use = self.token
        email_to_use = self.account_email

        if not token_to_use:
            chosen = select_chatgpt_account()
            if chosen:
                token_to_use = chosen.get("access_token")
                email_to_use = chosen.get("email")
                self.token = token_to_use
                self.account_email = email_to_use

        if not token_to_use:
            logger.info("No token override provided; using existing accounts from voice.db.")
            return

        # Query gateway accounts
        req = urllib.request.Request(f"{self.gateway_url}/api/accounts", headers={"Cookie": self.session_cookie})
        existing_accounts = []
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                existing_accounts = data.get("accounts", [])
        except Exception as e:
            logger.debug(f"Could not query gateway accounts: {e}")

        # Check if email_to_use matches any existing account
        matched = None
        if email_to_use:
            matched = next((a for a in existing_accounts if a.get("email", "").lower() == email_to_use.lower()), None)
        elif existing_accounts:
            matched = existing_accounts[0]

        if matched:
            self.account_id = matched["id"]
            if not self.account_email:
                self.account_email = matched.get("email", "")
            logger.info(f"Targeting gateway account ID {self.account_id} ({self.account_email})")
            try:
                put_payload = json.dumps({
                    "email": self.account_email,
                    "access_token": token_to_use,
                    "disabled": False
                }).encode("utf-8")
                put_req = urllib.request.Request(
                    f"{self.gateway_url}/api/accounts/{self.account_id}",
                    data=put_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Cookie": self.session_cookie,
                        "X-CSRF-Token": self.csrf_token
                    },
                    method="PUT"
                )
                with urllib.request.urlopen(put_req):
                    logger.info(f"Updated gateway account ID {self.account_id} with fresh token.")
            except Exception as e:
                logger.debug(f"Could not update account {self.account_id}: {e}")
        else:
            logger.info(f"Adding ChatGPT account ({email_to_use or 'chatgpt_user@camxposed.lan'}) to voice gateway...")
            payload = json.dumps({
                "email": email_to_use or "chatgpt_user@camxposed.lan",
                "access_token": token_to_use
            }).encode("utf-8")
            post_req = urllib.request.Request(f"{self.gateway_url}/api/accounts", data=payload, headers={
                "Content-Type": "application/json",
                "Cookie": self.session_cookie,
                "X-CSRF-Token": self.csrf_token
            })
            try:
                with urllib.request.urlopen(post_req) as resp:
                    resp_data = json.loads(resp.read().decode())
                    acc = resp_data.get("account", {})
                    self.account_id = acc.get("id", 1)
                    if not self.account_email:
                        self.account_email = acc.get("email", email_to_use or "")
                    logger.info(f"ChatGPT account synced to gateway with ID {self.account_id}")
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    logger.info("Account already exists on voice gateway.")
                else:
                    raise

    def update_gateway_context(self, conversation_id: str = "", parent_message_id: str = "", voice_session_id: str = ""):
        """Pushes active conversation IDs to the voice gateway and local conversation row to prevent session reaping."""
        if not self.voice_session_id:
            return
        cid = conversation_id or self.upstream_conversation_id
        pid = parent_message_id or self.upstream_parent_message_id
        uvs = voice_session_id or self.upstream_voice_session_id

        # 1. Ensure local conversation row exists in database so sessions can resume seamlessly
        if not hasattr(self, "local_conversation_id") or not self.local_conversation_id:
            try:
                list_req = urllib.request.Request(f"{self.gateway_url}/api/conversations", headers={"Cookie": self.session_cookie})
                with urllib.request.urlopen(list_req) as resp:
                    convs = json.loads(resp.read().decode()).get("conversations", [])
                    matched = next((c for c in convs if cid and c.get("upstream_conversation_id") == cid and c.get("account_id") == getattr(self, "account_id", 1)), None)
                    if matched:
                        self.local_conversation_id = matched["id"]
                    else:
                        create_payload = json.dumps({"title": "CamXposed Live Voice"}).encode("utf-8")
                        create_req = urllib.request.Request(f"{self.gateway_url}/api/conversations", data=create_payload, headers={
                            "Content-Type": "application/json",
                            "Cookie": self.session_cookie,
                            "X-CSRF-Token": self.csrf_token
                        })
                        with urllib.request.urlopen(create_req) as c_resp:
                            self.local_conversation_id = json.loads(c_resp.read().decode())["conversation"]["id"]
            except Exception as e:
                logger.debug(f"Failed to ensure local conversation row: {e}")

        # 2. Patch local conversation with continuity metadata
        if getattr(self, "local_conversation_id", None):
            try:
                patch_payload = json.dumps({
                    "account_id": getattr(self, "account_id", 1),
                    "upstream_conversation_id": cid,
                    "upstream_parent_message_id": pid,
                    "upstream_voice_session_id": uvs,
                    "gateway_voice_session_id": self.voice_session_id
                }).encode("utf-8")
                patch_req = urllib.request.Request(f"{self.gateway_url}/api/conversations/{self.local_conversation_id}", data=patch_payload, headers={
                    "Content-Type": "application/json",
                    "Cookie": self.session_cookie,
                    "X-CSRF-Token": self.csrf_token
                }, method="PATCH")
                with urllib.request.urlopen(patch_req):
                    pass
            except Exception as e:
                logger.debug(f"Failed to patch local conversation: {e}")

        # 3. Update memory session context in gateway
        if not (cid or pid or uvs):
            return
        payload = json.dumps({
            "voice_session_id": self.voice_session_id,
            "upstream_conversation_id": cid,
            "upstream_parent_message_id": pid,
            "upstream_voice_session_id": uvs
        }).encode("utf-8")
        req = urllib.request.Request(f"{self.gateway_url}/api/voice/session/context", data=payload, headers={
            "Content-Type": "application/json",
            "Cookie": self.session_cookie,
            "X-CSRF-Token": self.csrf_token
        })
        try:
            with urllib.request.urlopen(req):
                pass
        except Exception as e:
            logger.debug(f"Failed to update session context on gateway: {e}")

    def send_dc_event(self, event_obj: dict):
        """Sends a JSON control event wrapped in data_message over the negotiated oai-events DataChannel."""
        if self.dc and getattr(self.dc, "readyState", "") == "open":
            try:
                wrapped = json.dumps({
                    "type": "data_message",
                    "data": json.dumps(event_obj)
                })
                self.dc.send(wrapped)
                logger.info(f"Sent Realtime DC event: {event_obj.get('type')}")
            except Exception as e:
                logger.warning(f"Failed to send DC event: {e}")

    def send_relay_text(self, text: str):
        """Sends a text prompt or notification to ChatGPT over the oai-events DataChannel."""
        new_id = str(uuid.uuid4())
        msg = {
            "id": new_id,
            "author": {"role": "user"},
            "recipient": "all",
            "create_time": time.time(),
            "content": {
                "content_type": "text",
                "parts": [text]
            },
            "metadata": {
                "serialization_metadata": {
                    "custom_symbol_offsets": []
                }
            },
            "clientMetadata": {
                "isOptimistic": True
            }
        }
        pid = getattr(self, "upstream_parent_message_id", "")
        if not pid:
            _, _, pid = get_active_voice_session_ids()
            if pid:
                self.upstream_parent_message_id = pid
        if pid:
            msg["parent_id"] = pid

        event_obj = {
            "type": "relay_message",
            "payload": {
                "type": "relay_message",
                "message": msg
            }
        }
        self.send_dc_event(event_obj)
        self.upstream_parent_message_id = new_id
        self.update_gateway_context()

    def on_camera_event(self, event: dict):
        """Invoked when an ONVIF event (motion, alarm, analytics) triggers on the camera.
        - While camera is active and connection is idle, alerts are COMPLETELY DISABLED.
        - While in Standby mode, an alert WAKES UP the system and starts a fresh session.
        - Events during camera movement or settling cooldown are completely dropped and forgotten.
        """
        # 1. If camera is active or connection is idle or alerts disabled: DROP IMMEDIATELY
        if self.session_active or not self.standby_mode or not self.engine.are_alerts_enabled():
            return

        topic = event.get("topic", "CameraEvent")
        camera_ip = event.get("camera_ip", self.camera_ip)
        data = event.get("data", {})
        utc_time = event.get("utc_time", "")

        # 2. Ignore inactive/cleared events (e.g. IsMotion: false)
        if not self.engine.is_active_alarm(data):
            return

        # 3. If in Standby mode: camera is looking for chaos / external events to wake up
        # Check if camera was moving or in cooldown (if so, drop it)
        is_moving, remaining = self.engine.is_camera_moving_or_cooling(camera_ip, getattr(self, "cam", None), event_utc=utc_time)
        if is_moving:
            logger.info(f"[OnvifEvent] Dropping motion alert during/after movement ({remaining:.1f}s cooldown remaining): {topic}")
            return

        logger.info(f"[OnvifEvent] External alert triggered while in Standby: {topic}! Waking up fresh ChatGPT session...")
        self.trigger_wake_up(reason="alert", detail=f"ONVIF alert '{topic}'", alert_event=event)

    def on_barge_in(self):
        """Called when user starts speaking via Silero VAD.
        Since the camera hardware filters out its own sounds, we rely purely on Silero VAD.
        If the speaker was actively outputting audio, abort it immediately and send stop_speaking.
        """
        self.mark_talk_activity(source="vad")
        is_playing = bool(self.speaker and getattr(self.speaker, "is_playing", False))
        if is_playing:
            logger.info("[Barge-in] User speech detected while speaker playing -> interrupting immediately.")
            if self.speaker:
                self.speaker.abort("user_barge_in")
            stop_event = {
                "type": "action_request",
                "payload": {"action": "stop_speaking"}
            }
            if hasattr(self, "loop") and self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(self.send_dc_event, stop_event)
            else:
                self.send_dc_event(stop_event)
        else:
            logger.debug(f"[VAD] User speaking, speaker idle; normal speech turn.")

    async def connect_session(self, resume: bool = False):
        """Initiates WebRTC session with ChatGPT Realtime via the local Go gateway.
        When resume is False (default), guarantees a fresh conversation is started."""
        self.loop = asyncio.get_running_loop()
        self.login_gateway()
        self.sync_token_if_needed()

        self.voice_session_id = ""
        if not resume:
            # Server restart or fresh session: strictly start a new conversation
            self.upstream_conversation_id = ""
            self.upstream_parent_message_id = ""
            self.upstream_voice_session_id = ""
            self.local_conversation_id = ""

        self.reconnect_event = asyncio.Event()
        self.pc = RTCPeerConnection()
        self.speaker = CameraSpeakerStreamer(self.camera_ip, on_speech_active=self.mark_talk_activity)
        if self.mic_capture:
            self.mic_capture.clear_buffer()
            self.mic_track = CameraMicTrack(self.mic_capture)
        else:
            rtsp_url = f"rtsp://admin:@{self.camera_ip}:554/ch0_0.264"
            self.mic_capture = CameraMicCapture(rtsp_url, on_speech=self.on_speech_detected)
            self.mic_track = CameraMicTrack(self.mic_capture)

        self.pc.addTrack(self.mic_track)
        
        # Negotiated DataChannel "oai-events" (id=0) as specified by ChatGPT WebRTC protocol
        self.dc = self.pc.createDataChannel("oai-events", id=0, negotiated=True)

        @self.dc.on("open")
        def on_open():
            logger.info(">>> ChatGPT Realtime DataChannel (oai-events) connected! Live voice active. <<<")
            self.session_active = True
            self.mark_talk_activity(source="connect")
            self.stop_event_subscription()
            self.engine.set_live_session_state(is_active=True, alerts_enabled=False, camera_ip=self.camera_ip)

            # Instruct OpenAI Realtime server-side VAD to allow natural speech pauses (1500ms silence gap)
            try:
                session_update = {
                    "type": "session.update",
                    "session": {
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 1500
                        }
                    }
                }
                self.send_dc_event(session_update)
            except Exception as e:
                logger.debug(f"Failed to send session.update: {e}")

            # If fresh connection (not mid-call reconnect), prompt ChatGPT to speak an initial greeting through the camera
            if not resume:
                async def _send_initial_hello():
                    await asyncio.sleep(1.0)
                    try:
                        logger.info("Triggering initial spoken greeting through camera speaker...")
                        self.send_relay_text("I have connected to your camera audio. Please say a brief one-sentence greeting out loud through the camera speaker to confirm the link is active.")
                    except Exception as ex:
                        logger.debug(f"Initial greeting trigger failed: {ex}")

                asyncio.create_task(_send_initial_hello())

        @self.dc.on("close")
        def on_close():
            logger.warning(">>> ChatGPT Realtime DataChannel (oai-events) closed! Triggering auto-reconnect... <<<")
            self.reconnect_event.set()

        @self.dc.on("error")
        def on_error(err):
            logger.error(f">>> ChatGPT Realtime DataChannel (oai-events) error: {err}! Triggering auto-reconnect... <<<")
            self.reconnect_event.set()

        @self.dc.on("message")
        def on_message(msg):
            try:
                m = json.loads(msg)
                if m.get("type") == "data_message" and isinstance(m.get("data"), str):
                    m = json.loads(m["data"])
                t = m.get("type", "")

                self.last_activity_time = time.time()
                if t == "state_update":
                    st = (m.get("payload") or {}).get("new_state", "")
                    self.chatgpt_state = st
                    logger.info(f"[ChatGPT State] {st}")
                    if st in ("speaking", "answering", "generating", "responding", "thinking"):
                        self.mark_talk_activity(source="agent")
                    elif st in ("listening", "idle"):
                        self.last_agent_done_time = time.monotonic()
                elif t == "startup_telemetry":
                    p = m.get("payload") or m
                    if p.get("conversation_id"):
                        self.upstream_conversation_id = p["conversation_id"]
                    if p.get("voice_session_id"):
                        self.upstream_voice_session_id = str(p["voice_session_id"])
                    self.update_gateway_context()
                elif t == "conversation_update":
                    p = m.get("payload") or m
                    if p.get("conversation_id"):
                        self.upstream_conversation_id = p["conversation_id"]
                    if p.get("parent_message_id"):
                        self.upstream_parent_message_id = p["parent_message_id"]
                    self.update_gateway_context()
                elif t == "chat_message_delta":
                    self.mark_talk_activity(source="delta")
                    self.last_agent_stream_time = time.monotonic()
                    d = (m.get("delta") or (m.get("payload") or {}).get("delta") or {})
                    v = d.get("v")
                    if isinstance(v, dict):
                        msg_obj = v.get("message") or {}
                        if msg_obj.get("id"):
                            self.upstream_parent_message_id = msg_obj["id"]
                            self.update_gateway_context()
                        parts = msg_obj.get("content", {}).get("parts", [])
                        if parts and isinstance(parts[0], str):
                            logger.info(f"[ChatGPT Spoken Output] {parts[0][:120]}")
                elif t in ("spawn_update", "usage_update"):
                    p = m.get("payload") or m
                    limits = p.get("limits", {}).get("audio", {})
                    remaining = limits.get("remaining_seconds")
                    rate_limit = p.get("rate_limit_message") or limits.get("message")
                    if remaining is not None and remaining <= 0:
                        desc = ""
                        resets_at = None
                        if rate_limit and isinstance(rate_limit, dict):
                            exceed = rate_limit.get("exceed_limit_message") or {}
                            desc = exceed.get("description") or exceed.get("title") or ""
                            resets_at = rate_limit.get("resets_at")
                        m_time = re.search(r"(\d{1,2}:\d{2}\s*(?:am|pm|AM|PM))", desc)
                        reset_str = m_time.group(1) if m_time else ""
                        self.reset_str = reset_str
                        self.resets_at = resets_at
                        logger.error(f"[ChatGPT Voice Cap Reached] Daily audio voice limit exceeded! ({desc})")
                        if getattr(self, "account_email", None):
                            save_account_status(self.account_email, limited=True, reset_str=reset_str, resets_at=resets_at)
                    elif t == "usage_update":
                        logger.debug(f"[ChatGPT Event] type={t} payload={m.get('payload')}")
                elif t == "goodbye":
                    p = m.get("payload") or m
                    reason = p.get("reason", "")
                    detail = p.get("detail", "")
                    if reason == "cap_reached" or "usage exceeded" in detail:
                        logger.error(f"*******************************************************************************")
                        logger.error(f"[ChatGPT Account Limit] DAILY VOICE USAGE CAP REACHED ({detail})!")
                        logger.error(f"OpenAI server closed session with goodbye ({reason}: {detail}).")
                        logger.error(f"The bridge will back off and wait instead of rapid reconnect loops.")
                        logger.error(f"*******************************************************************************")
                        self.cap_reached = True
                        if getattr(self, "account_email", None):
                            save_account_status(
                                self.account_email,
                                limited=True,
                                reset_str=getattr(self, "reset_str", "daily cap reached"),
                                resets_at=getattr(self, "resets_at", None)
                            )
                    elif reason == "error" or "error" in detail:
                        logger.error(f"[ChatGPT Error] Server closed session with error ({detail}). Resetting session IDs for clean reconnect.")
                        self.upstream_conversation_id = ""
                        self.upstream_parent_message_id = ""
                        self.upstream_voice_session_id = ""
                        self.voice_session_id = ""
                    else:
                        logger.info(f"[ChatGPT Event] type=goodbye reason={reason} detail={detail}")
                elif t:
                    logger.info(f"[ChatGPT Event] type={t} raw={json.dumps(m)[:200]}")
            except Exception as e:
                logger.warning(f"DC message parse error: {e}")

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                logger.info("Received ChatGPT audio stream -> routing to camera speaker!")
                self.mark_talk_activity()
                asyncio.create_task(self.speaker.consume_track(track))

        @self.pc.on("iceconnectionstatechange")
        def on_ice():
            logger.info(f"WebRTC ICE connection state: {self.pc.iceConnectionState}")
            if self.pc.iceConnectionState in ("failed", "disconnected", "closed"):
                self.reconnect_event.set()

        @self.pc.on("connectionstatechange")
        def on_state():
            logger.info(f"WebRTC connection state: {self.pc.connectionState}")
            if self.pc.connectionState in ("failed", "disconnected", "closed"):
                self.reconnect_event.set()

        # WebRTC Offer
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)

        # Allow brief moment for ICE candidates to gather
        try:
            for _ in range(10):
                if self.pc.iceGatheringState == "complete":
                    break
                await asyncio.sleep(0.08)
        except Exception:
            pass

        offer_sdp = self.pc.localDescription.sdp if self.pc.localDescription else offer.sdp

        # Exchange SDP with chatgpt-web-voice (resumes ongoing conversation if reconnecting mid-chat)
        req_body = {
            "offer_sdp": offer_sdp,
            "voice": self.voice,
            "voice_mode": "wingman",
            "language_code": "auto",
            "voice_session_id": "",
            "account_id": getattr(self, "account_id", 1),
            "upstream_conversation_id": getattr(self, "upstream_conversation_id", ""),
            "upstream_parent_message_id": getattr(self, "upstream_parent_message_id", ""),
            "upstream_voice_session_id": getattr(self, "upstream_voice_session_id", "")
        }
        session_payload = json.dumps(req_body).encode("utf-8")

        req = urllib.request.Request(f"{self.gateway_url}/api/voice/session", data=session_payload, headers={
            "Content-Type": "application/json",
            "Cookie": self.session_cookie,
            "X-CSRF-Token": self.csrf_token
        })

        logger.info("Connecting WebRTC session via voice gateway...")
        with urllib.request.urlopen(req) as resp:
            resp_data = json.loads(resp.read().decode())
            answer_sdp = resp_data["answer_sdp"]
            vs_id = resp_data.get("voice_session_id")
            self.voice_session_id = vs_id
            logger.info(f"Gateway session established: {vs_id}")

            answer = RTCSessionDescription(sdp=answer_sdp, type="answer")
            await self.pc.setRemoteDescription(answer)

        logger.info("Session connected. Camera microphone & speaker are directly linked to ChatGPT Live!")

    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()

        # 1. Start persistent RTSP microphone capture & Silero VAD (listens 24/7)
        rtsp_url = f"rtsp://admin:@{self.camera_ip}:554/ch0_0.264"
        self.mic_capture = CameraMicCapture(rtsp_url, on_speech=self.on_speech_detected)

        # 2. Alerts start DISABLED (only enabled in Standby mode)
        self.stop_event_subscription()
        self.engine.set_live_session_state(is_active=True, alerts_enabled=False, camera_ip=self.camera_ip)

        # Configurable inactivity timeout (default: 180 seconds = 3 minutes to comfortably allow visual subagent searches)
        inactivity_limit_s = float(os.getenv("CAMXPOSED_INACTIVITY_TIMEOUT_S", "180.0"))
        first_connect = True
        should_standby = False

        while self.running:
            if not first_connect and should_standby:
                # Enter STANDBY mode only when genuine inactivity timeout was reached
                self.standby_mode = True
                self.session_active = False
                self.wake_event = asyncio.Event()

                # In Standby mode: arm alerts and look for chaos
                self.engine.set_live_session_state(is_active=False, alerts_enabled=True, camera_ip=self.camera_ip)
                self.start_event_subscription()

                logger.info("===============================================================================")
                logger.info(">>> CHATGPT DISCONNECTED — STANDBY MODE ACTIVE <<<")
                logger.info(f"- Inactivity timeout reached (camera idle for > {inactivity_limit_s:.0f}s with agent done).")
                logger.info("- Continuous VAD active: Listening for voice on camera microphone.")
                logger.info("- Camera alerts ARMED: Motion and alarm sensors active to trigger wake-up.")
                logger.info("- Speak into mic OR trigger camera alert to start a fresh chat.")
                logger.info("===============================================================================")

                # Periodically poll while in Standby to wake up if a background subagent finishes!
                while self.running and not self.wake_event.is_set():
                    try:
                        from lan_engine import _session_state_file
                        sf = _session_state_file()
                        if sf.exists():
                            sdata = json.loads(sf.read_text(encoding="utf-8"))
                            ev = sdata.get("subagent_completed_event")
                            if ev and ev.get("timestamp", 0) > self.last_subagent_event_time:
                                self.wake_reason = f"subagent_completed:{ev.get('task')[:30]}"
                                self.wake_event.set()
                                break
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(self.wake_event.wait(), timeout=1.0)
                        break
                    except asyncio.TimeoutError:
                        continue

                if not self.running:
                    break
                logger.info(f">>> WAKING UP (Trigger: {getattr(self, 'wake_reason', 'wake_event')})! Starting fresh ChatGPT session... <<<")
                self.standby_mode = False
                # Immediately disarm alerts
                self.stop_event_subscription()
                self.engine.set_live_session_state(is_active=True, alerts_enabled=False, camera_ip=self.camera_ip)
                # Fresh conversation on wake-up from standby
                self.upstream_conversation_id = ""
                self.upstream_parent_message_id = ""
                self.upstream_voice_session_id = ""
            elif not first_connect:
                # Unexpected disconnection (DataChannel closed or network blip during active chat):
                # Auto-reconnect immediately to resume the live session!
                logger.info(">>> Unintended disconnect during active session — reconnecting immediately to resume conversation... <<<")
                await asyncio.sleep(1.0)

            is_midcall_reconnect = bool(not first_connect and not should_standby)
            first_connect = False
            self.disconnect_due_to_inactivity = False
            heartbeat_task = None
            inactivity_task = None
            try:
                # Guarantee alerts are disabled during connection and active chat
                self.stop_event_subscription()
                self.engine.set_live_session_state(is_active=True, alerts_enabled=False, camera_ip=self.camera_ip)

                await self.connect_session(resume=is_midcall_reconnect)
                self.session_active = True
                self.mark_talk_activity(source="connect")

                now_mono = time.monotonic()
                self.last_agent_done_time = now_mono
                self.last_agent_busy_time = now_mono
                self.last_vad_speech_time = now_mono

                # Heartbeat: keep gateway session active every 20s
                async def _heartbeat_loop():
                    while self.running and not self.reconnect_event.is_set():
                        await asyncio.sleep(20)
                        try:
                            self.update_gateway_context()
                        except Exception:
                            pass
                heartbeat_task = asyncio.create_task(_heartbeat_loop())

                # Inactivity Monitor: check every 1.0s
                async def _inactivity_monitor():
                    last_log_time = 0.0
                    last_notified_subagent_time = time.time()
                    while self.running and not self.reconnect_event.is_set():
                        await asyncio.sleep(1.0)
                        now = time.monotonic()

                        # 0. Check for subagent background task completion
                        try:
                            from lan_engine import _session_state_file
                            sf = _session_state_file()
                            if sf.exists():
                                sdata = json.loads(sf.read_text(encoding="utf-8"))
                                ev = sdata.get("subagent_completed_event")
                                if ev and ev.get("timestamp", 0) > self.last_subagent_event_time:
                                    self.last_subagent_event_time = ev["timestamp"]
                                    task_title = ev.get("task", "search task")
                                    found = ev.get("found", None)
                                    status_tag = ev.get("status_tag", "FOUND" if found else "NOT_FOUND")
                                    conclusion = ev.get("conclusion", "")
                                    duration = ev.get("duration_seconds", "?")
                                    
                                    if status_tag == "POSSIBLE":
                                        found_str = "POSSIBLE TARGET LOCATED"
                                    elif found is True or status_tag == "FOUND":
                                        found_str = "FOUND"
                                    else:
                                        found_str = "NOT FOUND"

                                    clean_details = _clean_subagent_conclusion(conclusion)
                                    detail_str = f" {clean_details}." if clean_details else ""
                                    notice = (
                                        f"[Search Result: {found_str} - '{task_title}'.{detail_str} "
                                        f"Camera is stopped at target. Announce outcome to user concisely in 1-2 spoken sentences.]"
                                    )
                                    logger.info(f"[LiveVoiceBridge] Subagent search completed (status={status_tag})! Relaying concise outcome to ChatGPT live session...")
                                    self.send_relay_text(notice)
                        except Exception:
                            pass

                        # 1. Check if it is the agent's turn (thinking, generating, speaking, executing MCP tools)
                        agent_busy, reason = self.is_agent_turn()
                        if agent_busy:
                            self.last_agent_busy_time = now
                            self.last_agent_done_time = now
                            if (now - last_log_time) >= 15.0:
                                last_log_time = now
                                logger.info(f"[Inactivity Monitor] Agent's turn is active ({reason}); inactivity timer paused.")
                            continue

                        # 2. Agent's turn is done! Measure idle silence (time since agent finished AND no user VAD speech)
                        silence_ref = max(self.last_vad_speech_time, self.last_agent_done_time)
                        idle_s = now - silence_ref

                        if idle_s >= inactivity_limit_s:
                            logger.info("===============================================================================")
                            logger.info(f"[Inactivity Timeout] Camera idle for {idle_s:.1f}s (> {inactivity_limit_s:.0f}s) with agent done and no user speech.")
                            logger.info("Closing active session and disconnecting from ChatGPT server...")
                            logger.info("===============================================================================")
                            self.disconnect_due_to_inactivity = True
                            self.reconnect_event.set()
                            break
                inactivity_task = asyncio.create_task(_inactivity_monitor())

                await self.reconnect_event.wait()

            except Exception as e:
                logger.warning(f"Live voice bridge session error: {e}")
            finally:
                if heartbeat_task:
                    heartbeat_task.cancel()
                if inactivity_task:
                    inactivity_task.cancel()
                should_standby = bool(self.disconnect_due_to_inactivity)
                await self.stop_session(clear_context=should_standby)

            if self.running:
                if getattr(self, "cap_reached", False):
                    logger.warning("Voice usage cap active on account. Backing off 60s...")
                    self.cap_reached = False
                    await asyncio.sleep(60.0)
                elif not should_standby:
                    logger.info("Connection closed during active chat; reconnecting in 1s to continue...")
                    await asyncio.sleep(1.0)

    async def stop_session(self, clear_context: bool = False):
        """Stops active WebRTC session while keeping persistent VAD and alert listeners active."""
        self.session_active = False
        if self.speaker:
            try: self.speaker.close()
            except Exception: pass
            self.speaker = None
        if self.mic_track:
            try: self.mic_track.stop()
            except Exception: pass
            self.mic_track = None
        if self.pc:
            try: await self.pc.close()
            except Exception: pass
            self.pc = None
        self.dc = None
        self.voice_session_id = ""
        if clear_context:
            self.upstream_conversation_id = ""
            self.upstream_parent_message_id = ""
            self.upstream_voice_session_id = ""
            self.local_conversation_id = ""

    async def stop(self):
        self.running = False
        self.session_active = False
        self.standby_mode = False
        if hasattr(self, "wake_event") and self.wake_event:
            self.wake_event.set()
        if hasattr(self, "reconnect_event") and self.reconnect_event:
            self.reconnect_event.set()
        if self.event_subscriber:
            try: self.event_subscriber.stop()
            except Exception: pass
            self.event_subscriber = None
        if self.mic_capture:
            try: self.mic_capture.close()
            except Exception: pass
            self.mic_capture = None
        await self.stop_session()


def run_live_bridge(camera_ip: str, voice: str = "cove", token: Optional[str] = None, account_email: Optional[str] = None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bridge = LiveVoiceBridge(camera_ip=camera_ip, voice=voice, token=token, account_email=account_email)
    try:
        loop.run_until_complete(bridge.run())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        loop.run_until_complete(bridge.stop())
        loop.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CamXposed <-> ChatGPT Web Voice Bridge")
    parser.add_argument("--camera-ip", default="10.195.38.213", help="Camera local IP address")
    parser.add_argument("--voice", default="cove", help="ChatGPT voice name (e.g. cove, breeze, ember, juniper, vale)")
    parser.add_argument("--token", default=None, help="Optional ChatGPT session/access token")
    parser.add_argument("--account-email", default=None, help="Optional ChatGPT account email")
    parser.add_argument("--motion-cooldown", type=float, default=None, help="Seconds after movement to ignore motion alarms")
    args = parser.parse_args()
    if args.motion_cooldown is not None:
        os.environ["CAMXPOSED_MOTION_COOLDOWN_S"] = str(args.motion_cooldown)
    run_live_bridge(camera_ip=args.camera_ip, voice=args.voice, token=args.token, account_email=args.account_email)

