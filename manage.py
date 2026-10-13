#!/usr/bin/env python3
"""Ferramentas de linha de comando da plataforma Horizon.

  python manage.py servidor                      inicia o servidor web
  python manage.py migrar                        aplica as migrações do banco
  python manage.py criar-admin                   cria o primeiro administrador (interativo ou por variáveis)
  python manage.py redefinir-senha <email>       define nova senha (recuperação de acesso do administrador)
  python manage.py importar-planilhas <pasta>    lê as planilhas e mostra a revisão; use --confirmar para gravar
  python manage.py executar-rotinas              gera notificações/e-mails/backup (para uso com cron)
  python manage.py backup                        cria um backup agora
  python manage.py restaurar <arquivo>           restaura um backup (.db no SQLite, com o servidor parado; .hzb no PostgreSQL)
  python manage.py exportar-backup <arq.hzb>     gera um backup lógico do banco em uso (para migrar SQLite → PostgreSQL)

Com a variável DATABASE_URL definida (postgresql://...), todos os comandos atuam no PostgreSQL.
"""
import getpass
import json
import os
import sys
from pathlib import Path


def _db():
    from server.db import get_db, migrate
    db = get_db()
    migrate(db)
    from server.modules.admin import ensure_base_data
    db.begin()
    ensure_base_data(db)
    db.commit()
    return db


def cmd_servidor(args):
    import uvicorn
    from server.config import cfg
    uvicorn.run("server.app:app", host=cfg.HOST, port=cfg.PORT, proxy_headers=True, forwarded_allow_ips="*", log_level="info")


def cmd_migrar(args):
    from server.db import get_db, migrate
    applied = migrate(get_db())
    print("Migrações aplicadas:", ", ".join(applied) if applied else "nenhuma pendente")


def cmd_criar_admin(args):
    from server import auth
    db = _db()
    name = os.environ.get("HORIZON_ADMIN_NAME") or input("Nome completo: ").strip()
    email = (os.environ.get("HORIZON_ADMIN_EMAIL") or input("E-mail: ").strip()).lower()
    pw = os.environ.get("HORIZON_ADMIN_PASSWORD") or getpass.getpass("Senha (mín. 8 caracteres, com letras e números): ")
    teacher = os.environ.get("HORIZON_ADMIN_IS_TEACHER", "1") == "1"
    try:
        auth.check_password_policy(pw)
    except Exception as exc:
        sys.exit(f"Senha recusada: {exc}")
    if db.one("SELECT 1 FROM users WHERE email=?", (email,)):
        sys.exit("Já existe um usuário com este e-mail.")
    from server.db import now_iso
    db.begin()
    uid = auth.create_user_record(db, name, email, pw, "admin", is_teacher=teacher)
    db.ex("INSERT INTO audit_log (at, user_id, user_name, action, entity_type, entity_id, summary) VALUES (?,?,?,?,?,?,?)",
          (now_iso(), uid, name, "criar", "users", uid, "Administrador criado pela linha de comando"))
    db.commit()
    print(f"Administrador criado: {name} <{email}>" + (" (marcado como professor técnico)" if teacher else ""))


def cmd_redefinir_senha(args):
    from server import auth
    from server.db import now_iso
    if not args:
        sys.exit("Informe o e-mail do usuário.")
    db = _db()
    u = db.one("SELECT * FROM users WHERE email=? AND deleted_at IS NULL", (args[0].lower(),))
    if not u:
        sys.exit("Usuário não encontrado.")
    pw = os.environ.get("HORIZON_NEW_PASSWORD") or getpass.getpass("Nova senha: ")
    try:
        auth.check_password_policy(pw)
    except Exception as exc:
        sys.exit(f"Senha recusada: {exc}")
    db.begin()
    db.ex("UPDATE users SET password_hash=?, must_change_password=0, failed_logins=0, locked_until=NULL, active=1, updated_at=?, version=version+1 WHERE id=?",
          (auth.hash_password(pw), now_iso(), u["id"]))
    db.ex("DELETE FROM sessions WHERE user_id=?", (u["id"],))
    db.ex("INSERT INTO audit_log (at, user_id, user_name, action, entity_type, entity_id, summary) VALUES (?,?,?,?,?,?,?)",
          (now_iso(), u["id"], u["name"], "redefinir_senha", "users", u["id"], "Senha redefinida pela linha de comando do servidor"))
    db.commit()
    print("Senha redefinida. As sessões abertas deste usuário foram encerradas.")


def cmd_importar_planilhas(args):
    from server.core import Ctx, get_setting
    from server.modules import importer
    confirm = "--confirmar" in args
    args = [a for a in args if not a.startswith("--")]
    folder = Path(args[0] if args else "dados-originais")
    files = sorted(folder.glob("*.xlsx"))
    if not files:
        sys.exit(f"Nenhuma planilha .xlsx encontrada em {folder}")
    db = _db()
    admin = db.one("SELECT * FROM users WHERE role='admin' AND active=1 AND deleted_at IS NULL ORDER BY created_at LIMIT 1")
    if not admin:
        sys.exit("Crie o administrador antes de importar (python manage.py criar-admin).")
    sid = get_setting(db, "default_season_id")
    db.begin()
    try:
        ctx = Ctx(db, admin, method="POST", path="cli")
        imp = importer.create_import(ctx, sid, [{"name": f.name, "data": f.read_bytes()} for f in files])
        print("\nArquivos:")
        for f in imp["files"]:
            print(f"  - {f['name']}: {f['status']}" + (f" — {f['message']}" if f.get("message") else ""))
        print("\nResumo da revisão:")
        for k, v in imp["staging"]["summary"].items():
            print(f"  {v['label']}: " + ", ".join(f"{kk}={vv}" for kk, vv in v.items() if kk != "label"))
        print("\nConferências:")
        for c in imp["staging"]["checks"]:
            print(f"  [{'ok' if c['ok'] else 'DIVERGENTE'}] {c['check']}: esperado {c['expected']}, obtido {c['actual']}")
        if confirm:
            done = importer.confirm(ctx, imp["id"])
            print("\nImportação confirmada:")
            for k, v in done["summary"]["result"].items():
                print(f"  {done['summary']['labels'].get(k, k)}: {v}")
            for c in done["summary"]["checks"]:
                print(f"  [{'ok' if c['ok'] else 'DIVERGENTE'}] {c['check']}: esperado {c['expected']}, obtido {c['actual']}")
        else:
            print("\nNada foi gravado nas tabelas de negócio. Revise na tela Configurações > Importações ou repita com --confirmar.")
        db.commit()
    except BaseException:
        db.rollback()
        raise


def cmd_executar_rotinas(args):
    from server import jobs
    print(json.dumps(jobs.run_all(_db(), kind="cli"), ensure_ascii=False, indent=1))


def cmd_backup(args):
    from server.modules.admin import create_backup
    _db()
    print(json.dumps(create_backup("manual"), ensure_ascii=False))


def cmd_restaurar(args):
    import sqlite3
    from server.config import cfg
    from server.modules.admin import _validate_backup_file, create_backup
    if not args:
        sys.exit("Informe o arquivo de backup (.db no modo SQLite; .hzb no modo PostgreSQL).")
    src_path = Path(args[0]).resolve()
    if not src_path.exists():
        sys.exit("Arquivo não encontrado.")
    if cfg.DATABASE_URL:   # PostgreSQL: backup lógico (.hzb), inclusive um gerado a partir do SQLite com "exportar-backup"
        from server.modules.admin import restore_logical
        db = _db()
        try:
            safety = restore_logical(db, src_path.read_bytes(), {"id": None, "name": "linha de comando"}, src_path.name)
        except Exception as exc:
            sys.exit(f"Restauração não realizada: {getattr(exc, 'message', exc)}")
        print("Backup de segurança do estado anterior:", safety["name"])
        print("Banco restaurado a partir de", src_path.name)
        return
    try:
        _validate_backup_file(src_path)
    except Exception as exc:
        sys.exit(f"Backup inválido: {exc}")
    if cfg.DB_PATH.exists():
        print("Backup de segurança do estado atual:", create_backup("pre-restauracao")["name"])
    cfg.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(cfg.DB_PATH))
    src.backup(dst)
    dst.execute("DELETE FROM sessions")
    dst.commit()
    dst.close()
    src.close()
    cmd_migrar([])
    print("Banco restaurado a partir de", src_path.name)


def cmd_exportar_backup(args):
    """Gera um backup lógico (.hzb) do banco em uso — serve para levar os dados do SQLite local para o PostgreSQL."""
    from server import backup_logical as bl
    if not args:
        sys.exit("Informe o arquivo de destino (ex.: dados.hzb).")
    db = _db()
    db.begin()
    data = bl.dump(db)
    db.rollback()
    head, _ = bl.read(data, require_admin=False)
    Path(args[0]).write_bytes(data)
    print(f"Backup lógico gravado em {args[0]} ({len(data)} bytes; {sum(head['counts'].values())} registros em {len(head['counts'])} tabelas).")


COMMANDS = {"exportar-backup": cmd_exportar_backup, "servidor": cmd_servidor, "migrar": cmd_migrar, "criar-admin": cmd_criar_admin, "redefinir-senha": cmd_redefinir_senha,
            "importar-planilhas": cmd_importar_planilhas, "executar-rotinas": cmd_executar_rotinas, "backup": cmd_backup, "restaurar": cmd_restaurar}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
