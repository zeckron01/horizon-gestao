"""Dashboard estratégico: todos os números são calculados dos registros do banco, respeitando os filtros."""
from ..core import INTERNAL, add_days, bad, get_setting, judge_module_guard, judge_view, route, today_local, user_names
from .activities import ACT, STATUS_LABEL, critical_path, expand_activities, load_graph
from .evidences import evidence_indicators
from .finance import finance_summary, month_range
from .risks import RISK, expand_risks


def filtered_activities(db, q):
    sid = q.get("season_id")
    rows = db.q("SELECT * FROM activities WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (sid,))
    f, t = q.get("from"), q.get("to")
    if q.get("month"):
        f, t = q["month"] + "-01", q["month"] + "-31"
    out = []
    parts = {}
    if q.get("owner_id"):
        for p in db.q("SELECT activity_id FROM activity_participants WHERE user_id=?", (q["owner_id"],)):
            parts[p["activity_id"]] = True
    for a in rows:
        if q.get("area_id") and a["area_id"] != q["area_id"]:
            continue
        if q.get("owner_id") and a["owner_id"] != q["owner_id"] and a["id"] not in parts:
            continue
        if q.get("category") and a["category"] != q["category"]:
            continue
        ref_s, ref_e = a["planned_start"] or a["planned_end"], a["planned_end"] or a["planned_start"]
        if (f or t) and (not ref_e or (f and ref_e < f) or (t and ref_s > t)):
            continue
        out.append(a)
    out = expand_activities(db, [ACT.ser(a) for a in out])
    st = q.get("status")
    if st == "atrasada":
        out = [a for a in out if a["is_late"]]
    elif st:
        out = [a for a in out if a["status"] == st]
    return out, f, t


@route("GET", "/api/dashboard")
def dashboard(ctx):
    judge_module_guard(ctx, "dashboard")
    db, q = ctx.db, ctx.query
    sid = q.get("season_id")
    if not sid:
        raise bad("Informe a temporada.")
    today = today_local(db)
    acts, f, t = filtered_activities(db, q)
    live = [a for a in acts if a["status"] != "cancelada"]
    names = user_names(db)
    areas = db.q("SELECT id, name, color FROM areas WHERE deleted_at IS NULL ORDER BY sort, name")

    def brief(a):
        return {"id": a["id"], "title": a["title"], "planned_end": a["planned_end"], "area_id": a["area_id"], "owner_name": names.get(a["owner_id"]),
                "status": a["status"], "days_late": a["days_late"], "progress": a["progress"], "is_milestone": a["is_milestone"]}
    done = [a for a in live if a["status"] == "concluida"]
    late = [a for a in live if a["is_late"]]
    open_ = [a for a in live if a["status"] != "concluida"]
    due7 = [a for a in open_ if a["planned_end"] and today <= a["planned_end"] <= add_days(today, 7)]
    due30 = [a for a in open_ if a["planned_end"] and today <= a["planned_end"] <= add_days(today, 30)]
    miles = sorted([a for a in open_ if a["is_milestone"] and a["planned_end"] and a["planned_end"] >= today], key=lambda a: a["planned_end"])
    kpis = {
        "total": len(live), "done": len(done), "in_progress": sum(1 for a in live if a["status"] in ("em_andamento", "em_revisao")),
        "not_started": sum(1 for a in live if a["status"] == "nao_iniciada"), "late": len(late),
        "blocked": sum(1 for a in live if a["status"] == "bloqueada"), "waiting_approval": sum(1 for a in live if a["status"] == "aguardando_aprovacao"),
        "cancelled": len(acts) - len(live),
        "completion_pct": round(len(done) * 100 / len(live), 1) if live else None,
        "progress_avg": round(sum(a["progress"] for a in live) / len(live), 1) if live else None,
        "due_7": len(due7), "due_30": len(due30), "milestones_upcoming": len(miles),
        "done_on_time": sum(1 for a in done if not a["completed_late"]), "done_late": sum(1 for a in done if a["completed_late"]),
        "no_owner": sum(1 for a in open_ if not a["owner_id"]),
    }
    status_chart = [{"key": k, "label": STATUS_LABEL[k], "value": sum(1 for a in live if a["status"] == k and not a["is_late"])} for k in STATUS_LABEL if k != "cancelada"]
    status_chart.append({"key": "atrasada", "label": "Atrasadas (não concluídas)", "value": len(late)})
    by_area = []
    for ar in areas + [{"id": None, "name": "(sem área)", "color": "#94a3b8"}]:
        xs = [a for a in live if a["area_id"] == ar["id"]]
        if not xs:
            continue
        d_ = [a for a in xs if a["status"] == "concluida"]
        by_area.append({"area_id": ar["id"], "name": ar["name"], "color": ar["color"], "total": len(xs), "done": len(d_),
                        "late": sum(1 for a in xs if a["is_late"]), "in_progress": sum(1 for a in xs if a["status"] in ("em_andamento", "em_revisao", "aguardando_aprovacao")),
                        "done_late": sum(1 for a in d_ if a["completed_late"]),
                        "completion_pct": round(len(d_) * 100 / len(xs), 1), "progress_avg": round(sum(a["progress"] for a in xs) / len(xs), 1)})
    # evolução mensal: prazos planejados × conclusões reais (e acumulados)
    months_src = [a["planned_end"][:7] for a in live if a["planned_end"]] + [a["actual_end"][:7] for a in done if a["actual_end"]]
    timeline = []
    if months_src:
        cp = cd = 0
        for mo in month_range(min(months_src), max(months_src)):
            p = sum(1 for a in live if a["planned_end"] and a["planned_end"][:7] == mo)
            d_ = sum(1 for a in done if a["actual_end"] and a["actual_end"][:7] == mo)
            cp, cd = cp + p, cd + d_
            timeline.append({"month": mo, "planned": p, "done": d_, "planned_cum": cp, "done_cum": cd if mo <= today[:7] else None, "is_future": mo > today[:7]})
    # riscos
    risks = expand_risks(db, [RISK.ser(r) for r in db.q("SELECT * FROM risks WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (sid,))])
    if q.get("area_id"):
        risks = [r for r in risks if q["area_id"] in r["area_ids"]]
    if q.get("owner_id"):
        risks = [r for r in risks if r["owner_id"] == q["owner_id"]]
    open_r = [r for r in risks if r["status"] != "encerrado"]
    scale = get_setting(db, "risk_scale")
    risk_levels = [{"key": b["name"], "label": b["name"], "color": b["color"], "value": sum(1 for r in open_r if r["level"] == b["name"])} for b in scale["bands"]]
    unrated = sum(1 for r in open_r if not r["level"])
    top_names = [b["name"] for b in scale["bands"][-2:]]
    kpis["risks_open"] = len(open_r)
    kpis["risks_critical_high"] = sum(1 for r in open_r if r["level"] in top_names)
    kpis["risks_critical"] = sum(1 for r in open_r if r["level"] == scale["bands"][-1]["name"])
    # financeiro (somente se o perfil puder ver)
    fin = None
    jv = judge_view(ctx)
    if ctx.role != "juiz" or (jv["modules"].get("financeiro") and (jv["finance_summary"] or jv["finance_detail"])):
        fs = finance_summary(db, sid, f, t, q.get("area_id") or None)
        fin = {"budget": fs["budget"], "funding": fs["funding"], "cash": fs["cash"], "cashflow": fs["cashflow"], "received_by_sponsor": fs["received_by_sponsor"]}
        st = db.one("SELECT COUNT(*) n FROM stakeholders WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status IN ('confirmado','parcialmente_confirmado','concluido')", (sid,))
        fin["sponsors_confirmed"] = st["n"]
    # evidências
    ev = evidence_indicators(ctx, sid)
    kpis["evidence_entries"] = sum(w["registered"] for w in ev["weeks"])
    kpis["evidence_weeks"] = len(ev["weeks"])
    kpis["evidence_weeks_complete"] = sum(1 for w in ev["weeks"] if w["state"] == "completa")
    # aprovações pendentes
    pend = [a for a in live if a["approval_status"] in ("aguardando_aprovacao", "reenviada")]
    kpis["pending_approvals"] = len(pend)
    # participação: quem atualizou informações nos últimos 14 dias (registros de auditoria)
    participation = None
    if ctx.role in INTERNAL:
        since = add_days(today, -14)
        rows = db.q("SELECT user_id, COUNT(*) n, MAX(at) last FROM audit_log WHERE user_id IS NOT NULL AND at >= ? AND action NOT IN ('login','logout','login_falhou') GROUP BY user_id", (since,))
        last_all = {r["user_id"]: r["last"] for r in db.q("SELECT user_id, MAX(at) last FROM audit_log WHERE user_id IS NOT NULL AND action NOT IN ('login','logout','login_falhou') GROUP BY user_id")}
        by_user = {r["user_id"]: r for r in rows}
        participation = [{"user_id": u["id"], "name": u["name"], "has_login": bool(u["password_hash"]), "updates_14d": by_user.get(u["id"], {}).get("n", 0),
                          "last_update": last_all.get(u["id"]), "open_activities": sum(1 for a in open_ if a["owner_id"] == u["id"]),
                          "late_activities": sum(1 for a in late if a["owner_id"] == u["id"])}
                         for u in db.q("SELECT * FROM users WHERE deleted_at IS NULL AND active=1 AND role<>'juiz' ORDER BY name")]
    graph_acts, deps = load_graph(db, sid)
    _, critical = critical_path(graph_acts, deps)
    return {
        "today": today, "filters": {"from": f, "to": t}, "kpis": kpis,
        "status_chart": status_chart, "by_area": by_area, "timeline": timeline,
        "late": [brief(a) for a in sorted(late, key=lambda a: -a["days_late"])[:12]],
        "due_soon": [brief(a) for a in sorted(due30, key=lambda a: a["planned_end"])[:12]],
        "milestones": [brief(a) for a in miles[:8]],
        "critical_open": [brief(a) for a in live if a["id"] in critical][:12],
        "pending_approvals": [brief(a) for a in pend[:10]],
        "risk_levels": risk_levels, "risks_unrated": unrated,
        "top_risks": [{"id": r["id"], "code": r["code"], "title": r["title"], "level": r["level"], "score": r["score"], "no_plan": r["no_plan"]}
                      for r in sorted(open_r, key=lambda r: -(r["score"] or 0))[:6]],
        "finance": fin, "evidence": {"weeks": ev["weeks"]}, "participation": participation,
        "events": db.q("SELECT id, name, start_date, end_date, location, status FROM events WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status='ativo' AND COALESCE(end_date,start_date)>=? ORDER BY start_date LIMIT 5", (sid, today)),
    }
