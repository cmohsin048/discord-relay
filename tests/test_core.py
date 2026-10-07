import contextlib
import getpass
import io
import json
import logging
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
import relay
import sqlite3
from relay_core import Queue, load_config, matching_routes, selected, split_text


class CoreTests(unittest.TestCase):
    def test_multiple_channels_stay_in_source_server(self):
        cfg = dict(source_server_ids={1}, source_channel_ids={2, 4}, source_author_ids=set())
        self.assertTrue(selected(cfg, 1, 2, None, 3))
        self.assertTrue(selected(cfg, 1, 4, None, 3))
        self.assertFalse(selected(cfg, 1, 5, None, 3))
        self.assertFalse(selected(cfg, 9, 2, None, 3))

    def test_all_channels_retains_server_author_and_thread_boundaries(self):
        cfg = dict(source_server_ids={1}, source_channel_ids=set(), source_author_ids={3}, all_source_channels=True)
        self.assertTrue(selected(cfg, 1, 123, None, 3))
        self.assertTrue(selected(cfg, 1, 456, None, 3))
        self.assertFalse(selected(cfg, 9, 123, None, 3))
        self.assertFalse(selected(cfg, 1, 123, None, 99))
        self.assertFalse(selected(cfg, 1, 456, 123, 3))
        cfg["include_threads"] = True
        self.assertTrue(selected(cfg, 1, 456, 123, 3))

    def test_all_channels_config_requires_boolean_opt_in(self):
        cfg = dict(source_server_ids=["1"], source_channel_ids=[], destination_server_id="9", all_source_channels=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(cfg))
            self.assertTrue(load_config(path)["routes"][0]["all_source_channels"])
            cfg["all_source_channels"] = "true"
            path.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_source_scope_and_thread_filter(self):
        cfg = dict(source_server_ids={1}, source_channel_ids={2}, source_author_ids={3})
        self.assertTrue(selected(cfg, 1, 2, None, 3))
        self.assertFalse(selected(cfg, 1, 4, 2, 3))
        cfg["include_threads"] = True
        self.assertTrue(selected(cfg, 1, 4, 2, 3))
        self.assertFalse(selected(cfg, 99, 2, None, 3))
        self.assertFalse(selected(cfg, 1, 2, None, 99))
        self.assertFalse(selected(cfg, 1, 99, None, 3))
        cfg["source_channel_ids"] = set()
        self.assertFalse(selected(cfg, 1, 2, None, 3))

    def test_config_rejects_broad_scope_wrong_destination_and_secrets(self):
        base = dict(source_server_ids=["1"], source_channel_ids=["2"], destination_server_id="9")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for changes in ({"source_channel_ids": []}, {"destination_server_id": "1"},
                            {"token": "synthetic-secret"}, {"source_server_ids": [True]},
                            {"include_threads": "false"}):
                with self.subTest(changes=changes):
                    path.write_text(json.dumps(base | changes))
                    with self.assertRaises(ValueError):
                        load_config(path)
            path.write_text(json.dumps(base))
            self.assertFalse(load_config(path)["routes"][0]["include_threads"])

    def test_offline_start_never_reads_secrets_or_opens_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(dict(source_server_ids=["1"], source_channel_ids=["2"], destination_server_id="9")))
            output = io.StringIO()
            with patch("relay.read_secret", side_effect=AssertionError("Secret read")), \
                 patch("socket.socket", side_effect=AssertionError("Network opened")), \
                 patch.dict("sys.modules", {"relay_live": None, "discord": None, "aiohttp": None}), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(relay.main(["--config", str(path)]), 0)
            self.assertIn("OFFLINE CHECK PASSED", output.getvalue())
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_secret_prompt_fails_when_input_would_echo(self):
        def insecure_prompt(prompt):
            warnings.warn("Input would echo", getpass.GetPassWarning)
            self.fail("Prompt must not continue after warning")
        with patch.dict("os.environ", {}, clear=True), patch("getpass.getpass", side_effect=insecure_prompt):
            with self.assertRaises(getpass.GetPassWarning):
                relay.read_secret("DISCORD_USER_TOKEN", "Hidden: ")

    def test_logs_redact_credentials_and_tracebacks(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.addFilter(relay.RedactSecrets(("synthetic-secret",)))
        logger = logging.Logger("test")
        logger.addHandler(handler)
        try:
            raise ValueError("synthetic-secret")
        except ValueError:
            logger.error("Request %s failed", "synthetic-secret", exc_info=True)
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())

    def test_text_keeps_every_character(self):
        text = "image caption\n" * 500
        parts = split_text(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part) <= 1900 for part in parts))

    def test_restart_preserves_queue_and_flags_ambiguous_send(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "queue.db"
            queue = Queue(path)
            self.assertTrue(queue.enqueue(100, 20, "a", 5))
            self.assertFalse(queue.enqueue(100, 20, "a", 5))
            queue.status(100, "a", "sending")
            queue.db.close()
            queue = Queue(path)
            self.assertEqual(queue.counts(), {"uncertain": 1})
            self.assertIsNone(queue.next(["a"]))
            queue.db.close()

    def test_acknowledgement_and_capacity(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = Queue(Path(folder) / "queue.db")
            queue.enqueue(100, 20, "a", 1)
            with self.assertRaises(RuntimeError):
                queue.enqueue(101, 20, "a", 1)
            queue.acknowledge(100, "a", 900, False)
            self.assertEqual(queue.next(["a"]), (100, 20, "a", 1))
            queue.acknowledge(100, "a", 901, True)
            self.assertIsNone(queue.next(["a"]))
            self.assertFalse(queue.enqueue(100, 20, "a", 1))
            self.assertTrue(queue.enqueue(101, 20, "a", 1))
            queue.db.close()

    def test_one_message_queues_separately_per_route(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = Queue(Path(folder) / "queue.db")
            self.assertTrue(queue.enqueue(100, 20, "a", 5))
            self.assertTrue(queue.enqueue(100, 20, "b", 5))
            self.assertEqual(queue.next(["b"]), (100, 20, "b", 0))
            queue.acknowledge(100, "a", 900, True)
            self.assertIsNone(queue.next(["a"]))
            self.assertEqual(queue.next(["a", "b"]), (100, 20, "b", 0))
            queue.db.close()

    def test_legacy_queue_history_moves_to_first_route(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "queue.db"
            db = sqlite3.connect(path)
            db.execute("""CREATE TABLE deliveries (message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', part INTEGER NOT NULL DEFAULT 0,
                destination_ids TEXT NOT NULL DEFAULT '[]')""")
            db.execute("INSERT INTO deliveries(message_id,channel_id,status) VALUES (100,20,'done')")
            db.commit()
            db.close()
            queue = Queue(path, legacy_route="main")
            self.assertFalse(queue.enqueue(100, 20, "main", 5))
            self.assertTrue(queue.enqueue(100, 20, "other", 5))
            queue.db.close()

    def test_routes_config_maps_channels_to_separate_destinations(self):
        cfg = {"routes": [
            dict(name="a", source_server_ids=["1"], source_channel_ids=["2", "3"], destination_server_id="8", webhook_env="HOOK_A"),
            dict(name="b", source_server_ids=["1", "5"], source_channel_ids=["3", "6"], destination_server_id="9", webhook_env="HOOK_B"),
        ]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(cfg))
            config = load_config(path)
            self.assertEqual([r["name"] for r in matching_routes(config, 1, 2, None, 7)], ["a"])
            self.assertEqual([r["name"] for r in matching_routes(config, 1, 3, None, 7)], ["a", "b"])
            self.assertEqual([r["name"] for r in matching_routes(config, 5, 6, None, 7)], ["b"])
            self.assertEqual(matching_routes(config, 5, 2, None, 7), [])
            for broken in ({"destination_server_id": "5"}, {"name": "a"}, {"webhook_env": "lower"},
                           {"webhook_url": "https://example"}):
                with self.subTest(broken=broken):
                    bad = {"routes": [cfg["routes"][0], cfg["routes"][1] | broken]}
                    path.write_text(json.dumps(bad))
                    with self.assertRaises(ValueError):
                        load_config(path)

if __name__ == "__main__":
    unittest.main()
