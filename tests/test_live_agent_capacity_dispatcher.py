import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

import live_agent_capacity as capacity


class LiveAgentCapacityDispatcherTests(unittest.TestCase):
    def agent(self, agent_id, start, end, count=0, maximum=5, priority=1):
        return {
            "agent_id": agent_id,
            "display_name": agent_id.title(),
            "jira_account_id": f"jira-{agent_id}",
            "shift_start": start,
            "shift_end": end,
            "timezone": "Asia/Kolkata",
            "active_count": count,
            "max_capacity": maximum,
            "priority": priority,
            "enabled": True,
        }

    def ist(self, hour, minute=0):
        return datetime(2026, 7, 9, hour, minute, tzinfo=ZoneInfo("Asia/Kolkata"))

    def test_day_shift(self):
        self.assertTrue(capacity.is_agent_on_shift(self.agent("KARAN", "08:00", "20:00"), self.ist(10)))

    def test_night_shift_and_cross_midnight(self):
        agent = self.agent("VIBHU", "20:00", "08:00")
        self.assertTrue(capacity.is_agent_on_shift(agent, self.ist(22)))
        self.assertTrue(capacity.is_agent_on_shift(agent, self.ist(2)))
        self.assertFalse(capacity.is_agent_on_shift(agent, self.ist(8, 30)))

    def test_sort_prefers_least_loaded_then_priority_then_name(self):
        agents = [
            self.agent("KARAN", "08:00", "20:00", count=5),
            self.agent("RAHUL", "08:00", "20:00", count=2, priority=2),
            self.agent("AMIT", "08:00", "20:00", count=2, priority=1),
        ]
        ordered = capacity.sort_agents_for_assignment(agents)
        self.assertEqual([agent["agent_id"] for agent in ordered], ["AMIT", "RAHUL", "KARAN"])

    def test_choose_skips_full_agent(self):
        originals = capacity.get_enabled_agents, capacity.reserve_agent_capacity
        try:
            agents = [
                self.agent("KARAN", "08:00", "20:00", count=5),
                self.agent("RAHUL", "08:00", "20:00", count=2),
            ]
            capacity.get_enabled_agents = lambda include_disabled=False: agents
            capacity.reserve_agent_capacity = lambda agent: {
                "ok": True,
                "agent": {**agent, "active_count": agent["active_count"] + 1},
                "active_count_before": agent["active_count"],
                "active_count_after": agent["active_count"] + 1,
            }
            result = capacity.choose_and_reserve_agent(now=self.ist(10))
            self.assertEqual(result["agent"]["agent_id"], "RAHUL")
            self.assertIn(
                {"agent_id": "KARAN", "reason": "FULL_CAPACITY"},
                result["skipped_agents_with_reason"],
            )
        finally:
            capacity.get_enabled_agents, capacity.reserve_agent_capacity = originals

    def test_all_full_queues(self):
        original = capacity.get_enabled_agents
        try:
            capacity.get_enabled_agents = lambda include_disabled=False: [
                self.agent("KARAN", "08:00", "20:00", count=5)
            ]
            result = capacity.choose_and_reserve_agent(now=self.ist(10))
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "NO_CAPACITY")
        finally:
            capacity.get_enabled_agents = original


if __name__ == "__main__":
    unittest.main()
