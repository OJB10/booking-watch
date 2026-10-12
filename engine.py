"""Deterministic booking checks with persistent alert state. No LLM calls."""

import argparse
from datetime import date, datetime, timedelta, timezone
import fcntl
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError
import urllib.request

RANK = {"NOT_RELEASED": 0, "REQUEST_ONLY": 1, "BOOKABLE": 2}
DEFAULT_STATE = {
    "last_state": None, "alerted_rank": 0, "fail_streak": 0,
    "first_fail_ts": None, "broken_alerted": False, "done": False,
    "last_ok_ts": None, "backoff_until": None, "last_http_status": None,
}


def load_env_file(path=None):
    """Read Telegram KEY=VALUE settings without executing shell code."""
    path = Path(path) if path is not None else Path.home() / ".config/booking-watch/env"
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    if path.stat().st_mode & 0o077:
        print(f"Warning: settings file allows group or other access: {path}")
    allowed = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_THREAD_ID"}
    for line in contents.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in allowed:
            continue
        quote = None
        for index, character in enumerate(value):
            if character == quote:
                quote = None
            elif quote is None:
                if character in "\"'":
                    quote = character
                elif character == "#" and index > 0 and value[index - 1] == " ":
                    value = value[:index].rstrip()
                    break
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def send_telegram(cfg, message, opener=None):
    """Send once. Never expose request URLs, response bodies or exceptions."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (cfg.get("tg_chat") or "").strip() or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    thread = (cfg.get("tg_thread") or "").strip() or os.environ.get("TELEGRAM_THREAD_ID", "").strip()
    for variable, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)):
        if not value:
            print("Missing " + variable)
            return False
    payload = {"chat_id": chat, "text": _truncate(message, 4000)}
    if thread:
        payload["message_thread_id"] = thread
    try:
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with (opener or urllib.request.urlopen)(request, timeout=60) as response:
            if response.status != 200:
                return False
            result = json.loads(response.read())
            return isinstance(result, dict) and result.get("ok") is True
    except Exception:
        # urllib exceptions can contain the credential-bearing URL.
        return False


def _save(path, state):
    """Replace state only after the complete new JSON has been written."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp",
                                         delete=False) as stream:
            temporary = stream.name
            json.dump(state, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def _truncate(text, limit):
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _send(send_fn, message):
    # Delivery failures must not consume an alert or discard check state.
    try:
        return send_fn(_truncate(message, 4000)) is True
    except Exception:
        return False


def _date_human(date_iso):
    day = date.fromisoformat(date_iso)
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{weekdays[day.weekday()]} {day.day} {months[day.month - 1]}"


def _failed_check(cfg, path, state, now, send_fn, reason):
    """Keep the failure clock running even while requests are paused."""
    state["fail_streak"] += 1
    if state["first_fail_ts"] is None:
        state["first_fail_ts"] = now.isoformat()
    elapsed = now - datetime.fromisoformat(state["first_fail_ts"])
    minutes = int(elapsed.total_seconds() // 60)
    if (elapsed >= timedelta(minutes=cfg.get("broken_after_minutes", 120))
            and not state["broken_alerted"]):
        message = (
            f"{_truncate(cfg['label'], 100)} watcher can't read the booking page (failing for {minutes} min). "
            f"It will keep trying. Check it or book by hand: {cfg['booking_url']}"
        )
        if _send(send_fn, message):
            state["broken_alerted"] = True
    _save(path, state)
    return f"{reason}; fail_streak={state['fail_streak']}; broken_alerted={state['broken_alerted']}"


def run_once(cfg, state_path, now, check_fn, send_fn) -> str:
    """Run one check; clock, network and delivery are supplied by the caller."""
    if now.date() > date.fromisoformat(cfg["date"]):
        return "inactive"
    path = Path(state_path)
    try:
        with path.open(encoding="utf-8") as stream:
            loaded = json.load(stream)
    except FileNotFoundError:
        loaded = {}
    state = {**DEFAULT_STATE, **loaded} if isinstance(loaded, dict) else DEFAULT_STATE.copy()
    if state["done"]:
        return "inactive"

    if state["backoff_until"] is not None:
        if now < datetime.fromisoformat(state["backoff_until"]):
            return _failed_check(cfg, path, state, now, send_fn,
                                 f"backing off (HTTP {state['last_http_status']})")
        state["backoff_until"] = None

    try:
        current, times = check_fn(cfg)
        rank = RANK[current]
    except Exception as error:
        if isinstance(error, HTTPError) and error.code in (403, 429):
            state["last_http_status"] = error.code
            state["backoff_until"] = (now + timedelta(minutes=60)).isoformat()
            print(f"{cfg['id']}: HTTP {error.code}, backing off for 60 minutes")
        return _failed_check(cfg, path, state, now, send_fn,
                             f"check failed ({type(error).__name__})")

    label = _truncate(cfg["label"], 100)
    if state["broken_alerted"]:
        if _send(send_fn, f"{label} watcher: reading the booking page again."):
            state["broken_alerted"] = False
    state.update(fail_streak=0, first_fail_ts=None, last_ok_ts=now.isoformat(), last_state=current)
    delivery = "no alert"
    if rank > state["alerted_rank"]:
        human = _date_human(cfg["date"])
        if current == "REQUEST_ONLY":
            message = (
                f"{label}: {human} has been released but there are no instant tables yet (full). "
                f"Cancellations can appear, so check it: {cfg['booking_url']}"
            )
        else:
            prefix = f"{label}: TABLES OPEN for {human}, party of {cfg['party_size']}. Times: "
            suffix = f". Book now: {cfg['booking_url']}"
            shown = [_truncate(time, 16) for time in times[:8]]
            more = f" and {len(times) - 8} more" if len(times) > 8 else ""
            message = prefix + ", ".join(shown) + more + suffix
            while len(message) > 4000 and shown:
                shown.pop()
                message = prefix + ", ".join(shown) + (", " if shown else "") + "..." + suffix
        if _send(send_fn, message):
            state["alerted_rank"] = rank
            if current == "BOOKABLE":
                state["done"] = True
            delivery = "alert sent"
        else:
            delivery = "delivery failed; will retry"
    _save(path, state)
    return f"{current}; {delivery}; done={state['done']}"


def _load_config(filename):
    with open(filename, encoding="utf-8") as stream:
        cfg = json.load(stream)
    if not isinstance(cfg, dict):
        raise ValueError("Config must be a JSON object")
    for key in ("id", "label", "adapter", "venue", "date", "preferred_time",
                "booking_url"):
        if not isinstance(cfg.get(key), str) or not cfg[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
        if any(character in cfg[key] for character in "\r\n"):
            raise ValueError(f"{key} must be a single line")
    for key in ("tg_chat", "tg_thread"):
        value = cfg.get(key)
        if value is not None and (not isinstance(value, str)
                                  or any(character in value for character in "\r\n")):
            raise ValueError(f"{key} must be a single-line string or null")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cfg["id"]):
        raise ValueError("id must contain only letters, digits, underscores or hyphens")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", cfg["venue"]):
        raise ValueError("venue must contain only letters, digits, underscores or hyphens")
    if not cfg["booking_url"].startswith("https://"):
        raise ValueError("booking_url must start with https://")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", cfg["adapter"]):
        raise ValueError("adapter must be a module name")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cfg["date"]):
        raise ValueError("date must be YYYY-MM-DD")
    date.fromisoformat(cfg["date"])
    if type(cfg.get("party_size")) is not int or cfg["party_size"] < 1:
        raise ValueError("party_size must be a positive integer")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cfg["preferred_time"]):
        raise ValueError("preferred_time must be HH:MM")
    threshold = cfg.get("broken_after_minutes", 120)
    if (type(threshold) not in (int, float) or not math.isfinite(threshold)
            or threshold < 0):
        raise ValueError("broken_after_minutes must be a finite non-negative number")
    adapter = importlib.import_module("adapters." + cfg["adapter"])
    if not callable(getattr(adapter, "check", None)):
        raise ValueError("adapter must provide check(cfg)")
    return cfg, adapter.check


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", metavar="watch-config.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        load_env_file()
        cfg, check_fn = _load_config(args.config)
    except Exception as error:
        print(f"{now.isoformat()} bad config ({type(error).__name__})")
        return 2

    try:
        directory = Path(os.environ.get("BOOKING_WATCH_STATE_DIR")
                         or Path.home() / ".local/state/booking-watch").expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".dryrun.json" if args.dry_run else ".json"
        state_path = directory / (cfg["id"] + suffix)

        def send_fn(message):
            if args.dry_run:
                print("WOULD SEND: " + message)
                return True
            return send_telegram(cfg, message)

        lock_fd = os.open(str(state_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, "a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(f"{cfg['id']}: previous run still active, skipping")
                return 0
            summary = run_once(cfg, state_path, now, check_fn, send_fn)
    except Exception as error:
        summary = f"runtime error ({type(error).__name__})"
    print(f"{now.isoformat()} {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
