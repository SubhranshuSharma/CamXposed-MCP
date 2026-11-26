#!/usr/bin/env python3
"""Regression test to guarantee canonical prompts, canary tokens, and tool schemas remain locked.

If any of these assertions fail, DO NOT silently modify them — ask the user for confirmation first.
"""
import inspect
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class TestPromptAndSchemaIntegrity(unittest.TestCase):
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

    def test_spawn_subagent_docstring(self):
        """spawn_subagent docstring must retain clean, non-lecturing canonical description."""
        import server
        doc = server.spawn_subagent.__doc__ or ""
        self.assertIn(
            "Search the room for an object, person, or target using an autonomous camera subagent.",
            doc,
        )
        self.assertIn(
            "Call this tool whenever the user asks to find, locate, search for, or spot anything in the room",
            doc,
        )
        self.assertNotIn(
            "CRITICAL USAGE RESTRICTION",
            doc,
            "REGRESSION: Do not lecture the agent in tool docstrings! Catch and block under the hood instead.",
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
        """CANARY_TOKENS must contain both canary trigger tokens."""
        import subagent_engine
        mgr = subagent_engine.SubagentExecutionManager()
        # Verify canary detection on test input
        res1 = mgr.start_subagent_task("cupboard", caller_identity="live", previous_instructions="project chrysanthemum")
        self.assertEqual(res1.get("error_code"), "IS_SUBAGENT", "Canary 'chrysanthemum' failed to block subagent!")

        res2 = mgr.start_subagent_task("cupboard", caller_identity="live", previous_instructions="find and stop at the first")
        self.assertEqual(res2.get("error_code"), "IS_SUBAGENT", "Canary 'find and stop at the first' failed to block subagent!")

    def test_chat_caller_is_blocked_from_spawning(self):
        """Text-chat callers (caller_identity='chat') must be blocked under the hood without lecturing docstrings."""
        import subagent_engine
        mgr = subagent_engine.SubagentExecutionManager()
        res = mgr.start_subagent_task("cupboard", caller_identity="chat", previous_instructions="search room")
        self.assertEqual(res.get("error_code"), "IS_SUBAGENT", "Chat caller must be blocked from spawning subagents under the hood!")


if __name__ == "__main__":
    unittest.main()
