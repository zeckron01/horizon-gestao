"""Autenticação (sessões no servidor, senha com scrypt), recuperação de acesso e gestão de usuários."""
import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from .config import cfg
from .core import (ApiError, F, INTERNAL, ROLES, Resource, audit, bad, conflict, forbidden, get_setting,
                   judge_view, not_found, route)
from .db import new_id, now_iso
from .mailer import send_mail

COOKIE = "hz_session"
_SCRYPT = dict(n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024)


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt$15$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password, stored):
    try:
        alg, ln, r, p, salt, dk = stored.split("$")
        if alg != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), dklen=32,
                              n=2 ** int(ln), r=int(r), p=int(p), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def check_password_policy(pw):
    if not isinstance(pw, str) or len(pw) < 8:
        raise bad("A senha deve ter pelo menos 8 caracteres.", {"password": "Mínimo de 8 caracteres."})
    if len(pw) > 200:
        raise bad("A senha é longa demais.")
    if not any(c.isalpha() for c in pw) or not any(c.isdigit() for c in pw):
        raise bad("A senha deve conter letras e números.", {"password": "Use letras e números."})


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db, user_id, ip, ua):
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.insert("sessions", {"token_hash": _h(token), "user_id": user_id, "csrf": secrets.token_urlsafe(24),
                           "created_at": now_iso(), "expires_at": (now + timedelta(hours=cfg.SESSION_HOURS)).isoformat(),
                           "last_seen_at": now_iso(), "ip": ip, "user_agent": (ua or "")[:300]})
    return token


def load_session(db, token):
    """Retorna (usuário, sessão) se o token é válido e o usuário continua ativo; caso contrário (None, None)."""
    if not token:
        return None, None
    s = db.one("SELECT * FROM sessions WHERE token_hash=?", (_h(token),))
    if not s or s["expires_at"] < datetime.now(timezone.utc).isoformat():
        return None, None
    u = db.one("SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (s["user_id"],))
    if not u or not u["active"] or not u["password_hash"]:
        return None, None
    return u, s


def public_user(u):
    return {k: u[k] for k in ("id", "name", "email", "role", "area_id", "position", "must_change_password")} | {
        "is_teacher": bool(u["is_teacher"]), "must_change_password": bool(u["must_change_password"])}


# ------------------------------------------------------------------ limitação de tentativas por IP (memória)
_attempts = {}


def _throttle(ip, count=True):
    """Até 30 tentativas a cada 10 minutos por endereço. No login só as tentativas que FALHAM contam
    (count=False + _fail), para não bloquear uma turma inteira que entra pela mesma rede."""
    now = time.time()
    lst = [t for t in _attempts.get(ip, []) if now - t < 600]
    if len(lst) >= 30:
        raise ApiError(429, "muitas_tentativas", "Muitas tentativas de acesso. Aguarde alguns minutos e tente novamente.")
    if count:
        lst.append(now)
    _attempts[ip] = lst


def _fail(ip):
    _attempts.setdefault(ip, []).append(time.time())


class SetCookie:
    """Marcador devolvido pelos handlers para o despachante definir/limpar o cookie de sessão."""

    def __init__(self, payload, token=None, clear=False):
        self.payload, self.token, self.clear = payload, token, clear


@route("POST", "/api/auth/login", public=True)
def login(ctx):
    _throttle(ctx.ip or "?", count=False)
    email = str(ctx.body.get("email", "")).strip().lower()
    password = str(ctx.body.get("password", ""))
    generic = ApiError(401, "credenciais_invalidas", "E-mail ou senha incorretos.")
    u = ctx.db.one("SELECT * FROM users WHERE email=? AND deleted_at IS NULL", (email,)) if email else None
    if not u or not u["password_hash"]:
        hash_password("tempo-constante")  # reduz diferença de tempo entre usuário existente e inexistente
        _fail(ctx.ip or "?")
        audit(ctx, "login_falhou", "sessao", None, f"E-mail não cadastrado: {email[:80]}")
        ctx.db.commit()
        raise generic
    nowi = datetime.now(timezone.utc).isoformat()
    if u["locked_until"] and u["locked_until"] > nowi:
        raise ApiError(429, "conta_bloqueada", "Conta temporariamente bloqueada por excesso de tentativas. Tente novamente em alguns minutos.")
    if not verify_password(password, u["password_hash"]):
        _fail(ctx.ip or "?")
        fails = u["failed_logins"] + 1
        lock = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat() if fails >= 5 else None
        ctx.db.ex("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (0 if lock else fails, lock, u["id"]))
        ctx.user = None
        audit(ctx, "login_falhou", "sessao", u["id"], f"Senha incorreta para {u['name']}")
        ctx.db.commit()  # registra a tentativa mesmo devolvendo erro
        raise generic
    if not u["active"]:
        audit(ctx, "login_falhou", "sessao", u["id"], f"Usuário desativado: {u['name']}")
        ctx.db.commit()
        raise ApiError(403, "usuario_desativado", "Este usuário está desativado. Procure o administrador.")
    ctx.db.ex("UPDATE users SET failed_logins=0, locked_until=NULL, last_login_at=? WHERE id=?", (now_iso(), u["id"]))
    ctx.db.ex("DELETE FROM sessions WHERE expires_at < ?", (nowi,))
    token = create_session(ctx.db, u["id"], ctx.ip, getattr(ctx, "ua", None))
    ctx.user = u
    audit(ctx, "login", "sessao", u["id"], f"Acesso de {u['name']}")
    return SetCookie({"ok": True}, token=token)


@route("POST", "/api/auth/logout", allow=ROLES, judge_ok=True)
def logout(ctx):
    ctx.db.ex("DELETE FROM sessions WHERE token_hash=?", (ctx.session["token_hash"],))
    audit(ctx, "logout", "sessao", ctx.uid, f"Saída de {ctx.user['name']}")
    return SetCookie({"ok": True}, clear=True)


@route("POST", "/api/auth/senha", allow=ROLES, judge_ok=True)
def change_own_password(ctx):
    if not verify_password(str(ctx.body.get("current", "")), ctx.user["password_hash"]):
        raise bad("A senha atual está incorreta.", {"current": "Senha incorreta."})
    new = ctx.body.get("password")
    check_password_policy(new)
    ctx.db.ex("UPDATE users SET password_hash=?, must_change_password=0, updated_at=?, version=version+1 WHERE id=?",
              (hash_password(new), now_iso(), ctx.uid))
    # encerra as demais sessões do usuário
    ctx.db.ex("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (ctx.uid, ctx.session["token_hash"]))
    audit(ctx, "trocar_senha", "users", ctx.uid, "Senha alterada pelo próprio usuário")
    return {"ok": True}


def _make_reset(db, user_id, by, minutes=60):
    token = secrets.token_urlsafe(32)
    db.insert("password_resets", {"token_hash": _h(token), "user_id": user_id, "created_by": by, "created_at": now_iso(),
                                  "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()})
    return token


@route("POST", "/api/auth/esqueci", public=True)
def forgot(ctx):
    """Recuperação por e-mail. Só envia se o SMTP estiver configurado; a resposta é sempre genérica."""
    _throttle("f:" + (ctx.ip or "?"))
    email = str(ctx.body.get("email", "")).strip().lower()
    if not cfg.smtp_configured:
        return {"ok": True, "email_enabled": False,
                "message": "O envio de e-mails não está configurado. Peça ao administrador um link de redefinição de senha."}
    u = ctx.db.one("SELECT * FROM users WHERE email=? AND active=1 AND deleted_at IS NULL AND password_hash IS NOT NULL", (email,))
    if u:
        token = _make_reset(ctx.db, u["id"], None)
        base = cfg.PUBLIC_URL or ""
        send_mail(u["email"], "Redefinição de senha — Horizon Escuderia",
                  f"Olá, {u['name']}.\n\nPara definir uma nova senha, acesse (válido por 60 minutos):\n{base}/#/redefinir/{token}\n\n"
                  "Se você não solicitou, ignore esta mensagem.")
        audit(ctx, "reset_solicitado", "users", u["id"], "Redefinição de senha solicitada por e-mail")
    return {"ok": True, "email_enabled": True,
            "message": "Se o e-mail estiver cadastrado, enviaremos um link de redefinição válido por 60 minutos."}


@route("POST", "/api/auth/redefinir", public=True)
def reset_password(ctx):
    _throttle("r:" + (ctx.ip or "?"))
    token = str(ctx.body.get("token", ""))
    r = ctx.db.one("SELECT * FROM password_resets WHERE token_hash=?", (_h(token),))
    if not r or r["used_at"] or r["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise ApiError(400, "token_invalido", "Link de redefinição inválido ou expirado. Solicite um novo.")
    new = ctx.body.get("password")
    check_password_policy(new)
    ctx.db.ex("UPDATE password_resets SET used_at=? WHERE token_hash=?", (now_iso(), r["token_hash"]))
    ctx.db.ex("UPDATE users SET password_hash=?, must_change_password=0, failed_logins=0, locked_until=NULL, updated_at=?, version=version+1 WHERE id=?",
              (hash_password(new), now_iso(), r["user_id"]))
    ctx.db.ex("DELETE FROM sessions WHERE user_id=?", (r["user_id"],))
    ctx.user = ctx.db.one("SELECT * FROM users WHERE id=?", (r["user_id"],))
    audit(ctx, "redefinir_senha", "users", r["user_id"], "Senha redefinida por link de uso único")
    return {"ok": True}


# ------------------------------------------------------------------ primeira configuração
def has_admin(db):
    return bool(db.val("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND deleted_at IS NULL AND password_hash IS NOT NULL"))


def create_user_record(db, name, email, password, role, is_teacher=False, by=None, must_change=False, area_id=None, position=None):
    ts = now_iso()
    uid = new_id()
    db.insert("users", {"id": uid, "name": name, "email": email.lower() if email else None,
                        "password_hash": hash_password(password) if password else None, "role": role,
                        "is_teacher": 1 if is_teacher else 0, "area_id": area_id, "position": position,
                        "must_change_password": 1 if must_change else 0,
                        "created_at": ts, "updated_at": ts, "created_by": by})
    return uid


@route("GET", "/api/setup/status", public=True)
def setup_status(ctx):
    return {"needs_setup": not has_admin(ctx.db)}


@route("POST", "/api/setup", public=True)
def setup(ctx):
    """Cria o primeiro administrador. Exige o token de instalação exibido no log do servidor (ou HORIZON_SETUP_TOKEN)."""
    if has_admin(ctx.db):
        raise conflict("A plataforma já possui um administrador.")
    _throttle("s:" + (ctx.ip or "?"))
    from . import app as app_mod
    if cfg.SERVERLESS and not cfg.SETUP_TOKEN:
        raise forbidden("Instalação bloqueada: defina a variável de ambiente HORIZON_SETUP_TOKEN na hospedagem e publique novamente.")
    expected = app_mod.setup_token()
    if not expected or not hmac.compare_digest(str(ctx.body.get("token", "")), expected):
        raise forbidden("Token de instalação inválido. Consulte o log do servidor ou a variável HORIZON_SETUP_TOKEN.")
    name = str(ctx.body.get("name", "")).strip()
    email = str(ctx.body.get("email", "")).strip().lower()
    if len(name) < 3:
        raise bad("Informe o nome completo.", {"name": "Informe o nome completo."})
    F("email", "E-mail", required=True).parse(ctx.db, "email", email)
    check_password_policy(ctx.body.get("password"))
    existing = ctx.db.one("SELECT id FROM users WHERE email=?", (email,))
    if existing:
        raise conflict("Já existe um usuário com este e-mail.")
    uid = create_user_record(ctx.db, name, email, ctx.body["password"], "admin", is_teacher=bool(ctx.body.get("is_teacher", True)))
    ctx.user = ctx.db.one("SELECT * FROM users WHERE id=?", (uid,))
    audit(ctx, "criar", "users", uid, f"Primeiro administrador: {name}")
    return {"ok": True}


# ------------------------------------------------------------------ usuários / integrantes
USER_FIELDS = {
    "name": F("text", "Nome", required=True, max=120),
    "email": F("email", "E-mail"),
    "role": F("enum", "Perfil de acesso", required=True, options=ROLES, filter=True),
    "is_teacher": F("bool", "Professor técnico"),
    "area_id": F("ref", "Área", ref="areas", filter=True),
    "position": F("text", "Função no time", max=120),
    "phone": F("text", "Telefone", max=40),
    "active": F("bool", "Ativo", default=1),
}
_USER_REFS = (
    ("activities", "owner_id", "atividade(s) sob sua responsabilidade", "deleted_at IS NULL"),
    ("activities", "created_by", "atividade(s) criadas", "deleted_at IS NULL"),
    ("raci", "user_id", "atribuição(ões) na matriz RACI"),
    ("approvals", "user_id", "registro(s) de aprovação"),
    ("risks", "owner_id", "risco(s)", "deleted_at IS NULL"),
    ("comments", "author_id", "comentário(s)"),
    ("movements", "created_by", "movimento(s) financeiro(s)"),
    ("evidence_members", "user_id", "evidência(s) de monitoramento"),
    ("stakeholders", "owner_id", "stakeholder(s)", "deleted_at IS NULL"),
    ("wbs_elements", "owner_id", "elemento(s) da EAP", "deleted_at IS NULL"),
    ("events", "owner_id", "evento(s)", "deleted_at IS NULL"),
)


def _ser_user(ctx, u):
    out = {"id": u["id"], "name": u["name"], "role": u["role"], "is_teacher": bool(u["is_teacher"]),
           "area_id": u["area_id"], "position": u["position"], "active": bool(u["active"]),
           "has_login": bool(u["password_hash"]), "version": u["version"]}
    if ctx.role in INTERNAL or judge_view(ctx)["contacts"]:
        out["email"], out["phone"] = u["email"], u["phone"]
    if ctx.role == "admin":
        out["last_login_at"], out["must_change_password"] = u["last_login_at"], bool(u["must_change_password"])
        out["locked"] = bool(u["locked_until"] and u["locked_until"] > datetime.now(timezone.utc).isoformat())
    return out


@route("GET", "/api/users")
def list_users(ctx):
    rows = ctx.db.q("SELECT * FROM users WHERE deleted_at IS NULL ORDER BY active DESC, name COLLATE NOCASE")
    if ctx.role == "juiz":
        rows = [r for r in rows if r["role"] != "juiz" and r["active"]]
    return {"items": [_ser_user(ctx, r) for r in rows]}


def _count_active_admins(db, exclude=None):
    return db.val("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND deleted_at IS NULL AND password_hash IS NOT NULL AND id<>?", (exclude or "",))


def _parse_user(ctx, body, existing=None):
    data = {}
    for name, f in USER_FIELDS.items():
        if name in body:
            data[name] = f.parse(ctx.db, name, body[name])
        elif existing is None and f.required:
            raise bad(f"{f.label}: campo obrigatório.", {name: "Campo obrigatório."})
    return data


@route("POST", "/api/users", allow=("admin", "gestor"))
def create_user(ctx):
    data = _parse_user(ctx, ctx.body)
    password = ctx.body.get("password") or None
    if ctx.role != "admin":
        # Gestor só cadastra integrantes sem privilégios e sem credenciais (menor privilégio).
        if data.get("role") != "membro" or password or data.get("is_teacher"):
            raise forbidden("Somente o administrador pode criar contas com acesso, definir perfis ou marcar o professor técnico.")
    if password:
        if not data.get("email"):
            raise bad("Informe o e-mail para criar uma conta com acesso.", {"email": "Obrigatório para contas com acesso."})
        check_password_policy(password)
    if data.get("role") in ("admin", "gestor", "juiz") and not password:
        raise bad("Este perfil exige e-mail e senha inicial.", {"password": "Informe uma senha inicial."})
    if data.get("email") and ctx.db.one("SELECT 1 FROM users WHERE email=?", (data["email"],)):
        raise conflict("Já existe um usuário com este e-mail.", fields={"email": "E-mail já cadastrado."})
    ts = now_iso()
    uid = new_id()
    ctx.db.insert("users", {"id": uid, **data, "password_hash": hash_password(password) if password else None,
                            "must_change_password": 1 if password else 0, "created_at": ts, "updated_at": ts, "created_by": ctx.uid})
    u = ctx.db.one("SELECT * FROM users WHERE id=?", (uid,))
    audit(ctx, "criar", "users", uid, f"Usuário {u['name']} ({u['role']})", after={k: u[k] for k in USER_FIELDS})
    return _ser_user(ctx, u)


@route("PUT", "/api/users/{id}", allow=("admin", "gestor"))
def update_user(ctx):
    u = ctx.db.one("SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (ctx.params["id"],))
    if not u:
        raise not_found("Usuário")
    data = _parse_user(ctx, ctx.body, u)
    if ctx.role != "admin":
        if u["role"] != "membro" or any(k in data and data[k] != u[k] for k in ("role", "is_teacher", "active", "email")):
            raise forbidden("Somente o administrador pode alterar perfis, e-mails de acesso, ativação ou a marcação de professor técnico.")
    if u["id"] == ctx.uid and (("role" in data and data["role"] != u["role"]) or ("active" in data and not data["active"])):
        raise forbidden("Você não pode alterar o próprio perfil de acesso nem desativar a própria conta.")
    will_admin = data.get("role", u["role"]) == "admin" and data.get("active", u["active"])
    if u["role"] == "admin" and u["active"] and not will_admin and _count_active_admins(ctx.db, u["id"]) == 0:
        raise conflict("É necessário manter pelo menos um administrador ativo.")
    if data.get("email") and ctx.db.one("SELECT 1 FROM users WHERE email=? AND id<>?", (data["email"], u["id"])):
        raise conflict("Já existe um usuário com este e-mail.", fields={"email": "E-mail já cadastrado."})
    if data.get("role", u["role"]) in ("admin", "gestor", "juiz") and not u["password_hash"]:
        raise bad("Defina uma senha para este usuário antes de atribuir um perfil com acesso.")
    version = ctx.body.get("version")
    if not isinstance(version, int):
        raise bad("Informe a versão do registro (version).", code="versao_obrigatoria")
    if data:
        sets = ", ".join(f"{k}=?" for k in data)
        cur = ctx.db.ex(f"UPDATE users SET {sets}, updated_at=?, version=version+1 WHERE id=? AND version=?",
                        list(data.values()) + [now_iso(), u["id"], version])
        if cur.rowcount == 0:
            raise conflict("Este usuário foi alterado por outra pessoa. Recarregue antes de salvar.", code="conflito_versao")
    new = ctx.db.one("SELECT * FROM users WHERE id=?", (u["id"],))
    if not new["active"] or (new["role"] != u["role"]):
        ctx.db.ex("DELETE FROM sessions WHERE user_id=?", (u["id"],))  # desativação/troca de perfil encerra as sessões
    audit(ctx, "alterar", "users", u["id"], f"Usuário {new['name']}", before={k: u[k] for k in USER_FIELDS}, after={k: new[k] for k in USER_FIELDS})
    return _ser_user(ctx, new)


@route("POST", "/api/users/{id}/senha", allow=("admin",))
def admin_set_password(ctx):
    u = ctx.db.one("SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (ctx.params["id"],))
    if not u:
        raise not_found("Usuário")
    if not u["email"]:
        raise bad("Cadastre um e-mail para este usuário antes de definir a senha.")
    pw = ctx.body.get("password")
    check_password_policy(pw)
    ctx.db.ex("UPDATE users SET password_hash=?, must_change_password=1, failed_logins=0, locked_until=NULL, updated_at=?, version=version+1 WHERE id=?",
              (hash_password(pw), now_iso(), u["id"]))
    ctx.db.ex("DELETE FROM sessions WHERE user_id=?", (u["id"],))
    audit(ctx, "definir_senha", "users", u["id"], f"Senha provisória definida para {u['name']}")
    return {"ok": True}


@route("POST", "/api/users/{id}/link-redefinicao", allow=("admin",))
def admin_reset_link(ctx):
    u = ctx.db.one("SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (ctx.params["id"],))
    if not u:
        raise not_found("Usuário")
    if not u["email"]:
        raise bad("Cadastre um e-mail para este usuário antes de gerar o link.")
    if not u["password_hash"]:
        # conta ainda sem senha: cria um hash inutilizável para habilitar o login após a redefinição
        ctx.db.ex("UPDATE users SET password_hash=? WHERE id=?", ("scrypt$15$8$1$AAAA$AAAA", u["id"]))
    token = _make_reset(ctx.db, u["id"], ctx.uid)
    audit(ctx, "gerar_link_reset", "users", u["id"], f"Link de redefinição gerado para {u['name']}")
    return {"token": token, "path": f"/#/redefinir/{token}", "expires_minutes": 60}


@route("DELETE", "/api/users/{id}", allow=("admin",))
def delete_user(ctx):
    u = ctx.db.one("SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (ctx.params["id"],))
    if not u:
        raise not_found("Usuário")
    if u["id"] == ctx.uid:
        raise forbidden("Você não pode excluir a própria conta.")
    if u["role"] == "admin" and u["active"] and _count_active_admins(ctx.db, u["id"]) == 0:
        raise conflict("É necessário manter pelo menos um administrador ativo.")
    refs = []
    for table, col, label, *cond in _USER_REFS:
        n = ctx.db.val(f"SELECT COUNT(*) FROM {table} WHERE {col}=?" + (f" AND {cond[0]}" if cond else ""), (u["id"],))
        if n:
            refs.append({"label": label, "count": n})
    if refs:
        raise conflict("Este usuário possui histórico vinculado (" + ", ".join(f"{r['count']} {r['label']}" for r in refs) +
                       "). Para preservar o histórico, desative-o em vez de excluir.", code="vinculos_existentes", references=refs, can_archive=True)
    ctx.db.ex("DELETE FROM sessions WHERE user_id=?", (u["id"],))
    ctx.db.ex("UPDATE users SET deleted_at=?, active=0, email=NULL, password_hash=NULL, version=version+1 WHERE id=?", (now_iso(), u["id"]))
    audit(ctx, "excluir", "users", u["id"], f"Usuário {u['name']}")
    return {"ok": True}
