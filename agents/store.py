"""Proposal store — holds a generated proposal between two async moments.

A review card is posted, then (seconds or hours later) someone clicks Approve.
The click arrives as a fresh request with only an id, so the generated intents +
draft sheet must live somewhere in between. This is that somewhere.

InMemoryProposalStore is for tests/local. A DynamoDB implementation drops in for
Lambda by subclassing ProposalStore — callers never change (this repo already
uses DynamoDB in chat_locks.py, so the same table pattern applies).
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional


class ProposalStore(ABC):
    """Persist proposals by id across the post-card / click-approve gap."""

    @abstractmethod
    def put(self, proposal: Dict) -> None: ...

    @abstractmethod
    def get(self, proposal_id: str) -> Optional[Dict]: ...

    @abstractmethod
    def set_status(self, proposal_id: str, status: str) -> None: ...


class InMemoryProposalStore(ProposalStore):
    """Dict-backed store for tests and single-process/local runs."""

    def __init__(self) -> None:
        self._items: Dict[str, Dict] = {}

    def put(self, proposal: Dict) -> None:
        self._items[proposal["proposal_id"]] = proposal

    def get(self, proposal_id: str) -> Optional[Dict]:
        return self._items.get(proposal_id)

    def set_status(self, proposal_id: str, status: str) -> None:
        if proposal_id in self._items:
            self._items[proposal_id]["status"] = status


class DynamoProposalStore(ProposalStore):
    """DynamoDB-backed store for Lambda, where the generate call and the later
    Approve click are separate invocations with no shared memory.

    The whole proposal is stored as a JSON blob under `data` (avoids Decimal/nested
    float headaches); `status` is kept as its own attribute so a click can flip it
    with a single UpdateItem. Items expire via `ttl` so old drafts self-clean.

    Table: partition key `proposal_id` (S), TTL attribute `ttl`.
    """

    def __init__(self, table_name: str, *, region: Optional[str] = None,
                 ttl_seconds: int = 7 * 24 * 3600) -> None:
        import boto3  # local import: only needed in the deployed Lambda

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self._ttl_seconds = ttl_seconds

    def put(self, proposal: Dict) -> None:
        self._table.put_item(Item={
            "proposal_id": proposal["proposal_id"],
            "data": json.dumps(proposal),
            "status": proposal.get("status", ""),
            "ttl": int(time.time()) + self._ttl_seconds,
        })

    def get(self, proposal_id: str) -> Optional[Dict]:
        item = self._table.get_item(Key={"proposal_id": proposal_id}).get("Item")
        if not item:
            return None
        proposal = json.loads(item["data"])
        # The top-level status attribute is the source of truth (a click may have
        # updated it after the blob was written).
        if item.get("status"):
            proposal["status"] = item["status"]
        return proposal

    def set_status(self, proposal_id: str, status: str) -> None:
        self._table.update_item(
            Key={"proposal_id": proposal_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status},
        )
