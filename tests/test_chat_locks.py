import copy
import os
import unittest

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

from botocore.exceptions import ClientError

import chat_locks


def conditional_failed(operation="FakeOperation"):
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "condition failed"}},
        operation,
    )


def transaction_failed():
    return ClientError(
        {"Error": {"Code": "TransactionCanceledException", "Message": "transaction cancelled"}},
        "TransactWriteItems",
    )


def eval_attr_condition(condition, item):
    if condition is None:
        return True

    data = condition.get_expression()
    operator = data.get("operator")
    values = data.get("values", ())

    if operator == "AND":
        return all(eval_attr_condition(value, item) for value in values)

    if operator == "OR":
        return any(eval_attr_condition(value, item) for value in values)

    if operator == "=":
        attr, expected = values
        return item.get(attr.name) == expected

    return True


class FakeDynamoDB:
    def __init__(self):
        self.items = {}

    def key_tuple(self, key):
        return key["PK"], key["SK"]

    def get(self, key):
        return self.items.get(self.key_tuple(key))

    def set(self, item):
        self.items[(item["PK"], item["SK"])] = item


class FakeTable:
    def __init__(self, store):
        self.store = store

    def get_item(self, Key):
        item = self.store.get(Key)
        return {"Item": copy.deepcopy(item)} if item else {}

    def update_item(
        self,
        Key,
        UpdateExpression=None,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
        ConditionExpression=None,
        ReturnValues=None,
    ):
        item = self.store.get(Key)
        if item is None:
            item = dict(Key)
            self.store.set(item)

        if not eval_attr_condition(ConditionExpression, item):
            raise conditional_failed("UpdateItem")

        values = ExpressionAttributeValues or {}
        if ":processing" in values:
            item["status"] = values[":processing"]
            item["processing_started_at"] = values[":now"]
        if ":assigned" in values:
            item["status"] = values[":assigned"]
            item["assigned_at"] = values[":now"]
            item["agent_account_id"] = values[":agent_account_id"]
        if ":waiting" in values:
            item["status"] = values[":waiting"]
            item["reason"] = values.get(":reason", item.get("reason"))
        if ":failed" in values:
            item["status"] = values[":failed"]
            item["failure_reason"] = values.get(":reason", "")
            item["failed_at"] = values.get(":now")
        if ":closed" in values:
            item["status"] = values[":closed"]
            item["closed_at"] = values.get(":now")
            item["close_reason"] = values.get(":reason", "")
        if ":ttl" in values:
            item["ttl"] = values[":ttl"]
        if ":now" in values:
            item["updated_at"] = values[":now"]

        return {"Attributes": copy.deepcopy(item)} if ReturnValues == "ALL_NEW" else {}

    def query(self, KeyConditionExpression=None, ScanIndexForward=True, Limit=None):
        items = [
            copy.deepcopy(item)
            for (pk, sk), item in self.store.items.items()
            if pk == chat_locks.queue_pk() and sk.startswith("WAITING#")
        ]
        items.sort(key=lambda item: item["SK"], reverse=not ScanIndexForward)
        if Limit:
            items = items[:Limit]
        return {"Items": items}


class FakeClient:
    def __init__(self, store):
        self.store = store

    def decode_item(self, item):
        return chat_locks.from_dynamodb_item(item)

    def transact_write_items(self, TransactItems):
        new_items = copy.deepcopy(self.store.items)
        try:
            for action in TransactItems:
                if "Put" in action:
                    self.apply_put(new_items, action["Put"])
                elif "Update" in action:
                    self.apply_update(new_items, action["Update"])
        except ClientError:
            raise transaction_failed()
        self.store.items = new_items
        return {}

    def apply_put(self, items, operation):
        item = self.decode_item(operation["Item"])
        key = (item["PK"], item["SK"])
        if key in items:
            raise conditional_failed()
        items[key] = item

    def apply_update(self, items, operation):
        key = self.decode_item(operation["Key"])
        item_key = (key["PK"], key["SK"])
        item = items.get(item_key)
        values = self.decode_item(operation.get("ExpressionAttributeValues", {}))
        condition = " ".join((operation.get("ConditionExpression") or "").split())
        update_expression = " ".join((operation.get("UpdateExpression") or "").split())

        if condition == "attribute_not_exists(active_count) OR active_count < :max":
            active_count = int((item or {}).get("active_count", 0))
            if item and active_count >= values[":max"]:
                raise conditional_failed()
            item = item or dict(key)
            item["active_count"] = active_count + values[":one"]
            item["updated_at"] = values[":updated_at"]
            item["ttl"] = values[":ttl"]
            items[item_key] = item
            return

        if condition == "active_count > :zero":
            if not item or int(item.get("active_count", 0)) <= values[":zero"]:
                raise conditional_failed()
            item["active_count"] = int(item.get("active_count", 0)) - values[":one"]
            item["updated_at"] = values[":now"]
            item["ttl"] = values[":ttl"]
            return

        if "#status = :active" in condition:
            if not item or item.get("status") != values[":active"]:
                raise conditional_failed()
            item["status"] = values[":closed"]
            item["closed_at"] = values[":now"]
            item["close_reason"] = values[":reason"]
            item["updated_at"] = values[":now"]
            item["ttl"] = values[":ttl"]
            return

        if "attribute_not_exists(PK)" in condition:
            allowed_statuses = {
                values.get(":status_waiting"),
                values.get(":status_processing"),
                values.get(":status_assigned"),
                values.get(":status_closed"),
                values.get(":status_failed"),
                values.get(":closed"),
                values.get(":failed"),
            }
            allowed_statuses.discard(None)
            if item and item.get("status") not in allowed_statuses:
                raise conditional_failed()
            item = item or dict(key)
            for name in (
                ":agent_account_id",
                ":agent_display_name",
                ":agent_email",
                ":ticket_key",
                ":ticket_url",
                ":ticket_status",
                ":queue_pk",
                ":queue_sk",
            ):
                if name in values:
                    item[name[1:]] = values[name]
            if ":status_active" in values:
                item["status"] = values[":status_active"]
            if ":waiting" in values:
                item["status"] = values[":waiting"]
            item["created_at"] = item.get("created_at") or values.get(":created_at")
            item["updated_at"] = values.get(":updated_at")
            item["ttl"] = values.get(":ttl")
            items[item_key] = item
            return

        raise AssertionError(f"Unhandled fake update: {update_expression} / {condition}")


def agent(account_id="agent-1"):
    return {
        "account_id": account_id,
        "display_name": "Agent One",
        "email": "agent@example.com",
    }


def ticket(ticket_key):
    return {
        "ticket_key": ticket_key,
        "ticket_url": f"https://innovyq.atlassian.net/browse/{ticket_key}",
        "status": "To Do",
    }


def slack_context(ticket_key):
    return {
        "session_id": f"issue:D123:{ticket_key}",
        "slack_channel": "D123",
        "slack_thread_ts": "",
        "slack_user": "U123",
    }


class ChatLocksTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeDynamoDB()
        self.original_table = chat_locks.table
        self.original_client = chat_locks.client
        self.original_max = chat_locks.MAX_ACTIVE_CHATS_PER_AGENT
        chat_locks.table = FakeTable(self.store)
        chat_locks.client = FakeClient(self.store)
        chat_locks.MAX_ACTIVE_CHATS_PER_AGENT = 5

    def tearDown(self):
        chat_locks.table = self.original_table
        chat_locks.client = self.original_client
        chat_locks.MAX_ACTIVE_CHATS_PER_AGENT = self.original_max

    def test_first_five_locks_succeed_and_sixth_fails(self):
        for number in range(1, 6):
            result = chat_locks.acquire_chat_lock(agent(), ticket(f"IVY-{number}"), slack_context(f"IVY-{number}"))
            self.assertTrue(result["ok"])
            self.assertFalse(result["already_locked"])

        result = chat_locks.acquire_chat_lock(agent(), ticket("IVY-6"), slack_context("IVY-6"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "agent_at_capacity")
        self.assertEqual(result["active_count"], 5)

    def test_duplicate_ticket_does_not_double_increment_counter(self):
        first = chat_locks.acquire_chat_lock(agent(), ticket("IVY-10"), slack_context("IVY-10"))
        duplicate = chat_locks.acquire_chat_lock(agent(), ticket("IVY-10"), slack_context("IVY-10"))

        self.assertTrue(first["ok"])
        self.assertTrue(duplicate["ok"])
        self.assertTrue(duplicate["already_locked"])
        self.assertEqual(chat_locks.get_agent_active_count("agent-1"), 1)

    def test_release_decrements_counter(self):
        chat_locks.acquire_chat_lock(agent(), ticket("IVY-20"), slack_context("IVY-20"))

        result = chat_locks.release_chat_lock("IVY-20", "resolved")

        self.assertTrue(result["ok"])
        self.assertTrue(result["released"])
        self.assertEqual(chat_locks.get_agent_active_count("agent-1"), 0)

    def test_release_is_idempotent(self):
        chat_locks.acquire_chat_lock(agent(), ticket("IVY-21"), slack_context("IVY-21"))
        first = chat_locks.release_chat_lock("IVY-21", "resolved")
        second = chat_locks.release_chat_lock("IVY-21", "resolved")

        self.assertTrue(first["released"])
        self.assertFalse(second["released"])
        self.assertEqual(chat_locks.get_agent_active_count("agent-1"), 0)

    def test_queue_oldest_item_is_popped_first(self):
        first = chat_locks.enqueue_live_agent_request(ticket("IVY-30"), slack_context("IVY-30"), "capacity")
        time_shifted_ticket = ticket("IVY-31")
        second = chat_locks.enqueue_live_agent_request(time_shifted_ticket, slack_context("IVY-31"), "capacity")

        popped = chat_locks.pop_oldest_waiting_request()

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(popped["ticket_key"], "IVY-30")
        self.assertEqual(popped["status"], "processing")


if __name__ == "__main__":
    unittest.main()
