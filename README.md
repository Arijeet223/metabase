# MetaBase — Automated Metadata Management System

MetaBase is a lightweight DBMS course project that catalogs source databases and tracks both:
- **Data changes**: INSERT, UPDATE, DELETE, captured by SQLite triggers.
- **Schema changes**: detected by comparing schema snapshots during synchronization.

The initial version intentionally uses SQLite so it can be installed and demonstrated easily on Windows. The design has 13 core metadata entities, including the added `DATA_CHANGE_LOG` entity.

## Features
- Register SQLite source databases by file path.
- Extract schemas, tables, columns, primary keys, foreign keys, and indexes.
- Install source-side audit triggers for INSERT / UPDATE / DELETE.
- Import audit events into the centralized MetaBase database with deduplication.
- Periodically/ manually synchronize schema metadata and detect added/removed/modified tables and columns.
- Keep schema snapshots and a searchable catalog.
- Add business annotations to tables or columns.
- Dashboard, catalog explorer, change log, and schema snapshots in a small web UI.
- A demo HR database is created automatically on first run.
- **Test Update** button changes a demo employee's salary, then syncs and displays the captured log.

## Requirements
- Python 3.10+ recommended.
- Windows, macOS, or Linux.

## Run on Windows
1. Extract the ZIP.
2. Open PowerShell in the extracted `MetaBase_Project` folder.
3. Create and activate a virtual environment:
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
4. Install dependencies:
   ```powershell
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
5. Start the app:
   ```powershell
   python run.py
   ```
6. Open `http://127.0.0.1:8000`.

If PowerShell blocks activation, run `.venv\Scripts\python.exe -m pip install -r requirements.txt` and then `.venv\Scripts\python.exe run.py`.

## Run on macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

## Demonstrate that update logging works
1. Open the dashboard.
2. Click **Test Update**. It changes the demo employee's salary in the source database.
3. Open **Change Log**. A DATA / UPDATE event should appear with the old and new values.
4. Open **Catalog** and **Schema History** to inspect extracted metadata and snapshots.
5. Click **Sync Now** after changing the source database externally. New trigger events are imported and schema differences are logged.

You can also use a SQLite editor or Python to update `data/demo_hr.db`, then click **Sync Now** in the UI.

## API endpoints
- `GET /api/health`
- `GET /api/sources`
- `POST /api/sources` — JSON: `{"source_name":"My DB","db_type":"sqlite","database_path":"C:/path/to/source.db"}`
- `POST /api/sync/{source_id}`
- `POST /api/sync-all`
- `GET /api/catalog`
- `GET /api/logs?limit=100`
- `GET /api/snapshots`
- `GET /api/annotations`
- `POST /api/annotations` — JSON: `{"table_id":1,"column_id":null,"description":"...","owner":"...","tags":"..."}`
- `POST /api/demo/test-update`

## The 13 core entities
1. `DATA_SOURCE`
2. `SCHEMA_META`
3. `TABLE_META`
4. `COLUMN_META`
5. `INDEX_META`
6. `REFERENCE`
7. `TYPE_MAPPING`
8. `BUSINESS_ANNOTATION`
9. `OPERATIONAL_METRIC`
10. `APP_USER`
11. `SCHEMA_SNAPSHOT`
12. `SCHEMA_CHANGE_LOG`
13. `DATA_CHANGE_LOG`

## Important design notes
- Source data stays in the source database; MetaBase stores catalog metadata and audit history.
- SQLite triggers write to a local `_metabase_audit` table in the same source database. Synchronization copies those events into MetaBase.
- Data changes are recorded immediately in the source-side audit table. The central dashboard reflects them after synchronization (manual sync or automatic background polling).
- Schema changes are detected by snapshot comparison, so schema detection is periodic rather than instant.
- For the course-project demo, use trusted local SQLite files only. Do not register arbitrary database files from untrusted users.
- This version uses SQLite as its supported source adapter. PostgreSQL/MySQL adapters can be added later.
- Audit values can contain sensitive data. This demo is intended for local coursework; add access controls and retention policies before any real deployment.
