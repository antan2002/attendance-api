"""
Employee Attendance & Analytics API.

A small HR attendance service: employees punch in and out, managers regularize
records, and the business reads analytics over MongoDB aggregation pipelines.

Run from the repository root:

    uvicorn app.main:app --port 8000

Configuration (real environment variables take priority over a local `.env`):

    MONGO_URI   MongoDB connection string, e.g. mongodb://localhost:27017
    MONGO_DB    database name

Everything the API returns uses integer epoch milliseconds for instants and
plain strings for calendar values (dates `YYYY-MM-DD`, times `HH:MM`).  MongoDB
stores instants as BSON datetimes.  See DATA_MODEL.md for the stored shapes and
openapi.yaml for the HTTP contract and the business rules R1-R10.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------- #
# Configuration
#
# load_dotenv() does not override variables that already exist in the real
# environment, so injected values always win over a local .env file.
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True makes every BSON date come back as an aware UTC datetime, which
# removes the naive/aware ambiguity at the heart of the original helper bugs.
client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
employees_coll = db["employees"]
logs_coll = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# The contract accepts only milliseconds in this range; anything smaller looks
# like seconds and is rejected.
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

# An instant as integer milliseconds.  strict=True rejects strings and floats
# (e.g. "1783312500000" or 1783312500000.0) with a 422.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]

DERIVED_FIELDS = ("work_hours", "late_minutes", "overtime_minutes", "half_day")
RECORD_FIELDS = ("status", "punch_in", "punch_out") + DERIVED_FIELDS

_MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


# --------------------------------------------------------------------------- #
# Enums and request models
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: Date

    @model_validator(mode="after")
    def _shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    """Only these fields may change; derived fields sent by a client are ignored."""

    model_config = ConfigDict(extra="ignore")

    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self) -> "RegularizeRequest":
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} may not be null")
        return self


# --------------------------------------------------------------------------- #
# Time and rule helpers (R1-R5)
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    """Current instant, UTC, truncated to whole seconds (R1)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _to_datetime(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def _to_millis(value: Optional[datetime]) -> Optional[int]:
    """Aware (or naive-UTC) datetime -> epoch milliseconds."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _round_half_up(value: float, places: int) -> float:
    """Round half-up (not banker's rounding) to the given number of decimals (R8)."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _minutes_of_day(hm: str) -> int:
    hours, minutes = hm.split(":")
    return int(hours) * 60 + int(minutes)


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    """A shift is overnight when its end is at or before its start (R1)."""
    return _minutes_of_day(shift_end) <= _minutes_of_day(shift_start)


def _shift_moment(day: str, hm: str) -> datetime:
    """Combine a `YYYY-MM-DD` calendar date and an `HH:MM` IST time into an instant."""
    d = Date.fromisoformat(day)
    hours, minutes = map(int, hm.split(":"))
    return datetime(d.year, d.month, d.day, hours, minutes, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    The IST attendance date of a punch-in (R1).

    Normally the IST calendar date of the punch.  For an overnight shift a
    punch-in earlier than the shift end belongs to the shift that started the
    previous day.
    """
    local = punch_in.astimezone(IST)
    day = local.date()
    if _is_overnight(shift_start, shift_end) and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def late_minutes(punch_in: datetime, shift_start: str, day: str) -> int:
    """Whole minutes late, counted from the shift start, not from the grace end (R2)."""
    elapsed = int((punch_in.astimezone(IST) - _shift_moment(day, shift_start)).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, day: str) -> int:
    """Whole minutes past the shift end, only when at least 30 of them (R3)."""
    end_day = Date.fromisoformat(day)
    if _is_overnight(shift_start, shift_end):
        end_day += timedelta(days=1)
    elapsed = int((punch_out.astimezone(IST) - _shift_moment(end_day.isoformat(), shift_end)).total_seconds())
    minutes = elapsed // 60
    return minutes if minutes >= 30 else 0


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """Exact hours between the punches, rounded to 2 decimals half-up (R4)."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(hours: float) -> bool:
    """A day is half when the rounded work hours are below 4.50 (R5)."""
    return hours < 4.50


# --------------------------------------------------------------------------- #
# API serialization helpers (BSON -> epoch milliseconds)
# --------------------------------------------------------------------------- #
def _history_change_value(field: str, value: Any) -> Any:
    return _to_millis(value) if field in ("punch_in", "punch_out") else value


def _serialize_history(entries: list) -> list:
    out = []
    for entry in entries or []:
        changes = {
            field: {
                "from": _history_change_value(field, delta.get("from")),
                "to": _history_change_value(field, delta.get("to")),
            }
            for field, delta in (entry.get("changes") or {}).items()
        }
        out.append(
            {
                "at": _to_millis(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return out


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _to_millis(doc["created_at"]),
    }


def serialize_record(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _to_millis(doc.get("punch_in")),
        "punch_out": _to_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": _serialize_history(doc.get("history", [])),
    }


# --------------------------------------------------------------------------- #
# Indexes (created once at startup, idempotently)
# --------------------------------------------------------------------------- #
def create_indexes() -> None:
    employees_coll.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
    employees_coll.create_index([("department", 1), ("emp_code", 1)], name="dept_emp")
    employees_coll.create_index([("joined_on", 1)], name="joined_on")

    # The unique key both enforces "one record per (emp_code, date)" and lets the
    # punch-in race be decided by the database instead of a check-then-insert.
    logs_coll.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_date"
    )
    logs_coll.create_index([("date", -1), ("emp_code", 1)], name="date_desc_emp")


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health() -> dict:
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate) -> dict:
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on.isoformat(),
        "created_at": _now(),
    }
    # Rely on the unique index: two concurrent creates cannot both succeed.
    try:
        employees_coll.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    query: dict = {}
    if department is not None:
        query["department"] = department
    total = employees_coll.count_documents(query)
    cursor = (
        employees_coll.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance: punch in / out, list, regularize
# --------------------------------------------------------------------------- #
def _get_employee(emp_code: str) -> dict:
    emp = employees_coll.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")
    return emp


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest) -> dict:
    emp = _get_employee(body.emp_code)
    punched_at = _to_datetime(body.punched_at) if body.punched_at is not None else _now()
    day = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status.value,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(punched_at, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        logs_coll.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest) -> dict:
    emp = _get_employee(body.emp_code)
    punched_at = _to_datetime(body.punched_at) if body.punched_at is not None else _now()

    # The most recent record whose punch-in is at or before this instant.  This
    # works across midnight for overnight shifts because punch_in is an instant.
    record = logs_coll.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")

    punch_in_at = record["punch_in"]
    delta = (punched_at - punch_in_at).total_seconds()
    if delta <= 0 or delta > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    hours = work_hours(punch_in_at, punched_at)
    updates = {
        "punch_out": punched_at,
        "work_hours": hours,
        "overtime_minutes": overtime_minutes(
            punched_at, emp["shift_start"], emp["shift_end"], record["date"]
        ),
        "half_day": is_half_day(hours),
    }
    # Conditional update on punch_out == None: two simultaneous punch-outs
    # yield exactly one 200, the loser gets 409.
    updated = logs_coll.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    return serialize_record(updated)


def _attendance_query(
    emp_code: Optional[str],
    date_from: Optional[Date],
    date_to: Optional[Date],
    status: Optional[Status],
) -> dict:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        date_range: dict = {}
        if date_from is not None:
            date_range["$gte"] = date_from.isoformat()
        if date_to is not None:
            date_range["$lte"] = date_to.isoformat()
        query["date"] = date_range
    if status is not None:
        query["status"] = status.value
    return query


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[Date] = None,
    date_to: Optional[Date] = None,
    status: Optional[Status] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    query = _attendance_query(emp_code, date_from, date_to, status)
    total = logs_coll.count_documents(query)
    cursor = (
        logs_coll.find(query, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _normalize_record(record: dict) -> dict:
    """Current record values with the documented defaults applied (missing == 0/false)."""
    return {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }


def _apply_regularization(emp: dict, record: dict, body: RegularizeRequest, day: str) -> dict:
    """
    Merge the request onto the stored record, enforce the PATCH rules, then
    recompute every derived field from the final punch times (R2-R5).
    """
    supplied = body.model_fields_set
    status = body.status.value if "status" in supplied else record.get("status")
    punch_in = _to_datetime(body.punch_in) if "punch_in" in supplied else record.get("punch_in")
    punch_out = _to_datetime(body.punch_out) if "punch_out" in supplied else record.get("punch_out")

    if status in (Status.ABSENT.value, Status.LEAVE.value):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(status_code=422, detail="punch times are not allowed for ABSENT or LEAVE")
        punch_in = None
        punch_out = None
    elif punch_in is None:
        raise HTTPException(status_code=422, detail="a presence status requires a punch_in")

    if punch_in is not None and attendance_date(punch_in, emp["shift_start"], emp["shift_end"]) != day:
        raise HTTPException(status_code=422, detail="punch_in does not belong to this attendance date")

    if punch_out is not None:
        delta = (punch_out - punch_in).total_seconds()
        if delta <= 0 or delta > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    if punch_in is None:  # ABSENT / LEAVE
        return {
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }

    final_work_hours = None
    final_overtime = 0
    final_half_day = False
    if punch_out is not None:
        final_work_hours = work_hours(punch_in, punch_out)
        final_overtime = overtime_minutes(punch_out, emp["shift_start"], emp["shift_end"], day)
        final_half_day = is_half_day(final_work_hours)

    return {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": final_work_hours,
        "late_minutes": late_minutes(punch_in, emp["shift_start"], day),
        "overtime_minutes": final_overtime,
        "half_day": final_half_day,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize(emp_code: str, date: Date, body: RegularizeRequest) -> dict:
    emp = _get_employee(emp_code)
    day = date.isoformat()
    record = logs_coll.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(status_code=404, detail="no attendance record for that date")

    final = _apply_regularization(emp, record, body, day)

    current = _normalize_record(record)
    changes = {
        field: {"from": current[field], "to": final[field]}
        for field in RECORD_FIELDS
        if current[field] != final[field]
    }
    if not changes:
        raise HTTPException(status_code=422, detail="the request does not change anything")

    entry = {
        "at": _now(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic guard: match the history array we read.  If another edit
    # appended to it in the meantime the match fails and we return 409 instead
    # of silently dropping a history entry.
    if "history" in record:
        guard: dict = {"_id": record["_id"], "history": record["history"]}
    else:
        guard = {"_id": record["_id"], "history": {"$exists": False}}

    updated = logs_coll.find_one_and_update(
        guard,
        {"$set": {field: final[field] for field in RECORD_FIELDS}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record was modified concurrently, retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# Analytics: shared aggregation fragments
# --------------------------------------------------------------------------- #
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

_IS_PRESENCE = {"$in": ["$status", PRESENCE_STATUSES]}
_IS_WEEKDAY = {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}
_HALF_OR_FULL = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}


def _month_bounds(month: str) -> tuple[Date, Date]:
    year, mon = (int(part) for part in month.split("-"))
    start = Date(year, mon, 1)
    end = Date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
    return start, end


def _utc_midnight(day: Date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _working_days(month_start: Date, month_end: Date, joined_on: Date) -> int:
    """Weekdays (Mon-Fri) in the month from the later of the month start and joined_on (R7)."""
    start = max(month_start, joined_on)
    if start > month_end:
        return 0
    return sum(
        1
        for offset in range((month_end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
    )


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = _month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$addFields": {"_dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [_IS_PRESENCE, _IS_WEEKDAY]}, _HALF_OR_FULL, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = _month_bounds(month)
    match: dict = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department

    def per_log(expr: dict) -> dict:
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    return [
        # Headcount (R9) falls out of the filtered group: everyone still in the
        # pipeline joined on or before the end of the month.
        {"$match": match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "codes": {"$push": "$emp_code"}}},
        # localField/foreignField uses the attendance_logs emp_code index.
        {"$lookup": {"from": "attendance_logs", "localField": "codes", "foreignField": "emp_code", "as": "logs"}},
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {"$and": [{"$gte": ["$$l.date", start.isoformat()]}, {"$lte": ["$$l.date", end.isoformat()]}]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": per_log(
                    {"$cond": [
                        {"$and": [
                            {"$in": ["$$l.status", PRESENCE_STATUSES]},
                            {"$and": [
                                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 2]},
                                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 6]},
                            ]},
                        ]},
                        {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]},
                        0,
                    ]}
                ),
                "work_hours_sum": per_log(
                    {"$cond": [
                        {"$and": [{"$in": ["$$l.status", PRESENCE_STATUSES]}, {"$ne": ["$$l.work_hours", None]}]},
                        {"$ifNull": ["$$l.work_hours", 0]},
                        0,
                    ]}
                ),
                "work_hours_count": per_log(
                    {"$cond": [
                        {"$and": [{"$in": ["$$l.status", PRESENCE_STATUSES]}, {"$ne": ["$$l.work_hours", None]}]},
                        1,
                        0,
                    ]}
                ),
                "late_count": per_log({"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]}),
                "total_late_minutes": per_log({"$ifNull": ["$$l.late_minutes", 0]}),
                "leave_count": per_log({"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}),
                "on_duty_count": per_log({"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}),
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, department: Optional[str], limit: int) -> list:
    start, end = _month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # inner $unwind drops logs whose employee does not exist
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$emp.name", "department": "$emp.department"}},
        # $rank is standard competition ranking (1, 2, 2, 4) and requires a
        # single-field sortBy, so ranking uses the total only; the emp_code
        # tie-break is applied to the final output order below.
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_pipeline(department: str, date_from: Date, date_to: Date) -> list:
    lower, upper = date_from.isoformat(), date_to.isoformat()
    # $densify treats its upper bound as exclusive, so add a day to include `to`.
    lower_dt = _utc_midnight(date_from)
    upper_dt = _utc_midnight(date_to + timedelta(days=1))

    return [
        {"$match": {"date": {"$gte": lower, "$lte": upper}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "present_count": {"$sum": {"$cond": [_IS_PRESENCE, _HALF_OR_FULL, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        # Seed one row so $densify always has input, even when the department has
        # no records at all in the range.
        {"$unionWith": {
            "coll": "employees",
            "pipeline": [
                {"$match": {"department": department}},
                {"$limit": 1},
                {"$project": {"_id": 0, "day": lower_dt, "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}},
            ],
        }},
        {"$group": {"_id": "$day", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [lower_dt, upper_dt]}}},
        {"$addFields": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$sort": {"_id": 1}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
            "dow": {"$dayOfWeek": "$_id"},
        }},
        # Headcount per day (R9): a constant department match keeps this lookup
        # on the employees department index.
        {"$lookup": {
            "from": "employees",
            "pipeline": [{"$match": {"department": department}}, {"$project": {"_id": 0, "joined_on": 1}}],
            "as": "dept_emps",
        }},
        {"$addFields": {"headcount": {"$size": {"$filter": {
            "input": "$dept_emps",
            "as": "e",
            "cond": {"$lte": ["$$e.joined_on", "$date"]},
        }}}}},
        {"$addFields": {"is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=_MONTH_PATTERN)],
) -> dict:
    emp = _get_employee(emp_code)
    start, end = _month_bounds(month)
    rows = list(logs_coll.aggregate(employee_monthly_pipeline(emp_code, month)))
    row = rows[0] if rows else {}

    working_days = _working_days(start, end, Date.fromisoformat(emp["joined_on"]))
    present_days = float(row.get("present_days", 0.0))
    attendance_pct = _round_half_up(present_days / working_days * 100, 4) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": _round_half_up(present_days, 2),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: Annotated[str, Query(pattern=_MONTH_PATTERN)],
    department: Optional[str] = None,
) -> dict:
    rows = list(employees_coll.aggregate(department_summary_pipeline(month, department)))
    items = []
    for row in rows:
        total_work_hours = float(row.get("work_hours_sum", 0.0))
        work_hours_count = int(row.get("work_hours_count", 0))
        items.append({
            "department": row["department"],
            "headcount": int(row.get("headcount", 0)),
            "present_days": _round_half_up(float(row.get("present_days", 0.0)), 2),
            "avg_work_hours": _round_half_up(total_work_hours / work_hours_count, 2) if work_hours_count else None,
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: Annotated[str, Query(pattern=_MONTH_PATTERN)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
) -> dict:
    rows = list(logs_coll.aggregate(leaderboard_pipeline(month, department, limit)))
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_: Annotated[Date, Query(alias="from")],
    to: Annotated[Date, Query()],
) -> dict:
    if employees_coll.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="unknown department")
    if to < from_:
        raise HTTPException(status_code=422, detail="to must not be before from")
    if (to - from_).days > 91:
        raise HTTPException(status_code=422, detail="range exceeds 92 days")

    rows = list(logs_coll.aggregate(trend_pipeline(department, from_, to)))
    items = []
    for row in rows:
        rate = row.get("attendance_rate")
        moving = row.get("moving_avg_7d")
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": _round_half_up(float(row["present_count"]), 2),
            "late_count": int(row["late_count"]),
            "attendance_rate": _round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": _round_half_up(moving, 4) if moving is not None else None,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain an endpoint's main query
# --------------------------------------------------------------------------- #
ExplainTarget = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=_MONTH_PATTERN),
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[Date] = None,
    date_to: Optional[Date] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[Date], Query(alias="from")] = None,
    to: Optional[Date] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    if endpoint == "attendance_list":
        query = _attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        inner: dict = {
            "find": collection,
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        collection = "attendance_logs"
        inner = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "employees"
        inner = {"aggregate": collection, "pipeline": department_summary_pipeline(month, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "attendance_logs"
        inner = {"aggregate": collection, "pipeline": leaderboard_pipeline(month, department, limit), "cursor": {}}
    else:  # department_trend
        if department is None or from_ is None or to is None:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        if to < from_:
            raise HTTPException(status_code=422, detail="to must not be before from")
        if (to - from_).days > 91:
            raise HTTPException(status_code=422, detail="range exceeds 92 days")
        collection = "attendance_logs"
        inner = {"aggregate": collection, "pipeline": trend_pipeline(department, from_, to), "cursor": {}}

    raw_explain = db.command("explain", inner, verbosity="executionStats")
    # Explain output can hold BSON types (e.g. Timestamp); round-trip through
    # Extended JSON so the response is plain, serializable JSON.
    explain = json.loads(json_util.dumps(raw_explain))
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
