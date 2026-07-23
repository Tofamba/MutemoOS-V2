"""
deadline_engine.py — Deterministic date/deadline calculation for legal
queries. Pure code, no LLM involved in the arithmetic — extraction and
calculation are both regex/date-math only, so results are either found
in cited retrieved text or explicitly reported as not found. Never
guesses or infers a date or period.
"""

import re
from datetime import date, datetime, timedelta
from typing import Optional


def extract_event_date(query: str) -> Optional[date]:
    patterns = [
        r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b",
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b",
    ]
    months = {m: i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"], start=1)}

    for pattern in patterns:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            groups = match.groups()
            try:
                if groups[0].isdigit():
                    day, month_name, year = int(groups[0]), groups[1], int(groups[2])
                else:
                    month_name, day, year = groups[0], int(groups[1]), int(groups[2])
                month = months[month_name.capitalize()]
                return date(year, month, day)
            except (ValueError, KeyError):
                continue
    return None


def extract_notice_period_days(source_text: str) -> Optional[int]:
    patterns = [
        r"\bat least\s+(\d+)\s+days?\b",
        r"\bnot less than\s+(\d+)\s+days?\b",
        r"\bwithin\s+(\d+)\s+days?\b",
        r"\b(\d+)\s+days?\s+(?:before|prior to|in advance)\b",
        r"\b(\d+)\s+days?\'?\s+(?:written\s+)?notice\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, source_text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def calculate_deadline(event_date: date, notice_days: int, today: Optional[date] = None) -> dict:
    today = today or datetime.utcnow().date()
    deadline = event_date - timedelta(days=notice_days)
    days_remaining = (deadline - today).days

    if days_remaining < 0:
        status = f"DEADLINE PASSED — was due {abs(days_remaining)} day(s) ago"
    elif days_remaining == 0:
        status = "URGENT — deadline is today"
    elif days_remaining <= 2:
        status = f"URGENT — only {days_remaining} day(s) remaining"
    else:
        status = f"{days_remaining} day(s) remaining"

    return {
        "event_date": event_date.isoformat(),
        "notice_period_days": notice_days,
        "deadline": deadline.isoformat(),
        "today": today.isoformat(),
        "days_remaining": days_remaining,
        "status": status,
    }


def try_compute_deadline(query: str, legal_results: list, zlr_results: list) -> Optional[dict]:
    event_date = extract_event_date(query)
    if not event_date:
        return None

    for r in (legal_results or []) + (zlr_results or []):
        notice_days = extract_notice_period_days(r.get("text", ""))
        if notice_days is not None:
            result = calculate_deadline(event_date, notice_days)
            result["source_reference"] = r.get("reference") or r.get("filename") or r.get("citation") or "Unknown source"
            return result
    return None
