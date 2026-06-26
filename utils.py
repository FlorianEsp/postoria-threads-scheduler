from __future__ import annotations

import hashlib
from datetime import datetime, date, time
from zoneinfo import ZoneInfo


def caption_hash(caption: str) -> str:
    return hashlib.sha256(caption.strip().encode("utf-8")).hexdigest()


def local_to_utc_z(day: date, local_time: time, tz_name: str = "Europe/Brussels") -> str:
    tz = ZoneInfo(tz_name)
    local_dt = datetime.combine(day, local_time).replace(tzinfo=tz)
    utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_utc_to_local(iso_z: str, tz_name: str = "Europe/Brussels") -> datetime:
    dt = datetime.strptime(iso_z, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(tz_name))
