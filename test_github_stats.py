import unittest
from unittest.mock import AsyncMock, Mock, patch

from github_stats import Queries


class RestDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = Mock()
        self.queries = Queries("user", "secret-token", self.session)
        self.path = "/repos/owner/project/traffic/views"

    def response(self, status, body="", headers=None, result=None):
        response = Mock(status=status, headers=headers or {})
        response.text = AsyncMock(return_value=body)
        response.json = AsyncMock(return_value=result)
        return response

    async def test_success_preserves_dictionary_and_list_payloads(self):
        for result in ({"views": [{"count": 3}]}, [{"author": {"login": "user"}}]):
            self.session.get = AsyncMock(return_value=self.response(200, result=result))
            self.assertEqual(await self.queries.query_rest(self.path), result)
            self.assertEqual(
                self.session.get.call_args.kwargs["headers"]["Authorization"],
                "token secret-token",
            )

    async def test_pending_statistics_retry_then_succeed(self):
        self.session.get = AsyncMock(side_effect=[
            self.response(202), self.response(200, result={"views": []})
        ])
        with patch("github_stats.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with patch("builtins.print") as output:
                self.assertEqual(await self.queries.query_rest(self.path), {"views": []})
        sleep.assert_awaited_once_with(2)
        self.assertIn("attempt 1/60", output.call_args_list[0].args[0])

    async def test_pending_statistics_exhaustion_names_endpoint(self):
        self.session.get = AsyncMock(return_value=self.response(202))
        with patch("github_stats.asyncio.sleep", new_callable=AsyncMock):
            with patch("builtins.print") as output:
                self.assertEqual(await self.queries.query_rest(self.path), {})
        self.assertEqual(self.session.get.await_count, 60)
        self.assertIn(self.path, output.call_args.args[0])
        self.assertIn("60 attempts", output.call_args.args[0])

    async def test_no_content_does_not_parse_json_or_blame_authentication(self):
        response = self.response(204)
        self.session.get = AsyncMock(return_value=response)
        with patch("builtins.print") as output:
            self.assertEqual(await self.queries.query_rest(self.path), {})
        response.json.assert_not_awaited()
        self.assertIn("not an authentication failure", output.call_args.args[0])

    async def test_forbidden_explains_endpoint_permissions_and_redacts_token(self):
        self.session.get = AsyncMock(return_value=self.response(
            403, '{"message": "Resource not accessible secret-token"}',
            {"X-GitHub-Request-Id": "request-id"},
        ))
        with patch("builtins.print") as output:
            self.assertEqual(await self.queries.query_rest(self.path), {})
        message = output.call_args.args[0]
        for expected in (self.path, "Resource not accessible", "push access", "request-id"):
            self.assertIn(expected, message)
        self.assertNotIn("secret-token", message)

    async def test_rate_limits_are_not_reported_as_permission_failures(self):
        for status, headers, body in (
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "123"}, ""),
            (403, {"Retry-After": "60"}, ""),
            (403, {}, '{"message": "You have exceeded a secondary rate limit."}'),
            (429, {}, ""),
        ):
            self.session.get = AsyncMock(return_value=self.response(status, body, headers))
            with patch("builtins.print") as output:
                self.assertEqual(await self.queries.query_rest(self.path), {})
            message = output.call_args.args[0]
            self.assertIn("rate limit reached", message)
            self.assertNotIn("Access forbidden", message)
            for name, value in headers.items():
                self.assertIn(f"{name}={value}", message)

    async def test_unauthorized_fails_with_github_message(self):
        self.session.get = AsyncMock(return_value=self.response(
            401, '{"message": "Bad credentials"}'
        ))
        with self.assertRaisesRegex(RuntimeError, "Bad credentials.*ACCESS_TOKEN"):
            await self.queries.query_rest(self.path)

    async def test_non_json_error_is_bounded_and_does_not_hide_status(self):
        self.session.get = AsyncMock(return_value=self.response(503, "upstream\n" * 100))
        with patch("builtins.print") as output:
            self.assertEqual(await self.queries.query_rest(self.path), {})
        message = output.call_args.args[0]
        self.assertIn("503", message)
        self.assertIn("upstream", message)
        self.assertNotIn("\n", message)
        self.assertLess(len(message), 500)

    async def test_transport_fallback_has_same_diagnostics(self):
        import aiohttp

        self.session.get = AsyncMock(side_effect=aiohttp.ClientConnectionError("offline"))
        response = Mock(status_code=403, headers={"Retry-After": "30"}, text="")
        with patch("github_stats.requests.get", return_value=response):
            with patch("builtins.print") as output:
                self.assertEqual(await self.queries.query_rest(self.path), {})
        self.assertIn("rate limit reached", output.call_args.args[0])
        self.assertIn("Retry-After=30", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
