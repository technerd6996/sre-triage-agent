"""Local PostgreSQL access.

The app used to talk to a cloud Supabase through supabase-py. All persistence now goes to a local PostgreSQL
(see scripts/local_db.ps1) through this module, which keeps the same query-builder calls the rest of the code
already uses:

    db.table("runs").select("id,status").eq("id", x).order("ts", desc=True).limit(5).execute().data

Every value is sent as a bound parameter. Table and column names are checked against a strict pattern, so
nothing from a request can reach the SQL text.
"""
import atexit
import logging
import os
import re
import threading

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

log = logging.getLogger("uvicorn.error")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()
_pk_cache: dict[str, list[str]] = {}
_type_cache: dict[str, dict[str, str]] = {}


class DatabaseConfigError(RuntimeError):
    pass


def _ident(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return '"' + name + '"'


def database_url() -> str:
    from app import bootstrap_store
    bootstrap_store.load()
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise DatabaseConfigError("DATABASE_URL is not set. Start the app with scripts\start.ps1, which creates the "
                                  "local database and its bootstrap file on first run.")
    return url


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                p = ConnectionPool(database_url(), min_size=1, max_size=int(os.getenv("DB_POOL_MAX", "10")),
                                   kwargs={"autocommit": True, "row_factory": dict_row,
                                           "connect_timeout": 10}, open=False)
                p.open(wait=True, timeout=20)       # fails fast with a clear error if the database is down
                _pool = p
                atexit.register(close_pool)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _adapt(v):
    """Python value -> bound parameter. dict -> jsonb; a list of numbers -> double precision[] (embeddings);
    any other list -> jsonb."""
    if isinstance(v, dict):
        return Jsonb(v)
    if isinstance(v, (list, tuple)):
        if v and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            return [float(x) for x in v]
        return Jsonb(list(v))
    return v


class Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


def _primary_key(table: str) -> list[str]:
    if table not in _pk_cache:
        with get_pool().connection() as c:
            rows = c.execute(
                "select a.attname from pg_index i join pg_attribute a on a.attrelid = i.indrelid "
                "and a.attnum = any(i.indkey) where i.indrelid = %s::regclass and i.indisprimary "
                "order by array_position(i.indkey::int[], a.attnum::int)", (_ident(table),)).fetchall()
        _pk_cache[table] = [r["attname"] for r in rows]
    return _pk_cache[table]


def _col_types(table: str) -> dict[str, str]:
    if table not in _type_cache:
        with get_pool().connection() as c:
            rows = c.execute("select column_name, data_type from information_schema.columns "
                             "where table_schema = current_schema() and table_name = %s", (table,)).fetchall()
        _type_cache[table] = {r["column_name"]: r["data_type"] for r in rows}
    return _type_cache[table]


def _adapt_col(table: str, col: str, v):
    """Like _adapt, but a json/jsonb column takes any Python value (True, 5, "x", [..], {..}) as JSON."""
    if v is not None and _col_types(table).get(col) in ("json", "jsonb"):
        return Jsonb(v)
    return _adapt(v)


class _Query:
    def __init__(self, table: str):
        self._table = table
        self._op = "select"
        self._cols = "*"
        self._rows: list[dict] = []
        self._where: list[tuple[str, list]] = []
        self._order: list[tuple[str, bool]] = []
        self._limit: int | None = None
        self._single = False

    # ---- operations ----
    def select(self, cols: str = "*"):
        self._op, self._cols = "select", cols
        return self

    def insert(self, rows):
        self._op, self._rows = "insert", [rows] if isinstance(rows, dict) else list(rows)
        return self

    def upsert(self, rows):
        self._op, self._rows = "upsert", [rows] if isinstance(rows, dict) else list(rows)
        return self

    def update(self, values: dict):
        self._op, self._rows = "update", [values]
        return self

    def delete(self):
        self._op = "delete"
        return self

    # ---- filters ----
    def _add(self, col, op, val):
        self._where.append((f"{_ident(col)} {op} %s", [val]))
        return self

    def eq(self, c, v): return self._add(c, "=", v)
    def neq(self, c, v): return self._add(c, "<>", v)
    def gt(self, c, v): return self._add(c, ">", v)
    def gte(self, c, v): return self._add(c, ">=", v)
    def lt(self, c, v): return self._add(c, "<", v)
    def lte(self, c, v): return self._add(c, "<=", v)
    def like(self, c, p): return self._add(c, "like", p)
    def ilike(self, c, p): return self._add(c, "ilike", p)

    def in_(self, col, values):
        vals = list(values)
        if not vals:
            self._where.append(("false", []))
        else:
            self._where.append((f"{_ident(col)} = any(%s)", [vals]))
        return self

    def is_(self, col, val):
        if val in (None, "null"):
            self._where.append((f"{_ident(col)} is null", []))
        elif val in (True, "true"):
            self._where.append((f"{_ident(col)} is true", []))
        elif val in (False, "false"):
            self._where.append((f"{_ident(col)} is false", []))
        else:
            raise ValueError("is_() accepts only null/true/false")
        return self

    def order(self, col, desc: bool = False):
        self._order.append((_ident(col), bool(desc)))
        return self

    def limit(self, n: int):
        self._limit = int(n)
        return self

    def single(self):
        self._single = True
        return self

    # ---- build and run ----
    def _where_sql(self):
        if not self._where:
            return "", []
        return " where " + " and ".join(w for w, _ in self._where), [p for _, ps in self._where for p in ps]

    def _build(self):
        t = _ident(self._table)
        wsql, wparams = self._where_sql()
        if self._op == "select":
            cols = "*" if self._cols.strip() == "*" else ", ".join(_ident(c.strip()) for c in self._cols.split(","))
            sql = f"select {cols} from {t}{wsql}"
            if self._order:
                sql += " order by " + ", ".join(c + (" desc" if d else " asc") for c, d in self._order)
            if self._limit is not None:
                sql += f" limit {self._limit}"
            return sql, wparams
        if self._op in ("insert", "upsert"):
            if not self._rows:
                return None, []
            keys = list(self._rows[0].keys())
            if any(set(r.keys()) != set(keys) for r in self._rows):
                raise ValueError("all rows in one insert must have the same columns")
            cols = ", ".join(_ident(k) for k in keys)
            one = "(" + ", ".join(["%s"] * len(keys)) + ")"
            sql = f"insert into {t} ({cols}) values " + ", ".join([one] * len(self._rows))
            params = [_adapt_col(self._table, k, r[k]) for r in self._rows for k in keys]
            if self._op == "upsert":
                pk = _primary_key(self._table)
                if not pk:
                    raise ValueError(f"{self._table} has no primary key to upsert on")
                upd = [k for k in keys if k not in pk]
                sql += " on conflict (" + ", ".join(_ident(p) for p in pk) + ") "
                sql += ("do update set " + ", ".join(f"{_ident(k)} = excluded.{_ident(k)}" for k in upd)
                        if upd else "do nothing")
            return sql + " returning *", params
        if not self._where:
            raise ValueError(f"{self._op} without a filter is refused")
        if self._op == "update":
            vals = self._rows[0]
            sets = ", ".join(f"{_ident(k)} = %s" for k in vals)
            return f"update {t} set {sets}{wsql} returning *", [_adapt_col(self._table, k, v) for k, v in vals.items()] + wparams
        return f"delete from {t}{wsql} returning *", wparams

    def execute(self) -> Result:
        sql, params = self._build()
        if sql is None:
            return Result([])
        with get_pool().connection() as c:
            rows = c.execute(sql, params).fetchall()
        if self._single:
            if len(rows) != 1:
                raise LookupError(f"expected one row, got {len(rows)}")
            return Result(rows[0])
        return Result(rows)


class _Rpc:
    def __init__(self, fn: str, params: dict):
        self._fn, self._params = fn, params or {}

    def execute(self) -> Result:
        args = ", ".join(f"{_ident(k)} => %s" for k in self._params)
        sql = f"select * from {_ident(self._fn)}({args})"
        with get_pool().connection() as c:
            return Result(c.execute(sql, [_adapt(v) for v in self._params.values()]).fetchall())


class Client:
    """Drop-in for the supabase-py Client for the calls this app makes."""

    def table(self, name: str) -> _Query:
        _ident(name)
        return _Query(name)

    def rpc(self, fn: str, params: dict | None = None) -> _Rpc:
        return _Rpc(fn, params or {})


def connect() -> Client:
    return Client()
