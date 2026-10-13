"""Gerenciamento de riscos: pontuação = probabilidade × impacto, faixas configuráveis, reavaliações e ações de mitigação."""
from ..core import (F, Resource, audit, bad, get_setting, judge_module_guard, not_found, route, today_local, user_names)
from ..db import new_id, now_iso

RISK_STATUS = ("aberto", "em_tratamento", "monitorado", "ocorrido", "encerrado")
RISK_STATUS_LABEL = {"aberto": "Aberto", "em_tratamento": "Em tratamento", "monitorado": "Monitorado", "ocorrido": "Ocorrido", "encerrado": "Encerrado"}
OPEN = ("aberto", "em_tratamento", "monitorado", "ocorrido")


def level_of(scale, score):
    if score is None:
        return None
    for b in scale["bands"]:
        if score <= b["max"]:
            return b["name"]
    return scale["bands"][-1]["name"]


class Risks(Resource):
    def validate(self, ctx, r, old):
        scale = get_setting(ctx.db, "risk_scale")
        for k, lab in (("probability", "Probabilidade"), ("impact", "Impacto"), ("residual_probability", "Probabilidade residual"), ("residual_impact", "Impacto residual")):
            if r.get(k) is not None and not 1 <= r[k] <= scale["size"]:
                raise bad(f"{lab}: informe um valor de 1 a {scale['size']}.", {k: "Fora da escala configurada."})
        if not r.get("code"):
            n = ctx.db.val("SELECT COUNT(*) FROM risks WHERE season_id=?", (r["season_id"],)) + 1
            while ctx.db.one("SELECT 1 FROM risks WHERE season_id=? AND code=?", (r["season_id"], f"R-{n:03d}")):
                n += 1
            r["code"] = f"R-{n:03d}"
        elif ctx.db.one("SELECT 1 FROM risks WHERE season_id=? AND code=? AND id<>?", (r["season_id"], r["code"], (old or {}).get("id", ""))):
            raise bad("Já existe um risco com este código nesta temporada.", {"code": "Código duplicado."})
        if not old and not r.get("identified_at"):
            r["identified_at"] = today_local(ctx.db)
        if old and any(r.get(k) != old.get(k) for k in ("probability", "impact", "residual_probability", "residual_impact")):
            r["last_review_at"] = today_local(ctx.db)

    def after_save(self, ctx, r, old):
        db, b = ctx.db, ctx.body
        if isinstance(b.get("area_ids"), list):
            db.ex("DELETE FROM risk_areas WHERE risk_id=?", (r["id"],))
            for a in dict.fromkeys(b["area_ids"]):
                if db.one("SELECT 1 FROM areas WHERE id=?", (a,)):
                    db.ex("INSERT INTO risk_areas (risk_id, area_id) VALUES (?,?)", (r["id"], a))
        for key, typ, table in (("activity_ids", "activity", "activities"), ("wbs_ids", "wbs", "wbs_elements")):
            if isinstance(b.get(key), list):
                db.ex("DELETE FROM risk_links WHERE risk_id=? AND entity_type=?", (r["id"], typ))
                for i in dict.fromkeys(b[key]):
                    if db.one(f"SELECT 1 FROM {table} WHERE id=? AND deleted_at IS NULL", (i,)):
                        db.ex("INSERT INTO risk_links (risk_id, entity_type, entity_id) VALUES (?,?,?)", (r["id"], typ, i))
        if old is None or any(r.get(k) != old.get(k) for k in ("probability", "impact", "residual_probability", "residual_impact")):
            if r.get("probability") or r.get("impact"):
                db.insert("risk_reviews", {"id": new_id(), "risk_id": r["id"], "at": now_iso(), "user_id": ctx.uid,
                                           "probability": r["probability"], "impact": r["impact"],
                                           "residual_probability": r["residual_probability"], "residual_impact": r["residual_impact"],
                                           "note": (b.get("review_note_text") or ("Avaliação inicial" if old is None else "Reavaliação"))[:1000]})

    def expand(self, ctx, rows):
        return expand_risks(ctx.db, rows)


RISK = Risks("risks", "risks", "Risco", {
    "season_id": F("ref", "Temporada", required=True, ref="seasons"),
    "code": F("text", "Código", max=20),
    "title": F("text", "Título", required=True, max=200),
    "description": F("long", "Descrição"),
    "category": F("text", "Categoria", max=80, filter=True),
    "cause": F("long", "Causa provável", max=4000),
    "risk_event": F("long", "Evento de risco", max=4000),
    "consequence": F("long", "Consequência", max=4000),
    "probability": F("int", "Probabilidade", min=1, max=10),
    "impact": F("int", "Impacto", min=1, max=10),
    "owner_id": F("ref", "Responsável pelo risco", ref="users", filter=True),
    "preventive_plan": F("long", "Plano preventivo", max=4000),
    "contingency_plan": F("long", "Plano de contingência", max=4000),
    "mitigation": F("long", "Ações de mitigação (resumo)", max=4000),
    "action_deadline": F("date", "Prazo para executar as ações"),
    "warning_indicators": F("long", "Indicadores de alerta", max=4000),
    "status": F("enum", "Situação", options=RISK_STATUS, default="aberto", filter=True),
    "identified_at": F("date", "Data de identificação"),
    "last_review_at": F("date", "Data da última avaliação"),
    "residual_probability": F("int", "Probabilidade residual", min=1, max=10),
    "residual_impact": F("int", "Impacto residual", min=1, max=10),
    "occurrence_result": F("long", "Resultado após a ocorrência", max=4000),
    "notes": F("long", "Observações"),
}, season=True, order="code", search=("title", "description", "code", "cause", "consequence"))


def expand_risks(db, rows):
    if not rows:
        return rows
    scale = get_setting(db, "risk_scale")
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    areas, links, actions = {}, {}, {}
    for a in db.q(f"SELECT * FROM risk_areas WHERE risk_id IN ({marks})", ids):
        areas.setdefault(a["risk_id"], []).append(a["area_id"])
    for l in db.q(f"SELECT * FROM risk_links WHERE risk_id IN ({marks})", ids):
        links.setdefault((l["risk_id"], l["entity_type"]), []).append(l["entity_id"])
    for a in db.q(f"SELECT * FROM risk_actions WHERE risk_id IN ({marks}) ORDER BY COALESCE(due_date,'9999'), created_at", ids):
        actions.setdefault(a["risk_id"], []).append(a)
    for r in rows:
        p, i = r.get("probability"), r.get("impact")
        r["score"] = p * i if p and i else None
        r["level"] = level_of(scale, r["score"])
        rp, ri = r.get("residual_probability"), r.get("residual_impact")
        r["residual_score"] = rp * ri if rp and ri else None
        r["residual_level"] = level_of(scale, r["residual_score"])
        r["area_ids"] = areas.get(r["id"], [])
        r["activity_ids"] = links.get((r["id"], "activity"), [])
        r["wbs_ids"] = links.get((r["id"], "wbs"), [])
        r["actions"] = actions.get(r["id"], [])
        open_actions = [a for a in r["actions"] if a["status"] in ("pendente", "em_andamento")]
        r["no_owner"] = not r.get("owner_id")
        r["no_plan"] = not ((r.get("mitigation") or "").strip() or (r.get("preventive_plan") or "").strip() or r["actions"])
        r["open_actions"] = len(open_actions)
        r["level_divergence"] = bool(r.get("original_level") and r["level"] and r["original_level"].strip().lower() != r["level"].lower())
    return rows


@route("GET", "/api/risks")
def list_risks(ctx):
    judge_module_guard(ctx, "riscos")
    res = RISK.list(ctx)
    q = ctx.query
    if q.get("area_id"):
        res["items"] = [r for r in res["items"] if q["area_id"] in r["area_ids"]]
    res["items"].sort(key=lambda r: (-(r["score"] or 0), r["code"]))
    return res


@route("GET", "/api/risks/{id}")
def get_risk(ctx):
    judge_module_guard(ctx, "riscos")
    r = RISK.get(ctx, ctx.params["id"])
    names = user_names(ctx.db)
    r["reviews"] = [dict(x, user_name=names.get(x["user_id"])) for x in ctx.db.q("SELECT * FROM risk_reviews WHERE risk_id=? ORDER BY at DESC", (r["id"],))]
    return r


RISK.register(methods=("create", "update", "delete", "archive"))

ACTION_FIELDS = {
    "description": F("long", "Ação", required=True, max=2000),
    "owner_id": F("ref", "Responsável", ref="users"),
    "due_date": F("date", "Prazo"),
    "status": F("enum", "Situação", options=("pendente", "em_andamento", "concluida", "cancelada"), default="pendente"),
}


@route("POST", "/api/risks/{id}/acoes")
def add_action(ctx):
    r = RISK.raw(ctx.db, ctx.params["id"])
    if not r:
        raise not_found("Risco")
    data = {k: f.parse(ctx.db, k, ctx.body.get(k, f.default)) for k, f in ACTION_FIELDS.items()}
    ts = now_iso()
    row = {"id": new_id(), "risk_id": r["id"], **data, "done_at": ts if data["status"] == "concluida" else None, "created_at": ts, "updated_at": ts, "created_by": ctx.uid}
    ctx.db.insert("risk_actions", row)
    audit(ctx, "criar", "risk_actions", row["id"], f"{r['code']} — {data['description'][:80]}", after=row)
    return RISK.get(ctx, r["id"])


@route("PUT", "/api/risk-actions/{id}")
def update_action(ctx):
    a = ctx.db.one("SELECT * FROM risk_actions WHERE id=?", (ctx.params["id"],))
    if not a:
        raise not_found("Ação")
    data = {k: f.parse(ctx.db, k, ctx.body[k]) for k, f in ACTION_FIELDS.items() if k in ctx.body}
    if data.get("status") == "concluida" and a["status"] != "concluida":
        data["done_at"] = now_iso()
    elif "status" in data and data["status"] != "concluida":
        data["done_at"] = None
    if data:
        sets = ", ".join(f"{k}=?" for k in data)
        ctx.db.ex(f"UPDATE risk_actions SET {sets}, updated_at=?, version=version+1 WHERE id=?", list(data.values()) + [now_iso(), a["id"]])
    new = ctx.db.one("SELECT * FROM risk_actions WHERE id=?", (a["id"],))
    audit(ctx, "alterar", "risk_actions", a["id"], a["description"][:80], before=a, after=new)
    return RISK.get(ctx, a["risk_id"])


@route("DELETE", "/api/risk-actions/{id}")
def delete_action(ctx):
    a = ctx.db.one("SELECT * FROM risk_actions WHERE id=?", (ctx.params["id"],))
    if not a:
        raise not_found("Ação")
    ctx.db.ex("DELETE FROM risk_actions WHERE id=?", (a["id"],))
    audit(ctx, "excluir", "risk_actions", a["id"], a["description"][:80], before=a)
    return RISK.get(ctx, a["risk_id"])
