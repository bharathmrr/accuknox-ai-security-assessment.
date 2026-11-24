# Assignment 1 – Python and Database Programming

| Task | File(s) | Run |
|---|---|---|
| 1. API → SQLite (CRUD) | `api_to_sqlite.py`, `data/sample_openlibrary_response.json` | `python api_to_sqlite.py fetch "clean code" --limit 5` |
| 2. Student scores + chart | `student_scores.py`, `data/student_scores.json` | `python student_scores.py` |
| 3. CSV → SQLite importer | `csv_to_sqlite.py`, `sample_users.csv` | `python csv_to_sqlite.py sample_users.csv` |
| 4. Complex Python | `advanced_python_example.py` | `python advanced_python_example.py` |
| 5. Complex SQL | `advanced_database.sql` | see below |

Generated files (databases, CSV, PNG) are written to `output/`, which is git-ignored. A sample chart is committed at `../docs/images/student_averages.png`.

---

## Task 1 – Open Library → SQLite

**API used:** Open Library Search API, `GET https://openlibrary.org/search.json?q=<query>&fields=key,title,author_name,first_publish_year&limit=<n>`. It is public and needs no key ([docs](https://openlibrary.org/dev/docs/api/search)).

**Schema**

```sql
CREATE TABLE books (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT    NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 500),
    author            TEXT    NOT NULL CHECK (length(trim(author)) BETWEEN 1 AND 500),
    publication_year  INTEGER CHECK (publication_year IS NULL OR publication_year BETWEEN 1000 AND 2100),
    source            TEXT    NOT NULL DEFAULT 'manual' CHECK (source IN ('openlibrary','manual')),
    source_key        TEXT,                       -- Open Library work key, e.g. /works/OL…W
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (title, author, publication_year)
);
CREATE UNIQUE INDEX ux_books_source_key ON books(source_key) WHERE source_key IS NOT NULL;
CREATE INDEX ix_books_author ON books(author);
```

**Robustness and security**

- `(connect, read)` timeout of `(5s, 10s)`. Up to three attempts with exponential backoff, only for timeouts, connection errors, HTTP 429 and HTTP 5xx. Other 4xx errors fail fast.
- The response shape is validated. Bad documents are skipped and counted; one bad document never aborts the batch.
- Inputs are validated: query length, `1 ≤ limit ≤ 100`, non-empty title/author, year range.
- Every SQL statement uses `?` placeholders. `UPDATE` only touches a whitelisted set of columns. `LIKE` wildcards in user filters are escaped.
- Bulk inserts run in a single transaction (`with conn:`).

**Sample API response:** `data/sample_openlibrary_response.json`. This is a **representative** response that follows the documented structure. It is not a live capture, because the environment that built this repo had no outbound access to openlibrary.org. To capture a real one, run `fetch … --save-raw data/live_response.json`.

**Expected terminal output** (actual output from a manual run):

```text
$ python api_to_sqlite.py add --title "Clean Code" --author "Robert C. Martin" --year 2008
Created book 1.
  ID  Year  Title                                       Author
----------------------------------------------------------------------------------
   1  2008  Clean Code                                  Robert C. Martin

$ python api_to_sqlite.py add --title "Bad" --author "X" --year 3000
Error: Publication year must be between 1000 and 2027.

$ python api_to_sqlite.py fetch "clean code" --limit 3      # with no network access
WARNING api_to_sqlite: Attempt 1/3 failed (Could not connect to Open Library (network error).); retrying in 0.5s
WARNING api_to_sqlite: Attempt 2/3 failed (Could not connect to Open Library (network error).); retrying in 1.0s
Error: Could not connect to Open Library (network error).
```

With network access, `fetch` prints `Fetched N documents for 'clean code': X inserted, Y duplicates ignored, Z invalid skipped.` and then the table. That path is covered by mocked unit tests in `tests/test_api_to_sqlite.py`.

---

## Task 2 – Student score analysis

There is no reliable public student-score API, so the program reads a **local mock API fixture** (`data/student_scores.json`). Passing `--source https://…` uses the same code path over HTTP with a timeout. No real endpoint is invented.

The fixture contains synthetic students and deliberately includes a `null` score, a missing subject, `"absent"`, a value above 100, a negative value, a numeric string `"84"` and an empty name.

**Rules**

- Missing or invalid scores are excluded from that student's average and listed under `issues`.
- Students with no name, or with no valid score, are excluded from the ranking.
- The overall average is the mean of **all valid individual scores**, not the mean of student averages, so students with fewer valid scores are not over-weighted.
- Ties for highest or lowest performer are all reported.

**Actual output**

```text
S002  Diya Patel         avg= 94.00  valid=5
S006  Meera Nair         avg= 89.75  valid=4  issues: Programming: out of range (105)
...
Overall average (all valid scores): 77.65
Highest performer(s): Diya Patel (94.0)
Lowest performer(s): Karthik Reddy (47.2)
Excluded from ranking: S009 (missing name), S010 (Mathematics: out of range (-5); ...)
```

Outputs: `output/student_results.csv` and `output/student_averages.png` (committed copy: `../docs/images/student_averages.png`).

---

## Task 3 – CSV → SQLite importer

- **Schema:** `users(id INTEGER PRIMARY KEY CHECK(id>0), name TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE, phone TEXT, imported_at)`, plus an `import_runs` audit table.
- **Validation:**
  - Email uses a regex with no consecutive or leading dots and requires a TLD.
  - Phone must have 7–15 digits (the E.164 maximum) and only `+ ( ) - space`.
  - ID must be a positive integer. Required fields must be present.
- **Duplicates:** detected within the file and against rows already in the DB, by ID **and** by case-insensitive email.
- **Transactions:** the whole import runs in one `BEGIN IMMEDIATE … COMMIT`.
  - `--mode skip` (default) imports the valid rows and reports the rest.
  - `--mode strict` rolls back everything on the first bad row, and the failed run is still written to `import_runs`.
- **SQL injection:** row 9 of `sample_users.csv` has the name `Robert'); DROP TABLE users;--`. Because inserts are parameterised, it is stored as plain text and the table survives. A test asserts this.

**Actual output**

```text
  rejected line 5: invalid email 'rahul.gupta@example'
  rejected line 6: missing name
  rejected line 8: duplicate email within file
  rejected line 9: duplicate id within file
  rejected line 10: invalid email 'lakshmi..pillai@example.com'
  rejected line 12: id 'abc' is not a positive integer
  rejected line 13: phone '12ab34' contains invalid characters
Import statistics
-----------------
Rows read            : 14
Inserted             : 7
Rejected             : 7
  missing fields     : 1
  invalid email      : 2
  other invalid data : 2
  duplicate in file  : 2
  duplicate in DB    : 0
Transaction status   : committed
```

(Line numbers are physical CSV lines, and the header is line 1.)

---

## Task 4 – Most complex Python code

> **Honesty note:** `advanced_python_example.py` is a **demonstration written for this assessment**. It is modelled on the problem domain of the GhostAgent / DataRipple project (see Assignment 3) but is **not** code taken from that project. All telephony, LLM and CRM integrations are in-process mocks so it runs offline and deterministically (`seed=7`).

**What it does:** it receives webhooks from three telephony providers, each with a different payload shape. It then:

1. normalises and validates each payload,
2. extracts intent, email and requirements using a mock "LLM",
3. applies intent-specific compliance rules (required documents, prohibited sales phrases),
4. upserts the CRM idempotently, and
5. routes failures to a dead-letter queue.

**Why it is complex**

1. **Async concurrency with back-pressure.** `asyncio.gather` runs every event, but an `asyncio.Semaphore` caps in-flight work and `asyncio.wait_for` bounds each call. One slow call cannot stall the batch, and the pipeline cannot exceed downstream rate limits.
2. **Layered failure handling.**
   - Transient errors are retried with exponential backoff and *full jitter*, which avoids retry storms.
   - A **circuit breaker** stops hammering a failing CRM.
   - Validation errors are **not** retried. They go straight to the DLQ.
   - Every failure class maps to an explicit outcome, so no exception escapes a task.
3. **Idempotency under concurrency.** Telephony providers redeliver webhooks. A lock-guarded set of SHA-256 idempotency keys prevents double CRM writes. The key is *released* if processing fails, so a redelivery can still succeed. That is a subtle correctness point.
4. **Adapter pattern for heterogeneous APIs.** Three payload schemas map onto one frozen, validated `CallRecord` (with an E.164 phone check and transcript checks), which keeps the business logic provider-agnostic.
5. **Business rules as data.** `REQUIRED_DOCUMENTS` maps intents to document sets. Low-confidence extractions and script violations are forced to **human review** instead of being auto-closed, which is a deliberate human-in-the-loop control.
6. **Security hygiene.** PII (emails, phone numbers, Indian PAN) is masked before logging. The CRM token is read from the environment.

Actual output:

```text
Processed 67 webhook events in 0.56s (concurrency=8)
  compliant          6
  duplicate_skipped  5
  human_review       22
  non_compliant      32
  rejected_invalid   2
CRM records stored: 60  |  circuit-breaker trips: 0
Dead-letter queue (2):
  CAbad1     validation: transcript is empty or too short
  ?          validation: unsupported provider 'zoom'
PII masking example: Hi, I need a home loan of 40 lakhs, my email is [EMAIL]. What EMI options exist? ...PAN [PAN].
```

(Timing varies by machine.)

---

## Task 5 – Most complex database code

`advanced_database.sql` is a **demonstration schema** for contact-centre compliance tracking, written in the SQLite dialect so it can be executed and verified here. `tests/test_advanced_database.py` runs it.

Run it:

```bash
python -c "import sqlite3; c=sqlite3.connect('output/compliance.db'); c.executescript(open('advanced_database.sql').read()); print('ok')"
```

| Requirement | Where |
|---|---|
| Relational design | 9 tables in 3NF. Two many-to-many junction tables (`intent_required_documents`, `call_documents`) turn business rules into data |
| Primary / foreign keys | Every table. FKs use deliberate `ON DELETE CASCADE` vs `RESTRICT` (calls cannot orphan agents) |
| Constraints | `CHECK` (enums, E.164 `GLOB`, ranges, date validity), `UNIQUE (provider, provider_call_ref)` for webhook idempotency, `COLLATE NOCASE` emails |
| Indexing | FK indexes, a composite `(agent_id, started_at)`, a **partial** index on open high/critical findings, and a partial covering index for the retry worker |
| Transactions | Seed load in `BEGIN…COMMIT`. `BEGIN IMMEDIATE` resolves a finding, records the document and conditionally flips call status atomically |
| Joins and aggregation | `NOT EXISTS` anti-join for missing documents, CTE-based agent scorecard with `RANK() OVER (PARTITION BY team)`, running totals and moving averages with window frames, correlated subquery for the latest retry, `GROUP BY … HAVING` |
| Optimisation | `EXPLAIN QUERY PLAN` shows `SEARCH … USING COVERING INDEX` for both target queries. `ANALYZE` refreshes statistics. A trigger keeps derived status consistent |

**Why it is complex:**

- Correctness is enforced in the database itself (constraints, a trigger and atomic transactions), not only in application code.
- The indexes were designed from the queries backwards, and the plans were checked.
- The queries combine anti-joins, CTEs and window functions. NULL-safe choices are deliberate: `NOT EXISTS` instead of `NOT IN`, and `COALESCE` on aggregates over outer joins.

**PostgreSQL differences:**

- Use `GENERATED ALWAYS AS IDENTITY`, `timestamptz` and native `ENUM` or `CHECK` types.
- Use `SELECT … FOR UPDATE` inside the transaction.
- Use `CREATE INDEX CONCURRENTLY` on live tables.
- Use `EXPLAIN (ANALYZE, BUFFERS)` for plans.
