import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import market_monitor


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


def settings(recipients: str = "first@example.com,second@example.com") -> SimpleNamespace:
    return SimpleNamespace(
        email_provider="resend",
        email_recipients=recipients,
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

        with patch.dict(sys.modules, {"requests": fake_requests}), patch.object(market_monitor.time, "sleep"):
            market_monitor.send_resend_email(settings(), "subject", "plain", "html")

        first_key = fake_requests.calls[0]["headers"]["Idempotency-Key"]
        retry_key = fake_requests.calls[1]["headers"]["Idempotency-Key"]
        second_recipient_key = fake_requests.calls[2]["headers"]["Idempotency-Key"]
        self.assertEqual(first_key, retry_key)
        self.assertNotEqual(first_key, second_recipient_key)

    def test_permanent_failure_does_not_block_later_recipient(self) -> None:
        fake_requests = FakeRequests([FakeResponse(400, "invalid recipient"), FakeResponse(200)])

        with patch.dict(sys.modules, {"requests": fake_requests}), patch.object(market_monitor.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "delivered to 1/2 recipients"):
                market_monitor.send_resend_email(settings(), "subject", "plain", "html")

        self.assertEqual(len(fake_requests.calls), 2)
        self.assertEqual(fake_requests.calls[1]["json"]["to"], ["second@example.com"])

    def test_smtp_sends_separate_messages_without_exposing_other_recipients(self) -> None:
        smtp_settings = SimpleNamespace(
            email_recipients="first@example.com,second@example.com",
            smtp_sender="sender@example.com",
            smtp_use_ssl=False,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_use_starttls=False,
            smtp_user="sender@example.com",
            smtp_password="test-password",
        )

        with patch.object(market_monitor.smtplib, "SMTP") as smtp_class:
            server = smtp_class.return_value.__enter__.return_value
            market_monitor.send_smtp_email(smtp_settings, "subject", "plain", "html")

        self.assertEqual(server.send_message.call_count, 2)
        first_call, second_call = server.send_message.call_args_list
        self.assertEqual(first_call.args[0]["To"], "first@example.com")
        self.assertEqual(first_call.kwargs["to_addrs"], ["first@example.com"])
        self.assertEqual(second_call.args[0]["To"], "second@example.com")
        self.assertEqual(second_call.kwargs["to_addrs"], ["second@example.com"])

    def test_requests_are_paced(self) -> None:
        fake_requests = FakeRequests([FakeResponse(200), FakeResponse(200)])

        with (
            patch.dict(sys.modules, {"requests": fake_requests}),
            patch.object(market_monitor.time, "monotonic", side_effect=[0.0, 0.0, 0.25]),
            patch.object(market_monitor.time, "sleep") as sleep_mock,
        ):
            market_monitor.send_resend_email(settings(), "subject", "plain", "html")

        sleep_mock.assert_called_once_with(0.25)

    def test_duplicate_recipients_are_sent_once(self) -> None:
        fake_requests = FakeRequests([FakeResponse(200), FakeResponse(200)])

        with patch.dict(sys.modules, {"requests": fake_requests}):
            market_monitor.send_resend_email(
                settings("first@example.com, FIRST@example.com, second@example.com"),
                "subject",
                "plain",
                "html",
            )

        self.assertEqual(len(fake_requests.calls), 2)
        self.assertEqual(fake_requests.calls[0]["json"]["to"], ["first@example.com"])
        self.assertEqual(fake_requests.calls[1]["json"]["to"], ["second@example.com"])

    def test_failure_notice_is_sent_only_to_first_configured_recipient(self) -> None:
        fake_requests = FakeRequests([FakeResponse(200)])

        with patch.dict(sys.modules, {"requests": fake_requests}):
            market_monitor.send_failure_email(
                settings("owner@example.com,employee1@example.com,employee2@example.com"),
                "test failure",
                Path("test.log"),
            )

        self.assertEqual(len(fake_requests.calls), 1)
        self.assertEqual(fake_requests.calls[0]["json"]["to"], ["owner@example.com"])
        self.assertEqual(fake_requests.calls[0]["json"]["subject"], "[市场监控异常] 日报运行失败")


if __name__ == "__main__":
    unittest.main()
