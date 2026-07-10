import os
import unittest

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

import lambda_o3_live_agent as live_agent


def queue_item(ticket_key="IVY-70"):
    return {
        "PK": "QUEUE#live_agent",
        "SK": f"WAITING#1#{ticket_key}",
        "ticket_key": ticket_key,
        "ticket_url": f"https://example.atlassian.net/browse/{ticket_key}",
        "ticket_status": "Open",
        "session_id": "issue:D123:1",
        "slack_channel": "D123",
        "slack_thread_ts": "1",
        "slack_user": "U123",
        "status": "processing",
    }


class LiveAgentQueueDrainTests(unittest.TestCase):
    def setUp(self):
        self.originals = {
            "max_drain": live_agent.MAX_QUEUE_DRAIN_PER_INVOCATION,
            "pop": live_agent.chat_locks.pop_oldest_waiting_request,
            "oncall": live_agent.get_current_oncall_user,
            "acquire": live_agent.chat_locks.acquire_chat_lock,
            "assign_jira": live_agent.assign_jira_ticket,
            "mark_assigned": live_agent.chat_locks.mark_queue_item_assigned,
            "mark_waiting": live_agent.chat_locks.mark_queue_item_waiting_again,
            "mark_failed": live_agent.chat_locks.mark_queue_item_failed,
            "release": live_agent.chat_locks.release_chat_lock,
            "slack": live_agent.post_slack_message,
            "pointer": live_agent.get_live_agent_ticket_pointer,
            "marker": live_agent.create_live_agent_event_marker,
            "update_pointer": live_agent.update_status_pointer,
            "update_target": live_agent.update_status_target_session,
        }
        live_agent.MAX_QUEUE_DRAIN_PER_INVOCATION = 1
        live_agent.get_current_oncall_user = lambda ticket_key=None: {
            "ok": True,
            "account_id": "agent-1",
            "display_name": "Karan",
            "email": "karan@example.com",
        }
        live_agent.post_slack_message = lambda channel, text, thread_ts="": {
            "attempted": True,
            "ts": "2",
            "text": text,
        }

    def tearDown(self):
        live_agent.MAX_QUEUE_DRAIN_PER_INVOCATION = self.originals["max_drain"]
        live_agent.chat_locks.pop_oldest_waiting_request = self.originals["pop"]
        live_agent.get_current_oncall_user = self.originals["oncall"]
        live_agent.chat_locks.acquire_chat_lock = self.originals["acquire"]
        live_agent.assign_jira_ticket = self.originals["assign_jira"]
        live_agent.chat_locks.mark_queue_item_assigned = self.originals["mark_assigned"]
        live_agent.chat_locks.mark_queue_item_waiting_again = self.originals["mark_waiting"]
        live_agent.chat_locks.mark_queue_item_failed = self.originals["mark_failed"]
        live_agent.chat_locks.release_chat_lock = self.originals["release"]
        live_agent.post_slack_message = self.originals["slack"]
        live_agent.get_live_agent_ticket_pointer = self.originals["pointer"]
        live_agent.create_live_agent_event_marker = self.originals["marker"]
        live_agent.update_status_pointer = self.originals["update_pointer"]
        live_agent.update_status_target_session = self.originals["update_target"]

    def test_resolved_ticket_releases_capacity_and_assigns_oldest_queued_ticket(self):
        oldest = queue_item("IVY-70")
        assigned = []
        operations = []
        live_agent.chat_locks.pop_oldest_waiting_request = lambda: oldest
        live_agent.chat_locks.acquire_chat_lock = lambda agent, ticket, slack: {
            "ok": True,
            "ticket_key": ticket["ticket_key"],
        }
        live_agent.assign_jira_ticket = lambda ticket_key, account_id: {
            "ok": True,
            "status": 204,
        }
        live_agent.chat_locks.mark_queue_item_assigned = (
            lambda item, agent, ticket_key: (
                operations.append("assign"),
                assigned.append(ticket_key),
            )
        )
        live_agent.chat_locks.release_chat_lock = (
            lambda ticket_key, close_reason: operations.append("release")
            or {"ok": True, "released": True, "agent_account_id": "agent-1"}
        )
        live_agent.get_live_agent_ticket_pointer = lambda ticket_key: {
            "target_session_id": "issue:D123:closed",
            "slack_channel": "",
            "slack_thread_ts": "",
        }
        live_agent.create_live_agent_event_marker = lambda callback, pointer_id: {
            "created": True,
            "session_id": "marker-1",
        }
        live_agent.update_status_pointer = lambda pointer_id, callback: None
        live_agent.update_status_target_session = lambda target_id, callback: None

        result = live_agent.handle_live_agent_status_changed({
            "event_type": "live_agent_status_changed",
            "ticket_key": "IVY-69",
            "ticket_status": "Resolved",
        })

        self.assertTrue(result["lock_release"]["released"])
        self.assertTrue(result["queue_drain"]["drained"])
        self.assertEqual(result["queue_drain"]["assignment_count"], 1)
        self.assertEqual(assigned, ["IVY-70"])
        self.assertEqual(operations, ["release", "assign"])
        self.assertIn(
            "Karan has been assigned to IVY-70",
            result["queue_drain"]["slack"]["text"],
        )

    def test_jira_assignment_failure_releases_new_lock(self):
        item = queue_item("IVY-71")
        released = []
        failed = []
        live_agent.chat_locks.pop_oldest_waiting_request = lambda: item
        live_agent.chat_locks.acquire_chat_lock = lambda agent, ticket, slack: {"ok": True}
        live_agent.assign_jira_ticket = lambda ticket_key, account_id: {
            "ok": False,
            "retryable": False,
            "reason": "jira_assignment_http_error",
            "status": 400,
        }
        live_agent.chat_locks.release_chat_lock = (
            lambda ticket_key, close_reason: released.append((ticket_key, close_reason))
            or {"ok": True, "released": True}
        )
        live_agent.chat_locks.mark_queue_item_failed = (
            lambda queue_item, reason: failed.append(reason)
        )

        result = live_agent.drain_live_agent_queue()

        self.assertFalse(result["drained"])
        self.assertEqual(released, [("IVY-71", "jira_assignment_failed")])
        self.assertEqual(failed, ["jira_assignment_http_error"])

    def test_full_current_oncall_agent_keeps_item_waiting(self):
        item = queue_item("IVY-72")
        waiting = []
        live_agent.chat_locks.pop_oldest_waiting_request = lambda: item
        live_agent.chat_locks.acquire_chat_lock = lambda agent, ticket, slack: {
            "ok": False,
            "reason": "agent_at_capacity",
            "active_count": 5,
        }
        live_agent.chat_locks.mark_queue_item_waiting_again = (
            lambda queue_item, reason: waiting.append(reason)
        )
        live_agent.assign_jira_ticket = lambda *args: self.fail("Jira assignment must not run")

        result = live_agent.drain_live_agent_queue()

        self.assertFalse(result["drained"])
        self.assertEqual(result["reason"], "agent_at_capacity")
        self.assertEqual(waiting, ["agent_at_capacity"])


if __name__ == "__main__":
    unittest.main()
