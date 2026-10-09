import unittest

import httpx

from quark_cli.client import API_HOST, QUARK_DESKTOP_USER_AGENT, QuarkClient
from quark_cli.errors import AuthenticationError, NetworkError, RemoteError


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


if __name__ == "__main__":
    unittest.main()
