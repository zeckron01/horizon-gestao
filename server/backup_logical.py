"""Backup lógico (arquivo .hzb): todas as tabelas em JSON compactado, com soma de verificação.

Usado no modo PostgreSQL, em que não existe um arquivo de banco para copiar. O arquivo é independente do banco de
origem: um .hzb gerado a partir do SQLite (python manage.py exportar-backup) pode ser restaurado no PostgreSQL."""
import base64
import gzip
import hashlib
import json

from .db import now_iso

MAGIC = "horizon-backup"
SKIP = {"backups", "sessions", "idempotency", "schema_migrations", "password_resets"}
REQUIRED = {"users", "activities", "audit_log", "settings"}


class BackupError(Exception):
    pass


def table_names(db):
    if db.dialect == "pg":
        rows = db.q("SELECT table_name AS n FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE' ORDER BY table_name")
    else:
        rows = db.q("SELECT name AS n FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return [r["n"] for r in rows]


def _enc(v):
    return {"$b": base64.b64encode(bytes(v)).decode()} if isinstance(v, (bytes, bytearray, memoryview)) else v


def _dec(v):
    return base64.b64decode(v["$b"]) if isinstance(v, dict) and "$b" in v else v


def dump(db):
    """Gera o conteúdo do backup. Deve ser chamado dentro de uma transação para a cópia ser consistente."""
    tables = {}
    for t in table_names(db):
        if t in SKIP:
            continue
        cols = db.columns(t)
        rows = db.q(f"SELECT * FROM {t}")
        tables[t] = {"columns": cols, "rows": [[_enc(r.get(c)) for c in cols] for r in rows]}
    body = json.dumps(tables, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    head = {"format": MAGIC, "version": 1, "created_at": now_iso(), "origin": db.dialect,
            "migrations": [r["version"] for r in db.q("SELECT version FROM schema_migrations ORDER BY version")],
            "counts": {t: len(v["rows"]) for t, v in tables.items()}, "sha256": hashlib.sha256(body).hexdigest()}
    return gzip.compress(json.dumps(head, ensure_ascii=False).encode("utf-8") + b"\n" + body, compresslevel=6)


def read(data, require_admin=True):
    """Valida o arquivo (formato, soma de verificação, conteúdo mínimo) e devolve (cabeçalho, tabelas).
    require_admin=False é usado só para conferir um backup recém-gerado (ex.: o de segurança de um banco ainda vazio)."""
    if not data[:2] == b"\x1f\x8b":
        raise BackupError("O arquivo enviado não é um backup desta plataforma (.hzb).")
    try:
        raw = gzip.decompress(data)
        head_raw, body = raw.split(b"\n", 1)
        head = json.loads(head_raw)
    except Exception:
        raise BackupError("O arquivo de backup está corrompido (não foi possível ler).")
    if head.get("format") != MAGIC:
        raise BackupError("O arquivo enviado não é um backup desta plataforma (.hzb).")
    if hashlib.sha256(body).hexdigest() != head.get("sha256"):
        raise BackupError("O arquivo de backup está corrompido (falha na verificação de integridade).")
    tables = json.loads(body)
    if not REQUIRED <= set(tables):
        raise BackupError("O arquivo não é um backup completo desta plataforma.")
    u = tables["users"]
    ix = {c: i for i, c in enumerate(u["columns"])}
    if require_admin and not any(r[ix["role"]] == "admin" and r[ix["active"]] and r[ix["password_hash"]] and not r[ix["deleted_at"]] for r in u["rows"]):
        raise BackupError("O backup não contém nenhum administrador ativo; a restauração deixaria a plataforma sem acesso.")
    return head, tables


def restore_pg(db, tables, applied_migrations):
    """Substitui o conteúdo do banco PostgreSQL pelo do backup. Chamar dentro de uma transação: ou tudo, ou nada."""
    current = set(table_names(db))
    unknown = [t for t in tables if t not in current]
    if unknown:
        raise BackupError("O backup contém tabelas que esta versão da plataforma não conhece: " + ", ".join(unknown))
    # As chaves estrangeiras são removidas e recriadas ao final (a recriação revalida todos os vínculos).
    fks = db.q("SELECT conrelid::regclass::text AS tbl, conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
               "WHERE contype='f' AND connamespace=current_schema()::regnamespace ORDER BY conname")
    for fk in fks:
        db.ex(f'ALTER TABLE {fk["tbl"]} DROP CONSTRAINT "{fk["conname"]}"')
    targets = [t for t in current if t not in SKIP]
    db.ex("TRUNCATE " + ", ".join(targets + ["sessions", "idempotency", "password_resets"]))
    for t, spec in tables.items():
        have = set(db.columns(t))
        cols = [c for c in spec["columns"] if c in have]
        idx = [spec["columns"].index(c) for c in cols]
        sql = f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        for r in spec["rows"]:
            db.ex(sql, [_dec(r[i]) for i in idx])
    for t in targets:   # sequências de colunas seriais (ex.: audit_log.id)
        for c in db.q("SELECT column_name AS c FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=? AND column_default LIKE 'nextval%'", (t,)):
            db.val(f"SELECT setval(pg_get_serial_sequence('{t}', '{c['c']}'), COALESCE((SELECT MAX({c['c']}) FROM {t}), 0) + 1, false)")
    for fk in fks:
        db.ex(f'ALTER TABLE {fk["tbl"]} ADD CONSTRAINT "{fk["conname"]}" {fk["def"]}')
