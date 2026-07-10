import os
import unittest

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("BOT_ID", "test-bot")
os.environ.setdefault("BOT_ALIAS_ID", "test-alias")
os.environ.setdefault("SLACK_BOT_TOKEN", "test-token")
os.environ.setdefault("SCREENSHOT_VECTOR_BACKEND", "dynamodb")

import lambda_o3_slack_worker as worker


class SlackWorkerClaudeToggleTests(unittest.TestCase):
    def test_fallback_routing_is_independent_of_invocation_toggle(self):
        original_enabled = worker.ENABLE_CLAUDE_FALLBACK
        try:
            worker.ENABLE_CLAUDE_FALLBACK = False
            self.assertTrue(
                worker.should_use_claude_fallback(
                    "unmatched request",
                    "FallbackIntent",
                    "ReadyForFulfillment",
                    False,
                )
            )
        finally:
            worker.ENABLE_CLAUDE_FALLBACK = original_enabled

    def test_disabled_fallback_reuses_previous_answer_as_success_text(self):
        result = worker.disabled_claude_fallback_result("Previous Lex answer")

        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "Previous Lex answer")
        self.assertIsNone(result["error"])
        self.assertTrue(result["fallback_disabled"])

    def test_disabled_fallback_without_previous_answer_is_failure(self):
        result = worker.disabled_claude_fallback_result("")

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "claude_fallback_disabled_no_previous_reply",
        )

    def test_final_support_response_keeps_existing_flow(self):
        claude_result = worker.disabled_claude_fallback_result("Previous Lex answer")
        result = worker.build_final_support_result(
            {
                "session_id": "session-1",
                "assistance_lex_intent": "FallbackIntent",
                "assistance_lex_state": "ReadyForFulfillment",
                "assistance_lex_reply": "Previous Lex answer",
                "assistance_original_text": "User question",
            },
            claude_result,
            "2026-07-09T00:00:00+00:00",
        )

        self.assertEqual(result["lex_state"], "Fulfilled")
        self.assertEqual(result["response_source"], "claude")
        self.assertIn("Previous Lex answer", result["reply"])
        self.assertEqual(result["support_claude_reply"], "Previous Lex answer")
        self.assertIsNone(result["claude_fallback_error"])


if __name__ == "__main__":
    unittest.main()
