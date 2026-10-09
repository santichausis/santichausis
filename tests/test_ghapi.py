import io
import json
import os
import sys
import unittest
import urllib.error
from email.message import Message
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import ghapi  # noqa: E402


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", headers, None)


def ok(payload):
    return io.BytesIO(json.dumps(payload).encode())


class Scripted:
    """urlopen falso: devuelve/levanta lo que se le va indicando, en orden."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class RetryTests(unittest.TestCase):
    def run_api(self, *steps):
        fake = Scripted(*steps)
        sleeps = []
        with mock.patch.object(ghapi.urllib.request, "urlopen", fake), \
                mock.patch.object(ghapi, "_sleep", sleeps.append):
            try:
                result = ghapi.api("/x")
            except Exception as e:  # noqa: BLE001 - el test inspecciona la excepción
                result = e
        return result, fake.calls, sleeps

    def test_retries_502_then_succeeds(self):
        result, calls, sleeps = self.run_api(http_error(502), ok({"a": 1}))
        self.assertEqual(result, {"a": 1})
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [1])

    def test_404_is_not_retried(self):
        result, calls, sleeps = self.run_api(http_error(404))
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual((calls, sleeps), (1, []))

    def test_gives_up_after_max_attempts_with_exponential_backoff(self):
        result, calls, sleeps = self.run_api(*[http_error(503) for _ in range(ghapi.MAX_ATTEMPTS)])
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual(calls, ghapi.MAX_ATTEMPTS)
        self.assertEqual(sleeps, [1, 2, 4])

    def test_network_errors_are_retried(self):
        result, calls, _ = self.run_api(urllib.error.URLError("boom"), TimeoutError(), ok([1]))
        self.assertEqual(result, [1])
        self.assertEqual(calls, 3)

    def test_secondary_rate_limit_honors_retry_after(self):
        result, calls, sleeps = self.run_api(http_error(403, retry_after=2), ok({"ok": True}))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(sleeps, [2])

    def test_403_without_retry_after_is_not_retried(self):
        result, calls, _ = self.run_api(http_error(403))
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual(calls, 1)

    def test_retry_after_too_long_is_not_retried(self):
        result, calls, _ = self.run_api(http_error(429, retry_after=3600))
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual(calls, 1)

    def test_truncated_json_is_retried(self):
        result, calls, _ = self.run_api(io.BytesIO(b'{"a": '), ok({"a": 1}))
        self.assertEqual(result, {"a": 1})
        self.assertEqual(calls, 2)


class PaginateTests(unittest.TestCase):
    def test_stops_on_short_page(self):
        pages = [list(range(100)), list(range(3))]
        with mock.patch.object(ghapi, "api", side_effect=pages) as api:
            out = ghapi.paginate("/x")
        self.assertEqual(len(out), 103)
        self.assertEqual(api.call_count, 2)

    def test_respects_max_pages(self):
        with mock.patch.object(ghapi, "api", return_value=list(range(100))) as api:
            out = ghapi.paginate("/x", max_pages=3)
        self.assertEqual((len(out), api.call_count), (300, 3))


class GraphqlTests(unittest.TestCase):
    def test_requires_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                ghapi.graphql("query{}", {})

    def test_graphql_errors_raise(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}), \
                mock.patch.object(ghapi, "_open_json", return_value={"errors": [{"message": "nope"}]}):
            with self.assertRaises(RuntimeError):
                ghapi.graphql("query{}", {})

    def test_returns_data(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}), \
                mock.patch.object(ghapi, "_open_json", return_value={"data": {"x": 1}}):
            self.assertEqual(ghapi.graphql("query{}", {}), {"x": 1})


if __name__ == "__main__":
    unittest.main()
