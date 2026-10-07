"""Configuration and durable delivery state; no Discord credentials stored here."""
import json
import re
import sqlite3
from pathlib import Path

ROUTE_FIELDS = {"name", "source_server_ids", "source_channel_ids", "source_author_ids",
                "destination_server_id", "include_threads", "all_source_channels",
                "webhook_env", "webhook_username"}
GLOBAL_FIELDS = {"routes", "max_total_attachment_mb", "max_pending_messages"}
DEFAULT_WEBHOOK_ENV = "DISCORD_DEST_WEBHOOK"
DEFAULT_ROUTE = "default"


def _id_set(route, key):
    values = route.get(key, [])
    if not isinstance(values, list):
        raise ValueError(f"{key} must be a JSON list")
    if any(isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() for value in values):
        raise ValueError(f"{key} must contain decimal integer IDs")
    ids = {int(value) for value in values}
    if any(value <= 0 for value in ids):
        raise ValueError(f"{key} must contain positive IDs")
    return ids


def _load_route(route, index):
    if not isinstance(route, dict) or set(route) - ROUTE_FIELDS:
        raise ValueError("Unsupported route fields; keep credentials outside this file")
    name = route.get("name", f"route{index + 1}")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("Route name must be 1-64 letters, digits, '_' or '-'")
    parsed = {"name": name}
    for key in ("source_server_ids", "source_channel_ids", "source_author_ids"):
        parsed[key] = _id_set(route, key)
    if not parsed["source_server_ids"]:
        raise ValueError(f"Route {name}: at least one source_server_ids entry is required")
    for key in ("all_source_channels", "include_threads"):
        parsed[key] = route.get(key, False)
        if not isinstance(parsed[key], bool):
            raise ValueError(f"Route {name}: {key} must be true or false")
    if not parsed["source_channel_ids"] and not parsed["all_source_channels"]:
        raise ValueError(f"Route {name}: list source_channel_ids or explicitly enable all_source_channels")
    destination = route.get("destination_server_id", "")
    if isinstance(destination, bool) or not str(destination).isascii() or not str(destination).isdigit():
        raise ValueError(f"Route {name}: destination_server_id must be a positive integer ID")
    parsed["destination_server_id"] = int(destination)
    if parsed["destination_server_id"] <= 0:
        raise ValueError(f"Route {name}: destination_server_id must be positive")
    # The webhook URL is a credential, so the config only names the variable holding it.
    parsed["webhook_env"] = route.get("webhook_env", DEFAULT_WEBHOOK_ENV)
    if not isinstance(parsed["webhook_env"], str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", parsed["webhook_env"]):
        raise ValueError(f"Route {name}: webhook_env must be an UPPER_CASE environment variable name")
    parsed["webhook_username"] = route.get("webhook_username", "Signal Relay")
    if not isinstance(parsed["webhook_username"], str) or not 1 <= len(parsed["webhook_username"]) <= 80:
        raise ValueError(f"Route {name}: webhook_username must be 1-80 characters")
    return parsed


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    if "routes" in config:
        if set(config) - GLOBAL_FIELDS:
            raise ValueError("Unsupported configuration fields; keep credentials outside this file")
        routes = config["routes"]
        if not isinstance(routes, list) or not routes:
            raise ValueError("routes must be a non-empty JSON list")
    else:
        # Single-route legacy layout: route fields at the top level.
        legacy = {key: value for key, value in config.items() if key not in GLOBAL_FIELDS}
        if set(legacy) & {"name", "webhook_env", "webhook_username"}:
            raise ValueError("Route-only fields require the routes layout")
        routes = [dict(legacy, name=DEFAULT_ROUTE)]
    parsed = {"routes": [_load_route(route, index) for index, route in enumerate(routes)]}
    names = [route["name"] for route in parsed["routes"]]
    if len(names) != len(set(names)):
        raise ValueError("Route names must be unique")
    sources = set().union(*(route["source_server_ids"] for route in parsed["routes"]))
    if any(route["destination_server_id"] in sources for route in parsed["routes"]):
        # Otherwise relayed messages could be picked up again and loop between servers.
        raise ValueError("Destination servers must differ from every source server")
    for key, default in (("max_total_attachment_mb", 25), ("max_pending_messages", 5000)):
        value = config.get(key, default)
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a positive integer")
        parsed[key] = int(value)
        if parsed[key] <= 0:
            raise ValueError(f"{key} must be positive")
    return parsed


def selected(route, guild_id, channel_id, parent_id, author_id):
    channels = route["source_channel_ids"]
    authors = route["source_author_ids"]
    if route.get("all_source_channels", False):
        channel_selected = parent_id is None or route.get("include_threads", False)
    else:
        channel_selected = channel_id in channels or (route.get("include_threads", False) and parent_id in channels)
    return (guild_id in route["source_server_ids"]
            and channel_selected
            and (not authors or author_id in authors))


def matching_routes(config, guild_id, channel_id, parent_id, author_id):
    return [route for route in config["routes"]
            if selected(route, guild_id, channel_id, parent_id, author_id)]


def split_text(text, limit=1900):
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]


class Queue:
    def __init__(self, path, legacy_route=DEFAULT_ROUTE):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS route_deliveries (
                message_id INTEGER NOT NULL, route TEXT NOT NULL, channel_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', part INTEGER NOT NULL DEFAULT 0,
                destination_ids TEXT NOT NULL DEFAULT '[]', PRIMARY KEY (message_id, route))""")
            # Carry single-destination history over so restarts do not resend old messages.
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deliveries'").fetchone():
                self.db.execute("""INSERT OR IGNORE INTO route_deliveries
                    SELECT message_id, ?, channel_id, status, part, destination_ids FROM deliveries""",
                                (legacy_route,))
                self.db.execute("DROP TABLE deliveries")
            # A process may have died after a successful send but before recording it.
            self.db.execute("UPDATE route_deliveries SET status='uncertain' WHERE status='sending'")

    def enqueue(self, message_id, channel_id, route, maximum):
        if self.db.execute("SELECT 1 FROM route_deliveries WHERE message_id=? AND route=?",
                           (message_id, route)).fetchone():
            return False
        pending = self.db.execute("SELECT COUNT(*) FROM route_deliveries WHERE status='pending'").fetchone()[0]
        if pending >= maximum:
            raise RuntimeError("Delivery queue is full")
        with self.db:
            self.db.execute("INSERT INTO route_deliveries(message_id,route,channel_id) VALUES (?,?,?)",
                            (message_id, route, channel_id))
        return True

    def next(self, routes):
        routes = list(routes)
        marks = ",".join("?" * len(routes))
        return self.db.execute(
            f"SELECT message_id,channel_id,route,part FROM route_deliveries WHERE status='pending' "
            f"AND route IN ({marks}) ORDER BY message_id LIMIT 1", routes).fetchone()

    def status(self, message_id, route, status):
        with self.db:
            self.db.execute("UPDATE route_deliveries SET status=? WHERE message_id=? AND route=?",
                            (status, message_id, route))

    def acknowledge(self, message_id, route, destination_id, finished):
        row = self.db.execute("SELECT destination_ids FROM route_deliveries WHERE message_id=? AND route=?",
                              (message_id, route)).fetchone()
        ids = json.loads(row[0]) + [str(destination_id)]
        with self.db:
            self.db.execute("UPDATE route_deliveries SET part=part+1,destination_ids=?,status=? "
                            "WHERE message_id=? AND route=?",
                            (json.dumps(ids), "done" if finished else "pending", message_id, route))

    def counts(self):
        return dict(self.db.execute("SELECT status,COUNT(*) FROM route_deliveries GROUP BY status"))
