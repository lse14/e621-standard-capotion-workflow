from __future__ import annotations

import io
import json
import socket
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "src"))

from anima_core.nl_diagnostics import MAX_RESPONSE_BYTES, TEST_MESSAGE, NlDiagnosticClient


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.read_limits: list[int] = []

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        self.read_limits.append(limit)
        return self.body

    def getcode(self) -> int:
        return self.status


class FakeOpener:
    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        self.outcome = outcome
        self.requests: list[object] = []
        self.timeouts: list[float] = []

    def open(self, request: object, *, timeout: float) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    @property
    def request(self) -> object:
        return self.requests[0]


def _models(models: list[object]) -> bytes:
    return json.dumps({"data": [{"id": model} for model in models]}).encode("utf-8")


def _completion(
    content: object = "Connected.",
    *,
    model: str = "actual-model",
    usage: object = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
) -> bytes:
    return json.dumps({"model": model, "choices": [{"message": {"content": content}}], "usage": usage}).encode("utf-8")


class NlDiagnosticClientTests(unittest.TestCase):
    def test_model_discovery_normalizes_the_models_url_and_sorts_results(self) -> None:
        fake = FakeOpener(FakeResponse(_models(["Beta", "alpha", "ALPHA"])))
        result = NlDiagnosticClient(opener=fake, clock=lambda: 1.0).discover_models(
            endpoint="https://example.test/v1",
            api_key="secret-value",
        )
        self.assertEqual("https://example.test/v1/models", fake.request.full_url)
        self.assertEqual("GET", fake.request.get_method())
        self.assertEqual("Bearer secret-value", fake.request.get_header("Authorization"))
        self.assertEqual(["alpha", "Beta"], result["models"])
        self.assertEqual((True, 0, None, None), (result["ok"], result["latencyMs"], result["errorCode"], result["errorReason"]))
        self.assertEqual(1, len(fake.requests))

    def test_remote_http_normalizes_and_issues_models_request(self) -> None:
        fake = FakeOpener(FakeResponse(_models(["remote-model"])))
        result = NlDiagnosticClient(opener=fake).discover_models(
            endpoint="http://provider.example/v1",
            api_key="secret-value",
        )
        self.assertTrue(result["ok"])
        self.assertEqual("http://provider.example/v1/models", fake.request.full_url)

    def test_test_message_uses_current_unsaved_values_once(self) -> None:
        fake = FakeOpener(FakeResponse(_completion()))
        result = NlDiagnosticClient(opener=fake, clock=iter((1.0, 1.234)).__next__).test_message(
            endpoint="https://example.test/v1/chat/completions",
            model="model-a",
            api_key="secret-value",
            base_prompt="Base rules",
        )
        self.assertEqual("https://example.test/v1/chat/completions", fake.request.full_url)
        self.assertEqual("POST", fake.request.get_method())
        body = json.loads(fake.request.data)
        self.assertEqual("model-a", body["model"])
        self.assertEqual("Base rules", body["messages"][0]["content"])
        self.assertEqual(TEST_MESSAGE, body["messages"][1]["content"])
        self.assertEqual((0, 64), (body["temperature"], body["max_tokens"]))
        self.assertEqual((True, 234, "actual-model", "Connected."), (result["ok"], result["latencyMs"], result["actualModel"], result["replyText"]))
        self.assertEqual({"promptTokens": 3, "completionTokens": 4, "totalTokens": 7}, result["usage"])
        self.assertEqual(1, len(fake.requests))

    def test_invalid_endpoints_do_not_issue_requests(self) -> None:
        for endpoint in (
            "https://user:pass@example.test/v1",
            "https://example.test/v1?key=x",
            "https://example.test/v1#fragment",
            "https://example.test/ white",
            "https:///v1",
        ):
            with self.subTest(endpoint=endpoint):
                fake = FakeOpener(FakeResponse(_models([])))
                result = NlDiagnosticClient(opener=fake).discover_models(endpoint=endpoint, api_key="secret-value")
                self.assertEqual((False, "invalid_endpoint"), (result["ok"], result["errorCode"]))
                self.assertEqual([], fake.requests)

    def test_loopback_http_is_allowed(self) -> None:
        fake = FakeOpener(FakeResponse(_models([])))
        result = NlDiagnosticClient(opener=fake).discover_models(endpoint="http://127.0.0.1:1234/v1", api_key="secret-value")
        self.assertTrue(result["ok"])
        self.assertEqual("http://127.0.0.1:1234/v1/models", fake.request.full_url)

    def test_provider_and_transport_failures_have_stable_codes(self) -> None:
        cases = (
            (HTTPError("https://example.test", 302, "redirect", None, io.BytesIO(b"")), "redirect_rejected"),
            (HTTPError("https://example.test", 401, "unauthorized", None, io.BytesIO(b"")), "authentication_failed"),
            (HTTPError("https://example.test", 403, "forbidden", None, io.BytesIO(b"")), "authentication_failed"),
            (HTTPError("https://example.test", 404, "missing", None, io.BytesIO(b"")), "endpoint_or_model_not_found"),
            (HTTPError("https://example.test", 429, "slow down", None, io.BytesIO(b"")), "rate_limited"),
            (HTTPError("https://example.test", 503, "unavailable", None, io.BytesIO(b"")), "provider_unavailable"),
            (HTTPError("https://example.test", 418, "rejected", None, io.BytesIO(b"")), "provider_rejected"),
            (TimeoutError("timed out"), "timeout"),
            (URLError(socket.timeout("timed out")), "timeout"),
            (URLError(ssl.SSLError("tls failure")), "connection_failed"),
            (URLError(OSError("connection refused")), "connection_failed"),
            (FakeResponse(b"not json"), "response_invalid"),
            (FakeResponse(json.dumps({"data": "not-a-list"}).encode("utf-8")), "response_invalid"),
            (FakeResponse(b"x" * (MAX_RESPONSE_BYTES + 1)), "response_too_large"),
        )
        for outcome, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                fake = FakeOpener(outcome)
                result = NlDiagnosticClient(opener=fake).discover_models(endpoint="https://example.test/v1", api_key="secret-value")
                self.assertEqual((False, expected_code), (result["ok"], result["errorCode"]))
                self.assertEqual(1, len(fake.requests))

    def test_completion_accepts_text_parts_and_reports_usage_without_estimating(self) -> None:
        fake = FakeOpener(FakeResponse(_completion([{"type": "text", "text": "One "}, {"type": "text", "text": "two."}], usage={"prompt_tokens": 1, "completion_tokens": -1})))
        result = NlDiagnosticClient(opener=fake).test_message(endpoint="https://example.test/v1", model="model-a", api_key="secret-value", base_prompt="Base rules")
        self.assertEqual((True, "actual-model", "One two."), (result["ok"], result["actualModel"], result["replyText"]))
        self.assertEqual({"promptTokens": 1, "completionTokens": None, "totalTokens": None}, result["usage"])

    def test_invalid_completion_is_fail_closed(self) -> None:
        for body in (
            b"not json",
            json.dumps({"choices": []}).encode("utf-8"),
            json.dumps({"choices": [{"message": {"content": [{"type": "image"}]}}]}).encode("utf-8"),
            json.dumps({"model": "", "choices": [{"message": {"content": "ok"}}]}).encode("utf-8"),
        ):
            with self.subTest(body=body[:32]):
                fake = FakeOpener(FakeResponse(body))
                result = NlDiagnosticClient(opener=fake).test_message(endpoint="https://example.test/v1", model="model-a", api_key="secret-value", base_prompt="Base rules")
                self.assertEqual((False, "response_invalid"), (result["ok"], result["errorCode"]))

    def test_model_limit_deduplication_and_read_bound_are_exact(self) -> None:
        response = FakeResponse(_models([f"model-{index:04d}" for index in range(1_010)]))
        fake = FakeOpener(response)
        result = NlDiagnosticClient(opener=fake).discover_models(endpoint="https://example.test/v1", api_key="secret-value")
        self.assertTrue(result["ok"])
        self.assertEqual(1_000, len(result["models"]))
        self.assertEqual(("model-0000", "model-0999"), (result["models"][0], result["models"][-1]))
        self.assertEqual([MAX_RESPONSE_BYTES + 1], response.read_limits)

    def test_failure_results_sanitize_provider_messages_and_never_echo_secrets(self) -> None:
        endpoint = "https://example.test/v1"
        secret = "secret-value"
        message = f"Authorization: Bearer {secret}\n{endpoint}\tC:\\private\\trace.py {secret} " + "z" * 600
        error = HTTPError(endpoint, 401, "unauthorized", None, io.BytesIO(json.dumps({"error": {"message": message}}).encode("utf-8")))
        fake = FakeOpener(error)
        result = NlDiagnosticClient(opener=fake).discover_models(endpoint=endpoint, api_key=secret)
        rendered = repr(result)
        self.assertEqual("authentication_failed", result["errorCode"])
        self.assertNotIn(secret, rendered)
        self.assertNotIn(endpoint, rendered)
        self.assertNotIn("Authorization", rendered)
        self.assertNotIn("C:\\private", rendered)
        self.assertLessEqual(len(str(result["errorReason"])), 512)
        self.assertNotIn("\n", str(result["errorReason"]))


if __name__ == "__main__":
    unittest.main()
