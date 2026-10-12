# REVIEW.md

Defects found in the starter `app/main.py`. The starter assumes UTC==IST, naive
datetimes equal to whatever clock happens to be local, no indexes and no
concurrency control; most defects follow from those four assumptions.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Applies the shift time to the punch instant as if it were IST (`punch_in.replace(hour=9, minute=30)`), but the value is UTC, so every late calculation is 5:30 off. | Employee on shift 09:30, punch at 09:31 IST (`04:01Z`): function compares 04:01 against 09:30 -> late 0. | Convert the punch to IST first, then anchor the shift start on the attendance date. |
| 2 | `compute_late_minutes` | Grace test is floored minutes `> 10` (i.e. `> 10:59`), not elapsed seconds `> 600`. | Punch 09:40:01 (`601s`, R2 says late=10) returns 0; only 09:41:00+ is caught. | Test `elapsed_seconds > 600`, then `minutes = elapsed // 60`. |
| 3 | `compute_late_minutes` | No overnight handling: a 02:00 punch for a 22:00-06:00 shift is measured against 22:00 the same calendar day. | Overnight employee punches at 02:00 IST -> late is nonsense (large negative/clamped 0). | Compute the attendance date (R1) and anchor the shift start on it; 02:00 belongs to the previous day's 22:00 shift. |
| 4 | `compute_overtime` | Subtracts a timezone-aware `end` (`tzinfo=IST`) from a naive `punch_out` -> `TypeError`, a 500. | Any punch-out reaching this helper: `TypeError: can't subtract offset-naive and offset-aware datetimes`. | Keep every instant tz-aware UTC and convert to IST only for comparison. Helper rewritten. |
| 5 | `compute_overtime` | Missing the R3 minimum: returns any positive minutes. | Punch out 5 min after shift end -> returns 5; contract says 0 (needs >= 30). | Return the floored minutes only when `>= 30`, else 0. |
| 6 | `compute_overtime` | Uses the attendance date for the shift end even for overnight shifts. | 22:00-06:00, out 06:40 -> measures from 06:00 the same day, off by 24h. | Shift end falls on the next day for overnight shifts; helper accounts for it. |
| 7 | `compute_work_hours` | Python `round()` is banker's rounding, not half-up (R4/R8). | `round(2.675, 2) == 2.67` instead of the contract's `2.68`. | Compute from exact seconds with `Decimal` and `ROUND_HALF_UP`. |
| 8 | `create_employee` | Check-then-insert race and no unique index, so two concurrent creates can both insert the same `emp_code`. | Fire 10 parallel POSTs with the same body: more than one 201, duplicate rows. | Unique index on `emp_code`; insert directly and map `DuplicateKeyError` -> 409. |
| 9 | `create_employee` | `created_at = datetime.now()` is naive local time and is returned as a datetime, not epoch ms. | Response `created_at` is an ISO string, not an integer. | Server clock in UTC, serialized to epoch milliseconds. |
| 10 | `create_employee` | `shift_start` equal to `shift_end` is not rejected. | POST with both `09:30` -> 201; contract requires 422. | Model validator rejects equal shift times. |
| 11 | `list_employees` | `skip = page * page_size` should be `(page - 1) * page_size`; page 1 skips a page. | With 25 employees and `page_size=20`, `page=1` returns the last 5. | `.skip((page - 1) * page_size)`. |
| 12 | `list_employees` | `total = count_documents({})` ignores the `department` filter (R10). | `?department=Sales` with 3 of 25 matching returns `total=25`. | Count with the same query. |
| 13 | `list_employees` | No sort, so results come back in natural order, not by `emp_code`. | Insert out of order, list -> not ascending. | `.sort("emp_code", 1)`. |
| 14 | `list_employees` / `list_attendance` | `page >= 1` and `page_size <= 100` are not enforced (R10). | `?page=0` or `?page_size=100000` is accepted. | Query constraints `ge=1`, `le=100`. |
| 15 | `punch_in` | Unknown employee is not a 404: `emp["shift_start"]` raises `TypeError` -> 500. | POST with `EMP9999` -> 500. | Shared `_get_employee` -> 404. |
| 16 | `punch_in` | Check-then-insert race; no unique index. | Two identical concurrent punch-ins both return 201. | Unique `(emp_code, date)` index; insert and map `DuplicateKeyError` -> 409. |
| 17 | `punch_in` | Attendance date from `ts.date()` (UTC), not the IST date (R1); no overnight rule. | Punch at 05:00 IST records the previous UTC day. | `attendance_date()` computes the IST date and the overnight shift day. |
| 18 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the local timezone, and `datetime.now()` is naive and not truncated. | On a non-UTC host the stored instants shift. | Aware UTC via `fromtimestamp(..., tz=utc)`, truncated to seconds. |
| 19 | `punch_in` | `status` accepts any string; only PRESENT/WFH/ON_DUTY are allowed. | `{"status": "HOLIDAY"}` -> 201 (`"ABSENT"` would too). | `PresenceStatus` enum -> 422 otherwise. |
| 20 | `punch_in` / `list_attendance` | Leak the internal `_id` as `"id"` in the response. | Response contains an `id` field; `_id` must never appear. | Responses are built from an explicit field list; no `_id`. |
| 21 | `punch_in` / `list_attendance` | Return raw BSON datetimes; the contract wants epoch milliseconds. | `punch_in` is an ISO string in the response. | `serialize_record` converts every instant to epoch ms. |
| 22 | `list_attendance` | Loads the whole collection into memory and sorts in Python. | At 100k records the process holds 100k documents and the query COLLSCANs. | Indexed `.find().sort().skip().limit()`; counting with the same filter. |
| 23 | `list_attendance` | No `date_from > date_to` validation. | `?date_from=2026-07-10&date_to=2026-07-01` returns empty instead of 422. | Validate and raise 422. |
| 24 | `list_attendance` | Sort by `date` only, no `emp_code` tie-break. | Same-day records are not ordered by `emp_code`. | Sort `[("date", -1), ("emp_code", 1)]`. |
| 25 | `health` | Never pings MongoDB; always returns 200. | Stop MongoDB, `GET /health` still 200. | `admin.command("ping")`; return 503 on `PyMongoError`. |
| 26 | startup | No indexes are created. | `getIndexes()` shows only `_id`; explain shows COLLSCAN; punch-in race is unguarded. | `create_indexes()` runs in the lifespan on startup. |

## Looked at, and deliberately left alone

* `load_dotenv()` without `override=True` — correct: real environment variables
  must win over a local `.env`.
* The default `MONGO_URI` fallback to `localhost` — a harmless convenience; the
  injected environment variables override it.
* The unused `IST` constant in the helpers — a symptom of defects 1/4, not a bug
  in itself; the constant is reused correctly now.
* Pydantic ignoring unknown request fields — that is exactly what the contract
  wants for derived fields sent by a client ("ignore it"), so no change needed.
* `class EmployeeIn` naming and the `id`-shaped `_id` pop — the naming is fine;
  the `_id` handling was already the one correct instinct in the starter (kept).
