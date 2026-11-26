"""Helpers for the Indian market: rupee amounts in lakhs/crores, EMI, IST time."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

# Fixed offset avoids needing the tzdata package on Windows.
IST = timezone(timedelta(hours=5, minutes=30), "IST")

LAKH = 100_000
CRORE = 10_000_000

_UNITS = {
    "cr": CRORE, "crore": CRORE, "crores": CRORE,
    "l": LAKH, "lac": LAKH, "lacs": LAKH, "lakh": LAKH, "lakhs": LAKH,
    "k": 1_000, "thousand": 1_000,
}
_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?|lac|l|k|thousand)?\b", re.I)


def parse_inr(value: Union[str, int, float, None]) -> Optional[int]:
    """Convert '80 L', '1.2 Cr', '₹95,00,000', 1.5 or 8500000 into whole rupees.

    Bare numbers are read the way Indian buyers say them: below 10 means crores
    (1.5 -> 1.5 Cr), 10 to 999 means lakhs (80 -> 80 L), larger numbers are rupees.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _from_bare(float(value))
    text = str(value).lower().replace(",", "").replace("₹", "").replace("rs.", "").replace("rs", "").strip()
    if not text:
        return None
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    number, unit = float(m.group(1)), (m.group(2) or "").lower()
    if unit:
        return int(round(number * _UNITS[unit]))
    return _from_bare(number)


def _from_bare(n: float) -> Optional[int]:
    if n <= 0:
        return None
    if n < 10:
        return int(round(n * CRORE))
    if n < 1000:
        return int(round(n * LAKH))
    return int(round(n))


def indian_grouping(n: int) -> str:
    """12345678 -> '1,23,45,678'."""
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def format_inr(rupees: Optional[float]) -> str:
    """16200000 -> '₹1.62 Cr', 9500000 -> '₹95 L', 24000 -> '₹24,000'."""
    if rupees is None:
        return "—"
    if rupees >= CRORE:
        return "₹" + f"{rupees / CRORE:.2f}".rstrip("0").rstrip(".") + " Cr"
    if rupees >= LAKH:
        return "₹" + f"{rupees / LAKH:.1f}".rstrip("0").rstrip(".") + " L"
    return "₹" + indian_grouping(int(rupees))


def emi(price: float, loan_share: float = 0.8, annual_rate: float = 8.5, years: int = 20) -> int:
    """Monthly EMI for a home loan covering `loan_share` of the price."""
    principal = price * loan_share
    r = annual_rate / 12 / 100
    n = years * 12
    return int(round(principal * r * (1 + r) ** n / ((1 + r) ** n - 1)))


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_ist(value: str) -> datetime:
    """Parse '2026-11-28T11:00' (assumed IST) or an ISO string with offset."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def fmt_ist(dt: datetime) -> str:
    """'Sat 28 Nov, 11:00 AM IST'."""
    dt = dt.astimezone(IST)
    hour = dt.strftime("%I").lstrip("0")
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}, {hour}:{dt.strftime('%M %p')} IST"
