"""Review surface — the human-approval edge, kept platform-agnostic.

The pipeline produces a *neutral* IntentCard (via build_card). A ReviewSurface
renders that card for one platform and posts it; the viewer's click maps back to
one of three actions: APPROVE, EDIT, DISCARD. Slack lives here today; Microsoft
Teams (or email, or a web page) is a new render_* function + a new surface class
— nothing upstream in the pipeline changes.

    from agents.review import build_card, SlackReviewSurface
    card = build_card(proposal)
    surface = SlackReviewSurface(post_message=send_slack_message, channel="C123")
    surface.post(card)
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional

# Neutral action names the pipeline understands, regardless of platform.
APPROVE = "approve"
EDIT = "edit"
DISCARD = "discard"

# Slack Block Kit action_ids (the inbound click carries one of these).
ACTION_ID_APPROVE = "agent2_intents_approve"
ACTION_ID_EDIT = "agent2_intents_edit"
ACTION_ID_DISCARD = "agent2_intents_discard"

_SLACK_ACTION_MAP = {
    ACTION_ID_APPROVE: APPROVE,
    ACTION_ID_EDIT: EDIT,
    ACTION_ID_DISCARD: DISCARD,
}

MAX_ROWS_ON_CARD = 10


def slack_action_to_intent_action(action_id: str) -> Optional[str]:
    """Map an inbound Slack action_id to a neutral action (or None if unrelated)."""
    return _SLACK_ACTION_MAP.get(action_id)


# --- neutral card model ------------------------------------------------------
def build_card(proposal: Dict, *, max_rows: int = MAX_ROWS_ON_CARD) -> Dict:
    """Turn a proposal into a platform-neutral preview card.

    Shows up to `max_rows` intents; the rest are summarized as an overflow count.
    """
    intents = proposal.get("intents", [])
    stats = proposal.get("stats", {})
    rows = [
        {
            "name": intent.get("intent_name", ""),
            "category": intent.get("category", ""),
            "kind": "action" if intent.get("uses_lambda") else "info",
            "utterances": len(intent.get("utterances", [])),
            "response": (intent.get("response", "") or "").strip(),
        }
        for intent in intents[:max_rows]
    ]
    overflow = max(0, len(intents) - max_rows)

    title = (
        f"{len(intents)} proposed intent(s) from "
        f"{stats.get('tickets', '?')} {proposal.get('source', 'ticket')} ticket(s)"
    )
    summary = (
        f"{stats.get('interactions', '?')} interactions → "
        f"{stats.get('clusters', '?')} clusters → {len(intents)} intents"
    )
    return {
        "proposal_id": proposal["proposal_id"],
        "title": title,
        "summary": summary,
        "rows": rows,
        "overflow": overflow,
        "actions": [
            {"id": APPROVE, "label": "Approve & load", "style": "primary"},
            {"id": DISCARD, "label": "Deny (get CSV)", "style": "danger"},
        ],
    }


# --- Slack rendering ---------------------------------------------------------
_SLACK_ACTION_IDS = {APPROVE: ACTION_ID_APPROVE, EDIT: ACTION_ID_EDIT, DISCARD: ACTION_ID_DISCARD}


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_slack_blocks(card: Dict) -> List[Dict]:
    """Render a neutral card as Slack Block Kit blocks (matches this repo's style)."""
    blocks: List[Dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": _truncate(card["title"], 150)}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": card["summary"]}]},
        {"type": "divider"},
    ]
    for row in card["rows"]:
        kind = "🔧 action" if row["kind"] == "action" else "💬 info"
        line = (
            f"*{row['name']}*  ·  {row['category']}  ·  {kind}  ·  {row['utterances']} utterances\n"
            f"{_truncate(row['response'], 180)}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})
    if card["overflow"]:
        blocks.append(
            {"type": "context",
             "elements": [{"type": "mrkdwn", "text": f"…and {card['overflow']} more"}]}
        )

    value = json.dumps({"proposal_id": card["proposal_id"]})
    elements = []
    for action in card["actions"]:
        button = {
            "type": "button",
            "text": {"type": "plain_text", "text": action["label"]},
            "action_id": _SLACK_ACTION_IDS[action["id"]],
            "value": value,
        }
        if action["style"] in ("primary", "danger"):
            button["style"] = action["style"]
        elements.append(button)
    blocks.append({"type": "actions", "elements": elements})
    return blocks


# --- surfaces ----------------------------------------------------------------
class ReviewSurface(ABC):
    """A place to talk to the user: a review card (optional review mode) and/or a
    plain summary line (the automated path's 'here's what happened' message)."""

    @abstractmethod
    def post(self, card: Dict) -> Dict: ...

    @abstractmethod
    def post_summary(self, text: str) -> Dict: ...


class SlackReviewSurface(ReviewSurface):
    """Post to Slack. `post_message` is injected so this class never imports the
    bot's Slack client directly — in Lambda pass send_slack_message; in tests
    pass a recorder. Signature: post_message(channel, text, blocks) -> Any."""

    def __init__(self, post_message: Callable[..., object], channel: str,
                 thread_ts: Optional[str] = None) -> None:
        self._post_message = post_message
        self._channel = channel
        self._thread_ts = thread_ts

    def post(self, card: Dict) -> Dict:
        blocks = render_slack_blocks(card)
        kwargs = {"thread_ts": self._thread_ts} if self._thread_ts else {}
        ref = self._post_message(self._channel, card["title"], blocks=blocks, **kwargs)
        return {"platform": "slack", "channel": self._channel, "ref": ref,
                "proposal_id": card["proposal_id"]}

    def post_summary(self, text: str) -> Dict:
        kwargs = {"thread_ts": self._thread_ts} if self._thread_ts else {}
        ref = self._post_message(self._channel, text, **kwargs)
        return {"platform": "slack", "channel": self._channel, "ref": ref}


class MockReviewSurface(ReviewSurface):
    """Records posted cards and summaries for tests; no network."""

    def __init__(self) -> None:
        self.posted: List[Dict] = []
        self.summaries: List[str] = []

    def post(self, card: Dict) -> Dict:
        self.posted.append(card)
        return {"platform": "mock", "proposal_id": card["proposal_id"]}

    def post_summary(self, text: str) -> Dict:
        self.summaries.append(text)
        return {"platform": "mock"}
