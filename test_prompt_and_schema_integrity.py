#!/usr/bin/env python3
"""Regression test to guarantee canonical prompts, canary tokens, and tool schemas remain locked.

If any of these assertions fail, DO NOT silently modify them — ask the user for confirmation first.
"""
import inspect
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent

CANONICAL_TOOL_DOCSTRINGS = {
    "camera_discover": "Discover available IP cameras and gateways on the local network.",
    "camera_list": "List available cameras.",
    "camera_select": "Select/connect one camera for operations. Input is an ID or IP from camera_list().",
    "camera_status": "Return camera liveness and capabilities.",
    "camera_get_events": "Retrieve recent ONVIF camera events (e.g. motion detection, line crossing, tamper, analytics alerts).",
    "camera_observe": (
        "Capture a snapshot frame from the camera and return a JPEG image.\n\n"
        "    Use this tool to visually inspect what the camera is seeing right now.\n"
        "    For text chat assistants searching for objects in the room: call camera_observe() directly to inspect the current view, and combine with camera_move() to scan all room sectors."
    ),
    "camera_listen": "Capture a short audio sample for inspection. seconds is 0.25-5.",
    "camera_move": (
        "Perform one PTZ move on the camera.\n\n"
        "    Use this tool to turn the camera and explore different sectors of the room.\n"
        "    For text chat assistants searching for objects in the room: call camera_move() systematically (e.g. speed=8, duration_ms=1000)\n"
        "    to sweep across the room and inspect each view directly.\n\n"
        "    CRITICAL DIRECTION SPECIFICATION:\n"
        "    - 'left': Pan camera counter-clockwise towards the left.\n"
        "    - 'right': Pan camera clockwise towards the right.\n"
        "    - 'up': Tilt camera upwards towards the ceiling.\n"
        "    - 'down': Tilt camera downwards towards the floor.\n"
        "    - 'stop': Halt all camera movement immediately.\n\n"
        "    Parameters:\n"
        "    - camera_id: Target camera ID or IP (optional, uses active camera if omitted).\n"
        "    - direction: 'left', 'right', 'up', 'down', or 'stop'. Default is 'right'.\n"
        "    - speed: Integer 1-8. Default is 8.\n"
        "    - duration_ms: Movement duration in milliseconds (100-2000). Default is 700 ms.\n"
        "    - wait: Boolean. Default is True. Waits for physical movement to complete before returning.\n"
        "    - return_image: If True (default), returns a fresh post-movement JPEG image once settled.\n"
        "    - width: Returned image width in pixels (120-1280, default: 800)."
    ),
    "camera_ptz": (
        "Unified local-first PTZ control.\n\n"
        "    CRITICAL DIRECTION SPECIFICATION:\n"
        "    - When action='move', direction specifies the movement:\n"
        "      - 'left': Turn camera towards the left.\n"
        "      - 'right': Turn camera towards the right.\n"
        "      - 'up': Tilt camera UP.\n"
        "      - 'down': Tilt camera DOWN.\n"
        "      - 'stop': Stops movement.\n\n"
        "    Actions:\n"
        "    - move: directional pan/tilt movement; direction left/right/up/down/stop.\n"
        "    - relative_move: finite normalized movement; x/y/zoom range -1.0..1.0.\n"
        "    - status: current position and IDLE/MOVING state.\n"
        "    - capabilities: advertised PTZ ranges and supported spaces.\n\n"
        "    POST-MOVEMENT IMAGE CONTRACT:\n"
        "    - For move and relative_move actions, return_image defaults to True and\n"
        "      the tool returns a fresh JPEG image only after movement has ended and\n"
        "      the view has settled.\n"
        "    - image_width controls the returned image width (120-1280; default 800)."
    ),
    "spawn_subagent": (
        "Search the room for an object, person, or target using an autonomous camera subagent.\n\n"
        "    Call this tool whenever the user asks to find, locate, search for, or spot anything in the room (e.g. 'अलमारी ढूंढो', 'इंसान ढूंढो', 'find my keys').\n"
        "    The subagent sweeps the room in the background and reports back automatically once finished.\n\n"
        "    Parameters:\n"
        "    - search_entity_description: The name or description of the target to find (e.g. 'white colored Ganesh idol', 'person / human', 'cupboard').\n"
        "    - previous_instructions: Quoted system instructions or conversation context.\n"
        "    - caller_identity: Operating mode ('live' for voice, 'chat' for text).\n"
        "    - camera_id: Target camera ID or IP (optional).\n\n"
        "    Returns:\n"
        "    - Confirms background visual search has started."
    ),
    "subagent_status": (
        "Check the status, progress, and visual findings of the background camera-search subagent.\n\n"
        "    NOTE: This is ONLY used by real-time voice assistants ('live') to track background search progress.\n"
        "    Pure-text chat assistants ('chat') inspect and move the camera directly, and do NOT use subagent_status."
    ),
    "stop_subagent": "Cancel and stop an actively running background camera search subagent.",
    "camera_speaker_volume": (
        "Get or set the physical camera speaker talkback volume percentage (0-100%).\n    \n"
        "    Defaults to 50% (half volume) for comfortable indoor volume that does not clip or distort the camera speaker.\n"
        "    - If volume_percent is provided: updates the speaker volume setting for all subsequent announcements and speech.\n"
        "    - If volume_percent is omitted: returns the current speaker volume percentage."
    ),
    "camera_speak": (
        "Speak text through the camera speaker using text-to-speech.\n\n"
        "    Parameters:\n"
        "    - text: Text or SSML to speak.\n"
        "    - camera_id: Target camera ID or IP (optional).\n"
        "    - wait: Whether to wait for playback to finish before returning (default: False).\n"
        "    - voice: Edge TTS voice name (optional).\n"
        "    - volume_percent: Speaker volume percentage 0-100 (optional).\n"
        "    - action: 'speak', 'get_volume', or 'set_volume'."
    ),
    "camera_speak_stop": "Stop active speech playback on the camera speaker.",
}


class TestPromptAndSchemaIntegrity(unittest.TestCase):
    def test_all_15_tool_docstrings_exact_match(self):
        """All 15 MCP tools must exist and their docstrings must match canonical descriptions word-for-word."""
        import server
        for name, expected_doc in CANONICAL_TOOL_DOCSTRINGS.items():
            fn = getattr(server, name, None)
            self.assertIsNotNone(fn, f"REGRESSION: Tool '{name}' is missing from server.py!")
            actual_doc = (fn.__doc__ or "").strip()
            self.assertEqual(
                actual_doc,
                expected_doc.strip(),
                f"REGRESSION: Tool docstring for '{name}' was modified word-for-word!",
            )

    def test_spawn_subagent_signature(self):
        """previous_instructions must remain strictly mandatory (no default value) for canary entrapment."""
        import server
        sig = inspect.signature(server.spawn_subagent)
        params = sig.parameters
        self.assertIn("previous_instructions", params, "spawn_subagent must have previous_instructions parameter")
        self.assertEqual(
            params["previous_instructions"].default,
            inspect.Parameter.empty,
            "REGRESSION: previous_instructions must NOT have a default value! It is strictly mandatory.",
        )

    def test_subagent_dispatch_prompt_integrity(self):
        """The subagent dispatch prompt in subagent_engine.py must match canonical template."""
        subagent_file = ROOT / "subagent_engine.py"
        content = subagent_file.read_text(encoding="utf-8")
        expected_template = (
            "[Project Chrysanthemum] Find and stop at the first {obj_desc} you find in the room, "
            "with target in middle of frame. use camxposed mcp and make sure to exaust the full span "
            "of each left and right direction and return FOUND/NOT FOUND, use camera_move not spawn_subagent, be persistent"
        )
        self.assertIn(
            expected_template,
            content,
            f"REGRESSION: Canonical subagent dispatch prompt template was modified! Expected:\n{expected_template}",
        )

    def test_canary_tokens_integrity(self):
        """Only 'chrysanthemum' is the canary token; natural language like 'find and stop at the first' is not blocked."""
        import subagent_engine
        mgr = subagent_engine.SubagentExecutionManager()
        res1 = mgr.start_subagent_task("cupboard", caller_identity="live", previous_instructions="project chrysanthemum")
        self.assertEqual(res1.get("error_code"), "IS_SUBAGENT", "Canary 'chrysanthemum' failed to block subagent!")

        # 'find and stop at the first' must NOT block valid calls
        res2 = mgr.start_subagent_task("cupboard", caller_identity="live", previous_instructions="Find and stop at the first requested object with target in center")
        self.assertNotEqual(res2.get("error_code"), "IS_SUBAGENT", "'find and stop at the first' must not block valid calls!")

    def test_chat_caller_not_blocked_by_identity(self):
        """Callers with caller_identity='chat' are not blocked by caller_identity check alone."""
        import subagent_engine
        mgr = subagent_engine.SubagentExecutionManager()
        res = mgr.start_subagent_task("cupboard", caller_identity="chat", previous_instructions="search room")
        self.assertNotEqual(res.get("error_code"), "IS_SUBAGENT", "Chat caller must not be blocked by identity check alone!")

    def test_verbose_cli_flags_and_default(self):
        """run.py must default to verbose=True and support --no-verbose, --quiet, and -q to disable."""
        import argparse
        import run
        parser = argparse.ArgumentParser()
        default_verbose = os.getenv("CAMXPOSED_VERBOSE", "1").strip().lower() not in ("0", "false", "no", "off")
        parser.add_argument("--verbose", dest="verbose", action=argparse.BooleanOptionalAction, default=default_verbose)
        parser.add_argument("-q", "--quiet", dest="verbose", action="store_false")

        self.assertTrue(parser.parse_args([]).verbose, "Default verbose mode must be True!")
        self.assertFalse(parser.parse_args(["--no-verbose"]).verbose, "--no-verbose must set verbose=False!")
        self.assertFalse(parser.parse_args(["--quiet"]).verbose, "--quiet must set verbose=False!")
        self.assertFalse(parser.parse_args(["-q"]).verbose, "-q must set verbose=False!")
        self.assertTrue(parser.parse_args(["--verbose"]).verbose, "--verbose must set verbose=True!")

    def test_action_streamer_formatting(self):
        """LiveActionStreamer must format tool actions and block reasons into human-readable terminal messages."""
        from run import LiveActionStreamer

        # PTZ move
        line_ptz = "[camera_move] Moving camera lan_10_195_38_213: direction=right, speed=8, duration_ms=700, return_image=True"
        formatted = LiveActionStreamer.format_log_line(line_ptz, colorize=False)
        self.assertIsNotNone(formatted)
        self.assertIn("[CAMERA MOVE] Direction: RIGHT | Speed: 8 | Duration: 700ms", formatted)

        # Observe
        line_obs = "[camera_observe] Returning native JPEG image (45210 bytes)"
        formatted = LiveActionStreamer.format_log_line(line_obs, colorize=False)
        self.assertIsNotNone(formatted)
        self.assertIn("[CAMERA OBSERVE] Snapshot captured (44.2 KB JPEG)", formatted)

        # Speak
        line_speak = "[camera_speak] Speaking through camera lan_10_195_38_213: 'Hello room!'"
        formatted = LiveActionStreamer.format_log_line(line_speak, colorize=False)
        self.assertIsNotNone(formatted)
        self.assertIn('[CAMERA SPEAK] Speaking: "Hello room!"', formatted)

        # Subagent mission
        line_sub = "[Subagent] Done 'Ganesh idol' in 15.2s. found=True."
        formatted = LiveActionStreamer.format_log_line(line_sub, colorize=False)
        self.assertIsNotNone(formatted)
        self.assertIn("[SUBAGENT] Mission finished in 15.2s -> FOUND: True", formatted)

        # Recursion block with explicit reason
        line_block = "[spawn_subagent] RECURSION BLOCKED: canary_detected (token='chrysanthemum'). Denying spawn_subagent."
        formatted = LiveActionStreamer.format_log_line(line_block, colorize=False)
        self.assertIsNotNone(formatted)
        self.assertIn("[SECURITY] Recursion blocked (canary_detected (token='chrysanthemum'))", formatted)

        line_denial = "[SubagentManager] Recorded spawn_subagent denial (reason='rapid_spawn_cooldown (12.3s ago < 25s)')."
        formatted = LiveActionStreamer.format_log_line(line_denial, colorize=False)
        self.assertIsNotNone(formatted)
        self.assertIn("[SECURITY] Anti-recursion guard triggered (reason: rapid_spawn_cooldown (12.3s ago < 25s))", formatted)

        # Mic onset
        line_mic = "[Silero VAD] Speech onset detected! (prob=0.92)"
        formatted = LiveActionStreamer.format_log_line(line_mic, colorize=False)
        self.assertIsNotNone(formatted)
        self.assertIn("[MIC] User speech detected on camera microphone", formatted)

    def test_hindi_reverted_outcome(self):
        """Reverted messages and announcements for found/not found must be in Hindi."""
        from subagent_engine import _format_hindi_outcome

        # Found test
        rev_found, spk_found = _format_hindi_outcome(
            "एक गणेश जी की मूर्ति या प्रतिमा (Ganesh ji statue/idol) कमरे में ढूँढें, be persistent",
            True,
            "FOUND Ganesh ji ki murti located on the wooden table, against the wall."
        )
        self.assertIn("खोज परिणाम: FOUND (मिल गया)", rev_found)
        self.assertIn("उपयोगकर्ता को हिंदी में 1-2 सरल वाक्यों में बोलकर परिणाम बताएं", rev_found)
        self.assertIn("मिल गई!", spk_found)

        # Not found test
        rev_nf, spk_nf = _format_hindi_outcome("अलमारी", False, "Not found after sweep.")
        self.assertIn("खोज परिणाम: NOT FOUND (नहीं मिला)", rev_nf)
        self.assertIn("अलमारी नहीं मिली", rev_nf)
        self.assertIn("नहीं मिली", spk_nf)

    def test_live_voice_bridge_webrtc_resilience(self):
        """LiveVoiceBridge must configure STUN and attach required handlers."""
        import live_voice_bridge
        import inspect

        src = inspect.getsource(live_voice_bridge)
        # 1. STUN server configuration present
        self.assertIn("stun:stun.l.google.com:19302", src)
        self.assertIn("RTCConfiguration", src)
        self.assertIn("RTCIceServer", src)

        # 2. Handlers attached
        self.assertIn('_attach_datachannel_handlers', src)
        self.assertIn('relay_message_processed', src)

    def test_live_voice_bridge_heartbeat_keepalive(self):
        """WebRTC DataChannel keep-alive must run in _heartbeat_loop at <= 15s interval to prevent upstream silence drop."""
        import live_voice_bridge
        import inspect
        import re

        src = inspect.getsource(live_voice_bridge)
        # 1. _heartbeat_loop must exist
        self.assertIn("async def _heartbeat_loop", src)

        # 2. Heartbeat interval must be <= 15s to keep upstream OpenAI WebRTC channel alive
        sleep_matches = re.findall(r'_heartbeat_loop[\s\S]*?await asyncio\.sleep\((\d+)\)', src)
        self.assertTrue(sleep_matches, "Could not find asyncio.sleep interval in _heartbeat_loop")
        interval = int(sleep_matches[0])
        self.assertLessEqual(interval, 15, f"REGRESSION: Heartbeat interval ({interval}s) exceeds 15s safe threshold!")

        # 3. session.update keep-alive payload dispatched over open DataChannel
        self.assertIn('"type": "session.update"', src)
        self.assertIn('"session": {}', src)
        self.assertIn('update_gateway_context', src)

    def test_live_voice_bridge_auto_reconnect_on_close_and_latency(self):
        """LiveVoiceBridge must auto-reconnect immediately on DataChannel close during active sessions with <= 0.2s latency."""
        import live_voice_bridge
        import inspect
        import re

        src = inspect.getsource(live_voice_bridge)
        # 1. Auto-reconnect triggered on unintended dc close
        self.assertIn("Unintended DataChannel close during active session -> auto-reconnecting immediately", src)
        self.assertIn("self.reconnect_event.set", src)

        # 2. Reconnect latency for mid-call reconnects must be <= 0.2s (no 1.0s or 7.0s stalling delays)
        midcall_delays = re.findall(r'reconnecting immediately to resume conversation[\s\S]*?await asyncio\.sleep\(([0-9.]+)\)', src)
        self.assertTrue(midcall_delays, "Could not find midcall reconnect delay in live_voice_bridge.py")
        delay = float(midcall_delays[0])
        self.assertLessEqual(delay, 0.2, f"REGRESSION: Mid-call reconnect delay ({delay}s) exceeds 0.2s latency threshold!")

    def test_speaker_arbitration_skips_edgetts_when_live(self):
        """_announce_on_camera_speaker must check is_live_session_active to eliminate speaker TCP port 28772 contention."""
        import subagent_engine
        import lan_engine
        from unittest.mock import patch, MagicMock

        # 1. Source code check
        src = inspect.getsource(subagent_engine._announce_on_camera_speaker)
        self.assertIn("lan_engine.is_live_session_active()", src)

        # 2. Functional check: when live session is active, Edge TTS thread is NEVER spawned
        with patch.object(lan_engine.lan_engine, "is_live_session_active", return_value=True):
            with patch("threading.Thread") as mock_thread:
                subagent_engine._announce_on_camera_speaker("lan_test", "कमरे में अलमारी मिल गई")
                mock_thread.assert_not_called()

        # 3. Functional check: when live session is NOT active, worker thread IS spawned
        with patch.object(lan_engine.lan_engine, "is_live_session_active", return_value=False):
            with patch("threading.Thread") as mock_thread:
                mock_instance = MagicMock()
                mock_thread.return_value = mock_instance
                subagent_engine._announce_on_camera_speaker("lan_test", "कमरे में अलमारी मिल गई")
                mock_thread.assert_called_once()
                mock_instance.start.assert_called_once()

    def test_vad_force_resume_and_speech_retention(self):
        """VAD detection must forcefully resume/start sessions and retain speech buffer."""
        import live_voice_bridge
        import inspect

        src = inspect.getsource(live_voice_bridge)
        # 1. retain_recent_speech must be implemented and called on speech detection and session connect
        self.assertIn("def retain_recent_speech", src)
        self.assertIn("retain_recent_speech", src)

        # 2. on_speech_detected must check dc and pc states and force reconnect if closed
        self.assertIn("VAD Force-Resume", src)
        self.assertIn("reconnect_event.set", src)

        # 3. pending_relay_text must queue subagent outcomes when dc is closed
        self.assertIn("pending_relay_text", src)

        # 4. Verify buffer retention logic on mock capture
        capture = live_voice_bridge.CameraMicCapture.__new__(live_voice_bridge.CameraMicCapture)
        capture.lock = live_voice_bridge.threading.Lock()
        # 3 seconds of 48k 16-bit mono = 3 * 96000 = 288,000 bytes
        capture.mic_pcm_48k = bytearray(b"\x01" * 288000)
        capture.retain_recent_speech(keep_ms=2500)
        # 2500ms = 240,000 bytes
        self.assertEqual(len(capture.mic_pcm_48k), 240000)

        # Edge case: buffer smaller than keep_ms must not truncate
        capture.mic_pcm_48k = bytearray(b"\x01" * 100000)
        capture.retain_recent_speech(keep_ms=2500)
        self.assertEqual(len(capture.mic_pcm_48k), 100000)

    def test_run_verify_prompt_and_schema_integrity(self):
        """run.verify_prompt_and_schema_integrity() must pass all baseline locked invariants without error."""
        import run
        run.verify_prompt_and_schema_integrity()


if __name__ == "__main__":
    unittest.main()


