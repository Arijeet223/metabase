import json
import os
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
META_DB_PATH = Path(os.getenv("METABASE_DB_PATH", str(DATA_DIR / "metabase.db")))


def connect_meta() -> sqlite3.Connection:
    conn = sqlite3.connect(META_DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def connect_source(path: str) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Source database file does not exist: {resolved}")
    conn = sqlite3.connect(str(resolved), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_meta_db() -> None:
    conn = connect_meta()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS DATA_SOURCE (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            db_type TEXT NOT NULL DEFAULT 'sqlite',
            database_path TEXT NOT NULL UNIQUE,
            host TEXT DEFAULT 'local',
            database_name TEXT,
            status TEXT NOT NULL DEFAULT 'REGISTERED',
            last_synced TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS SCHEMA_META (
            schema_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES DATA_SOURCE(source_id) ON DELETE CASCADE,
            schema_name TEXT NOT NULL,
            description TEXT,
            UNIQUE(source_id, schema_name)
        );

        CREATE TABLE IF NOT EXISTS TABLE_META (
            table_id INTEGER PRIMARY KEY AUTOINCREMENT,
            schema_id INTEGER NOT NULL REFERENCES SCHEMA_META(schema_id) ON DELETE CASCADE,
            table_name TEXT NOT NULL,
            table_type TEXT NOT NULL DEFAULT 'TABLE',
            row_count INTEGER DEFAULT 0,
            last_updated TEXT,
            UNIQUE(schema_id, table_name)
        );

        CREATE TABLE IF NOT EXISTS COLUMN_META (
            column_id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id INTEGER NOT NULL REFERENCES TABLE_META(table_id) ON DELETE CASCADE,
            column_name TEXT NOT NULL,
            ordinal_position INTEGER NOT NULL,
            native_type TEXT,
            normalized_type TEXT,
            is_primary_key INTEGER NOT NULL DEFAULT 0,
            is_nullable INTEGER NOT NULL DEFAULT 1,
            default_value TEXT,
            UNIQUE(table_id, column_name)
        );

        CREATE TABLE IF NOT EXISTS INDEX_META (
            index_id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id INTEGER NOT NULL REFERENCES TABLE_META(table_id) ON DELETE CASCADE,
            index_name TEXT NOT NULL,
            index_type TEXT DEFAULT 'BTREE',
            is_unique INTEGER NOT NULL DEFAULT 0,
            columns_json TEXT,
            UNIQUE(table_id, index_name)
        );

        CREATE TABLE IF NOT EXISTS REFERENCE (
            reference_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_table_id INTEGER NOT NULL REFERENCES TABLE_META(table_id) ON DELETE CASCADE,
            source_column_id INTEGER REFERENCES COLUMN_META(column_id) ON DELETE CASCADE,
            target_table_id INTEGER REFERENCES TABLE_META(table_id) ON DELETE CASCADE,
            target_column_id INTEGER REFERENCES COLUMN_META(column_id) ON DELETE CASCADE,
            relationship_type TEXT NOT NULL DEFAULT 'FOREIGN_KEY',
            constraint_name TEXT
        );

        CREATE TABLE IF NOT EXISTS TYPE_MAPPING (
            mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
            db_type TEXT NOT NULL,
            native_type TEXT NOT NULL,
            normalized_type TEXT NOT NULL,
            UNIQUE(db_type, native_type)
        );

        CREATE TABLE IF NOT EXISTS BUSINESS_ANNOTATION (
            annotation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id INTEGER REFERENCES TABLE_META(table_id) ON DELETE CASCADE,
            column_id INTEGER REFERENCES COLUMN_META(column_id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            owner TEXT,
            tags TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (table_id IS NOT NULL OR column_id IS NOT NULL)
        );

        CREATE TABLE IF NOT EXISTS OPERATIONAL_METRIC (
            metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id INTEGER NOT NULL REFERENCES TABLE_META(table_id) ON DELETE CASCADE,
            row_count INTEGER,
            last_checked TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sync_status TEXT NOT NULL DEFAULT 'SUCCESS'
        );

        CREATE TABLE IF NOT EXISTS APP_USER (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT,
            password_hash TEXT,
            role TEXT NOT NULL DEFAULT 'viewer'
        );

        CREATE TABLE IF NOT EXISTS SCHEMA_SNAPSHOT (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES DATA_SOURCE(source_id) ON DELETE CASCADE,
            schema_version INTEGER NOT NULL,
            captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            schema_definition TEXT NOT NULL,
            UNIQUE(source_id, schema_version)
        );

        CREATE TABLE IF NOT EXISTS SCHEMA_CHANGE_LOG (
            change_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES DATA_SOURCE(source_id) ON DELETE CASCADE,
            table_name TEXT,
            object_name TEXT NOT NULL,
            change_type TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS DATA_CHANGE_LOG (
            data_change_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL REFERENCES DATA_SOURCE(source_id) ON DELETE CASCADE,
            source_event_id INTEGER NOT NULL,
            table_name TEXT NOT NULL,
            record_key TEXT,
            operation TEXT NOT NULL CHECK(operation IN ('INSERT','UPDATE','DELETE')),
            old_values TEXT,
            new_values TEXT,
            changed_at TEXT NOT NULL,
            changed_by TEXT DEFAULT 'database-trigger',
            UNIQUE(source_id, source_event_id)
        );

        CREATE INDEX IF NOT EXISTS idx_data_change_source_time
            ON DATA_CHANGE_LOG(source_id, changed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_schema_change_source_time
            ON SCHEMA_CHANGE_LOG(source_id, changed_at DESC);
        """)
        seed_type_mappings(conn)
        conn.commit()
    finally:
        conn.close()


def seed_type_mappings(conn: sqlite3.Connection) -> None:
    mappings = [
        ("sqlite", "INTEGER", "INTEGER"),
        ("sqlite", "INT", "INTEGER"),
        ("sqlite", "REAL", "DECIMAL"),
        ("sqlite", "NUMERIC", "DECIMAL"),
        ("sqlite", "DECIMAL", "DECIMAL"),
        ("sqlite", "TEXT", "STRING"),
        ("sqlite", "VARCHAR", "STRING"),
        ("sqlite", "CHAR", "STRING"),
        ("sqlite", "BLOB", "BINARY"),
        ("sqlite", "BOOLEAN", "BOOLEAN"),
        ("sqlite", "DATE", "DATE"),
        ("sqlite", "DATETIME", "DATETIME"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO TYPE_MAPPING(db_type,native_type,normalized_type) VALUES(?,?,?)",
        mappings,
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
