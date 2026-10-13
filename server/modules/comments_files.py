"""Comentários (com menções) e anexos protegidos por autorização. Os arquivos ficam no banco, nunca em pasta pública."""
import hashlib
import json

from ..config import cfg
from ..core import (INTERNAL, ROLES, STAFF, ApiError, F, Raw, audit, bad, forbidden, get_setting, judge_view, not_found, notify, route, user_names)
from ..db import new_id, now_iso

ENTITIES = {"activities": ("activities", "title"), "risks": ("risks", "title"), "evidence_entries": ("evidence_entries", None),
            "events": ("events", "name"), "stakeholders": ("stakeholders", "name"), "expenses": ("expenses", "name"),
            "incomes": ("incomes", "description"), "wbs": ("wbs_elements", "name"), "scopes": ("scopes", None),
            "branding": (None, None), "areas": ("areas", "name")}
JUDGE_MODULE = {"activities": "planejamento", "risks": "riscos", "evidence_entries": "evidencias", "events": "calendario",
                "stakeholders": "stakeholders", "expenses": "financeiro", "incomes": "financeiro", "wbs": "eap", "scopes": "escopo"}
INLINE = {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}
MIME_BY_EXT = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
               "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xls": "application/vnd.ms-excel", "csv": "text/csv",
               "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "doc": "application/msword",
               "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "ppt": "application/vnd.ms-powerpoint",
               "txt": "text/plain", "zip": "application/zip", "mp4": "video/mp4"}
MAGIC = {"pdf": [b"%PDF"], "png": [b"\x89PNG"], "jpg": [b"\xff\xd8"], "jpeg": [b"\xff\xd8"], "gif": [b"GIF8"], "webp": [b"RIFF"],
         "xlsx": [b"PK"], "docx": [b"PK"], "pptx": [b"PK"], "zip": [b"PK"]}


def _entity(db, et, eid):
    if et not in ENTITIES:
        raise bad("Tipo de registro inválido.")
    table, title = ENTITIES[et]
    if table is None:
        return {"id": eid}
    soft = " AND deleted_at IS NULL" if et not in ("evidence_entries", "scopes") else ""
    row = db.one(f"SELECT * FROM {table} WHERE id=?{soft}", (eid,))
    if not row:
        raise not_found("Registro vinculado")
    return row


def _member_can_write(ctx, et, row):
    if ctx.role in STAFF:
        return True
    if ctx.role != "membro":
        return False
    if et == "activities":
        from .activities import is_assignee
        return is_assignee(ctx.db, row, ctx.uid)
    if et == "evidence_entries":
        return row["area_id"] == ctx.user["area_id"] or bool(ctx.db.one("SELECT 1 FROM evidence_members WHERE entry_id=? AND user_id=?", (row["id"], ctx.uid)))
    return False


# ----------------------------------------------------------------------------- comentários
@route("GET", "/api/comments")
def list_comments(ctx):
    if ctx.role == "juiz":
        return {"items": []}  # comentários são comunicação interna da equipe
    et, eid = ctx.query.get("entity_type"), ctx.query.get("entity_id")
    _entity(ctx.db, et, eid)
    names = user_names(ctx.db)
    rows = ctx.db.q("SELECT * FROM comments WHERE entity_type=? AND entity_id=? ORDER BY created_at", (et, eid))
    return {"items": [{"id": c["id"], "author_id": c["author_id"], "author_name": names.get(c["author_id"]), "created_at": c["created_at"],
                       "deleted": bool(c["deleted_at"]), "body": None if c["deleted_at"] else c["body"],
                       "mentions": [] if c["deleted_at"] else json.loads(c["mentions"] or "[]")} for c in rows]}


@route("POST", "/api/comments", allow=INTERNAL)
def add_comment(ctx):
    db = ctx.db
    et, eid = ctx.body.get("entity_type"), ctx.body.get("entity_id")
    if et not in ("activities", "risks", "evidence_entries", "events", "stakeholders", "expenses", "incomes", "wbs"):
        raise bad("Comentários não estão disponíveis para este tipo de registro.")
    row = _entity(db, et, eid)
    body = F("long", "Comentário", required=True, max=5000).parse(db, "body", ctx.body.get("body"))
    names = user_names(db)
    mentions = [u for u in dict.fromkeys(ctx.body.get("mentions") or []) if isinstance(u, str) and u in names][:30]
    c = {"id": new_id(), "entity_type": et, "entity_id": eid, "author_id": ctx.uid, "body": body, "mentions": json.dumps(mentions), "created_at": now_iso()}
    db.insert("comments", c)
    title = row.get(ENTITIES[et][1]) if ENTITIES[et][1] else "registro de evidência"
    for u in mentions:
        if u != ctx.uid:
            notify(db, u, "mencao", f"{ctx.user['name']} mencionou você em “{title}”", body[:300], et, eid, f"mention:{c['id']}:{u}")
    if et == "activities" and row.get("owner_id") and row["owner_id"] not in mentions + [ctx.uid]:
        notify(db, row["owner_id"], "mencao", f"Novo comentário em “{title}”", f"{ctx.user['name']}: {body[:280]}", et, eid, f"comment:{c['id']}")
    audit(ctx, "comentar", et, eid, f"Comentário em “{title}”")
    return {"id": c["id"], "author_id": ctx.uid, "author_name": ctx.user["name"], "created_at": c["created_at"], "deleted": False, "body": body, "mentions": mentions}


@route("DELETE", "/api/comments/{id}", allow=INTERNAL)
def delete_comment(ctx):
    c = ctx.db.one("SELECT * FROM comments WHERE id=? AND deleted_at IS NULL", (ctx.params["id"],))
    if not c:
        raise not_found("Comentário")
    if c["author_id"] != ctx.uid and ctx.role not in STAFF:
        raise forbidden("Você só pode remover os próprios comentários.")
    ctx.db.ex("UPDATE comments SET deleted_at=?, deleted_by=? WHERE id=?", (now_iso(), ctx.uid, c["id"]))  # o texto original permanece no banco/auditoria
    audit(ctx, "remover_comentario", c["entity_type"], c["entity_id"], "Comentário removido", before={"body": c["body"], "author_id": c["author_id"]})
    return {"ok": True}


# ----------------------------------------------------------------------------- anexos
def _ser_file(f, names=None):
    out = {k: f[k] for k in ("id", "entity_type", "entity_id", "name", "mime", "size", "created_at", "uploaded_by")}
    out["judge_visible"] = bool(f["judge_visible"])
    if names:
        out["uploaded_by_name"] = names.get(f["uploaded_by"])
    return out


def validate_upload(db, name, data):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    allowed = get_setting(db, "uploads")["allowed_ext"]
    if ext not in allowed:
        raise bad(f"Tipo de arquivo não permitido (.{ext or '?'}). Tipos aceitos: {', '.join(allowed)}.", code="tipo_nao_permitido")
    if len(data) == 0:
        raise bad("O arquivo está vazio.")
    if len(data) > cfg.MAX_UPLOAD_MB * 1024 * 1024:
        raise ApiError(413, "arquivo_grande", f"Arquivo acima do limite de {cfg.MAX_UPLOAD_MB} MB.")
    if ext in MAGIC and not any(data.startswith(m) for m in MAGIC[ext]):
        raise bad("O conteúdo do arquivo não corresponde à extensão informada.", code="conteudo_invalido")
    return ext, MIME_BY_EXT.get(ext, "application/octet-stream")


def store_file(ctx, et, eid, name, data, judge_visible=False):
    ext, mime = validate_upload(ctx.db, name, data)
    safe = "".join(c for c in name if c.isprintable() and c not in '\\/:*?"<>|')[:180] or f"arquivo.{ext}"
    f = {"id": new_id(), "entity_type": et, "entity_id": eid, "name": safe, "mime": mime, "size": len(data),
         "sha256": hashlib.sha256(data).hexdigest(), "data": data, "judge_visible": 1 if judge_visible else 0,
         "uploaded_by": ctx.uid, "created_at": now_iso()}
    ctx.db.insert("files", f)
    return f


@route("GET", "/api/files")
def list_files(ctx):
    et, eid = ctx.query.get("entity_type"), ctx.query.get("entity_id")
    _entity(ctx.db, et, eid)
    rows = ctx.db.q("SELECT id, entity_type, entity_id, name, mime, size, created_at, uploaded_by, judge_visible FROM files WHERE entity_type=? AND entity_id=? AND deleted_at IS NULL ORDER BY created_at", (et, eid))
    if ctx.role == "juiz":
        mod = JUDGE_MODULE.get(et)
        if not mod or not judge_view(ctx)["modules"].get(mod):
            return {"items": []}
        rows = [r for r in rows if r["judge_visible"]]
    return {"items": [_ser_file(r, user_names(ctx.db)) for r in rows]}


@route("POST", "/api/files", allow=INTERNAL)
def upload_file(ctx):
    et, eid = ctx.body.get("entity_type"), ctx.body.get("entity_id")
    row = _entity(ctx.db, et, eid)
    if et in ("branding", "areas") and ctx.role != "admin":
        raise forbidden("Somente o administrador altera imagens da identidade visual.")
    if not _member_can_write(ctx, et, row):
        raise forbidden("Você não pode anexar arquivos a este registro.")
    if not ctx.files:
        raise bad("Nenhum arquivo enviado.")
    if et in ("branding", "areas") and not ctx.files[0]["name"].lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        raise bad("Use uma imagem PNG, JPG ou WebP.")
    out = []
    for up in ctx.files[:10]:
        f = store_file(ctx, et, eid, up["name"], up["data"], judge_visible=str(ctx.body.get("judge_visible")) in ("1", "true") and ctx.role in STAFF)
        audit(ctx, "anexar", et, eid, f"Arquivo anexado: {f['name']} ({f['size']} bytes)")
        out.append(_ser_file(f))
    if et == "activities":
        from .activities import content_changed
        content_changed(ctx, eid, "Anexo adicionado após a aprovação")
    return {"items": out}


def _load_file(ctx, fid, with_data=True):
    cols = "*" if with_data else "id, entity_type, entity_id, name, mime, size, created_at, uploaded_by, judge_visible"
    f = ctx.db.one(f"SELECT {cols} FROM files WHERE id=? AND deleted_at IS NULL", (fid,))
    if not f:
        raise not_found("Arquivo")
    return f


@route("GET", "/api/files/{id}")
def download_file(ctx):
    f = _load_file(ctx, ctx.params["id"])
    if f["entity_type"] == "import" and ctx.role != "admin":
        raise forbidden("Os arquivos originais de importação são restritos ao administrador.")
    if ctx.role == "juiz" and f["entity_type"] not in ("branding", "areas"):
        mod = JUDGE_MODULE.get(f["entity_type"])
        if not f["judge_visible"] or not mod or not judge_view(ctx)["modules"].get(mod):
            raise forbidden("Este documento não está liberado para o perfil de juiz.")
    return Raw(bytes(f["data"]), f["mime"], f["name"], inline=f["mime"] in INLINE)


@route("PUT", "/api/files/{id}")
def update_file(ctx):
    f = _load_file(ctx, ctx.params["id"], False)
    ctx.db.ex("UPDATE files SET judge_visible=? WHERE id=?", (1 if ctx.body.get("judge_visible") else 0, f["id"]))
    audit(ctx, "alterar", "files", f["id"], f"{f['name']} — visível para juízes: {'sim' if ctx.body.get('judge_visible') else 'não'}")
    return _ser_file(_load_file(ctx, f["id"], False))


@route("DELETE", "/api/files/{id}", allow=INTERNAL)
def delete_file(ctx):
    f = _load_file(ctx, ctx.params["id"], False)
    if f["uploaded_by"] != ctx.uid and ctx.role not in STAFF:
        raise forbidden("Você só pode remover arquivos que enviou.")
    ctx.db.ex("UPDATE files SET deleted_at=? WHERE id=?", (now_iso(), f["id"]))
    audit(ctx, "remover_anexo", f["entity_type"], f["entity_id"], f"Arquivo removido: {f['name']}")
    if f["entity_type"] == "activities":
        from .activities import content_changed
        content_changed(ctx, f["entity_id"], "Anexo removido após a aprovação")
    return {"ok": True}


@route("GET", "/api/public/marca", public=True)
def public_branding(ctx):
    """Dados mínimos de identidade visual para a tela de acesso (sem informações do projeto)."""
    theme = get_setting(ctx.db, "theme")
    return {"team_name": get_setting(ctx.db, "team_name"), "team_subtitle": get_setting(ctx.db, "team_subtitle"),
            "primary": theme["primary"], "logo_file_id": theme.get("logo_file_id")}


@route("GET", "/api/public/marca/logo", public=True)
def public_logo(ctx):
    fid = get_setting(ctx.db, "theme").get("logo_file_id")
    f = ctx.db.one("SELECT * FROM files WHERE id=? AND entity_type='branding' AND deleted_at IS NULL", (fid,)) if fid else None
    if not f:
        raise not_found("Logo")
    return Raw(bytes(f["data"]), f["mime"], f["name"], inline=True)
