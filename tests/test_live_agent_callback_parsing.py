import io
import json
import io
import os
import unittest

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

import lambda_o3_live_agent as live_agent


class FakeLambdaClient:
    def __init__(self, payload):
        self.payload = payload
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        return {
            "StatusCode": 200,
            "Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8")),
        }


class LiveAgentCallbackParsingTests(unittest.TestCase):
    def test_empty_api_gateway_handoff_is_rejected_before_webhook(self):
        original_call_webhook = live_agent.call_webhook
        calls = []

        try:
            live_agent.call_webhook = lambda payload: calls.append(payload) or {"ok": True, "status": 200}
            response = live_agent.lambda_handler({
                "body": json.dumps({}),
                "headers": {},
            }, None)

            body = json.loads(response["body"])
            self.assertEqual(response["statusCode"], 400)
            self.assertFalse(body["ok"])
            self.assertEqual(body["error_code"], "invalid_live_agent_handoff_payload")
            self.assertEqual(
                body["missing_fields"],
                ["session_id", "slack_channel", "slack_user", "description_or_conversation_summary"],
            )
            self.assertEqual(calls, [])
        finally:
            live_agent.call_webhook = original_call_webhook

    def test_valid_api_gateway_handoff_calls_webhook_with_canonical_context(self):
        original_call_webhook = live_agent.call_webhook
        calls = []

        try:
            live_agent.call_webhook = lambda payload: calls.append(payload) or {"ok": True, "status": 200}
            response = live_agent.lambda_handler({
                "body": json.dumps({
                    "session_id": "issue:D123:178000.900",
                    "session_root_ts": "178000.900",
                    "slack_channel": "D123",
                    "slack_thread_ts": "",
                    "slack_user": "U123",
                    "description": "Need live agent support",
                }),
                "headers": {},
            }, None)

            body = json.loads(response["body"])
            self.assertEqual(response["statusCode"], 200)
            self.assertTrue(body["ok"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["session_id"], "issue:D123:178000.900")
            self.assertEqual(calls[0]["session_root_ts"], "178000.900")
            self.assertEqual(calls[0]["slack_channel"], "D123")
            self.assertEqual(calls[0]["slack_thread_ts"], "")
            self.assertEqual(calls[0]["slack_user"], "U123")
            self.assertEqual(calls[0]["requestType"], "live_agent")
            self.assertEqual(calls[0]["branching"], "live_agent")
            self.assertEqual(calls[0]["slack"]["channelId"], "D123")
            self.assertEqual(calls[0]["slack"]["threadTs"], "")
            self.assertEqual(calls[0]["slack"]["userId"], "U123")
        finally:
            live_agent.call_webhook = original_call_webhook

    def test_new_ticket_created_payload_is_normalized(self):
        payload = {
            "event_type": "live_agent_ticket_created",
            "ticket_key": "IVY-55",
            "ticket_url": "https://innovyq.atlassian.net/browse/IVY-55",
            "ticket_status": "To Do",
            "assignee_account_id": "712020:agent",
            "assignee_display_name": "Karan Prajapat",
            "assignee_email": "karan@example.com",
            "slack_channel": "D123",
            "slack_thread_ts": "178000.100",
            "slack_user": "U123",
            "session_id": "issue:D123:178000.100",
            "user_request": "Need live agent support",
        }

        callback = live_agent.normalize_callback(payload)

        self.assertEqual(callback["event_type"], "live_agent_ticket_created")
        self.assertEqual(callback["ticket_key"], "IVY-55")
        self.assertEqual(callback["ticket_url"], "https://innovyq.atlassian.net/browse/IVY-55")
        self.assertEqual(callback["ticket_status"], "To Do")
        self.assertEqual(callback["assignee_account_id"], "712020:agent")
        self.assertEqual(callback["assignee_display_name"], "Karan Prajapat")
        self.assertEqual(callback["assignee_email"], "karan@example.com")
        self.assertEqual(callback["slack_channel"], "D123")
        self.assertEqual(callback["slack_thread_ts"], "178000.100")
        self.assertEqual(callback["slack_user"], "U123")
        self.assertEqual(callback["session_id"], "issue:D123:178000.100")
        self.assertEqual(callback["user_request"], "Need live agent support")

    def test_existing_nested_payload_aliases_still_work(self):
        payload = {
            "event_type": "live_agent_ticket_created",
            "sessionId": "legacy-session",
            "createdIssue": {
                "key": "IVY-56",
                "status": {"name": "Open"},
            },
            "issue": {
                "fields": {
                    "assignee": {
                        "accountId": "712020:nested",
                        "displayName": "Vibhu Jain",
                        "emailAddress": "vibhu@example.com",
                    },
                    "project": {"key": "IVY"},
                }
            },
            "slack": {
                "channelId": "D999",
                "threadTs": "178000.200",
                "userId": "U999",
            },
        }

        callback = live_agent.normalize_callback(payload)

        self.assertEqual(callback["ticket_key"], "IVY-56")
        self.assertEqual(callback["ticket_status"], "Open")
        self.assertEqual(callback["jira_project"], "IVY")
        self.assertEqual(callback["assignee_account_id"], "712020:nested")
        self.assertEqual(callback["assignee_display_name"], "Vibhu Jain")
        self.assertEqual(callback["assignee_email"], "vibhu@example.com")
        self.assertEqual(callback["slack_channel"], "D999")
        self.assertEqual(callback["slack_thread_ts"], "178000.200")
        self.assertEqual(callback["slack_user"], "U999")
        self.assertEqual(callback["session_id"], "legacy-session")

    def test_ticket_created_validation_requires_slack_and_session_fields(self):
        result = live_agent.handle_jsm_callback({
            "event_type": "live_agent_ticket_created",
            "ticket_key": "IVY-57",
            "slack_channel": "D123",
            "session_id": "issue:D123:178000.300",
        })

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "missing_slack_user")
        self.assertEqual(result["missing_fields"], ["slack_user"])

    def test_missing_assignee_invokes_oncall_helper(self):
        original_function = live_agent.ONCALL_USER_FUNCTION
        original_client = live_agent.lambda_client

        try:
            live_agent.ONCALL_USER_FUNCTION = "o3_jsm_oncall_user"
            fake_client = FakeLambdaClient({
                "ok": True,
                "assignee": {
                    "account_id": "712020:helper",
                    "display_name": "Resolved Agent",
                    "email": "resolved@example.com",
                },
            })
            live_agent.lambda_client = fake_client

            callback = live_agent.normalize_callback({
                "event_type": "live_agent_ticket_created",
                "ticket_key": "IVY-58",
                "slack_channel": "D123",
                "slack_user": "U123",
                "session_id": "issue:D123:178000.400",
            })
            enriched = live_agent.enrich_missing_assignee(callback)

            self.assertEqual(enriched["assignee_account_id"], "712020:helper")
            self.assertEqual(enriched["assignee_display_name"], "Resolved Agent")
            self.assertEqual(enriched["assignee_email"], "resolved@example.com")
            self.assertEqual(len(fake_client.invocations), 1)
            self.assertEqual(
                json.loads(fake_client.invocations[0]["Payload"].decode("utf-8"))["ticket_key"],
                "IVY-58",
            )
        finally:
            live_agent.ONCALL_USER_FUNCTION = original_function
            live_agent.lambda_client = original_client

    def test_ticket_created_lock_acquired_response(self):
        original_update_session = live_agent.update_live_agent_session
        original_put_pointer = live_agent.put_live_agent_ticket_pointer
        original_acquire_confirmation = live_agent.acquire_slack_confirmation
        original_oncall = live_agent.get_current_oncall_user
        original_acquire_lock = live_agent.chat_locks.acquire_chat_lock
        original_enqueue = live_agent.chat_locks.enqueue_live_agent_request

        try:
            live_agent.update_live_agent_session = lambda callback: None
            live_agent.put_live_agent_ticket_pointer = lambda callback: f"live_agent_ticket:{callback['ticket_key']}"
            live_agent.acquire_slack_confirmation = lambda callback, pointer_session_id: False
            live_agent.get_current_oncall_user = lambda ticket_key=None, webhook_payload=None: {
                "ok": True,
                "source": "webhook_assignee",
                "account_id": "712020:agent",
                "display_name": "Karan",
                "email": "karan@example.com",
                "ticket_key": ticket_key,
            }
            live_agent.chat_locks.acquire_chat_lock = lambda agent, ticket, slack_context: {
                "ok": True,
                "already_locked": False,
                "ticket_key": ticket["ticket_key"],
                "agent_account_id": agent["account_id"],
            }
            live_agent.chat_locks.enqueue_live_agent_request = lambda ticket, slack_context, reason: (_ for _ in ()).throw(AssertionError("queue should not run"))
            result = live_agent.handle_jsm_callback({
                "event_type": "live_agent_ticket_created",
                "ticket_key": "IVY-59",
                "ticket_url": "https://innovyq.atlassian.net/browse/IVY-59",
                "slack_channel": "D123",
                "slack_user": "U123",
                "session_id": "issue:D123:178000.500",
            })

            self.assertTrue(result["ok"])
            self.assertTrue(result["lock_acquired"])
            self.assertFalse(result["queued"])
            self.assertIn("assigned to Karan", result["reply"])
        finally:
            live_agent.update_live_agent_session = original_update_session
            live_agent.put_live_agent_ticket_pointer = original_put_pointer
            live_agent.acquire_slack_confirmation = original_acquire_confirmation
            live_agent.get_current_oncall_user = original_oncall
            live_agent.chat_locks.acquire_chat_lock = original_acquire_lock
            live_agent.chat_locks.enqueue_live_agent_request = original_enqueue

    def test_ticket_created_agent_at_capacity_queues(self):
        original_update_session = live_agent.update_live_agent_session
        original_put_pointer = live_agent.put_live_agent_ticket_pointer
        original_acquire_confirmation = live_agent.acquire_slack_confirmation
        original_oncall = live_agent.get_current_oncall_user
        original_acquire_lock = live_agent.chat_locks.acquire_chat_lock
        original_enqueue = live_agent.chat_locks.enqueue_live_agent_request

        try:
            live_agent.update_live_agent_session = lambda callback: None
            live_agent.put_live_agent_ticket_pointer = lambda callback: f"live_agent_ticket:{callback['ticket_key']}"
            live_agent.acquire_slack_confirmation = lambda callback, pointer_session_id: False
            live_agent.get_current_oncall_user = lambda ticket_key=None, webhook_payload=None: {
                "ok": True,
                "source": "jira_issue_assignee",
                "account_id": "712020:agent",
                "display_name": "Karan",
                "email": "karan@example.com",
                "ticket_key": ticket_key,
            }
            live_agent.chat_locks.acquire_chat_lock = lambda agent, ticket, slack_context: {
                "ok": False,
                "reason": "agent_at_capacity",
                "active_count": 5,
            }
            live_agent.chat_locks.enqueue_live_agent_request = lambda ticket, slack_context, reason: {
                "ok": True,
                "already_queued": False,
                "ticket_key": ticket["ticket_key"],
                "reason": reason,
            }

            result = live_agent.handle_jsm_callback({
                "event_type": "live_agent_ticket_created",
                "ticket_key": "IVY-60",
                "slack_channel": "D123",
                "slack_user": "U123",
                "session_id": "issue:D123:178000.600",
            })

            self.assertTrue(result["ok"])
            self.assertFalse(result["lock_acquired"])
            self.assertTrue(result["queued"])
            self.assertEqual(result["queue"]["reason"], "agent_at_capacity")
            self.assertIn("currently busy", result["reply"])
        finally:
            live_agent.update_live_agent_session = original_update_session
            live_agent.put_live_agent_ticket_pointer = original_put_pointer
            live_agent.acquire_slack_confirmation = original_acquire_confirmation
            live_agent.get_current_oncall_user = original_oncall
            live_agent.chat_locks.acquire_chat_lock = original_acquire_lock
            live_agent.chat_locks.enqueue_live_agent_request = original_enqueue

    def test_ticket_created_no_oncall_queues(self):
        original_update_session = live_agent.update_live_agent_session
        original_put_pointer = live_agent.put_live_agent_ticket_pointer
        original_acquire_confirmation = live_agent.acquire_slack_confirmation
        original_oncall = live_agent.get_current_oncall_user
        original_acquire_lock = live_agent.chat_locks.acquire_chat_lock
        original_enqueue = live_agent.chat_locks.enqueue_live_agent_request

        try:
            live_agent.update_live_agent_session = lambda callback: None
            live_agent.put_live_agent_ticket_pointer = lambda callback: f"live_agent_ticket:{callback['ticket_key']}"
            live_agent.acquire_slack_confirmation = lambda callback, pointer_session_id: False
            live_agent.get_current_oncall_user = lambda ticket_key=None, webhook_payload=None: {
                "ok": False,
                "reason": "no_oncall_user_available",
                "ticket_key": ticket_key,
            }
            live_agent.chat_locks.acquire_chat_lock = lambda agent, ticket, slack_context: (_ for _ in ()).throw(AssertionError("lock should not run"))
            live_agent.chat_locks.enqueue_live_agent_request = lambda ticket, slack_context, reason: {
                "ok": True,
                "already_queued": False,
                "ticket_key": ticket["ticket_key"],
                "reason": reason,
            }

            result = live_agent.handle_jsm_callback({
                "event_type": "live_agent_ticket_created",
                "ticket_key": "IVY-61",
                "slack_channel": "D123",
                "slack_user": "U123",
                "session_id": "issue:D123:178000.700",
            })

            self.assertTrue(result["ok"])
            self.assertFalse(result["lock_acquired"])
            self.assertTrue(result["queued"])
            self.assertEqual(result["queue"]["reason"], "no_oncall_user")
            self.assertIn("no on-call agent", result["reply"])
        finally:
            live_agent.update_live_agent_session = original_update_session
            live_agent.put_live_agent_ticket_pointer = original_put_pointer
            live_agent.acquire_slack_confirmation = original_acquire_confirmation
            live_agent.get_current_oncall_user = original_oncall
            live_agent.chat_locks.acquire_chat_lock = original_acquire_lock
            live_agent.chat_locks.enqueue_live_agent_request = original_enqueue


if __name__ == "__main__":
    unittest.main()
