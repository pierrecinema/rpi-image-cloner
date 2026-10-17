import sqlite3
import os
from cryptography.fernet import Fernet

DB_PATH = os.environ.get("DB_PATH", "/app/data/cloner.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            ip TEXT NOT NULL,
            port INTEGER DEFAULT 22,
            user TEXT NOT NULL,
            password TEXT NOT NULL,
            backup_device TEXT DEFAULT '/dev/sda',
            excludes TEXT DEFAULT '',
            schedule_enabled INTEGER DEFAULT 0,
            schedule_cron TEXT DEFAULT '0 3 * * *',
            safe_mode INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pi_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT DEFAULT 'running',
            log TEXT DEFAULT '',
            duration_seconds INTEGER,
            FOREIGN KEY (pi_id) REFERENCES pis(id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()

    row = conn.execute("SELECT value FROM settings WHERE key='fernet_key'").fetchone()
    if not row:
        key = Fernet.generate_key().decode()
        conn.execute("INSERT INTO settings (key, value) VALUES ('fernet_key', ?)", (key,))
        conn.commit()

    conn.close()


def _get_fernet():
    conn = get_db()
    key = conn.execute("SELECT value FROM settings WHERE key='fernet_key'").fetchone()['value']
    conn.close()
    return Fernet(key.encode())


def encrypt_password(password: str) -> str:
    return _get_fernet().encrypt(password.encode()).decode()


def decrypt_password(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()