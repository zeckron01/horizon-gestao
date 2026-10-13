"""Stakeholders e patrocínios. O valor recebido é sempre derivado dos movimentos de caixa; o apoio não financeiro,
dos itens recebidos por patrocínio/doação vinculados. O valor confirmado gera uma receita "a receber" (nunca caixa)."""
from ..core import (F, INTERNAL, Resource, audit, bad, check_order, conflict, from_cents, judge_module_guard, judge_view, not_found, route, to_cents)
from ..db import new_id, now_iso
from .finance import INC, month_range

ST_STATUS = ("prospectado", "primeiro_contato", "em_negociacao", "proposta_enviada", "aguardando_resposta",
             "confirmado", "parcialmente_confirmado", "recusado", "concluido")
ST_LABEL = {"prospectado": "Prospectado", "primeiro_contato": "Primeiro contato realizado", "em_negociacao": "Em negociação",
            "proposta_enviada": "Proposta enviada", "aguardando_resposta": "Aguardando resposta", "confirmado": "Confirmado",
            "parcialmente_confirmado": "Parcialmente confirmado", "recusado": "Recusado", "concluido": "Concluído"}
WON = ("confirmado", "parcialmente_confirmado", "concluido")
PROPOSED = ("proposta_enviada", "aguardando_resposta", "confirmado", "parcialmente_confirmado", "recusado", "concluido")


class Stakeholders(Resource):
    def validate(self, ctx, s, old):
        check_order(s, "first_contact_date", "confirmation_date", "Data do primeiro contato", "Data da confirmação")
        if s["status"] == "parcialmente_confirmado" and s.get("requested_cents") and s.get("confirmed_cents") is not None \
                and s["confirmed_cents"] >= s["requested_cents"]:
            raise bad("Em uma confirmação parcial, o valor confirmado deve ser menor que o valor solicitado.", {"confirmed": "Deve ser menor que o solicitado."})
        if s["status"] not in WON and s.get("confirmed_cents"):
            raise bad("Informe valor confirmado apenas quando a negociação estiver confirmada (total ou parcialmente).", {"confirmed": "Negociação ainda não confirmada."})

    def after_save(self, ctx, s, old):
        sync_income(ctx, s)

    def before_delete(self, ctx, s):
        n = ctx.db.val("SELECT COUNT(*) FROM movements m JOIN incomes i ON m.origin_type='income' AND m.origin_id=i.id WHERE i.stakeholder_id=?", (s["id"],))
        if n:
            raise conflict("Este stakeholder possui recebimentos registrados. Arquive-o para preservar o histórico financeiro.", code="vinculos_existentes", can_archive=True)
        # receita automática sem movimentos é removida junto
        ctx.db.ex("UPDATE incomes SET deleted_at=? WHERE stakeholder_id=? AND import_source LIKE 'stakeholder:auto%' AND deleted_at IS NULL", (now_iso(), s["id"]))

    def expand(self, ctx, rows):
        return expand_stakeholders(ctx, rows)


ST = Stakeholders("stakeholders", "stakeholders", "Stakeholder", {
    "season_id": F("ref", "Temporada", required=True, ref="seasons"),
    "name": F("text", "Nome ou razão social", required=True, max=200),
    "type": F("text", "Tipo de stakeholder", max=80, filter=True),
    "contact_name": F("text", "Pessoa de contato", max=160),
    "email": F("email", "E-mail"),
    "phone": F("text", "Telefone", max=40),
    "purpose": F("long", "Objetivo do relacionamento", max=4000),
    "need": F("long", "Necessidade atendida", max=4000),
    "area_id": F("ref", "Área beneficiada", ref="areas", filter=True),
    "contribution_type": F("text", "Tipo de contribuição", max=60, filter=True),
    "requested": F("money", "Valor solicitado"),
    "confirmed": F("money", "Valor confirmado"),
    "level": F("text", "Nível de patrocínio", max=60, filter=True),
    "first_contact_date": F("date", "Data do primeiro contato"),
    "proposal_date": F("date", "Data da proposta"),
    "confirmation_date": F("date", "Data da confirmação"),
    "expected_receipt_date": F("date", "Data prevista para recebimento"),
    "status": F("enum", "Situação da negociação", options=ST_STATUS, default="prospectado", filter=True),
    "owner_id": F("ref", "Responsável pelo relacionamento", ref="users", filter=True),
    "next_action": F("long", "Próxima ação", max=2000),
    "notes": F("long", "Observações"),
}, season=True, order="name COLLATE NOCASE", search=("name", "contact_name", "purpose", "need"),
    refs=(("expenses", "stakeholder_id", "despesa(s)/item(ns) vinculados", "deleted_at IS NULL"),
          ("incomes", "stakeholder_id", "receita(s) vinculada(s)", "deleted_at IS NULL AND (import_source IS NULL OR import_source NOT LIKE 'stakeholder:auto%')")))


def sync_income(ctx, s):
    """Mantém a receita 'a receber' correspondente ao valor confirmado do stakeholder (fonte única, sem duplicar valores)."""
    db = ctx.db
    auto = db.one("SELECT * FROM incomes WHERE stakeholder_id=? AND import_source LIKE 'stakeholder:auto%' AND deleted_at IS NULL", (s["id"],))
    want = s["confirmed_cents"] if s["status"] in WON and s["confirmed_cents"] else 0
    received = db.val("SELECT COALESCE(SUM(amount_cents),0) FROM movements WHERE origin_type='income' AND origin_id=?", (auto["id"],)) if auto else 0
    ts = now_iso()
    if want and not auto:
        db.insert("incomes", {"id": new_id(), "season_id": s["season_id"], "description": f"Patrocínio — {s['name']}"[:200], "purpose": s["need"],
                              "source": "Patrocínio", "stakeholder_id": s["id"], "area_id": s["area_id"], "expected_date": s["expected_receipt_date"],
                              "expected_cents": want, "status": "confirmada", "import_source": "stakeholder:auto",
                              "created_at": ts, "updated_at": ts, "created_by": ctx.uid})
    elif auto and want:
        if want < received:
            raise bad("O valor confirmado não pode ser menor que o valor já recebido deste patrocinador.", {"confirmed": "Menor que o valor recebido."})
        db.ex("UPDATE incomes SET expected_cents=?, expected_date=?, area_id=?, status='confirmada', description=?, updated_at=?, version=version+1 WHERE id=?",
              (want, s["expected_receipt_date"], s["area_id"], f"Patrocínio — {s['name']}"[:200], ts, auto["id"]))
    elif auto and not want:
        if received:
            raise bad("Este patrocinador já possui recebimentos registrados. Estorne os recebimentos antes de remover o valor confirmado.")
        db.ex("UPDATE incomes SET status='cancelada', updated_at=?, version=version+1 WHERE id=?", (ts, auto["id"]))


def expand_stakeholders(ctx, rows):
    if not rows:
        return rows
    db = ctx.db
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    rec = {r["sid"]: r["t"] for r in db.q(
        f"SELECT i.stakeholder_id sid, COALESCE(SUM(m.amount_cents),0) t FROM movements m JOIN incomes i ON m.origin_type='income' AND m.origin_id=i.id "
        f"WHERE i.stakeholder_id IN ({marks}) AND i.deleted_at IS NULL GROUP BY i.stakeholder_id", ids)}
    kind = {r["sid"]: r for r in db.q(
        f"SELECT stakeholder_id sid, COALESCE(SUM(COALESCE(total_actual_cents, total_planned_cents, 0)),0) t, COUNT(*) n, "
        f"SUM(CASE WHEN COALESCE(total_actual_cents, total_planned_cents, 0)=0 THEN 1 ELSE 0 END) unvalued "
        f"FROM expenses WHERE stakeholder_id IN ({marks}) AND is_cash=0 AND status<>'cancelada' AND deleted_at IS NULL AND archived_at IS NULL GROUP BY stakeholder_id", ids)}
    cps = {}
    for c in db.q(f"SELECT * FROM counterparts WHERE stakeholder_id IN ({marks}) ORDER BY COALESCE(due_date,'9999'), created_at", ids):
        cps.setdefault(c["stakeholder_id"], []).append(c)
    hide = ctx.role == "juiz" and not judge_view(ctx)["contacts"]
    for r in rows:
        r["received"] = from_cents(rec.get(r["id"], 0))
        k = kind.get(r["id"])
        r["in_kind_value"] = from_cents(k["t"]) if k else 0.0
        r["in_kind_items"] = k["n"] if k else 0
        r["in_kind_unvalued"] = k["unvalued"] if k else 0
        conf = to_cents(r.get("confirmed")) or 0
        r["to_receive"] = from_cents(max(0, conf - rec.get(r["id"], 0))) if r["status"] in WON else 0.0
        r["counterparts"] = cps.get(r["id"], [])
        r["counterparts_pending"] = sum(1 for c in r["counterparts"] if c["status"] == "pendente")
        if hide:
            for k2 in ("contact_name", "email", "phone"):
                r[k2] = None
    return rows


@route("GET", "/api/stakeholders")
def list_stakeholders(ctx):
    judge_module_guard(ctx, "stakeholders")
    return ST.list(ctx)


ST.register(methods=("get", "create", "update", "delete", "archive"))

CP_FIELDS = {
    "kind": F("text", "Tipo de contrapartida", max=80),
    "description": F("long", "Descrição", required=True, max=2000),
    "due_date": F("date", "Prazo"),
    "status": F("enum", "Situação", options=("pendente", "cumprida", "cancelada"), default="pendente"),
    "done_date": F("date", "Data do cumprimento"),
    "record_note": F("long", "Registro do cumprimento (o que foi feito, onde, link/evidência)", max=4000),
}


def _cp_check(data):
    if data.get("status") == "cumprida" and not (data.get("done_date") and (data.get("record_note") or "").strip()):
        raise bad("Uma contrapartida só pode ser marcada como cumprida com o registro correspondente (data e descrição do que foi realizado).",
                  {"record_note": "Registro obrigatório.", "done_date": "Data obrigatória."}, code="registro_obrigatorio")


@route("POST", "/api/stakeholders/{id}/contrapartidas")
def add_counterpart(ctx):
    s = ST.raw(ctx.db, ctx.params["id"])
    if not s:
        raise not_found("Stakeholder")
    data = {k: f.parse(ctx.db, k, ctx.body.get(k, f.default)) for k, f in CP_FIELDS.items()}
    _cp_check(data)
    ts = now_iso()
    row = {"id": new_id(), "stakeholder_id": s["id"], **data, "created_at": ts, "updated_at": ts, "created_by": ctx.uid}
    ctx.db.insert("counterparts", row)
    audit(ctx, "criar", "counterparts", row["id"], f"{s['name']} — {data['description'][:80]}", after=row)
    return ST.get(ctx, s["id"])


@route("PUT", "/api/counterparts/{id}")
def update_counterpart(ctx):
    c = ctx.db.one("SELECT * FROM counterparts WHERE id=?", (ctx.params["id"],))
    if not c:
        raise not_found("Contrapartida")
    data = {k: f.parse(ctx.db, k, ctx.body[k]) for k, f in CP_FIELDS.items() if k in ctx.body}
    merged = {**c, **data}
    _cp_check(merged)
    if data:
        sets = ", ".join(f"{k}=?" for k in data)
        ctx.db.ex(f"UPDATE counterparts SET {sets}, updated_at=?, version=version+1 WHERE id=?", list(data.values()) + [now_iso(), c["id"]])
    new = ctx.db.one("SELECT * FROM counterparts WHERE id=?", (c["id"],))
    audit(ctx, "alterar", "counterparts", c["id"], c["description"][:80], before=c, after=new)
    return ST.get(ctx, c["stakeholder_id"])


@route("DELETE", "/api/counterparts/{id}")
def delete_counterpart(ctx):
    c = ctx.db.one("SELECT * FROM counterparts WHERE id=?", (ctx.params["id"],))
    if not c:
        raise not_found("Contrapartida")
    ctx.db.ex("DELETE FROM counterparts WHERE id=?", (c["id"],))
    audit(ctx, "excluir", "counterparts", c["id"], c["description"][:80], before=c)
    return ST.get(ctx, c["stakeholder_id"])


def stakeholder_analysis(ctx, season_id):
    db = ctx.db
    rows = expand_stakeholders(ctx, [ST.ser(r) for r in db.q("SELECT * FROM stakeholders WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (season_id,))])
    areas = {a["id"]: a["name"] for a in db.q("SELECT id, name FROM areas")}
    proposed = [r for r in rows if r["status"] in PROPOSED or r.get("proposal_date")]
    won = [r for r in rows if r["status"] in WON]
    sponsors = [r for r in won if (r.get("type") or "Patrocinador") in ("Patrocinador", "Apoiador") or r.get("confirmed") or r["in_kind_items"]]
    tot = lambda k: round(sum((r.get(k) or 0) for r in rows), 2)  # noqa: E731

    def grp(keyf, val="received"):
        g = {}
        for r in rows:
            k = keyf(r) or "(não informado)"
            g[k] = round(g.get(k, 0) + (r.get(val) or 0), 2)
        return [{"key": k, "value": v} for k, v in sorted(g.items(), key=lambda kv: -kv[1])]
    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    by_month = {}
    for m in db.q("SELECT m.date, m.amount_cents FROM movements m JOIN incomes i ON m.origin_type='income' AND m.origin_id=i.id "
                  "WHERE m.season_id=? AND i.stakeholder_id IS NOT NULL AND i.deleted_at IS NULL", (season_id,)):
        by_month[m["date"][:7]] = by_month.get(m["date"][:7], 0) + m["amount_cents"]
    months = month_range(min(by_month), max(by_month)) if by_month else []
    return {
        "total": len(rows), "confirmed_count": len(won), "sponsors_confirmed": len(sponsors),
        "proposals": len(proposed), "conversion_pct": round(len([r for r in proposed if r["status"] in WON]) * 100 / len(proposed), 1) if proposed else None,
        "requested": tot("requested"), "confirmed": round(sum((r.get("confirmed") or 0) for r in won), 2), "received": tot("received"),
        "in_kind_value": tot("in_kind_value"), "in_kind_unvalued": sum(r["in_kind_unvalued"] for r in rows),
        "counterparts_pending": sum(r["counterparts_pending"] for r in rows),
        "by_status": [{"key": k, "label": ST_LABEL[k], "value": by_status.get(k, 0)} for k in ST_STATUS if by_status.get(k)],
        "received_by_month": [{"key": mo, "value": from_cents(by_month.get(mo, 0))} for mo in months],
        "received_by_area": [x for x in grp(lambda r: areas.get(r["area_id"])) if x["value"]],
        "confirmed_by_level": [x for x in grp(lambda r: r.get("level"), "confirmed") if x["value"]],
        "received_by_level": [x for x in grp(lambda r: r.get("level")) if x["value"]],
        "formulas": {"Taxa de conversão": "stakeholders confirmados (total ou parcialmente) ÷ stakeholders que receberam proposta",
                     "Valor recebido": "soma dos recebimentos registrados no caixa vinculados ao stakeholder",
                     "Apoio não financeiro": "valor estimado dos itens/serviços recebidos por patrocínio ou doação vinculados ao stakeholder"},
    }


@route("GET", "/api/stakeholders-analise")
def analysis(ctx):
    judge_module_guard(ctx, "stakeholders")
    if not ctx.query.get("season_id"):
        raise bad("Informe a temporada.")
    return stakeholder_analysis(ctx, ctx.query["season_id"])
