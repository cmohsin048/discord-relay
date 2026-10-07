"""Offline configuration check by default. --live explicitly enables networking."""
import argparse
import asyncio
import getpass
import logging
import os
from pathlib import Path
import re
import warnings

from relay_core import load_config

BASE = Path(__file__).resolve().parent


def normalize_webhook_url(value):
    match = re.fullmatch(
        r"https://(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/(\d+)/([A-Za-z0-9_.-]+)/?",
        value.strip(), re.IGNORECASE,
    )
    if not match:
        raise ValueError("Expected a raw Discord webhook URL")
    return f"https://discord.com/api/webhooks/{match.group(1)}/{match.group(2)}"


def normalize_token(value):
    # discord.py-self sends this value verbatim as the Authorization header, so it must
    # be the bare personal-account token: no quotes, whitespace, or "Bot "/"Bearer " prefix.
    token = value.strip().strip("\"'").strip()
    if token.lower().startswith(("bot ", "bearer ")):
        raise ValueError("Bot and bearer tokens cannot log in through discord.py-self")
    if not token or len(token.split()) != 1:
        raise ValueError("Token must be a single value without spaces")
    return token


def read_secret(name, prompt):
    value = os.environ.get(name)
    if not value:
        # Fail instead of letting getpass fall back to visible input.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(prompt)
    if not value.strip():
        raise ValueError("Required credential is empty")
    return value.strip()


class RedactSecrets(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record):
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg, record.args = message, ()
        # Exception tracebacks may contain request URLs and credentials.
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(BASE / "config.json"))
    parser.add_argument("--live", action="store_true", help="Connect and forward; Discord self-bot ban risk remains")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError, TypeError):
        print("Configuration invalid or missing. Use config.example.json and specify source server/channel IDs and a distinct destination server ID. Credentials do not belong in config.json.")
        return 2
    if not args.live:
        print("OFFLINE CHECK PASSED. No account login, network connection, credential read, or forwarding occurred.")
        print(f"Routes: {len(config['routes'])}; destination webhooks: {len({r['webhook_env'] for r in config['routes']})}.")
        for route in config["routes"]:
            scope = "ALL ACCESSIBLE CHANNELS" if route['all_source_channels'] else f"{len(route['source_channel_ids'])} explicitly selected channels"
            print(f"- {route['name']}: {len(route['source_server_ids'])} source server(s); scope: {scope}; "
                  f"threads: {route['include_threads']}; author filter: {'enabled' if route['source_author_ids'] else 'ALL AUTHORS'}; "
                  f"webhook from {route['webhook_env']}.")
        print("This checks configuration syntax only, not permissions or whether the IDs exist.")
        print("Live mode requires --live. Personal-account automation can still result in a Discord ban.")
        return 0
    print("LIVE MODE: personal-account automation can result in restrictions or a permanent Discord ban.")
    stage = "loading dependencies"
    try:
        # Keep third-party imports and credential access entirely out of offline mode.
        from relay_live import run
        stage = "reading hidden credentials"
        try:
            token = normalize_token(read_secret("DISCORD_USER_TOKEN", "Discord user token (hidden): "))
        except ValueError:
            print("Token invalid. Paste the bare personal-account token without quotes or a 'Bot ' prefix; this relay cannot authenticate a bot application.")
            return 2
        webhooks, secrets = {}, [token]
        # Routes sharing a webhook_env share one destination webhook and one prompt.
        for env in dict.fromkeys(route["webhook_env"] for route in config["routes"]):
            names = ", ".join(route["name"] for route in config["routes"] if route["webhook_env"] == env)
            original_webhook = read_secret(env, f"Destination webhook URL for {names} [{env}] (hidden): ")
            try:
                webhooks[env] = normalize_webhook_url(original_webhook)
            except ValueError:
                print(f"Webhook URL in {env} invalid. Paste the raw HTTPS URL copied from Discord (discord.com or discordapp.com), without Markdown or quotation marks.")
                return 2
            secrets += [original_webhook, webhooks[env], webhooks[env].rsplit("/", 1)[-1]]
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        redactor = RedactSecrets(secrets)
        for handler in logging.getLogger().handlers:
            handler.addFilter(redactor)
        logging.getLogger("discord").setLevel(logging.CRITICAL)
        stage = "connecting to Discord and checking the destination"
        return asyncio.run(run(config, token, webhooks))
    except KeyboardInterrupt:
        return 0
    except ImportError:
        print("Live dependencies unavailable. Install requirements.txt in a dedicated virtual environment.")
        return 2
    except Exception as exc:
        # Do not print exception details or secret-bearing request URLs.
        kind = type(exc).__name__
        hints = {
            "LoginFailure": "Discord rejected the account credential.",
            "Forbidden": "Discord denied access; check account and destination permissions.",
            "NotFound": "The destination webhook may be deleted or invalid; create a replacement.",
            "GetPassWarning": "This terminal cannot hide input. Use an interactive PowerShell window.",
            "ClientConnectorError": "Could not reach Discord; check the network connection.",
            "TimeoutError": "The connection timed out.",
            "ValueError": "Check that each route's webhook belongs to its destination server and another relay is not already running.",
        }
        print(f"Live startup stopped while {stage} ({kind}). {hints.get(kind, 'Check configuration and connectivity.')} No credential values are shown.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
