import os
import unittest

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

from botocore.exceptions import ClientError

import lambda_o3_live_agent as live_agent


def conditional_failed():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "condition failed"}},
        "FakeOperation",
    )


def eval_condition(expression, item):
    if expression is None:
        return True

    if not hasattr(expression, "get_expression"):
        return True

    data = expression.get_expression()
    operator = data.get("operator")
    values = data.get("values", ())

    if operator == "AND":
        return all(eval_condition(value, item) for value in values)

    if operator == "OR":
        return any(eval_condition(value, item) for value in values)

    if operator == "=":
        attr, expected = values
        return item.get(attr.name) == expected

    if operator == "<>":
        attr, expected = values
        return item.get(attr.name) != expected

    if operator == "attribute_not_exists":
        attr = values[0]
        return attr.name not in item

    return True


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["lock_id"] if "lock_id" in Key else Key["session_id"] if "session_id" in Key else Key["oncall_key"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item.get("lock_id") or Item.get("session_id") or Item.get("oncall_key")
        existing = self.items.get(key)
        if existing and not eval_condition(ConditionExpression, existing):
            raise conditional_failed()
        if existing and Item.get("record_type") == "ticket_lock" and ConditionExpression is not None:
            raise conditional_failed()
        self.items[key] = dict(Item)
        return {}

    def update_item(self, Key, UpdateExpression=None, ExpressionAttributeValues=None, ConditionExpression=None, **kwargs):
        key = Key.get("lock_id") or Key.get("session_id") or Key.get("oncall_key")
        item = self.items.setdefault(key, dict(Key))
        if not eval_condition(ConditionExpression, item):
            raise conditional_failed()

        values = ExpressionAttributeValues or {}
        if ":released" in values:
            item["lock_status"] = values[":released"]
            item["released_at"] = values.get(":now")
        if ":active" in values:
            item["lock_status"] = values[":active"]
            item["activated_at"] = values.get(":now")
        if ":slot_lock_id" in values:
            item["slot_lock_id"] = values[":slot_lock_id"]
        if ":slot_number" in values:
            item["slot_number"] = values[":slot_number"]
        if ":capacity_status" in values:
            item["live_agent_capacity_status"] = values[":capacity_status"]
        if ":queued_status" in values:
            item["live_agent_status"] = values[":queued_status"]
        if ":posting" in values:
            item["live_agent_queue_promotion_slack_status"] = values[":posting"]
            item["live_agent_slack_confirmation_status"] = values[":posting"]
        if ":status" in values:
            item["live_agent_queue_promotion_slack_status"] = values[":status"]
            item["live_agent_slack_confirmation_status"] = values[":status"]
        if ":now" in values:
            item["updated_at"] = values[":now"]
        if ":ttl" in values:
            item["ttl"] = values[":ttl"]
        return {}

    def scan(self, FilterExpression=None, Limit=None, **kwargs):
        items = [
            dict(item) for item in self.items.values()
            if eval_condition(FilterExpression, item)
        ]
        if Limit:
            items = items[:Limit]
        return {"Items": items}


def callback(ticket_key, account_id="agent-1", session_id=None, slack_channel=""):
    return {
        "session_id": session_id or f"issue:D123:{ticket_key}",
        "ticket_key": ticket_key,
        "ticket_url": f"https://innovyq.atlassian.net/browse/{ticket_key}",
        "slack_channel": slack_channel,
        "slack_thread_ts": "",
        "slack_user": "U123",
        "assignee_account_id": account_id,
        "assignee_email": "",
        "assignee_display_name": "Agent One",
    }


class LiveAgentCapacityTests(unittest.TestCase):
    def setUp(self):
        self.lock_table = FakeTable()
        self.session_table = FakeTable()
        self.oncall_table = FakeTable()
        self.original_lock_table = live_agent.agent_chat_lock_table
        self.original_session_table = live_agent.session_table
        self.original_oncall_table = live_agent.oncall_user_table
        self.original_max = live_agent.LIVE_AGENT_MAX_ACTIVE_CHATS
        live_agent.agent_chat_lock_table = self.lock_table
        live_agent.session_table = self.session_table
        live_agent.oncall_user_table = self.oncall_table
        live_agent.LIVE_AGENT_MAX_ACTIVE_CHATS = 2

    def tearDown(self):
        live_agent.agent_chat_lock_table = self.original_lock_table
        live_agent.session_table = self.original_session_table
        live_agent.oncall_user_table = self.original_oncall_table
        live_agent.LIVE_AGENT_MAX_ACTIVE_CHATS = self.original_max

    def test_capacity_creates_active_locks_until_limit_then_queues(self):
        first = live_agent.apply_chat_capacity(callback("IVY-1"), "live_agent_ticket:IVY-1")
        second = live_agent.apply_chat_capacity(callback("IVY-2"), "live_agent_ticket:IVY-2")
        third = live_agent.apply_chat_capacity(callback("IVY-3"), "live_agent_ticket:IVY-3")

        self.assertEqual(first["status"], "active")
        self.assertEqual(second["status"], "active")
        self.assertEqual(third["status"], "waiting")
        self.assertEqual(live_agent.get_chat_lock("IVY-3")["lock_status"], "waiting")

    def test_duplicate_ticket_callback_reuses_existing_lock(self):
        first = live_agent.apply_chat_capacity(callback("IVY-10"), "live_agent_ticket:IVY-10")
        duplicate = live_agent.apply_chat_capacity(callback("IVY-10"), "live_agent_ticket:IVY-10")

        self.assertEqual(first["status"], "active")
        self.assertEqual(duplicate["status"], "active")
        self.assertTrue(duplicate["duplicate"])

    def test_release_promotes_oldest_waiting_ticket(self):
        live_agent.apply_chat_capacity(callback("IVY-1"), "live_agent_ticket:IVY-1")
        live_agent.apply_chat_capacity(callback("IVY-2"), "live_agent_ticket:IVY-2")
        live_agent.apply_chat_capacity(callback("IVY-3"), "live_agent_ticket:IVY-3")

        release = live_agent.release_chat_lock_for_ticket("IVY-1")
        promotion = live_agent.promote_oldest_waiting_chat(release["agent_key"])

        self.assertTrue(release["released"])
        self.assertTrue(promotion["promoted"])
        self.assertEqual(promotion["ticket_key"], "IVY-3")
        self.assertEqual(live_agent.get_chat_lock("IVY-3")["lock_status"], "active")

    def test_oncall_cache_used_when_callback_has_no_assignee(self):
        self.oncall_table.items["current"] = {
            "oncall_key": "current",
            "account_id": "cached-agent",
            "display_name": "Cached Agent",
        }
        data = callback("IVY-20", account_id="")
        data["assignee_account_id"] = ""
        data["assignee_display_name"] = ""

        result = live_agent.apply_chat_capacity(data, "live_agent_ticket:IVY-20")

        self.assertEqual(result["status"], "active")
        self.assertEqual(result["agent_key"], "cached-agent")


if __name__ == "__main__":
    unittest.main()
