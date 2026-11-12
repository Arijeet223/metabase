import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect_meta, connect_source, json_dumps

AUDIT_TABLE = "_metabase_audit"


def qident(name: str) -> str:
    """Quote a SQLite identifier safely."""
    return '"' + name.replace('"', '""') + '"'


def normalize_type(native_type: str | None) -> str:
    t = (native_type or "").strip().upper()
    if not t:
        return "UNKNOWN"
    if any(x in t for x in ("INT", "SERIAL")):
        return "INTEGER"
    if any(x in t for x in ("CHAR", "CLOB", "TEXT", "VARCHAR", "STRING")):
        return "STRING"
    if any(x in t for x in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return "DECIMAL"
    if "BLOB" in t or "BINARY" in t:
        return "BINARY"
    if "BOOL" in t:
        return "BOOLEAN"
    if "DATE" in t and "TIME" in t:
        return "DATETIME"
    if "DATE" in t:
        return "DATE"
    return t


def source_tables(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT name, type, sql
        FROM sqlite_master
        WHERE type IN ('table','view')
          AND name NOT LIKE 'sqlite_%'
          AND name <> ?
        ORDER BY name
    """, (AUDIT_TABLE,)).fetchall()
    return [dict(r) for r in rows]


def extract_source_schema(path: str) -> dict[str, Any]:
    conn = connect_source(path)
    try:
        tables = []
        for row in source_tables(conn):
            name = row["name"]
            if row["type"] == "view":
                # Views are catalogued, but trigger auditing is only installed on tables.
                columns = [dict(c) for c in conn.execute(f"PRAGMA table_info({qident(name)})").fetchall()]
                indexes = []
                fks = []
                row_count = None
            else:
                columns = [dict(c) for c in conn.execute(f"PRAGMA table_info({qident(name)})").fetchall()]
                indexes = []
                for idx in conn.execute(f"PRAGMA index_list({qident(name)})").fetchall():
                    idx = dict(idx)
                    idx_cols = [dict(c) for c in conn.execute(f"PRAGMA index_info({qident(idx['name'])})").fetchall()]
                    indexes.append({
                        "name": idx["name"],
                        "unique": bool(idx.get("unique", 0)),
                        "origin": idx.get("origin", "c"),
                        "columns": [c["name"] for c in idx_cols],
                    })
                fks = [dict(fk) for fk in conn.execute(f"PRAGMA foreign_key_list({qident(name)})").fetchall()]
                try:
                    row_count = conn.execute(f"SELECT COUNT(*) FROM {qident(name)}").fetchone()[0]
                except sqlite3.DatabaseError:
                    row_count = None
            tables.append({
                "name": name,
                "type": row["type"].upper(),
                "create_sql": row["sql"],
                "columns": columns,
                "indexes": indexes,
                "foreign_keys": fks,
                "row_count": row_count,
            })
        return {
            "database_path": str(Path(path).resolve()),
            "schema_name": "main",
            "tables": tables,
        }
    finally:
        conn.close()


def schema_signature(schema: dict[str, Any]) -> dict[str, Any]:
    """Stable, comparable schema representation; row counts are deliberately excluded."""
    result = {"schema_name": schema.get("schema_name", "main"), "tables": []}
    for table in schema["tables"]:
        result["tables"].append({
            "name": table["name"],
            "type": table["type"],
            "create_sql": table.get("create_sql"),
            "columns": [
                {
                    "name": c["name"],
                    "type": c.get("type") or "",
                    "notnull": int(c.get("notnull", 0)),
                    "default": c.get("dflt_value"),
                    "pk": int(c.get("pk", 0)),
                } for c in table["columns"]
            ],
            "indexes": sorted(table["indexes"], key=lambda x: x["name"]),
            "foreign_keys": sorted(table["foreign_keys"], key=lambda x: (str(x.get("from")), str(x.get("table")), str(x.get("to")))),
        })
    result["tables"].sort(key=lambda x: x["name"])
    return result


def _json_object_expr(columns: list[str], prefix: str) -> str:
    if not columns:
        return "json('{}')"
    pairs = []
    for col in columns:
        pairs.append("'" + col.replace("'", "''") + "'")
        pairs.append(f"{prefix}.{qident(col)}")
    return "json_object(" + ", ".join(pairs) + ")"


def install_audit_triggers(path: str) -> dict[str, Any]:
    """Install/refresh SQLite row-level triggers for all ordinary source tables."""
    conn = connect_source(path)
    installed = []
    skipped = []
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {qident(AUDIT_TABLE)} (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                record_key TEXT,
                operation TEXT NOT NULL,
                old_values TEXT,
                new_values TEXT,
                changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                changed_by TEXT NOT NULL DEFAULT 'sqlite-trigger'
            )
        """)
        tables = source_tables(conn)
        for table in tables:
            if table["type"] != "table":
                skipped.append(table["name"])
                continue
            table_name = table["name"]
            cols = [dict(c) for c in conn.execute(f"PRAGMA table_info({qident(table_name)})").fetchall()]
            if not cols:
                continue
            pk_cols = [c for c in cols if int(c.get("pk", 0)) > 0]
            if pk_cols:
                # Supports composite keys, too.
                old_key = _json_object_expr([c["name"] for c in pk_cols], "OLD")
                new_key = _json_object_expr([c["name"] for c in pk_cols], "NEW")
            else:
                old_key = "CAST(OLD.rowid AS TEXT)"
                new_key = "CAST(NEW.rowid AS TEXT)"
            col_names = [c["name"] for c in cols]
            old_json = _json_object_expr(col_names, "OLD")
            new_json = _json_object_expr(col_names, "NEW")
            safe_suffix = re.sub(r"[^A-Za-z0-9_]", "_", table_name)
            for op in ("INSERT", "UPDATE", "DELETE"):
                trigger_name = f"_mb_{safe_suffix}_{op.lower()}"
                conn.execute(f"DROP TRIGGER IF EXISTS {qident(trigger_name)}")
                if op == "INSERT":
                    key_expr, old_expr, new_expr = new_key, "NULL", new_json
                elif op == "UPDATE":
                    key_expr, old_expr, new_expr = new_key, old_json, new_json
                else:
                    key_expr, old_expr, new_expr = old_key, old_json, "NULL"
                sql = f"""
                    CREATE TRIGGER {qident(trigger_name)}
                    AFTER {op} ON {qident(table_name)}
                    FOR EACH ROW
                    BEGIN
                        INSERT INTO {qident(AUDIT_TABLE)}
                            (table_name, record_key, operation, old_values, new_values)
                        VALUES (
                            '{table_name.replace("'", "''")}',
                            CAST({key_expr} AS TEXT),
                            '{op}',
                            {old_expr},
                            {new_expr}
                        );
                    END;
                """
                conn.execute(sql)
                installed.append(f"{table_name}:{op}")
        conn.commit()
        return {"installed": installed, "skipped_views": skipped}
    finally:
        conn.close()


def _upsert_source_structure(meta: sqlite3.Connection, source_id: int, schema: dict[str, Any]) -> None:
    meta.execute(
        "INSERT OR IGNORE INTO SCHEMA_META(source_id,schema_name,description) VALUES(?,?,?)",
        (source_id, schema.get("schema_name", "main"), "Extracted SQLite schema"),
    )
    schema_id = meta.execute(
        "SELECT schema_id FROM SCHEMA_META WHERE source_id=? AND schema_name=?",
        (source_id, schema.get("schema_name", "main")),
    ).fetchone()["schema_id"]

    existing_tables = meta.execute("SELECT table_id,table_name FROM TABLE_META WHERE schema_id=?", (schema_id,)).fetchall()
    existing_names = {r["table_name"]: r["table_id"] for r in existing_tables}
    current_names = {t["name"] for t in schema["tables"]}

    # Remove dropped tables; cascades clear dependent catalog metadata.
    for row in existing_tables:
        if row["table_name"] not in current_names:
            meta.execute("DELETE FROM TABLE_META WHERE table_id=?", (row["table_id"],))

    table_ids: dict[str, int] = {}
    for table in schema["tables"]:
        meta.execute("""
            INSERT INTO TABLE_META(schema_id,table_name,table_type,row_count,last_updated)
            VALUES(?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(schema_id,table_name) DO UPDATE SET
                table_type=excluded.table_type,
                row_count=excluded.row_count,
                last_updated=CURRENT_TIMESTAMP
        """, (schema_id, table["name"], table["type"], table.get("row_count")))
        table_id = meta.execute(
            "SELECT table_id FROM TABLE_META WHERE schema_id=? AND table_name=?",
            (schema_id, table["name"]),
        ).fetchone()["table_id"]
        table_ids[table["name"]] = table_id

        old_cols = meta.execute("SELECT column_id,column_name FROM COLUMN_META WHERE table_id=?", (table_id,)).fetchall()
        current_cols = {c["name"] for c in table["columns"]}
        for old in old_cols:
            if old["column_name"] not in current_cols:
                meta.execute("DELETE FROM COLUMN_META WHERE column_id=?", (old["column_id"],))

        col_ids = {}
        for c in table["columns"]:
            native = (c.get("type") or "").strip() or "UNKNOWN"
            normalized = normalize_type(native)
            meta.execute("""
                INSERT INTO COLUMN_META(table_id,column_name,ordinal_position,native_type,normalized_type,
                                        is_primary_key,is_nullable,default_value)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(table_id,column_name) DO UPDATE SET
                    ordinal_position=excluded.ordinal_position,
                    native_type=excluded.native_type,
                    normalized_type=excluded.normalized_type,
                    is_primary_key=excluded.is_primary_key,
                    is_nullable=excluded.is_nullable,
                    default_value=excluded.default_value
            """, (table_id, c["name"], c.get("cid", 0) + 1, native, normalized,
                  1 if int(c.get("pk", 0)) else 0, 0 if int(c.get("notnull", 0)) else 1,
                  None if c.get("dflt_value") is None else str(c.get("dflt_value"))))
            col_id = meta.execute(
                "SELECT column_id FROM COLUMN_META WHERE table_id=? AND column_name=?",
                (table_id, c["name"]),
            ).fetchone()["column_id"]
            col_ids[c["name"]] = col_id

        meta.execute("DELETE FROM INDEX_META WHERE table_id=?", (table_id,))
        for idx in table["indexes"]:
            meta.execute("""
                INSERT INTO INDEX_META(table_id,index_name,index_type,is_unique,columns_json)
                VALUES(?,?,?,?,?)
            """, (table_id, idx["name"], idx.get("origin", "BTREE"), int(idx["unique"]), json_dumps(idx["columns"])))

        # Rebuild FK relationships for this table.
        meta.execute("DELETE FROM REFERENCE WHERE source_table_id=?", (table_id,))
        for fk in table["foreign_keys"]:
            target_name = fk.get("table")
            target_col = fk.get("to")
            source_col = fk.get("from")
            target_id = table_ids.get(target_name)
            # A referenced table may occur later in iteration, so resolve below after all tables.
            source_column_id = col_ids.get(source_col)
            target_column_id = None
            if target_id and target_col:
                found = meta.execute(
                    "SELECT column_id FROM COLUMN_META WHERE table_id=? AND column_name=?",
                    (target_id, target_col),
                ).fetchone()
                target_column_id = found["column_id"] if found else None
            meta.execute("""
                INSERT INTO REFERENCE(source_table_id,source_column_id,target_table_id,target_column_id,
                                      relationship_type,constraint_name)
                VALUES(?,?,?,?,?,?)
            """, (table_id, source_column_id, target_id, target_column_id, "FOREIGN_KEY", str(fk.get("id", ""))))

    # Resolve references again now all table/column ids exist.
    meta.execute("DELETE FROM REFERENCE WHERE source_table_id IN (SELECT table_id FROM TABLE_META WHERE schema_id=?)", (schema_id,))
    for table in schema["tables"]:
        source_table_id = table_ids[table["name"]]
        for fk in table["foreign_keys"]:
            target_name, target_col, source_col = fk.get("table"), fk.get("to"), fk.get("from")
            target_id = table_ids.get(target_name)
            source_col_row = meta.execute(
                "SELECT column_id FROM COLUMN_META WHERE table_id=? AND column_name=?",
                (source_table_id, source_col),
            ).fetchone()
            target_col_row = None
            if target_id and target_col:
                target_col_row = meta.execute(
                    "SELECT column_id FROM COLUMN_META WHERE table_id=? AND column_name=?",
                    (target_id, target_col),
                ).fetchone()
            meta.execute("""
                INSERT INTO REFERENCE(source_table_id,source_column_id,target_table_id,target_column_id,
                                      relationship_type,constraint_name)
                VALUES(?,?,?,?,?,?)
            """, (source_table_id, source_col_row["column_id"] if source_col_row else None,
                  target_id, target_col_row["column_id"] if target_col_row else None,
                  "FOREIGN_KEY", str(fk.get("id", ""))))

        # Latest operational metric per observed table.
        meta.execute("""
            INSERT INTO OPERATIONAL_METRIC(table_id,row_count,last_checked,sync_status)
            VALUES(?,?,CURRENT_TIMESTAMP,'SUCCESS')
        """, (source_table_id, table["row_count"]))


def compare_schemas(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if old is None:
        return changes
    old_tables = {t["name"]: t for t in old.get("tables", [])}
    new_tables = {t["name"]: t for t in new.get("tables", [])}
    for name in sorted(new_tables.keys() - old_tables.keys()):
        changes.append({"table_name": name, "object_name": name, "change_type": "TABLE_ADDED",
                        "old_value": None, "new_value": json_dumps(new_tables[name])})
    for name in sorted(old_tables.keys() - new_tables.keys()):
        changes.append({"table_name": name, "object_name": name, "change_type": "TABLE_DROPPED",
                        "old_value": json_dumps(old_tables[name]), "new_value": None})
    for name in sorted(old_tables.keys() & new_tables.keys()):
        ot, nt = old_tables[name], new_tables[name]
        old_cols = {c["name"]: c for c in ot["columns"]}
        new_cols = {c["name"]: c for c in nt["columns"]}
        for col in sorted(new_cols.keys() - old_cols.keys()):
            changes.append({"table_name": name, "object_name": col, "change_type": "COLUMN_ADDED",
                            "old_value": None, "new_value": json_dumps(new_cols[col])})
        for col in sorted(old_cols.keys() - new_cols.keys()):
            changes.append({"table_name": name, "object_name": col, "change_type": "COLUMN_DROPPED",
                            "old_value": json_dumps(old_cols[col]), "new_value": None})
        for col in sorted(old_cols.keys() & new_cols.keys()):
            if old_cols[col] != new_cols[col]:
                changes.append({"table_name": name, "object_name": col, "change_type": "COLUMN_MODIFIED",
                                "old_value": json_dumps(old_cols[col]), "new_value": json_dumps(new_cols[col])})
        # Catch table type / index / FK changes that do not alter columns.
        if ot.get("type") != nt.get("type"):
            changes.append({"table_name": name, "object_name": name, "change_type": "TABLE_TYPE_CHANGED",
                            "old_value": ot.get("type"), "new_value": nt.get("type")})
        if ot.get("indexes") != nt.get("indexes"):
            changes.append({"table_name": name, "object_name": name, "change_type": "INDEXES_CHANGED",
                            "old_value": json_dumps(ot.get("indexes")), "new_value": json_dumps(nt.get("indexes"))})
        if ot.get("foreign_keys") != nt.get("foreign_keys"):
            changes.append({"table_name": name, "object_name": name, "change_type": "FOREIGN_KEYS_CHANGED",
                            "old_value": json_dumps(ot.get("foreign_keys")), "new_value": json_dumps(nt.get("foreign_keys"))})
    return changes


def sync_source(source_id: int) -> dict[str, Any]:
    meta = connect_meta()
    try:
        source = meta.execute("SELECT * FROM DATA_SOURCE WHERE source_id=?", (source_id,)).fetchone()
        if not source:
            raise ValueError(f"Unknown source_id: {source_id}")
        path = source["database_path"]

        # Install triggers first. Newly installed triggers begin capturing future writes.
        trigger_result = install_audit_triggers(path)
        current_schema = extract_source_schema(path)
        signature = schema_signature(current_schema)
        old_snapshot_row = meta.execute("""
            SELECT schema_definition, schema_version FROM SCHEMA_SNAPSHOT
            WHERE source_id=? ORDER BY schema_version DESC LIMIT 1
        """, (source_id,)).fetchone()
        old_signature = json.loads(old_snapshot_row["schema_definition"]) if old_snapshot_row else None

        # Copy newly recorded source-side audit events. UNIQUE(source_id, source_event_id) prevents duplicates.
        src = connect_source(path)
        try:
            audit_rows = src.execute(f"SELECT * FROM {qident(AUDIT_TABLE)} ORDER BY event_id").fetchall()
            imported = 0
            for event in audit_rows:
                cur = meta.execute("""
                    INSERT OR IGNORE INTO DATA_CHANGE_LOG
                        (source_id,source_event_id,table_name,record_key,operation,old_values,new_values,changed_at,changed_by)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (source_id, event["event_id"], event["table_name"], event["record_key"],
                      event["operation"], event["old_values"], event["new_values"],
                      event["changed_at"], event["changed_by"]))
                imported += cur.rowcount
        finally:
            src.close()

        schema_changes = compare_schemas(old_signature, signature)
        if old_snapshot_row and schema_changes:
            for change in schema_changes:
                meta.execute("""
                    INSERT INTO SCHEMA_CHANGE_LOG
                        (source_id,table_name,object_name,change_type,old_value,new_value)
                    VALUES(?,?,?,?,?,?)
                """, (source_id, change["table_name"], change["object_name"], change["change_type"],
                      change["old_value"], change["new_value"]))

        version = (old_snapshot_row["schema_version"] + 1) if old_snapshot_row else 1
        if not old_snapshot_row or schema_changes:
            meta.execute("""
                INSERT INTO SCHEMA_SNAPSHOT(source_id,schema_version,schema_definition)
                VALUES(?,?,?)
            """, (source_id, version, json_dumps(signature)))

        _upsert_source_structure(meta, source_id, current_schema)
        meta.execute("""
            UPDATE DATA_SOURCE SET status='CONNECTED', last_synced=CURRENT_TIMESTAMP
            WHERE source_id=?
        """, (source_id,))
        meta.commit()
        return {
            "source_id": source_id,
            "source_name": source["source_name"],
            "imported_data_events": imported,
            "schema_changes_detected": len(schema_changes),
            "schema_change_details": schema_changes,
            "schema_version": version if (not old_snapshot_row or schema_changes) else old_snapshot_row["schema_version"],
            "triggers_installed": len(trigger_result["installed"]),
            "trigger_details": trigger_result["installed"],
            "status": "success",
        }
    except Exception:
        meta.rollback()
        try:
            meta.execute("UPDATE DATA_SOURCE SET status='ERROR' WHERE source_id=?", (source_id,))
            meta.commit()
        except Exception:
            pass
        raise
    finally:
        meta.close()
