"""Disk-backed FE evidence for models that exceed the in-memory fast path."""

from collections.abc import Mapping, Sequence
import json
import os
import sqlite3
import tempfile


class Evidence:
    def __init__(self):
        fd, self.path = tempfile.mkstemp(prefix="ansa-quality-fe-", suffix=".sqlite3")
        os.close(fd)
        self.db = sqlite3.connect(self.path)
        self.db.execute("CREATE TABLE elements (seq INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def add_element(self, row):
        self.db.execute("INSERT INTO elements(body) VALUES (?)", (_encode(row),))

    def add_node(self, key, row):
        self.db.execute("INSERT INTO nodes VALUES (?,?)", (key, _encode(row)))

    def snapshot(self):
        self.db.commit()
        return {"elements": Elements(self), "nodes": Nodes(self)}

    def __del__(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            os.unlink(self.path)
        except (OSError, AttributeError):
            pass


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class Elements(Sequence):
    def __init__(self, evidence):
        self.evidence = evidence

    def __len__(self):
        return self.evidence.db.execute("SELECT COUNT(*) FROM elements").fetchone()[0]

    def __iter__(self):
        for row, in self.evidence.db.execute("SELECT body FROM elements ORDER BY seq"):
            yield json.loads(row)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += len(self)
        row = self.evidence.db.execute("SELECT body FROM elements WHERE seq=?", (index + 1,)).fetchone()
        if row is None:
            raise IndexError(index)
        return json.loads(row[0])

    def __eq__(self, other):
        return isinstance(other, (Sequence, list)) and len(self) == len(other) and all(a == b for a, b in zip(self, other))


class Nodes(Mapping):
    def __init__(self, evidence):
        self.evidence = evidence

    def __len__(self):
        return self.evidence.db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    def __iter__(self):
        for key, in self.evidence.db.execute("SELECT id FROM nodes ORDER BY id"):
            yield key

    def __getitem__(self, key):
        row = self.evidence.db.execute("SELECT body FROM nodes WHERE id=?", (str(key),)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def items(self):
        for key, body in self.evidence.db.execute("SELECT id,body FROM nodes ORDER BY id"):
            yield key, json.loads(body)

    def __eq__(self, other):
        return isinstance(other, Mapping) and len(self) == len(other) and all(key in other and row == other[key] for key, row in self.items())
