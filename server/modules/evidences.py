"""Evidências semanais de monitoramento (diário de bordo gerencial) e indicadores de desempenho.

Metodologia da pontuação composta (exibida na interface):
cada critério vale de 0 a 100 e é ponderado pelos pesos configuráveis; critérios sem dados na semana são
desconsiderados (os pesos restantes são renormalizados). Semanas sem registro não recebem pontuação.
A quantidade de texto nunca entra no cálculo."""
from ..core import (F, INTERNAL, STAFF, audit, bad, check_order, conflict, forbidden, get_setting, judge_module_guard,
                    not_found, route, user_names)
from ..db import new_id, now_iso

MONTHS = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]
TEXT_FIELDS = {
    "activities_done": "Atividades realizadas", "deliverables": "Entregas produzidas", "results": "Resultados alcançados",
    "difficulties": "Dificuldades encontradas", "decisions": "Decisões tomadas", "next_actions": "Próximas ações",
    "pending": "Pendências", "extras": "Evidências complementares", "manager_comment": "Comentários do gestor",
}
CRITERIA = {
    "entregas": "Entregas concluídas na semana ÷ entregas com prazo na semana (limitado a 100%)",
    "marcos": "Marcos alcançados na semana ÷ marcos com prazo na semana (limitado a 100%)",
    "prazos": "Entregas concluídas dentro do prazo ÷ entregas concluídas na semana",
    "metas": "Metas atingidas ÷ metas definidas para a semana (informadas no registro)",
    "evidencias": "100 quando há ao menos um anexo válido no registro da área; 0 caso contrário",
    "progresso": "Progresso médio atual das atividades da área planejadas para o período da semana",
    "qualidade": "Avaliação qualitativa do gestor (1 a 5) convertida para 0–100",
}
WEEK_FIELDS = {
    "season_id": F("ref", "Temporada", required=True, ref="seasons"),
    "month": F("text", "Mês de referência", required=True, max=7),
    "week_number": F("int", "Número da semana", required=True, min=1, max=6),
    "start_date": F("date", "Data de início"),
    "end_date": F("date", "Data de término"),
    "supervisor_id": F("ref", "Gestor/professor responsável pelo acompanhamento", ref="users"),
}


def month_label(m):
    return f"{MONTHS[int(m[5:7]) - 1]} de {m[:4]}"


def _area_metrics(db, week, area_id):
    """Indicadores automáticos da área na semana, calculados dos registros reais de atividades."""
    s, e = week["start_date"], week["end_date"]
    if not s or not e:
        return {"has_dates": False}
    cache = getattr(db, "scache", None)
    ck = ("acts", week["season_id"])
    if cache is not None and ck in cache:
        by_area = cache[ck]
    else:   # uma consulta por temporada (e não por semana × área)
        by_area = {}
        for a in db.q("SELECT * FROM activities WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status<>'cancelada'", (week["season_id"],)):
            by_area.setdefault(a["area_id"], []).append(a)
        if cache is not None:
            cache[ck] = by_area
    acts = by_area.get(area_id, [])
    done = [a for a in acts if a["status"] == "concluida" and a["actual_end"] and s <= a["actual_end"] <= e]
    due = [a for a in acts if a["planned_end"] and s <= a["planned_end"] <= e]
    active = [a for a in acts if a["planned_end"] and (a["planned_start"] or a["planned_end"]) <= e and a["planned_end"] >= s]
    return {
        "has_dates": True, "deliveries_done": len(done), "deliveries_due": len(due),
        "milestones_done": sum(1 for a in done if a["is_milestone"]), "milestones_due": sum(1 for a in due if a["is_milestone"]),
        "on_time": sum(1 for a in done if not a["planned_end"] or a["actual_end"] <= a["planned_end"]),
        "late": sum(1 for a in done if a["planned_end"] and a["actual_end"] > a["planned_end"]),
        "active_count": len(active), "avg_progress": round(sum(a["progress"] for a in active) / len(active)) if active else None,
    }


def score_entry(weights, m, entry, n_files):
    """Pontuação composta (0–100) e detalhamento por critério; None quando não há nenhum critério com dados."""
    comp = {}
    if m.get("has_dates"):
        if m["deliveries_due"] or m["deliveries_done"]:
            comp["entregas"] = min(100, round(m["deliveries_done"] * 100 / max(1, m["deliveries_due"])))
        if m["milestones_due"] or m["milestones_done"]:
            comp["marcos"] = min(100, round(m["milestones_done"] * 100 / max(1, m["milestones_due"])))
        if m["deliveries_done"]:
            comp["prazos"] = round(m["on_time"] * 100 / m["deliveries_done"])
        if m["avg_progress"] is not None:
            comp["progresso"] = m["avg_progress"]
    if entry.get("m_goals_total"):
        comp["metas"] = min(100, round((entry.get("m_goals_met") or 0) * 100 / entry["m_goals_total"]))
    comp["evidencias"] = 100 if n_files else 0
    if entry.get("m_quality"):
        comp["qualidade"] = round((entry["m_quality"] - 1) * 25)
    if list(comp) == ["evidencias"] and not n_files:
        return None, comp  # sem nenhum dado mensurável (só texto): não pontua, em vez de receber zero
    wsum = sum(weights.get(k, 0) for k in comp)
    score = round(sum(weights.get(k, 0) * v for k, v in comp.items()) / wsum, 1) if wsum else None
    return score, comp


def week_bundle(db, weeks):
    """Carrega de uma vez (poucas consultas) tudo o que load_week precisa para várias semanas."""
    ids = [w["id"] for w in weeks]
    b = {"areas": db.q("SELECT id, name, color, deleted_at, archived_at FROM areas ORDER BY sort, name"), "names": user_names(db),
         "entries": {}, "files": {}, "members": {}, "acts": {}}
    if not ids:
        return b
    marks = ",".join("?" for _ in ids)
    sub = f"(SELECT id FROM evidence_entries WHERE week_id IN ({marks}))"
    for e in db.q(f"SELECT * FROM evidence_entries WHERE week_id IN ({marks})", ids):
        b["entries"].setdefault(e["week_id"], {})[e["area_id"]] = e
    for r in db.q(f"SELECT entity_id, COUNT(*) AS n FROM files WHERE entity_type='evidence_entries' AND deleted_at IS NULL AND entity_id IN {sub} GROUP BY entity_id", ids):
        b["files"][r["entity_id"]] = r["n"]
    for r in db.q(f"SELECT entry_id, user_id FROM evidence_members WHERE entry_id IN {sub}", ids):
        b["members"].setdefault(r["entry_id"], []).append(r["user_id"])
    for r in db.q(f"SELECT entry_id, activity_id FROM evidence_activities WHERE entry_id IN {sub}", ids):
        b["acts"].setdefault(r["entry_id"], []).append(r["activity_id"])
    return b


def load_week(ctx, week, full=True, bundle=None):
    db = ctx.db
    b = bundle or week_bundle(db, [week])
    entries = b["entries"].get(week["id"], {})
    areas = [{"id": a["id"], "name": a["name"], "color": a["color"]} for a in b["areas"] if not a["deleted_at"] and not a["archived_at"]]
    weights = get_setting(db, "evidence_weights")
    names = b["names"]
    out = dict(week)
    out["needs_review"] = bool(week["needs_review"])
    out["month_label"] = month_label(week["month"])
    out["supervisor_name"] = names.get(week["supervisor_id"])
    out["entries"] = []
    reg = 0
    # inclui áreas arquivadas que possuam registro histórico
    active_ids = {x["id"] for x in areas}
    extra = [{"id": a["id"], "name": a["name"], "color": a["color"]} for a in b["areas"] if a["id"] in entries and a["id"] not in active_ids]
    for a in areas + extra:
        e = entries.get(a["id"])
        m = _area_metrics(db, week, a["id"])
        item = {"area_id": a["id"], "area_name": a["name"], "area_color": a["color"], "metrics": m, "exists": bool(e)}
        if e:
            n_files = b["files"].get(e["id"], 0)
            item.update({k: e[k] for k in ("id", "record_date", "status", "m_goals_met", "m_goals_total", "m_quality", "version", "updated_at", *TEXT_FIELDS)})
            item["file_count"] = n_files
            item["member_ids"] = b["members"].get(e["id"], [])
            item["activity_ids"] = b["acts"].get(e["id"], [])
            if e["status"] == "registrado":
                reg += 1
                item["score"], item["score_components"] = score_entry(weights, m, e, n_files)
            else:
                item["score"], item["score_components"] = None, {}
        out["entries"].append(item)
    out["areas_total"], out["areas_registered"] = len(areas), reg
    out["drafts"] = sum(1 for e in entries.values() if e["status"] == "rascunho")
    out["state"] = "completa" if reg >= len(areas) and areas else "parcial" if entries else "pendente"
    return out


@route("GET", "/api/evidence-weeks")
def list_weeks(ctx):
    judge_module_guard(ctx, "evidencias")
    sid = ctx.query.get("season_id")
    weeks = ctx.db.q("SELECT * FROM evidence_weeks WHERE season_id=? AND deleted_at IS NULL ORDER BY month DESC, week_number DESC", (sid,))
    bundle = week_bundle(ctx.db, weeks)
    return {"items": [load_week(ctx, w, bundle=bundle) for w in weeks], "criteria": CRITERIA, "weights": get_setting(ctx.db, "evidence_weights"), "fields": TEXT_FIELDS}


@route("GET", "/api/evidence-weeks/{id}")
def get_week(ctx):
    judge_module_guard(ctx, "evidencias")
    w = ctx.db.one("SELECT * FROM evidence_weeks WHERE id=? AND deleted_at IS NULL", (ctx.params["id"],))
    if not w:
        raise not_found("Semana")
    return load_week(ctx, w)


def _parse_week(ctx, existing=None):
    data = {}
    for k, f in WEEK_FIELDS.items():
        if k in ctx.body:
            data[k] = f.parse(ctx.db, k, ctx.body[k])
        elif existing is None and f.required:
            raise bad(f"{f.label}: campo obrigatório.", {k: "Campo obrigatório."})
    merged = {**(existing or {}), **data}
    m = merged.get("month") or ""
    if len(m) != 7 or m[4] != "-" or not m[:4].isdigit() or not m[5:].isdigit() or not 1 <= int(m[5:]) <= 12:
        raise bad("Mês de referência inválido (use AAAA-MM).", {"month": "Formato inválido."})
    check_order(merged, "start_date", "end_date", "Data de início", "Data de término")
    dup = ctx.db.one("SELECT id FROM evidence_weeks WHERE season_id=? AND month=? AND week_number=? AND deleted_at IS NULL AND id<>?",
                     (merged["season_id"], merged["month"], merged["week_number"], (existing or {}).get("id", "")))
    if dup:
        raise conflict("Já existe um registro para esta semana neste mês.", code="semana_duplicada", existing_id=dup["id"])
    return data


@route("POST", "/api/evidence-weeks")
def create_week(ctx):
    data = _parse_week(ctx)
    ts = now_iso()
    # reaproveita o identificador de uma semana excluída logicamente para respeitar a unicidade
    ctx.db.ex("DELETE FROM evidence_weeks WHERE season_id=? AND month=? AND week_number=? AND deleted_at IS NOT NULL", (data["season_id"], data["month"], data["week_number"]))
    row = {"id": new_id(), **data, "created_at": ts, "updated_at": ts, "created_by": ctx.uid}
    if "supervisor_id" not in data or not data["supervisor_id"]:
        row["supervisor_id"] = ctx.uid
    ctx.db.insert("evidence_weeks", row)
    w = ctx.db.one("SELECT * FROM evidence_weeks WHERE id=?", (row["id"],))
    audit(ctx, "criar", "evidence_weeks", w["id"], f"{month_label(w['month'])} — semana {w['week_number']}", after=w)
    return load_week(ctx, w)


@route("PUT", "/api/evidence-weeks/{id}")
def update_week(ctx):
    w = ctx.db.one("SELECT * FROM evidence_weeks WHERE id=? AND deleted_at IS NULL", (ctx.params["id"],))
    if not w:
        raise not_found("Semana")
    data = _parse_week(ctx, w)
    data.pop("season_id", None)
    if not isinstance(ctx.body.get("version"), int):
        raise bad("Informe a versão do registro (version).", code="versao_obrigatoria")
    if ctx.body.get("resolve_review"):
        data["needs_review"], data["review_note"] = 0, None
    sets = ", ".join(f"{k}=?" for k in data) + (", " if data else "")
    cur = ctx.db.ex(f"UPDATE evidence_weeks SET {sets}updated_at=?, version=version+1 WHERE id=? AND version=?", list(data.values()) + [now_iso(), w["id"], ctx.body["version"]])
    if cur.rowcount == 0:
        raise conflict("Esta semana foi alterada por outra pessoa. Recarregue antes de salvar.", code="conflito_versao")
    new = ctx.db.one("SELECT * FROM evidence_weeks WHERE id=?", (w["id"],))
    audit(ctx, "alterar", "evidence_weeks", w["id"], f"{month_label(w['month'])} — semana {w['week_number']}", before=w, after=new)
    return load_week(ctx, new)


@route("DELETE", "/api/evidence-weeks/{id}")
def delete_week(ctx):
    w = ctx.db.one("SELECT * FROM evidence_weeks WHERE id=? AND deleted_at IS NULL", (ctx.params["id"],))
    if not w:
        raise not_found("Semana")
    n = ctx.db.val("SELECT COUNT(*) FROM evidence_entries WHERE week_id=?", (w["id"],))
    if n and ctx.query.get("cascade") != "1":
        raise conflict(f"Esta semana possui {n} registro(s) de área. Confirme para excluir a semana com todos os registros.",
                       code="vinculos_existentes", references=[{"label": "registro(s) de área", "count": n}], can_cascade=True, can_archive=False)
    ctx.db.ex("UPDATE evidence_weeks SET deleted_at=?, version=version+1 WHERE id=?", (now_iso(), w["id"]))
    audit(ctx, "excluir", "evidence_weeks", w["id"], f"{month_label(w['month'])} — semana {w['week_number']} ({n} registro(s))", before=w)
    return {"ok": True}


@route("PUT", "/api/evidence-weeks/{id}/areas/{area_id}", allow=INTERNAL)
def save_entry(ctx):
    """Cria ou atualiza o registro de uma área na semana (rascunho ou registrado)."""
    db = ctx.db
    w = db.one("SELECT * FROM evidence_weeks WHERE id=? AND deleted_at IS NULL", (ctx.params["id"],))
    area = db.one("SELECT * FROM areas WHERE id=? AND deleted_at IS NULL", (ctx.params["area_id"],))
    if not w or not area:
        raise not_found("Semana ou área")
    e = db.one("SELECT * FROM evidence_entries WHERE week_id=? AND area_id=?", (w["id"], area["id"]))
    b = ctx.body
    if ctx.role not in STAFF:
        member_of = ctx.user["area_id"] == area["id"] or (e and db.one("SELECT 1 FROM evidence_members WHERE entry_id=? AND user_id=?", (e["id"], ctx.uid)))
        if not member_of:
            raise forbidden("Integrantes só registram evidências da própria área.")
        if any(k in b and b[k] != (e or {}).get(k) for k in ("manager_comment", "m_quality")):
            raise forbidden("Comentários e avaliação do gestor são exclusivos da gestão.")
    data = {}
    for k, label in TEXT_FIELDS.items():
        if k in b:
            data[k] = F("long", label, max=20000).parse(db, k, b[k])
    if "record_date" in b:
        data["record_date"] = F("date", "Data do registro").parse(db, "record_date", b["record_date"])
    for k, lab, lo, hi in (("m_goals_met", "Metas atingidas", 0, 999), ("m_goals_total", "Metas definidas", 0, 999), ("m_quality", "Avaliação qualitativa", 1, 5)):
        if k in b:
            data[k] = F("int", lab, min=lo, max=hi).parse(db, k, b[k])
    if "status" in b:
        data["status"] = F("enum", "Situação", options=("rascunho", "registrado")).parse(db, "status", b["status"])
    merged = {**(e or {}), **data}
    if merged.get("m_goals_total") is not None and (merged.get("m_goals_met") or 0) > merged["m_goals_total"]:
        raise bad("Metas atingidas não pode ser maior que metas definidas.", {"m_goals_met": "Maior que o total."})
    if merged.get("status") == "registrado" and not any((merged.get(k) or "").strip() for k in ("activities_done", "deliverables", "results")):
        raise bad("Para registrar a evidência, descreva ao menos as atividades realizadas, as entregas ou os resultados. Você pode salvar como rascunho.",
                  {"activities_done": "Descreva as atividades realizadas."})
    ts = now_iso()
    if e is None:
        from ..core import today_local
        row = {"id": new_id(), "week_id": w["id"], "area_id": area["id"], "status": "rascunho", **data, "created_at": ts, "updated_at": ts, "created_by": ctx.uid}
        row.setdefault("record_date", today_local(db))
        db.insert("evidence_entries", row)
        eid = row["id"]
        before = None
    else:
        if not isinstance(b.get("version"), int):
            raise bad("Informe a versão do registro (version).", code="versao_obrigatoria")
        sets = ", ".join(f"{k}=?" for k in data) + (", " if data else "")
        cur = db.ex(f"UPDATE evidence_entries SET {sets}updated_at=?, version=version+1 WHERE id=? AND version=?", list(data.values()) + [ts, e["id"], b["version"]])
        if cur.rowcount == 0:
            raise conflict("Este registro foi alterado por outra pessoa enquanto você editava. Recarregue para ver a versão atual; seu texto não foi salvo.",
                           code="conflito_versao", current=e)
        eid, before = e["id"], e
    for key, table, col, ref in (("member_ids", "evidence_members", "user_id", "users"), ("activity_ids", "evidence_activities", "activity_id", "activities")):
        if isinstance(b.get(key), list):
            db.ex(f"DELETE FROM {table} WHERE entry_id=?", (eid,))
            for i in dict.fromkeys(b[key]):
                if db.one(f"SELECT 1 FROM {ref} WHERE id=? AND deleted_at IS NULL", (i,)):
                    db.ex(f"INSERT INTO {table} (entry_id, {col}) VALUES (?,?)", (eid, i))
    new = db.one("SELECT * FROM evidence_entries WHERE id=?", (eid,))
    audit(ctx, "criar" if before is None else "alterar", "evidence_entries", eid,
          f"{month_label(w['month'])} — semana {w['week_number']} — {area['name']}", before=before, after=new)
    return load_week(ctx, w)


@route("DELETE", "/api/evidence-entries/{id}")
def delete_entry(ctx):
    e = ctx.db.one("SELECT * FROM evidence_entries WHERE id=?", (ctx.params["id"],))
    if not e:
        raise not_found("Registro")
    ctx.db.ex("DELETE FROM evidence_entries WHERE id=?", (e["id"],))
    ctx.db.ex("UPDATE files SET deleted_at=? WHERE entity_type='evidence_entries' AND entity_id=?", (now_iso(), e["id"]))
    audit(ctx, "excluir", "evidence_entries", e["id"], "Registro de evidência removido", before=e)
    return {"ok": True}


def evidence_indicators(ctx, season_id):
    """Séries por semana: volume (entregas e registros), desempenho (pontuação) e qualidade — sempre separados."""
    weeks = ctx.db.q("SELECT * FROM evidence_weeks WHERE season_id=? AND deleted_at IS NULL ORDER BY month, week_number", (season_id,))
    series, by_area = [], {}
    bundle = week_bundle(ctx.db, weeks)
    for w in weeks:
        lw = load_week(ctx, w, bundle=bundle)
        scored = [e for e in lw["entries"] if e.get("score") is not None]
        quals = [e["m_quality"] for e in lw["entries"] if e.get("exists") and e.get("status") == "registrado" and e.get("m_quality")]
        has_dates = bool(w["start_date"] and w["end_date"])
        series.append({
            "week_id": w["id"], "label": f"{MONTHS[int(w['month'][5:7]) - 1][:3]}/S{w['week_number']}", "month": w["month"], "week_number": w["week_number"],
            "registered": lw["areas_registered"], "areas_total": lw["areas_total"], "state": lw["state"],
            "deliveries": sum(e["metrics"].get("deliveries_done", 0) for e in lw["entries"]) if has_dates else None,
            "on_time": sum(e["metrics"].get("on_time", 0) for e in lw["entries"]) if has_dates else None,
            "late": sum(e["metrics"].get("late", 0) for e in lw["entries"]) if has_dates else None,
            "score": round(sum(e["score"] for e in scored) / len(scored), 1) if scored else None,
            "quality": round(sum(quals) / len(quals), 1) if quals else None,
        })
        for e in lw["entries"]:
            if e.get("exists"):
                by_area.setdefault(e["area_name"], []).append({"label": series[-1]["label"], "score": e.get("score"), "status": e["status"],
                                                               "deliveries": e["metrics"].get("deliveries_done")})
    return {"weeks": series, "by_area": by_area, "criteria": CRITERIA, "weights": get_setting(ctx.db, "evidence_weights")}


@route("GET", "/api/evidences/indicadores")
def indicators(ctx):
    judge_module_guard(ctx, "evidencias")
    return evidence_indicators(ctx, ctx.query.get("season_id"))
