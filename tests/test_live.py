"""Offline tests for rate-limit retries and token handling; no network or login."""
import asyncio
import unittest
from unittest.mock import patch

import discord

import relay
import relay_live
from relay_live import call_with_rate_limit, retry_after_seconds


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status = status
        self.reason = "Too Many Requests" if status == 429 else "Error"
        self.headers = headers or {}


def http_error(status, headers=None):
    return discord.HTTPException(FakeResponse(status, headers), {"message": "synthetic", "code": 0})


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

        async def fake_sleep(seconds):
            self.sleeps.append(seconds)

        self.sleep = patch("relay_live.asyncio.sleep", side_effect=fake_sleep)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_retry_after_header_plus_buffer_or_fallback(self):
        self.assertAlmostEqual(retry_after_seconds(http_error(429, {"Retry-After": "3.5"})), 3.75)
        self.assertEqual(retry_after_seconds(http_error(429)), 2.0)
        self.assertEqual(retry_after_seconds(http_error(429, {"Retry-After": "junk"})), 2.0)
        self.assertAlmostEqual(retry_after_seconds(discord.RateLimited(4.0)), 4.25)

    def test_429_waits_retry_after_then_succeeds(self):
        outcomes = [http_error(429, {"Retry-After": "1.5"}), http_error(429), "sent"]

        async def send(**kwargs):
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        result = asyncio.run(call_with_rate_limit("webhook send", send, content="x"))
        self.assertEqual(result, "sent")
        self.assertEqual(self.sleeps, [1.75, 2.0])

    def test_non_429_errors_are_not_retried(self):
        calls = []

        async def send():
            calls.append(1)
            raise http_error(400)

        with self.assertRaises(discord.HTTPException):
            asyncio.run(call_with_rate_limit("webhook send", send))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_persistent_429_gives_up_as_rate_limited(self):
        calls = []

        async def send():
            calls.append(1)
            raise http_error(429, {"Retry-After": "0.5"})

        with self.assertRaises(discord.RateLimited) as raised:
            asyncio.run(call_with_rate_limit("webhook send", send))
        self.assertEqual(len(calls), relay_live.RATE_LIMIT_ATTEMPTS)
        self.assertEqual(len(self.sleeps), relay_live.RATE_LIMIT_ATTEMPTS - 1)
        self.assertAlmostEqual(raised.exception.retry_after, 0.75)

    def test_uploads_are_rewound_before_retry(self):
        class Upload:
            resets = 0

            def reset(self, *, seek=True):
                Upload.resets += 1

        outcomes = [http_error(429), "sent"]

        async def send(**kwargs):
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        asyncio.run(call_with_rate_limit("webhook send", send, files=[Upload(), Upload()]))
        self.assertEqual(Upload.resets, 2)


class TokenTests(unittest.TestCase):
    def test_bare_token_is_kept_and_quotes_removed(self):
        self.assertEqual(relay.normalize_token(' "synthetic.token.value" \n'), "synthetic.token.value")

    def test_bot_prefix_and_spaces_are_rejected(self):
        for value in ("Bot synthetic", "bearer synthetic", "two words", "   "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                relay.normalize_token(value)


if __name__ == "__main__":
    unittest.main()
