#!/usr/bin/env python3
"""Create, start, stop and inspect deterministic booking watches."""

import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys

import engine


HERE = Path(__file__).resolve().parent


def run(command):
    """The only process boundary; inject a fake callable in offline tests."""
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, check=False)
    return result.returncode, result.stdout


def valid_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("id must contain only letters, digits, underscores or hyphens")
    return value


def require_current(cfg):
    if date.fromisoformat(cfg["date"]) < datetime.now(timezone.utc).date():
        raise ValueError("watch date is in the past (UTC)")


class WatchCtl:
    """Keep filesystem effects under explicit roots and commands behind a runner."""

    def __init__(self, directory, home, runner):
        self.directory = Path(directory).resolve()
        self.home = Path(home).resolve()
        self.watches = self.directory / "watches"
        self.agents = self.home / "Library/LaunchAgents"
        self.state = Path(os.environ.get("BOOKING_WATCH_STATE_DIR")
                          or self.home / ".local/state/booking-watch").expanduser().resolve()
        self.runner = runner
        self.domain = "gui/{}".format(os.getuid())

    def paths(self, watch_id):
        valid_id(watch_id)
        label = "local.booking-watch." + watch_id
        return self.watches / (watch_id + ".json"), self.agents / (label + ".plist"), label

    def load(self, path):
        cfg, _ = engine._load_config(path)
        if cfg["id"] != path.stem:
            raise ValueError("config id must match its filename")
        return cfg

    def new(self, args):
        path, _, _ = self.paths(args.id)
        if path.exists() or path.is_symlink():
            raise ValueError("watch already exists: " + str(path))
        cfg = {key: getattr(args, key) for key in (
            "id", "label", "adapter", "venue", "date", "party_size", "preferred_time",
            "booking_url", "tg_chat", "tg_thread", "broken_after_minutes")}
        cfg = {key: value for key, value in cfg.items()
               if key not in ("tg_chat", "tg_thread") or value is not None}
        if len(cfg["label"]) > 100:
            raise ValueError("label must be at most 100 characters")
        if cfg["booking_url"] is None:
            cfg["booking_url"] = (
                "https://www.sevenrooms.com/explore/{venue}/reservations/create/search"
                "?date={date}&party_size={party_size}".format(**cfg))
        require_current(cfg)
        self.watches.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents a duplicate from being overwritten in a race.
        with path.open("x", encoding="utf-8") as stream:
            try:
                json.dump(cfg, stream, indent=2)
                stream.write("\n")
                stream.flush()
                engine._load_config(path)
            except Exception:
                path.unlink()
                raise
        print("Created " + str(path))

    def start(self, args):
        config, plist, label = self.paths(args.id)
        cfg = self.load(config)
        require_current(cfg)
        log = str(self.state / (args.id + ".log"))
        environment = {
            "PATH": str(self.home / ".local/bin") + ":/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "HOME": str(self.home),
        }
        if os.environ.get("BOOKING_WATCH_STATE_DIR"):
            environment["BOOKING_WATCH_STATE_DIR"] = str(self.state)
        payload = plistlib.dumps({
            "Label": label,
            "ProgramArguments": [sys.executable, str(self.directory / "engine.py"), str(config)],
            "WorkingDirectory": str(self.directory),
            "EnvironmentVariables": environment,
            "StartInterval": 300,
            "RunAtLoad": True,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        })
        check = ["launchctl", "print", self.domain + "/" + label]
        bootstrap = ["launchctl", "bootstrap", self.domain, str(plist)]
        if args.dry_run:
            print(payload.decode("utf-8"), end="")
            print("# Check first; refuse if already loaded:")
            print(shlex.join(check))
            print(shlex.join(["mkdir", "-p", str(self.agents), str(self.state)]))
            print("# Write the XML above to " + str(plist))
            print(shlex.join(bootstrap))
            return
        code, _ = self.runner(check)
        if code == 0:
            raise ValueError("watch already loaded: " + label)
        self.agents.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(payload)
        code, output = self.runner(bootstrap)
        if code:
            raise ValueError("launchctl bootstrap failed ({}): {}".format(code, output.strip()))
        print("Started " + args.id)

    def cron(self, args):
        config, _, _ = self.paths(args.id)
        require_current(self.load(config))
        command = [sys.executable, str(self.directory / "engine.py"), str(config)]
        if os.environ.get("BOOKING_WATCH_STATE_DIR"):
            command = ["env", "BOOKING_WATCH_STATE_DIR=" + str(self.state)] + command
        if any("\n" in part or "\r" in part for part in command):
            raise ValueError("cron paths must be single lines")
        # crontab treats percent signs specially, including inside shell quotes.
        print("*/5 * * * * " + shlex.join(command).replace("%", r"\%"))

    def stop(self, args):
        _, plist, label = self.paths(args.id)
        command = ["launchctl", "bootout", self.domain + "/" + label]
        if args.dry_run:
            print(shlex.join(command))
            print("# Remove if present: " + str(plist))
            return
        code, output = self.runner(command)
        if code and not any(message in output.lower() for message in (
                "not loaded", "no such process", "could not find service")):
            raise ValueError("launchctl bootout failed ({}): {}".format(code, output.strip()))
        plist.unlink(missing_ok=True)
        print("Stopped " + args.id)

    def status(self, args):
        rows = [["ID", "LABEL", "DATE", "PLIST", "LOADED", "LAST_STATE", "DONE", "ALERTED_RANK", "LAST LOG LINE"]]
        for path in sorted(self.watches.glob("*.json")):
            watch_id = path.stem
            cfg, errors = {}, []
            try:
                cfg = self.load(path)
            except Exception as error:
                errors.append("config error: " + str(error))
            present, loaded = "?", "?"
            try:
                _, plist, label = self.paths(watch_id)
                present = "yes" if plist.exists() else "no"
                code, _ = self.runner(["launchctl", "print", self.domain + "/" + label])
                loaded = "yes" if code == 0 else "no"
            except (OSError, ValueError) as error:
                errors.append("launchctl error: " + str(error))
            state, last_line = {}, "-"
            try:
                state = json.loads((self.state / (watch_id + ".json")).read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    raise ValueError("state must be a JSON object")
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as error:
                state = {}
                errors.append("state error: " + str(error))
            try:
                with (self.state / (watch_id + ".log")).open(encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        last_line = line.rstrip("\r\n")
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append("log error: " + str(error))
            if errors:
                last_line += " [" + "; ".join(errors) + "]"
            rows.append([watch_id, cfg.get("label", "?"), cfg.get("date", "?"), present, loaded,
                         state.get("last_state"), state.get("done"), state.get("alerted_rank"), last_line])
        # Normalise control whitespace so each watch occupies exactly one row.
        rows = [[" ".join((str(value).lower() if isinstance(value, bool) else
                           str(value) if value is not None else "-").split()) for value in row]
                for row in rows]
        widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
        for row in rows:
            print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())


def main(argv=None, *, directory=None, home=None, runner=None):
    """Return an exit code; tests override both filesystem roots and the runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="create a validated watch config")
    for name in ("id", "label", "venue", "date", "preferred-time"):
        new.add_argument("--" + name, required=True)
    new.add_argument("--adapter", required=True, choices=["sevenrooms"])
    new.add_argument("--party-size", required=True, type=int)
    new.add_argument("--booking-url")
    new.add_argument("--tg-chat")
    new.add_argument("--tg-thread")
    new.add_argument("--broken-after-minutes", type=float, default=120)
    for name in ("start", "stop"):
        command = commands.add_parser(name)
        command.add_argument("id")
        command.add_argument("--dry-run", action="store_true")
    commands.add_parser("status", help="show watches, launchd status and saved state")
    cron = commands.add_parser("cron", help="print a crontab line; install nothing")
    cron.add_argument("id")
    args = parser.parse_args(argv)
    try:
        controller = WatchCtl(directory if directory is not None else HERE,
                              home if home is not None else os.environ.get("BOOKING_WATCH_HOME", Path.home()),
                              runner if runner is not None else run)
        getattr(controller, args.command)(args)
    except Exception as error:
        print("watchctl: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
