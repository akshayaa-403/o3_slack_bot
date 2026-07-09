import json
import os
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError


AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
CHAT_LOCKS_TABLE = os.environ.get("CHAT_LOCKS_TABLE", "O3_Lambda_Agent_Chat_Locks")
MAX_ACTIVE_CHATS_PER_AGENT = int(os.environ.get("MAX_ACTIVE_CHATS_PER_AGENT", "5"))
CHAT_LOCK_TTL_HOURS = int(os.environ.get("CHAT_LOCK_TTL_HOURS", "24"))
LIVE_AGENT_QUEUE_NAME = os.environ.get("LIVE_AGENT_QUEUE_NAME", "live_agent")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(CHAT_LOCKS_TABLE)
client = boto3.client("dynamodb", region_name=AWS_REGION)
serializer = TypeSerializer()
deserializer = TypeDeserializer()


def log_json(data):
    print(json.dumps(data, default=str))


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def epoch_ms():
    return int(time.time() * 1000)


def ttl_epoch():
    return int(time.time()) + (CHAT_LOCK_TTL_HOURS * 60 * 60)


def text_or_empty(value):
    if value is None:
        return ""

    return str(value).strip()


def agent_pk(agent_account_id):
    return f"AGENT#{agent_account_id}"


def ticket_pk(ticket_key):
    return f"TICKET#{ticket_key}"


def queue_pk():
    return f"QUEUE#{LIVE_AGENT_QUEUE_NAME}"


def lock_sk(ticket_key):
    return f"LOCK#{ticket_key}"


def waiting_sk(created_epoch_ms, ticket_key):
    return f"WAITING#{created_epoch_ms}#{ticket_key}"


def dynamodb_value(value):
    return serializer.serialize(value)


def dynamodb_item(item):
    return {
        key: dynamodb_value(value)
        for key, value in item.items()
        if value is not None
    }


def dynamodb_key(pk, sk):
    return dynamodb_item({
        "PK": pk,
        "SK": sk,
    })


def from_dynamodb_item(item):
    return {
        key: deserializer.deserialize(value)
        for key, value in (item or {}).items()
    }


def is_conditional_failure(error):
    return error.response.get("Error", {}).get("Code") in {
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
    }


def get_ticket_index(ticket_key):
    response = table.get_item(Key={"PK": ticket_pk(ticket_key), "SK": "LOCK"})
    return response.get("Item")


def get_active_lock(agent_account_id, ticket_key):
    response = table.get_item(Key={"PK": agent_pk(agent_account_id), "SK": lock_sk(ticket_key)})
    return response.get("Item")


def get_agent_active_count(agent_account_id) -> int:
    response = table.get_item(Key={"PK": agent_pk(agent_account_id), "SK": "COUNTER"})
    item = response.get("Item") or {}
    active_count = int(item.get("active_count") or 0)
    log_json({
        "level": "INFO",
        "message": "chat_locks_get_agent_active_count",
        "agent_account_id": agent_account_id,
        "active_count": active_count,
    })
    return active_count


def active_lock_response(ticket_index, already_locked=False):
    return {
        "ok": True,
        "already_locked": already_locked,
        "agent_account_id": ticket_index.get("agent_account_id"),
        "ticket_key": ticket_index.get("ticket_key"),
        "status": ticket_index.get("status"),
    }


def acquire_chat_lock(agent, ticket, slack_context) -> dict:
    agent_account_id = text_or_empty((agent or {}).get("account_id"))
    ticket_key = text_or_empty((ticket or {}).get("ticket_key"))
    timestamp = now_iso()
    ttl = ttl_epoch()

    log_json({
        "level": "INFO",
        "message": "chat_locks_acquire_started",
        "agent_account_id": agent_account_id,
        "ticket_key": ticket_key,
    })

    if not agent_account_id:
        return {"ok": False, "reason": "missing_agent_account_id"}

    if not ticket_key:
        return {"ok": False, "reason": "missing_ticket_key"}

    existing_ticket = get_ticket_index(ticket_key)
    if existing_ticket and existing_ticket.get("status") == "active":
        log_json({
            "level": "INFO",
            "message": "chat_locks_acquire_duplicate_active",
            "agent_account_id": existing_ticket.get("agent_account_id"),
            "ticket_key": ticket_key,
        })
        return active_lock_response(existing_ticket, already_locked=True)

    lock_item = {
        "PK": agent_pk(agent_account_id),
        "SK": lock_sk(ticket_key),
        "ticket_key": ticket_key,
        "ticket_url": text_or_empty((ticket or {}).get("ticket_url")),
        "session_id": text_or_empty((slack_context or {}).get("session_id")),
        "slack_channel": text_or_empty((slack_context or {}).get("slack_channel")),
        "slack_thread_ts": text_or_empty((slack_context or {}).get("slack_thread_ts")),
        "slack_user": text_or_empty((slack_context or {}).get("slack_user")),
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
        "ttl": ttl,
    }

    ticket_index_update_values = {
        ":agent_account_id": agent_account_id,
        ":agent_display_name": text_or_empty((agent or {}).get("display_name")),
        ":agent_email": text_or_empty((agent or {}).get("email")),
        ":ticket_key": ticket_key,
        ":ticket_url": text_or_empty((ticket or {}).get("ticket_url")),
        ":ticket_status": text_or_empty((ticket or {}).get("status")),
        ":status_active": "active",
        ":created_at": timestamp,
        ":updated_at": timestamp,
        ":ttl": ttl,
        ":status_waiting": "waiting",
        ":status_processing": "processing",
        ":status_assigned": "assigned",
        ":status_closed": "closed",
        ":status_failed": "failed",
    }

    transaction = [
        {
            "Update": {
                "TableName": CHAT_LOCKS_TABLE,
                "Key": dynamodb_key(agent_pk(agent_account_id), "COUNTER"),
                "UpdateExpression": """
                    SET
                        active_count = if_not_exists(active_count, :zero) + :one,
                        updated_at = :updated_at,
                        #ttl = :ttl
                """,
                "ConditionExpression": "attribute_not_exists(active_count) OR active_count < :max",
                "ExpressionAttributeNames": {"#ttl": "ttl"},
                "ExpressionAttributeValues": dynamodb_item({
                    ":zero": 0,
                    ":one": 1,
                    ":max": MAX_ACTIVE_CHATS_PER_AGENT,
                    ":updated_at": timestamp,
                    ":ttl": ttl,
                }),
            }
        },
        {
            "Put": {
                "TableName": CHAT_LOCKS_TABLE,
                "Item": dynamodb_item(lock_item),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        },
        {
            "Update": {
                "TableName": CHAT_LOCKS_TABLE,
                "Key": dynamodb_key(ticket_pk(ticket_key), "LOCK"),
                "UpdateExpression": """
                    SET
                        agent_account_id = :agent_account_id,
                        agent_display_name = :agent_display_name,
                        agent_email = :agent_email,
                        ticket_key = :ticket_key,
                        ticket_url = :ticket_url,
                        ticket_status = :ticket_status,
                        #status = :status_active,
                        created_at = if_not_exists(created_at, :created_at),
                        updated_at = :updated_at,
                        #ttl = :ttl
                """,
                "ConditionExpression": """
                    attribute_not_exists(PK)
                    OR #status IN (:status_waiting, :status_processing, :status_assigned, :status_closed, :status_failed)
                """,
                "ExpressionAttributeNames": {
                    "#status": "status",
                    "#ttl": "ttl",
                },
                "ExpressionAttributeValues": dynamodb_item(ticket_index_update_values),
            }
        },
    ]

    try:
        client.transact_write_items(TransactItems=transaction)
    except ClientError as error:
        if not is_conditional_failure(error):
            raise

        existing_ticket = get_ticket_index(ticket_key)
        if existing_ticket and existing_ticket.get("status") == "active":
            return active_lock_response(existing_ticket, already_locked=True)

        active_count = get_agent_active_count(agent_account_id)
        if active_count >= MAX_ACTIVE_CHATS_PER_AGENT:
            log_json({
                "level": "INFO",
                "message": "chat_locks_acquire_agent_at_capacity",
                "agent_account_id": agent_account_id,
                "ticket_key": ticket_key,
                "active_count": active_count,
            })
            return {
                "ok": False,
                "reason": "agent_at_capacity",
                "active_count": active_count,
            }

        log_json({
            "level": "WARN",
            "message": "chat_locks_acquire_transaction_conflict",
            "agent_account_id": agent_account_id,
            "ticket_key": ticket_key,
            "active_count": active_count,
            "error": str(error),
        })
        return {
            "ok": False,
            "reason": "transaction_conflict",
            "active_count": active_count,
        }

    active_count = get_agent_active_count(agent_account_id)
    log_json({
        "level": "INFO",
        "message": "chat_locks_acquire_completed",
        "agent_account_id": agent_account_id,
        "ticket_key": ticket_key,
        "active_count": active_count,
    })
    return {
        "ok": True,
        "already_locked": False,
        "agent_account_id": agent_account_id,
        "ticket_key": ticket_key,
        "active_count": active_count,
        "status": "active",
    }


def release_chat_lock(ticket_key, close_reason) -> dict:
    ticket_key = text_or_empty(ticket_key)
    timestamp = now_iso()
    ttl = ttl_epoch()

    log_json({
        "level": "INFO",
        "message": "chat_locks_release_started",
        "ticket_key": ticket_key,
        "close_reason": close_reason,
    })

    ticket_index = get_ticket_index(ticket_key)
    if not ticket_index or ticket_index.get("status") != "active":
        return {
            "ok": True,
            "released": False,
            "ticket_key": ticket_key,
        }

    agent_account_id = ticket_index.get("agent_account_id")
    lock = get_active_lock(agent_account_id, ticket_key)
    if not lock or lock.get("status") != "active":
        table.update_item(
            Key={"PK": ticket_pk(ticket_key), "SK": "LOCK"},
            UpdateExpression="SET #status = :closed, closed_at = :now, close_reason = :reason, updated_at = :now, #ttl = :ttl",
            ExpressionAttributeNames={"#status": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":closed": "closed",
                ":now": timestamp,
                ":reason": text_or_empty(close_reason),
                ":ttl": ttl,
            },
        )
        return {
            "ok": True,
            "released": False,
            "ticket_key": ticket_key,
            "agent_account_id": agent_account_id,
        }

    transaction = [
        {
            "Update": {
                "TableName": CHAT_LOCKS_TABLE,
                "Key": dynamodb_key(agent_pk(agent_account_id), "COUNTER"),
                "UpdateExpression": "SET active_count = active_count - :one, updated_at = :now, #ttl = :ttl",
                "ConditionExpression": "active_count > :zero",
                "ExpressionAttributeNames": {"#ttl": "ttl"},
                "ExpressionAttributeValues": dynamodb_item({
                    ":one": 1,
                    ":zero": 0,
                    ":now": timestamp,
                    ":ttl": ttl,
                }),
            }
        },
        {
            "Update": {
                "TableName": CHAT_LOCKS_TABLE,
                "Key": dynamodb_key(agent_pk(agent_account_id), lock_sk(ticket_key)),
                "UpdateExpression": """
                    SET
                        #status = :closed,
                        closed_at = :now,
                        close_reason = :reason,
                        updated_at = :now,
                        #ttl = :ttl
                """,
                "ConditionExpression": "#status = :active",
                "ExpressionAttributeNames": {
                    "#status": "status",
                    "#ttl": "ttl",
                },
                "ExpressionAttributeValues": dynamodb_item({
                    ":closed": "closed",
                    ":active": "active",
                    ":now": timestamp,
                    ":reason": text_or_empty(close_reason),
                    ":ttl": ttl,
                }),
            }
        },
        {
            "Update": {
                "TableName": CHAT_LOCKS_TABLE,
                "Key": dynamodb_key(ticket_pk(ticket_key), "LOCK"),
                "UpdateExpression": """
                    SET
                        #status = :closed,
                        closed_at = :now,
                        close_reason = :reason,
                        updated_at = :now,
                        #ttl = :ttl
                """,
                "ConditionExpression": "#status = :active",
                "ExpressionAttributeNames": {
                    "#status": "status",
                    "#ttl": "ttl",
                },
                "ExpressionAttributeValues": dynamodb_item({
                    ":closed": "closed",
                    ":active": "active",
                    ":now": timestamp,
                    ":reason": text_or_empty(close_reason),
                    ":ttl": ttl,
                }),
            }
        },
    ]

    try:
        client.transact_write_items(TransactItems=transaction)
    except ClientError as error:
        if not is_conditional_failure(error):
            raise
        return {
            "ok": True,
            "released": False,
            "ticket_key": ticket_key,
            "agent_account_id": agent_account_id,
        }

    active_count = get_agent_active_count(agent_account_id)
    log_json({
        "level": "INFO",
        "message": "chat_locks_release_completed",
        "agent_account_id": agent_account_id,
        "ticket_key": ticket_key,
        "active_count": active_count,
    })
    return {
        "ok": True,
        "released": True,
        "ticket_key": ticket_key,
        "agent_account_id": agent_account_id,
        "active_count": active_count,
    }


def enqueue_live_agent_request(ticket, slack_context, reason) -> dict:
    ticket_key = text_or_empty((ticket or {}).get("ticket_key"))
    timestamp = now_iso()
    created_ms = epoch_ms()
    ttl = ttl_epoch()

    log_json({
        "level": "INFO",
        "message": "chat_locks_enqueue_started",
        "ticket_key": ticket_key,
        "reason": reason,
    })

    if not ticket_key:
        return {"ok": False, "reason": "missing_ticket_key"}

    existing_ticket = get_ticket_index(ticket_key)
    if existing_ticket and existing_ticket.get("status") == "waiting":
        return {
            "ok": True,
            "already_queued": True,
            "ticket_key": ticket_key,
            "queue_pk": existing_ticket.get("queue_pk"),
            "queue_sk": existing_ticket.get("queue_sk"),
        }

    if existing_ticket and existing_ticket.get("status") == "active":
        return {
            "ok": False,
            "reason": "ticket_already_active",
            "ticket_key": ticket_key,
        }

    queue_item = {
        "PK": queue_pk(),
        "SK": waiting_sk(created_ms, ticket_key),
        "ticket_key": ticket_key,
        "ticket_url": text_or_empty((ticket or {}).get("ticket_url")),
        "ticket_status": text_or_empty((ticket or {}).get("status")),
        "session_id": text_or_empty((slack_context or {}).get("session_id")),
        "slack_channel": text_or_empty((slack_context or {}).get("slack_channel")),
        "slack_thread_ts": text_or_empty((slack_context or {}).get("slack_thread_ts")),
        "slack_user": text_or_empty((slack_context or {}).get("slack_user")),
        "status": "waiting",
        "reason": text_or_empty(reason),
        "requested_at": timestamp,
        "created_at": timestamp,
        "updated_at": timestamp,
        "ttl": ttl,
    }

    transaction = [
        {
            "Put": {
                "TableName": CHAT_LOCKS_TABLE,
                "Item": dynamodb_item(queue_item),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        },
        {
            "Update": {
                "TableName": CHAT_LOCKS_TABLE,
                "Key": dynamodb_key(ticket_pk(ticket_key), "LOCK"),
                "UpdateExpression": """
                    SET
                        ticket_key = :ticket_key,
                        ticket_url = :ticket_url,
                        ticket_status = :ticket_status,
                        queue_pk = :queue_pk,
                        queue_sk = :queue_sk,
                        #status = :waiting,
                        created_at = if_not_exists(created_at, :created_at),
                        updated_at = :updated_at,
                        #ttl = :ttl
                """,
                "ConditionExpression": "attribute_not_exists(PK) OR #status IN (:closed, :failed)",
                "ExpressionAttributeNames": {
                    "#status": "status",
                    "#ttl": "ttl",
                },
                "ExpressionAttributeValues": dynamodb_item({
                    ":ticket_key": ticket_key,
                    ":ticket_url": queue_item["ticket_url"],
                    ":ticket_status": queue_item["ticket_status"],
                    ":queue_pk": queue_item["PK"],
                    ":queue_sk": queue_item["SK"],
                    ":waiting": "waiting",
                    ":closed": "closed",
                    ":failed": "failed",
                    ":created_at": timestamp,
                    ":updated_at": timestamp,
                    ":ttl": ttl,
                }),
            }
        },
    ]

    try:
        client.transact_write_items(TransactItems=transaction)
    except ClientError as error:
        if not is_conditional_failure(error):
            raise

        existing_ticket = get_ticket_index(ticket_key)
        if existing_ticket and existing_ticket.get("status") == "waiting":
            return {
                "ok": True,
                "already_queued": True,
                "ticket_key": ticket_key,
                "queue_pk": existing_ticket.get("queue_pk"),
                "queue_sk": existing_ticket.get("queue_sk"),
            }
        return {
            "ok": False,
            "reason": "queue_transaction_conflict",
            "ticket_key": ticket_key,
        }

    log_json({
        "level": "INFO",
        "message": "chat_locks_enqueue_completed",
        "ticket_key": ticket_key,
        "queue_sk": queue_item["SK"],
    })
    return {
        "ok": True,
        "already_queued": False,
        "ticket_key": ticket_key,
        "queue_pk": queue_item["PK"],
        "queue_sk": queue_item["SK"],
    }


def pop_oldest_waiting_request():
    log_json({
        "level": "INFO",
        "message": "chat_locks_pop_oldest_started",
        "queue_name": LIVE_AGENT_QUEUE_NAME,
    })

    response = table.query(
        KeyConditionExpression=Key("PK").eq(queue_pk()) & Key("SK").begins_with("WAITING#"),
        ScanIndexForward=True,
        Limit=25,
    )

    for item in response.get("Items", []):
        if item.get("status") != "waiting":
            continue

        timestamp = now_iso()
        try:
            updated = table.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET #status = :processing, processing_started_at = :now, updated_at = :now",
                ConditionExpression=Attr("status").eq("waiting"),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": "processing",
                    ":now": timestamp,
                },
                ReturnValues="ALL_NEW",
            )
            queue_item = updated.get("Attributes") or {
                **item,
                "status": "processing",
                "processing_started_at": timestamp,
                "updated_at": timestamp,
            }
            log_json({
                "level": "INFO",
                "message": "chat_locks_pop_oldest_completed",
                "ticket_key": queue_item.get("ticket_key"),
                "queue_sk": queue_item.get("SK"),
            })
            return queue_item
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            raise

    log_json({
        "level": "INFO",
        "message": "chat_locks_pop_oldest_empty",
        "queue_name": LIVE_AGENT_QUEUE_NAME,
    })
    return None


def mark_queue_item_assigned(queue_item, agent, ticket_key):
    timestamp = now_iso()
    log_json({
        "level": "INFO",
        "message": "chat_locks_queue_mark_assigned",
        "ticket_key": ticket_key,
        "queue_sk": (queue_item or {}).get("SK"),
        "agent_account_id": (agent or {}).get("account_id"),
    })
    return table.update_item(
        Key={"PK": queue_item["PK"], "SK": queue_item["SK"]},
        UpdateExpression="""
            SET
                #status = :assigned,
                assigned_at = :now,
                updated_at = :now,
                agent_account_id = :agent_account_id,
                agent_display_name = :agent_display_name,
                agent_email = :agent_email,
                ticket_key = :ticket_key
        """,
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":assigned": "assigned",
            ":now": timestamp,
            ":agent_account_id": text_or_empty((agent or {}).get("account_id")),
            ":agent_display_name": text_or_empty((agent or {}).get("display_name")),
            ":agent_email": text_or_empty((agent or {}).get("email")),
            ":ticket_key": text_or_empty(ticket_key),
        },
        ReturnValues="ALL_NEW",
    ).get("Attributes")


def mark_queue_item_waiting_again(queue_item, reason):
    timestamp = now_iso()
    log_json({
        "level": "INFO",
        "message": "chat_locks_queue_mark_waiting_again",
        "ticket_key": (queue_item or {}).get("ticket_key"),
        "queue_sk": (queue_item or {}).get("SK"),
        "reason": reason,
    })
    return table.update_item(
        Key={"PK": queue_item["PK"], "SK": queue_item["SK"]},
        UpdateExpression="SET #status = :waiting, reason = :reason, updated_at = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":waiting": "waiting",
            ":reason": text_or_empty(reason),
            ":now": timestamp,
        },
        ReturnValues="ALL_NEW",
    ).get("Attributes")


def mark_queue_item_failed(queue_item, reason):
    timestamp = now_iso()
    log_json({
        "level": "INFO",
        "message": "chat_locks_queue_mark_failed",
        "ticket_key": (queue_item or {}).get("ticket_key"),
        "queue_sk": (queue_item or {}).get("SK"),
        "reason": reason,
    })
    updated = table.update_item(
        Key={"PK": queue_item["PK"], "SK": queue_item["SK"]},
        UpdateExpression="SET #status = :failed, failure_reason = :reason, failed_at = :now, updated_at = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":failed": "failed",
            ":reason": text_or_empty(reason),
            ":now": timestamp,
        },
        ReturnValues="ALL_NEW",
    ).get("Attributes")

    ticket_key_value = text_or_empty((queue_item or {}).get("ticket_key"))
    if ticket_key_value:
        table.update_item(
            Key={"PK": ticket_pk(ticket_key_value), "SK": "LOCK"},
            UpdateExpression="SET #status = :failed, failure_reason = :reason, failed_at = :now, updated_at = :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": "failed",
                ":reason": text_or_empty(reason),
                ":now": timestamp,
            },
        )

    return updated
