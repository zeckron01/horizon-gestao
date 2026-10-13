"""Acesso ao banco: SQLite (arquivo local) ou PostgreSQL (DATABASE_URL, ex.: Neon), com a mesma interface.

O SQL da aplicação é escrito uma vez, no dialeto comum; `_to_pg` adapta as poucas diferenças para o PostgreSQL."""
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import pg
from .config import cfg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_local = threading.local()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id():
    return str(uuid.uuid4())


IntegrityError = (sqlite3.IntegrityError, pg.IntegrityError)
BusyError = (sqlite3.OperationalError,)
WRITE_LOCK = 74261     # trava consultiva: uma transação de escrita por vez (mesma garantia do SQLite)
MIGRATE_LOCK = 74262


class DB:
    """Envoltório fino sobre sqlite3 com linhas em dict."""
    dialect = "sqlite"

    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 15000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
        self.in_tx = False

    def q(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def val(self, sql, params=()):
        r = self.conn.execute(sql, params).fetchone()
        return r[0] if r else None

    def ex(self, sql, params=()):
        return self.conn.execute(sql, params)

    def begin(self):
        self.conn.execute("BEGIN IMMEDIATE")
        self.in_tx = True

    def commit(self):
        if self.in_tx:
            self.conn.execute("COMMIT")
            self.in_tx = False

    def rollback(self):
        if self.in_tx:
            try:
                self.conn.execute("ROLLBACK")
            finally:
                self.in_tx = False

    def insert(self, table, row):
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))

    def columns(self, table):
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]

    def script(self, sql):
        self.conn.executescript(sql)

    def close(self):
        self.conn.close()


_TR = {}
_RX_QMARK = re.compile(r"'(?:[^']|'')*'|\?")
_RX_NOCASE = re.compile(r"([\w.]+)\s+COLLATE\s+NOCASE", re.I)


def _to_pg(sql):
    """SQL comum → PostgreSQL: marcadores $n, INSERT OR IGNORE, COLLATE NOCASE, LIKE sem diferenciar maiúsculas, IS ?."""
    out = _TR.get(sql)
    if out is None:
        s = sql
        ignore = bool(re.match(r"\s*INSERT\s+OR\s+IGNORE\b", s, re.I))
        if ignore:
            s = re.sub(r"^(\s*)INSERT\s+OR\s+IGNORE\b", r"\1INSERT", s, flags=re.I)
        s = _RX_NOCASE.sub(r"lower(\1)", s)
        s = re.sub(r"\bIS\s+NOT\s+\?", "IS DISTINCT FROM ?", s, flags=re.I)
        s = re.sub(r"\bIS\s+\?", "IS NOT DISTINCT FROM ?", s, flags=re.I)
        s = re.sub(r"\bLIKE\b", "ILIKE", s, flags=re.I)
        n = [0]

        def mark(m):
            if m.group(0) != "?":
                return m.group(0)
            n[0] += 1
            return f"${n[0]}"
        s = _RX_QMARK.sub(mark, s)
        if ignore:
            s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        out = _TR[sql] = s
    return out


def ddl_to_pg(sql):
    """Migrações: tipos e cláusulas do SQLite → PostgreSQL."""
    s = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", sql, flags=re.I)
    s = re.sub(r"\bBLOB\b", "BYTEA", s)
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
    s = re.sub(r"\s+COLLATE\s+NOCASE\b", "", s, flags=re.I)
    return s


class _Lazy:
    """Resultado de uma instrução enviada em lote: só vai ao servidor quando alguém precisa do resultado."""
    __slots__ = ("db", "res")

    def __init__(self, db, res):
        self.db, self.res = db, res

    @property
    def rowcount(self):
        if not self.res.done:
            self.db.conn.flush()
        return self.res.rowcount

    def fetchall(self):
        if not self.res.done:
            self.db.conn.flush()
        return [tuple(r) for r in self.res.rows]

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None


class PgDB:
    """Mesma interface de DB, sobre PostgreSQL. Dentro de transação, gravações são enviadas em lote."""
    dialect = "pg"

    def __init__(self, url):
        self.path = url
        self.conn = pg.Connection(url)
        self.in_tx = False
        self.queries = 0   # idas ao servidor (diagnóstico)

    def _run(self, sql, params):
        sql = _to_pg(sql)
        self.queries += 1
        try:
            return self.conn.execute(sql, params)
        except pg.OperationalError:
            if self.in_tx:
                raise
            self.conn = pg.Connection(self.path)   # conexão ociosa encerrada pelo servidor: reconecta uma vez
            return self.conn.execute(sql, params)

    def q(self, sql, params=()):
        r = self._run(sql, params)
        cols = r.columns
        return [dict(zip(cols, row)) for row in r.rows]

    def one(self, sql, params=()):
        r = self._run(sql, params)
        return dict(zip(r.columns, r.rows[0])) if r.rows else None

    def val(self, sql, params=()):
        r = self._run(sql, params)
        return r.rows[0][0] if r.rows else None

    def ex(self, sql, params=()):
        if self.in_tx:
            return _Lazy(self, self.conn.defer(_to_pg(sql), params))
        return _Lazy(self, self._run(sql, params))

    def begin(self):
        sql = f"BEGIN; SELECT pg_advisory_xact_lock({WRITE_LOCK})"
        self.queries += 1
        try:
            self.conn.script(sql)
        except pg.OperationalError:
            self.conn = pg.Connection(self.path)
            self.conn.script(sql)
        self.in_tx = True

    def commit(self):
        if self.in_tx:
            self.queries += 1
            try:
                self.conn.script("COMMIT")   # envia antes as gravações pendentes; um erro nelas aborta a transação
            except Exception:
                self.rollback()
                raise
            self.in_tx = False

    def rollback(self):
        if self.in_tx:
            self.in_tx = False
            self.conn.pending, self.conn.pending_res = [], []
            try:
                if not self.conn.closed:
                    self.conn.script("ROLLBACK")
            except Exception:
                self.conn.close()

    def insert(self, table, row):
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        self.ex(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))

    def columns(self, table):
        return [r["column_name"] for r in self.q("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=? ORDER BY ordinal_position", (table,))]

    def script(self, sql):
        self.conn.script(sql)

    def close(self):
        self.conn.close()


class NotConfigured(Exception):
    pass


_pool, _pool_lock = [], threading.Lock()
POOL_MAX_IDLE = 4


def acquire():
    """Conexão para atender uma requisição. SQLite: a da thread. PostgreSQL: uma conexão reaproveitada de um
    pequeno conjunto (abrir conexão custa caro e as threads de trabalho do servidor são descartáveis)."""
    if not cfg.DATABASE_URL:
        return get_db()
    db = None
    with _pool_lock:
        while _pool:
            cand = _pool.pop()
            if not cand.conn.closed and cand.path == cfg.DATABASE_URL:
                db = cand
                break
            cand.close()
    if db is None:
        db = PgDB(cfg.DATABASE_URL)
    _local.db, _local.gen = db, _generation
    return db


def release(db):
    if db is None or db.dialect != "pg":
        return
    if getattr(_local, "db", None) is db:
        _local.db = None
    try:
        if db.in_tx:
            db.rollback()
    except Exception:
        db.close()
    with _pool_lock:
        if not db.conn.closed and db.path == cfg.DATABASE_URL and len(_pool) < POOL_MAX_IDLE:
            _pool.append(db)
            return
    db.close()


def get_db():
    """Conexão da thread atual (reaberta se o banco mudou ou a conexão caiu). Dentro de uma requisição,
    é a mesma conexão entregue por acquire()."""
    if cfg.SERVERLESS and not cfg.DATABASE_URL:
        raise NotConfigured("Banco de dados não configurado: defina a variável de ambiente DATABASE_URL (PostgreSQL) na hospedagem.")
    db = getattr(_local, "db", None)
    target = cfg.DATABASE_URL or str(cfg.DB_PATH)
    if db is None or db.path != target or getattr(_local, "gen", 0) != _generation or (db.dialect == "pg" and db.conn.closed):
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
        if cfg.DATABASE_URL:
            db = PgDB(cfg.DATABASE_URL)
        else:
            cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            db = DB(cfg.DB_PATH)
        _local.db = db
        _local.gen = _generation
    return db


_generation = 0


def invalidate_connections():
    """Força todas as threads a reabrirem a conexão (usado após restaurar um backup)."""
    global _generation
    _generation += 1


def migrate(db=None):
    """Aplica, em ordem, as migrações ainda não registradas em schema_migrations."""
    db = db or get_db()
    db.ex("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    done = {r["version"] for r in db.q("SELECT version FROM schema_migrations")}
    if all(f.stem in done for f in files):
        return []
    applied = []
    if db.dialect == "pg":
        # Várias instâncias podem iniciar ao mesmo tempo: a trava garante que só uma aplique cada migração.
        db.script(f"BEGIN; SELECT pg_advisory_xact_lock({MIGRATE_LOCK})")
        db.in_tx = True
        try:
            done = {r["version"] for r in db.q("SELECT version FROM schema_migrations")}
            for f in files:
                if f.stem in done:
                    continue
                db.script(ddl_to_pg(f.read_text(encoding="utf-8")))
                db.ex("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (f.stem, now_iso()))
                applied.append(f.stem)
            db.commit()
        except Exception:
            db.rollback()
            raise
        return applied
    for f in files:
        if f.stem in done:
            continue
        sql = f.read_text(encoding="utf-8")
        try:
            db.conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
        except Exception:
            try:
                db.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        db.ex("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (f.stem, now_iso()))
        applied.append(f.stem)
    return applied
