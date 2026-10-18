"""Stage-one binding and switch audit; task queue arrives in stage two."""

import json
import sqlite3
import time


class BindingStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS binding (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                backend_id TEXT NOT NULL, instance_id TEXT NOT NULL,
                session_id TEXT NOT NULL, generation INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('intent', 'active', 'revoked'))
            );
            CREATE TABLE IF NOT EXISTS switch_audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                source_instance TEXT, target_instance TEXT, generation INTEGER,
                state TEXT NOT NULL, detail TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(binding)")}
        for name, kind in (("pid", "INTEGER"), ("process_start", "TEXT")):
            if name not in columns:
                self.db.execute("ALTER TABLE binding ADD COLUMN " + name + " " + kind)
        self.db.commit()

    def current(self):
        row = self.db.execute("SELECT backend_id,instance_id,session_id,generation,state,pid,process_start FROM binding WHERE singleton=1").fetchone()
        return dict(zip(("backend_id", "instance_id", "session_id", "generation", "state", "pid", "process_start"), row)) if row else None

    def record(self, backend_id, instance_id, session_id, generation, state, detail="", pid=None, process_start=None):
        old = self.current()
        with self.db:
            self.db.execute("INSERT INTO binding(singleton,backend_id,instance_id,session_id,generation,state,pid,process_start) VALUES (1,?,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET backend_id=excluded.backend_id,instance_id=excluded.instance_id,session_id=excluded.session_id,generation=excluded.generation,state=excluded.state,pid=excluded.pid,process_start=excluded.process_start",
                            (backend_id, instance_id, session_id, generation, state, pid, process_start))
            self.db.execute("INSERT INTO switch_audit(at,source_instance,target_instance,generation,state,detail) VALUES (?,?,?,?,?,?)",
                            (time.time(), old["instance_id"] if old else None, instance_id, generation, state, detail))

    def close(self):
        self.db.close()

    def audit_failure(self, target_instance, detail):
        old = self.current()
        with self.db:
            self.db.execute("INSERT INTO switch_audit(at,source_instance,target_instance,generation,state,detail) VALUES (?,?,?,?,?,?)",
                            (time.time(), old["instance_id"] if old else None, target_instance,
                             old["generation"] if old else 0, "failed", detail))
