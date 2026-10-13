"""Central de relatórios: cada relatório é montado dos registros do banco (mesmas funções usadas nas telas)
e pode ser visto em tela (JSON) ou exportado em PDF, Excel e Word."""
import json
from datetime import datetime, timezone

from ..core import (INTERNAL, Raw, bad, fmt_date, forbidden, from_cents, get_setting, judge_view, route, team_tz, today_local, user_names)
from ..exporters import to_docx, to_pdf, to_xlsx
from .activities import APPROVAL_LABEL, STATUS_LABEL, critical_path, load_graph, raci_alerts, schedule_conflicts
from .dashboard import filtered_activities
from .evidences import CRITERIA, MONTHS, TEXT_FIELDS, evidence_indicators, load_week, month_label
from .finance import EXP, EXP_STATUS_LABEL, INC, INC_STATUS_LABEL, acquisition_types, finance_summary
from .risks import RISK, RISK_STATUS_LABEL, expand_risks
from .scope import SECTION_LABELS, analyze, references
from .stakeholders import ST, ST_LABEL, expand_stakeholders, stakeholder_analysis
from .wbs import build_tree

CATALOG = [
    {"id": "executivo", "name": "Relatório executivo do projeto", "module": "dashboard", "description": "Visão geral: indicadores, áreas, prazos, riscos e finanças.",
     "sections": [("resumo", "Indicadores gerais"), ("areas", "Desempenho por área"), ("proximos", "Próximas entregas"), ("atrasos", "Entregas atrasadas"), ("riscos", "Principais riscos"), ("financeiro", "Resumo financeiro"), ("eventos", "Próximos eventos")]},
    {"id": "cronograma", "name": "Relatório de evolução do cronograma", "module": "gantt", "description": "Planejado × realizado de cada atividade, marcos, dependências e alterações.",
     "sections": [("tabela", "Cronograma planejado × realizado"), ("marcos", "Marcos de entrega"), ("conflitos", "Conflitos e dependências em risco"), ("alteracoes", "Histórico de alterações do cronograma")]},
    {"id": "entregas_area", "name": "Relatório de entregas por área", "module": "planejamento", "description": "Entregas de cada área com situação, responsável e prazos.",
     "sections": [("resumo", "Resumo por área"), ("entregas", "Entregas de cada área")]},
    {"id": "atrasos", "name": "Relatório de atrasos e justificativas", "module": "gantt", "description": "Atrasos em aberto, entregas concluídas com atraso e o histórico de justificativas.",
     "sections": [("abertos", "Entregas atrasadas"), ("concluidas", "Entregas concluídas com atraso"), ("historico", "Histórico de registros de atraso")]},
    {"id": "monitoramento", "name": "Relatório de monitoramento mensal", "module": "evidencias", "description": "Consolidado das evidências semanais do mês, por área.", "needs_month": True,
     "sections": [("resumo", "Resumo do mês"), ("registros", "Registros por semana e área"), ("indicadores", "Indicadores semanais"), ("pendencias", "Pendências de registro")]},
    {"id": "financeiro", "name": "Relatório financeiro", "module": "financeiro", "description": "Orçamento, despesas, receitas, movimentos e fluxo de caixa mensal.",
     "sections": [("resumo", "Controle orçamentário"), ("fluxo", "Fluxo de caixa mensal"), ("despesas", "Despesas"), ("apoio", "Itens obtidos por patrocínio ou doação"), ("receitas", "Receitas e captação"), ("movimentos", "Movimentos de caixa"), ("areas", "Custos por área"), ("metodologia", "Fórmulas utilizadas")]},
    {"id": "patrocinios", "name": "Relatório de patrocínios", "module": "stakeholders", "description": "Stakeholders, negociações, valores e contrapartidas.",
     "sections": [("resumo", "Indicadores de captação"), ("stakeholders", "Stakeholders"), ("contrapartidas", "Contrapartidas"), ("mensal", "Captação por mês")]},
    {"id": "riscos", "name": "Relatório de riscos", "module": "riscos", "description": "Matriz de riscos, classificação, planos e ações de mitigação.",
     "sections": [("escala", "Escala e legenda"), ("riscos", "Riscos identificados"), ("acoes", "Ações de mitigação"), ("lacunas", "Riscos sem responsável ou sem plano"), ("residual", "Risco inicial × residual")]},
    {"id": "responsabilidades", "name": "Relatório de responsabilidades", "module": "raci", "description": "Matriz RACI, alertas de governança e histórico de aprovações.",
     "sections": [("matriz", "Matriz RACI"), ("alertas", "Alertas de governança"), ("aprovacoes", "Histórico de aprovações")]},
    {"id": "metas", "name": "Relatório de cumprimento das metas", "module": "planejamento", "description": "Entregas previstas até a data × entregues no prazo, com atraso e pendentes.",
     "sections": [("resumo", "Cumprimento de prazos"), ("areas", "Cumprimento por área"), ("marcos", "Marcos")]},
    {"id": "desempenho", "name": "Relatório de desempenho da temporada", "module": "dashboard", "description": "Evolução mensal das entregas, desempenho semanal, finanças e riscos da temporada.",
     "sections": [("evolucao", "Evolução mensal das entregas"), ("areas", "Desempenho por área"), ("semanal", "Desempenho semanal registrado"), ("financeiro", "Execução financeira"), ("riscos", "Riscos")]},
    {"id": "escopo", "name": "Escopo do projeto", "module": "escopo", "description": "Documento de escopo organizado para apresentação.",
     "sections": [("identificacao", "Identificação"), ("objetivos", "Objetivos e justificativa"), ("entregas", "Entregas"), ("limites", "Limites, premissas e restrições"), ("criterios", "Critérios"), ("referencias", "Referências de cronograma, orçamento, riscos e stakeholders"), ("analise", "Análise crítica")]},
    {"id": "eap", "name": "Estrutura analítica do projeto (EAP)", "module": "eap", "description": "Hierarquia de entregas, pacotes e atividades.",
     "sections": [("estrutura", "Estrutura hierárquica"), ("entregas", "Relatório de entregas por pacote")]},
]
BY_ID = {r["id"]: r for r in CATALOG}


def _opts(ctx):
    q = ctx.query
    o = {"season_id": q.get("season_id"), "from": q.get("from") or None, "to": q.get("to") or None, "area_id": q.get("area_id") or None,
         "month": q.get("month") or None, "members": [m for m in (q.get("members") or "").split(",") if m],
         "sections": [s for s in (q.get("sections") or "").split(",") if s]}
    if not o["season_id"]:
        raise bad("Informe a temporada.")
    return o


def _acts(ctx, o):
    q = {"season_id": o["season_id"], "area_id": o["area_id"], "from": o["from"], "to": o["to"]}
    acts, _, _ = filtered_activities(ctx.db, {k: v for k, v in q.items() if v})
    if o["members"]:
        acts = [a for a in acts if a["owner_id"] in o["members"] or set(a["participants"]) & set(o["members"])]
    return acts


class B:
    """Contexto de montagem: nomes, áreas e atalhos de formatação."""

    def __init__(self, ctx, o):
        self.ctx, self.db, self.o = ctx, ctx.db, o
        self.names = user_names(ctx.db)
        self.areas = {a["id"]: a["name"] for a in ctx.db.q("SELECT id, name FROM areas")}
        self.today = today_local(ctx.db)
        self.sections = []

    def area(self, i):
        return self.areas.get(i, "—")

    def user(self, i):
        return self.names.get(i, "—")

    def add(self, key, heading, **kw):
        if self.o["sections"] and key not in self.o["sections"]:
            return
        self.sections.append({"key": key, "heading": heading, **kw})


def money(v):
    return "—" if v is None else "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(v):
    return "—" if v is None else f"{v:.1f}%".replace(".", ",")


def _act_row(b, a, critical=()):
    dev = a["days_late"] if (a["is_late"] or a["completed_late"]) else 0
    return [b.area(a["area_id"]), a["seq"] or "", a["title"], b.user(a["owner_id"]), fmt_date(a["planned_start"]), fmt_date(a["planned_end"]),
            fmt_date(a["actual_start"]), fmt_date(a["actual_end"]), STATUS_LABEL[a["status"]] + (" (atrasada)" if a["is_late"] else ""), a["progress"], dev,
            ", ".join(f"{p['seq'] or p['title'][:18]} ({p['type']}{'%+d' % p['lag_days'] if p['lag_days'] else ''})" for p in a["predecessors"]) or "—",
            "Sim" if a["id"] in critical else "Não"]


ACT_COLS = ["Área", "Nº", "Atividade", "Responsável", "Início plan.", "Término plan.", "Início real", "Término real", "Situação", "%", "Desvio (dias)", "Depende de", "Crítica"]
ACT_W = [9, 3, 20, 10, 7, 7, 7, 7, 10, 3, 5, 9, 4]


# ----------------------------------------------------------------------------- relatórios
def r_executivo(b):
    acts = _acts(b.ctx, b.o)
    live = [a for a in acts if a["status"] != "cancelada"]
    done = [a for a in live if a["status"] == "concluida"]
    late = [a for a in live if a["is_late"]]
    b.add("resumo", "Indicadores gerais", kv=[
        ("Atividades cadastradas", len(live)), ("Concluídas", len(done)), ("Em andamento", sum(1 for a in live if a["status"] in ("em_andamento", "em_revisao"))),
        ("Não iniciadas", sum(1 for a in live if a["status"] == "nao_iniciada")), ("Atrasadas", len(late)),
        ("Percentual de conclusão", pct(len(done) * 100 / len(live)) if live else "—"),
        ("Concluídas no prazo / com atraso", f"{sum(1 for a in done if not a['completed_late'])} / {sum(1 for a in done if a['completed_late'])}")])
    rows = []
    for aid in list(b.areas) + [None]:
        xs = [a for a in live if a["area_id"] == aid]
        if xs:
            d_ = sum(1 for a in xs if a["status"] == "concluida")
            rows.append([b.area(aid) if aid else "(sem área)", len(xs), d_, sum(1 for a in xs if a["is_late"]), pct(d_ * 100 / len(xs))])
    b.add("areas", "Desempenho por área", table={"columns": ["Área", "Atividades", "Concluídas", "Atrasadas", "% concluído"], "rows": rows})
    soon = sorted([a for a in live if a["status"] != "concluida" and a["planned_end"] and a["planned_end"] >= b.today], key=lambda a: a["planned_end"])[:15]
    b.add("proximos", "Próximas entregas", table={"columns": ["Prazo", "Atividade", "Área", "Responsável", "Situação"], "widths": [6, 20, 9, 10, 8],
                                               "rows": [[fmt_date(a["planned_end"]), a["title"], b.area(a["area_id"]), b.user(a["owner_id"]), STATUS_LABEL[a["status"]]] for a in soon]})
    b.add("atrasos", "Entregas atrasadas", table={"columns": ["Atividade", "Área", "Responsável", "Prazo", "Dias de atraso", "Justificativa"], "widths": [16, 8, 9, 6, 5, 16],
                                               "rows": [[a["title"], b.area(a["area_id"]), b.user(a["owner_id"]), fmt_date(a["planned_end"]), a["days_late"], a["delay_justification"] or "Não registrada"]
                                                        for a in sorted(late, key=lambda a: -a["days_late"])], "empty": "Nenhuma entrega atrasada."})
    risks = _risks(b)
    b.add("riscos", "Principais riscos", table={"columns": ["Código", "Risco", "P", "I", "Pontuação", "Nível", "Situação"], "widths": [5, 22, 3, 3, 5, 6, 8],
                                              "rows": [[r["code"], r["title"], r["probability"], r["impact"], r["score"], r["level"], RISK_STATUS_LABEL[r["status"]]] for r in risks[:10]]})
    if _fin_allowed(b.ctx, detail=False):
        fs = finance_summary(b.db, b.o["season_id"], b.o["from"], b.o["to"], b.o["area_id"])
        b.add("financeiro", "Resumo financeiro", kv=_fin_kv(fs))
    ev = b.db.q("SELECT * FROM events WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status='ativo' AND COALESCE(end_date,start_date)>=? ORDER BY start_date LIMIT 10", (b.o["season_id"], b.today))
    b.add("eventos", "Próximos eventos", table={"columns": ["Evento", "Início", "Término", "Local"], "rows": [[e["name"], fmt_date(e["start_date"]), fmt_date(e["end_date"]), e["location"]] for e in ev], "empty": "Nenhum evento futuro cadastrado."})


def _risks(b):
    rows = expand_risks(b.db, [RISK.ser(r) for r in b.db.q("SELECT * FROM risks WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (b.o["season_id"],))])
    if b.o["area_id"]:
        rows = [r for r in rows if b.o["area_id"] in r["area_ids"]]
    if b.o["members"]:
        rows = [r for r in rows if r["owner_id"] in b.o["members"]]
    rows.sort(key=lambda r: (-(r["score"] or 0), r["code"]))
    return rows


def _fin_allowed(ctx, detail=True):
    if ctx.role != "juiz":
        return True
    jv = judge_view(ctx)
    return bool(jv["modules"].get("financeiro") and (jv["finance_detail"] if detail else (jv["finance_summary"] or jv["finance_detail"])))


def _fin_kv(fs):
    bg, fu, ca = fs["budget"], fs["funding"], fs["cash"]
    return [("Orçamento total planejado", money(bg["planned_total"])), ("Soma dos itens planejados", money(bg["planned_items"])), ("Total comprometido", money(bg["committed"])),
            ("Total realizado", money(bg["realized"])), ("Total pago", money(bg["paid"])), ("Total a pagar", money(bg["payable"])),
            ("Orçamento restante", money(bg["remaining"])), ("Desvio (realizado − planejado)", money(bg["deviation"])), ("Execução do orçamento", pct(bg["execution_pct"])),
            ("Meta de captação", money(fu["goal"])), ("Recursos recebidos", money(fu["received"])), ("Captado e ainda não recebido", money(fu["receivable"])),
            ("Recursos apenas previstos", money(fu["forecast"])), ("Captação confirmada em relação à meta", pct(fu["confirmed_pct"])),
            ("Valor recebido em relação à meta", pct(fu["received_pct"])),
            ("Materiais e serviços obtidos por patrocínio (valor estimado, fora do caixa)", money(bg["in_kind_value"])),
            ("Saldo inicial", money(ca["opening"])), ("Saldo disponível em caixa", money(ca["balance"]))]


def r_cronograma(b):
    acts = _acts(b.ctx, b.o)
    g, deps = load_graph(b.db, b.o["season_id"])
    _, critical = critical_path(g, deps)
    acts.sort(key=lambda a: (b.area(a["area_id"]), a["planned_start"] or a["planned_end"] or "9999", a["seq"] or 0))
    b.add("tabela", "Cronograma planejado × realizado", table={"columns": ACT_COLS, "widths": ACT_W, "rows": [_act_row(b, a, critical) for a in acts if a["status"] != "cancelada"]},
          note="Desvio = dias entre o prazo planejado e a conclusão real (ou a data de hoje, se ainda não concluída). Crítica = sem folga em relação às sucessoras ou ao fim do projeto.")
    b.add("marcos", "Marcos de entrega", table={"columns": ["Marco", "Área", "Prazo planejado", "Conclusão real", "Situação"],
                                             "rows": [[a["title"], b.area(a["area_id"]), fmt_date(a["planned_end"]), fmt_date(a["actual_end"]), STATUS_LABEL[a["status"]]] for a in acts if a["is_milestone"]],
                                             "empty": "Nenhuma atividade marcada como marco."})
    ids = {a["id"] for a in acts}
    cf = [c for c in schedule_conflicts(g, deps, b.today) if c["successor_id"] in ids or c["predecessor_id"] in ids]
    b.add("conflitos", "Conflitos e dependências em risco", table={"columns": ["Tipo", "Predecessora", "Sucessora", "Dependência", "Descrição"], "widths": [5, 12, 12, 5, 26],
                                                                "rows": [["Conflito" if c["kind"] == "conflito" else "Em risco", c["predecessor_title"], c["successor_title"], c["type"], c["message"]] for c in cf],
                                                                "empty": "Nenhum conflito de dependência identificado."})
    hist = b.db.q("SELECT * FROM audit_log WHERE entity_type IN ('activities','schedule','dependencies') AND action IN ('alterar','ajuste_cronograma','registrar_atraso','redefinir_linha_base','criar','excluir') ORDER BY id DESC LIMIT 400")
    rows = []
    for h in hist:
        bf, af = json.loads(h["before"] or "{}"), json.loads(h["after"] or "{}")
        ch = [k for k in ("planned_start", "planned_end", "actual_start", "actual_end", "forecast_end") if k in af and bf.get(k) != af.get(k)]
        if h["entity_type"] == "activities" and h["action"] == "alterar" and not ch:
            continue
        if h["entity_type"] == "activities" and h["action"] in ("criar", "excluir"):
            continue
        det = "; ".join(f"{ {'planned_start': 'início plan.', 'planned_end': 'término plan.', 'actual_start': 'início real', 'actual_end': 'término real', 'forecast_end': 'nova previsão'}[k]}: {fmt_date(bf.get(k))} → {fmt_date(af.get(k))}" for k in ch)
        rows.append([fmt_dt(b.db, h["at"]), h["user_name"] or "—", h["summary"] or "", det or h["action"]])
    b.add("alteracoes", "Histórico de alterações do cronograma", table={"columns": ["Data e hora", "Usuário", "Registro", "Alteração"], "widths": [8, 9, 18, 22], "rows": rows[:150],
                                                                    "empty": "Nenhuma alteração de datas registrada."})


def fmt_dt(db, iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(team_tz(db)).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return iso


def r_entregas_area(b):
    acts = [a for a in _acts(b.ctx, b.o) if a["status"] != "cancelada"]
    rows = []
    groups = [(aid, [a for a in acts if a["area_id"] == aid]) for aid in list(b.areas) + [None]]
    for aid, xs in groups:
        if xs:
            d_ = sum(1 for a in xs if a["status"] == "concluida")
            rows.append([b.area(aid) if aid else "(sem área)", len(xs), d_, sum(1 for a in xs if a["status"] in ("em_andamento", "em_revisao", "aguardando_aprovacao")),
                         sum(1 for a in xs if a["is_late"]), sum(1 for a in xs if a["completed_late"]), pct(d_ * 100 / len(xs))])
    b.add("resumo", "Resumo por área", table={"columns": ["Área", "Entregas", "Concluídas", "Em andamento", "Atrasadas", "Concluídas com atraso", "% concluído"], "rows": rows})
    if b.o["sections"] and "entregas" not in b.o["sections"]:
        return
    for aid, xs in groups:
        if xs:
            xs.sort(key=lambda a: (a["planned_end"] or "9999", a["seq"] or 0))
            b.sections.append({"key": "entregas", "heading": f"Entregas — {b.area(aid) if aid else '(sem área)'}",
                               "table": {"columns": ["Nº", "Entrega", "Responsável", "Prazo", "Conclusão real", "Situação", "%", "Aprovação"], "widths": [3, 22, 10, 7, 7, 10, 3, 9],
                                         "rows": [[a["seq"] or "", a["title"], b.user(a["owner_id"]), fmt_date(a["planned_end"]), fmt_date(a["actual_end"]),
                                                   STATUS_LABEL[a["status"]] + (" (atrasada)" if a["is_late"] else ""), a["progress"], APPROVAL_LABEL[a["approval_status"]]] for a in xs]}})


def r_atrasos(b):
    acts = [a for a in _acts(b.ctx, b.o) if a["status"] != "cancelada"]
    late = sorted([a for a in acts if a["is_late"]], key=lambda a: -a["days_late"])
    b.add("abertos", "Entregas atrasadas", table={
        "columns": ["Atividade", "Área", "Responsável", "Prazo", "Dias", "Justificativa", "Impacto", "Plano de recuperação", "Resp. recuperação", "Nova previsão"],
        "widths": [14, 7, 8, 6, 3, 14, 10, 12, 8, 6],
        "rows": [[a["title"], b.area(a["area_id"]), b.user(a["owner_id"]), fmt_date(a["planned_end"]), a["days_late"], a["delay_justification"] or "NÃO REGISTRADA",
                  a["delay_impact"], a["recovery_plan"], b.user(a["recovery_owner_id"]), fmt_date(a["forecast_end"])] for a in late], "empty": "Nenhuma entrega atrasada."})
    dl = sorted([a for a in acts if a["completed_late"]], key=lambda a: -a["days_late"])
    b.add("concluidas", "Entregas concluídas com atraso", table={"columns": ["Atividade", "Área", "Prazo planejado", "Conclusão real", "Dias de atraso", "Justificativa"], "widths": [16, 8, 7, 7, 5, 18],
                                                            "rows": [[a["title"], b.area(a["area_id"]), fmt_date(a["planned_end"]), fmt_date(a["actual_end"]), a["days_late"], a["delay_justification"] or "Não registrada"] for a in dl],
                                                            "empty": "Nenhuma entrega concluída após o prazo."})
    ids = {a["id"]: a for a in acts}
    recs = [r for r in b.db.q("SELECT * FROM delay_records ORDER BY registered_at DESC") if r["activity_id"] in ids]
    b.add("historico", "Histórico de registros de atraso", table={"columns": ["Registrado em", "Por", "Atividade", "Prazo na época", "Justificativa", "Impacto", "Plano de recuperação", "Nova previsão"],
                                                               "widths": [8, 8, 13, 6, 14, 10, 12, 6],
                                                               "rows": [[fmt_dt(b.db, r["registered_at"]), b.user(r["registered_by"]), ids[r["activity_id"]]["title"], fmt_date(r["planned_end"]), r["justification"],
                                                                         r["impact"], r["recovery_plan"], fmt_date(r["forecast_end"])] for r in recs], "empty": "Nenhum registro de atraso."})


def r_monitoramento(b):
    month = b.o["month"]
    weeks = b.db.q("SELECT * FROM evidence_weeks WHERE season_id=? AND deleted_at IS NULL" + (" AND month=?" if month else "") + " ORDER BY month, week_number",
                   (b.o["season_id"], month) if month else (b.o["season_id"],))
    from .evidences import week_bundle
    bundle = week_bundle(b.db, weeks)
    loaded = [load_week(b.ctx, w, bundle=bundle) for w in weeks]
    total = sum(len([e for e in w["entries"] if e["exists"] and e["status"] == "registrado"]) for w in loaded)
    b.add("resumo", "Resumo do mês", kv=[("Mês de referência", month_label(month) if month else "Todos os meses da temporada"), ("Semanas registradas", len(loaded)),
                                          ("Registros de área concluídos", total), ("Semanas com todas as áreas registradas", sum(1 for w in loaded if w["state"] == "completa"))])
    want = not b.o["sections"] or "registros" in b.o["sections"]
    for w in loaded:
        for e in w["entries"]:
            if not e["exists"] or (b.o["area_id"] and e["area_id"] != b.o["area_id"]) or not want:
                continue
            if b.o["members"] and not set(e["member_ids"]) & set(b.o["members"]):
                continue
            kv = [("Período", f"{fmt_date(w['start_date'])} a {fmt_date(w['end_date'])}" if w["start_date"] else "Datas da semana não informadas"),
                  ("Responsável pelo acompanhamento", w["supervisor_name"] or "—"), ("Data do registro", fmt_date(e["record_date"])),
                  ("Situação do registro", "Registrado" if e["status"] == "registrado" else "Rascunho"),
                  ("Integrantes envolvidos", ", ".join(b.user(u) for u in e["member_ids"]) or "—")]
            kv += [(label, e[k]) for k, label in TEXT_FIELDS.items() if e.get(k)]
            if e.get("file_count"):
                files = b.db.q("SELECT name FROM files WHERE entity_type='evidence_entries' AND entity_id=? AND deleted_at IS NULL", (e["id"],))
                kv.append(("Anexos", "; ".join(f["name"] for f in files)))
            b.sections.append({"key": "registros", "heading": f"{w['month_label']} — Semana {w['week_number']} — {e['area_name']}", "kv": kv})
    ind = evidence_indicators(b.ctx, b.o["season_id"])
    rows = [[w["label"], f"{w['registered']}/{w['areas_total']}", w["deliveries"], w["on_time"], w["late"], w["score"], w["quality"]] for w in ind["weeks"] if not month or w["month"] == month]
    b.add("indicadores", "Indicadores semanais", table={"columns": ["Semana", "Áreas registradas", "Entregas concluídas", "No prazo", "Com atraso", "Pontuação (0–100)", "Qualidade (1–5)"], "rows": rows},
          note="Volume (entregas), desempenho (pontuação composta) e qualidade (avaliação do gestor) são medidas distintas. Semanas sem registro ou sem datas ficam sem valor. "
               "Pesos: " + ", ".join(f"{k} {v}" for k, v in ind["weights"].items()) + ". Critérios: " + " | ".join(f"{k}: {v}" for k, v in CRITERIA.items()))
    pend = [[w["month_label"], w["week_number"], e["area_name"], "Rascunho" if e["exists"] else "Sem registro"] for w in loaded for e in w["entries"] if not e["exists"] or e["status"] != "registrado"]
    b.add("pendencias", "Pendências de registro", table={"columns": ["Mês", "Semana", "Área", "Situação"], "rows": pend, "empty": "Todas as áreas possuem registro nas semanas cadastradas."})


def r_financeiro(b):
    if not _fin_allowed(b.ctx, detail=False):
        raise forbidden("As informações financeiras não estão liberadas para o perfil de juiz.")
    detail = _fin_allowed(b.ctx, detail=True)
    o = b.o
    fs = finance_summary(b.db, o["season_id"], o["from"], o["to"], o["area_id"])
    b.add("resumo", "Controle orçamentário", kv=_fin_kv(fs))
    b.add("fluxo", "Fluxo de caixa mensal", table={
        "columns": ["Mês", "Saldo inicial", "Entradas recebidas", "Saídas pagas", "Fluxo líquido", "Saldo final", "Receitas previstas", "Despesas previstas", "Contas a pagar", "Contas a receber"],
        "rows": [[f"{MONTHS[int(r['month'][5:]) - 1][:3]}/{r['month'][:4]}" + (" (futuro)" if r["is_future"] else ""), r["opening"], r["inflow"], r["outflow"], r["net"], r["closing"],
                  r["expected_income"], r["expected_expense"], r["payable"], r["receivable"]] for r in fs["cashflow"]]},
          note="As colunas de previsão (receitas/despesas previstas, contas a pagar e a receber) são projeções e não entram no saldo.")
    if not detail:
        b.add("metodologia", "Fórmulas utilizadas", kv=list(fs["formulas"].items()))
        return
    types = acquisition_types(b.db)
    exps = EXP.expand(b.ctx, [EXP.ser(e) for e in b.db.q("SELECT * FROM expenses WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL ORDER BY area_id, name", (o["season_id"],))])
    if o["area_id"]:
        exps = [e for e in exps if e["area_id"] == o["area_id"]]
    sts = {s["id"]: s["name"] for s in b.db.q("SELECT id, name FROM stakeholders")}
    b.add("despesas", "Despesas", table={
        "columns": ["Item", "Área", "Categoria", "Fornecedor", "Qtd.", "Un.", "Unit. planejado", "Total planejado", "Total realizado", "Pago", "Pendente", "Situação", "Vencimento", "Pagamento"],
        "widths": [14, 7, 7, 8, 3, 4, 6, 6, 6, 6, 6, 7, 6, 6],
        "rows": [[e["name"], b.area(e["area_id"]), e["category"], e["supplier"], e["quantity"], e["unit"], e["unit_planned"], e["total_planned"], e["total_actual"], e["paid"], e["pending"],
                  EXP_STATUS_LABEL[e["status"]], fmt_date(e["due_date"]), fmt_date(e["payment_date"])] for e in exps if e["is_cash"]], "empty": "Nenhuma despesa em dinheiro cadastrada."})
    b.add("apoio", "Itens obtidos por patrocínio ou doação", table={
        "columns": ["Item", "Área", "Tipo de aquisição", "Patrocinador", "Valor estimado"], "widths": [16, 8, 14, 10, 7],
        "rows": [[e["name"], b.area(e["area_id"]), types.get(e["acquisition_type"], {}).get("label", e["acquisition_type"]), sts.get(e["stakeholder_id"], "—"),
                  e["total_actual"] if e["total_actual"] is not None else e["total_planned"]] for e in exps if not e["is_cash"]], "empty": "Nenhum item recebido por patrocínio ou doação."},
          note="Estes itens não são saídas de caixa: o valor estimado registra apenas o apoio recebido.")
    incs = INC.expand(b.ctx, [INC.ser(i) for i in b.db.q("SELECT * FROM incomes WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL ORDER BY expected_date", (o["season_id"],))])
    b.add("receitas", "Receitas e captação", table={
        "columns": ["Descrição", "Patrocinador", "Fonte", "Data prevista", "Valor previsto", "Valor recebido", "A receber", "Situação", "Último recebimento"], "widths": [14, 9, 7, 6, 6, 6, 6, 9, 6],
        "rows": [[i["description"], sts.get(i["stakeholder_id"], "—"), i["source"], fmt_date(i["expected_date"]), i["expected"], i["received"], i["to_receive"],
                  INC_STATUS_LABEL[i["status"]] if i["receipt_state"] == "nao_recebida" else ("Recebida" if i["receipt_state"] == "recebida" else "Recebida parcialmente"),
                  fmt_date(i["received_date"])] for i in incs], "empty": "Nenhuma receita cadastrada."},
          note="Valores confirmados e ainda não recebidos não fazem parte do saldo em caixa.")
    movs = b.db.q("SELECT m.*, COALESCE(e.name, i.description) origin FROM movements m LEFT JOIN expenses e ON m.origin_type='expense' AND e.id=m.origin_id "
                  "LEFT JOIN incomes i ON m.origin_type='income' AND i.id=m.origin_id WHERE m.season_id=? ORDER BY m.date, m.created_at", (o["season_id"],))
    movs = [m for m in movs if (not o["from"] or m["date"] >= o["from"]) and (not o["to"] or m["date"] <= o["to"])]
    b.add("movimentos", "Movimentos de caixa", table={"columns": ["Data", "Tipo", "Origem", "Valor", "Método", "Observação", "Lançado por"], "widths": [6, 5, 16, 6, 6, 14, 9],
                                                    "rows": [[fmt_date(m["date"]), "Entrada" if m["direction"] == "entrada" else "Saída", m["origin"], from_cents(m["amount_cents"]), m["method"], m["note"], b.user(m["created_by"])] for m in movs],
                                                    "empty": "Nenhum pagamento ou recebimento registrado."})
    b.add("areas", "Custos por área", table={"columns": ["Área", "Planejado", "Realizado", "Pago", "Apoio não financeiro (estimado)"],
                                           "rows": [[r["key"], r["planned"], r["realized"], r["paid"], r["in_kind"]] for r in fs["by_area"]]})
    b.add("metodologia", "Fórmulas utilizadas", kv=list(fs["formulas"].items()))


def r_patrocinios(b):
    an = stakeholder_analysis(b.ctx, b.o["season_id"])
    b.add("resumo", "Indicadores de captação", kv=[
        ("Stakeholders cadastrados", an["total"]), ("Patrocinadores/apoiadores confirmados", an["sponsors_confirmed"]), ("Propostas enviadas", an["proposals"]),
        ("Taxa de conversão de propostas", pct(an["conversion_pct"])), ("Valor solicitado", money(an["requested"])), ("Valor confirmado", money(an["confirmed"])),
        ("Valor efetivamente recebido", money(an["received"])), ("Apoio não financeiro (valor estimado)", money(an["in_kind_value"])),
        ("Contrapartidas pendentes", an["counterparts_pending"])],
          note="Valor confirmado é compromisso do patrocinador; só o valor recebido entra no caixa.")
    rows = expand_stakeholders(b.ctx, [ST.ser(r) for r in b.db.q("SELECT * FROM stakeholders WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL ORDER BY name COLLATE NOCASE", (b.o["season_id"],))])
    if b.o["area_id"]:
        rows = [r for r in rows if r["area_id"] == b.o["area_id"]]
    b.add("stakeholders", "Stakeholders", table={
        "columns": ["Nome", "Tipo", "Necessidade atendida", "Contribuição", "Nível", "Situação", "Solicitado", "Confirmado", "Recebido", "Apoio não financeiro", "Responsável", "Próxima ação"],
        "widths": [10, 6, 14, 6, 5, 8, 6, 6, 6, 6, 8, 10],
        "rows": [[r["name"].strip(), r["type"], r["need"] or r["purpose"], r["contribution_type"], r["level"], ST_LABEL[r["status"]], r["requested"], r["confirmed"], r["received"],
                  r["in_kind_value"], b.user(r["owner_id"]), r["next_action"]] for r in rows]})
    cps = [[r["name"].strip(), c["kind"], c["description"], fmt_date(c["due_date"]), {"pendente": "Pendente", "cumprida": "Cumprida", "cancelada": "Cancelada"}[c["status"]], fmt_date(c["done_date"]), c["record_note"]]
           for r in rows for c in r["counterparts"]]
    b.add("contrapartidas", "Contrapartidas", table={"columns": ["Stakeholder", "Tipo", "Contrapartida", "Prazo", "Situação", "Cumprida em", "Registro"], "widths": [9, 8, 14, 6, 6, 6, 14], "rows": cps,
                                                   "empty": "Nenhuma contrapartida registrada."})
    b.add("mensal", "Captação por mês", table={"columns": ["Mês", "Valor recebido"], "rows": [[x["key"], x["value"]] for x in an["received_by_month"]], "empty": "Nenhum recebimento de patrocínio registrado."})


def r_riscos(b):
    scale = get_setting(b.db, "risk_scale")
    lo = 0
    bands = []
    for bd in scale["bands"]:
        bands.append([bd["name"], f"{lo + 1} a {bd['max']}", bd.get("hint", "")])
        lo = bd["max"]
    b.add("escala", "Escala e legenda", text=[f"Pontuação do risco = probabilidade × impacto (escala de 1 a {scale['size']}).",
                                             "Probabilidade: " + "; ".join(f"{i + 1} = {x}" for i, x in enumerate(scale["probability_labels"])) + ".",
                                             "Impacto: " + "; ".join(f"{i + 1} = {x}" for i, x in enumerate(scale["impact_labels"])) + "."],
          table={"columns": ["Nível", "Faixa de pontuação", "Significado"], "rows": bands})
    risks = _risks(b)
    b.add("riscos", "Riscos identificados", table={
        "columns": ["Código", "Risco", "Causa", "Consequência", "Áreas", "P", "I", "Pontos", "Nível", "Nível na planilha original", "Plano de ação / mitigação", "Responsável", "Situação"],
        "widths": [4, 11, 11, 11, 7, 2, 2, 3, 5, 6, 14, 8, 6],
        "rows": [[r["code"], r["title"], r["cause"], r["consequence"], ", ".join(b.area(a) for a in r["area_ids"]), r["probability"], r["impact"], r["score"], r["level"], r["original_level"],
                  r["mitigation"] or r["preventive_plan"], b.user(r["owner_id"]) if r["owner_id"] else (f"(área) {r['owner_text']}" if r.get("owner_text") else "—"),
                  RISK_STATUS_LABEL[r["status"]]] for r in risks]},
          note="Quando o nível da planilha original difere do calculado, ambos são exibidos — nenhuma classificação foi convertida sem regra explícita.")
    b.add("acoes", "Ações de mitigação", table={"columns": ["Risco", "Ação", "Responsável", "Prazo", "Situação"], "widths": [10, 20, 8, 6, 6],
                                              "rows": [[f"{r['code']} — {r['title']}", a["description"], b.user(a["owner_id"]), fmt_date(a["due_date"]), a["status"]] for r in risks for a in r["actions"]],
                                              "empty": "Nenhuma ação de mitigação cadastrada."})
    b.add("lacunas", "Riscos sem responsável ou sem plano", table={"columns": ["Risco", "Nível", "Sem responsável (pessoa)", "Sem plano de ação"],
                                                                 "rows": [[f"{r['code']} — {r['title']}", r["level"], r["no_owner"], r["no_plan"]] for r in risks if (r["no_owner"] or r["no_plan"]) and r["status"] != "encerrado"],
                                                                 "empty": "Todos os riscos abertos têm responsável e plano."})
    b.add("residual", "Risco inicial × residual", table={"columns": ["Risco", "Pontuação inicial", "Nível inicial", "Pontuação residual", "Nível residual"],
                                                       "rows": [[f"{r['code']} — {r['title']}", r["score"], r["level"], r["residual_score"], r["residual_level"]] for r in risks if r["residual_score"]],
                                                       "empty": "Nenhum risco com avaliação residual registrada."})


def r_responsabilidades(b):
    db, sid = b.db, b.o["season_id"]
    users = [u for u in db.q("SELECT * FROM users WHERE deleted_at IS NULL AND role<>'juiz' ORDER BY is_teacher, name")]
    acts = db.q("SELECT * FROM activities WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL ORDER BY area_id, COALESCE(planned_start, planned_end, '9999'), seq, title", (sid,))
    if b.o["area_id"]:
        acts = [a for a in acts if a["area_id"] == b.o["area_id"]]
    ras = {}
    for r in db.q("SELECT r.* FROM raci r JOIN activities a ON a.id=r.activity_id WHERE a.season_id=?", (sid,)):
        ras.setdefault(r["activity_id"], {})[r["user_id"]] = r["role"]
    cols = [u for u in users if u["active"] or any(u["id"] in m for m in ras.values())]
    if b.o["members"]:
        cols = [u for u in cols if u["id"] in b.o["members"]]
    with_raci = [a for a in acts if a["id"] in ras]
    b.add("matriz", "Matriz RACI", table={"columns": ["Área", "Atividade"] + [u["name"].split()[0] + (" (téc.)" if u["is_teacher"] else "") for u in cols], "widths": [8, 18] + [4] * len(cols),
                                        "rows": [[b.area(a["area_id"]), a["title"]] + [ras[a["id"]].get(u["id"], "") for u in cols] for a in with_raci],
                                        "empty": "Nenhuma atividade com atribuições RACI."},
          note="R = responsável pela execução; A = aprovador final; C = consultado; I = informado. Colunas: " + "; ".join(f"{u['name'].split()[0]} = {u['name']}" for u in cols) +
               f". {len(acts) - len(with_raci)} atividade(s) ainda sem atribuições RACI.")
    um = {u["id"]: u for u in users}
    actives = [u for u in users if u["active"]]
    al = [[b.area(a["area_id"]), a["title"], x["message"]] for a in with_raci for x in raci_alerts(db, a, ras[a["id"]], um, actives) if x["level"] in ("erro", "aviso")]
    b.add("alertas", "Alertas de governança", table={"columns": ["Área", "Atividade", "Alerta"], "widths": [6, 14, 24], "rows": al, "empty": "Nenhum alerta: todas as atividades com RACI têm responsável e aprovador elegível."},
          note="Regras aplicadas no servidor: o professor técnico não aprova entregas; o criador não aprova a própria entrega; um único aprovador final por atividade.")
    ids = {a["id"]: a for a in acts}
    ap = [x for x in db.q("SELECT * FROM approvals ORDER BY at DESC LIMIT 500") if x["activity_id"] in ids]
    lab = {"submetida": "Submetida", "reenviada": "Reenviada", "aprovada": "Aprovada", "reprovada": "Reprovada para ajustes", "invalidada": "Aprovação invalidada", "tentativa_bloqueada": "Tentativa bloqueada"}
    b.add("aprovacoes", "Histórico de aprovações", table={"columns": ["Data e hora", "Atividade", "Evento", "Usuário", "Versão", "Comentário / motivo"], "widths": [8, 14, 8, 8, 3, 18],
                                                        "rows": [[fmt_dt(db, x["at"]), ids[x["activity_id"]]["title"], lab[x["action"]], b.user(x["user_id"]), x["content_version"], x["comment"] or x["blocked_reason"]] for x in ap],
                                                        "empty": "Nenhum evento de aprovação registrado."})


def r_metas(b):
    acts = [a for a in _acts(b.ctx, b.o) if a["status"] != "cancelada" and a["planned_end"]]
    due = [a for a in acts if a["planned_end"] <= b.today]

    def stats(xs):
        on = sum(1 for a in xs if a["status"] == "concluida" and not a["completed_late"])
        lt = sum(1 for a in xs if a["completed_late"])
        pend = sum(1 for a in xs if a["status"] != "concluida")
        return [len(xs), on, lt, pend, pct(on * 100 / len(xs)) if xs else "—"]
    s = stats(due)
    b.add("resumo", "Cumprimento de prazos", kv=[("Data de referência", fmt_date(b.today)), ("Entregas com prazo até a data", s[0]), ("Entregues no prazo", s[1]),
                                                 ("Entregues com atraso", s[2]), ("Ainda pendentes (atrasadas)", s[3]), ("Índice de cumprimento de prazos", s[4]),
                                                 ("Entregas com prazo futuro", len(acts) - len(due))],
          note="Índice de cumprimento = entregas concluídas no prazo ÷ entregas cujo prazo já venceu.")
    b.add("areas", "Cumprimento por área", table={"columns": ["Área", "Com prazo vencido", "No prazo", "Com atraso", "Pendentes", "Índice de cumprimento"],
                                                "rows": [[b.area(aid) if aid else "(sem área)"] + stats([a for a in due if a["area_id"] == aid]) for aid in list(b.areas) + [None] if any(a["area_id"] == aid for a in due)]})
    ms = [a for a in acts if a["is_milestone"]]
    b.add("marcos", "Marcos", table={"columns": ["Marco", "Área", "Prazo", "Conclusão real", "Resultado"],
                                    "rows": [[a["title"], b.area(a["area_id"]), fmt_date(a["planned_end"]), fmt_date(a["actual_end"]),
                                              "No prazo" if a["status"] == "concluida" and not a["completed_late"] else "Com atraso" if a["completed_late"] else "Atrasado" if a["is_late"] else "Previsto"] for a in ms],
                                    "empty": "Nenhuma atividade marcada como marco."})


def r_desempenho(b):
    acts = [a for a in _acts(b.ctx, b.o) if a["status"] != "cancelada"]
    months = sorted({a["planned_end"][:7] for a in acts if a["planned_end"]} | {a["actual_end"][:7] for a in acts if a["actual_end"]})
    cp = cd = 0
    rows = []
    for mo in months:
        p = sum(1 for a in acts if a["planned_end"] and a["planned_end"][:7] == mo)
        d_ = sum(1 for a in acts if a["status"] == "concluida" and a["actual_end"] and a["actual_end"][:7] == mo)
        cp, cd = cp + p, cd + d_
        rows.append([f"{MONTHS[int(mo[5:]) - 1]}/{mo[:4]}", p, d_ if mo <= b.today[:7] else None, cp, cd if mo <= b.today[:7] else None])
    b.add("evolucao", "Evolução mensal das entregas", table={"columns": ["Mês", "Entregas planejadas", "Entregas concluídas", "Planejado acumulado", "Concluído acumulado"], "rows": rows})
    ar = []
    for aid in list(b.areas) + [None]:
        xs = [a for a in acts if a["area_id"] == aid]
        if xs:
            d_ = [a for a in xs if a["status"] == "concluida"]
            ar.append([b.area(aid) if aid else "(sem área)", len(xs), len(d_), sum(1 for a in d_ if not a["completed_late"]), sum(1 for a in d_ if a["completed_late"]),
                       sum(1 for a in xs if a["is_late"]), pct(len(d_) * 100 / len(xs)), round(sum(a["progress"] for a in xs) / len(xs))])
    b.add("areas", "Desempenho por área", table={"columns": ["Área", "Atividades", "Concluídas", "No prazo", "Com atraso", "Atrasadas em aberto", "% concluído", "Progresso médio (%)"], "rows": ar})
    ind = evidence_indicators(b.ctx, b.o["season_id"])
    b.add("semanal", "Desempenho semanal registrado", table={"columns": ["Semana", "Áreas registradas", "Entregas concluídas", "Pontuação (0–100)", "Qualidade (1–5)"],
                                                           "rows": [[w["label"], f"{w['registered']}/{w['areas_total']}", w["deliveries"], w["score"], w["quality"]] for w in ind["weeks"]],
                                                           "empty": "Nenhuma semana de evidências registrada."},
          note="Semanas sem registro ou sem datas não recebem valores.")
    if _fin_allowed(b.ctx, detail=False):
        fs = finance_summary(b.db, b.o["season_id"], b.o["from"], b.o["to"], b.o["area_id"])
        b.add("financeiro", "Execução financeira", kv=_fin_kv(fs))
    risks = _risks(b)
    scale = get_setting(b.db, "risk_scale")
    b.add("riscos", "Riscos", table={"columns": ["Nível", "Riscos em aberto"], "rows": [[bd["name"], sum(1 for r in risks if r["level"] == bd["name"] and r["status"] != "encerrado")] for bd in scale["bands"]]})


def r_escopo(b):
    s = b.db.one("SELECT * FROM scopes WHERE season_id=?", (b.o["season_id"],))
    if not s:
        b.add("identificacao", "Identificação", text="O escopo desta temporada ainda não foi elaborado.")
        return
    d_ = json.loads(s["data"])
    L = SECTION_LABELS
    b.add("identificacao", "Identificação", kv=[(L["project_name"], d_.get("project_name")), (L["team_name"], d_.get("team_name") or get_setting(b.db, "team_name")),
                                                 (L["manager_name"], d_.get("manager_name")), (L["teacher_name"], d_.get("teacher_name")), (L["date"], fmt_date(d_.get("date"))),
                                                 ("Versão do documento", s["doc_version"])])
    objs = d_.get("specific_objectives") or []
    b.add("objetivos", "Objetivos e justificativa", kv=[(L["general_objective"], d_.get("general_objective")),
                                                         (L["specific_objectives"], "\n".join(f"• {o['text']}" for o in objs) or None),
                                                         (L["justification"], d_.get("justification")), (L["expected_results"], "\n".join(f"• {x}" for x in d_.get("expected_results") or []) or None)])
    omap = {o["id"]: o["text"] for o in objs}
    dels = d_.get("deliverables") or []
    b.add("entregas", "Entregas", table={"columns": ["Área", "Entrega", "Principal", "Objetivo relacionado", "Critério de aceitação"], "widths": [8, 18, 4, 12, 14],
                                       "rows": [[b.area(x["area_id"]) if x.get("area_id") else "Geral", x["text"], x.get("main", False), omap.get(x.get("objective_id")), x.get("acceptance")]
                                                for x in sorted(dels, key=lambda x: b.area(x.get("area_id")))], "empty": "Nenhuma entrega cadastrada no escopo."})
    lst = lambda k: "\n".join(f"• {x}" for x in d_.get(k) or []) or None  # noqa: E731
    b.add("limites", "Limites, premissas e restrições", kv=[(L["limits"], d_.get("limits")), (L["included"], lst("included")), (L["excluded"], lst("excluded")),
                                                             (L["assumptions"], lst("assumptions")), (L["constraints"], lst("constraints"))])
    b.add("criterios", "Critérios", kv=[(L["acceptance_criteria"], lst("acceptance_criteria")), (L["approval_criteria"], d_.get("approval_criteria")), (L["approvals_needed"], lst("approvals_needed"))])
    ref = references(b.db, b.o["season_id"])
    b.add("referencias", "Referências de cronograma, orçamento, riscos e stakeholders", kv=[
        (L["schedule_reference"], d_.get("schedule_reference")),
        ("Cronograma cadastrado", f"{fmt_date(ref['schedule']['start'])} a {fmt_date(ref['schedule']['end'])} — {ref['schedule']['activities']} atividade(s), {ref['schedule']['milestones']} marco(s)"),
        (L["budget_reference"], d_.get("budget_reference")),
        ("Orçamento cadastrado", f"Total planejado {money(ref['budget']['planned_total'])}; itens planejados {money(ref['budget']['planned_items'])}; meta de captação {money(ref['budget']['goal'])}"),
        (L["main_risks"], d_.get("main_risks")), ("Riscos de maior pontuação cadastrados", "\n".join(f"• {r['code']} {r['title']} ({r['level']})" for r in ref["top_risks"]) or None),
        (L["main_stakeholders"], d_.get("main_stakeholders")), ("Stakeholders confirmados cadastrados", ", ".join(ref["stakeholders"]) or None)])
    an = analyze(b.db, b.o["season_id"], d_)
    b.add("analise", "Análise crítica", table={"columns": ["Gravidade", "Apontamento", "Itens"], "widths": [5, 18, 22],
                                             "rows": [[{"erro": "Erro", "aviso": "Atenção", "info": "Informação"}[i["level"]], i["message"], "; ".join(i["items"][:12])] for i in an],
                                             "empty": "Nenhum apontamento."})
    vs = b.db.q("SELECT * FROM scope_versions WHERE scope_id=? ORDER BY doc_version DESC", (s["id"],))
    b.sections.append({"key": "versoes", "heading": "Histórico de versões", "table": {"columns": ["Versão", "Data", "Autor", "Resumo"],
                                                                                  "rows": [[v["doc_version"], fmt_dt(b.db, v["created_at"]), b.user(v["created_by"]), v["note"]] for v in vs]}})


def r_eap(b):
    tree = build_tree(b.db, b.o["season_id"])
    if b.o["area_id"]:
        tree = [n for n in tree if n["area_id"] == b.o["area_id"] or n["level"] == 1]
    b.add("estrutura", "Estrutura hierárquica", table={
        "columns": ["Código", "Elemento", "Nível", "Área", "Responsável", "Início", "Término", "Duração (dias)", "Custo estimado", "Atividades vinculadas", "Progresso (%)"],
        "widths": [5, 24, 3, 8, 8, 6, 6, 4, 6, 4, 4],
        "rows": [[n["code"], ("   " * (n["level"] - 1)) + n["name"], n["level"], b.area(n["area_id"]), b.user(n["owner_id"]), fmt_date(n["start_date"]), fmt_date(n["end_date"]),
                  n["duration_days"], n["cost_rollup"], n["activity_count"], n["progress"]] for n in tree]},
          note="Progresso: média das atividades vinculadas (ou valor manual) nas folhas; nos níveis superiores, média dos subelementos. Elementos sem dados contam como 0%.")
    packs = [n for n in tree if n["child_count"] and all(c["child_count"] == 0 for c in tree if c["parent_id"] == n["id"])]
    b.add("entregas", "Relatório de entregas por pacote", table={"columns": ["Código", "Pacote de trabalho", "Área", "Itens", "Itens com dados", "Progresso (%)", "Critérios de aceitação"],
                                                              "widths": [5, 20, 8, 4, 5, 5, 14],
                                                              "rows": [[n["code"], n["name"], b.area(n["area_id"]), n["leaves"], n["leaves_with_data"], n["progress"], n["acceptance_criteria"]] for n in packs]})


BUILDERS = {"executivo": r_executivo, "cronograma": r_cronograma, "entregas_area": r_entregas_area, "atrasos": r_atrasos, "monitoramento": r_monitoramento,
            "financeiro": r_financeiro, "patrocinios": r_patrocinios, "riscos": r_riscos, "responsabilidades": r_responsabilidades, "metas": r_metas,
            "desempenho": r_desempenho, "escopo": r_escopo, "eap": r_eap}
WIDE = {"cronograma", "atrasos", "financeiro", "patrocinios", "riscos", "responsabilidades", "eap"}


def build_report(ctx, rid, o):
    spec = BY_ID.get(rid)
    if not spec:
        raise bad("Relatório desconhecido.")
    if ctx.role == "juiz":
        jv = judge_view(ctx)
        if not jv["modules"].get("relatorios") or not jv["modules"].get(spec["module"]):
            raise forbidden("Este relatório não está disponível para o perfil de juiz.")
    db = ctx.db
    season = db.one("SELECT * FROM seasons WHERE id=? AND deleted_at IS NULL", (o["season_id"],))
    if not season:
        raise bad("Temporada inválida.")
    b = B(ctx, o)
    BUILDERS[rid](b)
    now = datetime.now(timezone.utc).astimezone(team_tz(db))
    if o["month"] and rid == "monitoramento":
        period = month_label(o["month"])
    elif o["from"] or o["to"]:
        period = f"{fmt_date(o['from']) if o['from'] else 'início da temporada'} a {fmt_date(o['to']) if o['to'] else 'data de geração'}"
    else:
        period = f"Temporada completa ({fmt_date(season['start_date'])} a {fmt_date(season['end_date'])})" if season["start_date"] else "Temporada completa"
    filters = []
    if o["area_id"]:
        filters.append(("Área", b.area(o["area_id"])))
    if o["members"]:
        filters.append(("Integrantes", ", ".join(b.user(m) for m in o["members"])))
    theme = get_setting(db, "theme")
    return {"id": rid, "title": spec["name"], "team": get_setting(db, "team_name"), "subtitle": get_setting(db, "team_subtitle"), "season": season["name"],
            "period_label": period, "generated_at": now.strftime("%d/%m/%Y %H:%M") + f" ({get_setting(db, 'timezone')})", "generated_by": ctx.user["name"],
            "source": "banco de dados da plataforma de gestão (registros vigentes na data de geração)", "filters": filters,
            "primary": theme["primary"], "landscape": rid in WIDE, "sections": b.sections}


def _logo(db):
    fid = get_setting(db, "theme").get("logo_file_id")
    f = db.one("SELECT data FROM files WHERE id=? AND deleted_at IS NULL", (fid,)) if fid else None
    return bytes(f["data"]) if f else None


@route("GET", "/api/reports")
def catalog(ctx):
    jv = judge_view(ctx)
    items = [dict(r, sections=[{"key": k, "label": lab} for k, lab in r["sections"]]) for r in CATALOG
             if ctx.role != "juiz" or (jv["modules"].get("relatorios") and jv["modules"].get(r["module"]) and (r["id"] != "financeiro" or jv["finance_summary"] or jv["finance_detail"]))]
    return {"items": items, "formats": ["pdf", "xlsx", "docx"]}


@route("GET", "/api/reports/{id}")
def report(ctx):
    o = _opts(ctx)
    doc = build_report(ctx, ctx.params["id"], o)
    fmt = ctx.query.get("format", "json")
    stamp = datetime.now(timezone.utc).astimezone(team_tz(ctx.db)).strftime("%Y-%m-%d")
    base = f"horizon-{ctx.params['id']}-{stamp}"
    if fmt == "json":
        return doc
    if fmt == "pdf":
        return Raw(to_pdf(doc, _logo(ctx.db)), "application/pdf", base + ".pdf")
    if fmt == "xlsx":
        return Raw(to_xlsx(doc), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", base + ".xlsx")
    if fmt == "docx":
        return Raw(to_docx(doc, _logo(ctx.db)), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", base + ".docx")
    raise bad("Formato inválido (use pdf, xlsx ou docx).")


EXPORT_TABLES = ["seasons", "areas", "users", "activities", "activity_participants", "dependencies", "delay_records", "raci", "approvals", "wbs_elements",
                 "risks", "risk_areas", "risk_links", "risk_actions", "risk_reviews", "events", "evidence_weeks", "evidence_entries", "evidence_members",
                 "evidence_activities", "stakeholders", "counterparts", "expenses", "incomes", "movements", "scopes", "scope_versions", "change_requests", "comments", "templates"]
SECRET_COLS = {"password_hash", "failed_logins", "locked_until"}


def export_all(db, season_id=None):
    out = {}
    for t in EXPORT_TABLES:
        rows = db.q(f"SELECT * FROM {t}")
        out[t] = [{k: v for k, v in r.items() if k not in SECRET_COLS} for r in rows]
    return out


@route("GET", "/api/admin/exportar", allow=("admin",))
def export_data(ctx):
    """Exportação completa dos dados (sem senhas nem sessões), em JSON ou em Excel (uma aba por tabela)."""
    from ..core import audit
    data = export_all(ctx.db)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    if ctx.query.get("format") == "xlsx":
        from openpyxl import Workbook
        import io
        wb = Workbook()
        wb.remove(wb.active)
        for t, rows in data.items():
            ws = wb.create_sheet(t[:31])
            cols = list(rows[0].keys()) if rows else [c for c in ctx.db.columns(t) if c not in SECRET_COLS]
            ws.append(cols)
            for r in rows:
                ws.append([r[c] if not isinstance(r[c], (bytes, dict, list)) else str(r[c]) for c in cols])
            ws.freeze_panes = "A2"
        buf = io.BytesIO()
        wb.save(buf)
        return Raw(buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"horizon-dados-{stamp}.xlsx")
    payload = json.dumps({"exported_at": stamp, "note": "Valores monetários em centavos (colunas *_cents). Senhas e sessões não são exportadas.", "tables": data}, ensure_ascii=False, indent=1)
    return Raw(payload.encode("utf-8"), "application/json", f"horizon-dados-{stamp}.json")
