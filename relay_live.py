"""New-message relay. Credentials come only from environment or hidden prompts."""
import asyncio
import contextlib
import logging
import os
import random
import socket
from pathlib import Path

import aiohttp
import discord

from relay_core import Queue, matching_routes, selected, split_text

LOG = logging.getLogger("relay")
BASE = Path(__file__).resolve().parent

# Outbound pacing and rate-limit policy. discord.py-self already retries a 429 up to
# five times using Discord's retry_after; this layer covers the cases where the
# library gives up (persistent or Cloudflare 429s) so a temporary limit never marks
# a message failed. A 429 means the request was rejected, so retrying cannot duplicate.
SEND_DELAY_RANGE = (1.5, 3.0)
RATE_LIMIT_BUFFER = 0.25
RATE_LIMIT_FALLBACK = 2.0
RATE_LIMIT_ATTEMPTS = 8


def is_rate_limited(exc):
    return isinstance(exc, discord.RateLimited) or (
        isinstance(exc, discord.HTTPException) and exc.status == 429)


def retry_after_seconds(exc):
    """Seconds to wait for a 429: the Retry-After header plus a small buffer, else the fallback."""
    if isinstance(exc, discord.RateLimited):
        return exc.retry_after + RATE_LIMIT_BUFFER
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        seconds = float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return RATE_LIMIT_FALLBACK
    if seconds < 0:
        return RATE_LIMIT_FALLBACK
    return seconds + RATE_LIMIT_BUFFER


async def call_with_rate_limit(label, call, *args, **kwargs):
    """Run one Discord API call, honouring HTTP 429 Retry-After before each retry."""
    for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
        try:
            return await call(*args, **kwargs)
        except (discord.RateLimited, discord.HTTPException) as exc:
            if not is_rate_limited(exc):
                raise
            delay = retry_after_seconds(exc)
            if attempt == RATE_LIMIT_ATTEMPTS:
                LOG.error("Rate limited on %s %s times in a row; giving up for now", label, attempt)
                raise discord.RateLimited(delay) from None
            LOG.warning("Rate limited on %s (attempt %s/%s); waiting %.2fs before retrying",
                        label, attempt, RATE_LIMIT_ATTEMPTS, delay)
            for file in kwargs.get("files") or ():
                file.reset(seek=True)  # rewind uploads the rejected attempt consumed
            await asyncio.sleep(delay)


class Relay(discord.Client):
    def __init__(self, config, webhook_urls, queue):
        super().__init__()
        self.config = config
        self.webhook_urls = webhook_urls  # webhook_env name -> URL
        self.webhook_ids = set()
        self.queue = queue
        self.workers = []
        self.monitor = None
        self.session = None
        self.fatal = False

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        # Verify every destination before starting any delivery worker.
        webhooks = {}
        for env, url in self.webhook_urls.items():
            webhook = discord.Webhook.from_url(url, session=self.session)
            destination = await call_with_rate_limit("webhook check", webhook.fetch)
            for route in self.config["routes"]:
                if route["webhook_env"] == env and destination.guild_id != route["destination_server_id"]:
                    raise ValueError(f"Webhook for route {route['name']} does not belong to its destination server")
            webhooks[env] = webhook
            self.webhook_ids.add(destination.id)
        # One worker per destination webhook so a busy destination does not delay the others.
        for env, webhook in webhooks.items():
            routes = [route for route in self.config["routes"] if route["webhook_env"] == env]
            self.workers.append(asyncio.create_task(self.deliver(webhook, routes)))
        self.monitor = asyncio.create_task(self.health())

    async def on_ready(self):
        LOG.info("Connected. Monitoring %s route(s). Queue: %s", len(self.config["routes"]), self.queue.counts())
        present = {guild.id for guild in self.guilds}
        for route in self.config["routes"]:
            for guild_id in route["source_server_ids"] - present:
                LOG.error("Route %s: source server %s is not accessible", route["name"], guild_id)

    async def on_message(self, message):
        if not message.guild or message.author.id == self.user.id:
            return
        if message.webhook_id in self.webhook_ids:
            return  # never relay this relay's own output
        routes = matching_routes(self.config, message.guild.id, message.channel.id,
                                 getattr(message.channel, "parent_id", None), message.author.id)
        if not routes or not (message.content or message.attachments or message.embeds):
            return
        try:
            for route in routes:
                added = self.queue.enqueue(message.id, message.channel.id, route["name"],
                                           self.config["max_pending_messages"])
                if added:
                    LOG.info("Queued source message %s for route %s", message.id, route["name"])
        except Exception:
            LOG.error("Unable to persist message %s; stopping to avoid silent loss", message.id)
            self.fatal = True
            await self.close()

    async def on_error(self, event_method, *args, **kwargs):
        # Library exception strings can contain sensitive request details.
        LOG.error("Event handler failed: %s; inspect configuration and queue", event_method)

    async def on_disconnect(self):
        LOG.warning("Discord disconnected; library will attempt reconnection")

    async def health(self):
        while not self.is_closed():
            await asyncio.sleep(60)
            LOG.info("Health: connected=%s queue=%s", self.is_ready(), self.queue.counts())

    async def deliver(self, webhook, routes):
        routes = {route["name"]: route for route in routes}
        await self.wait_until_ready()
        while not self.is_closed():
            row = self.queue.next(routes)
            if row is None:
                await asyncio.sleep(1)
                continue
            message_id, channel_id, route_name, start_part = row
            route = routes[route_name]
            files = []
            sending = False
            try:
                channel = self.get_channel(channel_id) or await call_with_rate_limit(
                    "channel fetch", self.fetch_channel, channel_id)
                message = await call_with_rate_limit("message fetch", channel.fetch_message, message_id)
                if not message.guild or not selected(
                    route, message.guild.id, channel_id,
                    getattr(channel, "parent_id", None), message.author.id
                ):
                    self.queue.status(message_id, route_name, "skipped")
                    continue
                if len(message.attachments) > 10 or sum(a.size for a in message.attachments) > self.config["max_total_attachment_mb"] * 1024 * 1024:
                    self.queue.status(message_id, route_name, "failed")
                    LOG.error("Message %s exceeds configured attachment limits; nothing sent", message_id)
                    continue
                parts = split_text(message.content)
                # Upload original files rather than relying on expiring attachment URLs.
                if start_part == 0:
                    for attachment in message.attachments:
                        files.append(await call_with_rate_limit("attachment download", attachment.to_file))
                embeds = []
                for embed in message.embeds[:10]:
                    if embed.type == "rich":
                        payload = embed.to_dict()
                        for key in ("type", "provider", "video"):
                            payload.pop(key, None)
                        embeds.append(discord.Embed.from_dict(payload))
                for index in range(start_part, len(parts)):
                    self.queue.status(message_id, route_name, "sending")
                    sending = True
                    result = await call_with_rate_limit(
                        "webhook send", webhook.send,
                        content=parts[index], username=route["webhook_username"],
                        embeds=embeds if index == 0 else [],
                        files=files if index == 0 else [],
                        allowed_mentions=discord.AllowedMentions.none(), wait=True,
                    )
                    self.queue.acknowledge(message_id, route_name, result.id, index == len(parts) - 1)
                    sending = False
                    # Pace outbound sends only after Discord accepted this part.
                    await asyncio.sleep(random.uniform(*SEND_DELAY_RANGE))
                LOG.info("Delivered source message %s via route %s", message_id, route_name)
            except (discord.NotFound, discord.Forbidden) as exc:
                self.queue.status(message_id, route_name, "failed")
                LOG.error("Access unavailable for message %s on route %s (HTTP %s)",
                          message_id, route_name, exc.status)
                if sending:
                    LOG.error("Destination for route %s unavailable; stopping. Fix access before restarting",
                              route_name)
                    self.fatal = True
                    await self.close()
                    return
            except discord.RateLimited as exc:
                # Every attempt was rejected, so nothing was sent; keep the entry for later.
                self.queue.status(message_id, route_name, "pending")
                pause = max(exc.retry_after, 30.0)
                LOG.error("Message %s not sent on route %s: rate limit persisted; pausing this destination for %.0fs",
                          message_id, route_name, pause)
                await asyncio.sleep(pause)
            except discord.HTTPException as exc:
                self.queue.status(message_id, route_name, "uncertain" if sending and exc.status >= 500 else "failed")
                LOG.error("Message %s %s failed on route %s (HTTP %s); check queue",
                          message_id, "send" if sending else "read", route_name, exc.status)
                if exc.status == 401:
                    self.fatal = True
                    await self.close()
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                if sending:
                    self.queue.status(message_id, route_name, "uncertain")
                    LOG.error("Send outcome unknown for %s on route %s; not automatically duplicating it",
                              message_id, route_name)
                else:
                    LOG.warning("Temporary read/download failure for %s; retrying", message_id)
                    await asyncio.sleep(15)
            except Exception as exc:
                self.queue.status(message_id, route_name, "uncertain" if sending else "failed")
                LOG.error("Message %s %s failed on route %s (%s); check queue",
                          message_id, "send" if sending else "read", route_name, type(exc).__name__)
            finally:
                for file in files:
                    file.close()

    async def close(self):
        for task in (*self.workers, self.monitor):
            if task and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await super().close()
        if self.session and not self.session.closed:
            await self.session.close()


async def run(config, token, webhook_urls):
    data = Path(os.environ.get("RELAY_DATA_DIR", str(BASE / "data")))
    # One running copy per host. This socket is never used to accept connections.
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 47861))
    except OSError:
        raise ValueError("Another relay is running, or local lock port 47861 is occupied") from None
    # A pre-routes queue's history is assigned to the first route so it is not resent.
    queue = Queue(data / "deliveries.sqlite3", legacy_route=config["routes"][0]["name"])
    try:
        async with Relay(config, webhook_urls, queue) as client:
            await client.start(token.strip(), reconnect=True)
            return 2 if client.fatal else 0
    finally:
        queue.db.close()
        lock.close()


if __name__ == "__main__":
    raise SystemExit("Use relay.py. Live forwarding requires the explicit --live option.")
