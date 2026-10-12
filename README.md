# Employee Attendance & Analytics API

FastAPI + MongoDB service for employee punch-in/punch-out, HR corrections with
an audit trail, and analytics built on MongoDB aggregation pipelines. All
application code lives in `app/main.py`; this repo follows the supplied
`openapi.yaml` contract and `DATA_MODEL.md` document shapes.

## Requirements

* Python 3.11+
* MongoDB 6.0+ (a local `mongod`, a container, or a free Atlas M0 cluster)

## Run

From the repository root:

```bash
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# Windows cmd:         .venv\Scripts\activate.bat
# macOS/Linux:         source .venv/bin/activate
pip install -r requirements.txt

# configure the database: copy the example and edit it.
# Windows:      copy .env.example .env
# macOS/Linux:  cp .env.example .env
# .env is git-ignored; real environment variables override it.
# (Or set MONGO_URI / MONGO_DB directly in the environment.)

uvicorn app.main:app --port 8000
```

`GET /health` returns 200 once MongoDB answers a ping, 503 otherwise. All
indexes are created automatically at startup, so there is no manual setup step.

## Notes

* Instants cross the API boundary as integer epoch **milliseconds** and are
  stored as BSON datetimes (aware UTC); calendar values stay plain strings
  (`YYYY-MM-DD`, `HH:MM`, `YYYY-MM`).
* `emp_code` is the employee identity everywhere; MongoDB `_id` is never
  accepted or returned. A record is addressed by `(emp_code, date)`.
* Derived fields (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`)
  are written only by the API and read back verbatim by the analytics.
* Concurrency is decided by the database: unique indexes for punch-in and
  employee creation, and a compare-and-set on `history` for corrections.
* The analytics endpoints use aggregation pipelines (`$lookup`, `$densify`,
  `$setWindowFields`); no documents are loaded into Python to be counted.

## Status

All endpoints in the contract are implemented; nothing is missing. See
`REVIEW.md` for the bugs fixed in the starter code and `DECISIONS.md` for the
five design answers.
