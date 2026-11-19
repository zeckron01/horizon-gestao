"""Central de relatórios: cada relatório é montado dos registros do banco (mesmas funções usadas nas telas)
e pode ser visto em tela (JSON) ou exportado em PDF, Excel e Word."""
from ..i18n import _t, _tc, fmt_dt_l, fmt_num_l, localize, money_l
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
    {"id": "executivo", "name": _t("Relatório executivo do projeto"), "module": "dashboard", "description": _t("Visão geral: indicadores, áreas, prazos, riscos e finanças."),
     "sections": [("resumo", _t("Indicadores gerais")), ("areas", _t("Desempenho por área")), ("proximos", _t("Próximas entregas")), ("atrasos", _t("Entregas atrasadas")), ("riscos", _t("Principais riscos")), ("financeiro", _t("Resumo financeiro")), ("eventos", _t("Próximos eventos"))]},
    {"id": "cronograma", "name": _t("Relatório de evolução do cronograma"), "module": "gantt", "description": _t("Planejado × realizado de cada atividade, marcos, dependências e alterações."),
     "sections": [("tabela", _t("Cronograma planejado × realizado")), ("marcos", _t("Marcos de entrega")), ("conflitos", _t("Conflitos e dependências em risco")), ("alteracoes", _t("Histórico de alterações do cronograma"))]},
    {"id": "entregas_area", "name": _t("Relatório de entregas por área"), "module": "planejamento", "description": _t("Entregas de cada área com situação, responsável e prazos."),
     "sections": [("resumo", _t("Resumo por área")), ("entregas", _t("Entregas de cada área"))]},
    {"id": "atrasos", "name": _t("Relatório de atrasos e justificativas"), "module": "gantt", "description": _t("Atrasos em aberto, entregas concluídas com atraso e o histórico de justificativas."),
     "sections": [("abertos", _t("Entregas atrasadas")), ("concluidas", _t("Entregas concluídas com atraso")), ("historico", _t("Histórico de registros de atraso"))]},
    {"id": "monitoramento", "name": _t("Relatório de monitoramento mensal"), "module": "evidencias", "description": _t("Consolidado das evidências semanais do mês, por área."), "needs_month": True,
     "sections": [("resumo", _t("Resumo do mês")), ("registros", _t("Registros por semana e área")), ("indicadores", _t("Indicadores semanais")), ("pendencias", _t("Pendências de registro"))]},
    {"id": "financeiro", "name": _t("Relatório financeiro"), "module": "financeiro", "description": _t("Orçamento, despesas, receitas, movimentos e fluxo de caixa mensal."),
     "sections": [("resumo", _t("Controle orçamentário")), ("fluxo", _t("Fluxo de caixa mensal")), ("despesas", _t("Despesas")), ("apoio", _t("Itens obtidos por patrocínio ou doação")), ("receitas", _t("Receitas e captação")), ("movimentos", _t("Movimentos de caixa")), ("areas", _t("Custos por área")), ("metodologia", _t("Fórmulas utilizadas"))]},
    {"id": "patrocinios", "name": _t("Relatório de patrocínios"), "module": "stakeholders", "description": _t("Stakeholders, negociações, valores e contrapartidas."),
     "sections": [("resumo", _t("Indicadores de captação")), ("stakeholders", _t("Stakeholders")), ("contrapartidas", _t("Contrapartidas")), ("mensal", _t("Captação por mês"))]},
    {"id": "riscos", "name": _t("Relatório de riscos"), "module": "riscos", "description": _t("Matriz de riscos, classificação, planos e ações de mitigação."),
     "sections": [("escala", _t("Escala e legenda")), ("riscos", _t("Riscos identificados")), ("acoes", _t("Ações de mitigação")), ("lacunas", _t("Riscos sem responsável ou sem plano")), ("residual", _t("Risco inicial × residual"))]},
    {"id": "responsabilidades", "name": _t("Relatório de responsabilidades"), "module": "raci", "description": _t("Matriz RACI, alertas de governança e histórico de aprovações."),
     "sections": [("matriz", _t("Matriz RACI")), ("alertas", _t("Alertas de governança")), ("aprovacoes", _t("Histórico de aprovações"))]},
    {"id": "metas", "name": _t("Relatório de cumprimento das metas"), "module": "planejamento", "description": _t("Entregas previstas até a data × entregues no prazo, com atraso e pendentes."),
     "sections": [("resumo", _t("Cumprimento de prazos")), ("areas", _t("Cumprimento por área")), ("marcos", _t("Marcos"))]},
    {"id": "desempenho", "name": _t("Relatório de desempenho da temporada"), "module": "dashboard", "description": _t("Evolução mensal das entregas, desempenho semanal, finanças e riscos da temporada."),
     "sections": [("evolucao", _t("Evolução mensal das entregas")), ("areas", _t("Desempenho por área")), ("semanal", _t("Desempenho semanal registrado")), ("financeiro", _t("Execução financeira")), ("riscos", _t("Riscos"))]},
    {"id": "escopo", "name": _tc("menu-escopo", "Escopo do projeto"), "module": "escopo", "description": _t("Documento de escopo organizado para apresentação."),
     "sections": [("identificacao", _t("Identificação")), ("objetivos", _t("Objetivos e justificativa")), ("entregas", _t("Entregas")), ("limites", _t("Limites, premissas e restrições")), ("criterios", _t("Critérios")), ("referencias", _t("Referências de cronograma, orçamento, riscos e stakeholders")), ("analise", _t("Análise crítica"))]},
    {"id": "caminho_critico", "name": _t("Caminho crítico: problemas e caminhos até a competição"), "module": "caminho_critico",
     "description": _t("Para cada risco cadastrado: atividades afetadas, dias de atraso estimados, caminho crítico resultante e nova previsão em relação à data-limite. Simulação; o plano oficial não muda."),
     "sections": [("resumo", _t("Situação oficial")), ("problemas", _t("Problemas e impacto no prazo")), ("caminhos", _t("Caminho de cada problema até a competição")), ("respostas", _t("Respostas previstas"))]},
    {"id": "eap", "name": _t("Estrutura analítica do projeto (EAP)"), "module": "eap", "description": _t("Hierarquia de entregas, pacotes e atividades."),
     "sections": [("estrutura", _t("Estrutura hierárquica")), ("entregas", _t("Relatório de entregas por pacote"))]},
]
BY_ID = {r["id"]: r for r in CATALOG}


def _opts(ctx):
    q = ctx.query
    o = {"season_id": q.get("season_id"), "from": q.get("from") or None, "to": q.get("to") or None, "area_id": q.get("area_id") or None,
         "month": q.get("month") or None, "members": [m for m in (q.get("members") or "").split(",") if m],
         "sections": [s for s in (q.get("sections") or "").split(",") if s]}
    if not o["season_id"]:
        raise bad(_t("Informe a temporada."))
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
    return money_l(v)


def pct(v):
    return "—" if v is None else fmt_num_l(v, 1) + "%"


def _act_row(b, a, critical=()):
    dev = a["days_late"] if (a["is_late"] or a["completed_late"]) else 0
    return [b.area(a["area_id"]), a["seq"] or "", a["title"], b.user(a["owner_id"]), fmt_date(a["planned_start"]), fmt_date(a["planned_end"]),
            fmt_date(a["actual_start"]), fmt_date(a["actual_end"]), STATUS_LABEL[a["status"]] + (" (atrasada)" if a["is_late"] else ""), a["progress"], dev,
            ", ".join(f"{p['seq'] or p['title'][:18]} ({p['type']}{'%+d' % p['lag_days'] if p['lag_days'] else ''})" for p in a["predecessors"]) or "—",
            _t("Sim") if a["id"] in critical else _t("Não")]


ACT_COLS = [_t("Área"), "Nº", _t("Atividade"), _t("Responsável"), _t("Início plan."), _t("Término plan."), _t("Início real"), _t("Término real"), _t("Situação"), "%", _t("Desvio (dias)"), _t("Depende de"), _t("Crítica")]
ACT_W = [9, 3, 20, 10, 7, 7, 7, 7, 10, 3, 5, 9, 4]


# ----------------------------------------------------------------------------- relatórios
def r_executivo(b):
    acts = _acts(b.ctx, b.o)
    live = [a for a in acts if a["status"] != "cancelada"]
    done = [a for a in live if a["status"] == "concluida"]
    late = [a for a in live if a["is_late"]]
    b.add("resumo", _t("Indicadores gerais"), kv=[
        (_t("Atividades cadastradas"), len(live)), (_t("Concluídas"), len(done)), (_t("Em andamento"), sum(1 for a in live if a["status"] in ("em_andamento", "em_revisao"))),
        (_t("Não iniciadas"), sum(1 for a in live if a["status"] == "nao_iniciada")), (_t("Atrasadas"), len(late)),
        (_t("Percentual de conclusão"), pct(len(done) * 100 / len(live)) if live else "—"),
        (_t("Concluídas no prazo / com atraso"), f"{sum(1 for a in done if not a['completed_late'])} / {sum(1 for a in done if a['completed_late'])}")])
    rows = []
    for aid in list(b.areas) + [None]:
        xs = [a for a in live if a["area_id"] == aid]
        if xs:
            d_ = sum(1 for a in xs if a["status"] == "concluida")
            rows.append([b.area(aid) if aid else _t("(sem área)"), len(xs), d_, sum(1 for a in xs if a["is_late"]), pct(d_ * 100 / len(xs))])
    b.add("areas", _t("Desempenho por área"), table={"columns": [_t("Área"), _t("Atividades"), _t("Concluídas"), _t("Atrasadas"), "% concluído"], "rows": rows})
    soon = sorted([a for a in live if a["status"] != "concluida" and a["planned_end"] and a["planned_end"] >= b.today], key=lambda a: a["planned_end"])[:15]
    b.add("proximos", _t("Próximas entregas"), table={"columns": [_t("Prazo"), _t("Atividade"), _t("Área"), _t("Responsável"), _t("Situação")], "widths": [6, 20, 9, 10, 8],
                                               "rows": [[fmt_date(a["planned_end"]), a["title"], b.area(a["area_id"]), b.user(a["owner_id"]), STATUS_LABEL[a["status"]]] for a in soon]})
    b.add("atrasos", _t("Entregas atrasadas"), table={"columns": [_t("Atividade"), _t("Área"), _t("Responsável"), _t("Prazo"), _t("Dias de atraso"), _t("Justificativa")], "widths": [16, 8, 9, 6, 5, 16],
                                               "rows": [[a["title"], b.area(a["area_id"]), b.user(a["owner_id"]), fmt_date(a["planned_end"]), a["days_late"], a["delay_justification"] or _t("Não registrada")]
                                                        for a in sorted(late, key=lambda a: -a["days_late"])], "empty": _t("Nenhuma entrega atrasada.")})
    risks = _risks(b)
    b.add("riscos", _t("Principais riscos"), table={"columns": [_t("Código"), _t("Risco"), "P", "I", _t("Pontuação"), _t("Nível"), _t("Situação")], "widths": [5, 22, 3, 3, 5, 6, 8],
                                              "rows": [[r["code"], r["title"], r["probability"], r["impact"], r["score"], r["level"], RISK_STATUS_LABEL[r["status"]]] for r in risks[:10]]})
    if _fin_allowed(b.ctx, detail=False):
        fs = finance_summary(b.db, b.o["season_id"], b.o["from"], b.o["to"], b.o["area_id"])
        b.add("financeiro", _t("Resumo financeiro"), kv=_fin_kv(fs))
    ev = b.db.q("SELECT * FROM events WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status='ativo' AND COALESCE(end_date,start_date)>=? ORDER BY start_date LIMIT 10", (b.o["season_id"], b.today))
    b.add("eventos", _t("Próximos eventos"), table={"columns": [_t("Evento"), _t("Início"), _t("Término"), _t("Local")], "rows": [[e["name"], fmt_date(e["start_date"]), fmt_date(e["end_date"]), e["location"]] for e in ev], "empty": _t("Nenhum evento futuro cadastrado.")})


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
    return [(_t("Orçamento total planejado"), money(bg["planned_total"])), (_t("Soma dos itens planejados"), money(bg["planned_items"])), (_t("Total comprometido"), money(bg["committed"])),
            (_t("Total realizado"), money(bg["realized"])), (_t("Total pago"), money(bg["paid"])), (_t("Total a pagar"), money(bg["payable"])),
            (_t("Orçamento restante"), money(bg["remaining"])), (_t("Desvio (realizado − planejado)"), money(bg["deviation"])), (_t("Execução do orçamento"), pct(bg["execution_pct"])),
            (_t("Meta de captação"), money(fu["goal"])), (_t("Recursos recebidos"), money(fu["received"])), (_t("Captado e ainda não recebido"), money(fu["receivable"])),
            (_t("Recursos apenas previstos"), money(fu["forecast"])), (_t("Captação confirmada em relação à meta"), pct(fu["confirmed_pct"])),
            (_t("Valor recebido em relação à meta"), pct(fu["received_pct"])),
            (_t("Materiais e serviços obtidos por patrocínio (valor estimado, fora do caixa)"), money(bg["in_kind_value"])),
            (_t("Saldo inicial"), money(ca["opening"])), (_t("Saldo disponível em caixa"), money(ca["balance"]))]


def r_cronograma(b):
    acts = _acts(b.ctx, b.o)
    g, deps = load_graph(b.db, b.o["season_id"])
    _, critical = critical_path(g, deps)
    acts.sort(key=lambda a: (b.area(a["area_id"]), a["planned_start"] or a["planned_end"] or "9999", a["seq"] or 0))
    b.add("tabela", _t("Cronograma planejado × realizado"), table={"columns": ACT_COLS, "widths": ACT_W, "rows": [_act_row(b, a, critical) for a in acts if a["status"] != "cancelada"]},
          note=_t("Desvio = dias entre o prazo planejado e a conclusão real (ou a data de hoje, se ainda não concluída). Crítica = sem folga em relação às sucessoras ou ao fim do projeto."))
    b.add("marcos", _t("Marcos de entrega"), table={"columns": [_t("Marco"), _t("Área"), _t("Prazo planejado"), _t("Conclusão real"), _t("Situação")],
                                             "rows": [[a["title"], b.area(a["area_id"]), fmt_date(a["planned_end"]), fmt_date(a["actual_end"]), STATUS_LABEL[a["status"]]] for a in acts if a["is_milestone"]],
                                             "empty": _t("Nenhuma atividade marcada como marco.")})
    ids = {a["id"] for a in acts}
    cf = [c for c in schedule_conflicts(g, deps, b.today) if c["successor_id"] in ids or c["predecessor_id"] in ids]
    b.add("conflitos", _t("Conflitos e dependências em risco"), table={"columns": [_t("Tipo"), _t("Predecessora"), _t("Sucessora"), _t("Dependência"), _t("Descrição")], "widths": [5, 12, 12, 5, 26],
                                                                "rows": [[_t("Conflito") if c["kind"] == "conflito" else _t("Em risco"), c["predecessor_title"], c["successor_title"], c["type"], c["message"]] for c in cf],
                                                                "empty": _t("Nenhum conflito de dependência identificado.")})
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
    b.add("alteracoes", _t("Histórico de alterações do cronograma"), table={"columns": [_t("Data e hora"), _t("Usuário"), _t("Registro"), _t("Alteração")], "widths": [8, 9, 18, 22], "rows": rows[:150],
                                                                    "empty": _t("Nenhuma alteração de datas registrada.")})


def fmt_dt(db, iso):
    if not iso:
        return "—"
    try:
        return fmt_dt_l(datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(team_tz(db)))
    except ValueError:
        return iso


def r_entregas_area(b):
    acts = [a for a in _acts(b.ctx, b.o) if a["status"] != "cancelada"]
    rows = []
    groups = [(aid, [a for a in acts if a["area_id"] == aid]) for aid in list(b.areas) + [None]]
    for aid, xs in groups:
        if xs:
            d_ = sum(1 for a in xs if a["status"] == "concluida")
            rows.append([b.area(aid) if aid else _t("(sem área)"), len(xs), d_, sum(1 for a in xs if a["status"] in ("em_andamento", "em_revisao", "aguardando_aprovacao")),
                         sum(1 for a in xs if a["is_late"]), sum(1 for a in xs if a["completed_late"]), pct(d_ * 100 / len(xs))])
    b.add("resumo", _t("Resumo por área"), table={"columns": [_t("Área"), _t("Entregas"), _t("Concluídas"), _t("Em andamento"), _t("Atrasadas"), _t("Concluídas com atraso"), "% concluído"], "rows": rows})
    if b.o["sections"] and "entregas" not in b.o["sections"]:
        return
    for aid, xs in groups:
        if xs:
            xs.sort(key=lambda a: (a["planned_end"] or "9999", a["seq"] or 0))
            b.sections.append({"key": "entregas", "heading": _t("Entregas — {0}", b.area(aid) if aid else _t('(sem área)')),
                               "table": {"columns": ["Nº", _t("Entrega"), _t("Responsável"), _t("Prazo"), _t("Conclusão real"), _t("Situação"), "%", _t("Aprovação")], "widths": [3, 22, 10, 7, 7, 10, 3, 9],
                                         "rows": [[a["seq"] or "", a["title"], b.user(a["owner_id"]), fmt_date(a["planned_end"]), fmt_date(a["actual_end"]),
                                                   STATUS_LABEL[a["status"]] + (" (atrasada)" if a["is_late"] else ""), a["progress"], APPROVAL_LABEL[a["approval_status"]]] for a in xs]}})


def r_atrasos(b):
    acts = [a for a in _acts(b.ctx, b.o) if a["status"] != "cancelada"]
    late = sorted([a for a in acts if a["is_late"]], key=lambda a: -a["days_late"])
    b.add("abertos", _t("Entregas atrasadas"), table={
        "columns": [_t("Atividade"), _t("Área"), _t("Responsável"), _t("Prazo"), _t("Dias"), _t("Justificativa"), _t("Impacto"), _t("Plano de recuperação"), _t("Resp. recuperação"), _t("Nova previsão")],
        "widths": [14, 7, 8, 6, 3, 14, 10, 12, 8, 6],
        "rows": [[a["title"], b.area(a["area_id"]), b.user(a["owner_id"]), fmt_date(a["planned_end"]), a["days_late"], a["delay_justification"] or _t("NÃO REGISTRADA"),
                  a["delay_impact"], a["recovery_plan"], b.user(a["recovery_owner_id"]), fmt_date(a["forecast_end"])] for a in late], "empty": _t("Nenhuma entrega atrasada.")})
    dl = sorted([a for a in acts if a["completed_late"]], key=lambda a: -a["days_late"])
    b.add("concluidas", _t("Entregas concluídas com atraso"), table={"columns": [_t("Atividade"), _t("Área"), _t("Prazo planejado"), _t("Conclusão real"), _t("Dias de atraso"), _t("Justificativa")], "widths": [16, 8, 7, 7, 5, 18],
                                                            "rows": [[a["title"], b.area(a["area_id"]), fmt_date(a["planned_end"]), fmt_date(a["actual_end"]), a["days_late"], a["delay_justification"] or _t("Não registrada")] for a in dl],
                                                            "empty": _t("Nenhuma entrega concluída após o prazo.")})
    ids = {a["id"]: a for a in acts}
    recs = [r for r in b.db.q("SELECT * FROM delay_records ORDER BY registered_at DESC") if r["activity_id"] in ids]
    b.add("historico", _t("Histórico de registros de atraso"), table={"columns": [_t("Registrado em"), _t("Por"), _t("Atividade"), _t("Prazo na época"), _t("Justificativa"), _t("Impacto"), _t("Plano de recuperação"), _t("Nova previsão")],
                                                               "widths": [8, 8, 13, 6, 14, 10, 12, 6],
                                                               "rows": [[fmt_dt(b.db, r["registered_at"]), b.user(r["registered_by"]), ids[r["activity_id"]]["title"], fmt_date(r["planned_end"]), r["justification"],
                                                                         r["impact"], r["recovery_plan"], fmt_date(r["forecast_end"])] for r in recs], "empty": _t("Nenhum registro de atraso.")})


def r_monitoramento(b):
    month = b.o["month"]
    weeks = b.db.q("SELECT * FROM evidence_weeks WHERE season_id=? AND deleted_at IS NULL" + (" AND month=?" if month else "") + " ORDER BY month, week_number",
                   (b.o["season_id"], month) if month else (b.o["season_id"],))
    from .evidences import week_bundle
    bundle = week_bundle(b.db, weeks)
    loaded = [load_week(b.ctx, w, bundle=bundle) for w in weeks]
    total = sum(len([e for e in w["entries"] if e["exists"] and e["status"] == "registrado"]) for w in loaded)
    b.add("resumo", _t("Resumo do mês"), kv=[(_t("Mês de referência"), month_label(month) if month else _t("Todos os meses da temporada")), (_t("Semanas registradas"), len(loaded)),
                                          (_t("Registros de área concluídos"), total), (_t("Semanas com todas as áreas registradas"), sum(1 for w in loaded if w["state"] == "completa"))])
    want = not b.o["sections"] or "registros" in b.o["sections"]
    for w in loaded:
        for e in w["entries"]:
            if not e["exists"] or (b.o["area_id"] and e["area_id"] != b.o["area_id"]) or not want:
                continue
            if b.o["members"] and not set(e["member_ids"]) & set(b.o["members"]):
                continue
            kv = [(_t("Período"), f"{fmt_date(w['start_date'])} a {fmt_date(w['end_date'])}" if w["start_date"] else _t("Datas da semana não informadas")),
                  (_t("Responsável pelo acompanhamento"), w["supervisor_name"] or "—"), (_t("Data do registro"), fmt_date(e["record_date"])),
                  (_t("Situação do registro"), _t("Registrado") if e["status"] == "registrado" else _t("Rascunho")),
                  (_t("Integrantes envolvidos"), ", ".join(b.user(u) for u in e["member_ids"]) or "—")]
            kv += [(label, e[k]) for k, label in TEXT_FIELDS.items() if e.get(k)]
            if e.get("file_count"):
                files = b.db.q("SELECT name FROM files WHERE entity_type='evidence_entries' AND entity_id=? AND deleted_at IS NULL", (e["id"],))
                kv.append((_t("Anexos"), "; ".join(f["name"] for f in files)))
            b.sections.append({"key": "registros", "heading": _t("{0} — Semana {1} — {2}", w['month_label'], w['week_number'], e['area_name']), "kv": kv})
    ind = evidence_indicators(b.ctx, b.o["season_id"])
    rows = [[w["label"], f"{w['registered']}/{w['areas_total']}", w["deliveries"], w["on_time"], w["late"], w["score"], w["quality"]] for w in ind["weeks"] if not month or w["month"] == month]
    b.add("indicadores", _t("Indicadores semanais"), table={"columns": [_t("Semana"), _t("Áreas registradas"), _t("Entregas concluídas"), _t("No prazo"), _t("Com atraso"), _t("Pontuação (0–100)"), _t("Qualidade (1–5)")], "rows": rows},
          note=_t("Volume (entregas), desempenho (pontuação composta) e qualidade (avaliação do gestor) são medidas distintas. Semanas sem registro ou sem datas ficam sem valor. "
               "Pesos: ") + ", ".join(f"{k} {v}" for k, v in ind["weights"].items()) + ". Critérios: " + " | ".join(f"{k}: {v}" for k, v in CRITERIA.items()))
    pend = [[w["month_label"], w["week_number"], e["area_name"], _t("Rascunho") if e["exists"] else _t("Sem registro")] for w in loaded for e in w["entries"] if not e["exists"] or e["status"] != "registrado"]
    b.add("pendencias", _t("Pendências de registro"), table={"columns": [_t("Mês"), _t("Semana"), _t("Área"), _t("Situação")], "rows": pend, "empty": _t("Todas as áreas possuem registro nas semanas cadastradas.")})


def r_financeiro(b):
    if not _fin_allowed(b.ctx, detail=False):
        raise forbidden(_t("As informações financeiras não estão liberadas para o perfil de juiz."))
    detail = _fin_allowed(b.ctx, detail=True)
    o = b.o
    fs = finance_summary(b.db, o["season_id"], o["from"], o["to"], o["area_id"])
    b.add("resumo", _t("Controle orçamentário"), kv=_fin_kv(fs))
    b.add("fluxo", _t("Fluxo de caixa mensal"), table={
        "columns": [_t("Mês"), _t("Saldo inicial"), _t("Entradas recebidas"), _t("Saídas pagas"), _t("Fluxo líquido"), _t("Saldo final"), _t("Receitas previstas"), _t("Despesas previstas"), _t("Contas a pagar"), _t("Contas a receber")],
        "rows": [[f"{MONTHS[int(r['month'][5:]) - 1][:3]}/{r['month'][:4]}" + (" (futuro)" if r["is_future"] else ""), r["opening"], r["inflow"], r["outflow"], r["net"], r["closing"],
                  r["expected_income"], r["expected_expense"], r["payable"], r["receivable"]] for r in fs["cashflow"]]},
          note=_t("As colunas de previsão (receitas/despesas previstas, contas a pagar e a receber) são projeções e não entram no saldo."))
    if not detail:
        b.add("metodologia", _t("Fórmulas utilizadas"), kv=list(fs["formulas"].items()))
        return
    types = acquisition_types(b.db)
    exps = EXP.expand(b.ctx, [EXP.ser(e) for e in b.db.q("SELECT * FROM expenses WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL ORDER BY area_id, name", (o["season_id"],))])
    if o["area_id"]:
        exps = [e for e in exps if e["area_id"] == o["area_id"]]
    sts = {s["id"]: s["name"] for s in b.db.q("SELECT id, name FROM stakeholders")}
    b.add("despesas", _t("Despesas"), table={
        "columns": [_t("Item"), _t("Área"), _t("Categoria"), _t("Fornecedor"), _t("Qtd."), _t("Un."), _t("Unit. planejado"), _t("Total planejado"), _t("Total realizado"), _t("Pago"), _t("Pendente"), _t("Situação"), _t("Vencimento"), _t("Pagamento")],
        "widths": [14, 7, 7, 8, 3, 4, 6, 6, 6, 6, 6, 7, 6, 6],
        "rows": [[e["name"], b.area(e["area_id"]), e["category"], e["supplier"], e["quantity"], e["unit"], e["unit_planned"], e["total_planned"], e["total_actual"], e["paid"], e["pending"],
                  EXP_STATUS_LABEL[e["status"]], fmt_date(e["due_date"]), fmt_date(e["payment_date"])] for e in exps if e["is_cash"]], "empty": _t("Nenhuma despesa em dinheiro cadastrada.")})
    b.add("apoio", _t("Itens obtidos por patrocínio ou doação"), table={
        "columns": [_t("Item"), _t("Área"), _t("Tipo de aquisição"), _t("Patrocinador"), _t("Valor estimado")], "widths": [16, 8, 14, 10, 7],
        "rows": [[e["name"], b.area(e["area_id"]), types.get(e["acquisition_type"], {}).get("label", e["acquisition_type"]), sts.get(e["stakeholder_id"], "—"),
                  e["total_actual"] if e["total_actual"] is not None else e["total_planned"]] for e in exps if not e["is_cash"]], "empty": _t("Nenhum item recebido por patrocínio ou doação.")},
          note=_t("Estes itens não são saídas de caixa: o valor estimado registra apenas o apoio recebido."))
    incs = INC.expand(b.ctx, [INC.ser(i) for i in b.db.q("SELECT * FROM incomes WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL ORDER BY expected_date", (o["season_id"],))])
    b.add("receitas", _t("Receitas e captação"), table={
        "columns": [_t("Descrição"), _t("Patrocinador"), _t("Fonte"), _t("Data prevista"), _t("Valor previsto"), _t("Valor recebido"), _t("A receber"), _t("Situação"), _t("Último recebimento")], "widths": [14, 9, 7, 6, 6, 6, 6, 9, 6],
        "rows": [[i["description"], sts.get(i["stakeholder_id"], "—"), i["source"], fmt_date(i["expected_date"]), i["expected"], i["received"], i["to_receive"],
                  INC_STATUS_LABEL[i["status"]] if i["receipt_state"] == "nao_recebida" else (_t("Recebida") if i["receipt_state"] == "recebida" else _t("Recebida parcialmente")),
                  fmt_date(i["received_date"])] for i in incs], "empty": _t("Nenhuma receita cadastrada.")},
          note=_t("Valores confirmados e ainda não recebidos não fazem parte do saldo em caixa."))
    movs = b.db.q("SELECT m.*, COALESCE(e.name, i.description) origin FROM movements m LEFT JOIN expenses e ON m.origin_type='expense' AND e.id=m.origin_id "
                  "LEFT JOIN incomes i ON m.origin_type='income' AND i.id=m.origin_id WHERE m.season_id=? ORDER BY m.date, m.created_at", (o["season_id"],))
    movs = [m for m in movs if (not o["from"] or m["date"] >= o["from"]) and (not o["to"] or m["date"] <= o["to"])]
    b.add("movimentos", _t("Movimentos de caixa"), table={"columns": [_t("Data"), _t("Tipo"), _t("Origem"), _t("Valor"), _t("Método"), _t("Observação"), _t("Lançado por")], "widths": [6, 5, 16, 6, 6, 14, 9],
                                                    "rows": [[fmt_date(m["date"]), _t("Entrada") if m["direction"] == "entrada" else _t("Saída"), m["origin"], from_cents(m["amount_cents"]), m["method"], m["note"], b.user(m["created_by"])] for m in movs],
                                                    "empty": _t("Nenhum pagamento ou recebimento registrado.")})
    b.add("areas", _t("Custos por área"), table={"columns": [_t("Área"), _t("Planejado"), _t("Realizado"), _t("Pago"), _t("Apoio não financeiro (estimado)")],
                                           "rows": [[r["key"], r["planned"], r["realized"], r["paid"], r["in_kind"]] for r in fs["by_area"]]})
    b.add("metodologia", _t("Fórmulas utilizadas"), kv=list(fs["formulas"].items()))


def r_patrocinios(b):
    an = stakeholder_analysis(b.ctx, b.o["season_id"])
    b.add("resumo", _t("Indicadores de captação"), kv=[
        (_t("Stakeholders cadastrados"), an["total"]), (_t("Patrocinadores/apoiadores confirmados"), an["sponsors_confirmed"]), (_t("Propostas enviadas"), an["proposals"]),
        (_t("Taxa de conversão de propostas"), pct(an["conversion_pct"])), (_t("Valor solicitado"), money(an["requested"])), (_t("Valor confirmado"), money(an["confirmed"])),
        (_t("Valor efetivamente recebido"), money(an["received"])), (_t("Apoio não financeiro (valor estimado)"), money(an["in_kind_value"])),
        (_t("Contrapartidas pendentes"), an["counterparts_pending"])],
          note=_t("Valor confirmado é compromisso do patrocinador; só o valor recebido entra no caixa."))
    rows = expand_stakeholders(b.ctx, [ST.ser(r) for r in b.db.q("SELECT * FROM stakeholders WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL ORDER BY name COLLATE NOCASE", (b.o["season_id"],))])
    if b.o["area_id"]:
        rows = [r for r in rows if r["area_id"] == b.o["area_id"]]
    b.add("stakeholders", _t("Stakeholders"), table={
        "columns": [_t("Nome"), _t("Tipo"), _t("Necessidade atendida"), _t("Contribuição"), _t("Nível"), _t("Situação"), _t("Solicitado"), _t("Confirmado"), _t("Recebido"), _t("Apoio não financeiro"), _t("Responsável"), _t("Próxima ação")],
        "widths": [10, 6, 14, 6, 5, 8, 6, 6, 6, 6, 8, 10],
        "rows": [[r["name"].strip(), r["type"], r["need"] or r["purpose"], r["contribution_type"], r["level"], ST_LABEL[r["status"]], r["requested"], r["confirmed"], r["received"],
                  r["in_kind_value"], b.user(r["owner_id"]), r["next_action"]] for r in rows]})
    cps = [[r["name"].strip(), c["kind"], c["description"], fmt_date(c["due_date"]), {"pendente": _t("Pendente"), "cumprida": _t("Cumprida"), "cancelada": _t("Cancelada")}[c["status"]], fmt_date(c["done_date"]), c["record_note"]]
           for r in rows for c in r["counterparts"]]
    b.add("contrapartidas", _t("Contrapartidas"), table={"columns": [_t("Stakeholder"), _t("Tipo"), _t("Contrapartida"), _t("Prazo"), _t("Situação"), _t("Cumprida em"), _t("Registro")], "widths": [9, 8, 14, 6, 6, 6, 14], "rows": cps,
                                                   "empty": _t("Nenhuma contrapartida registrada.")})
    b.add("mensal", _t("Captação por mês"), table={"columns": [_t("Mês"), _t("Valor recebido")], "rows": [[x["key"], x["value"]] for x in an["received_by_month"]], "empty": _t("Nenhum recebimento de patrocínio registrado.")})


def r_riscos(b):
    scale = get_setting(b.db, "risk_scale")
    lo = 0
    bands = []
    for bd in scale["bands"]:
        bands.append([bd["name"], f"{lo + 1} a {bd['max']}", bd.get("hint", "")])
        lo = bd["max"]
    b.add("escala", _t("Escala e legenda"), text=[_t("Pontuação do risco = probabilidade × impacto (escala de 1 a {0}).", scale['size']),
                                             _t("Probabilidade: {0}.", "; ".join(f"{i + 1} = {x}" for i, x in enumerate(scale["probability_labels"]))),
                                             _t("Impacto: {0}.", "; ".join(f"{i + 1} = {x}" for i, x in enumerate(scale["impact_labels"])))],
          table={"columns": [_t("Nível"), _t("Faixa de pontuação"), _t("Significado")], "rows": bands})
    risks = _risks(b)
    b.add("riscos", _t("Riscos identificados"), table={
        "columns": [_t("Código"), _t("Risco"), _t("Causa"), _t("Consequência"), _t("Áreas"), "P", "I", _t("Pontos"), _t("Nível"), _t("Nível na planilha original"), _t("Plano de ação / mitigação"), _t("Responsável"), _t("Situação")],
        "widths": [4, 11, 11, 11, 7, 2, 2, 3, 5, 6, 14, 8, 6],
        "rows": [[r["code"], r["title"], r["cause"], r["consequence"], ", ".join(b.area(a) for a in r["area_ids"]), r["probability"], r["impact"], r["score"], r["level"], r["original_level"],
                  r["mitigation"] or r["preventive_plan"], b.user(r["owner_id"]) if r["owner_id"] else (_t("(área) {0}", r['owner_text']) if r.get("owner_text") else "—"),
                  RISK_STATUS_LABEL[r["status"]]] for r in risks]},
          note=_t("Quando o nível da planilha original difere do calculado, ambos são exibidos — nenhuma classificação foi convertida sem regra explícita."))
    b.add("acoes", _t("Ações de mitigação"), table={"columns": [_t("Risco"), _t("Ação"), _t("Responsável"), _t("Prazo"), _t("Situação")], "widths": [10, 20, 8, 6, 6],
                                              "rows": [[f"{r['code']} — {r['title']}", a["description"], b.user(a["owner_id"]), fmt_date(a["due_date"]), a["status"]] for r in risks for a in r["actions"]],
                                              "empty": _t("Nenhuma ação de mitigação cadastrada.")})
    b.add("lacunas", _t("Riscos sem responsável ou sem plano"), table={"columns": [_t("Risco"), _t("Nível"), _t("Sem responsável (pessoa)"), _t("Sem plano de ação")],
                                                                 "rows": [[f"{r['code']} — {r['title']}", r["level"], r["no_owner"], r["no_plan"]] for r in risks if (r["no_owner"] or r["no_plan"]) and r["status"] != "encerrado"],
                                                                 "empty": _t("Todos os riscos abertos têm responsável e plano.")})
    b.add("residual", _t("Risco inicial × residual"), table={"columns": [_t("Risco"), _t("Pontuação inicial"), _t("Nível inicial"), _t("Pontuação residual"), _t("Nível residual")],
                                                       "rows": [[f"{r['code']} — {r['title']}", r["score"], r["level"], r["residual_score"], r["residual_level"]] for r in risks if r["residual_score"]],
                                                       "empty": _t("Nenhum risco com avaliação residual registrada.")})


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
    b.add("matriz", _t("Matriz RACI"), table={"columns": [_t("Área"), _t("Atividade")] + [u["name"].split()[0] + (_t(" (téc.)") if u["is_teacher"] else "") for u in cols], "widths": [8, 18] + [4] * len(cols),
                                        "rows": [[b.area(a["area_id"]), a["title"]] + [ras[a["id"]].get(u["id"], "") for u in cols] for a in with_raci],
                                        "empty": _t("Nenhuma atividade com atribuições RACI.")},
          note=_t("R = responsável pela execução; A = aprovador final; C = consultado; I = informado. Colunas: {0}. {1} atividade(s) ainda sem atribuições RACI.",
                 "; ".join(f"{u['name'].split()[0]} = {u['name']}" for u in cols), len(acts) - len(with_raci)))
    um = {u["id"]: u for u in users}
    actives = [u for u in users if u["active"]]
    al = [[b.area(a["area_id"]), a["title"], x["message"]] for a in with_raci for x in raci_alerts(db, a, ras[a["id"]], um, actives) if x["level"] in ("erro", "aviso")]
    b.add("alertas", _t("Alertas de governança"), table={"columns": [_t("Área"), _t("Atividade"), _t("Alerta")], "widths": [6, 14, 24], "rows": al, "empty": _t("Nenhum alerta: todas as atividades com RACI têm responsável e aprovador elegível.")},
          note=_t("Regras aplicadas no servidor: o professor técnico não aprova entregas; o criador não aprova a própria entrega; um único aprovador final por atividade."))
    ids = {a["id"]: a for a in acts}
    ap = [x for x in db.q("SELECT * FROM approvals ORDER BY at DESC LIMIT 500") if x["activity_id"] in ids]
    lab = {"submetida": _t("Submetida"), "reenviada": _t("Reenviada"), "aprovada": _t("Aprovada"), "reprovada": _t("Reprovada para ajustes"), "invalidada": _t("Aprovação invalidada"), "tentativa_bloqueada": _t("Tentativa bloqueada")}
    b.add("aprovacoes", _t("Histórico de aprovações"), table={"columns": [_t("Data e hora"), _t("Atividade"), _t("Evento"), _t("Usuário"), _t("Versão"), _t("Comentário / motivo")], "widths": [8, 14, 8, 8, 3, 18],
                                                        "rows": [[fmt_dt(db, x["at"]), ids[x["activity_id"]]["title"], lab[x["action"]], b.user(x["user_id"]), x["content_version"], x["comment"] or x["blocked_reason"]] for x in ap],
                                                        "empty": _t("Nenhum evento de aprovação registrado.")})


def r_metas(b):
    acts = [a for a in _acts(b.ctx, b.o) if a["status"] != "cancelada" and a["planned_end"]]
    due = [a for a in acts if a["planned_end"] <= b.today]

    def stats(xs):
        on = sum(1 for a in xs if a["status"] == "concluida" and not a["completed_late"])
        lt = sum(1 for a in xs if a["completed_late"])
        pend = sum(1 for a in xs if a["status"] != "concluida")
        return [len(xs), on, lt, pend, pct(on * 100 / len(xs)) if xs else "—"]
    s = stats(due)
    b.add("resumo", _t("Cumprimento de prazos"), kv=[(_t("Data de referência"), fmt_date(b.today)), (_t("Entregas com prazo até a data"), s[0]), (_t("Entregues no prazo"), s[1]),
                                                 (_t("Entregues com atraso"), s[2]), (_t("Ainda pendentes (atrasadas)"), s[3]), (_t("Índice de cumprimento de prazos"), s[4]),
                                                 (_t("Entregas com prazo futuro"), len(acts) - len(due))],
          note=_t("Índice de cumprimento = entregas concluídas no prazo ÷ entregas cujo prazo já venceu."))
    b.add("areas", _t("Cumprimento por área"), table={"columns": [_t("Área"), _t("Com prazo vencido"), _t("No prazo"), _t("Com atraso"), _t("Pendentes"), _t("Índice de cumprimento")],
                                                "rows": [[b.area(aid) if aid else _t("(sem área)")] + stats([a for a in due if a["area_id"] == aid]) for aid in list(b.areas) + [None] if any(a["area_id"] == aid for a in due)]})
    ms = [a for a in acts if a["is_milestone"]]
    b.add("marcos", _t("Marcos"), table={"columns": [_t("Marco"), _t("Área"), _t("Prazo"), _t("Conclusão real"), _t("Resultado")],
                                    "rows": [[a["title"], b.area(a["area_id"]), fmt_date(a["planned_end"]), fmt_date(a["actual_end"]),
                                              _t("No prazo") if a["status"] == "concluida" and not a["completed_late"] else _t("Com atraso") if a["completed_late"] else _t("Atrasado") if a["is_late"] else _t("Previsto")] for a in ms],
                                    "empty": _t("Nenhuma atividade marcada como marco.")})


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
    b.add("evolucao", _t("Evolução mensal das entregas"), table={"columns": [_t("Mês"), _t("Entregas planejadas"), _t("Entregas concluídas"), _t("Planejado acumulado"), _t("Concluído acumulado")], "rows": rows})
    ar = []
    for aid in list(b.areas) + [None]:
        xs = [a for a in acts if a["area_id"] == aid]
        if xs:
            d_ = [a for a in xs if a["status"] == "concluida"]
            ar.append([b.area(aid) if aid else _t("(sem área)"), len(xs), len(d_), sum(1 for a in d_ if not a["completed_late"]), sum(1 for a in d_ if a["completed_late"]),
                       sum(1 for a in xs if a["is_late"]), pct(len(d_) * 100 / len(xs)), round(sum(a["progress"] for a in xs) / len(xs))])
    b.add("areas", _t("Desempenho por área"), table={"columns": [_t("Área"), _t("Atividades"), _t("Concluídas"), _t("No prazo"), _t("Com atraso"), _t("Atrasadas em aberto"), "% concluído", _t("Progresso médio (%)")], "rows": ar})
    ind = evidence_indicators(b.ctx, b.o["season_id"])
    b.add("semanal", _t("Desempenho semanal registrado"), table={"columns": [_t("Semana"), _t("Áreas registradas"), _t("Entregas concluídas"), _t("Pontuação (0–100)"), _t("Qualidade (1–5)")],
                                                           "rows": [[w["label"], f"{w['registered']}/{w['areas_total']}", w["deliveries"], w["score"], w["quality"]] for w in ind["weeks"]],
                                                           "empty": _t("Nenhuma semana de evidências registrada.")},
          note=_t("Semanas sem registro ou sem datas não recebem valores."))
    if _fin_allowed(b.ctx, detail=False):
        fs = finance_summary(b.db, b.o["season_id"], b.o["from"], b.o["to"], b.o["area_id"])
        b.add("financeiro", _t("Execução financeira"), kv=_fin_kv(fs))
    risks = _risks(b)
    scale = get_setting(b.db, "risk_scale")
    b.add("riscos", _t("Riscos"), table={"columns": [_t("Nível"), _t("Riscos em aberto")], "rows": [[bd["name"], sum(1 for r in risks if r["level"] == bd["name"] and r["status"] != "encerrado")] for bd in scale["bands"]]})


def r_escopo(b):
    s = b.db.one("SELECT * FROM scopes WHERE season_id=?", (b.o["season_id"],))
    if not s:
        b.add("identificacao", _t("Identificação"), text=_t("O escopo desta temporada ainda não foi elaborado."))
        return
    d_ = json.loads(s["data"])
    L = SECTION_LABELS
    b.add("identificacao", _t("Identificação"), kv=[(L["project_name"], d_.get("project_name")), (L["team_name"], d_.get("team_name") or get_setting(b.db, "team_name")),
                                                 (L["manager_name"], d_.get("manager_name")), (L["teacher_name"], d_.get("teacher_name")), (L["date"], fmt_date(d_.get("date"))),
                                                 (_t("Versão do documento"), s["doc_version"])])
    objs = d_.get("specific_objectives") or []
    b.add("objetivos", _t("Objetivos e justificativa"), kv=[(L["general_objective"], d_.get("general_objective")),
                                                         (L["specific_objectives"], "\n".join(f"• {o['text']}" for o in objs) or None),
                                                         (L["justification"], d_.get("justification")), (L["expected_results"], "\n".join(f"• {x}" for x in d_.get("expected_results") or []) or None)])
    omap = {o["id"]: o["text"] for o in objs}
    dels = d_.get("deliverables") or []
    b.add("entregas", _t("Entregas"), table={"columns": [_t("Área"), _t("Entrega"), _t("Principal"), _t("Objetivo relacionado"), _t("Critério de aceitação")], "widths": [8, 18, 4, 12, 14],
                                       "rows": [[b.area(x["area_id"]) if x.get("area_id") else _t("Geral"), x["text"], x.get("main", False), omap.get(x.get("objective_id")), x.get("acceptance")]
                                                for x in sorted(dels, key=lambda x: b.area(x.get("area_id")))], "empty": _t("Nenhuma entrega cadastrada no escopo.")})
    lst = lambda k: "\n".join(f"• {x}" for x in d_.get(k) or []) or None  # noqa: E731
    b.add("limites", _t("Limites, premissas e restrições"), kv=[(L["limits"], d_.get("limits")), (L["included"], lst("included")), (L["excluded"], lst("excluded")),
                                                             (L["assumptions"], lst("assumptions")), (L["constraints"], lst("constraints"))])
    b.add("criterios", _t("Critérios"), kv=[(L["acceptance_criteria"], lst("acceptance_criteria")), (L["approval_criteria"], d_.get("approval_criteria")), (L["approvals_needed"], lst("approvals_needed"))])
    ref = references(b.db, b.o["season_id"])
    b.add("referencias", _t("Referências de cronograma, orçamento, riscos e stakeholders"), kv=[
        (L["schedule_reference"], d_.get("schedule_reference")),
        (_t("Cronograma cadastrado"), _t("{0} a {1} — {2} atividade(s), {3} marco(s)", fmt_date(ref['schedule']['start']), fmt_date(ref['schedule']['end']), ref['schedule']['activities'], ref['schedule']['milestones'])),
        (L["budget_reference"], d_.get("budget_reference")),
        (_t("Orçamento cadastrado"), _t("Total planejado {0}; itens planejados {1}; meta de captação {2}", money(ref['budget']['planned_total']), money(ref['budget']['planned_items']), money(ref['budget']['goal']))),
        (L["main_risks"], d_.get("main_risks")), (_t("Riscos de maior pontuação cadastrados"), "\n".join(f"• {r['code']} {r['title']} ({r['level']})" for r in ref["top_risks"]) or None),
        (L["main_stakeholders"], d_.get("main_stakeholders")), (_t("Stakeholders confirmados cadastrados"), ", ".join(ref["stakeholders"]) or None)])
    an = analyze(b.db, b.o["season_id"], d_)
    b.add("analise", _t("Análise crítica"), table={"columns": [_t("Gravidade"), _t("Apontamento"), _t("Itens")], "widths": [5, 18, 22],
                                             "rows": [[{"erro": _t("Erro"), "aviso": _t("Atenção"), "info": _t("Informação")}[i["level"]], i["message"], "; ".join(i["items"][:12])] for i in an],
                                             "empty": _t("Nenhum apontamento.")})
    vs = b.db.q("SELECT * FROM scope_versions WHERE scope_id=? ORDER BY doc_version DESC", (s["id"],))
    b.sections.append({"key": "versoes", "heading": _t("Histórico de versões"), "table": {"columns": [_t("Versão"), _t("Data"), _t("Autor"), _t("Resumo")],
                                                                                  "rows": [[v["doc_version"], fmt_dt(b.db, v["created_at"]), b.user(v["created_by"]), v["note"]] for v in vs]}})


def r_eap(b):
    tree = build_tree(b.db, b.o["season_id"])
    if b.o["area_id"]:
        tree = [n for n in tree if n["area_id"] == b.o["area_id"] or n["level"] == 1]
    b.add("estrutura", _t("Estrutura hierárquica"), table={
        "columns": [_t("Código"), _t("Elemento"), _t("Nível"), _t("Área"), _t("Responsável"), _t("Início"), _t("Término"), _t("Duração (dias)"), _t("Custo estimado"), _t("Atividades vinculadas"), _t("Progresso (%)")],
        "widths": [5, 24, 3, 8, 8, 6, 6, 4, 6, 4, 4],
        "rows": [[n["code"], ("   " * (n["level"] - 1)) + n["name"], n["level"], b.area(n["area_id"]), b.user(n["owner_id"]), fmt_date(n["start_date"]), fmt_date(n["end_date"]),
                  n["duration_days"], n["cost_rollup"], n["activity_count"], n["progress"]] for n in tree]},
          note=_t("Progresso: média das atividades vinculadas (ou valor manual) nas folhas; nos níveis superiores, média dos subelementos. Elementos sem dados contam como 0%."))
    packs = [n for n in tree if n["child_count"] and all(c["child_count"] == 0 for c in tree if c["parent_id"] == n["id"])]
    b.add("entregas", _t("Relatório de entregas por pacote"), table={"columns": [_t("Código"), _t("Pacote de trabalho"), _t("Área"), _t("Itens"), _t("Itens com dados"), _t("Progresso (%)"), _t("Critérios de aceitação")],
                                                              "widths": [5, 20, 8, 4, 5, 5, 14],
                                                              "rows": [[n["code"], n["name"], b.area(n["area_id"]), n["leaves"], n["leaves_with_data"], n["progress"], n["acceptance_criteria"]] for n in packs]})


def r_caminho_critico(b):
    from .critical import problem_paths
    d_ = problem_paths(b.ctx, b.o["season_id"])
    off = d_["official"]
    margin = off["deadline_margin"]
    b.add("resumo", _t("Situação oficial"), kv=[
        (_t("Previsão oficial de conclusão"), fmt_date(off["forecast_finish"])),
        (_t("Data-limite (competição)"), fmt_date(off["deadline"]) if off["deadline"] else _t("Não definida")),
        (_t("Margem atual"), _t("{0} dia(s)", margin) if margin is not None else "—"),
        (_t("Etapa seguinte"), _t("Nacional: data a definir. Quando for cadastrada como data-limite, as margens passam a ser calculadas em relação a ela.")),
        (_t("Natureza dos números"), _t("Os dias de atraso por problema são estimativas registradas no vínculo risco × atividade (aba Riscos e soluções). Revise-as com a equipe."))])
    rows = []
    for x in d_["items"]:
        aff = "; ".join(f"{a['title']} ({b.area(a['area_id'])}): +{a['days'] or 0}" for a in x["affected"]) or _t("Nenhuma atividade vinculada")
        ok = x["meets_deadline"]
        rows.append([x["code"], x["title"], x["level"], aff, x["extra_days_total"], fmt_date(x["forecast_finish"]), x["delta_days"],
                     x["deadline_margin"] if x["deadline_margin"] is not None else "—",
                     _t("Sim") if ok else (_t("Não") if ok is False else "—")])
    b.add("problemas", _t("Problemas e impacto no prazo"), table={
        "columns": [_t("Código"), _t("Problema"), _t("Nível"), _t("Atividades afetadas (dias)"), _t("Dias somados"), _t("Nova previsão"), _t("Atraso no fim (dias)"), _t("Margem até a data-limite"), _t("Chega a tempo?")],
        "widths": [4, 12, 5, 22, 4, 6, 4, 5, 4], "rows": rows, "empty": _t("Nenhum risco cadastrado.")})
    prow = []
    for x in d_["items"]:
        steps = " → ".join(f"{p['title']} ({b.area(p['area_id'])}, {fmt_date(p['start'])}–{fmt_date(p['finish'])}{', +' + str(p['extra_days']) if p['extra_days'] else ''})" for p in x["path"])
        end = _t("Competição em {0}", fmt_date(x["deadline"])) if x["deadline"] else _t("Competição (data a definir)")
        prow.append([x["code"], x["title"], (steps + " → " if steps else "") + end + " → " + _t("Nacional (a definir)")])
    b.add("caminhos", _t("Caminho de cada problema até a competição"), table={"columns": [_t("Código"), _t("Problema"), _t("Caminho crítico previsto")], "widths": [4, 10, 34], "rows": prow})
    b.add("respostas", _t("Respostas previstas"), table={"columns": [_t("Código"), _t("Problema"), _t("Mitigação"), _t("Plano de contingência")], "widths": [4, 10, 17, 17],
                                                      "rows": [[x["code"], x["title"], x["mitigation"] or "—", x["contingency_plan"] or "—"] for x in d_["items"]]})


BUILDERS = {"executivo": r_executivo, "cronograma": r_cronograma, "entregas_area": r_entregas_area, "atrasos": r_atrasos, "monitoramento": r_monitoramento,
            "financeiro": r_financeiro, "patrocinios": r_patrocinios, "riscos": r_riscos, "responsabilidades": r_responsabilidades, "metas": r_metas,
            "desempenho": r_desempenho, "escopo": r_escopo, "eap": r_eap, "caminho_critico": r_caminho_critico}
WIDE = {"caminho_critico", "cronograma", "atrasos", "financeiro", "patrocinios", "riscos", "responsabilidades", "eap"}


def build_report(ctx, rid, o):
    spec = BY_ID.get(rid)
    if not spec:
        raise bad(_t("Relatório desconhecido."))
    if ctx.role == "juiz":
        jv = judge_view(ctx)
        if not jv["modules"].get("relatorios") or not jv["modules"].get(spec["module"]):
            raise forbidden(_t("Este relatório não está disponível para o perfil de juiz."))
    db = ctx.db
    season = db.one("SELECT * FROM seasons WHERE id=? AND deleted_at IS NULL", (o["season_id"],))
    if not season:
        raise bad(_t("Temporada inválida."))
    b = B(ctx, o)
    BUILDERS[rid](b)
    now = datetime.now(timezone.utc).astimezone(team_tz(db))
    if o["month"] and rid == "monitoramento":
        period = month_label(o["month"])
    elif o["from"] or o["to"]:
        period = f"{fmt_date(o['from']) if o['from'] else 'início da temporada'} a {fmt_date(o['to']) if o['to'] else 'data de geração'}"
    else:
        period = _t("Temporada completa ({0} a {1})", fmt_date(season['start_date']), fmt_date(season['end_date'])) if season["start_date"] else _t("Temporada completa")
    filters = []
    if o["area_id"]:
        filters.append((_t("Área"), b.area(o["area_id"])))
    if o["members"]:
        filters.append((_t("Integrantes"), ", ".join(b.user(m) for m in o["members"])))
    theme = get_setting(db, "theme")
    return {"id": rid, "title": spec["name"], "team": get_setting(db, "team_name"), "subtitle": get_setting(db, "team_subtitle"), "season": season["name"],
            "period_label": period, "generated_at": fmt_dt_l(now) + f" ({get_setting(db, 'timezone')})", "generated_by": ctx.user["name"],
            "source": _t("banco de dados da plataforma de gestão (registros vigentes na data de geração)"), "filters": filters,
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
    doc = localize(build_report(ctx, ctx.params["id"], o))   # textos do sistema no idioma da interface; dados como gravados
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
    raise bad(_t("Formato inválido (use pdf, xlsx ou docx)."))


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
