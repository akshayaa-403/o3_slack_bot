import io
import json
import os
import unittest
import urllib.error

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

import lambda_o3_jsm_oncall_user as oncall


class FakeCacheTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)
        return {}

    def get_item(self, Key):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}


class OncallUserResolverTests(unittest.TestCase):
    def setUp(self):
        self.original_table = oncall.cache_table
        self.original_jira_get_json = oncall.jira_get_json
        self.original_jira_request = oncall.jira_request
        self.original_opsgenie_key = oncall.OPSGENIE_API_KEY
        self.original_opsgenie_schedule = oncall.OPSGENIE_SCHEDULE_ID
        self.original_schedule_name = oncall.LIVE_AGENT_SCHEDULE_NAME
        self.original_secret_cache = oncall._jira_secret_cache
        self.table = FakeCacheTable()
        oncall.cache_table = self.table
        oncall.OPSGENIE_API_KEY = ""
        oncall.OPSGENIE_SCHEDULE_ID = ""
        oncall.LIVE_AGENT_SCHEDULE_NAME = "live_agent"
        oncall._jira_secret_cache = None

    def tearDown(self):
        oncall.cache_table = self.original_table
        oncall.jira_get_json = self.original_jira_get_json
        oncall.jira_request = self.original_jira_request
        oncall.OPSGENIE_API_KEY = self.original_opsgenie_key
        oncall.OPSGENIE_SCHEDULE_ID = self.original_opsgenie_schedule
        oncall.LIVE_AGENT_SCHEDULE_NAME = self.original_schedule_name
        oncall._jira_secret_cache = self.original_secret_cache

    def test_webhook_assignee_path(self):
        result = oncall.get_current_oncall_user(
            ticket_key="IVY-25",
            webhook_payload={
                "assignee_account_id": "712020:webhook",
                "assignee_display_name": "Karan",
                "assignee_email": "karan@example.com",
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "webhook_assignee")
        self.assertEqual(result["account_id"], "712020:webhook")
        self.assertEqual(result["display_name"], "Karan")
        self.assertEqual(result["email"], "karan@example.com")
        self.assertEqual(result["ticket_key"], "IVY-25")

    def test_jira_issue_assignee_path(self):
        def fake_jira_request(method, path, body=None, query=None):
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/rest/api/3/issue/IVY-26")
            self.assertEqual(query, {"fields": "assignee"})
            return {
                "ok": True,
                "status_code": 200,
                "body": {
                    "fields": {
                        "assignee": {
                            "accountId": "712020:jira",
                            "displayName": "Vibhu",
                            "emailAddress": "vibhu@example.com",
                        }
                    }
                },
            }

        oncall.jira_request = fake_jira_request
        result = oncall.get_current_oncall_user(ticket_key="IVY-26", webhook_payload={})

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "jira_issue_assignee")
        self.assertEqual(result["account_id"], "712020:jira")
        self.assertEqual(result["display_name"], "Vibhu")
        self.assertEqual(result["email"], "vibhu@example.com")
        self.assertEqual(result["ticket_key"], "IVY-26")

    def test_missing_assignee_returns_controlled_error(self):
        result = oncall.get_current_oncall_user(ticket_key=None, webhook_payload={})

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_oncall_user_available")

    def test_cache_write_and_read(self):
        user = {
            "ok": True,
            "source": "webhook_assignee",
            "account_id": "712020:cached",
            "display_name": "Cached Agent",
            "email": "cached@example.com",
            "ticket_key": "IVY-27",
        }

        item = oncall.cache_oncall_user(user)
        cached = oncall.get_cached_oncall_user()

        self.assertEqual(item["PK"], "SCHEDULE#live_agent")
        self.assertEqual(item["SK"], "CURRENT")
        self.assertTrue(cached["ok"])
        self.assertEqual(cached["source"], "manual_fallback")
        self.assertEqual(cached["account_id"], "712020:cached")
        self.assertEqual(cached["display_name"], "Cached Agent")
        self.assertEqual(cached["email"], "cached@example.com")
        self.assertEqual(cached["ticket_key"], "IVY-27")


class FakeHttpResponse:
    def __init__(self, status, body=""):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body.encode("utf-8")


class JiraHelperHttpTests(unittest.TestCase):
    def setUp(self):
        self.original_get_secret = oncall.get_jira_secret
        self.original_urlopen = oncall.urllib.request.urlopen
        self.original_sleep = oncall.time.sleep
        self.original_attempts = oncall.JIRA_MAX_ATTEMPTS
        self.original_unassign = oncall.UNASSIGN_WHEN_AGENT_FULL
        oncall.get_jira_secret = lambda: {
            "site_url": "https://example.atlassian.net",
            "email": "test@example.com",
            "api_token": "secret-token",
        }
        oncall.time.sleep = lambda seconds: None
        oncall.JIRA_MAX_ATTEMPTS = 3

    def tearDown(self):
        oncall.get_jira_secret = self.original_get_secret
        oncall.urllib.request.urlopen = self.original_urlopen
        oncall.time.sleep = self.original_sleep
        oncall.JIRA_MAX_ATTEMPTS = self.original_attempts
        oncall.UNASSIGN_WHEN_AGENT_FULL = self.original_unassign

    def test_assign_jira_issue_treats_204_as_success(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            return FakeHttpResponse(204)

        oncall.urllib.request.urlopen = fake_urlopen
        result = oncall.assign_jira_issue("IVY-80", "account-1")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 204)
        self.assertEqual(requests[0].method, "PUT")
        self.assertEqual(json.loads(requests[0].data), {"accountId": "account-1"})

    def test_unassign_jira_issue_sends_null_account_id_when_enabled(self):
        requests = []
        oncall.UNASSIGN_WHEN_AGENT_FULL = True
        oncall.urllib.request.urlopen = lambda request, timeout: (
            requests.append(request) or FakeHttpResponse(204)
        )

        result = oncall.unassign_jira_issue("IVY-81")

        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(requests[0].data), {"accountId": None})

    def test_get_jira_issue_assignee_returns_normalized_user(self):
        body = json.dumps({
            "fields": {
                "assignee": {
                    "accountId": "account-2",
                    "displayName": "Vibhu",
                    "emailAddress": "vibhu@example.com",
                }
            }
        })
        oncall.urllib.request.urlopen = lambda request, timeout: FakeHttpResponse(200, body)

        result = oncall.get_jira_issue_assignee("IVY-82")

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], "account-2")
        self.assertEqual(result["display_name"], "Vibhu")
        self.assertEqual(result["email"], "vibhu@example.com")

    def test_transient_503_is_retried(self):
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    503,
                    "Unavailable",
                    {},
                    io.BytesIO(),
                )
            return FakeHttpResponse(204)

        oncall.urllib.request.urlopen = fake_urlopen
        result = oncall.assign_jira_issue("IVY-83", "account-3")

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(calls), 2)

    def test_non_retryable_403_is_not_retried(self):
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "Forbidden",
                {},
                io.BytesIO(),
            )

        oncall.urllib.request.urlopen = fake_urlopen
        result = oncall.assign_jira_issue("IVY-84", "account-4")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 403)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
