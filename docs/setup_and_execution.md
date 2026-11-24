# Setup and Execution Guide

## 1. Prerequisites

- Python **3.10+** (developed and tested on 3.13)
- `git`
- Internet access, only for the live Open Library calls in Task 1. Everything else, including the tests, runs offline.
- Optional: Node.js 18+ to re-render Mermaid diagrams locally.

## 2. Install

```bash
git clone https://github.com/bharathmrr/accuknox-ai-security-assessment. accuknox-ai-security-assessment
cd accuknox-ai-security-assessment

python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt      # requests, matplotlib, pytest

# Optional: enables the Qdrant ACL demo and its tests
pip install qdrant-client
```

## 3. Run the tests

```bash
pytest -v
```

- HTTP calls are mocked, so no network is needed.
- `tests/test_qdrant_acl_demo.py` is **skipped** automatically if `qdrant-client` is not installed.

## 4. Run each task

All Assignment 1 commands are run from inside `assignment-1-python/`. Output files go to `assignment-1-python/output/`.

```bash
cd assignment-1-python
```

### Task 1 – Open Library → SQLite

```bash
python api_to_sqlite.py fetch "clean code" --limit 5      # search API, store results
python api_to_sqlite.py fetch "dune" --save-raw data/live_response.json   # also save raw JSON
python api_to_sqlite.py list                              # show all books
python api_to_sqlite.py list --author martin              # filter by author substring
python api_to_sqlite.py get 1
python api_to_sqlite.py add --title "Clean Code" --author "Robert C. Martin" --year 2008
python api_to_sqlite.py update 1 --year 2009
python api_to_sqlite.py delete 1
python api_to_sqlite.py --db /tmp/other.db list           # use another database file
python api_to_sqlite.py -v fetch "python"                 # verbose logging
```

| Exit code | Meaning |
|---|---|
| `0` | Success |
| `1` | Validation, API or not-found error (message on stderr) |
| `2` | Database error |

### Task 2 – Student scores

```bash
python student_scores.py                                   # uses data/student_scores.json
python student_scores.py --source http://localhost:8000/scores   # any URL with the same JSON shape
python student_scores.py --out-dir /tmp/report --no-chart
```

Outputs: `output/student_results.csv` and `output/student_averages.png`.

To try the HTTP path locally, serve the fixture with Python's built-in server:

```bash
python -m http.server 8000 --directory data &
python student_scores.py --source http://localhost:8000/student_scores.json
```

### Task 3 – CSV import

```bash
python csv_to_sqlite.py sample_users.csv                               # skip mode (default)
python csv_to_sqlite.py sample_users.csv --rejects output/rejected_rows.csv
python csv_to_sqlite.py sample_users.csv --db /tmp/strict.db --mode strict   # all-or-nothing
```

- Running the same file twice shows **duplicate-in-DB** detection.
- Strict mode on the sample file rolls back (exit code `3`) because the sample contains invalid rows on purpose.

### Task 4 – Async compliance pipeline

```bash
python advanced_python_example.py
CRM_API_TOKEN=dummy python advanced_python_example.py    # token is read from the environment
```

### Task 5 – Advanced SQL

With Python (no extra install needed):

```bash
python -c "import sqlite3; c=sqlite3.connect('output/compliance.db'); c.executescript(open('advanced_database.sql').read()); print('schema + data loaded')"
```

With the `sqlite3` CLI, if installed:

```bash
sqlite3 output/compliance.db < advanced_database.sql
sqlite3 -header -column output/compliance.db "SELECT * FROM v_agent_compliance;"
```

### Assignment 2 – Qdrant ACL demo (optional)

```bash
cd ..   # repository root
python assignment-2-ai-llm/qdrant_acl_demo.py
```

Expected output:

```text
engineer    -> ['Company holiday calendar', 'Parental leave policy', 'XR-200 error E-4021 fix']
hr_manager  -> ['Company holiday calendar', 'Executive salary bands', 'Parental leave policy']
contractor  -> []
```

## 5. Viewing and re-rendering diagrams

- GitHub renders the `mermaid` code blocks in the Markdown files automatically.
- The `.mmd` sources are in `assignment-2-ai-llm/diagrams/`, and PNG renders are in `docs/images/`.

To regenerate the PNGs:

```bash
npx -p @mermaid-js/mermaid-cli mmdc -i assignment-2-ai-llm/diagrams/chatbot_architecture.mmd \
    -o docs/images/chatbot_architecture.png -s 2 -b white
```

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not connect to Open Library` | Check internet and proxy settings. Tests do not need the network. |
| `ModuleNotFoundError: requests` | Activate the virtualenv and run `pip install -r requirements.txt`. |
| Matplotlib backend errors on a server | Not expected: the script forces the headless `Agg` backend. |
| `database is locked` | Another process holds the SQLite DB. Close it or use `--db` with another path. |
