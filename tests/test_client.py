import json
import math
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx

from quark_cli.client import API_HOST, QUARK_DESKTOP_USER_AGENT, QuarkClient
from quark_cli.errors import AccessRestrictedError, AuthenticationError, NetworkError, RateLimitError, RemoteError
from quark_cli.request_guard import REASON_ACCESS_RESTRICTED, REASON_RATE_LIMITED, RequestGuard


def ok(data):
    return {"status": 200, "code": 0, "data": data}


class ClientTests(unittest.TestCase):
    def test_pds_cookie_header_is_minimal_and_host_scoped(self):
        with QuarkClient(
            "sid=account-private; __puus=puus-private; other=other-private",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=ok({"list": []}), request=request)),
        ) as client:
            self.assertEqual(client.pds_cookie_header("dl-pc-sz.pds.quark.cn"), "__puus=puus-private")
            self.assertIsNone(client.pds_cookie_header("cdn.quark.cn"))
            self.assertIsNone(client.pds_cookie_header("pds.quark.cn"))
            self.assertIsNone(client.pds_cookie_header("bad..pds.quark.cn"))

    def test_transfer_cookie_header_allows_only_validated_quark_storage_subdomains(self):
        with QuarkClient("sid=account-private; __puus=puus-private; other=other-private") as client:
            self.assertEqual(client.transfer_cookie_header("dl-pc-sz.pds.quark.cn"), "__puus=puus-private")
            self.assertEqual(client.transfer_cookie_header("dl-pc.drive.quark.cn"), "__puus=puus-private")
            for host in ("quark.cn", "drive.quark.cn", "pds.quark.cn", "cdn.quark.cn",
                         "drive.quark.cn.attacker.example", "bad..drive.quark.cn"):
                with self.subTest(host=host):
                    self.assertIsNone(client.transfer_cookie_header(host))

    def test_cookie_only_goes_to_fixed_api_host(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=ok({"list": [], "_total": 0}), request=request)

        with QuarkClient("sid=mock-cookie", transport=httpx.MockTransport(handler)) as client:
            result = client.auth_status()
        self.assertTrue(result["authenticated"])
        self.assertEqual(seen[0].url.host, API_HOST)
        self.assertEqual(seen[0].headers["cookie"], "sid=mock-cookie")
        self.assertNotEqual(seen[0].headers["user-agent"], QUARK_DESKTOP_USER_AGENT)
        self.assertEqual(seen[0].url.path, "/1/clouddrive/file/sort")
        self.assertEqual(seen[0].url.params["pdir_fid"], "0")
        self.assertEqual(seen[0].url.params["_page"], "1")
        self.assertEqual(seen[0].url.params["_size"], "1")

    def test_api_refreshes_puus_for_following_api_and_pds_requests(self):
        seen = []

        def handler(request):
            seen.append(request.headers["cookie"])
            return httpx.Response(
                200,
                headers={"set-cookie": "__puus=refreshed-private; Domain=.quark.cn; Path=/; Secure"},
                json=ok({"list": []}),
                request=request,
            )

        with QuarkClient("sid=account-private; __puus=original-private; other=other-private", transport=httpx.MockTransport(handler)) as client:
            client.auth_status()
            self.assertEqual(client.pds_cookie_header("dl-pc-sz.pds.quark.cn"), "__puus=refreshed-private")
            client.auth_status()
            self.assertIn("__puus=refreshed-private", seen[1])
            self.assertNotIn("original-private", seen[1])
            self.assertIn("sid=account-private", seen[1])
            self.assertIn("other=other-private", seen[1])

    def test_api_errors_redact_refreshed_puus_and_original_credentials(self):
        def handler(request):
            return httpx.Response(
                200,
                headers={"set-cookie": "__puus=refreshed-private; Domain=.quark.cn; Path=/; Secure"},
                json={"status": 400, "code": 121, "message": "original-private refreshed-private account-private rejected"},
                request=request,
            )

        with QuarkClient("sid=account-private; __puus=original-private", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(RemoteError) as caught:
                client.auth_status()
        for credential in ("original-private", "refreshed-private", "account-private"):
            self.assertNotIn(credential, str(caught.exception))

    def test_puus_refresh_only_accepts_account_wide_cookie_scope(self):
        for cookie in (
            "__puus=foreign-private; Domain=attacker.invalid; Path=/",
            "__puus=path-private; Domain=.quark.cn; Path=/1/clouddrive",
            "__puus=host-private; Path=/",
            "sid=another-account-private; Domain=.quark.cn; Path=/",
        ):
            with self.subTest(cookie=cookie):
                def handler(request):
                    return httpx.Response(200, headers={"set-cookie": cookie}, json=ok({"list": []}), request=request)

                with QuarkClient("sid=account-private; __puus=original-private", transport=httpx.MockTransport(handler)) as client:
                    client.auth_status()
                    self.assertEqual(client.pds_cookie_header("dl-pc-sz.pds.quark.cn"), "__puus=original-private")

    def test_authentication_error_on_401(self):
        def handler(request):
            return httpx.Response(401, request=request)

        with QuarkClient("sid=mock-cookie", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(AuthenticationError):
                client.auth_status()

    def test_requires_explicit_api_success_signal(self):
        def handler(request):
            return httpx.Response(200, json={"data": {}}, request=request)

        with QuarkClient("sid=mock-cookie", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(RemoteError, "明确的成功状态"):
                client.auth_status()

    def test_auth_status_requires_read_only_list_shape(self):
        def handler(request):
            return httpx.Response(200, json=ok({"list": None}), request=request)

        with QuarkClient("sid=mock-cookie", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(RemoteError, "data.list"):
                client.auth_status()

    def test_business_error_redacts_cookie_and_urls(self):
        secret = "sid=mock-cookie"

        def handler(request):
            return httpx.Response(
                200,
                json={"status": 400, "code": 121, "message": f"{secret} failed https://host.test/path?signature=hidden"},
                request=request,
            )

        with QuarkClient(secret, transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(RemoteError) as caught:
                client.auth_status()
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("https://", str(caught.exception))
        self.assertNotIn("signature=hidden", str(caught.exception))

    def test_non_json_and_timeout_are_sanitized(self):
        def html_handler(request):
            return httpx.Response(200, text="<html>login</html>", request=request)

        with QuarkClient("sid=secret", transport=httpx.MockTransport(html_handler)) as client:
            with self.assertRaisesRegex(RemoteError, "非 JSON"):
                client.auth_status()

        def timeout_handler(request):
            raise httpx.ConnectTimeout("https://drive-pc.quark.cn/?cookie=secret", request=request)

        with QuarkClient("sid=secret", transport=httpx.MockTransport(timeout_handler)) as client:
            with self.assertRaises(NetworkError) as caught:
                client.auth_status()
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("https://", str(caught.exception))

    def test_explicit_business_rejection_has_only_safe_classification_flag(self):
        def handler(request):
            return httpx.Response(200, json={"status": 400, "code": 41001, "message": "rejected-private"}, request=request)
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(RemoteError) as caught:
            client.post("share", body={})
        self.assertTrue(caught.exception.api_rejected)
        self.assertFalse(hasattr(caught.exception, "payload"))

    def test_partial_and_accepted_or_malformed_responses_are_not_rejection(self):
        for payload in ({"status": 400, "code": 41060, "data": {}},
                        {"status": 400, "code": 41001, "data": {"task_id": "accepted"}},
                        {"status": 400, "code": 41001, "data": {"task_resp": {"data": {"creation_snapshot": {}}}}},
                        {"status": 500, "code": 41001}, {"status": True, "code": 41001},
                        {"status": "bad-status", "code": 41001}, {"status": 400, "code": True}):
            with self.subTest(payload=payload):
                def handler(request):
                    return httpx.Response(200, json=payload, request=request)
                with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client, self.assertRaises(RemoteError) as caught:
                    client.post("share", body={})
                self.assertFalse(getattr(caught.exception, "api_rejected", False))

    def test_partial_business_response_requires_explicit_opt_in(self):
        payload = {"status": 400, "code": 41060, "data": {"task_id": "original-task"}}
        def handler(request):
            return httpx.Response(200, json=payload, request=request)
        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(RemoteError):
                client.post("share", body={})
            self.assertEqual(client.post("share", body={}, accepted_business_codes=(41060,)), payload)


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _http_date(epoch):
    return format_datetime(datetime.fromtimestamp(epoch, timezone.utc), usegmt=True)


class GuardedRequestTests(unittest.TestCase):
    def test_rejected_post_is_sent_once_and_never_replayed(self):
        sends = []

        def handler(request):
            sends.append(request)
            return httpx.Response(200, json={"status": 400, "code": 41001, "message": "rejected"}, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(RemoteError) as caught:
                client.post("share", body={})
        self.assertTrue(caught.exception.api_rejected)
        self.assertEqual(len(sends), 1)

    def test_429_records_cooldown_and_blocks_further_sends(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)
        sends = []

        def handler(request):
            sends.append(request)
            return httpx.Response(429, headers={"retry-after": "120"}, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(RateLimitError) as caught:
                client.post("share", body={})
            self.assertEqual(caught.exception.retry_after, 120.0)
            with self.assertRaises(RateLimitError):
                client.get("task")
        self.assertEqual(len(sends), 1)
        cooldown = guard.active_cooldown()
        self.assertEqual(cooldown.reason, REASON_RATE_LIMITED)
        self.assertEqual(cooldown.until, 1120.0)

    def test_429_retry_after_is_never_shortened_and_defaults_to_sixty(self):
        for header, expected in ((None, 60.0), ("invalid", 60.0), ("30", 60.0), ("120", 120.0)):
            with self.subTest(header=header):
                fake = FakeClock()
                guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)
                headers = {} if header is None else {"retry-after": header}

                def handler(request):
                    return httpx.Response(429, headers=headers, request=request)

                with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
                    with self.assertRaises(RateLimitError) as caught:
                        client.auth_status()
                self.assertEqual(caught.exception.retry_after, expected)

    def test_429_and_403_http_date_retry_after_is_honoured(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)
        retry = {"retry-after": _http_date(fake.now + 300)}

        def handler(request):
            return httpx.Response(429, headers=retry, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(RateLimitError) as caught:
                client.auth_status()
        self.assertEqual(caught.exception.retry_after, 300.0)

        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)
        retry = {"retry-after": _http_date(fake.now + 900)}

        def forbid(request):
            return httpx.Response(403, headers=retry, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(forbid), guard=guard) as client:
            with self.assertRaises(AccessRestrictedError) as caught:
                client.auth_status()
        self.assertEqual(caught.exception.retry_after, 900.0)
        self.assertEqual(guard.active_cooldown().until, 1900.0)

    def test_403_is_access_restricted_preserves_cookie_and_uses_six_hundred_seconds(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)
        sends = []

        def handler(request):
            sends.append(request)
            return httpx.Response(403, request=request)

        with QuarkClient("sid=secret-cookie; __puus=puus-secret",
                         transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(AccessRestrictedError) as caught:
                client.auth_status()
            self.assertNotIsInstance(caught.exception, AuthenticationError)
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(client.pds_cookie_header("dl-pc-sz.pds.quark.cn"), "__puus=puus-secret")
            with self.assertRaises(AccessRestrictedError):
                client.get("task")
        self.assertEqual(len(sends), 1)
        cooldown = guard.active_cooldown()
        self.assertEqual(cooldown.reason, REASON_ACCESS_RESTRICTED)
        self.assertEqual(cooldown.until, 1600.0)

    def test_401_remains_authentication_error_without_cooldown(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)

        def handler(request):
            return httpx.Response(401, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(AuthenticationError):
                client.auth_status()
        self.assertIsNone(guard.active_cooldown())

    def test_503_only_cooldowns_when_retry_after_is_present(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)

        def handler(request):
            return httpx.Response(503, headers={"retry-after": "45"}, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(RemoteError) as caught:
                client.auth_status()
        self.assertEqual(caught.exception.retry_after, 45.0)
        self.assertEqual(guard.active_cooldown().until, 1045.0)

        plain = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)

        def bare(request):
            return httpx.Response(503, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(bare), guard=plain) as client:
            with self.assertRaises(RemoteError) as caught:
                client.auth_status()
        self.assertFalse(hasattr(caught.exception, "retry_after"))
        self.assertIsNone(plain.active_cooldown())

    def test_restriction_phrase_wins_over_auth_keywords_and_redacts_credentials(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)

        def handler(request):
            return httpx.Response(
                200,
                json={"status": 400, "code": 121, "message": "登录异常 访问受限 sid=secret-cookie 请稍后再试"},
                request=request,
            )

        with QuarkClient("sid=secret-cookie", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(AccessRestrictedError) as caught:
                client.auth_status()
        self.assertNotIsInstance(caught.exception, AuthenticationError)
        self.assertTrue(caught.exception.api_rejected)
        self.assertNotIn("secret-cookie", str(caught.exception))
        self.assertEqual(guard.active_cooldown().reason, REASON_ACCESS_RESTRICTED)

    def test_success_payload_mentioning_restriction_is_not_rejected(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)
        payload = {"status": 200, "code": 0, "data": {"notice": "请求过于频繁时请稍后再试"}}

        def handler(request):
            return httpx.Response(200, json=payload, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            self.assertEqual(client.get("sort"), payload)
        self.assertIsNone(guard.active_cooldown())

    def test_durable_guard_is_shared_between_clients_and_a_new_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = FakeClock()
            sends = []

            def limited(request):
                sends.append(request)
                return httpx.Response(429, headers={"retry-after": "90"}, request=request)

            first = RequestGuard.durable(directory=directory, clock=fake.clock, sleep=fake.sleep)
            with QuarkClient("sid=offline", transport=httpx.MockTransport(limited), guard=first) as client:
                with self.assertRaises(RateLimitError):
                    client.auth_status()
            self.assertEqual(len(sends), 1)

            recovered = []

            def ok_handler(request):
                recovered.append(request)
                return httpx.Response(200, json=ok({"list": []}), request=request)

            second = RequestGuard.durable(directory=directory, clock=fake.clock, sleep=fake.sleep)
            with QuarkClient("sid=offline", transport=httpx.MockTransport(ok_handler), guard=second) as client:
                with self.assertRaises(RateLimitError):
                    client.auth_status()
                self.assertEqual(recovered, [])
                fake.now += 91.0
                self.assertTrue(client.auth_status()["authenticated"])
            self.assertEqual(len(recovered), 1)

    def test_huge_retry_after_falls_back_to_sixty_without_persisting_infinity(self):
        for header in ("9" * 400, "invalid"):
            with self.subTest(header=header[:8]):
                with tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    fake = FakeClock()
                    guard = RequestGuard.durable(directory=directory, clock=fake.clock, sleep=fake.sleep)

                    def handler(request):
                        return httpx.Response(429, headers={"retry-after": header}, request=request)

                    with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
                        with self.assertRaises(RateLimitError) as caught:
                            client.auth_status()
                    self.assertEqual(caught.exception.retry_after, 60.0)
                    document = json.loads((directory / "request_guard.json").read_text("utf-8"))
                    self.assertEqual(document["cooldown_until"], 1060.0)
                    self.assertTrue(all(math.isfinite(value) for value in document.values() if isinstance(value, float)))

    def test_representable_long_retry_after_is_preserved_and_stays_finite(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = FakeClock()
            guard = RequestGuard.durable(directory=directory, clock=fake.clock, sleep=fake.sleep)

            def handler(request):
                return httpx.Response(429, headers={"retry-after": "999999999999"}, request=request)

            with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
                with self.assertRaises(RateLimitError) as caught:
                    client.auth_status()
            self.assertEqual(caught.exception.retry_after, 999999999999.0)
            document = json.loads((directory / "request_guard.json").read_text("utf-8"))
            self.assertTrue(math.isfinite(document["cooldown_until"]))

    def test_exact_business_429_is_rate_limited_not_authentication(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)

        def handler(request):
            return httpx.Response(200, json={"status": 400, "code": 429, "message": "请稍后再试"}, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(RateLimitError) as caught:
                client.post("share", body={})
        self.assertNotIsInstance(caught.exception, AuthenticationError)
        self.assertEqual(caught.exception.retry_after, 60.0)
        self.assertEqual(guard.active_cooldown().reason, REASON_RATE_LIMITED)

    def test_exact_status_429_is_rate_limited(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)

        def handler(request):
            return httpx.Response(200, json={"status": 429, "code": 0, "data": {}}, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(RateLimitError):
                client.post("share", body={})
        self.assertEqual(guard.active_cooldown().reason, REASON_RATE_LIMITED)

    def test_numeric_429_substrings_are_not_rate_limited(self):
        for payload in ({"status": 400, "code": 14290, "message": "error 429 noted"},
                        {"status": 400, "code": 41001, "message": "retry after 429 seconds"}):
            with self.subTest(payload=payload):
                fake = FakeClock()
                guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)

                def handler(request, payload=payload):
                    return httpx.Response(200, json=payload, request=request)

                with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
                    with self.assertRaises(RemoteError) as caught:
                        client.post("share", body={})
                self.assertNotIsInstance(caught.exception, RateLimitError)
                self.assertNotIsInstance(caught.exception, AccessRestrictedError)
                self.assertIsNone(guard.active_cooldown())

    def test_unknown_40301_is_not_an_authentication_failure(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)

        def handler(request):
            return httpx.Response(200, json={"status": 400, "code": 40301, "message": "操作失败，请稍后再试"}, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(RemoteError) as caught:
                client.post("share", body={})
        self.assertNotIsInstance(caught.exception, AuthenticationError)
        self.assertNotIsInstance(caught.exception, AccessRestrictedError)
        self.assertIsNone(guard.active_cooldown())

    def test_exact_business_403_stays_access_restricted_with_cooldown_and_advice(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)

        def handler(request):
            return httpx.Response(200, json={"status": 400, "code": 403, "message": "无权限"}, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(AccessRestrictedError) as caught:
                client.post("share", body={})
        self.assertNotIsInstance(caught.exception, AuthenticationError)
        self.assertEqual(caught.exception.retry_after, 600.0)
        self.assertIn("请在官方客户端确认账号状态后重试", str(caught.exception))
        self.assertEqual(guard.active_cooldown().reason, REASON_ACCESS_RESTRICTED)

    def test_business_restriction_carries_action_advice_not_only_upstream_text(self):
        fake = FakeClock()
        guard = RequestGuard.memory(clock=fake.clock, sleep=fake.sleep)

        def handler(request):
            return httpx.Response(200, json={"status": 400, "code": 121, "message": "访问受限"}, request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            with self.assertRaises(AccessRestrictedError) as caught:
                client.post("share", body={})
        self.assertIn("请在官方客户端确认账号状态后重试", str(caught.exception))
        self.assertIn("访问受限", str(caught.exception))

    def test_cooldown_recorded_during_pacing_wait_blocks_the_send(self):
        fake = FakeClock()
        sends = []
        holder = {}

        def sleepy(seconds):
            holder["guard"].record_cooldown(60.0, REASON_RATE_LIMITED)
            fake.now += seconds

        guard = RequestGuard.memory(pacing=0.5, clock=fake.clock, sleep=sleepy)
        holder["guard"] = guard

        def handler(request):
            sends.append(request)
            return httpx.Response(200, json=ok({"list": []}), request=request)

        with QuarkClient("sid=offline", transport=httpx.MockTransport(handler), guard=guard) as client:
            client.auth_status()
            with self.assertRaises(RateLimitError):
                client.get("task")
        self.assertEqual(len(sends), 1)

    def test_queued_request_uses_puus_refreshed_while_waiting_for_its_slot(self):
        fake = FakeClock()
        seen = []
        holder = {}

        def sleepy(seconds):
            # A concurrent writer refreshed the account __puus during our wait.
            holder["client"]._cookie = "sid=account-private; __puus=refreshed-during-wait"
            fake.now += seconds

        guard = RequestGuard.memory(pacing=0.5, clock=fake.clock, sleep=sleepy)

        def handler(request):
            seen.append(request.headers.get("cookie", ""))
            return httpx.Response(200, json=ok({"list": []}), request=request)

        with QuarkClient("sid=account-private; __puus=original-private",
                         transport=httpx.MockTransport(handler), guard=guard) as client:
            holder["client"] = client
            client.auth_status()
            client.auth_status()
        self.assertEqual(len(seen), 2)
        self.assertIn("__puus=original-private", seen[0])
        self.assertIn("__puus=refreshed-during-wait", seen[1])
        self.assertNotIn("original-private", seen[1])

    def test_durable_guard_state_never_contains_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fake = FakeClock()
            guard = RequestGuard.durable(directory=directory, clock=fake.clock, sleep=fake.sleep)
            guard.record_cooldown(60.0, REASON_RATE_LIMITED)
            text = (directory / "request_guard.json").read_text("utf-8")
            self.assertNotIn("sid", text)
            self.assertNotIn("secret", text)


if __name__ == "__main__":
    unittest.main()
