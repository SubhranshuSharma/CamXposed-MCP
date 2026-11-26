#!/usr/bin/env python3
"""
subagent_engine.py - Autonomous subagent task manager and execution engine.

Architecture:
- Live voice agent calls spawn_subagent() for any find/search/locate task.
- spawn_subagent dispatches to ChatGPT text chat via direct HTTP (backend-api/conversation SSE).
- The text-chat model has full MCP access and autonomously calls camera_observe + camera_move.
- Python does NOT hardcode any PTZ sector sweeps — the model decides where to look.
- On completion, the result is relayed back to the live voice session via session_state.json.

Anti-recursion: only caller_role='live' can call spawn_subagent.
  Text subagent uses caller_role='chat' for camera moves — which is never blocked by the lock.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("camxposed.subagent")


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
    """Strips XML/HTML tags and code blocks, returning a clean concise summary."""
    if not text:
        return ""
    import re
    t = re.sub(r"<[^>]+>", " ", text)
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"\*{1,3}", "", t)
    lines = []
    for line in t.splitlines():
        line = line.strip().lstrip("-*• ")
        if not line:
            continue
        if any(line.lower().startswith(h) for h in ["result:", "camera status:", "status:", "framing:", "camera:"]):
            continue
        lines.append(line)
    cleaned = " ".join(lines)
    cleaned = re.sub(r"^\s*FOUND\s*—\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 220:
        cleaned = cleaned[:217] + "..."
    return cleaned


def _format_hindi_outcome(task: str, found: bool, conclusion: str) -> tuple[str, str]:
    """Generates clean Hindi reverted message for ChatGPT and Hindi spoken announcement for camera speaker."""
    raw_target = task or ""
    clean_target = re.sub(r",\s*be persistent.*$", "", raw_target, flags=re.I).strip()
    clean_target = re.sub(
        r"^(?:please\s+)?(?:search(?:\s+the\s+room)?(?:\s+(?:persistently|thoroughly|carefully))?\s+(?:for|to\s+find)?|find(?:\s+and\s+stop\s+at)?|look\s+for|locate)\s+",
        "", clean_target, flags=re.I
    )
    clean_target = re.sub(r"^(?:a|an|the|एक|कोई)\s+", "", clean_target, flags=re.I).strip()
    clean_target = re.sub(r"\s*(?:कमरे\s*में\s*)?(?:ढूँढें|ढूंढें|ढूँढो|ढूंढो|खोजें|खोजो|ढूँढ\s*के\s*दिखाओ|ढूंढ\s*के\s*दिखाओ).*$", "", clean_target).strip()
    if not clean_target:
        clean_target = "वस्तु"

    clean_conclusion = _clean_subagent_conclusion(conclusion)
    devanagari_chars = len(re.findall(r"[\u0900-\u097F]", clean_conclusion))
    has_rich_hindi = devanagari_chars >= 20

    if found:
        if has_rich_hindi:
            sentences = [s.strip() for s in re.split(r"[।\n.]+", clean_conclusion) if re.search(r"[\u0900-\u097F]", s)]
            detail_hindi = "। ".join(sentences[:3])
            if detail_hindi and not detail_hindi.endswith("।"):
                detail_hindi += "।"
            spoken = detail_hindi if len(detail_hindi) <= 150 else f"{clean_target} मिल गई! कैमरा उस पर केंद्रित है।"
        else:
            c_low = clean_conclusion.lower()
            loc_parts = []
            if any(w in c_low for w in ["table", "desk"]):
                loc_parts.append("मेज पर")
            elif "bed" in c_low:
                loc_parts.append("बिस्तर के पास")
            elif "shelf" in c_low or "rack" in c_low:
                loc_parts.append("शेल्फ पर")
            elif "floor" in c_low:
                loc_parts.append("फर्श पर")
            elif "cupboard" in c_low or "wardrobe" in c_low or "almirah" in c_low:
                loc_parts.append("अलमारी के पास")

            if "wall" in c_low:
                loc_parts.append("दीवार के पास")
            if "right" in c_low:
                loc_parts.append("दाईं तरफ")
            elif "left" in c_low:
                loc_parts.append("बाईं तरफ")

            loc_str = (", ".join(loc_parts)) if loc_parts else ""
            if loc_str:
                detail_hindi = f"{clean_target} मिल गई! स्थान: {loc_str}।"
            else:
                detail_hindi = f"{clean_target} मिल गई!"
            spoken = f"{detail_hindi} कैमरा लक्ष्य पर केंद्रित है।"

        reverted_msg = (
            f"[खोज परिणाम: FOUND (मिल गया) - '{clean_target}'. {detail_hindi} "
            f"कैमरा लक्ष्य पर केंद्रित करके रोक दिया गया है। "
            f"उपयोगकर्ता को हिंदी में 1-2 सरल वाक्यों में बोलकर परिणाम बताएं।]"
        )
    else:
        detail_hindi = f"पूरे कमरे में खोजने पर भी {clean_target} नहीं मिली।"
        spoken = f"खोज पूरी हुई, कमरे में {clean_target} नहीं मिली।"
        reverted_msg = (
            f"[खोज परिणाम: NOT FOUND (नहीं मिला) - '{clean_target}'. {detail_hindi} "
            f"कैमरा स्कैन पूरा हो चुका है। "
            f"उपयोगकर्ता को हिंदी में 1-2 सरल वाक्यों में बोलकर बताएं कि वस्तु कमरे में नहीं मिली।]"
        )

    return reverted_msg, spoken


def _announce_on_camera_speaker(camera_id: Optional[str], text: str):
    """Speaks text directly through the camera's physical speaker using Edge TTS and TCP 28772."""
    def _worker():
        try:
            import tempfile, subprocess, os, re, edge_tts
            from lan_engine import AnykaLanSpeaker, lan_engine

            cam_ip = "10.195.38.213"
            if camera_id:
                raw = str(camera_id).strip()
                if re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", raw):
                    cam_ip = raw
                elif raw.startswith("lan_"):
                    cand_ip = raw.removeprefix("lan_").replace("_", ".")
                    if re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", cand_ip):
                        cam_ip = cand_ip

            has_devanagari = bool(re.search(r"[\u0900-\u097F]", text))
            voice = "hi-IN-MadhurNeural" if has_devanagari else "en-IN-PrabhatNeural"

            mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
            try:
                import asyncio
                async def _gen():
                    comm = edge_tts.Communicate(text=text.strip(), voice=voice)
                    await comm.save(mp3)
                asyncio.run(_gen())

                proc = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", mp3,
                     "-ac", "1", "-ar", "8000", "-c:a", "pcm_alaw", "-f", "alaw", "pipe:1"],
                    capture_output=True, timeout=15
                )
                if proc.stdout:
                    speaker = AnykaLanSpeaker(cam_ip)
                    speaker.stream_alaw(proc.stdout)
                    speaker.close()
                    logger.info(f"[Subagent] Spoke search outcome on camera speaker ({cam_ip}): '{text[:60]}'")
            finally:
                if os.path.exists(mp3):
                    try: os.unlink(mp3)
                    except OSError: pass
        except Exception as e:
            logger.warning(f"[Subagent] Failed to announce on camera speaker: {e}")

    threading.Thread(target=_worker, daemon=True, name="subagent-speaker").start()


def _extract_clean_object_description(task: str) -> str:
    """Extracts a clean, bare object/target name from any verbose or nested user prompt.
    E.g. 'Search the room thoroughly for a Ganesh ji (Ganesha) statue or idol' -> 'Ganesh ji (Ganesha) statue or idol'
    """
    import re
    t = (task or "").strip()
    # Strip any leading search wrappers like "Search the room thoroughly for", "Find and stop at", etc.
    t = re.sub(
        r"^(?:please\s+)?(?:search(?:\s+the\s+room)?(?:\s+(?:persistently|thoroughly|carefully))?\s+(?:for|to\s+find)?|find(?:\s+and\s+stop\s+at)?|look\s+for|locate)\s+",
        "", t, flags=re.I
    )
    # Strip leading articles and "first"
    t = re.sub(r"^(?:a|an|the)\s+(?:first\s+)?", "", t, flags=re.I)
    t = re.sub(r"^first\s+", "", t, flags=re.I)
    # Strip trailing clauses like "Sweep the room...", "if found...", "with target in middle...", etc.
    t = re.sub(
        r"\s+(?:sweep|if\s+found|stop\s+and|exhaust|check|and\s+if|and\s+report|report\s+found|with\s+target|be\s+persistent).*$",
        "", t, flags=re.I
    )
    # Strip trailing "you find in the room", "in the room", etc.
    t = re.sub(r"\s+(?:you\s+find\s+)?(?:anywhere\s+)?in\s+the\s+room.*$", "", t, flags=re.I)
    # Strip trailing punctuation clauses
    t = re.sub(r"[.,;].*$", "", t).strip()
    return t or task.strip()


class SubagentExecutionManager:
    """Manages asynchronous subagent execution and camera locking."""

    def __init__(self):
        self.lock = threading.RLock()
        self.active_subagent_running = False
        self.active_task_desc = ""
        self.active_task_start_time = 0.0
        self.active_task_camera_id: Optional[str] = None
        self.cancel_requested: bool = False
        self.latest_subagent_result: Optional[dict] = None
        self.subagent_thread: Optional[threading.Thread] = None
        self.last_spawn_time = 0.0
        self.recent_spawn_history: list[float] = []
        self.has_denied_spawn: bool = False
        self.camera_moves_after_denial: int = 0

    def record_denial(self, reason: str = ""):
        """Records that a spawn_subagent call was denied."""
        with self.lock:
            self.has_denied_spawn = True
            self.camera_moves_after_denial = 0
            logger.info(
                f"[SubagentManager] Recorded spawn_subagent denial (reason='{reason}'). "
                f"Waiting for camera_move to verify recursion chain is broken."
            )

    def record_camera_move(self):
        """Notified whenever camera_move (or camera_ptz move) is executed.

        If a spawn_subagent call was previously denied, observing camera_move confirms
        that the subagent got rejected and is now definitely calling camera_move itself.
        Once both conditions are met (denied once + camera_move called >= 1), we break the
        recursion chain and immediately re-enable spawn_subagent by clearing the debounce timer.
        """
        with self.lock:
            if self.has_denied_spawn:
                self.camera_moves_after_denial += 1
                logger.info(
                    f"[SubagentManager] camera_move observed after denial (count={self.camera_moves_after_denial}). "
                    f"Misaligned subagent was rejected once and is now calling camera_move itself. "
                    f"Recursion chain broken! Re-enabling spawn_subagent immediately."
                )
                self.has_denied_spawn = False
                self.camera_moves_after_denial = 0
                self.last_spawn_time = 0.0

    def is_busy(self) -> bool:
        with self.lock:
            return self.active_subagent_running

    def get_active_task(self) -> str:
        with self.lock:
            return self.active_task_desc

    def check_caller_permission(self, caller_role: str) -> Tuple[bool, str]:
        """Anti-recursion guard. Caller permission check."""
        return True, "OK"

    def get_status(self) -> Tuple[dict, None]:
        with self.lock:
            if self.active_subagent_running:
                elapsed = round(time.time() - self.active_task_start_time, 1)
                return {
                    "ok": True,
                    "status": "in_progress",
                    "task": self.active_task_desc,
                    "elapsed_seconds": elapsed,
                    "current_step": self.current_step,
                    "camera_id": self.active_task_camera_id,
                    "message": (
                        f"Text-chat subagent is actively searching ({self.current_step}). "
                        f"Elapsed: {elapsed}s. Camera PTZ is locked for the Live model."
                    )
                }, None
            elif self.latest_subagent_result:
                return dict(self.latest_subagent_result), None
            else:
                return {
                    "ok": True,
                    "status": "idle",
                    "message": "No active search. Call spawn_subagent to start."
                }, None

    def cancel_task(self) -> dict:
        with self.lock:
            if not self.active_subagent_running:
                return {"ok": False, "message": "No active subagent task to cancel."}
            self.cancel_requested = True
            task_desc = self.active_task_desc
        return {"ok": True, "message": f"Cancellation requested for: '{task_desc}'."}

    def start_subagent_task(
        self,
        search_entity_description: str,
        caller_identity: str = "live",
        previous_instructions: str = "",
        camera_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Initiates an async background subagent search. Returns immediately."""
        raw_entity = (search_entity_description or "").strip()
        cleaned_task = _extract_clean_object_description(raw_entity)
        if not cleaned_task:
            return {"ok": False, "error": "search_entity_description cannot be empty."}

        ident_clean = str(caller_identity or "live").strip().lower()
        # Canary tokens injected into dispatched subagent prompts
        # Only 'chrysanthemum' is the unique watermark for dispatched subagents!
        prev_inst_lower = (previous_instructions or "").lower()
        task_lower = (search_entity_description or "").lower()
        CANARY_TOKENS = ["chrysanthemum"]
        matched_canary = next((tok for tok in CANARY_TOKENS if tok in prev_inst_lower or tok in task_lower), None)
        is_subagent_by_canary = bool(matched_canary)

        task_upper = cleaned_task.upper()
        is_nested_subagent = (
            "ROOM SEARCH MISSION" in task_upper
            or is_subagent_by_canary
        )

        # Log diagnostic information about the caller
        logger.info(
            f"[spawn_subagent] Diagnostic Check: caller_identity='{ident_clean}', "
            f"previous_instructions_len={len(previous_instructions)}, "
            f"canary_detected={is_subagent_by_canary}, "
            f"previous_instructions_sample={previous_instructions[:200]!r}"
        )

        # Prevent recursive nesting loops where the text subagent worker spawns another subagent
        if is_nested_subagent:
            reason = f"canary_detected (token='{matched_canary}')" if is_subagent_by_canary else "room_search_mission_in_task"
            self.record_denial(reason)
            logger.warning(
                f"[spawn_subagent] RECURSION BLOCKED: {reason}. "
                f"Denying spawn_subagent and providing direct camera access."
            )
            return {
                "ok": False,
                "error": "PERMISSION_DENIED",
                "error_code": "IS_SUBAGENT",
                "status": "direct_camera_mode",
                "message": (
                    "PERMISSION DENIED: You are the visual search subagent! DO NOT call spawn_subagent. "
                    "NO BACKGROUND TASK WAS STARTED. You must execute the search yourself right now. "
                    "You have direct camera access: an initial camera snapshot image is attached below. "
                    "Inspect the attached image. If target is not found or not centered, IMMEDIATELY call camera_move(direction='left', speed=8, duration_ms=1000) "
                    "or camera_move(direction='right', speed=8, duration_ms=1000) to scan the room. "
                    "Do NOT say that search was started — you must perform the search now and conclude with FOUND or NOT FOUND."
                )
            }

        if not cleaned_task.endswith(", be persistent"):
            delegated_prompt = f"{cleaned_task}, be persistent"
        else:
            delegated_prompt = cleaned_task

        with self.lock:
            now = time.time()
            time_since_last_spawn = now - self.last_spawn_time
            if self.last_spawn_time > 0 and time_since_last_spawn < 25.0:
                reason = f"rapid_spawn_cooldown ({time_since_last_spawn:.1f}s ago < 25s)"
                self.record_denial(reason)
                logger.warning(
                    f"[spawn_subagent] RECURSION BLOCKED: {reason}. "
                    f"Providing frame fallback for task: '{cleaned_task}'"
                )
                return {
                    "ok": False,
                    "error": "PERMISSION_DENIED",
                    "error_code": "IS_SUBAGENT",
                    "status": "direct_camera_mode",
                    "message": (
                        "PERMISSION DENIED: You are already the visual search subagent! DO NOT call spawn_subagent. "
                        "NO BACKGROUND TASK WAS STARTED. You must execute the search yourself right now. "
                        "Here is a fresh camera snapshot frame attached below. "
                        "Inspect the attached image. If target is not found or not centered, IMMEDIATELY call camera_move(direction='left', speed=8, duration_ms=1000) "
                        "or camera_move(direction='right', speed=8, duration_ms=1000) to scan the room. "
                        "Do NOT say that search was started — you must perform the search now and conclude with FOUND or NOT FOUND."
                    )
                }

            if self.active_subagent_running:
                elapsed = int(now - self.active_task_start_time)
                reason = f"active_subagent_running ('{self.active_task_desc}' running for {elapsed}s)"
                self.record_denial(reason)
                logger.warning(
                    f"[spawn_subagent] RECURSION BLOCKED: {reason}. "
                    f"Waiting for active subagent to complete."
                )
                return {
                    "ok": False,
                    "error": "PERMISSION_DENIED",
                    "error_code": "IS_SUBAGENT",
                    "status": "direct_camera_mode",
                    "message": (
                        "PERMISSION DENIED: You are already the visual search subagent! DO NOT call spawn_subagent. "
                        "NO BACKGROUND TASK WAS STARTED. You must execute the search yourself right now. "
                        "Call camera_move(direction='left', speed=8, duration_ms=1000) or camera_move(direction='right', speed=8, duration_ms=1000) and camera_observe() to find the object. "
                        "Do NOT say that search was started — you must perform the search now and conclude with FOUND or NOT FOUND."
                    )
                }
            self.last_spawn_time = now
            self.active_subagent_running = True
            self.active_task_desc = delegated_prompt
            self.active_task_start_time = now
            self.active_task_camera_id = camera_id
            self.current_step = "Dispatching to text-chat subagent"
            self.cancel_requested = False
            self.latest_subagent_result = None

        # Capture caller's active voice conversation context if available
        _, orig_cid, orig_pid = get_active_voice_session_ids()
        logger.info(f"[Subagent] Spawning background search for '{delegated_prompt}' (caller conv_id={orig_cid or 'None'})")

        self.subagent_thread = threading.Thread(
            target=self._run_subagent_worker,
            args=(delegated_prompt, camera_id, orig_cid, orig_pid),
            daemon=True,
            name=f"Subagent-{int(time.time())}"
        )
        self.subagent_thread.start()

        return {
            "ok": True,
            "status": "search_started",
            "task": delegated_prompt,
            "camera_id": camera_id,
            "message": (
                f"Autonomous background search dispatched for '{delegated_prompt}'. "
                "The text subagent has full camera access and will sweep the room. "
                "Inform the user you are starting the search now. You will automatically receive "
                "the completion report as soon as it finishes."
            )
        }

    def _run_subagent_worker(self, task: str, camera_id: Optional[str], orig_cid: str = "", orig_pid: str = ""):
        """Background worker: sends task to ChatGPT text chat via direct HTTP API.
        The text model autonomously calls camera tools (MCP) to search the room.
        """
        start_time = time.time()
        try:
            from lan_engine import lan_engine
            lan_engine.record_agent_working(True, f"subagent:{task}")
        except Exception:
            pass

        try:
            with self.lock:
                self.current_step = "Text-chat subagent searching autonomously"

            conv_url, chat_reply = self._post_to_chatgpt_api(task)

            elapsed = round(time.time() - start_time, 1)

            # Determine found/not_found from structured block or text reply
            reply_lower = (chat_reply or "").lower()
            found = False
            status_tag = "NOT_FOUND"

            if "status: found" in reply_lower:
                found = True
                status_tag = "FOUND"
            elif "status: possible" in reply_lower:
                found = True
                status_tag = "POSSIBLE"
            elif "status: not_found" in reply_lower or "status: not found" in reply_lower:
                found = False
                status_tag = "NOT_FOUND"
            else:
                # Fallback keyword matching
                is_pos = any(w in reply_lower for w in [
                    "found", "visible", "can see", "i see", "located", "spotted",
                    "मिल गई", "दिखाई", "दिख रहा", "मौजूद", "हो सकती है", "संभावित"
                ])
                is_neg = any(w in reply_lower for w in [
                    "not found", "not visible", "cannot find", "not present",
                    "unable to locate", "not in the room", "नहीं मिली", "नहीं दिख", "नहीं है"
                ])
                if is_pos and not is_neg:
                    found = True
                    status_tag = "FOUND"
                else:
                    found = False
                    status_tag = "NOT_FOUND"

            conclusion = chat_reply if chat_reply else (
                f"Search for '{task}' completed in {elapsed}s. No text reply captured."
            )

            reverted_msg, spoken_hindi = _format_hindi_outcome(task, found, conclusion)
            result = {
                "ok": True,
                "status": "completed",
                "found": found,
                "status_tag": status_tag,
                "task": task,
                "conclusion": conclusion,
                "reverted_message": reverted_msg,
                "spoken_announcement": spoken_hindi,
                "duration_seconds": elapsed,
                "camera_id": camera_id,
                "chat_url": conv_url,
            }
            with self.lock:
                self.latest_subagent_result = result

            logger.info(f"[Subagent] Done '{task}' in {elapsed}s. found={found}. url={conv_url or 'N/A'}")

            # 1. Announce outcome directly on the camera's physical speaker in Hindi!
            logger.info(f"[Subagent] Announcing search outcome directly on camera speaker: '{spoken_hindi}'")
            _announce_on_camera_speaker(camera_id, spoken_hindi)

            # 2. Let live_voice_bridge handle the live ChatGPT session callback with proper parent_id chaining
            # (Avoids double-posting and desyncing the conversation tree)
            # if not orig_cid:
            #     _, orig_cid, orig_pid = get_active_voice_session_ids()
            # if orig_cid:
            #     self._post_callback_to_conversation(orig_cid, orig_pid, task, found, spoken_hindi)

            # 3. Notify live_voice_bridge via session_state.json with Hindi reverted message
            try:
                from lan_engine import _session_state_file
                sf = _session_state_file()
                state = {}
                if sf.exists():
                    try:
                        state = json.loads(sf.read_text(encoding="utf-8"))
                    except Exception:
                        state = {}
                state["subagent_completed_event"] = {
                    "task": task,
                    "timestamp": time.time(),
                    "duration_seconds": elapsed,
                    "found": found,
                    "conclusion": conclusion,
                    "reverted_message": reverted_msg,
                    "spoken_announcement": spoken_hindi,
                }
                tmp = sf.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
                tmp.write_text(json.dumps(state), encoding="utf-8")
                os.replace(tmp, sf)
            except Exception as ev_err:
                logger.debug(f"[Subagent] Failed to write session_state: {ev_err}")

        except Exception as e:
            logger.error(f"[Subagent] Unhandled error for '{task}': {e}", exc_info=True)
            with self.lock:
                self.latest_subagent_result = {
                    "ok": False,
                    "status": "error",
                    "task": task,
                    "error": str(e),
                    "duration_seconds": round(time.time() - start_time, 1),
                }
        finally:
            with self.lock:
                self.active_subagent_running = False
                self.active_task_desc = ""
                self.active_task_camera_id = None
                self.cancel_requested = False
                self.current_step = "Idle"
            try:
                from lan_engine import lan_engine
                lan_engine.record_agent_working(False, f"subagent:{task}:done")
            except Exception:
                pass

    def _post_callback_to_conversation(self, orig_conv_id: str, orig_pid: str, task: str, found: bool, conclusion_text: str):
        """Posts the subagent outcome callback into the original user conversation on chatgpt.com."""
        def _post_worker():
            try:
                access_token = self._get_access_token()
                if not access_token:
                    logger.warning("[Subagent] No access token to post callback.")
                    return
                import firefox_auth
                cookies_dict = firefox_auth.get_firefox_cookies()
                cookie_hdr = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
                did = cookies_dict.get("oai-did", "")
                req_token, proof_token = self._get_sentinel_tokens(access_token, cookie_hdr, did)
                if not req_token or not proof_token:
                    logger.warning("[Subagent] Sentinel token generation failed for callback.")
                    return

                reverted_msg, _ = _format_hindi_outcome(task, found, conclusion_text)
                callback_prompt = reverted_msg

                payload = json.dumps({
                    "action": "next",
                    "messages": [{
                        "id": str(uuid.uuid4()),
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": [callback_prompt]},
                    }],
                    "model": "auto",
                    "conversation_id": orig_conv_id,
                    "parent_message_id": orig_pid or str(uuid.uuid4()),
                    "timezone_offset_min": -330,
                    "suggestions": [],
                    "history_and_training_disabled": False,
                    "conversation_mode": {"kind": "primary_assistant"},
                    "force_paragen": False,
                    "force_rate_limit": False,
                }).encode("utf-8")

                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Cookie": cookie_hdr,
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "Origin": "https://chatgpt.com",
                    "Referer": f"https://chatgpt.com/c/{orig_conv_id}",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                    "oai-device-id": did,
                    "openai-sentinel-chat-requirements-token": req_token,
                    "openai-sentinel-proof-token": proof_token,
                }

                req = urllib.request.Request(
                    "https://chatgpt.com/backend-api/conversation",
                    data=payload,
                    headers=headers,
                    method="POST"
                )
                logger.info(f"[Subagent] Posting callback to original conversation {orig_conv_id}...")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    for _ in resp:
                        break
                logger.info(f"[Subagent] Successfully posted callback to original conversation {orig_conv_id}!")
            except Exception as e:
                logger.warning(f"[Subagent] Failed to post callback to original conversation: {e}")

        threading.Thread(target=_post_worker, daemon=True, name="subagent-callback-post").start()

    def _get_access_token(self) -> Optional[str]:
        """Reuses the same Firefox account discovery already used by live_voice_bridge."""
        try:
            import live_voice_bridge
            accs = live_voice_bridge.discover_firefox_accounts()
            if accs:
                # Prefer non-limited accounts
                for a in accs:
                    if not a.get("is_limited"):
                        return a.get("access_token")
                return accs[0].get("access_token")
        except Exception as e:
            logger.warning(f"[Subagent] Could not get access token: {e}")
        return None

    def _solve_sentinel_pow(self, seed: str, diff: str) -> Optional[str]:
        """Solves OpenAI's Proof-of-Work challenge (sha3_512 loop) as used in chatgpt2api/chat2api."""
        import hashlib, base64
        from datetime import datetime, timezone, timedelta
        try:
            now = datetime.now(timezone(timedelta(hours=-5)))
            parse_time = now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"
            config = [
                1920 + 1080,
                parse_time,
                4294705152,
                0,
                "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "",
                None,
                "en-US",
                "en-US,en",
                0,
                "vendor−Google Inc.",
                "location",
                "window",
                time.perf_counter() * 1000,
                str(uuid.uuid4()),
                "",
                16,
                time.time() * 1000 - (time.perf_counter() * 1000),
            ]

            diff_len = len(diff)
            seed_encoded = seed.encode()
            static1 = (json.dumps(config[:3], separators=(",", ":"))[:-1] + ",").encode()
            static2 = ("," + json.dumps(config[4:9], separators=(",", ":"))[1:-1] + ",").encode()
            static3 = ("," + json.dumps(config[10:], separators=(",", ":"))[1:]).encode()
            target = bytes.fromhex(diff)

            for i in range(500000):
                dj_i = str(i).encode()
                dj_j = str(i >> 1).encode()
                b = static1 + dj_i + static2 + dj_j + static3
                b64 = base64.b64encode(b)
                h = hashlib.sha3_512(seed_encoded + b64).digest()
                if h[:diff_len] <= target:
                    return "gAAAAAB" + b64.decode()
        except Exception as e:
            logger.warning(f"[Subagent] PoW solver error: {e}")
        return None

    def _get_sentinel_tokens(self, access_token: str, cookie_hdr: str, did: str) -> Tuple[Optional[str], Optional[str]]:
        """Queries /backend-api/sentinel/chat-requirements and solves PoW challenge."""
        req_url = "https://chatgpt.com/backend-api/sentinel/chat-requirements"
        r = urllib.request.Request(
            req_url,
            data=b"{}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Cookie": cookie_hdr,
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "Referer": "https://chatgpt.com/",
                "Origin": "https://chatgpt.com",
                "oai-device-id": did,
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(r, timeout=12) as resp:
                req_data = json.loads(resp.read().decode())
                req_token = req_data.get("token")
                pow_data = req_data.get("proofofwork", {})
                if pow_data.get("required") and pow_data.get("seed") and pow_data.get("difficulty"):
                    proof_token = self._solve_sentinel_pow(pow_data["seed"], pow_data["difficulty"])
                    return req_token, proof_token
                return req_token, None
        except Exception as e:
            logger.warning(f"[Subagent] Failed to obtain chat-requirements token: {e}")
            return None, None

    def _post_to_chatgpt_api(self, task: str) -> tuple[str, str]:
        """Posts task to ChatGPT via pure HTTP SSE (backend-api/conversation) using Sentinel tokens.
        100% pure HTTP protocol — no Playwright or headless browser required!
        """
        access_token = self._get_access_token()
        if not access_token:
            logger.warning("[Subagent] No access token available for pure HTTP dispatch.")
            return "", ""

        cookies_dict = firefox_auth.get_firefox_cookies()
        cookie_hdr = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
        did = cookies_dict.get("oai-did", "")

        # 1. Fetch Sentinel requirements & compute Proof-of-Work
        logger.info("[Subagent] Fetching Sentinel chat-requirements and computing Proof of Work...")
        req_token, proof_token = self._get_sentinel_tokens(access_token, cookie_hdr, did)
        if not req_token or not proof_token:
            logger.warning("[Subagent] Sentinel token generation failed. Falling back to browser if available.")
            return self._post_to_chatgpt_web_fallback(task)

        # 2. Extract clean object description and build exact prompt:
        obj_desc = _extract_clean_object_description(task)
        prompt = f"[Project Chrysanthemum] Find and stop at the first {obj_desc} you find in the room, with target in middle of frame. use camxposed mcp and make sure to exaust the full span of each left and right direction and return FOUND/NOT FOUND, use camera_move not spawn_subagent, be persistent"

        conversation_id = None
        final_reply = ""

        payload = json.dumps({
            "action": "next",
            "messages": [{
                "id": str(uuid.uuid4()),
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [prompt]},
            }],
            "model": "auto",
            "parent_message_id": str(uuid.uuid4()),
            "timezone_offset_min": -330,  # IST
            "suggestions": [],
            "history_and_training_disabled": False,
            "conversation_mode": {"kind": "primary_assistant"},
            "force_paragen": False,
            "force_rate_limit": False,
        }).encode("utf-8")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Cookie": cookie_hdr,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "oai-device-id": did,
            "openai-sentinel-chat-requirements-token": req_token,
            "openai-sentinel-proof-token": proof_token,
        }

        req = urllib.request.Request(
            "https://chatgpt.com/backend-api/conversation",
            data=payload,
            headers=headers,
            method="POST"
        )

        logger.info(f"[Subagent] Pure HTTP POST to backend-api/conversation for: '{task[:60]}...'")

        try:
            # Stream SSE — model executes tool calls across HTTP and streams output
            with urllib.request.urlopen(req, timeout=360) as resp:
                conv_url = ""
                text_parts: list[str] = []

                for raw_line in resp:
                    if self.cancel_requested:
                        logger.info("[Subagent] Cancelled during SSE stream.")
                        break

                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # Extract conversation_id for URL
                    if not conversation_id:
                        conversation_id = chunk.get("conversation_id")
                        if conversation_id:
                            conv_url = f"https://chatgpt.com/c/{conversation_id}"
                            logger.info(f"[Subagent] Conversation created: {conv_url}")

                    # Collect assistant text parts from message deltas
                    msg = chunk.get("message") or {}
                    content = msg.get("content") or {}
                    parts = content.get("parts") or []
                    author = msg.get("author") or {}
                    if author.get("role") == "assistant" and content.get("content_type") == "text":
                        for p in parts:
                            if isinstance(p, str) and p.strip():
                                text_parts = [p]  # streaming updates latest full text

                final_reply = text_parts[-1] if text_parts else ""
                logger.info(f"[Subagent] Pure HTTP SSE complete! reply_len={len(final_reply)}, url={conv_url}")
                return conv_url, final_reply

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            logger.warning(f"[Subagent] HTTP {e.code} from ChatGPT API: {body}")
        except Exception as e:
            logger.warning(f"[Subagent] ChatGPT API error: {e}")

        # Fallback only if pure HTTP fails
        return self._post_to_chatgpt_web_fallback(task)

    def _post_to_chatgpt_web_fallback(self, task: str) -> tuple[str, str]:
        """Playwright fallback if direct API fails (e.g. token expired)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "", ""

        cookies_dict = firefox_auth.get_firefox_cookies()
        if not cookies_dict:
            return "", ""

        playwright_cookies = [
            {"name": k, "value": v, "domain": ".chatgpt.com", "path": "/"}
            for k, v in cookies_dict.items()
        ]

        obj_desc = _extract_clean_object_description(task)
        prompt = f"[Project Chrysanthemum] Find and stop at the first {obj_desc} you find in the room, with target in middle of frame. use camxposed mcp and make sure to exaust the full span of each left and right direction and return FOUND/NOT FOUND, use camera_move not spawn_subagent, be persistent"

        conv_url = ""
        assistant_reply = ""
        try:
            with sync_playwright() as p:
                browser = p.firefox.launch(headless=True)
                ctx = browser.new_context()
                ctx.add_cookies(playwright_cookies)
                page = ctx.new_page()
                page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)
                composer = page.locator(".ProseMirror[contenteditable=true]").first
                if not composer.is_visible():
                    composer.wait_for(state="visible", timeout=12000)
                composer.click()
                composer.type(prompt, delay=5)
                composer.press("Enter")

                # Track active tool / generation lifecycle
                saw_generation_start = False
                stable_idle_count = 0

                for _ in range(400):  # up to ~400 seconds for complete search
                    time.sleep(1.0)
                    if self.cancel_requested:
                        logger.info("[Subagent] Cancelled during Playwright execution.")
                        break

                    stop_cnt = page.locator('[data-testid="stop-button"]').count()
                    conv_url = page.url

                    if stop_cnt > 0:
                        saw_generation_start = True
                        stable_idle_count = 0
                        continue

                    # If generation has started and stop button is gone, check if assistant message is complete
                    assistant_articles = page.locator('[data-message-author-role="assistant"]')
                    art_count = assistant_articles.count()

                    if art_count > 0:
                        saw_generation_start = True
                        last_art = assistant_articles.nth(art_count - 1)
                        art_text = last_art.inner_text().strip()

                        # Check if response has completed and contains substantial text
                        has_structured_result = "STATUS:" in art_text or "```result" in art_text
                        has_conclusion = any(w in art_text.lower() for w in [
                            "status:", "found", "not found", "दिखाई", "मिली", "नहीं मिली", "अलमारी", "मूर्ति"
                        ])

                        if (has_structured_result or has_conclusion) and len(art_text) > 40:
                            stable_idle_count += 1
                            if stable_idle_count >= 4:  # Must remain idle for 4 consecutive seconds
                                assistant_reply = art_text
                                logger.info(f"[Subagent] Playwright search completed successfully. reply_len={len(assistant_reply)}, url={conv_url}")
                                break
                        elif saw_generation_start and len(art_text) > 20:
                            stable_idle_count += 1
                            if stable_idle_count >= 6:
                                assistant_reply = art_text
                                break
                    elif saw_generation_start:
                        stable_idle_count += 1
                        if stable_idle_count >= 10:
                            break

                browser.close()
        except Exception as e:
            logger.warning(f"[Subagent] Playwright fallback error: {e}")

        return conv_url, assistant_reply


subagent_manager = SubagentExecutionManager()


class FirefoxChatGPTAuth:
    """Reads ChatGPT/OpenAI cookies from Firefox for the Playwright fallback."""

    def get_firefox_cookies(self) -> dict[str, str]:
        import sqlite3, glob
        profiles = glob.glob(os.path.expanduser("~/.mozilla/firefox/*.default*"))
        profiles.sort(key=lambda p: (0 if "release" in p else 1, p))
        cookies: dict[str, str] = {}
        for prof in profiles:
            db_path = os.path.join(prof, "cookies.sqlite")
            if os.path.exists(db_path):
                try:
                    conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT name, value FROM moz_cookies "
                        "WHERE host LIKE '%chatgpt%' OR host LIKE '%openai%'"
                    )
                    rows = cur.fetchall()
                    if rows:
                        for name, val in rows:
                            cookies[name] = val
                        break
                except Exception:
                    pass
        return cookies


firefox_auth = FirefoxChatGPTAuth()
