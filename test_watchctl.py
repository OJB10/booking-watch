"""Offline CLI tests; temporary files and fake launchctl calls only."""

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import engine
import watchctl


ROOT = Path(__file__).resolve().parent


def setUpModule():
    for target in ("socket.socket.connect", "socket.getaddrinfo"):
        guard = patch(target, side_effect=AssertionError("network forbidden"))
        guard.start()
        unittest.addModuleCleanup(guard.stop)


class WatchCtlTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name) / "watcher"
        self.home = Path(tmp.name) / "home"
        self.runner = Mock(side_effect=self.fake_run)
        self.loaded = False
        self.bootout_result = (0, "")
        self.bootstrap_result = (0, "")
        now = datetime(2099, 9, 27, 12, tzinfo=timezone.utc)
        clock = patch("watchctl.datetime", wraps=datetime)
        clock.start().now.return_value = now
        self.addCleanup(clock.stop)
        self.day = now.date()
        self.future = (self.day + timedelta(days=10)).isoformat()
        self.plist = self.home / "Library/LaunchAgents/local.booking-watch.dinner.plist"
        self.config = self.directory / "watches/dinner.json"
        self.state_dir = self.home / ".local/state/booking-watch"
        self.target = "gui/{}/local.booking-watch.dinner".format(os.getuid())
        # Any accidental use of the real subprocess runner fails the test.
        guard = patch("watchctl.subprocess.run", side_effect=AssertionError("real process forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def fake_run(self, command):
        if command[1] == "print":
            return (0, "loaded") if self.loaded else (113, "Could not find service")
        if command[1] == "bootstrap":
            return self.bootstrap_result
        if command[1] == "bootout":
            return self.bootout_result
        self.fail("Unexpected command: " + repr(command))

    def call(self, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = watchctl.main(list(args), directory=self.directory,
                                 home=kwargs.get("home", self.home), runner=self.runner)
        return code, out.getvalue() + err.getvalue()

    def new(self, *extra, watch_id="dinner"):
        return self.call("new", "--id", watch_id, "--label", "Dinner",
                         "--adapter", "sevenrooms", "--venue", "your-venue-slug",
                         "--date", self.future, "--party-size", "2",
                         "--preferred-time", "19:00", *extra)

    def test_new_valid_config_and_default_url(self):
        self.assertEqual(self.new()[0], 0)
        cfg, check = engine._load_config(self.config)
        self.assertTrue(callable(check))
        self.assertEqual(cfg, {
            "id": "dinner", "label": "Dinner", "adapter": "sevenrooms",
            "venue": "your-venue-slug", "date": self.future, "party_size": 2,
            "preferred_time": "19:00", "broken_after_minutes": 120,
            "booking_url": "https://www.sevenrooms.com/explore/your-venue-slug/"
                           "reservations/create/search?date=" + self.future + "&party_size=2",
        })
        self.runner.assert_not_called()

    def test_new_optional_values(self):
        self.assertEqual(self.new("--booking-url", "https://example.com/book",
                                  "--tg-chat", "-123", "--tg-thread", "7",
                                  "--broken-after-minutes", "30")[0], 0)
        cfg, _ = engine._load_config(self.config)
        self.assertEqual(cfg["booking_url"], "https://example.com/book")
        self.assertEqual((cfg["tg_chat"], cfg["tg_thread"], cfg["broken_after_minutes"]),
                         ("-123", "7", 30))

    def test_new_refuses_duplicate_without_changing_it(self):
        self.assertEqual(self.new()[0], 0)
        before = self.config.read_bytes()
        code, output = self.new("--label", "Replacement")
        self.assertEqual(code, 2)
        self.assertIn("exists", output)
        self.assertEqual(self.config.read_bytes(), before)

    def test_new_rejects_long_label_unsafe_url_and_venue(self):
        for option, value in (("--label", "L" * 101), ("--booking-url", "http://example.com"),
                              ("--venue", "../escape"), ("--venue", "two words"),
                              ("--venue", "café")):
            with self.subTest(option=option, value=value):
                self.assertEqual(self.new(option, value)[0], 2)
                self.assertFalse(self.config.exists())

    def test_new_accepts_label_at_limit_and_safe_venue(self):
        self.assertEqual(self.new("--label", "L" * 100, "--venue", "Venue_123-test")[0], 0)

    def test_new_refuses_past_date(self):
        self.assertEqual(self.new("--date", (self.day - timedelta(days=1)).isoformat())[0], 2)
        self.assertFalse(self.config.exists())

    def test_new_accepts_today_in_utc(self):
        with patch("watchctl.datetime") as clock:
            clock.now.return_value = datetime(2099, 9, 28, 0, 1, tzinfo=timezone.utc)
            self.assertEqual(self.new("--date", "2099-09-28")[0], 0)
            clock.now.assert_called_with(timezone.utc)

    def test_new_removes_invalid_written_config(self):
        for option, value in (("--party-size", "0"), ("--preferred-time", "25:00"),
                              ("--broken-after-minutes", "-1"), ("--label", "")):
            with self.subTest(option=option):
                self.assertEqual(self.new(option, value)[0], 2)
                self.assertFalse(self.config.exists())

    def test_new_uses_engine_validation_on_written_file(self):
        real_load = engine._load_config
        with patch("watchctl.engine._load_config", wraps=real_load) as load:
            self.assertEqual(self.new()[0], 0)
        load.assert_called_once_with(self.config)

    def test_invalid_ids_cannot_escape_directory(self):
        for watch_id in ("../escape", "/absolute", "bad.name"):
            self.assertEqual(self.new(watch_id=watch_id)[0], 2)
            self.assertEqual(self.call("start", watch_id)[0], 2)
            self.assertEqual(self.call("stop", watch_id)[0], 2)
        self.runner.assert_not_called()

    def test_start_writes_expected_plist_and_bootstraps_once(self):
        self.new()
        self.assertEqual(self.call("start", "dinner")[0], 0)
        with self.plist.open("rb") as stream:
            result = plistlib.load(stream)
        self.assertEqual(result, {
            "Label": "local.booking-watch.dinner",
            "ProgramArguments": [sys.executable, str(self.directory / "engine.py"), str(self.config)],
            "WorkingDirectory": str(self.directory),
            "EnvironmentVariables": {"HOME": str(self.home), "PATH": str(self.home / ".local/bin")
                                     + ":/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"},
            "StartInterval": 300, "RunAtLoad": True,
            "StandardOutPath": str(self.state_dir / "dinner.log"),
            "StandardErrorPath": str(self.state_dir / "dinner.log"),
        })
        self.assertTrue(self.state_dir.is_dir())
        self.assertEqual(self.runner.call_args_list, [
            unittest.mock.call(["launchctl", "print", self.target]),
            unittest.mock.call(["launchctl", "bootstrap", "gui/{}".format(os.getuid()), str(self.plist)]),
        ])

    def test_start_refuses_already_loaded_without_writing(self):
        self.new()
        self.loaded = True
        code, output = self.call("start", "dinner")
        self.assertEqual(code, 2)
        self.assertIn("already loaded", output)
        self.assertFalse(self.home.exists())
        self.runner.assert_called_once_with(["launchctl", "print", self.target])

    def test_start_dry_run_prints_xml_and_commands_without_side_effects(self):
        self.new()
        before = self.config.read_bytes()
        code, output = self.call("start", "dinner", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("<?xml", output)
        self.assertIn("launchctl print " + self.target, output)
        self.assertIn("launchctl bootstrap", output)
        self.assertFalse(self.home.exists())
        self.assertEqual(self.config.read_bytes(), before)
        self.runner.assert_not_called()

    def test_start_refuses_missing_bad_expired_or_mismatched_config(self):
        self.assertEqual(self.call("start", "dinner")[0], 2)
        self.new()
        original = json.loads(self.config.read_text())
        for changes in ({"date": "2000-01-01"}, {"id": "other"}, {"party_size": 0}):
            self.config.write_text(json.dumps(dict(original, **changes)))
            self.assertEqual(self.call("start", "dinner")[0], 2)
        self.config.write_text("not json")
        self.assertEqual(self.call("start", "dinner")[0], 2)
        self.runner.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_start_reports_bootstrap_failure(self):
        self.new()
        self.bootstrap_result = (5, "Input/output error")
        code, output = self.call("start", "dinner")
        self.assertEqual(code, 2)
        self.assertIn("Input/output error", output)

    def test_stop_removes_only_plist_and_calls_bootout(self):
        self.new()
        self.call("start", "dinner")
        state = self.state_dir / "dinner.json"
        log = self.state_dir / "dinner.log"
        state.write_text('{"done": true}')
        log.write_text("last check\n")
        self.runner.reset_mock()
        self.assertEqual(self.call("stop", "dinner")[0], 0)
        self.runner.assert_called_once_with(["launchctl", "bootout", self.target])
        self.assertFalse(self.plist.exists())
        self.assertTrue(self.config.exists())
        self.assertEqual(state.read_text(), '{"done": true}')
        self.assertEqual(log.read_text(), "last check\n")

    def test_stop_ignores_not_loaded(self):
        for message in ("not loaded", "Boot-out failed: 3: No such process", "Could not find service"):
            self.plist.parent.mkdir(parents=True, exist_ok=True)
            self.plist.write_bytes(b"placeholder")
            self.bootout_result = (3, message)
            self.assertEqual(self.call("stop", "dinner")[0], 0)
            self.assertFalse(self.plist.exists())

    def test_stop_other_failure_keeps_plist(self):
        self.new()
        self.call("start", "dinner")
        self.bootout_result = (1, "Operation not permitted")
        self.assertEqual(self.call("stop", "dinner")[0], 2)
        self.assertTrue(self.plist.exists())

    def test_stop_dry_run_keeps_plist_and_runs_nothing(self):
        self.new()
        self.call("start", "dinner")
        before = self.plist.read_bytes()
        self.runner.reset_mock()
        code, output = self.call("stop", "dinner", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("launchctl bootout " + self.target, output)
        self.assertIn(str(self.plist), output)
        self.assertEqual(self.plist.read_bytes(), before)
        self.runner.assert_not_called()

    def test_status_contains_each_watch_state_and_last_log_line(self):
        self.new()
        self.new(watch_id="lunch")
        self.call("start", "dinner")
        self.loaded = True
        (self.state_dir / "dinner.json").write_text(json.dumps({
            "last_state": "BOOKABLE", "done": True, "alerted_rank": 2}))
        (self.state_dir / "dinner.log").write_text("old line\nlatest check\n")
        self.runner.reset_mock()
        code, output = self.call("status")
        self.assertEqual(code, 0)
        dinner = next(line for line in output.splitlines() if line.startswith("dinner"))
        self.assertIn("BOOKABLE", dinner)
        self.assertIn("true", dinner)
        self.assertIn("2", dinner)
        self.assertIn("latest check", dinner)
        self.assertIn("yes", dinner)
        self.assertIn(self.future, dinner)
        self.assertIn("lunch", output)
        self.assertNotIn("old line", output)
        self.assertEqual(self.runner.call_count, 2)

    def test_status_handles_corrupt_state_and_config(self):
        self.new()
        self.new(watch_id="lunch")
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "dinner.json").write_text("not json")
        (self.directory / "watches/lunch.json").write_text("not json")
        code, output = self.call("status")
        self.assertEqual(code, 0)
        self.assertIn("dinner", output)
        self.assertIn("lunch", output)
        self.assertIn("error", output.lower())

    def test_cron_prints_five_minute_line_without_side_effects(self):
        self.directory = self.directory / "with spaces"
        self.config = self.directory / "watches/dinner.json"
        self.assertEqual(self.new()[0], 0)
        before = self.config.read_bytes()
        code, output = self.call("cron", "dinner")
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "*/5 * * * * " + shlex.join([
            sys.executable, str(self.directory / "engine.py"), str(self.config)]))
        self.assertFalse(self.home.exists())
        self.assertEqual(self.config.read_bytes(), before)
        self.runner.assert_not_called()

    def test_cron_escapes_percent_for_crontab(self):
        self.directory = self.directory / "100% available"
        self.new()
        code, output = self.call("cron", "dinner")
        self.assertEqual(code, 0)
        self.assertIn("100\\% available", output)
        self.runner.assert_not_called()

    def test_cron_rejects_missing_and_invalid_id(self):
        self.assertEqual(self.call("cron", "missing")[0], 2)
        self.assertEqual(self.call("cron", "../escape")[0], 2)
        self.runner.assert_not_called()

    def test_token_never_written_to_config_plist_or_output(self):
        token = "fake-secret-token"
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": "123"}):
            code, created = self.new()
            self.assertEqual(code, 0)
            code, started = self.call("start", "dinner")
            self.assertEqual(code, 0)
        self.assertNotIn(token, created + started + self.config.read_text() + self.plist.read_text())

    def test_state_directory_override_passes_to_launch_agent(self):
        custom = self.home / "custom-state"
        self.new()
        with patch.dict(os.environ, {"BOOKING_WATCH_STATE_DIR": str(custom)}):
            self.assertEqual(self.call("start", "dinner")[0], 0)
        payload = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(payload["StandardOutPath"], str(custom / "dinner.log"))
        self.assertEqual(payload["EnvironmentVariables"]["BOOKING_WATCH_STATE_DIR"], str(custom))

    def test_environment_home_override(self):
        self.new()
        with patch.dict(os.environ, {"BOOKING_WATCH_HOME": str(self.home)}):
            self.assertEqual(self.call("start", "dinner", home=None)[0], 0)
        self.assertTrue(self.plist.exists())


if __name__ == "__main__":
    unittest.main()
