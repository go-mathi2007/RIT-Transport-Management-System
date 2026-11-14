import base64
import json
import os
import re
import urllib.request
import urllib.error

class TursoRow:
    """
    Drop-in replacement for sqlite3.Row.
    Supports:
      - dict key access: row['column'] (case-insensitive)
      - index access: row[0], row[1]
      - dict methods: .get(), .keys(), .items(), .values()
      - conversion: dict(row)
    """
    def __init__(self, cols, values):
        self._cols = list(cols)
        self._values = list(values)
        self._lower_map = {c.lower(): idx for idx, c in enumerate(self._cols)}

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._values[item]
        if isinstance(item, str):
            idx = self._lower_map.get(item.lower())
            if idx is not None:
                return self._values[idx]
            raise KeyError(item)
        raise TypeError(f"Row indices must be integers or strings, not {type(item).__name__}")

    def get(self, key, default=None):
        idx = self._lower_map.get(str(key).lower())
        if idx is not None:
            return self._values[idx]
        return default

    def __contains__(self, key):
        return str(key).lower() in self._lower_map

    def keys(self):
        return self._cols

    def values(self):
        return self._values

    def items(self):
        return list(zip(self._cols, self._values))

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        d = dict(zip(self._cols, self._values))
        return f"<TursoRow {d}>"


def _encode_arg(arg):
    if arg is None:
        return {"type": "null"}
    elif isinstance(arg, bool):
        return {"type": "integer", "value": "1" if arg else "0"}
    elif isinstance(arg, int):
        return {"type": "integer", "value": str(arg)}
    elif isinstance(arg, float):
        return {"type": "float", "value": arg}
    elif isinstance(arg, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(arg)).decode("ascii")}
    else:
        return {"type": "text", "value": str(arg)}


def _decode_val(typed_val):
    if not isinstance(typed_val, dict):
        return typed_val
    t = typed_val.get("type")
    if t == "null":
        return None
    elif t == "integer":
        return int(typed_val.get("value", 0))
    elif t == "float":
        return float(typed_val.get("value", 0.0))
    elif t == "text":
        return str(typed_val.get("value", ""))
    elif t == "blob":
        raw_b64 = typed_val.get("base64", "")
        # Add padding if Turso returns un-padded base64 (length not a multiple of 4)
        missing_padding = len(raw_b64) % 4
        if missing_padding:
            raw_b64 += "=" * (4 - missing_padding)
        return base64.b64decode(raw_b64)
    return typed_val.get("value")


class TursoCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.lastrowid = None
        self.rowcount = -1
        self._rows = []
        self._pos = 0

    def execute(self, sql, params=None):
        return self.connection._execute_cursor(self, sql, params)

    def executemany(self, sql, seq_of_params):
        for params in seq_of_params:
            self.execute(sql, params)
        return self

    def fetchone(self):
        if self._pos < len(self._rows):
            row = self._rows[self._pos]
            self._pos += 1
            return row
        return None

    def fetchall(self):
        if self._pos >= len(self._rows):
            return []
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def fetchmany(self, size=None):
        if size is None:
            size = 1
        res = self._rows[self._pos:self._pos + size]
        self._pos += len(res)
        return res

    def close(self):
        self._rows = []
        self._pos = 0

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class TursoConnection:
    def __init__(self, url, auth_token, timeout=30.0):
        # Normalize URL to HTTPS endpoint
        clean_url = url.strip()
        if clean_url.startswith("libsql://"):
            clean_url = "https://" + clean_url[9:]
        elif not clean_url.startswith("https://") and not clean_url.startswith("http://"):
            clean_url = "https://" + clean_url
        clean_url = clean_url.rstrip("/")
        if not clean_url.endswith("/v2/pipeline"):
            self.pipeline_url = clean_url + "/v2/pipeline"
        else:
            self.pipeline_url = clean_url

        self.auth_token = auth_token.strip()
        self.timeout = timeout
        self.row_factory = TursoRow
        self.baton = None  # Tracks the pinned session/transaction baton
        self.in_transaction = False

    def cursor(self):
        return TursoCursor(self)

    def execute(self, sql, params=None):
        cursor = self.cursor()
        return cursor.execute(sql, params)

    def begin(self):
        """Starts an explicit transaction, pinning a session baton on Turso."""
        if not self.in_transaction:
            self._send_raw([{"type": "execute", "stmt": {"sql": "BEGIN"}}], retain_baton=True)
            self.in_transaction = True

    def commit(self):
        """Commits the active transaction and closes the session baton."""
        if self.in_transaction and self.baton:
            try:
                self._send_raw([
                    {"type": "execute", "stmt": {"sql": "COMMIT"}},
                    {"type": "close"}
                ], retain_baton=False)
            finally:
                self.baton = None
                self.in_transaction = False

    def rollback(self):
        """Rolls back the active transaction and closes the session baton, discarding partial writes."""
        if self.in_transaction and self.baton:
            try:
                self._send_raw([
                    {"type": "execute", "stmt": {"sql": "ROLLBACK"}},
                    {"type": "close"}
                ], retain_baton=False)
            finally:
                self.baton = None
                self.in_transaction = False

    def close(self):
        """Closes the connection, rolling back any uncommitted transaction."""
        if self.in_transaction:
            self.rollback()
        elif self.baton:
            try:
                self._send_raw([{"type": "close"}], retain_baton=False)
            finally:
                self.baton = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()

    def _send_raw(self, requests, retain_baton=False):
        payload = {
            "baton": self.baton,
            "requests": requests
        }

        req = urllib.request.Request(
            self.pipeline_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.auth_token}",
                "Content-Type": "application/json",
                "User-Agent": "RIT-Transport-BusMate/1.0"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Turso HTTP {e.code} error: {err_body}")
        except Exception as e:
            raise RuntimeError(f"Turso connection error: {e}")

        # Update baton
        new_baton = data.get("baton")
        if retain_baton:
            self.baton = new_baton
        else:
            if not self.in_transaction:
                self.baton = None

        return data

    def _execute_cursor(self, cursor, sql, params=None):
        # Format arguments
        args = []
        if params is not None:
            if isinstance(params, (list, tuple)):
                args = [_encode_arg(p) for p in params]
            elif isinstance(params, dict):
                args = [_encode_arg(v) for v in params.values()]
            else:
                args = [_encode_arg(params)]

        # Detect DML statements that modify data to automatically start a transaction if not already started
        sql_upper = sql.strip().upper()
        is_write = any(sql_upper.startswith(w) for w in ("INSERT", "UPDATE", "DELETE", "REPLACE"))

        if is_write and not self.in_transaction:
            # Auto-start transaction like standard sqlite3
            self.begin()

        requests = [
            {
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": args
                }
            }
        ]

        # If not in transaction, close request immediately
        if not self.in_transaction:
            requests.append({"type": "close"})

        data = self._send_raw(requests, retain_baton=self.in_transaction)

        # Check for errors in results
        results = data.get("results", [])
        if not results:
            raise RuntimeError("Turso returned empty result set")

        first_res = results[0]
        if first_res.get("type") == "error":
            err_msg = first_res.get("error", {}).get("message", "Unknown Turso error")
            raise RuntimeError(f"Turso SQL error: {err_msg}")

        stmt_resp = first_res.get("response", {}).get("result", {})
        raw_cols = stmt_resp.get("cols", [])
        col_names = [c["name"] for c in raw_cols] if raw_cols else []
        cursor.description = tuple((name, None, None, None, None, None, None) for name in col_names) if col_names else None

        raw_rows = stmt_resp.get("rows", [])
        parsed_rows = []
        for r in raw_rows:
            parsed_vals = [_decode_val(v) for v in r]
            parsed_rows.append(TursoRow(col_names, parsed_vals))

        cursor._rows = parsed_rows
        cursor._pos = 0

        # Set lastrowid and rowcount
        raw_lastrowid = stmt_resp.get("last_insert_rowid")
        if raw_lastrowid is not None:
            try:
                cursor.lastrowid = int(raw_lastrowid)
            except (ValueError, TypeError):
                cursor.lastrowid = None
        else:
            cursor.lastrowid = None

        cursor.rowcount = stmt_resp.get("affected_row_count", len(parsed_rows))
        return cursor
