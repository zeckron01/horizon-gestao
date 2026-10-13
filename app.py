"""Aplicação ASGI: despachante da API (autenticação, CSRF, permissões, transações, idempotência) e arquivos estáticos."""
import asyncio
import hmac
import json
import logging
import secrets
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import APP_VERSION
from .config import cfg
from .core import ApiError, Ctx, Raw, ROLE_LABELS, RESOURCES, get_setting, match_route, route
from .db import NotConfigured, acquire, get_db, migrate, now_iso, release

log = logging.getLogger("horizon")
STATIC = Path(__file__).parent / "static"
_setup_token = None


def setup_token():
    """Token de instalação: variável de ambiente ou valor aleatório impresso no log enquanto não houver administrador."""
    global _setup_token
    if cfg.SETUP_TOKEN:
        return cfg.SETUP_TOKEN
    if _setup_token is None:
        _setup_token = secrets.token_urlsafe(12)
    return _setup_token


def _client_ip(request):
    if cfg.TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def _is_https(request):
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def _json(payload, status=200):
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


def _error(e: ApiError):
    return _json({"error": {"code": e.code, "message": e.message, **e.extra}}, e.status)


def _run(r, params, request_info, body, files):
    """Executa o handler dentro de uma transação (quando há escrita). Roda em thread do pool."""
    from .auth import COOKIE, SetCookie, load_session
    db = acquire()
    ctx = Ctx(db, None, params, request_info["query"], body, files, request_info["ip"], request_info["method"], request_info["path"])
    ctx.ua = request_info["ua"]
    ctx.authorization = request_info.get("authorization")
    ctx.session = None
    q0 = getattr(db, "queries", 0)
    db.scache = {}
    try:
        if r["tx"]:
            db.begin()
        if not r["public"]:
            user, session = load_session(db, request_info["cookie"])
            if not user:
                raise ApiError(401, "nao_autenticado", "Sessão expirada ou inválida. Entre novamente.")
            ctx.user, ctx.session = user, session
            if request_info["method"] != "GET":
                # CSRF: token por sessão, enviado em cabeçalho (não é acessível a outros sites).
                if not hmac.compare_digest(request_info["csrf"] or "", session["csrf"]):
                    raise ApiError(403, "csrf", "Requisição recusada (token de segurança ausente ou inválido). Recarregue a página.")
                if user["role"] == "juiz" and not r["judge_ok"]:
                    from .core import audit
                    audit(ctx, "tentativa_bloqueada", "permissao", None, f"Juiz tentou {request_info['method']} {request_info['path']}")
                    db.commit()
                    raise ApiError(403, "somente_leitura", "O perfil de juiz possui acesso somente de leitura.")
            if user["role"] not in r["allow"]:
                raise ApiError(403, "sem_permissao", "Você não tem permissão para realizar esta operação.")
        idem = request_info["idem"]
        if idem and r["tx"] and ctx.user and request_info["method"] == "POST":
            prev = db.one("SELECT * FROM idempotency WHERE key=? AND user_id=?", (idem, ctx.uid))
            if prev:
                db.rollback()
                return json.loads(prev["response"]), prev["status"], None
        result = r["fn"](ctx)
        cookie = None
        if isinstance(result, SetCookie):
            cookie, result = result, result.payload
        if idem and r["tx"] and ctx.user and request_info["method"] == "POST" and not isinstance(result, Raw):
            db.ex("INSERT OR IGNORE INTO idempotency (key, user_id, path, status, response, created_at) VALUES (?,?,?,?,?,?)",
                  (idem, ctx.uid, request_info["path"], 200, json.dumps(result, ensure_ascii=False, default=str), now_iso()))
        db.commit()
        request_info["db_queries"] = getattr(db, "queries", 0) - q0
        return result, 200, cookie
    except BaseException:
        db.rollback()
        raise
    finally:
        db.scache = None
        release(db)


async def api(request):
    from .auth import COOKIE
    method, path = request.method, request.url.path
    try:
        r, params = match_route(method, path)
        body, files = {}, []
        ctype = request.headers.get("content-type", "")
        clen = int(request.headers.get("content-length") or 0)
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            if ctype.startswith("multipart/form-data"):
                if clen > (cfg.MAX_UPLOAD_MB * 4 + 1) * 1024 * 1024:
                    raise ApiError(413, "arquivo_grande", f"Envio acima do limite permitido ({cfg.MAX_UPLOAD_MB} MB por arquivo).")
                form = await request.form()
                for key, val in form.multi_items():
                    if hasattr(val, "filename"):
                        data = await val.read()
                        files.append({"field": key, "name": val.filename or "arquivo", "mime": val.content_type or "application/octet-stream", "data": data})
                    else:
                        body[key] = val
            else:
                if clen > 3 * 1024 * 1024:
                    raise ApiError(413, "corpo_grande", "Conteúdo enviado acima do limite permitido.")
                raw = await request.body()
                if len(raw) > 3 * 1024 * 1024:
                    raise ApiError(413, "corpo_grande", "Conteúdo enviado acima do limite permitido.")
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        raise ApiError(400, "json_invalido", "O conteúdo enviado não é um JSON válido.")
                    if not isinstance(body, dict):
                        raise ApiError(400, "json_invalido", "O conteúdo enviado deve ser um objeto JSON.")
        info = {"method": method, "path": path, "query": dict(request.query_params), "ip": _client_ip(request),
                "ua": request.headers.get("user-agent"), "authorization": request.headers.get("authorization"), "cookie": request.cookies.get(COOKIE),
                "csrf": request.headers.get("x-csrf-token"), "idem": (request.headers.get("idempotency-key") or "")[:100] or None}
        if not _started:
            await run_in_threadpool(startup)
        result, status, cookie = await run_in_threadpool(_run, r, params, info, body, files)
        if isinstance(result, Raw):
            headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
            if result.filename:
                from urllib.parse import quote
                disp = "inline" if result.inline else "attachment"
                headers["Content-Disposition"] = f"{disp}; filename*=UTF-8''{quote(result.filename)}"
            return Response(result.data, media_type=result.mime, headers=headers)
        resp = _json(result, status)
        if info.get("db_queries"):
            resp.headers["X-DB-Roundtrips"] = str(info["db_queries"])   # diagnóstico: idas ao banco nesta requisição
        if cookie is not None:
            if cookie.clear:
                resp.delete_cookie(COOKIE, path="/")
            else:
                secure = _is_https(request) if cfg.COOKIE_SECURE == "auto" else cfg.COOKIE_SECURE == "1"
                resp.set_cookie(COOKIE, cookie.token, max_age=cfg.SESSION_HOURS * 3600, httponly=True, samesite="lax", secure=secure, path="/")
        return resp
    except ApiError as e:
        return _error(e)
    except Exception as e:  # erro inesperado: registra e devolve mensagem genérica (sem detalhes internos)
        import sqlite3
        from . import db as dbm, pg
        if isinstance(e, NotConfigured):
            return _error(ApiError(503, "banco_nao_configurado", str(e)))
        if isinstance(e, pg.OperationalError):
            log.error("Banco de dados indisponível em %s %s: %s", method, path, e)
            return _error(ApiError(503, "banco_indisponivel", "Não foi possível falar com o banco de dados. Nada foi salvo; tente novamente em instantes."))
        if isinstance(e, dbm.IntegrityError):
            log.warning("Violação de integridade em %s %s: %s", method, path, e)
            return _error(ApiError(409, "integridade", "Operação recusada: os dados violam uma regra de integridade do banco (registro duplicado ou vínculo inválido)."))
        if isinstance(e, sqlite3.OperationalError) and "locked" in str(e):
            return _error(ApiError(503, "ocupado", "O servidor está ocupado. Nada foi salvo; tente novamente em instantes."))
        log.error("Erro inesperado em %s %s%s\n%s", method, path, f" [SQL: {e.sql}]" if getattr(e, "sql", None) else "", traceback.format_exc())
        return _error(ApiError(500, "erro_interno", "Erro interno. A operação não foi concluída e nada foi salvo."))


@route("GET", "/api/me")
def me(ctx):
    from .auth import public_user
    db, u = ctx.db, ctx.user
    keys = ["team_name", "team_subtitle", "timezone", "date_format", "currency", "default_season_id", "theme", "alerts",
            "risk_scale", "catalogs", "evidence_weights", "custom_fields", "governance", "report_templates", "uploads"]
    settings = {k: get_setting(db, k) for k in keys}
    settings["judge_view"] = get_setting(db, "judge_view")
    return {
        "user": public_user(u), "csrf": ctx.session["csrf"], "version": APP_VERSION,
        "settings": settings,
        "prefs": {"ui": get_setting(db, "ui", u["id"]), "notify": get_setting(db, "notify", u["id"])},
        "seasons": db.q("SELECT id, name, year, start_date, end_date, archived_at FROM seasons WHERE deleted_at IS NULL ORDER BY year DESC, name"),
        "areas": db.q("SELECT id, name, color, sort, archived_at, logo_file_id FROM areas WHERE deleted_at IS NULL ORDER BY sort, name"),
        "role_labels": ROLE_LABELS,
        "integrations": {"email": cfg.smtp_configured, "push": False},
        "unread": db.val("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (u["id"],)),
        "meta": {name: res.meta() for name, res in RESOURCES.items()},
    }


async def health(request):
    def chk():
        startup()
        db = acquire()
        try:
            return db.val("SELECT COUNT(*) FROM schema_migrations"), db.dialect
        finally:
            release(db)
    try:
        n, dialect = await run_in_threadpool(chk)
    except Exception as e:
        log.error("Falha na verificação de saúde: %s", e)
        msg = str(e) if isinstance(e, NotConfigured) else "Banco de dados indisponível."
        return JSONResponse({"ok": False, "version": APP_VERSION, "error": msg}, status_code=503)
    return JSONResponse({"ok": True, "version": APP_VERSION, "migrations": n, "database": "postgresql" if dialect == "pg" else "sqlite"})


async def spa(request):
    index = STATIC / "index.html"
    if not index.exists():
        return Response("Interface não compilada. Execute: npm install && npm run build (pasta web/).", status_code=503)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send2(message):
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                h += [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                      (b"referrer-policy", b"same-origin"),
                      (b"content-security-policy", b"default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                                                   b"script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")]
            await send(message)

        await self.app(scope, receive, send2)


_started = False
_start_lock = threading.Lock()


def startup():
    """Prepara o banco (migrações e dados-base). Idempotente; roda na subida do servidor ou, em ambiente
    sem processo contínuo (Vercel), na primeira requisição de cada instância."""
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        from .auth import has_admin
        db = acquire()
        try:
            applied = migrate(db)
            if applied:
                log.info("Migrações aplicadas: %s", ", ".join(applied))
            _bootstrap()
            admin = has_admin(db)
        finally:
            release(db)
        if not admin:
            if cfg.SETUP_TOKEN or not cfg.SERVERLESS:
                log.warning("Nenhum administrador cadastrado. Acesse a aplicação e use o token de instalação "
                            "(ou crie pelo terminal: python manage.py criar-admin).")
                if not cfg.SETUP_TOKEN:
                    print(f"\n>>> TOKEN DE INSTALAÇÃO: {setup_token()}\n", flush=True)
            else:
                log.warning("Nenhum administrador cadastrado e HORIZON_SETUP_TOKEN não definido: defina a variável para liberar a instalação.")
        _started = True


@asynccontextmanager
async def lifespan(app):
    from . import jobs
    await run_in_threadpool(startup)
    stop = threading.Event()
    if cfg.JOBS_ENABLED and not cfg.SERVERLESS:
        threading.Thread(target=jobs.loop, args=(stop,), daemon=True, name="horizon-jobs").start()
    yield
    stop.set()


def _bootstrap():
    from .modules.admin import ensure_base_data
    db = get_db()
    if db.one("SELECT 1 FROM settings WHERE key='bootstrapped'"):
        return  # já preparado: evita uma transação de escrita a cada início de instância
    db.begin()
    try:
        ensure_base_data(db)
        db.commit()
    except BaseException:
        db.rollback()
        raise


# registra as rotas dos módulos
from . import auth, jobs  # noqa: E402,F401
from .modules import (activities, admin, comments_files, dashboard, events, evidences, finance, importer,  # noqa: E402,F401
                      notifications, reports, risks, scope, search, stakeholders, wbs)

app = Starlette(
    routes=[
        Route("/healthz", health),
        Route("/api/{rest:path}", api, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        Mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static"),
        Route("/{rest:path}", spa),
    ],
    lifespan=lifespan,
)
app = SecurityHeaders(app)
