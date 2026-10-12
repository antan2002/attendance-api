# DECISIONS.md

**1. Indexes.** `employees.emp_code` unique drives employee lookups and turns a
duplicate create into a 409. `employees (department, emp_code)` serves the
list filter and sort and the department joins. `employees (joined_on)` supports
the headcount filter when no department is given. `attendance_logs (emp_code,
date)` unique is the natural key: it guarantees one record per day, decides the
punch-in race, and backs per-employee month scans and lookups.
`attendance_logs (date desc, emp_code)` backs the global list sort, the month
ranges for the leaderboard and trend, and keeps every explain plan on an
IXSCAN. I considered a `status` index and rejected it: status has very low
selectivity and the date index already provides the required sort.

**2. Punch-in race.** There is no check-then-insert. The request inserts
directly; the unique `(emp_code, date)` index lets exactly one insert commit and
the other raises `DuplicateKeyError`, which I map to 409.

**3. Ties at the cutoff.** Ranking uses `$setWindowFields` `$rank`, i.e.
standard competition ranking. `limit` is applied *after* ranking
(`rank <= limit`), so every employee tied at a rank inside the cutoff is
returned and the response may hold more than `limit` rows; ties are ordered by
`emp_code`.

**4. Headcount with zero logs.** The department summary starts from `employees`
(filtered to `joined_on <= month end`) and groups by department; headcount is
that group's count, so employees with no logs are still counted. Attendance
logs are joined afterwards, so a missing log never removes anyone.

**5. At 100x.** I would stop recomputing per request: write a daily
per-department rollup on every punch and read that for the summary and trend,
then shard or time-partition `attendance_logs` and cache the analytics responses.
