import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import DATA_DIR, connect_meta, connect_source, init_meta_db
from .metadata import extract_source_schema, install_audit_triggers, sync_source, qident

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
POLL_SECONDS = int(os.getenv("METABASE_POLL_SECONDS", "15"))

app = FastAPI(title="MetaBase", description="Automated Metadata Management System")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_sync_thread_started = False
_stop_event = threading.Event()


class SourceCreate(BaseModel):
    source_name: str = Field(min_length=1, max_length=100)
    db_type: str = "sqlite"
    database_path: str = Field(min_length=1)


class AnnotationCreate(BaseModel):
    table_id: int | None = None
    column_id: int | None = None
    description: str = Field(min_length=1, max_length=2000)
    owner: str | None = None
    tags: str | None = None


def seed_demo_database() -> Path:
    path = DATA_DIR / "demo_hr.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS Departments (
            department_id INTEGER PRIMARY KEY,
            department_name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS Employees (
            employee_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            salary REAL NOT NULL,
            department_id INTEGER,
            FOREIGN KEY(department_id) REFERENCES Departments(department_id)
        );
        CREATE TABLE IF NOT EXISTS Projects (
            project_id INTEGER PRIMARY KEY,
            project_name TEXT NOT NULL,
            department_id INTEGER,
            FOREIGN KEY(department_id) REFERENCES Departments(department_id)
        );
        INSERT OR IGNORE INTO Departments(department_id,department_name) VALUES
            (1,'Engineering'),(2,'Human Resources'),(3,'Finance');
        INSERT OR IGNORE INTO Employees(employee_id,name,salary,department_id) VALUES
            (101,'Rahul Sharma',40000,1),(102,'Priya Singh',50000,2),(103,'Aman Verma',45000,1);
        INSERT OR IGNORE INTO Projects(project_id,project_name,department_id) VALUES
            (1,'Internal Portal',1),(2,'Hiring Dashboard',2);
        """)
        conn.commit()
    finally:
        conn.close()
    return path


def ensure_demo_source() -> None:
    init_meta_db()
    demo_path = seed_demo_database()
    conn = connect_meta()
    try:
        existing = conn.execute("SELECT source_id FROM DATA_SOURCE WHERE database_path=?", (str(demo_path.resolve()),)).fetchone()
        if not existing:
            conn.execute("""
                INSERT INTO DATA_SOURCE(source_name,db_type,database_path,host,database_name,status)
                VALUES(?,?,?,?,?,'REGISTERED')
            """, ("Demo HR Database", "sqlite", str(demo_path.resolve()), "local", "demo_hr.db"))
            conn.commit()
            source_id = conn.execute("SELECT source_id FROM DATA_SOURCE WHERE database_path=?", (str(demo_path.resolve()),)).fetchone()["source_id"]
        else:
            source_id = existing["source_id"]
    finally:
        conn.close()
    # Initial sync captures a baseline and installs triggers. No schema-change logs are emitted for baseline.
    sync_source(source_id)


def background_sync_loop() -> None:
    while not _stop_event.wait(max(POLL_SECONDS, 5)):
        conn = connect_meta()
        try:
            ids = [r["source_id"] for r in conn.execute("SELECT source_id FROM DATA_SOURCE").fetchall()]
        finally:
            conn.close()
        for source_id in ids:
            try:
                sync_source(source_id)
            except Exception as exc:
                print(f"[MetaBase] background sync failed for source {source_id}: {exc}")


@app.on_event("startup")
def startup() -> None:
    global _sync_thread_started
    ensure_demo_source()
    if not _sync_thread_started:
        thread = threading.Thread(target=background_sync_loop, daemon=True, name="metabase-sync")
        thread.start()
        _sync_thread_started = True


@app.on_event("shutdown")
def shutdown() -> None:
    _stop_event.set()


@app.get("/", response_class=HTMLResponse)
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "poll_seconds": POLL_SECONDS}


@app.get("/api/sources")
def get_sources() -> list[dict[str, Any]]:
    conn = connect_meta()
    try:
        rows = conn.execute("""
            SELECT ds.*,
                (SELECT COUNT(*) FROM SCHEMA_META sm WHERE sm.source_id=ds.source_id) AS schema_count,
                (SELECT COUNT(*) FROM TABLE_META tm JOIN SCHEMA_META sm ON sm.schema_id=tm.schema_id WHERE sm.source_id=ds.source_id) AS table_count,
                (SELECT COUNT(*) FROM DATA_CHANGE_LOG d WHERE d.source_id=ds.source_id) AS data_change_count
            FROM DATA_SOURCE ds ORDER BY ds.source_id
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/sources")
def create_source(body: SourceCreate) -> dict[str, Any]:
    if body.db_type.lower() != "sqlite":
        raise HTTPException(400, "This MVP currently supports SQLite sources only.")
    path = str(Path(body.database_path).expanduser().resolve())
    if not Path(path).exists():
        raise HTTPException(400, f"Database file does not exist: {path}")
    # Validate that it is a readable SQLite database.
    try:
        test = sqlite3.connect(path)
        test.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        test.close()
    except sqlite3.Error as exc:
        raise HTTPException(400, f"Could not open SQLite database: {exc}")
    conn = connect_meta()
    try:
        conn.execute("""
            INSERT INTO DATA_SOURCE(source_name,db_type,database_path,host,database_name,status)
            VALUES(?,?,?,?,?,'REGISTERED')
        """, (body.source_name.strip(), "sqlite", path, "local", Path(path).name))
        conn.commit()
        source_id = conn.execute("SELECT source_id FROM DATA_SOURCE WHERE database_path=?", (path,)).fetchone()["source_id"]
    except sqlite3.IntegrityError:
        raise HTTPException(409, "That database file is already registered.")
    finally:
        conn.close()
    try:
        result = sync_source(source_id)
    except Exception as exc:
        raise HTTPException(500, f"Source was registered but sync failed: {exc}")
    return result


@app.post("/api/sync/{source_id}")
def sync_one(source_id: int) -> dict[str, Any]:
    try:
        return sync_source(source_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Synchronization failed: {exc}")


@app.post("/api/sync-all")
def sync_all() -> dict[str, Any]:
    conn = connect_meta()
    try:
        ids = [r["source_id"] for r in conn.execute("SELECT source_id FROM DATA_SOURCE ORDER BY source_id").fetchall()]
    finally:
        conn.close()
    results = []
    for source_id in ids:
        try:
            results.append(sync_source(source_id))
        except Exception as exc:
            results.append({"source_id": source_id, "status": "error", "error": str(exc)})
    return {"results": results}


@app.get("/api/catalog")
def catalog(q: str | None = None) -> dict[str, Any]:
    conn = connect_meta()
    try:
        sql = """
        SELECT ds.source_id, ds.source_name, sm.schema_name, tm.table_id, tm.table_name,
               tm.table_type, tm.row_count, tm.last_updated
        FROM TABLE_META tm
        JOIN SCHEMA_META sm ON sm.schema_id=tm.schema_id
        JOIN DATA_SOURCE ds ON ds.source_id=sm.source_id
        """
        args: list[Any] = []
        if q:
            sql += """ WHERE ds.source_name LIKE ? OR sm.schema_name LIKE ? OR tm.table_name LIKE ?
                       OR EXISTS (SELECT 1 FROM COLUMN_META c WHERE c.table_id=tm.table_id AND c.column_name LIKE ?)"""
            args = [f"%{q}%"] * 4
        sql += " ORDER BY ds.source_name, tm.table_name"
        tables = [dict(r) for r in conn.execute(sql, args).fetchall()]
        for table in tables:
            table["columns"] = [dict(c) for c in conn.execute("""
                SELECT column_id,column_name,ordinal_position,native_type,normalized_type,
                       is_primary_key,is_nullable,default_value
                FROM COLUMN_META WHERE table_id=? ORDER BY ordinal_position
            """, (table["table_id"],)).fetchall()]
            table["indexes"] = [dict(i) for i in conn.execute("""
                SELECT index_name,index_type,is_unique,columns_json FROM INDEX_META WHERE table_id=?
            """, (table["table_id"],)).fetchall()]
            table["relationships"] = [dict(r) for r in conn.execute("""
                SELECT r.relationship_type, st.table_name AS source_table, sc.column_name AS source_column,
                       tt.table_name AS target_table, tc.column_name AS target_column
                FROM REFERENCE r
                JOIN TABLE_META st ON st.table_id=r.source_table_id
                LEFT JOIN COLUMN_META sc ON sc.column_id=r.source_column_id
                LEFT JOIN TABLE_META tt ON tt.table_id=r.target_table_id
                LEFT JOIN COLUMN_META tc ON tc.column_id=r.target_column_id
                WHERE r.source_table_id=? OR r.target_table_id=?
            """, (table["table_id"], table["table_id"])).fetchall()]
        return {"tables": tables, "count": len(tables)}
    finally:
        conn.close()


@app.get("/api/logs")
def get_logs(limit: int = Query(100, ge=1, le=1000), kind: str = "all") -> dict[str, Any]:
    conn = connect_meta()
    try:
        data = []
        schema = []
        if kind in ("all", "data"):
            data = [dict(r) for r in conn.execute("""
                SELECT d.data_change_id AS id, ds.source_name, d.table_name, d.record_key,
                       d.operation AS change_type, d.old_values, d.new_values, d.changed_at,
                       d.changed_by, 'DATA' AS log_kind
                FROM DATA_CHANGE_LOG d JOIN DATA_SOURCE ds ON ds.source_id=d.source_id
                ORDER BY d.data_change_id DESC LIMIT ?
            """, (limit,)).fetchall()]
        if kind in ("all", "schema"):
            schema = [dict(r) for r in conn.execute("""
                SELECT s.change_id AS id, ds.source_name, s.table_name, s.object_name,
                       s.change_type, s.old_value AS old_values, s.new_value AS new_values,
                       s.changed_at, 'schema-diff' AS changed_by, 'SCHEMA' AS log_kind
                FROM SCHEMA_CHANGE_LOG s JOIN DATA_SOURCE ds ON ds.source_id=s.source_id
                ORDER BY s.change_id DESC LIMIT ?
            """, (limit,)).fetchall()]
        merged = sorted(data + schema, key=lambda x: (x.get("changed_at") or "", x["id"]), reverse=True)[:limit]
        return {"logs": merged, "count": len(merged)}
    finally:
        conn.close()


@app.get("/api/snapshots")
def get_snapshots() -> dict[str, Any]:
    conn = connect_meta()
    try:
        rows = conn.execute("""
            SELECT s.snapshot_id,s.source_id,ds.source_name,s.schema_version,s.captured_at,s.schema_definition
            FROM SCHEMA_SNAPSHOT s JOIN DATA_SOURCE ds ON ds.source_id=s.source_id
            ORDER BY s.snapshot_id DESC LIMIT 100
        """).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            schema = json.loads(item.pop("schema_definition"))
            item["tables"] = len(schema.get("tables", []))
            item["table_names"] = [t["name"] for t in schema.get("tables", [])]
            result.append(item)
        return {"snapshots": result, "count": len(result)}
    finally:
        conn.close()


@app.get("/api/annotations")
def get_annotations() -> dict[str, Any]:
    conn = connect_meta()
    try:
        rows = conn.execute("""
            SELECT a.annotation_id,a.description,a.owner,a.tags,a.updated_at,
                   a.table_id,t.table_name,a.column_id,c.column_name
            FROM BUSINESS_ANNOTATION a
            LEFT JOIN TABLE_META t ON t.table_id=a.table_id
            LEFT JOIN COLUMN_META c ON c.column_id=a.column_id
            ORDER BY a.annotation_id DESC
        """).fetchall()
        return {"annotations": [dict(r) for r in rows], "count": len(rows)}
    finally:
        conn.close()


@app.post("/api/annotations")
def create_annotation(body: AnnotationCreate) -> dict[str, Any]:
    if body.table_id is None and body.column_id is None:
        raise HTTPException(400, "Provide table_id or column_id.")
    conn = connect_meta()
    try:
        if body.table_id is not None and not conn.execute("SELECT 1 FROM TABLE_META WHERE table_id=?", (body.table_id,)).fetchone():
            raise HTTPException(404, "Table not found.")
        if body.column_id is not None and not conn.execute("SELECT 1 FROM COLUMN_META WHERE column_id=?", (body.column_id,)).fetchone():
            raise HTTPException(404, "Column not found.")
        cur = conn.execute("""
            INSERT INTO BUSINESS_ANNOTATION(table_id,column_id,description,owner,tags)
            VALUES(?,?,?,?,?)
        """, (body.table_id, body.column_id, body.description, body.owner, body.tags))
        conn.commit()
        return {"annotation_id": cur.lastrowid, "status": "created"}
    finally:
        conn.close()


@app.post("/api/demo/test-update")
def demo_test_update() -> dict[str, Any]:
    conn = connect_meta()
    try:
        row = conn.execute("""
            SELECT source_id,database_path,source_name FROM DATA_SOURCE
            WHERE database_path LIKE '%demo_hr.db' ORDER BY source_id LIMIT 1
        """).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Demo HR database is not registered.")
    path = row["database_path"]
    src = connect_source(path)
    try:
        before = src.execute("SELECT employee_id,name,salary FROM Employees WHERE employee_id=101").fetchone()
        if not before:
            raise HTTPException(500, "Demo employee 101 was not found.")
        old_salary = float(before["salary"])
        new_salary = old_salary + 1000.0
        src.execute("UPDATE Employees SET salary=? WHERE employee_id=101", (new_salary,))
        src.commit()
    finally:
        src.close()
    result = sync_source(row["source_id"])
    conn = connect_meta()
    try:
        latest = conn.execute("""
            SELECT * FROM DATA_CHANGE_LOG
            WHERE source_id=? ORDER BY data_change_id DESC LIMIT 1
        """, (row["source_id"],)).fetchone()
    finally:
        conn.close()
    return {
        "status": "success",
        "message": "Test UPDATE executed in the source database and captured by the SQLite trigger.",
        "employee": "Rahul Sharma",
        "old_salary": old_salary,
        "new_salary": new_salary,
        "imported_data_events": result["imported_data_events"],
        "latest_log": dict(latest) if latest else None,
    }


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    conn = connect_meta()
    try:
        def count(table: str) -> int:
            return conn.execute(f"SELECT COUNT(*) FROM {qident(table)}").fetchone()[0]
        return {
            "sources": count("DATA_SOURCE"),
            "schemas": count("SCHEMA_META"),
            "tables": count("TABLE_META"),
            "columns": count("COLUMN_META"),
            "data_changes": count("DATA_CHANGE_LOG"),
            "schema_changes": count("SCHEMA_CHANGE_LOG"),
            "snapshots": count("SCHEMA_SNAPSHOT"),
            "annotations": count("BUSINESS_ANNOTATION"),
        }
    finally:
        conn.close()


def run() -> None:
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()
