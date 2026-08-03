import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import ndx_monitor


class FakeRequestException(Exception):
    pass


class FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeRequests:
    RequestException = FakeRequestException

    def __init__(self, outcomes: list[FakeResponse | Exception]):
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def post(self, _url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def settings(recipients: str = "first@example.com", bcc: str = "second@example.com") -> SimpleNamespace:
    return SimpleNamespace(
        smtp_recipient=recipients,
        smtp_bcc=bcc,
        resend_from="Market Monitor <report@example.com>",
        resend_api_key="test-key",
        resend_requests_per_second=4.0,
        resend_max_attempts=4,
        resend_retry_delay_seconds=1.0,
        request_timeout_seconds=20,
    )


class ResendDeliveryTests(unittest.TestCase):
    def test_429_is_retried_with_same_idempotency_key(self) -> None:
        fake_requests = FakeRequests(
            [
                FakeResponse(429, "rate limited", {"retry-after": "0"}),
                FakeResponse(200),
                FakeResponse(200),
            ]
        )

        with patch.dict(sys.modules, {"requests": fake_requests}), patch.object(ndx_monitor.time, "sleep"):
            ndx_monitor.send_resend_email(settings(), "subject", "plain", "html")

        first_key = fake_requests.calls[0]["headers"]["Idempotency-Key"]
        retry_key = fake_requests.calls[1]["headers"]["Idempotency-Key"]
        second_recipient_key = fake_requests.calls[2]["headers"]["Idempotency-Key"]
        self.assertEqual(first_key, retry_key)
        self.assertNotEqual(first_key, second_recipient_key)

    def test_permanent_failure_does_not_block_later_recipient(self) -> None:
        fake_requests = FakeRequests([FakeResponse(400, "invalid recipient"), FakeResponse(200)])

        with patch.dict(sys.modules, {"requests": fake_requests}), patch.object(ndx_monitor.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "delivered to 1/2 recipients"):
                ndx_monitor.send_resend_email(settings(), "subject", "plain", "html")

        self.assertEqual(len(fake_requests.calls), 2)
        self.assertEqual(fake_requests.calls[1]["json"]["to"], ["second@example.com"])

    def test_requests_are_paced(self) -> None:
        fake_requests = FakeRequests([FakeResponse(200), FakeResponse(200)])

        with (
            patch.dict(sys.modules, {"requests": fake_requests}),
            patch.object(ndx_monitor.time, "monotonic", side_effect=[0.0, 0.0, 0.25]),
            patch.object(ndx_monitor.time, "sleep") as sleep_mock,
        ):
            ndx_monitor.send_resend_email(settings(), "subject", "plain", "html")

        sleep_mock.assert_called_once_with(0.25)


if __name__ == "__main__":
    unittest.main()
