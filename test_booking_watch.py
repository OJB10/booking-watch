"""Offline regression tests. All temporary files stay in this directory."""

import contextlib
import fcntl
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from adapters import sevenrooms
import engine

ROOT = Path(__file__).resolve().parent
CFG = {
    "id": "dinner-2099-11-17", "label": "Dinner",
    "adapter": "sevenrooms", "venue": "your-venue-slug", "date": "2099-11-17",
    "party_size": 2, "preferred_time": "11:30",
    "booking_url": "https://www.sevenrooms.com/explore/your-venue-slug/reservations/create/search?date=2099-11-17&party_size=2",
    "broken_after_minutes": 120,
}


def setUpModule():
    for target in ("socket.socket.connect", "socket.getaddrinfo"):
        guard = patch(target, side_effect=AssertionError("network forbidden"))
        guard.start()
        unittest.addModuleCleanup(guard.stop)


class AdapterTests(unittest.TestCase):
    def test_fixtures(self):
        for name, day, expected in [
            ("not_released", "2099-11-17", ("NOT_RELEASED", [])),
            ("request_only", "2099-10-20", ("REQUEST_ONLY", [])),
            ("bookable", "2099-10-20", ("BOOKABLE", ["11:30", "12:00"])),
        ]:
            with self.subTest(name=name):
                payload = json.loads((ROOT / "fixtures" / f"sevenrooms_{name}.json").read_text())
                self.assertEqual(sevenrooms.parse(payload, day), expected)

    def test_error_fixture_and_missing_date(self):
        payload = json.loads((ROOT / "fixtures/sevenrooms_error.json").read_text())
        for value in (payload, {"data": {"availability": {}}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sevenrooms.parse(value, CFG["date"])

    def test_malformed_payloads(self):
        values = [None, [], {}, {"data": []}, {"data": {"availability": []}}]
        bad_shifts = [None, {}, "bad", [None], [{}], [{"times": {}}],
                      [{"times": [None]}], [{"times": [{}]}],
                      [{"times": [{"type": "book"}]}],
                      [{"times": [{"type": 2, "time": "11:30"}]}],
                      [{"times": [{"type": "request", "time": None}]}],
                      [{"times": [{"type": "book", "time": "11:30"}, {}]}]]
        values += [{"data": {"availability": {CFG["date"]: shifts}}} for shifts in bad_shifts]
        for payload in values:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                sevenrooms.parse(payload, CFG["date"])

    def test_all_shifts_sorted_and_empty_shift(self):
        payload = {"data": {"availability": {CFG["date"]: [
            {"times": [{"type": "book", "time": "12:00"}]},
            {"times": [{"type": "book", "time": "11:30"}]},
        ]}}}
        self.assertEqual(sevenrooms.parse(payload, CFG["date"]), ("BOOKABLE", ["11:30", "12:00"]))
        payload["data"]["availability"][CFG["date"]] = [{"times": []}]
        self.assertEqual(sevenrooms.parse(payload, CFG["date"]), ("REQUEST_ONLY", []))

    def test_url_and_injected_fetch(self):
        expected = "https://www.sevenrooms.com/api-yoa/availability/widget/range?venue=your-venue-slug&time_slot=11:30&party_size=2&halo_size_interval=48&start_date=11-17-2099&num_days=1&channel=SEVENROOMS_WIDGET"
        self.assertEqual(sevenrooms.build_url(CFG), expected)
        fetch = Mock(return_value=(ROOT / "fixtures/sevenrooms_not_released.json").read_bytes())
        self.assertEqual(sevenrooms.check(CFG, fetch), ("NOT_RELEASED", []))
        fetch.assert_called_once_with(expected)

    def test_fetch_errors_propagate(self):
        http_error = HTTPError("url", 400, "bad", {}, io.BytesIO(b""))
        self.addCleanup(http_error.close)
        for error in (TimeoutError(), http_error):
            with self.subTest(error=error), self.assertRaises(type(error)):
                sevenrooms.check(CFG, Mock(side_effect=error))
        with self.assertRaises(ValueError):
            sevenrooms.check(CFG, lambda url: b"not json")

    def test_default_fetch_timeout_and_user_agent(self):
        with patch("adapters.sevenrooms.urllib.request.build_opener") as build_opener:
            open_url = build_opener.return_value.open
            open_url.return_value.__enter__.return_value.read.return_value = (ROOT / "fixtures/sevenrooms_not_released.json").read_bytes()
            self.assertEqual(sevenrooms.check(CFG), ("NOT_RELEASED", []))
            args, kwargs = open_url.call_args
            self.assertEqual(args[0].get_header("User-agent"), "booking-watch/1.0 (+https://github.com/OJB10/booking-watch)")
            self.assertEqual(kwargs["timeout"], 20)
            open_url.return_value.__enter__.return_value.read.assert_called_once_with(2_000_001)

    def test_response_size_boundary(self):
        payload = (ROOT / "fixtures/sevenrooms_not_released.json").read_bytes()
        for size in (2_000_000, 2_000_001):
            with self.subTest(size=size), patch("adapters.sevenrooms.urllib.request.build_opener") as build:
                response = build.return_value.open.return_value.__enter__.return_value
                response.read.return_value = payload + b" " * (size - len(payload))
                if size == 2_000_000:
                    self.assertEqual(sevenrooms.check(CFG), ("NOT_RELEASED", []))
                else:
                    with self.assertRaises(ValueError):
                        sevenrooms.check(CFG)
                response.read.assert_called_once_with(2_000_001)

    def test_all_redirect_statuses_refused_without_second_request(self):
        import urllib.request
        from urllib.response import addinfourl

        class FakeHTTPS(urllib.request.HTTPSHandler):
            def https_open(self, request):
                requests.append(request.full_url)
                response = addinfourl(io.BytesIO(b"{}"),
                                     {"Location": "https://example.com/redirect"},
                                     request.full_url, code)
                response.msg = "redirect"
                return response

        real_build = urllib.request.build_opener
        for code in range(300, 400):
            requests = []
            with self.subTest(code=code), patch(
                    "adapters.sevenrooms.urllib.request.build_opener",
                    side_effect=lambda *handlers: real_build(FakeHTTPS(), *handlers)):
                with self.assertRaises(HTTPError) as caught:
                    sevenrooms.check(CFG)
                self.assertEqual(caught.exception.code, code)
                caught.exception.close()
                self.assertEqual(len(requests), 1)


class EngineTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.now = datetime(2099, 9, 27, 12, tzinfo=timezone.utc)
        self.check = Mock(return_value=("NOT_RELEASED", []))
        self.send = Mock(return_value=True)

    def run_watch(self, minutes=0, cfg=None):
        return engine.run_once(cfg or CFG, self.path, self.now + timedelta(minutes=minutes), self.check, self.send)

    def state(self):
        return json.loads(self.path.read_text())

    def test_incomplete_or_non_object_state_runs_and_alarms(self):
        for loaded in ([], {"done": False}):
            with self.subTest(loaded=loaded):
                self.path.write_text(json.dumps(loaded))
                self.check.side_effect = None
                self.send.reset_mock()
                self.assertIn("NOT_RELEASED", self.run_watch())
                self.path.write_text(json.dumps(loaded))
                self.check.side_effect = TimeoutError()
                self.run_watch()
                self.run_watch(120)
                self.send.assert_called_once()
                self.assertIn("can't read the booking page", self.send.call_args.args[0])

    def test_http_backoff_skips_requests_expires_and_keeps_alarm_clock(self):
        for code in (403, 429):
            with self.subTest(code=code):
                self.path.write_text("{}")
                error = HTTPError("url", code, "blocked", {}, io.BytesIO())
                self.addCleanup(error.close)
                self.check.reset_mock()
                self.send.reset_mock()
                self.check.side_effect = error
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.run_watch()
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                self.assertIn("backing off", output.getvalue())
                self.assertIn(str(code), output.getvalue())
                self.assertEqual(self.state()["last_http_status"], code)
                self.run_watch(30, dict(CFG, broken_after_minutes=30))
                self.run_watch(59)
                self.assertEqual(self.check.call_count, 1)
                self.send.assert_called_once()
                self.assertIn("failing for 30 min", self.send.call_args.args[0])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.run_watch(60)
                self.assertEqual(self.check.call_count, 2)
                self.assertEqual(self.state()["first_fail_ts"], self.now.isoformat())
                self.check.side_effect = None
                self.run_watch(119)
                self.assertEqual(self.check.call_count, 2)
                self.run_watch(120)
                self.assertEqual(self.check.call_count, 3)
                self.assertEqual(self.send.call_args.args[0], "Dinner watcher: reading the booking page again.")
                self.assertIsNone(self.state()["backoff_until"])

    def test_other_http_errors_do_not_back_off(self):
        error = HTTPError("url", 500, "failed", {}, io.BytesIO())
        self.addCleanup(error.close)
        self.check.side_effect = error
        self.run_watch()
        self.run_watch(5)
        self.assertEqual(self.check.call_count, 2)

    def test_long_label_and_times_are_capped(self):
        cfg = dict(CFG, label="L" * 5000)
        self.check.return_value = ("BOOKABLE", ["T" * 5000] * 20)
        self.run_watch(cfg=cfg)
        message = self.send.call_args.args[0]
        self.assertLessEqual(len(message), 4000)
        self.assertLessEqual(len(message.split(": TABLES", 1)[0]), 100)
        self.assertNotIn("T" * 17, message)
        self.assertIn("and 12 more", message)

    def test_times_shorten_before_booking_link_and_whole_message(self):
        self.check.return_value = ("BOOKABLE", ["T" * 16] * 20)
        for size in (3800, 5000):
            with self.subTest(size=size):
                self.path.write_text("{}")
                cfg = dict(CFG, booking_url="https://example.com/" + "x" * size)
                self.run_watch(cfg=cfg)
                message = self.send.call_args.args[0]
                self.assertLessEqual(len(message), 4000)
                self.assertIn("...", message)
                if size == 3800:
                    self.assertTrue(message.endswith(cfg["booking_url"]))
                else:
                    self.assertTrue(message.endswith("..."))

    def test_every_alarm_message_is_capped(self):
        cfg = dict(CFG, label="L" * 5000, booking_url="https://example.com/" + "x" * 5000)
        self.check.side_effect = TimeoutError()
        self.run_watch(cfg=cfg)
        self.run_watch(120, cfg)
        broken = self.send.call_args.args[0]
        self.assertLessEqual(len(broken), 4000)
        self.assertTrue(broken.endswith("..."))
        self.check.side_effect = None
        self.check.return_value = ("REQUEST_ONLY", [])
        self.run_watch(121, cfg)
        for call in self.send.call_args_list:
            self.assertLessEqual(len(call.args[0]), 4000)
            self.assertNotIn("L" * 101, call.args[0])

    def test_progression_and_done(self):
        self.run_watch()
        self.send.assert_not_called()
        self.assertEqual(self.state()["alerted_rank"], 0)
        self.check.return_value = ("REQUEST_ONLY", [])
        self.run_watch()
        self.run_watch()
        self.send.assert_called_once_with("Dinner: Tue 17 Nov has been released but there are no instant tables yet (full). Cancellations can appear, so check it: " + CFG["booking_url"])
        self.check.return_value = ("BOOKABLE", ["11:30", "12:00"])
        self.run_watch()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args.args[0], "Dinner: TABLES OPEN for Tue 17 Nov, party of 2. Times: 11:30, 12:00. Book now: " + CFG["booking_url"])
        self.assertTrue(self.state()["done"])
        before = self.path.read_bytes()
        self.check.reset_mock()
        self.assertEqual(self.run_watch(), "inactive")
        self.check.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_bookable_directly_and_flap(self):
        self.check.return_value = ("BOOKABLE", ["11:30"])
        self.run_watch()
        self.assertEqual(self.state()["alerted_rank"], 2)
        self.check.return_value = ("REQUEST_ONLY", [])
        self.check.reset_mock()
        self.assertEqual(self.run_watch(), "inactive")
        self.check.assert_not_called()
        self.send.assert_called_once()

    def test_downgrade_and_return_do_not_alert(self):
        for state in ("REQUEST_ONLY", "NOT_RELEASED", "REQUEST_ONLY"):
            self.check.return_value = (state, [])
            self.run_watch()
        self.send.assert_called_once()
        self.assertEqual(self.state()["alerted_rank"], 1)

    def test_failed_delivery_retries(self):
        self.check.return_value = ("BOOKABLE", ["11:30"])
        self.send.return_value = False
        self.run_watch()
        self.assertEqual(self.state()["alerted_rank"], 0)
        self.assertFalse(self.state()["done"])
        self.send.return_value = True
        self.run_watch()
        self.assertEqual(self.send.call_count, 2)
        self.assertTrue(self.state()["done"])

    def test_failure_boundary_and_recovery(self):
        self.check.side_effect = TimeoutError("failed")
        self.run_watch(0)
        self.run_watch(119)
        self.send.assert_not_called()
        self.run_watch(120)
        self.send.assert_called_once_with("Dinner watcher can't read the booking page (failing for 120 min). It will keep trying. Check it or book by hand: " + CFG["booking_url"])
        self.run_watch(180)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.state()["fail_streak"], 4)
        self.check.side_effect = None
        self.run_watch(181)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.send.call_args.args[0], "Dinner watcher: reading the booking page again.")
        state = self.state()
        self.assertFalse(state["broken_alerted"])
        self.assertEqual(state["fail_streak"], 0)
        self.assertIsNone(state["first_fail_ts"])
        self.assertEqual(state["last_ok_ts"], "2099-09-27T15:01:00+00:00")
        self.assertEqual(state["last_state"], "NOT_RELEASED")

    def test_broken_and_recovered_delivery_retry(self):
        self.check.side_effect = ValueError("bad payload")
        self.send.return_value = False
        self.run_watch()
        self.run_watch(120)
        self.assertFalse(self.state()["broken_alerted"])
        self.send.return_value = True
        self.run_watch(121)
        self.assertTrue(self.state()["broken_alerted"])
        self.check.side_effect = None
        self.send.return_value = False
        self.run_watch(122)
        self.assertTrue(self.state()["broken_alerted"])
        self.send.return_value = True
        self.run_watch(123)
        self.assertFalse(self.state()["broken_alerted"])
        self.assertEqual(self.send.call_count, 4)

    def test_success_restarts_failure_window(self):
        self.check.side_effect = OSError()
        self.run_watch()
        self.check.side_effect = None
        self.run_watch(119)
        self.check.side_effect = OSError()
        self.run_watch(120)
        self.run_watch(239)
        self.send.assert_not_called()
        self.run_watch(240)
        self.send.assert_called_once()

    def test_expired_inactive_and_date_itself_active(self):
        self.now = datetime(2099, 11, 18, tzinfo=timezone.utc)
        self.assertEqual(self.run_watch(), "inactive")
        self.check.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse(self.path.exists())
        self.now = datetime(2099, 11, 17, 23, 59, tzinfo=timezone.utc)
        self.run_watch()
        self.check.assert_called_once_with(CFG)

    def test_atomic_replace(self):
        import os
        real_replace = os.replace
        def verify_replace(source, destination):
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(json.loads(Path(source).read_text())["last_state"], "NOT_RELEASED")
            real_replace(source, destination)
        with patch("engine.os.replace", side_effect=verify_replace) as replace:
            self.run_watch()
            replace.assert_called_once()
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


class CliTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = self.home / "config.json"
        self.config.write_text(json.dumps(dict(CFG, date="2099-11-17")))

    def call_main(self, dry=False):
        out = io.StringIO()
        with patch("engine.Path.home", return_value=self.home), contextlib.redirect_stdout(out):
            status = engine.main([str(self.config)] + (["--dry-run"] if dry else []))
        return status, out.getvalue()

    def test_dry_run_separate_state(self):
        with patch("adapters.sevenrooms.check", return_value=("BOOKABLE", ["11:30"])), patch("engine.send_telegram") as send:
            status, output = self.call_main(dry=True)
        self.assertEqual(status, 0)
        self.assertIn("WOULD SEND: Dinner: TABLES OPEN", output)
        send.assert_not_called()
        directory = self.home / ".local/state/booking-watch"
        self.assertTrue(json.loads((directory / (CFG["id"] + ".dryrun.json")).read_text())["done"])
        self.assertFalse((directory / (CFG["id"] + ".json")).exists())

    def test_delivery_and_retry_on_failure(self):
        with patch("adapters.sevenrooms.check", return_value=("BOOKABLE", ["11:30"])), patch("engine.send_telegram", side_effect=[False, False, True]) as send:
            for _ in range(3):
                self.assertEqual(self.call_main()[0], 0)
        self.assertEqual(send.call_count, 3)
        self.assertIn("TABLES OPEN", send.call_args.args[1])
        state = self.home / ".local/state/booking-watch" / (CFG["id"] + ".json")
        self.assertTrue(json.loads(state.read_text())["done"])

    def test_startup_loads_credentials_from_file(self):
        path = self.home / ".config/booking-watch/env"
        path.parent.mkdir(parents=True)
        path.write_text("TELEGRAM_BOT_TOKEN=file-token\nTELEGRAM_CHAT_ID=123\n")
        with patch("adapters.sevenrooms.check", return_value=("BOOKABLE", ["11:30"])), patch("urllib.request.urlopen") as opener:
            response = opener.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"ok": true}'
            self.assertEqual(self.call_main()[0], 0)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/botfile-token/sendMessage")
        self.assertEqual(json.loads(request.data)["chat_id"], "123")

    def test_state_directory_override(self):
        directory = self.home / "custom-state"
        with patch.dict(os.environ, {"BOOKING_WATCH_STATE_DIR": str(directory)}), patch("adapters.sevenrooms.check", return_value=("NOT_RELEASED", [])):
            self.assertEqual(self.call_main()[0], 0)
        self.assertTrue((directory / (CFG["id"] + ".json")).exists())
        self.assertFalse((self.home / ".local").exists())

    def test_optional_telegram_config(self):
        for value in (None, "", "   ", "123"):
            self.config.write_text(json.dumps(dict(CFG, tg_chat=value, tg_thread=value)))
            self.assertEqual(engine._load_config(self.config)[0]["tg_chat"], value)
        for value in ([], 123, "123\n456"):
            self.config.write_text(json.dumps(dict(CFG, tg_chat=value)))
            with self.assertRaises(ValueError):
                engine._load_config(self.config)

    def test_bad_config_exit_two(self):
        for changes in ({"id": "../escape"}, {"date": "bad"}, {"party_size": 0}, {"adapter": "missing"}, {"broken_after_minutes": -1}):
            self.config.write_text(json.dumps(dict(CFG, **changes)))
            with self.subTest(changes=changes):
                self.assertEqual(self.call_main()[0], 2)
        self.config.write_text("{}")
        self.assertEqual(self.call_main()[0], 2)

    def test_runtime_error_exit_zero(self):
        with patch("engine.run_once", side_effect=OSError("state unavailable")):
            status, output = self.call_main()
        self.assertEqual(status, 0)
        self.assertIn("runtime error (OSError)", output)

    def test_held_lock_skips_and_dry_run_has_own_lock(self):
        directory = self.home / ".local/state/booking-watch"
        directory.mkdir(parents=True)
        for dry, suffix in ((False, ".json"), (True, ".dryrun.json")):
            with self.subTest(dry=dry):
                lock = directory / (CFG["id"] + suffix + ".lock")
                with lock.open("w") as stream:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with patch("adapters.sevenrooms.check") as check:
                        status, output = self.call_main(dry=dry)
                    self.assertEqual(status, 0)
                    self.assertEqual(output, CFG["id"] + ": previous run still active, skipping\n")
                    check.assert_not_called()
                    if not dry:
                        with patch("adapters.sevenrooms.check", return_value=("NOT_RELEASED", [])) as check:
                            self.assertEqual(self.call_main(dry=True)[0], 0)
                        check.assert_called_once()
                        dry_lock = directory / (CFG["id"] + ".dryrun.json.lock")
                        self.assertEqual(dry_lock.stat().st_mode & 0o777, 0o600)
                with patch("adapters.sevenrooms.check", return_value=("NOT_RELEASED", [])) as check:
                    self.assertEqual(self.call_main(dry=dry)[0], 0)
                check.assert_called_once()

    def test_engine_rejects_unsafe_links_and_venues(self):
        for key, values in (("booking_url", ("http://example.com", "javascript:alert(1)", "HTTPS://example.com")),
                            ("venue", ("../escape", "a/b", "a b", "a?b", "café"))):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.config.write_text(json.dumps(dict(CFG, **{key: value})))
                    with self.assertRaises(ValueError):
                        engine._load_config(self.config)



class LiveShapeRegressionTests(unittest.TestCase):
    """Regression: live data repeats each time once per table."""

    def test_live_bookable_times_are_unique(self):
        payload = json.loads((ROOT / "fixtures/sevenrooms_bookable_live.json").read_text())
        state, times = sevenrooms.parse(payload, "2099-10-07")
        self.assertEqual(state, "BOOKABLE")
        self.assertEqual(times, sorted(set(times)))
        self.assertGreater(len(times), 8)

    def test_long_time_list_is_capped_in_message(self):
        sent = []
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            engine.run_once(CFG, Path(tmp) / "s.json", datetime(2099, 9, 27, 12, tzinfo=timezone.utc),
                            lambda cfg: ("BOOKABLE", [f"{h:02d}:00" for h in range(10, 22)]),
                            lambda msg: sent.append(msg) or True)
        self.assertEqual(len(sent), 1)
        self.assertIn("and 4 more", sent[0])


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.token = "fake-secret-token"
        environment = patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": self.token, "TELEGRAM_CHAT_ID": "123",
            "TELEGRAM_THREAD_ID": "7",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.opener = MagicMock()
        self.response = self.opener.return_value.__enter__.return_value
        self.response.status = 200
        self.response.read.return_value = b'{"ok": true}'

    def send(self, cfg=None):
        return engine.send_telegram(cfg or {}, "Tables open", opener=self.opener)

    def test_success_posts_message_with_timeout(self):
        self.assertIs(self.send(), True)
        request = self.opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bot" + self.token + "/sendMessage")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "chat_id": "123", "message_thread_id": "7", "text": "Tables open"})
        self.assertEqual(self.opener.call_args.kwargs["timeout"], 60)

    def test_direct_messages_are_capped(self):
        self.assertTrue(engine.send_telegram({}, "x" * 5000, opener=self.opener))
        message = json.loads(self.opener.call_args.args[0].data)["text"]
        self.assertEqual(len(message), 4000)
        self.assertTrue(message.endswith("..."))

    def test_config_overrides_and_empty_values_use_environment(self):
        self.assertTrue(self.send({"tg_chat": "456", "tg_thread": "8"}))
        payload = json.loads(self.opener.call_args.args[0].data)
        self.assertEqual((payload["chat_id"], payload["message_thread_id"]), ("456", "8"))
        for value in (None, "", "   "):
            self.assertTrue(self.send({"tg_chat": value, "tg_thread": value}))
            payload = json.loads(self.opener.call_args.args[0].data)
            self.assertEqual((payload["chat_id"], payload["message_thread_id"]), ("123", "7"))

    def test_thread_is_optional(self):
        del os.environ["TELEGRAM_THREAD_ID"]
        self.assertTrue(self.send())
        self.assertNotIn("message_thread_id", json.loads(self.opener.call_args.args[0].data))

    def test_only_http_200_and_literal_ok_true_succeed(self):
        for status, body in ((201, b'{"ok": true}'), (400, b'{"ok": true}'),
                             (200, b'{"ok": false}'), (200, b'{"ok": 1}'),
                             (200, b'{}'), (200, b'[]'), (200, b'invalid')):
            with self.subTest(status=status, body=body):
                self.response.status, self.response.read.return_value = status, body
                self.assertIs(self.send(), False)

    def test_http_error_and_exception_never_expose_token(self):
        error = HTTPError("https://api.telegram.org/bot" + self.token, 401, self.token, {}, io.BytesIO(b""))
        self.addCleanup(error.close)
        for failure in (error, TimeoutError(self.token), ValueError(self.token)):
            self.opener.side_effect = failure
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertIs(self.send(), False)
            self.assertNotIn(self.token, out.getvalue() + err.getvalue())

    def test_missing_credentials_fail_with_one_plain_line(self):
        for missing in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            with self.subTest(missing=missing), patch.dict(os.environ, {missing: ""}):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertIs(self.send(), False)
                self.assertEqual(output.getvalue(), "Missing " + missing + "\n")
        self.opener.assert_not_called()


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "env"
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_missing_file_is_optional(self):
        engine.load_env_file(self.path)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)

    def test_load_values_comments_whitespace_quotes_and_embedded_equals(self):
        self.path.write_text("# Comment\n\n TELEGRAM_BOT_TOKEN = 'fake=token' \nTELEGRAM_CHAT_ID=123\nTELEGRAM_THREAD_ID=\"7\"\n")
        self.path.chmod(0o600)
        engine.load_env_file(self.path)
        self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "fake=token")
        self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "123")
        self.assertEqual(os.environ["TELEGRAM_THREAD_ID"], "7")

    def test_existing_environment_wins_even_when_empty(self):
        self.path.write_text("TELEGRAM_BOT_TOKEN=file-token\nTELEGRAM_CHAT_ID=123\n")
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "shell-token", "TELEGRAM_CHAT_ID": ""}):
            engine.load_env_file(self.path)
            self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "shell-token")
            self.assertEqual(os.environ["TELEGRAM_CHAT_ID"], "")

    def test_no_shell_execution_or_expansion_and_no_secret_output(self):
        marker = self.path.parent / "must-not-exist"
        value = "$(touch " + str(marker) + ")"
        self.path.write_text("TELEGRAM_BOT_TOKEN=" + value + "\ninvalid secret text\n")
        self.path.chmod(0o600)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            engine.load_env_file(self.path)
        self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], value)
        self.assertFalse(marker.exists())
        self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_unquoted_inline_comments_preserve_quoted_hashes(self):
        for value, expected in (("token # comment", "token"), ("token#keep", "token#keep"),
                                ("'token # keep'", "token # keep"),
                                ('"token # keep" # comment', "token # keep"),
                                ("'token # keep' # comment", "token # keep")):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                self.path.write_text("TELEGRAM_BOT_TOKEN=" + value + "\n")
                self.path.chmod(0o600)
                engine.load_env_file(self.path)
                self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], expected)

    def test_permissions_warning_names_file_without_values(self):
        self.path.write_text("TELEGRAM_BOT_TOKEN=never-print-this\nTELEGRAM_CHAT_ID=private-chat\n")
        for mode in (0o600, 0o640, 0o601):
            with self.subTest(mode=mode):
                self.path.chmod(mode)
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    engine.load_env_file(self.path)
                output = out.getvalue()
                if mode == 0o600:
                    self.assertEqual(output, "")
                else:
                    self.assertEqual(len(output.splitlines()), 1)
                    self.assertIn(str(self.path), output)
                    self.assertIn("warning", output.lower())
                    self.assertNotIn("never-print-this", output)
                    self.assertNotIn("private-chat", output)

if __name__ == "__main__":
    unittest.main()
