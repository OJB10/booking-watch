"""SevenRooms availability adapter, using only the Python standard library."""

from datetime import date
import json
from urllib.parse import urlencode
from urllib.error import HTTPError
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_response(self, request, response):
        if 300 <= response.code < 400:
            raise HTTPError(request.full_url, response.code, "Redirect refused",
                            response.headers, response)
        return response

    https_response = http_response


def build_url(cfg) -> str:
    day = date.fromisoformat(cfg["date"])
    query = urlencode({
        "venue": cfg["venue"],
        "time_slot": cfg["preferred_time"],
        "party_size": cfg["party_size"],
        "halo_size_interval": 48,
        "start_date": f"{day.month:02d}-{day.day:02d}-{day.year:04d}",
        "num_days": 1,
        "channel": "SEVENROOMS_WIDGET",
    }, safe=":")
    return "https://www.sevenrooms.com/api-yoa/availability/widget/range?" + query


def parse(payload: dict, date_iso: str) -> tuple:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Missing or invalid data object")
    availability = payload["data"].get("availability")
    if not isinstance(availability, dict) or date_iso not in availability:
        raise ValueError("Missing availability for requested date")
    shifts = availability[date_iso]
    if not isinstance(shifts, list):
        raise ValueError("Availability must be a list of shifts")
    if not shifts:
        return "NOT_RELEASED", []
    bookable = []
    for shift in shifts:
        if not isinstance(shift, dict) or not isinstance(shift.get("times"), list):
            raise ValueError("Each shift must have a times list")
        for entry in shift["times"]:
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get("type"), str)
                    or not isinstance(entry.get("time"), str)):
                raise ValueError("Each time must have string type and time fields")
            if entry["type"] == "book":
                bookable.append(entry["time"])
    return ("BOOKABLE", sorted(set(bookable))) if bookable else ("REQUEST_ONLY", [])


def check(cfg, fetch=None) -> tuple:
    if fetch is None:
        def fetch(url):
            request = urllib.request.Request(url, headers={
                "User-Agent": "booking-watch/1.0 (+https://github.com/OJB10/booking-watch)"})
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(request, timeout=20) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise ValueError("Booking response exceeds 2000000 bytes")
                return body
    return parse(json.loads(fetch(build_url(cfg))), cfg["date"])
