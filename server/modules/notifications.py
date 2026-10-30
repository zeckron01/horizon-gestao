"""Central de notificações internas e preferências individuais."""
from ..i18n import _t, tx
from ..config import cfg
from ..core import INTERNAL, ROLES, USER_SCOPED, audit, bad, get_setting, not_found, route, set_setting
from ..db import now_iso
from ..mailer import send_mail

KIND_LABEL = {"prazo": _t("Entrega próxima do prazo"), "atraso": _t("Entrega atrasada"), "bloqueada": _t("Atividade bloqueada"),
              "dependencia": _t("Dependência em risco"), "aprovacao": _t("Aprovações"), "evento": _t("Eventos"), "risco": _t("Riscos críticos"),
              "financeiro": _t("Financeiro"), "mencao": _t("Menções em comentários"), "atribuicao": _t("Atribuições"), "raci": _t("Responsabilidades (RACI)"),
              "atraso_registrado": _t("Atrasos registrados"), "sistema": _t("Sistema")}


@route("GET", "/api/notifications", allow=INTERNAL)
def list_notifications(ctx):
    from ..jobs import run_if_stale
    run_if_stale(ctx.db)
    conds, ps = ["user_id=?"], [ctx.uid]
    if ctx.query.get("unread") == "1":
        conds.append("read_at IS NULL")
    if ctx.query.get("kind"):
        conds.append("kind=?")
        ps.append(ctx.query["kind"])
    lim, off = ctx.qint("limit", 50, 1, 200), ctx.qint("offset", 0, 0)
    w = " AND ".join(conds)
    items = ctx.db.q(f"SELECT id, kind, title, body, entity_type, entity_id, created_at, read_at FROM notifications WHERE {w} ORDER BY created_at DESC LIMIT ? OFFSET ?", ps + [lim, off])
    for n in items:   # avisos são gravados em português pela rotina; aqui só a apresentação muda de idioma
        n["title"], n["body"] = tx(n["title"]), tx(n["body"])
    return {"items": items,
            "total": ctx.db.val(f"SELECT COUNT(*) FROM notifications WHERE {w}", ps),
            "unread": ctx.db.val("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (ctx.uid,)),
            "kinds": KIND_LABEL}


@route("POST", "/api/notifications/{id}/lida", allow=INTERNAL)
def mark_read(ctx):
    cur = ctx.db.ex("UPDATE notifications SET read_at=COALESCE(read_at, ?) WHERE id=? AND user_id=?", (now_iso(), ctx.params["id"], ctx.uid))
    if cur.rowcount == 0:
        raise not_found(_t("Notificação"))
    return {"ok": True, "unread": ctx.db.val("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (ctx.uid,))}


@route("POST", "/api/notifications/ler-todas", allow=INTERNAL)
def mark_all_read(ctx):
    ctx.db.ex("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (now_iso(), ctx.uid))
    return {"ok": True, "unread": 0}


@route("PUT", "/api/prefs/{key}", allow=ROLES, judge_ok=True)
def save_pref(ctx):
    key = ctx.params["key"]
    if key not in USER_SCOPED:
        raise bad(_t("Preferência desconhecida."))
    val = ctx.body.get("value")
    if not isinstance(val, dict):
        raise bad(_t("Valor inválido."))
    if key == "ui":
        fs = val.get("font_scale", 100)
        if not isinstance(fs, int) or not 80 <= fs <= 160:
            raise bad(_t("Tamanho de fonte inválido (80% a 160%)."))
        if val.get("mode") not in (None, "claro", "escuro", "auto"):
            raise bad(_t("Tema inválido."))
        if val.get("lang") not in (None, "pt-BR", "en"):
            raise bad(_t("Idioma inválido."))
        val = {"mode": val.get("mode"), "font_scale": fs, "large_font": bool(val.get("large_font")), "reduce_motion": bool(val.get("reduce_motion")),
               "lang": val.get("lang") or "pt-BR"}
    else:
        val = {"email": bool(val.get("email")), "kinds": {k: bool(v) for k, v in (val.get("kinds") or {}).items() if k in KIND_LABEL}}
    set_setting(ctx.db, key, val, user_id=ctx.uid, by=ctx.uid)
    return {"value": get_setting(ctx.db, key, ctx.uid)}


@route("POST", "/api/admin/email-teste", allow=("admin",), tx=False)
def email_test(ctx):
    """Envia um e-mail real de teste ao próprio administrador. Só afirma sucesso se o servidor SMTP aceitou a mensagem."""
    if not cfg.smtp_configured:
        return {"ok": False, "message": _t("SMTP não configurado. Defina SMTP_HOST e SMTP_FROM (ver .env.example).")}
    if not ctx.user["email"]:
        return {"ok": False, "message": _t("Seu usuário não possui e-mail cadastrado.")}
    ok, err = send_mail(ctx.user["email"], _t("Teste de e-mail — Horizon Escuderia"), _t("Este é um e-mail de teste da plataforma de gestão da Horizon Escuderia."))
    return {"ok": ok, "message": _t("Mensagem aceita pelo servidor SMTP. Confira a caixa de entrada.") if ok else _t("Falha no envio: {0}", err)}
