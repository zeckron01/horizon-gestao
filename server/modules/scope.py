"""Escopo do projeto: documento estruturado com versões, análise crítica e solicitações de alteração."""
from ..i18n import _t
import json
import re
import unicodedata

from ..core import (F, Resource, audit, bad, conflict, fmt_date, forbidden, from_cents, get_setting, judge_module_guard, not_found,
                    route, today_local, user_names)
from ..db import new_id, now_iso

TEXT_KEYS = {"project_name": 300, "team_name": 200, "manager_name": 200, "teacher_name": 200, "general_objective": 4000,
             "justification": 8000, "limits": 8000, "approval_criteria": 4000, "schedule_reference": 2000, "budget_reference": 2000,
             "main_risks": 8000, "main_stakeholders": 8000}
LIST_KEYS = ("expected_results", "included", "excluded", "acceptance_criteria", "assumptions", "constraints", "approvals_needed")
SECTION_LABELS = {
    "project_name": _t("Nome do projeto"), "team_name": _t("Nome da equipe"), "manager_name": _t("Responsável pela gestão"), "teacher_name": _t("Professor técnico"),
    "date": _t("Data de elaboração"), "general_objective": _t("Objetivo geral"), "specific_objectives": _t("Objetivos específicos"), "justification": _t("Justificativa"),
    "expected_results": _t("Resultados esperados"), "deliverables": _t("Entregas"), "limits": _t("Limites do projeto"), "included": _t("O que está incluído"),
    "excluded": _t("O que não está incluído"), "acceptance_criteria": _t("Critérios de aceitação"), "approval_criteria": _t("Critérios de aprovação"),
    "schedule_reference": _t("Cronograma de referência"), "budget_reference": _t("Orçamento de referência"), "assumptions": _t("Premissas"),
    "constraints": _t("Restrições"), "main_risks": _t("Riscos principais"), "main_stakeholders": _t("Stakeholders principais"), "approvals_needed": _t("Aprovações necessárias"),
}


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def clean_scope(db, raw):
    """Valida e normaliza o documento de escopo enviado pelo cliente."""
    if not isinstance(raw, dict):
        raise bad(_t("Documento de escopo inválido."))
    out = {}
    for k, mx in TEXT_KEYS.items():
        v = raw.get(k)
        out[k] = F("long", SECTION_LABELS[k], max=mx).parse(db, k, v) if v else None
    out["date"] = F("date", _t("Data de elaboração")).parse(db, "date", raw.get("date"))
    for k in LIST_KEYS:
        v = raw.get(k) or []
        if not isinstance(v, list) or len(v) > 200:
            raise bad(_t("{0}: lista inválida.", SECTION_LABELS[k]))
        out[k] = [str(x).strip()[:2000] for x in v if str(x).strip()]
    objs = raw.get("specific_objectives") or []
    if not isinstance(objs, list) or len(objs) > 100:
        raise bad(_t("Objetivos específicos: lista inválida."))
    out["specific_objectives"] = []
    for o in objs:
        if isinstance(o, str):
            o = {"text": o}
        if isinstance(o, dict) and str(o.get("text") or "").strip():
            out["specific_objectives"].append({"id": str(o.get("id") or new_id())[:40], "text": str(o["text"]).strip()[:2000]})
    obj_ids = {o["id"] for o in out["specific_objectives"]}
    area_ids = {a["id"] for a in db.q("SELECT id FROM areas")}
    dels = raw.get("deliverables") or []
    if not isinstance(dels, list) or len(dels) > 500:
        raise bad(_t("Entregas: lista inválida."))
    out["deliverables"] = []
    for x in dels:
        if isinstance(x, dict) and str(x.get("text") or "").strip():
            out["deliverables"].append({
                "id": str(x.get("id") or new_id())[:40], "text": str(x["text"]).strip()[:500],
                "area_id": x.get("area_id") if x.get("area_id") in area_ids else None, "main": bool(x.get("main")),
                "objective_id": x.get("objective_id") if x.get("objective_id") in obj_ids else None,
                "acceptance": str(x.get("acceptance") or "").strip()[:2000] or None})
    return out


def references(db, season_id):
    """Dados vivos dos outros módulos exibidos como referência no escopo (sem duplicar cadastro)."""
    from .finance import finance_summary
    from .risks import expand_risks, RISK
    a = db.one("SELECT MIN(COALESCE(planned_start, planned_end)) s, MAX(planned_end) e, COUNT(*) n, SUM(is_milestone) m FROM activities "
               "WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status<>'cancelada'", (season_id,))
    fin = finance_summary(db, season_id)
    risks = expand_risks(db, [RISK.ser(r) for r in db.q("SELECT * FROM risks WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (season_id,))])
    risks.sort(key=lambda r: -(r["score"] or 0))
    sts = db.q("SELECT name, status FROM stakeholders WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status IN ('confirmado','parcialmente_confirmado','concluido') ORDER BY name", (season_id,))
    return {"schedule": {"start": a["s"], "end": a["e"], "activities": a["n"], "milestones": a["m"] or 0},
            "budget": {"planned_total": fin["budget"]["planned_total"], "planned_items": fin["budget"]["planned_items"], "goal": fin["funding"]["goal"]},
            "top_risks": [{"id": r["id"], "code": r["code"], "title": r["title"], "level": r["level"], "score": r["score"]} for r in risks[:8]],
            "stakeholders": [s["name"].strip() for s in sts]}


def analyze(db, season_id, data):
    """Análise crítica do escopo: aponta lacunas e divergências com cronograma, riscos e orçamento."""
    issues = []

    def add(code, level, message, items=None):
        issues.append({"code": code, "level": level, "message": message, "items": items or []})
    data = data or {}
    dels = data.get("deliverables") or []
    linked = {x.get("objective_id") for x in dels if x.get("objective_id")}
    orphan = [o["text"] for o in data.get("specific_objectives") or [] if o["id"] not in linked]
    if orphan:
        add("objetivo_sem_entrega", "aviso", _t("{0} objetivo(s) específico(s) sem entregas associadas.", len(orphan)), orphan)
    if not (data.get("specific_objectives") or []):
        add("sem_objetivos", "info", _t("Nenhum objetivo específico cadastrado no escopo."))
    noacc = [x["text"] for x in dels if not x.get("acceptance")]
    if noacc and not (data.get("acceptance_criteria") or []):
        add("entrega_sem_criterio", "aviso", _t("{0} entrega(s) sem critério de aceitação (e nenhum critério geral definido).", len(noacc)), noacc[:40])
    elif noacc:
        add("entrega_sem_criterio", "info", _t("{0} entrega(s) sem critério de aceitação específico (há critérios gerais).", len(noacc)), noacc[:40])
    acts = db.q("SELECT id, title, owner_id, planned_end, planned_start, status FROM activities WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status<>'cancelada'", (season_id,))
    no_owner = [a["title"] for a in acts if not a["owner_id"]]
    if no_owner:
        add("atividade_sem_responsavel", "aviso", _t("{0} atividade(s) sem responsável principal.", len(no_owner)), no_owner[:40])
    no_due = [a["title"] for a in acts if not a["planned_end"]]
    if no_due:
        add("entrega_sem_prazo", "aviso", _t("{0} atividade(s)/entrega(s) sem prazo planejado.", len(no_due)), no_due[:40])
    risks = db.q("SELECT r.code, r.title, r.mitigation, r.preventive_plan, (SELECT COUNT(*) FROM risk_actions x WHERE x.risk_id=r.id) n FROM risks r "
                 "WHERE r.season_id=? AND r.deleted_at IS NULL AND r.archived_at IS NULL AND r.status<>'encerrado'", (season_id,))
    nomit = [f"{r['code']} — {r['title']}" for r in risks if not ((r["mitigation"] or "").strip() or (r["preventive_plan"] or "").strip() or r["n"])]
    if nomit:
        add("risco_sem_mitigacao", "aviso", _t("{0} risco(s) sem ações de mitigação.", len(nomit)), nomit)
    titles = [norm(a["title"]) for a in acts]

    def has_activity(text):
        t = re.sub(r"^\d+\s*o?\s+", "", norm(text))
        return any(t and (t in x or x in t) for x in titles if x)
    missing = [x["text"] for x in dels if not has_activity(x["text"])]
    if missing:
        add("escopo_sem_cronograma", "aviso", _t("{0} entrega(s) do escopo sem atividade correspondente no cronograma (comparação pelo nome).", len(missing)), missing[:60])
    season = db.one("SELECT * FROM seasons WHERE id=?", (season_id,))
    if season and season["end_date"]:
        late = [a["title"] for a in acts if a["planned_end"] and a["planned_end"] > season["end_date"]]
        if late:
            add("cronograma_fora_temporada", "aviso", _t("{0} atividade(s) com prazo após o fim da temporada ({1}).", len(late), fmt_date(season['end_date'])), late[:40])
    ref = references(db, season_id)
    b = ref["budget"]
    if b["planned_total"] and b["planned_items"] > b["planned_total"]:
        add("orcamento_excedido", "erro", _t("Os itens planejados (R$ {0}) superam o orçamento total planejado (R$ {1}).", format(b['planned_items'], '.2f'), format(b['planned_total'], '.2f')))
    m = re.search(r"R\$\s*([\d\.\s]+(?:,\d{2})?)", (data.get("budget_reference") or "") + " " + (data.get("main_risks") or ""))
    if m and b["planned_total"]:
        try:
            v = float(m.group(1).replace(" ", "").replace(".", "").replace(",", "."))
            if abs(v - b["planned_total"]) > 0.009:
                add("orcamento_divergente", "aviso", _t("O escopo menciona R$ {0}, mas o orçamento total planejado no módulo financeiro é R$ {1}.", format(v, '.2f'), format(b['planned_total'], '.2f')))
        except ValueError:
            pass
    crs = db.q("SELECT title, status FROM change_requests WHERE season_id=? AND deleted_at IS NULL AND status IN ('aberta','aprovada')", (season_id,))
    pend = [c["title"] for c in crs if c["status"] == "aprovada"]
    if pend:
        add("alteracao_nao_implementada", "aviso", _t("{0} alteração(ões) de escopo aprovada(s) ainda não refletida(s) no planejamento.", len(pend)), pend)
    opened = [c["title"] for c in crs if c["status"] == "aberta"]
    if opened:
        add("alteracao_aberta", "info", _t("{0} solicitação(ões) de alteração aguardando decisão.", len(opened)), opened)
    return issues


def _ser(db, s):
    names = user_names(db)
    return {"id": s["id"], "season_id": s["season_id"], "data": json.loads(s["data"]), "doc_version": s["doc_version"], "version": s["version"],
            "needs_review": bool(s["needs_review"]), "review_note": s["review_note"], "updated_at": s["updated_at"], "updated_by_name": names.get(s["updated_by"] or s["created_by"])}


@route("GET", "/api/scope")
def get_scope(ctx):
    judge_module_guard(ctx, "escopo")
    db, sid = ctx.db, ctx.query.get("season_id")
    if not sid:
        raise bad(_t("Informe a temporada."))
    s = db.one("SELECT * FROM scopes WHERE season_id=?", (sid,))
    names = user_names(db)
    versions = [dict(v, created_by_name=names.get(v["created_by"])) for v in
                db.q("SELECT id, doc_version, note, created_by, created_at FROM scope_versions WHERE scope_id=? ORDER BY doc_version DESC", (s["id"],))] if s else []
    data = json.loads(s["data"]) if s else None
    return {"scope": _ser(db, s) if s else None, "versions": versions, "references": references(db, sid),
            "analysis": analyze(db, sid, data), "labels": SECTION_LABELS}


@route("PUT", "/api/scope")
def save_scope(ctx):
    db, sid = ctx.db, ctx.body.get("season_id")
    if not db.one("SELECT 1 FROM seasons WHERE id=? AND deleted_at IS NULL", (sid,)):
        raise bad(_t("Temporada inválida."))
    data = clean_scope(db, ctx.body.get("data"))
    note = F("text", _t("Resumo da alteração"), max=300).parse(db, "note", ctx.body.get("note"))
    s = db.one("SELECT * FROM scopes WHERE season_id=?", (sid,))
    ts = now_iso()
    payload = json.dumps(data, ensure_ascii=False)
    if s is None:
        sid_ = new_id()
        db.insert("scopes", {"id": sid_, "season_id": sid, "data": payload, "doc_version": 1, "created_at": ts, "updated_at": ts, "created_by": ctx.uid, "updated_by": ctx.uid})
        db.insert("scope_versions", {"id": new_id(), "scope_id": sid_, "doc_version": 1, "data": payload, "note": note or "Versão inicial", "created_by": ctx.uid, "created_at": ts})
        audit(ctx, "criar", "scopes", sid_, _t("Escopo do projeto — versão 1"), after=data)
    else:
        if not isinstance(ctx.body.get("version"), int):
            raise bad(_t("Informe a versão do registro (version)."), code="versao_obrigatoria")
        if payload == s["data"] and not ctx.body.get("resolve_review"):
            return get_scope_after(ctx, sid)
        nv = s["doc_version"] + (0 if payload == s["data"] else 1)
        review = (0, None) if ctx.body.get("resolve_review") else (s["needs_review"], s["review_note"])
        cur = db.ex("UPDATE scopes SET data=?, doc_version=?, needs_review=?, review_note=?, updated_at=?, updated_by=?, version=version+1 WHERE id=? AND version=?",
                    (payload, nv, review[0], review[1], ts, ctx.uid, s["id"], ctx.body["version"]))
        if cur.rowcount == 0:
            raise conflict(_t("O escopo foi alterado por outra pessoa enquanto você editava. Recarregue para ver a versão atual."), code="conflito_versao", current=_ser(db, s))
        if nv != s["doc_version"]:
            db.insert("scope_versions", {"id": new_id(), "scope_id": s["id"], "doc_version": nv, "data": payload, "note": note, "created_by": ctx.uid, "created_at": ts})
        audit(ctx, "alterar", "scopes", s["id"], _t("Escopo do projeto — versão {0}", nv) + (f": {note}" if note else ""), before=json.loads(s["data"]), after=data)
    return get_scope_after(ctx, sid)


def get_scope_after(ctx, sid):
    ctx.query = {"season_id": sid}
    return get_scope(ctx)


@route("GET", "/api/scope/versoes/{id}")
def get_scope_version(ctx):
    judge_module_guard(ctx, "escopo")
    v = ctx.db.one("SELECT * FROM scope_versions WHERE id=?", (ctx.params["id"],))
    if not v:
        raise not_found(_t("Versão"))
    return dict(v, data=json.loads(v["data"]))


class ChangeRequests(Resource):
    def validate(self, ctx, c, old):
        if not old:
            c["requested_by"] = ctx.uid
        elif old["status"] != "aberta" and any(c.get(k) != old.get(k) for k in ("title", "description", "justification", "impact_schedule", "impact_budget", "impact_other")):
            raise bad(_t("Solicitações já decididas não podem ter o conteúdo alterado. Registre uma nova solicitação."))

    def expand(self, ctx, rows):
        names = user_names(ctx.db)
        for r in rows:
            r["requested_by_name"], r["decided_by_name"] = names.get(r["requested_by"]), names.get(r["decided_by"])
        return rows


CR = ChangeRequests("change-requests", "change_requests", _t("Solicitação de alteração de escopo"), {
    "season_id": F("ref", _t("Temporada"), required=True, ref="seasons"),
    "title": F("text", _t("Título"), required=True, max=200),
    "description": F("long", _t("Descrição da alteração"), max=8000),
    "justification": F("long", _t("Justificativa"), required=True, max=8000),
    "impact_schedule": F("long", _t("Impacto no cronograma"), max=4000),
    "impact_budget": F("long", _t("Impacto no orçamento"), max=4000),
    "impact_other": F("long", _t("Outros impactos"), max=4000),
}, season=True, order="created_at DESC", search=("title", "description"))
CR.register(methods=("list", "get", "create", "update", "delete"))


@route("POST", "/api/change-requests/{id}/decidir")
def decide_cr(ctx):
    c = CR.raw(ctx.db, ctx.params["id"])
    if not c:
        raise not_found(_t("Solicitação"))
    decision = ctx.body.get("decision")
    if decision not in ("aprovada", "rejeitada", "implementada"):
        raise bad(_t("Decisão inválida."))
    if decision == "implementada":
        if c["status"] != "aprovada":
            raise conflict(_t("Somente solicitações aprovadas podem ser marcadas como implementadas."))
    else:
        if c["status"] != "aberta":
            raise conflict(_t("Esta solicitação já foi decidida."))
        if c["requested_by"] == ctx.uid:
            raise forbidden(_t("Quem registrou a solicitação não pode aprová-la ou rejeitá-la. Peça a decisão de outro membro da gestão."))
    note = F("long", _t("Justificativa da decisão"), required=decision != "implementada", max=4000).parse(ctx.db, "note", ctx.body.get("note"))
    if decision == "implementada":
        ctx.db.ex("UPDATE change_requests SET status='implementada', updated_at=?, version=version+1 WHERE id=?", (now_iso(), c["id"]))
    else:
        ctx.db.ex("UPDATE change_requests SET status=?, decided_by=?, decided_at=?, decision_note=?, updated_at=?, version=version+1 WHERE id=?",
                  (decision, ctx.uid, now_iso(), note, now_iso(), c["id"]))
    audit(ctx, "decidir", "change-requests", c["id"], f"{c['title']} — {decision}" + (f": {note}" if note else ""))
    return CR.get(ctx, c["id"])
