"""Atividades: fonte única para Planejamento, Gantt, RACI e aprovações.

Regras de cronograma (documentadas também em docs/REGRAS.md):
- Datas planejadas e reais são campos separados; a linha de base (baseline_*) guarda o plano original.
- Atrasada = prazo planejado vencido e situação diferente de concluída/cancelada.
- Concluída com atraso = data real de conclusão posterior ao prazo planejado (histórico preservado).
- Dependências (predecessora -> sucessora), com defasagem em dias (positiva ou negativa):
    FS: início da sucessora >= término da predecessora + defasagem
    SS: início da sucessora >= início da predecessora + defasagem
    FF: término da sucessora >= término da predecessora + defasagem
    SF: término da sucessora >= início da predecessora + defasagem
  (seguindo a planilha original, a sucessora pode começar no mesmo dia em que a predecessora termina.)
"""
import json

from ..core import (F, INTERNAL, ROLES, STAFF, ApiError, Resource, add_days, audit, bad, check_order, conflict,
                    days_between, fmt_date, forbidden, get_setting, internal_login_ids, judge_module_guard, not_found,
                    notify, route, staff_ids, today_local, user_names)
from ..db import new_id, now_iso

STATUS = ("nao_iniciada", "em_andamento", "em_revisao", "aguardando_aprovacao", "bloqueada", "concluida", "cancelada")
STATUS_LABEL = {"nao_iniciada": "Não iniciada", "em_andamento": "Em andamento", "em_revisao": "Em revisão",
                "aguardando_aprovacao": "Aguardando aprovação", "bloqueada": "Bloqueada", "concluida": "Concluída", "cancelada": "Cancelada"}
APPROVAL_LABEL = {"rascunho": "Rascunho", "em_execucao": "Em execução", "aguardando_aprovacao": "Aguardando aprovação",
                  "aprovada": "Aprovada", "reprovada": "Reprovada para ajustes", "reenviada": "Reenviada para aprovação"}
DEP_LABEL = {"FS": "Término → Início", "SS": "Início → Início", "FF": "Término → Término", "SF": "Início → Término"}
CONTENT_FIELDS = ("title", "description", "objective", "completion_criteria")
MEMBER_FIELDS = {"status", "progress", "actual_start", "actual_end", "notes", "next_action", "version",
                 "delay_justification", "delay_impact", "recovery_plan", "forecast_end"}
DONE = ("concluida", "cancelada")


class Activities(Resource):
    def can_write(self, ctx, existing):
        if ctx.role in STAFF:
            return True
        if ctx.role == "membro" and existing is not None:
            return is_assignee(ctx.db, existing, ctx.uid)
        return False

    def parse(self, ctx, body, existing=None):
        if ctx.role == "membro":
            extra = set(body) - MEMBER_FIELDS - {"id"}
            changed = [k for k in extra if k in self.fields and existing is not None and
                       self.fields[k].out(existing.get(self.fields[k].col(k))) != body[k]]
            if changed:
                raise forbidden("Integrantes podem atualizar apenas situação, progresso, datas reais, observações e próxima ação "
                                "das atividades em que estão envolvidos.")
            body = {k: v for k, v in body.items() if k in MEMBER_FIELDS}
        return super().parse(ctx, body, existing)

    def validate(self, ctx, a, old):
        db = ctx.db
        check_order(a, "planned_start", "planned_end", "Início planejado", "Prazo final planejado")
        check_order(a, "actual_start", "actual_end", "Início real", "Conclusão real")
        today = today_local(db)
        gov = get_setting(db, "governance")
        if a.get("custom") is not None and not isinstance(a["custom"], str):
            a["custom"] = json.dumps(a["custom"], ensure_ascii=False)
        old_status = old["status"] if old else None
        st = a.get("status") or "nao_iniciada"
        internal = getattr(ctx, "_approval_flow", False)
        if st == "aguardando_aprovacao" and old_status != st and not internal:
            raise bad("Use a ação “Enviar para aprovação” para colocar a atividade em aprovação.", {"status": "Use Enviar para aprovação."})
        if old and old_status == "aguardando_aprovacao" and st != old_status and not internal:
            raise bad("Esta atividade aguarda aprovação. A situação só muda pela decisão do aprovador.", {"status": "Aguardando aprovação."})
        if st == "concluida" and old_status != "concluida":
            if gov["require_approval_to_complete"] and not internal:
                raise bad("A política da equipe exige aprovação para concluir uma entrega. Use “Enviar para aprovação”.",
                          {"status": "Exige aprovação."}, code="exige_aprovacao")
            a["progress"] = 100
            if not a.get("actual_end"):
                a["actual_end"] = today
            if a.get("actual_end") > today:
                raise bad("A data real de conclusão não pode estar no futuro.", {"actual_end": "Data futura."})
            if a.get("actual_start") and a["actual_start"] > a["actual_end"]:
                raise bad("Início real não pode ser posterior à conclusão real.", {"actual_start": "Posterior à conclusão."})
            if a.get("planned_end") and a["actual_end"] > a["planned_end"] and not (a.get("delay_justification") or "").strip():
                raise bad("Esta entrega está sendo concluída após o prazo planejado. Registre a justificativa do atraso.",
                          {"delay_justification": "Justificativa obrigatória para conclusão após o prazo."}, code="justificativa_obrigatoria")
        if st != "concluida" and a.get("progress") == 100 and (not old or old.get("progress") != 100) and st in ("nao_iniciada", "em_andamento"):
            a["status"] = "em_revisao" if st == "em_andamento" else st
        if st in ("em_andamento", "em_revisao") and not a.get("actual_start") and old_status == "nao_iniciada":
            a["actual_start"] = min(today, a.get("actual_end") or today)
        if a.get("actual_start") and a["actual_start"] > today:
            raise bad("O início real não pode estar no futuro.", {"actual_start": "Data futura."})
        if st != "concluida" and a.get("actual_end") and old and old_status == "concluida":
            a["actual_end"] = None  # reabertura: a data real de conclusão deixa de valer (fica no histórico de auditoria)
        if st != "concluida" and a.get("actual_end") and (not old or (old_status != "concluida" and a["actual_end"] != old.get("actual_end"))):
            raise bad("Para registrar a conclusão real, altere a situação para Concluída.", {"actual_end": "Atividade não concluída."})
        # linha de base: definida na primeira vez em que há datas planejadas; nunca alterada em silêncio
        if not a.get("baseline_end") and a.get("planned_end"):
            a["baseline_start"], a["baseline_end"] = a.get("planned_start"), a["planned_end"]
        if a.get("approval_status") == "rascunho" and st in ("em_andamento", "em_revisao"):
            a["approval_status"] = "em_execucao"
        # alteração de conteúdo após aprovação exige nova aprovação (configurável)
        if old and old["approval_status"] == "aprovada" and gov["reapprove_on_change"] and not internal:
            if any((a.get(k) or "") != (old.get(k) or "") for k in CONTENT_FIELDS):
                invalidate_approval(ctx, a, "Conteúdo alterado após a aprovação")
        if old and any((a.get(k) or "") != (old.get(k) or "") for k in CONTENT_FIELDS):
            a["content_version"] = (old["content_version"] or 1) + 1
        if a.get("wbs_id"):
            w = db.one("SELECT season_id FROM wbs_elements WHERE id=?", (a["wbs_id"],))
            if w and w["season_id"] != a["season_id"]:
                raise bad("O elemento da EAP pertence a outra temporada.", {"wbs_id": "Temporada diferente."})

    def after_save(self, ctx, a, old):
        db = ctx.db
        if "participants" in ctx.body and ctx.role in STAFF and isinstance(ctx.body["participants"], list) and not getattr(ctx, "_skip_parts", False):
            ids = [u for u in dict.fromkeys(ctx.body["participants"]) if db.one("SELECT 1 FROM users WHERE id=? AND deleted_at IS NULL", (u,))]
            db.ex("DELETE FROM activity_participants WHERE activity_id=?", (a["id"],))
            for u in ids:
                db.ex("INSERT INTO activity_participants (activity_id, user_id) VALUES (?,?)", (a["id"], u))
        if old and a["status"] == "bloqueada" and old["status"] != "bloqueada":
            for uid in set(staff_ids(db) + ([a["owner_id"]] if a["owner_id"] else [])):
                notify(db, uid, "bloqueada", f"Atividade bloqueada: {a['title']}", a.get("notes") or "Verifique o impedimento e a próxima ação.",
                       "activities", a["id"], f"blocked:{a['id']}:{a['updated_at']}")
        if old and a["owner_id"] and a["owner_id"] != old["owner_id"] and a["owner_id"] != ctx.uid:
            notify(db, a["owner_id"], "atribuicao", f"Você é responsável por: {a['title']}",
                   f"Prazo planejado: {fmt_date(a['planned_end'])}", "activities", a["id"], f"owner:{a['id']}:{a['updated_at']}")

    def before_delete(self, ctx, a):
        if ctx.role not in STAFF:
            raise forbidden()

    def expand(self, ctx, rows):
        return expand_activities(ctx.db, rows)


ACT = Activities("activities", "activities", "Atividade", {
    "season_id": F("ref", "Temporada", required=True, ref="seasons"),
    "area_id": F("ref", "Área responsável", ref="areas", filter=True),
    "wbs_id": F("ref", "Elemento da EAP", ref="wbs_elements", filter=True),
    "seq": F("int", "Nº no cronograma", min=0, max=100000),
    "title": F("text", "Título", required=True, max=200),
    "description": F("long", "Descrição detalhada"),
    "objective": F("long", "Objetivo"),
    "category": F("text", "Categoria", max=120, filter=True),
    "priority": F("text", "Prioridade", max=40, filter=True),
    "owner_id": F("ref", "Responsável principal", ref="users", filter=True),
    "status": F("enum", "Situação", options=STATUS, default="nao_iniciada", filter=True),
    "progress": F("int", "Progresso (%)", min=0, max=100, default=0),
    "planned_start": F("date", "Início planejado"),
    "planned_end": F("date", "Prazo final planejado"),
    "actual_start": F("date", "Início real"),
    "actual_end": F("date", "Conclusão real"),
    "is_milestone": F("bool", "Marco de entrega"),
    "completion_criteria": F("long", "Critérios de conclusão"),
    "notes": F("long", "Observações"),
    "next_action": F("long", "Próxima ação", max=2000),
    "milestone_ref": F("text", "Marco ou objetivo relacionado", max=200),
    "alert_days": F("int", "Antecedência do alerta (dias)", min=0, max=365),
    "delay_justification": F("long", "Justificativa de atraso", max=4000),
    "delay_impact": F("long", "Impacto do atraso", max=4000),
    "recovery_plan": F("long", "Plano de recuperação", max=4000),
    "recovery_owner_id": F("ref", "Responsável pela recuperação", ref="users"),
    "forecast_end": F("date", "Nova previsão de término"),
    "custom": F("json", "Campos personalizados"),
}, season=True, order="COALESCE(planned_start, planned_end, '9999'), seq, title", search=("title", "description", "notes"))


def is_assignee(db, a, uid):
    if a.get("owner_id") == uid:
        return True
    if db.one("SELECT 1 FROM activity_participants WHERE activity_id=? AND user_id=?", (a["id"], uid)):
        return True
    return bool(db.one("SELECT 1 FROM raci WHERE activity_id=? AND user_id=? AND role='R'", (a["id"], uid)))


def lateness(a, today):
    """(atrasada?, dias de atraso, concluída com atraso?)"""
    pe = a.get("planned_end")
    if not pe:
        return False, 0, False
    if a["status"] == "concluida":
        late = bool(a.get("actual_end") and a["actual_end"] > pe)
        return False, days_between(pe, a["actual_end"]) if late else 0, late
    if a["status"] == "cancelada":
        return False, 0, False
    return (today > pe), max(0, days_between(pe, today)), False


def expand_activities(db, rows):
    if not rows:
        return rows
    today = today_local(db)
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    parts, raci, counts = {}, {}, {}
    for p in db.q(f"SELECT * FROM activity_participants WHERE activity_id IN ({marks})", ids):
        parts.setdefault(p["activity_id"], []).append(p["user_id"])
    for p in db.q(f"SELECT * FROM raci WHERE activity_id IN ({marks})", ids):
        raci.setdefault(p["activity_id"], {})[p["user_id"]] = p["role"]
    for t, key in (("comments", "comments"), ("files", "files")):
        for c in db.q(f"SELECT entity_id, COUNT(*) n FROM {t} WHERE entity_type='activities' AND deleted_at IS NULL AND entity_id IN ({marks}) GROUP BY entity_id", ids):
            counts.setdefault(c["entity_id"], {})[key] = c["n"]
    preds, succs = {}, {}
    for dp in db.q(f"SELECT d.*, p.title ptitle, p.seq pseq, s.title stitle, s.seq sseq FROM dependencies d "
                   f"JOIN activities p ON p.id=d.predecessor_id JOIN activities s ON s.id=d.successor_id "
                   f"WHERE p.deleted_at IS NULL AND s.deleted_at IS NULL AND (d.successor_id IN ({marks}) OR d.predecessor_id IN ({marks}))", ids + ids):
        preds.setdefault(dp["successor_id"], []).append({"dep_id": dp["id"], "id": dp["predecessor_id"], "title": dp["ptitle"], "seq": dp["pseq"], "type": dp["type"], "lag_days": dp["lag_days"]})
        succs.setdefault(dp["predecessor_id"], []).append({"dep_id": dp["id"], "id": dp["successor_id"], "title": dp["stitle"], "seq": dp["sseq"], "type": dp["type"], "lag_days": dp["lag_days"]})
    for r in rows:
        late, days, done_late = lateness(r, today)
        r["is_late"], r["days_late"], r["completed_late"] = late, days, done_late
        r["duration_planned"] = days_between(r["planned_start"], r["planned_end"]) + 1 if r.get("planned_start") and r.get("planned_end") else None
        r["duration_actual"] = days_between(r["actual_start"], r["actual_end"]) + 1 if r.get("actual_start") and r.get("actual_end") else None
        r["participants"] = parts.get(r["id"], [])
        r["raci"] = raci.get(r["id"], {})
        r["predecessors"] = preds.get(r["id"], [])
        r["successors"] = succs.get(r["id"], [])
        r["comment_count"] = counts.get(r["id"], {}).get("comments", 0)
        r["file_count"] = counts.get(r["id"], {}).get("files", 0)
    return rows


# ----------------------------------------------------------------------------- grafo do cronograma
def load_graph(db, season_id):
    acts = {a["id"]: a for a in db.q("SELECT * FROM activities WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (season_id,))}
    deps = [x for x in db.q("SELECT d.* FROM dependencies d JOIN activities p ON p.id=d.predecessor_id WHERE p.season_id=?", (season_id,))
            if x["predecessor_id"] in acts and x["successor_id"] in acts]
    return acts, deps


def would_cycle(db, pred, succ, ignore_dep=None):
    """Há caminho succ -> ... -> pred? (então pred -> succ fecharia um ciclo)"""
    edges = {}
    for x in db.q("SELECT id, predecessor_id p, successor_id s FROM dependencies"):
        if x["id"] != ignore_dep:
            edges.setdefault(x["p"], []).append(x["s"])
    stack, seen = [succ], set()
    while stack:
        n = stack.pop()
        if n == pred:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(edges.get(n, []))
    return False


def _se(a, proposed=None):
    """(início, término) planejados; sem início, considera duração zero no prazo final."""
    if proposed and a["id"] in proposed:
        return proposed[a["id"]]
    e = a.get("planned_end")
    s = a.get("planned_start") or e
    return s, e or s


def required_date(dep, pred_se, succ_se):
    """Retorna (campo da sucessora, data mínima exigida) para a dependência, ou None se faltam datas."""
    ps, pe = pred_se
    if dep["type"] in ("FS", "FF"):
        base = pe
    else:
        base = ps
    if not base:
        return None
    return ("start" if dep["type"] in ("FS", "SS") else "end"), add_days(base, dep["lag_days"])


def schedule_conflicts(acts, deps, today=None):
    """Conflitos de dependência com as datas planejadas e dependências em risco por atraso da predecessora."""
    out = []
    for dp in deps:
        p, s = acts[dp["predecessor_id"]], acts[dp["successor_id"]]
        if s["status"] == "cancelada" or p["status"] == "cancelada":
            continue
        req = required_date(dp, _se(p), _se(s))
        if not req:
            continue
        field, minimum = req
        ss, se = _se(s)
        cur = ss if field == "start" else se
        base = {"dep_id": dp["id"], "type": dp["type"], "lag_days": dp["lag_days"], "predecessor_id": p["id"], "successor_id": s["id"],
                "predecessor_title": p["title"], "successor_title": s["title"]}
        if cur and cur < minimum and s["status"] != "concluida":
            out.append({**base, "kind": "conflito", "days": days_between(cur, minimum),
                        "message": f"“{s['title']}” deveria {'iniciar' if field == 'start' else 'terminar'} a partir de {fmt_date(minimum)} "
                                   f"por depender de “{p['title']}” ({dp['type']}{'%+d' % dp['lag_days'] if dp['lag_days'] else ''}), "
                                   f"mas está planejada para {fmt_date(cur)}."})
        elif today and p["status"] not in DONE and s["status"] not in DONE:
            expected = p.get("forecast_end") or p.get("planned_end")
            if dp["type"] in ("FS", "FF") and expected and (today > p["planned_end"] if p.get("planned_end") else False):
                eff = max(today, p.get("forecast_end") or today)
                need = add_days(eff, dp["lag_days"])
                if cur and need > cur:
                    out.append({**base, "kind": "risco", "days": days_between(cur, need),
                                "message": f"“{p['title']}” está atrasada e pode comprometer “{s['title']}” (dependência {dp['type']})."})
    return out


def topo_order(acts, deps):
    indeg = {i: 0 for i in acts}
    adj = {}
    for dp in deps:
        indeg[dp["successor_id"]] += 1
        adj.setdefault(dp["predecessor_id"], []).append(dp)
    queue = [i for i, n in indeg.items() if n == 0]
    order = []
    while queue:
        n = queue.pop()
        order.append(n)
        for dp in adj.get(n, []):
            indeg[dp["successor_id"]] -= 1
            if indeg[dp["successor_id"]] == 0:
                queue.append(dp["successor_id"])
    return order, adj


def critical_path(acts, deps):
    """Folga total por passada regressiva sobre as datas planejadas. Crítica = folga <= 0 em atividade que participa da rede
    de dependências ou termina na data final do projeto. Devolve {id: folga_em_dias}."""
    dated = {i: a for i, a in acts.items() if a.get("planned_end") and a["status"] != "cancelada"}
    deps = [x for x in deps if x["predecessor_id"] in dated and x["successor_id"] in dated]
    if not dated:
        return {}, set()
    project_end = max(a["planned_end"] for a in dated.values())
    order, adj = topo_order(dated, deps)
    lf = {}
    for i in reversed(order):
        a = dated[i]
        s, e = _se(a)
        dur = days_between(s, e)
        limit = project_end
        for dp in adj.get(i, []):
            sa = dated[dp["successor_id"]]
            ss, se = _se(sa)
            sdur = days_between(ss, se)
            s_lf = lf.get(sa["id"], se)
            s_ls = add_days(s_lf, -sdur)
            t = dp["type"]
            cand = (add_days(s_ls, -dp["lag_days"]) if t == "FS" else
                    add_days(s_ls, -dp["lag_days"] + dur) if t == "SS" else
                    add_days(s_lf, -dp["lag_days"]) if t == "FF" else
                    add_days(s_lf, -dp["lag_days"] + dur))
            limit = min(limit, cand)
        lf[i] = limit
    floats = {i: days_between(_se(dated[i])[1], lf[i]) for i in lf}
    in_net = {x["predecessor_id"] for x in deps} | {x["successor_id"] for x in deps}
    critical = {i for i, f in floats.items() if f <= 0 and (i in in_net or dated[i]["planned_end"] == project_end)
                and dated[i]["status"] not in DONE}
    return floats, critical


def propose_shift(acts, deps, changed_id):
    """Proposta de ajuste em cascata das sucessoras (mantendo a duração). Nada é gravado aqui."""
    order, adj = topo_order(acts, deps)
    pos = {i: n for n, i in enumerate(order)}
    preds_of = {}
    for dp in deps:
        preds_of.setdefault(dp["successor_id"], []).append(dp)
    proposed, changes = {}, []
    affected, stack = set(), [changed_id]
    while stack:
        n = stack.pop()
        for dp in adj.get(n, []):
            if dp["successor_id"] not in affected:
                affected.add(dp["successor_id"])
                stack.append(dp["successor_id"])
    for i in sorted(affected, key=lambda x: pos.get(x, 0)):
        a = acts[i]
        if a["status"] in DONE or not a.get("planned_end"):
            continue
        s, e = _se(a)
        delta, reasons = 0, []
        for dp in preds_of.get(i, []):
            req = required_date(dp, _se(acts[dp["predecessor_id"]], proposed), (s, e))
            if not req:
                continue
            field, minimum = req
            cur = s if field == "start" else e
            need = days_between(cur, minimum)
            if need > delta:
                delta = need
            if need > 0:
                reasons.append(f"{dp['type']} de “{acts[dp['predecessor_id']]['title']}”")
        if delta > 0:
            ns, ne = add_days(s, delta), add_days(e, delta)
            proposed[i] = (ns, ne)
            changes.append({"id": i, "title": a["title"], "version": a["version"], "has_start": bool(a.get("planned_start")),
                            "old_start": a.get("planned_start"), "old_end": a["planned_end"],
                            "new_start": ns if a.get("planned_start") else None, "new_end": ne,
                            "shift_days": delta, "reason": "; ".join(reasons)})
    return changes


# ----------------------------------------------------------------------------- rotas: atividades
def _guard(ctx):
    judge_module_guard(ctx, "planejamento")


@route("GET", "/api/activities")
def list_activities(ctx):
    if ctx.role == "juiz":
        jv = get_setting(ctx.db, "judge_view")["modules"]
        if not (jv.get("planejamento") or jv.get("gantt") or jv.get("raci")):
            raise forbidden("Este módulo não está disponível para o perfil de juiz.")
    conds, ps = [], []
    q = ctx.query
    if q.get("from"):
        conds.append("COALESCE(planned_end, planned_start) >= ?")
        ps.append(q["from"])
    if q.get("to"):
        conds.append("COALESCE(planned_start, planned_end) <= ?")
        ps.append(q["to"])
    if q.get("participant_id"):
        conds.append("(owner_id = ? OR id IN (SELECT activity_id FROM activity_participants WHERE user_id = ?))")
        ps += [q["participant_id"], q["participant_id"]]
    res = ACT.list(ctx, " AND ".join(conds) if conds else None, ps)
    if q.get("late") == "1":
        res["items"] = [a for a in res["items"] if a["is_late"]]
        res["total"] = len(res["items"])
    return res


@route("GET", "/api/activities/{id}")
def get_activity(ctx):
    a = ACT.get(ctx, ctx.params["id"])
    db = ctx.db
    names = user_names(db)
    a["delays"] = [dict(r, registered_by_name=names.get(r["registered_by"])) for r in
                   db.q("SELECT * FROM delay_records WHERE activity_id=? ORDER BY registered_at DESC", (a["id"],))]
    a["approvals"] = [dict(r, user_name=names.get(r["user_id"])) for r in
                      db.q("SELECT * FROM approvals WHERE activity_id=? ORDER BY at DESC", (a["id"],))]
    a["raci_alerts"] = raci_alerts(db, ctx.db.one("SELECT * FROM activities WHERE id=?", (a["id"],)), a["raci"])
    a["risks"] = db.q("SELECT r.id, r.code, r.title FROM risks r JOIN risk_links l ON l.risk_id=r.id WHERE l.entity_type='activity' AND l.entity_id=? AND r.deleted_at IS NULL", (a["id"],))
    a["evidences"] = db.q("SELECT e.id entry_id, w.id week_id, w.month, w.week_number FROM evidence_activities ea JOIN evidence_entries e ON e.id=ea.entry_id "
                          "JOIN evidence_weeks w ON w.id=e.week_id WHERE ea.activity_id=? AND w.deleted_at IS NULL", (a["id"],))
    acts, deps = load_graph(db, a["season_id"])
    a["conflicts"] = [c for c in schedule_conflicts(acts, deps, today_local(db)) if a["id"] in (c["predecessor_id"], c["successor_id"])]
    return a


def _impacts(ctx, a):
    acts, deps = load_graph(ctx.db, a["season_id"])
    if a["id"] not in acts:
        return []
    return propose_shift(acts, deps, a["id"])


@route("POST", "/api/activities")
def create_activity(ctx):
    a = ACT.create(ctx, ctx.body)
    return a


@route("PUT", "/api/activities/{id}", allow=INTERNAL)
def update_activity(ctx):
    old = ACT.raw(ctx.db, ctx.params["id"])
    a = ACT.update(ctx, ctx.params["id"], ctx.body)
    if old and (old["planned_start"], old["planned_end"]) != (a["planned_start"], a["planned_end"]):
        a["impacts"] = _impacts(ctx, a)  # sucessoras afetadas: apenas proposta; nada é alterado sem confirmação
    return a


@route("DELETE", "/api/activities/{id}")
def delete_activity(ctx):
    a = ACT.raw(ctx.db, ctx.params["id"])
    if not a:
        raise not_found("Atividade")
    n_dep = ctx.db.val("SELECT COUNT(*) FROM dependencies WHERE predecessor_id=? OR successor_id=?", (a["id"], a["id"]))
    n_ev = ctx.db.val("SELECT COUNT(*) FROM evidence_activities WHERE activity_id=?", (a["id"],))
    n_ap = ctx.db.val("SELECT COUNT(*) FROM approvals WHERE activity_id=? AND action IN ('aprovada','reprovada')", (a["id"],))
    if (n_dep or n_ev or n_ap) and ctx.query.get("cascade") != "1":
        refs = [r for r in ({"label": "dependência(s) no cronograma", "count": n_dep}, {"label": "evidência(s) vinculada(s)", "count": n_ev},
                            {"label": "decisão(ões) de aprovação", "count": n_ap}) if r["count"]]
        raise conflict("Esta atividade possui vínculos (" + ", ".join(f"{r['count']} {r['label']}" for r in refs) +
                       "). Você pode arquivá-la (recomendado) ou confirmar a exclusão removendo também os vínculos.",
                       code="vinculos_existentes", references=refs, can_archive=True, can_cascade=True)
    ctx.db.ex("DELETE FROM dependencies WHERE predecessor_id=? OR successor_id=?", (a["id"], a["id"]))
    ctx.db.ex("DELETE FROM evidence_activities WHERE activity_id=?", (a["id"],))
    ctx.db.ex("DELETE FROM risk_links WHERE entity_type='activity' AND entity_id=?", (a["id"],))
    ctx.db.ex("UPDATE activities SET deleted_at=?, version=version+1 WHERE id=?", (now_iso(), a["id"]))
    audit(ctx, "excluir", "activities", a["id"], a["title"], before=ACT.ser(a))
    return {"ok": True}


route("POST", "/api/activities/{id}/arquivar")(lambda ctx: ACT.set_archived(ctx, ctx.params["id"], True))
route("POST", "/api/activities/{id}/desarquivar")(lambda ctx: ACT.set_archived(ctx, ctx.params["id"], False))


@route("POST", "/api/activities/{id}/duplicar")
def duplicate_activity(ctx):
    a = ACT.raw(ctx.db, ctx.params["id"])
    if not a:
        raise not_found("Atividade")
    keep = ("season_id", "area_id", "wbs_id", "description", "objective", "category", "priority", "owner_id",
            "planned_start", "planned_end", "is_milestone", "completion_criteria", "milestone_ref", "alert_days")
    body = {k: ACT.fields[k].out(a[k]) for k in keep}
    body["title"] = (a["title"] + " (cópia)")[:200]
    body["participants"] = [p["user_id"] for p in ctx.db.q("SELECT user_id FROM activity_participants WHERE activity_id=?", (a["id"],))]
    ctx.body = body
    return ACT.create(ctx, body)


@route("POST", "/api/activities/{id}/atraso", allow=INTERNAL)
def register_delay(ctx):
    """Registro de atraso: justificativa obrigatória; impacto, plano de recuperação, responsável e nova previsão."""
    db = ctx.db
    a = ACT.raw(db, ctx.params["id"])
    if not a:
        raise not_found("Atividade")
    if not ACT.can_write(ctx, a):
        raise forbidden()
    just = str(ctx.body.get("justification") or "").strip()
    if not just:
        raise bad("A justificativa é obrigatória para registrar um atraso.", {"justification": "Campo obrigatório."}, code="justificativa_obrigatoria")
    f = ACT.fields
    data = {"delay_justification": f["delay_justification"].parse(db, "justification", just),
            "delay_impact": f["delay_impact"].parse(db, "impact", ctx.body.get("impact")),
            "recovery_plan": f["recovery_plan"].parse(db, "recovery_plan", ctx.body.get("recovery_plan")),
            "recovery_owner_id": f["recovery_owner_id"].parse(db, "recovery_owner_id", ctx.body.get("recovery_owner_id")),
            "forecast_end": f["forecast_end"].parse(db, "forecast_end", ctx.body.get("forecast_end"))}
    if data["forecast_end"] and a["planned_end"] and data["forecast_end"] < a["planned_end"]:
        raise bad("A nova previsão não pode ser anterior ao prazo planejado original.", {"forecast_end": "Anterior ao prazo planejado."})
    db.insert("delay_records", {"id": new_id(), "activity_id": a["id"], "registered_by": ctx.uid, "registered_at": now_iso(),
                                "planned_end": a["planned_end"], "justification": data["delay_justification"], "impact": data["delay_impact"],
                                "recovery_plan": data["recovery_plan"], "recovery_owner_id": data["recovery_owner_id"], "forecast_end": data["forecast_end"]})
    sets = ", ".join(f"{k}=?" for k in data)
    db.ex(f"UPDATE activities SET {sets}, updated_at=?, version=version+1 WHERE id=?", list(data.values()) + [now_iso(), a["id"]])
    new = ACT.raw(db, a["id"])
    audit(ctx, "registrar_atraso", "activities", a["id"], a["title"], before=ACT.ser(a), after=ACT.ser(new))
    for uid in staff_ids(db):
        if uid != ctx.uid:
            notify(db, uid, "atraso_registrado", f"Atraso registrado: {a['title']}", just[:300], "activities", a["id"], f"delayreg:{a['id']}:{new['updated_at']}")
    out = ACT.get(ctx, a["id"])
    out["impacts"] = _impacts(ctx, out)
    return out


@route("POST", "/api/activities/{id}/redefinir-linha-base")
def reset_baseline(ctx):
    a = ACT.raw(ctx.db, ctx.params["id"])
    if not a:
        raise not_found("Atividade")
    reason = str(ctx.body.get("reason") or "").strip()
    if not reason:
        raise bad("Informe o motivo para redefinir a linha de base.", {"reason": "Campo obrigatório."})
    ctx.db.ex("UPDATE activities SET baseline_start=planned_start, baseline_end=planned_end, updated_at=?, version=version+1 WHERE id=?", (now_iso(), a["id"]))
    new = ACT.raw(ctx.db, a["id"])
    audit(ctx, "redefinir_linha_base", "activities", a["id"], f"{a['title']} — {reason}", before=ACT.ser(a), after=ACT.ser(new))
    return ACT.get(ctx, a["id"])


# ----------------------------------------------------------------------------- dependências
def _dep_parse(ctx, existing=None):
    b = ctx.body
    t = b.get("type", existing["type"] if existing else "FS")
    if t not in DEP_LABEL:
        raise bad("Tipo de dependência inválido (use FS, SS, FF ou SF).", {"type": "Tipo inválido."})
    lag = b.get("lag_days", existing["lag_days"] if existing else 0)
    if isinstance(lag, bool) or not isinstance(lag, int) or not -365 <= lag <= 365:
        raise bad("Defasagem inválida: informe um número inteiro de dias entre -365 e 365.", {"lag_days": "Valor inválido."})
    return t, lag


@route("POST", "/api/dependencies")
def create_dependency(ctx):
    db = ctx.db
    p, s = ACT.raw(db, str(ctx.body.get("predecessor_id"))), ACT.raw(db, str(ctx.body.get("successor_id")))
    if not p or not s:
        raise bad("Selecione a atividade predecessora e a sucessora.")
    if p["id"] == s["id"]:
        raise bad("Uma atividade não pode depender dela própria.", code="auto_dependencia")
    if p["season_id"] != s["season_id"]:
        raise bad("As atividades pertencem a temporadas diferentes.")
    t, lag = _dep_parse(ctx)
    if db.one("SELECT 1 FROM dependencies WHERE predecessor_id=? AND successor_id=?", (p["id"], s["id"])):
        raise conflict("Já existe uma dependência entre estas duas atividades. Edite a existente.", code="dependencia_duplicada")
    if would_cycle(db, p["id"], s["id"]):
        raise conflict(f"Dependência circular: “{p['title']}” já depende (direta ou indiretamente) de “{s['title']}”.", code="dependencia_circular")
    dep = {"id": new_id(), "predecessor_id": p["id"], "successor_id": s["id"], "type": t, "lag_days": lag, "created_by": ctx.uid, "created_at": now_iso()}
    db.insert("dependencies", dep)
    audit(ctx, "criar", "dependencies", dep["id"], f"{p['title']} → {s['title']} ({t}{'%+d' % lag if lag else ''})", after=dep)
    acts, deps = load_graph(db, p["season_id"])
    dep["conflicts"] = [c for c in schedule_conflicts(acts, deps) if c["dep_id"] == dep["id"]]
    dep["impacts"] = propose_shift(acts, deps, p["id"]) if dep["conflicts"] else []
    return dep


@route("PUT", "/api/dependencies/{id}")
def update_dependency(ctx):
    db = ctx.db
    dep = db.one("SELECT * FROM dependencies WHERE id=?", (ctx.params["id"],))
    if not dep:
        raise not_found("Dependência")
    t, lag = _dep_parse(ctx, dep)
    db.ex("UPDATE dependencies SET type=?, lag_days=? WHERE id=?", (t, lag, dep["id"]))
    new = db.one("SELECT * FROM dependencies WHERE id=?", (dep["id"],))
    audit(ctx, "alterar", "dependencies", dep["id"], "Dependência alterada", before=dep, after=new)
    p = ACT.raw(db, dep["predecessor_id"])
    acts, deps = load_graph(db, p["season_id"])
    new["conflicts"] = [c for c in schedule_conflicts(acts, deps) if c["dep_id"] == dep["id"]]
    new["impacts"] = propose_shift(acts, deps, p["id"]) if new["conflicts"] else []
    return new


@route("DELETE", "/api/dependencies/{id}")
def delete_dependency(ctx):
    dep = ctx.db.one("SELECT * FROM dependencies WHERE id=?", (ctx.params["id"],))
    if not dep:
        raise not_found("Dependência")
    ctx.db.ex("DELETE FROM dependencies WHERE id=?", (dep["id"],))
    audit(ctx, "excluir", "dependencies", dep["id"], "Dependência removida", before=dep)
    return {"ok": True}


@route("GET", "/api/gantt")
def gantt(ctx):
    judge_module_guard(ctx, "gantt")
    db, sid = ctx.db, ctx.query.get("season_id")
    if not sid:
        raise bad("Informe a temporada.")
    acts, deps = load_graph(db, sid)
    floats, critical = critical_path(acts, deps)
    today = today_local(db)
    items = expand_activities(db, [ACT.ser(a) for a in acts.values()])
    for a in items:
        a["critical"] = a["id"] in critical
        a["float_days"] = floats.get(a["id"])
    items.sort(key=lambda a: (a["planned_start"] or a["planned_end"] or "9999", a["seq"] or 0, a["title"]))
    return {"today": today, "activities": items, "dependencies": deps, "conflicts": schedule_conflicts(acts, deps, today),
            "critical_ids": sorted(critical)}


@route("POST", "/api/activities/{id}/proposta-ajuste", tx=False)
def shift_proposal(ctx):
    a = ACT.raw(ctx.db, ctx.params["id"])
    if not a:
        raise not_found("Atividade")
    acts, deps = load_graph(ctx.db, a["season_id"])
    return {"changes": propose_shift(acts, deps, a["id"])}


@route("POST", "/api/schedule/aplicar")
def apply_schedule(ctx):
    """Aplica ajustes de datas confirmados pelo usuário (ex.: proposta em cascata). Tudo ou nada."""
    changes = ctx.body.get("changes")
    reason = str(ctx.body.get("reason") or "").strip()
    if not isinstance(changes, list) or not changes:
        raise bad("Nenhum ajuste informado.")
    if not reason:
        raise bad("Informe o motivo do ajuste do cronograma.", {"reason": "Campo obrigatório."})
    out = []
    ctx._skip_parts = True
    for ch in changes:
        body = {"planned_end": ch.get("new_end"), "version": ch.get("version")}
        if ch.get("new_start"):
            body["planned_start"] = ch["new_start"]
        ctx.body = body
        out.append(ACT.update(ctx, str(ch.get("id")), body, action="ajuste_cronograma"))
    audit(ctx, "ajuste_cronograma", "schedule", None, f"{len(out)} atividade(s) reprogramada(s): {reason}")
    return {"items": out}


# ----------------------------------------------------------------------------- RACI
def _eligible_users(db):
    return {u["id"]: u for u in db.q("SELECT * FROM users WHERE deleted_at IS NULL")}


def approver_block_reason(a, u):
    """Motivo pelo qual o usuário NÃO pode ser aprovador final (None = pode)."""
    if u["role"] == "juiz":
        return "juiz", "O perfil de juiz é somente leitura e não pode aprovar entregas."
    if u["is_teacher"]:
        return "aprovador_professor", "O professor técnico não pode ser o aprovador final de nenhuma atividade."
    if a.get("created_by") and a["created_by"] == u["id"]:
        return "aprovador_criador", "O criador da atividade não pode aprovar a própria entrega."
    return None


def raci_alerts(db, a, assignments, users=None, actives=None):
    users = users or _eligible_users(db)
    alerts = []
    roles = list(assignments.values())
    if "R" not in roles:
        alerts.append({"code": "sem_responsavel", "level": "erro", "message": "Sem responsável pela execução (R)."})
    approver = next((u for u, r in assignments.items() if r == "A"), None)
    if not approver:
        alerts.append({"code": "sem_aprovador", "level": "erro", "message": "Sem aprovador final (A)."})
    else:
        u = users.get(approver)
        blk = approver_block_reason(a, u) if u else ("inexistente", "Aprovador não encontrado.")
        if blk:
            alerts.append({"code": blk[0], "level": "erro", "message": blk[1]})
        elif not u["active"]:
            alerts.append({"code": "aprovador_inativo", "level": "erro", "message": f"O aprovador {u['name']} está desativado."})
        elif not u["password_hash"]:
            alerts.append({"code": "aprovador_sem_acesso", "level": "aviso", "message": f"{u['name']} ainda não possui acesso à plataforma para registrar a aprovação."})
    if "C" not in roles:
        alerts.append({"code": "sem_consultados", "level": "info", "message": "Nenhuma pessoa consultada (C)."})
    if actives is None:
        actives = [u for u in users.values() if u["active"] and u["role"] != "juiz"]
    omitted = [u["name"] for u in actives if u["id"] not in assignments]
    if omitted and not (a.get("raci_exception") or "").strip():
        alerts.append({"code": "omitidos", "level": "aviso", "message": "Integrantes ativos sem ciência desta atividade: " + ", ".join(omitted) + "."})
    return alerts


@route("GET", "/api/raci")
def raci_matrix(ctx):
    judge_module_guard(ctx, "raci")
    db, sid = ctx.db, ctx.query.get("season_id")
    users = _eligible_users(db)
    actives = [u for u in users.values() if u["active"] and u["role"] != "juiz"]
    acts = db.q("SELECT * FROM activities WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL ORDER BY area_id, COALESCE(planned_start, planned_end, '9999'), seq, title", (sid,))
    ras = {}
    for r in db.q("SELECT r.* FROM raci r JOIN activities a ON a.id=r.activity_id WHERE a.season_id=?", (sid,)):
        ras.setdefault(r["activity_id"], {})[r["user_id"]] = r["role"]
    return {"users": [{"id": u["id"], "name": u["name"], "area_id": u["area_id"], "position": u["position"], "is_teacher": bool(u["is_teacher"]), "active": bool(u["active"])}
                      for u in sorted(users.values(), key=lambda x: (x["is_teacher"], x["name"])) if u["role"] != "juiz" and (u["active"] or any(u["id"] in m for m in ras.values()))],
            "activities": [{"id": a["id"], "title": a["title"], "area_id": a["area_id"], "category": a["category"], "created_by": a["created_by"], "status": a["status"],
                            "approval_status": a["approval_status"], "raci_exception": a["raci_exception"], "version": a["version"],
                            "raci": ras.get(a["id"], {}), "alerts": raci_alerts(db, a, ras.get(a["id"], {}), users, actives)} for a in acts]}


def _log_blocked(ctx, a, code, message):
    """Registra a tentativa bloqueada (auditoria + histórico de aprovações) e confirma o registro antes de devolver o erro."""
    ctx.db.rollback()
    ctx.db.begin()
    ctx.db.insert("approvals", {"id": new_id(), "activity_id": a["id"], "action": "tentativa_bloqueada", "user_id": ctx.uid, "at": now_iso(),
                                "content_version": a["content_version"], "comment": None, "blocked_reason": f"{code}: {message}"})
    audit(ctx, "tentativa_bloqueada", "activities", a["id"], f"{a['title']} — {message}")
    ctx.db.commit()
    return ApiError(403, code, message)


@route("PUT", "/api/activities/{id}/raci")
def save_raci(ctx):
    """Substitui as atribuições RACI da atividade. As restrições de aprovação são validadas aqui, no servidor."""
    db = ctx.db
    a = ACT.raw(db, ctx.params["id"])
    if not a:
        raise not_found("Atividade")
    raw = ctx.body.get("assignments")
    if not isinstance(raw, dict):
        raise bad("Informe as atribuições no formato {usuário: papel}.")
    users = _eligible_users(db)
    assign = {}
    for uid, role in raw.items():
        if role in (None, ""):
            continue
        if role not in ("R", "A", "C", "I"):
            raise bad("Papel RACI inválido (use R, A, C ou I).")
        u = users.get(uid)
        if not u:
            raise bad("Integrante não encontrado.")
        if u["role"] == "juiz":
            raise bad("Juízes não participam da matriz RACI.")
        assign[uid] = role
    approvers = [u for u, r in assign.items() if r == "A"]
    if len(approvers) > 1:
        raise bad("Cada atividade deve ter um único aprovador final (A).", code="varios_aprovadores")
    if approvers:
        blk = approver_block_reason(a, users[approvers[0]])
        if blk:
            raise _log_blocked(ctx, a, blk[0], blk[1])
        if not users[approvers[0]]["active"]:
            raise bad("O aprovador selecionado está desativado.")
    before = {r["user_id"]: r["role"] for r in db.q("SELECT * FROM raci WHERE activity_id=?", (a["id"],))}
    exception = ctx.body.get("raci_exception", a["raci_exception"])
    exception = F("long", "Justificativa da exceção", max=2000).parse(db, "raci_exception", exception)
    db.ex("DELETE FROM raci WHERE activity_id=?", (a["id"],))
    for uid, role in assign.items():
        db.ex("INSERT INTO raci (activity_id, user_id, role) VALUES (?,?,?)", (a["id"], uid, role))
    db.ex("UPDATE activities SET raci_exception=?, updated_at=? WHERE id=?", (exception, now_iso(), a["id"]))
    names = {u: users[u]["name"] for u in users}
    audit(ctx, "alterar_raci", "activities", a["id"], a["title"],
          before={"raci": {names.get(k, k): v for k, v in before.items()}, "raci_exception": a["raci_exception"]},
          after={"raci": {names.get(k, k): v for k, v in assign.items()}, "raci_exception": exception})
    for uid, role in assign.items():
        if before.get(uid) != role and uid != ctx.uid and role in ("R", "A"):
            notify(db, uid, "raci", f"Nova responsabilidade em “{a['title']}”",
                   "Você é " + ("responsável pela execução (R)." if role == "R" else "aprovador final (A)."), "activities", a["id"], f"raci:{a['id']}:{uid}:{role}:{now_iso()[:16]}")
    a2 = ACT.raw(db, a["id"])
    return {"id": a["id"], "raci": assign, "raci_exception": exception, "alerts": raci_alerts(db, a2, assign, users)}


@route("POST", "/api/raci/informar-todos")
def raci_inform_all(ctx):
    """Marca como Informado (I) todo integrante ativo ainda sem papel nas atividades indicadas (ou em toda a temporada)."""
    db, sid = ctx.db, ctx.body.get("season_id")
    ids = ctx.body.get("activity_ids")
    acts = db.q("SELECT id FROM activities WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (sid,))
    actives = db.q("SELECT id FROM users WHERE active=1 AND deleted_at IS NULL AND role<>'juiz'")
    n = 0
    for a in acts:
        if ids and a["id"] not in ids:
            continue
        for u in actives:
            n += db.ex("INSERT OR IGNORE INTO raci (activity_id, user_id, role) VALUES (?,?,'I')", (a["id"], u["id"])).rowcount
    audit(ctx, "alterar_raci", "raci", None, f"{n} atribuição(ões) “Informado” adicionada(s) automaticamente")
    return {"added": n}


# ----------------------------------------------------------------------------- fluxo de aprovação
def invalidate_approval(ctx, a, reason):
    a["approval_status"], a["approved_version"] = "em_execucao", None
    if a.get("status") == "concluida":
        a["status"] = "em_revisao"
        a["actual_end"] = None
    ctx.db.insert("approvals", {"id": new_id(), "activity_id": a["id"], "action": "invalidada", "user_id": ctx.uid, "at": now_iso(),
                                "content_version": a.get("content_version"), "comment": reason, "blocked_reason": None})
    approver = ctx.db.one("SELECT user_id FROM raci WHERE activity_id=? AND role='A'", (a["id"],))
    if approver:
        notify(ctx.db, approver["user_id"], "aprovacao", f"Aprovação invalidada: {a['title']}",
               f"{reason}. Será necessária nova aprovação.", "activities", a["id"], f"invalid:{a['id']}:{now_iso()}")


def content_changed(ctx, activity_id, reason):
    """Chamado quando anexos de uma entrega aprovada mudam."""
    a = ACT.raw(ctx.db, activity_id)
    if not a or a["approval_status"] != "aprovada" or not get_setting(ctx.db, "governance")["reapprove_on_change"]:
        return
    invalidate_approval(ctx, a, reason)
    ctx.db.ex("UPDATE activities SET approval_status=?, approved_version=NULL, status=?, actual_end=?, content_version=content_version+1, updated_at=?, version=version+1 WHERE id=?",
              (a["approval_status"], a["status"], a["actual_end"], now_iso(), a["id"]))
    audit(ctx, "aprovacao_invalidada", "activities", a["id"], f"{a['title']} — {reason}")


def _eligible_approver(db, a):
    row = db.one("SELECT u.* FROM raci r JOIN users u ON u.id=r.user_id WHERE r.activity_id=? AND r.role='A'", (a["id"],))
    if not row:
        return None, "Não há aprovador final (A) definido na matriz RACI."
    blk = approver_block_reason(a, row)
    if blk:
        return None, blk[1]
    if not row["active"] or row["deleted_at"]:
        return None, f"O aprovador {row['name']} está desativado."
    if not row["password_hash"]:
        return None, f"O aprovador {row['name']} ainda não possui acesso à plataforma."
    return row, None


@route("POST", "/api/activities/{id}/submeter", allow=INTERNAL)
def submit_for_approval(ctx):
    db = ctx.db
    a = ACT.raw(db, ctx.params["id"])
    if not a:
        raise not_found("Atividade")
    if ctx.role not in STAFF and not is_assignee(db, a, ctx.uid):
        raise forbidden("Somente quem executa a atividade (ou a gestão) pode submetê-la para aprovação.")
    if a["approval_status"] in ("aguardando_aprovacao", "reenviada"):
        raise conflict("Esta atividade já aguarda aprovação.")
    if a["approval_status"] == "aprovada":
        raise conflict("Esta atividade já está aprovada.")
    if a["status"] == "cancelada":
        raise conflict("Atividades canceladas não podem ser submetidas.")
    if not db.one("SELECT 1 FROM raci WHERE activity_id=? AND role='R'", (a["id"],)) and not a["owner_id"]:
        raise conflict("Defina um responsável pela execução antes de submeter.", code="sem_responsavel")
    approver, why = _eligible_approver(db, a)
    if approver and approver["id"] == ctx.uid:
        approver, why = None, "Quem submete a entrega não pode ser o seu aprovador."
    if not approver:
        msg = f"Submissão bloqueada: {why} Peça a um administrador que atribua outro integrante autorizado como aprovador."
        for uid in staff_ids(db):
            notify(db, uid, "aprovacao", f"Sem aprovador elegível: {a['title']}", why, "activities", a["id"], f"noappr:{a['id']}:{now_iso()[:13]}")
        audit(ctx, "tentativa_bloqueada", "activities", a["id"], f"{a['title']} — submissão sem aprovador elegível: {why}")
        db.commit()
        raise conflict(msg, code="sem_aprovador_elegivel")
    today = today_local(db)
    just = str(ctx.body.get("delay_justification") or a["delay_justification"] or "").strip()
    if a["planned_end"] and today > a["planned_end"] and not just:
        raise bad("A entrega está sendo submetida após o prazo planejado. Registre a justificativa do atraso.",
                  {"delay_justification": "Justificativa obrigatória."}, code="justificativa_obrigatoria")
    resend = a["approval_status"] == "reprovada"
    new_status = "reenviada" if resend else "aguardando_aprovacao"
    db.ex("UPDATE activities SET approval_status=?, status='aguardando_aprovacao', submitted_by=?, submitted_at=?, delay_justification=?, "
          "actual_start=COALESCE(actual_start, ?), updated_at=?, version=version+1 WHERE id=?",
          (new_status, ctx.uid, now_iso(), just or None, today, now_iso(), a["id"]))
    db.insert("approvals", {"id": new_id(), "activity_id": a["id"], "action": "reenviada" if resend else "submetida", "user_id": ctx.uid, "at": now_iso(),
                            "content_version": a["content_version"], "comment": F("long", "Comentário", max=4000).parse(db, "comment", ctx.body.get("comment")), "blocked_reason": None})
    notify(db, approver["id"], "aprovacao", f"Aprovação pendente: {a['title']}", f"Enviada por {ctx.user['name']} (versão {a['content_version']}).",
           "activities", a["id"], f"submit:{a['id']}:{a['content_version']}:{now_iso()}")
    audit(ctx, "submeter", "activities", a["id"], f"{a['title']} — versão {a['content_version']} enviada para {approver['name']}")
    return ACT.get(ctx, a["id"])


def _decide(ctx, approve):
    db = ctx.db
    a = ACT.raw(db, ctx.params["id"])
    if not a:
        raise not_found("Atividade")
    u = ctx.user
    # Identidade e autoria vêm exclusivamente da sessão autenticada e do banco — nunca do corpo da requisição.
    is_a = db.one("SELECT 1 FROM raci WHERE activity_id=? AND user_id=? AND role='A'", (a["id"], u["id"]))
    if u["is_teacher"]:
        raise _log_blocked(ctx, a, "aprovador_professor", "O professor técnico não pode aprovar ou reprovar entregas, mesmo com perfil de administrador.")
    if a["created_by"] == u["id"]:
        raise _log_blocked(ctx, a, "aprovador_criador", "O criador da atividade não pode aprovar a própria entrega.")
    if a["submitted_by"] == u["id"]:
        raise _log_blocked(ctx, a, "aprovador_autor", "Quem submeteu a entrega não pode decidir sobre a própria submissão.")
    if not is_a:
        raise _log_blocked(ctx, a, "nao_e_aprovador", "Somente o aprovador final (A) definido na matriz RACI pode decidir sobre esta entrega.")
    if a["approval_status"] not in ("aguardando_aprovacao", "reenviada"):
        raise conflict("Esta atividade não está aguardando aprovação.", code="estado_invalido")
    comment = F("long", "Comentário", max=4000).parse(db, "comment", ctx.body.get("comment"))
    if not approve and not comment:
        raise bad("Informe o que precisa ser ajustado ao devolver a entrega.", {"comment": "Campo obrigatório."})
    today = today_local(db)
    if approve:
        end = a["actual_end"] or today
        db.ex("UPDATE activities SET approval_status='aprovada', approved_version=content_version, status='concluida', progress=100, "
              "actual_end=?, updated_at=?, version=version+1 WHERE id=?", (end, now_iso(), a["id"]))
    else:
        db.ex("UPDATE activities SET approval_status='reprovada', status='em_andamento', updated_at=?, version=version+1 WHERE id=?", (now_iso(), a["id"]))
    db.insert("approvals", {"id": new_id(), "activity_id": a["id"], "action": "aprovada" if approve else "reprovada", "user_id": u["id"], "at": now_iso(),
                            "content_version": a["content_version"], "comment": comment, "blocked_reason": None})
    for uid in {a["submitted_by"], a["owner_id"]} - {None, u["id"]}:
        notify(db, uid, "aprovacao", ("Entrega aprovada: " if approve else "Entrega devolvida para ajustes: ") + a["title"],
               comment or f"Decisão de {u['name']}.", "activities", a["id"], f"decision:{a['id']}:{now_iso()}")
    audit(ctx, "aprovar" if approve else "reprovar", "activities", a["id"], f"{a['title']} — versão {a['content_version']}")
    return ACT.get(ctx, a["id"])


route("POST", "/api/activities/{id}/aprovar", allow=INTERNAL)(lambda ctx: _decide(ctx, True))
route("POST", "/api/activities/{id}/reprovar", allow=INTERNAL)(lambda ctx: _decide(ctx, False))


@route("GET", "/api/approvals/pendentes", allow=INTERNAL)
def pending_approvals(ctx):
    db = ctx.db
    rows = db.q("SELECT a.id, a.title, a.area_id, a.season_id, a.submitted_at, a.submitted_by, a.approval_status, a.content_version, r.user_id approver_id "
                "FROM activities a LEFT JOIN raci r ON r.activity_id=a.id AND r.role='A' "
                "WHERE a.deleted_at IS NULL AND a.approval_status IN ('aguardando_aprovacao','reenviada') ORDER BY a.submitted_at")
    names = user_names(db)
    for r in rows:
        r["submitted_by_name"], r["approver_name"] = names.get(r["submitted_by"]), names.get(r["approver_id"])
        r["mine"] = r["approver_id"] == ctx.uid
    if ctx.query.get("season_id"):
        rows = [r for r in rows if r["season_id"] == ctx.query["season_id"]]
    return {"items": rows}
