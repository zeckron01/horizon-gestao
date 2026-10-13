"""Administração: temporadas, áreas, configurações, auditoria, inconsistências, modelos, backup e restauração."""
import json
import re
import sqlite3
from datetime import datetime, timezone

from .. import APP_VERSION
from ..config import cfg
from ..core import (ADMIN_ONLY_SETTINGS, DEFAULT_SETTINGS, F, INTERNAL, ROLES, STAFF, USER_SCOPED, ApiError, Raw, Resource, audit, bad,
                    check_order, conflict, forbidden, fmt_date, from_cents, get_setting, not_found, route, set_setting, team_tz, today_local)
from ..db import get_db, invalidate_connections, migrate, new_id, now_iso


# ----------------------------------------------------------------------------- dados iniciais
def ensure_base_data(db):
    """Cria, uma única vez, as áreas das planilhas, a temporada 2026 e os três eventos iniciais (todos editáveis)."""
    if db.one("SELECT 1 FROM settings WHERE key='bootstrapped'"):
        return
    ts = now_iso()
    for i, (name, color) in enumerate((("Engenharia", "#cc0000"), ("Gestão do projeto", "#4a86e8"), ("Empreendimento", "#9f67c2"), ("Projeto social", "#6aa84f")), 1):
        db.insert("areas", {"id": new_id(), "name": name, "color": color, "sort": i, "created_at": ts, "updated_at": ts})
    sid = new_id()
    db.insert("seasons", {"id": sid, "name": "Temporada 2026", "year": 2026, "created_at": ts, "updated_at": ts})
    set_setting(db, "default_season_id", sid)
    for name, typ, s, e in (("TRGO", "Torneio", "2026-10-23", None), ("FIRST LEGO League", "Competição", "2026-12-04", "2026-12-05"),
                            ("Regional de STEM Racing", "Competição", "2026-12-06", None)):
        db.insert("events", {"id": new_id(), "season_id": sid, "name": name, "type": typ, "start_date": s, "end_date": e, "status": "ativo",
                             "source": "registro inicial (editável)", "created_at": ts, "updated_at": ts})
    set_setting(db, "bootstrapped", True)


# ----------------------------------------------------------------------------- temporadas e áreas
class Seasons(Resource):
    def validate(self, ctx, s, old):
        check_order(s, "start_date", "end_date", "Início", "Término")
        if ctx.db.one("SELECT 1 FROM seasons WHERE name=? AND id<>? AND deleted_at IS NULL", (s["name"], (old or {}).get("id", ""))):
            raise bad("Já existe uma temporada com este nome.", {"name": "Nome já utilizado."})

    def before_delete(self, ctx, s):
        if get_setting(ctx.db, "default_season_id") == s["id"]:
            raise conflict("Esta é a temporada padrão. Defina outra temporada como padrão antes de excluir.")


SEASON = Seasons("seasons", "seasons", "Temporada", {
    "name": F("text", "Nome", required=True, max=80),
    "year": F("int", "Ano", required=True, min=2000, max=2100),
    "start_date": F("date", "Início"),
    "end_date": F("date", "Término"),
    "notes": F("long", "Observações", max=4000),
    "budget_planned": F("money", "Orçamento total planejado", default=0),
    "fundraising_goal": F("money", "Meta de captação", default=0),
    "opening_balance": F("money", "Saldo inicial em caixa", min=-10 ** 12, default=0),
}, order="year DESC, name", refs=tuple(
    (t, "season_id", lab, "deleted_at IS NULL") for t, lab in (
        ("activities", "atividade(s)"), ("wbs_elements", "elemento(s) da EAP"), ("risks", "risco(s)"), ("events", "evento(s)"),
        ("expenses", "despesa(s)"), ("incomes", "receita(s)"), ("stakeholders", "stakeholder(s)"), ("evidence_weeks", "semana(s) de evidências")))
    + (("movements", "season_id", "movimento(s) de caixa"), ("scopes", "season_id", "escopo")))
SEASON.register()


class Areas(Resource):
    def validate(self, ctx, a, old):
        if ctx.db.one("SELECT 1 FROM areas WHERE name=? AND id<>? AND deleted_at IS NULL", (a["name"], (old or {}).get("id", ""))):
            raise bad("Já existe uma área com este nome.", {"name": "Nome já utilizado."})
        if not old and not a.get("sort"):
            a["sort"] = (ctx.db.val("SELECT COALESCE(MAX(sort),0) FROM areas") or 0) + 1


AREA = Areas("areas", "areas", "Área", {
    "name": F("text", "Nome", required=True, max=80),
    "color": F("color", "Cor", default="#64748b"),
    "sort": F("int", "Ordem", min=0, max=1000),
    "description": F("long", "Descrição", max=2000),
    "logo_file_id": F("ref", "Logo da área", ref="files"),
}, order="sort, name", refs=(
    ("activities", "area_id", "atividade(s)", "deleted_at IS NULL"), ("users", "area_id", "integrante(s)", "deleted_at IS NULL"),
    ("wbs_elements", "area_id", "elemento(s) da EAP", "deleted_at IS NULL"), ("expenses", "area_id", "despesa(s)", "deleted_at IS NULL"),
    ("evidence_entries", "area_id", "registro(s) de evidência"), ("risk_areas", "area_id", "risco(s)"),
    ("stakeholders", "area_id", "stakeholder(s)", "deleted_at IS NULL"), ("events", "area_id", "evento(s)", "deleted_at IS NULL")))
AREA.register()


@route("POST", "/api/areas/reordenar")
def reorder_areas(ctx):
    ids = ctx.body.get("ids")
    if not isinstance(ids, list):
        raise bad("Lista de áreas inválida.")
    for i, aid in enumerate(ids, 1):
        ctx.db.ex("UPDATE areas SET sort=?, updated_at=? WHERE id=?", (i, now_iso(), aid))
    audit(ctx, "reordenar", "areas", None, "Ordem das áreas alterada")
    return {"ok": True}


def _shift_year(day, years):
    if not day:
        return None
    y, m, dd = int(day[:4]) + years, int(day[5:7]), int(day[8:10])
    if m == 2 and dd == 29:
        dd = 28
    return f"{y:04d}-{m:02d}-{dd:02d}"


@route("POST", "/api/seasons/{id}/copiar-estrutura")
def copy_structure(ctx):
    """Copia a estrutura de outra temporada (EAP, atividades com dependências, riscos, eventos) como modelo.
    Nada de execução é copiado: situação, datas reais, aprovações, pagamentos e evidências começam do zero."""
    db = ctx.db
    target = SEASON.raw(db, ctx.params["id"])
    source = SEASON.raw(db, str(ctx.body.get("from_season_id")))
    if not target or not source or target["id"] == source["id"]:
        raise bad("Selecione uma temporada de origem diferente da de destino.")
    include = set(ctx.body.get("include") or ["wbs", "activities", "risks"])
    years = target["year"] - source["year"]
    ts = now_iso()
    counts = {}
    wmap = {}
    if "wbs" in include:
        rows = db.q("SELECT * FROM wbs_elements WHERE season_id=? AND deleted_at IS NULL ORDER BY sort", (source["id"],))
        for r in rows:
            wmap[r["id"]] = new_id()
        for r in rows:
            db.insert("wbs_elements", {"id": wmap[r["id"]], "season_id": target["id"], "parent_id": wmap.get(r["parent_id"]), "sort": r["sort"], "name": r["name"],
                                       "description": r["description"], "area_id": r["area_id"], "duration_days": r["duration_days"],
                                       "start_date": _shift_year(r["start_date"], years), "end_date": _shift_year(r["end_date"], years),
                                       "cost_estimate_cents": r["cost_estimate_cents"], "acceptance_criteria": r["acceptance_criteria"],
                                       "source": f"modelo:{source['name']}", "created_at": ts, "updated_at": ts, "created_by": ctx.uid})
        counts["eap"] = len(rows)
    amap = {}
    if "activities" in include:
        rows = db.q("SELECT * FROM activities WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (source["id"],))
        for r in rows:
            amap[r["id"]] = new_id()
            ps, pe = _shift_year(r["planned_start"], years), _shift_year(r["planned_end"], years)
            db.insert("activities", {"id": amap[r["id"]], "season_id": target["id"], "area_id": r["area_id"], "wbs_id": wmap.get(r["wbs_id"]), "seq": r["seq"],
                                     "title": r["title"], "description": r["description"], "objective": r["objective"], "category": r["category"], "priority": r["priority"],
                                     "planned_start": ps, "planned_end": pe, "baseline_start": ps, "baseline_end": pe, "is_milestone": r["is_milestone"],
                                     "completion_criteria": r["completion_criteria"], "milestone_ref": r["milestone_ref"], "alert_days": r["alert_days"],
                                     "source": f"modelo:{source['name']}", "created_at": ts, "updated_at": ts, "created_by": ctx.uid})
        n = 0
        for dp in db.q("SELECT d.* FROM dependencies d JOIN activities a ON a.id=d.predecessor_id WHERE a.season_id=?", (source["id"],)):
            if dp["predecessor_id"] in amap and dp["successor_id"] in amap:
                db.insert("dependencies", {"id": new_id(), "predecessor_id": amap[dp["predecessor_id"]], "successor_id": amap[dp["successor_id"]],
                                           "type": dp["type"], "lag_days": dp["lag_days"], "created_by": ctx.uid, "created_at": ts})
                n += 1
        counts["atividades"], counts["dependencias"] = len(rows), n
        if "raci" in include:
            active = {u["id"] for u in db.q("SELECT id FROM users WHERE active=1 AND deleted_at IS NULL AND role<>'juiz'")}
            teachers = {u["id"] for u in db.q("SELECT id FROM users WHERE is_teacher=1")}
            m = 0
            for r in db.q("SELECT r.* FROM raci r JOIN activities a ON a.id=r.activity_id WHERE a.season_id=?", (source["id"],)):
                # as regras de governança continuam valendo na cópia: professor técnico e criador nunca ficam como aprovadores
                if r["activity_id"] in amap and r["user_id"] in active and not (r["role"] == "A" and (r["user_id"] in teachers or r["user_id"] == ctx.uid)):
                    db.ex("INSERT INTO raci (activity_id, user_id, role) VALUES (?,?,?)", (amap[r["activity_id"]], r["user_id"], r["role"]))
                    m += 1
            counts["raci"] = m
    if "risks" in include:
        rows = db.q("SELECT * FROM risks WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (source["id"],))
        for r in rows:
            rid = new_id()
            db.insert("risks", {"id": rid, "season_id": target["id"], "code": r["code"], "title": r["title"], "description": r["description"], "category": r["category"],
                                "cause": r["cause"], "risk_event": r["risk_event"], "consequence": r["consequence"], "probability": r["probability"], "impact": r["impact"],
                                "preventive_plan": r["preventive_plan"], "contingency_plan": r["contingency_plan"], "mitigation": r["mitigation"],
                                "warning_indicators": r["warning_indicators"], "owner_text": r["owner_text"], "status": "aberto", "identified_at": today_local(db),
                                "source": f"modelo:{source['name']}", "created_at": ts, "updated_at": ts, "created_by": ctx.uid})
            for a in db.q("SELECT area_id FROM risk_areas WHERE risk_id=?", (r["id"],)):
                db.ex("INSERT INTO risk_areas (risk_id, area_id) VALUES (?,?)", (rid, a["area_id"]))
        counts["riscos"] = len(rows)
    if "events" in include:
        rows = db.q("SELECT * FROM events WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (source["id"],))
        for r in rows:
            db.insert("events", {"id": new_id(), "season_id": target["id"], "name": r["name"], "type": r["type"], "description": r["description"],
                                 "start_date": _shift_year(r["start_date"], years), "end_date": _shift_year(r["end_date"], years), "start_time": r["start_time"],
                                 "location": r["location"], "area_id": r["area_id"], "objectives": r["objectives"], "checklist": r["checklist"], "status": "ativo",
                                 "source": f"modelo:{source['name']}", "created_at": ts, "updated_at": ts, "created_by": ctx.uid})
        counts["eventos"] = len(rows)
    audit(ctx, "copiar_estrutura", "seasons", target["id"], f"Estrutura copiada de {source['name']} para {target['name']}: {counts}")
    return {"ok": True, "counts": counts, "shift_years": years}


# ----------------------------------------------------------------------------- configurações
def _validate_setting(db, key, v):
    if key in ("team_name", "team_subtitle"):
        return F("text", "Nome", required=key == "team_name", max=120).parse(db, key, v)
    if key == "timezone":
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(str(v))
        except Exception:
            raise bad("Fuso horário inválido (ex.: America/Sao_Paulo).")
        return str(v)
    if key == "date_format":
        if v not in ("DD/MM/AAAA", "AAAA-MM-DD", "DD/MM/AA"):
            raise bad("Formato de data inválido.")
        return v
    if key == "currency":
        if v != "BRL":
            raise bad("No momento a plataforma opera apenas em Real (BRL).")
        return v
    if key == "default_season_id":
        if not db.one("SELECT 1 FROM seasons WHERE id=? AND deleted_at IS NULL", (v,)):
            raise bad("Temporada inválida.")
        return v
    if not isinstance(v, (dict, list)):
        raise bad("Valor de configuração inválido.")
    if len(json.dumps(v)) > 100000:
        raise bad("Configuração extensa demais.")
    if key == "theme":
        for c in ("primary", "accent"):
            F("color", "Cor", required=True).parse(db, c, v.get(c))
        if v.get("mode") not in ("claro", "escuro", "auto"):
            raise bad("Tema inválido.")
        return {"primary": v["primary"], "accent": v["accent"], "mode": v["mode"], "background_file_id": v.get("background_file_id"), "logo_file_id": v.get("logo_file_id")}
    if key == "alerts":
        out = {}
        for k, dflt in DEFAULT_SETTINGS["alerts"].items():
            out[k] = F("int", "Dias", min=0 if k == "delivery_days" else 1, max=365).parse(db, k, v.get(k, dflt))
        return out
    if key == "risk_scale":
        size = v.get("size")
        if size not in (3, 4, 5, 10):
            raise bad("Tamanho de escala inválido (use 3, 4, 5 ou 10).")
        for lab in ("probability_labels", "impact_labels"):
            if not isinstance(v.get(lab), list) or len(v[lab]) != size or not all(str(x).strip() for x in v[lab]):
                raise bad(f"Informe {size} rótulos para a escala.")
        bands = v.get("bands")
        if not isinstance(bands, list) or not 2 <= len(bands) <= 6:
            raise bad("Defina entre 2 e 6 faixas de classificação.")
        prev = 0
        for b in bands:
            if not str(b.get("name") or "").strip() or not isinstance(b.get("max"), int) or b["max"] <= prev:
                raise bad("As faixas devem ter nome e limites máximos crescentes.")
            F("color", "Cor", required=True).parse(db, "color", b.get("color"))
            prev = b["max"]
        if bands[-1]["max"] != size * size:
            raise bad(f"A última faixa deve terminar em {size * size} (pontuação máxima).")
        return {"size": size, "probability_labels": [str(x).strip()[:40] for x in v["probability_labels"]], "impact_labels": [str(x).strip()[:40] for x in v["impact_labels"]],
                "bands": [{"name": str(b["name"]).strip()[:30], "max": b["max"], "color": b["color"], "hint": str(b.get("hint") or "")[:80]} for b in bands]}
    if key == "catalogs":
        out = {}
        for k, dflt in DEFAULT_SETTINGS["catalogs"].items():
            val = v.get(k, dflt)
            if not isinstance(val, list) or len(val) > 200:
                raise bad("Lista de categorias inválida.")
            if k == "acquisition_types":
                keys = set()
                out[k] = []
                for t in val:
                    kk = re.sub(r"[^a-z0-9_]", "", str(t.get("key") or "").lower())[:40]
                    if not kk or kk in keys or not str(t.get("label") or "").strip():
                        raise bad("Tipos de aquisição: cada tipo precisa de chave única e nome.")
                    keys.add(kk)
                    out[k].append({"key": kk, "label": str(t["label"]).strip()[:80], "is_cash": bool(t.get("is_cash"))})
                used = {r["acquisition_type"] for r in db.q("SELECT DISTINCT acquisition_type FROM expenses WHERE deleted_at IS NULL")}
                if used - keys:
                    raise bad("Não é possível remover tipos de aquisição em uso por despesas: " + ", ".join(sorted(used - keys)))
            else:
                out[k] = list(dict.fromkeys(str(x).strip()[:80] for x in val if str(x).strip()))
        return out
    if key == "judge_view":
        mods = {k: bool((v.get("modules") or {}).get(k, d)) for k, d in DEFAULT_SETTINGS["judge_view"]["modules"].items()}
        return {"finance_summary": bool(v.get("finance_summary")), "finance_detail": bool(v.get("finance_detail")), "contacts": bool(v.get("contacts")), "modules": mods}
    if key == "evidence_weights":
        out = {k: F("int", "Peso", min=0, max=100).parse(db, k, v.get(k, 0)) or 0 for k in DEFAULT_SETTINGS["evidence_weights"]}
        if sum(out.values()) <= 0:
            raise bad("Defina ao menos um critério com peso maior que zero.")
        return out
    if key == "governance":
        return {"require_approval_to_complete": bool(v.get("require_approval_to_complete")), "reapprove_on_change": bool(v.get("reapprove_on_change", True))}
    if key == "custom_fields":
        acts = []
        for f in (v.get("activities") or [])[:30]:
            k = re.sub(r"[^a-z0-9_]", "", str(f.get("key") or "").lower())[:40]
            if k and str(f.get("label") or "").strip() and f.get("type") in ("texto", "numero", "data", "sim_nao"):
                acts.append({"key": k, "label": str(f["label"]).strip()[:60], "type": f["type"]})
        return {"activities": acts}
    if key == "report_templates":
        return [{"id": str(t.get("id") or new_id())[:40], "name": str(t.get("name") or "Modelo")[:80], "report": str(t.get("report") or "")[:40],
                 "options": t.get("options") if isinstance(t.get("options"), dict) else {}} for t in v[:50] if isinstance(t, dict)]
    if key == "uploads":
        exts = [re.sub(r"[^a-z0-9]", "", str(x).lower())[:8] for x in (v.get("allowed_ext") or [])]
        banned = {"html", "htm", "svg", "js", "exe", "bat", "cmd", "sh", "php", "msi", "jar", "com", "scr", "vbs", "ps1"}
        exts = [x for x in dict.fromkeys(exts) if x and x not in banned]
        if not exts:
            raise bad("Informe ao menos um tipo de arquivo permitido.")
        return {"allowed_ext": exts}
    raise bad("Configuração desconhecida.")


@route("PUT", "/api/settings/{key}")
def save_setting(ctx):
    key = ctx.params["key"]
    if key not in DEFAULT_SETTINGS or key in USER_SCOPED:
        raise bad("Configuração desconhecida.")
    gestor_ok = {"catalogs", "alerts", "risk_scale", "evidence_weights", "custom_fields", "report_templates"}
    if ctx.role != "admin" and key not in gestor_ok:
        raise forbidden("Esta configuração é restrita ao administrador.")
    before = get_setting(ctx.db, key)
    val = _validate_setting(ctx.db, key, ctx.body.get("value"))
    set_setting(ctx.db, key, val, by=ctx.uid)
    audit(ctx, "configurar", "settings", key, f"Configuração alterada: {key}", before={"value": before}, after={"value": val})
    return {"key": key, "value": get_setting(ctx.db, key)}


# ----------------------------------------------------------------------------- auditoria
@route("GET", "/api/audit", allow=INTERNAL)
def list_audit(ctx):
    q = ctx.query
    conds, ps = [], []
    if ctx.role not in STAFF:
        # integrantes consultam apenas o histórico de um registro específico
        if not (q.get("entity_type") and q.get("entity_id")) or q["entity_type"] in ("users", "sessao", "settings", "permissao", "expenses", "incomes"):
            raise forbidden("O histórico geral é restrito à gestão.")
    for k in ("entity_type", "entity_id", "user_id", "action"):
        if q.get(k):
            conds.append(f"{k}=?")
            ps.append(q[k])
    if q.get("from"):
        conds.append("at >= ?")
        ps.append(q["from"])
    if q.get("to"):
        conds.append("at <= ?")
        ps.append(q["to"] + "T23:59:59Z")
    if q.get("q"):
        conds.append("summary LIKE ?")
        ps.append("%" + q["q"] + "%")
    w = (" WHERE " + " AND ".join(conds)) if conds else ""
    lim, off = ctx.qint("limit", 100, 1, 1000), ctx.qint("offset", 0, 0)
    rows = ctx.db.q(f"SELECT * FROM audit_log{w} ORDER BY id DESC LIMIT ? OFFSET ?", ps + [lim, off])
    for r in rows:
        r["before"] = json.loads(r["before"]) if r["before"] else None
        r["after"] = json.loads(r["after"]) if r["after"] else None
        if ctx.role != "admin":
            r.pop("ip", None)
    return {"items": rows, "total": ctx.db.val(f"SELECT COUNT(*) FROM audit_log{w}", ps)}


# ----------------------------------------------------------------------------- inconsistências
REVIEW_TABLES = {"activities": ("activities", "title", "Atividade"), "wbs": ("wbs_elements", "name", "EAP"), "risks": ("risks", "title", "Risco"),
                 "expenses": ("expenses", "name", "Despesa"), "incomes": ("incomes", "description", "Receita"),
                 "stakeholders": ("stakeholders", "name", "Stakeholder"), "evidence_weeks": ("evidence_weeks", "month", "Evidências"),
                 "scopes": ("scopes", None, "Escopo")}


def find_inconsistencies(ctx, season_id):
    from .activities import load_graph, raci_alerts, schedule_conflicts
    db = ctx.db
    out = []
    today = today_local(db)

    def add(module, level, title, message, entity_type=None, entity_id=None, fix=None, code=None):
        out.append({"module": module, "level": level, "title": title, "message": message, "entity_type": entity_type, "entity_id": entity_id, "fix": fix, "code": code})
    for et, (table, tcol, label) in REVIEW_TABLES.items():
        for r in db.q(f"SELECT * FROM {table} WHERE season_id=? AND needs_review=1" + (" AND deleted_at IS NULL" if et != "scopes" else ""), (season_id,)):
            title = "Escopo do projeto" if not tcol else (r[tcol] if et != "evidence_weeks" else f"{r['month']} — semana {r['week_number']}")
            add(label, "revisar", title, r["review_note"] or "Registro importado que precisa de revisão.", et, r["id"], "resolver_revisao", "revisao_importacao")
    acts, deps = load_graph(db, season_id)
    for c in schedule_conflicts(acts, deps, today):
        add("Cronograma", "erro" if c["kind"] == "conflito" else "aviso", c["successor_title"], c["message"], "activities", c["successor_id"], None, "dependencia_" + c["kind"])
    seen = {}
    for a in acts.values():
        if a["status"] == "cancelada":
            continue
        if not a["owner_id"]:
            add("Planejamento", "aviso", a["title"], "Atividade sem responsável principal.", "activities", a["id"], None, "sem_responsavel")
        if not a["planned_end"]:
            add("Planejamento", "aviso", a["title"], "Atividade sem prazo final planejado.", "activities", a["id"], None, "sem_prazo")
        elif not a["planned_start"]:
            add("Cronograma", "info", a["title"], "Atividade sem data de início planejada (aparece no Gantt apenas como ponto no prazo).", "activities", a["id"], None, "sem_inicio")
        if a["status"] != "concluida" and a["planned_end"] and a["planned_end"] < today and not (a["delay_justification"] or "").strip():
            add("Cronograma", "erro", a["title"], f"Atrasada desde {fmt_date(a['planned_end'])} sem justificativa registrada.", "activities", a["id"], None, "atraso_sem_justificativa")
        if a["status"] == "concluida" and a["planned_end"] and a["actual_end"] and a["actual_end"] > a["planned_end"] and not (a["delay_justification"] or "").strip():
            add("Cronograma", "aviso", a["title"], f"Concluída com atraso ({fmt_date(a['actual_end'])}, prazo {fmt_date(a['planned_end'])}) sem justificativa registrada.", "activities", a["id"], None, "conclusao_tardia_sem_justificativa")
        key = (a["area_id"], a["title"].strip().lower())
        if key in seen:
            add("Planejamento", "aviso", a["title"], "Título repetido na mesma área — verifique se é duplicidade.", "activities", a["id"], None, "titulo_duplicado")
        seen[key] = a["id"]
    users = {u["id"]: u for u in db.q("SELECT * FROM users WHERE deleted_at IS NULL")}
    actives = [u for u in users.values() if u["active"] and u["role"] != "juiz"]
    ras = {}
    for r in db.q("SELECT r.* FROM raci r JOIN activities a ON a.id=r.activity_id WHERE a.season_id=?", (season_id,)):
        ras.setdefault(r["activity_id"], {})[r["user_id"]] = r["role"]
    for a in acts.values():
        if a["id"] in ras:
            for al in raci_alerts(db, a, ras[a["id"]], users, actives):
                if al["level"] == "erro":
                    add("RACI", "erro", a["title"], al["message"], "activities", a["id"], None, "raci_" + al["code"])
    for e in db.q("SELECT * FROM expenses WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status<>'cancelada'", (season_id,)):
        for kind, lab in (("planned", "planejado"), ("actual", "real")):
            q, u, t = e["quantity"], e[f"unit_{kind}_cents"], e[f"total_{kind}_cents"]
            if q is not None and u is not None and t is not None and abs(round(q * u) - t) > 1:
                add("Orçamento", "erro", e["name"], f"Valor total {lab} (R$ {from_cents(t):.2f}) diferente de quantidade × unitário ({q:g} × R$ {from_cents(u):.2f} = R$ {from_cents(round(q * u)):.2f}).",
                    "expenses", e["id"], f"corrigir_total_{kind}", "total_inconsistente")
        if not e["is_cash"] and not (e["total_actual_cents"] or e["total_planned_cents"]):
            add("Orçamento", "info", e["name"], "Item recebido por patrocínio/doação sem valor estimado — o apoio não aparece nos totais de contribuição não financeira.", "expenses", e["id"], None, "apoio_sem_valor")
    season = db.one("SELECT * FROM seasons WHERE id=?", (season_id,))
    items = db.val("SELECT COALESCE(SUM(total_planned_cents),0) FROM expenses WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND is_cash=1 AND status<>'cancelada'", (season_id,))
    if season and season["budget_planned_cents"] and items > season["budget_planned_cents"]:
        add("Orçamento", "erro", "Orçamento da temporada", f"A soma dos itens planejados (R$ {from_cents(items):.2f}) supera o orçamento total (R$ {from_cents(season['budget_planned_cents']):.2f}).", "seasons", season_id, None, "orcamento_excedido")
    from .risks import RISK, expand_risks
    for r in expand_risks(db, [RISK.ser(x) for x in db.q("SELECT * FROM risks WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status<>'encerrado'", (season_id,))]):
        if r["no_owner"]:
            add("Riscos", "aviso", f"{r['code']} — {r['title']}", "Risco sem responsável (pessoa) definido." + (f" Planilha original: “{r['owner_text']}”." if r.get("owner_text") else ""), "risks", r["id"], None, "risco_sem_responsavel")
        if r["no_plan"]:
            add("Riscos", "aviso", f"{r['code']} — {r['title']}", "Risco sem plano de ação.", "risks", r["id"], None, "risco_sem_plano")
        if r["level_divergence"]:
            add("Riscos", "revisar", f"{r['code']} — {r['title']}", f"Nível na planilha original: “{r['original_level']}”; pela escala atual (P×I = {r['score']}): “{r['level']}”. Ajuste probabilidade/impacto ou as faixas.", "risks", r["id"], None, "nivel_divergente")
    for w in db.q("SELECT * FROM evidence_weeks WHERE season_id=? AND deleted_at IS NULL AND (start_date IS NULL OR end_date IS NULL)", (season_id,)):
        add("Evidências", "aviso", f"{w['month']} — semana {w['week_number']}", "Semana sem datas de início e término — os indicadores automáticos não podem ser calculados.", "evidence_weeks", w["id"], None, "semana_sem_datas")
    order = {"erro": 0, "revisar": 1, "aviso": 2, "info": 3}
    out.sort(key=lambda x: (order[x["level"]], x["module"], x["title"]))
    return out


@route("GET", "/api/admin/inconsistencias", allow=STAFF)
def inconsistencies(ctx):
    sid = ctx.query.get("season_id")
    if not sid:
        raise bad("Informe a temporada.")
    items = find_inconsistencies(ctx, sid)
    summary = {}
    for i in items:
        summary[i["level"]] = summary.get(i["level"], 0) + 1
    return {"items": items, "summary": summary}


@route("POST", "/api/admin/inconsistencias/corrigir", allow=STAFF)
def fix_inconsistency(ctx):
    """Correções seguras e explícitas: sempre escolhidas pelo usuário, nunca automáticas."""
    db, b = ctx.db, ctx.body
    fix, et, eid = b.get("fix"), b.get("entity_type"), b.get("entity_id")
    if fix == "resolver_revisao":
        if et not in REVIEW_TABLES:
            raise bad("Tipo de registro inválido.")
        table = REVIEW_TABLES[et][0]
        r = db.one(f"SELECT * FROM {table} WHERE id=?", (eid,))
        if not r:
            raise not_found("Registro")
        db.ex(f"UPDATE {table} SET needs_review=0, updated_at=? WHERE id=?", (now_iso(), eid))
        audit(ctx, "revisar", et, eid, f"Revisão concluída: {r.get('review_note') or ''}"[:300])
        return {"ok": True}
    if fix in ("corrigir_total_planned", "corrigir_total_actual"):
        kind = fix.rsplit("_", 1)[1]
        e = db.one("SELECT * FROM expenses WHERE id=? AND deleted_at IS NULL", (eid,))
        if not e:
            raise not_found("Despesa")
        mode = b.get("mode")
        q, u, t = e["quantity"], e[f"unit_{kind}_cents"], e[f"total_{kind}_cents"]
        if mode == "recalcular_total":
            newv = {f"total_{kind}_cents": int(round(q * u))}
        elif mode == "recalcular_unitario":
            if not q:
                raise bad("Quantidade zerada: não é possível recalcular o valor unitário.")
            unit = int(round(t / q))
            if abs(round(q * unit) - t) > 1:
                raise bad("O total não é divisível pela quantidade sem arredondamento; ajuste manualmente no cadastro da despesa.")
            newv = {f"unit_{kind}_cents": unit, f"total_{kind}_cents": int(round(q * unit))}
        else:
            raise bad("Escolha como corrigir: recalcular o total ou recalcular o valor unitário.")
        sets = ", ".join(f"{k}=?" for k in newv)
        db.ex(f"UPDATE expenses SET {sets}, updated_at=?, version=version+1 WHERE id=?", list(newv.values()) + [now_iso(), eid])
        audit(ctx, "corrigir", "expenses", eid, f"{e['name']} — {mode}", before={k: e[k] for k in newv}, after=newv)
        return {"ok": True}
    raise bad("Correção desconhecida.")


# ----------------------------------------------------------------------------- modelos reutilizáveis
TEMPLATE_KINDS = ("atividade", "risco", "evento", "relatorio")


@route("GET", "/api/templates", allow=INTERNAL)
def list_templates(ctx):
    rows = ctx.db.q("SELECT * FROM templates WHERE deleted_at IS NULL" + (" AND kind=?" if ctx.query.get("kind") else "") + " ORDER BY kind, name",
                    (ctx.query["kind"],) if ctx.query.get("kind") else ())
    return {"items": [dict(r, data=json.loads(r["data"])) for r in rows]}


@route("POST", "/api/templates")
def create_template(ctx):
    kind = ctx.body.get("kind")
    if kind not in TEMPLATE_KINDS:
        raise bad("Tipo de modelo inválido.")
    name = F("text", "Nome do modelo", required=True, max=120).parse(ctx.db, "name", ctx.body.get("name"))
    data = ctx.body.get("data")
    if not isinstance(data, dict) or len(json.dumps(data)) > 50000:
        raise bad("Conteúdo do modelo inválido.")
    for k in ("id", "version", "season_id", "created_at", "updated_at", "created_by", "status", "progress", "actual_start", "actual_end", "approval_status"):
        data.pop(k, None)
    row = {"id": new_id(), "kind": kind, "name": name, "data": json.dumps(data, ensure_ascii=False), "created_by": ctx.uid, "created_at": now_iso()}
    ctx.db.insert("templates", row)
    audit(ctx, "criar", "templates", row["id"], f"Modelo de {kind}: {name}")
    return dict(row, data=data)


@route("DELETE", "/api/templates/{id}")
def delete_template(ctx):
    t = ctx.db.one("SELECT * FROM templates WHERE id=? AND deleted_at IS NULL", (ctx.params["id"],))
    if not t:
        raise not_found("Modelo")
    ctx.db.ex("UPDATE templates SET deleted_at=? WHERE id=?", (now_iso(), t["id"]))
    audit(ctx, "excluir", "templates", t["id"], f"Modelo: {t['name']}")
    return {"ok": True}


# ----------------------------------------------------------------------------- backup e restauração
# Dois mecanismos, conforme o banco em uso:
#  - SQLite: cópia consistente do arquivo do banco (.db) na pasta de backups;
#  - PostgreSQL: backup lógico (.hzb) guardado na tabela `backups` do próprio banco (não há disco no servidor).
_BK_RX = re.compile(r"^horizon-[0-9A-Za-z_\-]+\.(db|hzb)$")
_KINDS = {"auto": "automático", "pre-restauracao": "pré-restauração", "enviado": "enviado", "manual": "manual"}


def _pg():
    return bool(cfg.DATABASE_URL)


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _kind(name):
    return next((v for k, v in _KINDS.items() if f"-{k}." in name), "manual")


def _create_backup_pg(db, label):
    """Backup lógico de todas as tabelas, lido em uma transação (cópia consistente) e conferido antes de ser guardado."""
    from .. import backup_logical as bl
    own = not db.in_tx
    if own:
        db.begin()
    try:
        data = bl.dump(db)
        head, _tables = bl.read(data, require_admin=False)   # relê e confere a soma de verificação do que acabou de ser gerado
        name = f"horizon-{_stamp()}-{label}.hzb"
        n = 1
        while db.one("SELECT 1 FROM backups WHERE name=?", (name,)):
            n += 1
            name = f"horizon-{_stamp()}-{n}-{label}.hzb"
        db.insert("backups", {"name": name, "kind": _kind(name), "created_at": now_iso(), "size": len(data), "data": data})
        if own:
            db.commit()
    except BaseException:
        if own:
            db.rollback()
        raise
    return {"name": name, "size": len(data), "integrity": "ok", "migrations": len(head["migrations"])}


def create_backup(label="manual", db=None):
    """Cria um backup do banco em uso e confere a integridade da cópia."""
    if _pg():
        return _create_backup_pg(db or get_db(), label)
    cfg.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"horizon-{_stamp()}-{label}.db"
    path = cfg.BACKUP_DIR / name
    src = sqlite3.connect(str(cfg.DB_PATH), timeout=30)
    dst = sqlite3.connect(str(path))
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        n = dst.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        path.unlink(missing_ok=True)
        raise ApiError(500, "backup_invalido", f"O backup falhou na verificação de integridade: {ok}")
    return {"name": name, "size": path.stat().st_size, "integrity": ok, "migrations": n}


def list_backups(db=None):
    if _pg():
        return [dict(r, kind=_kind(r["name"])) for r in (db or get_db()).q("SELECT name, size, created_at FROM backups ORDER BY created_at DESC, name DESC")]
    if not cfg.BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(cfg.BACKUP_DIR.glob("horizon-*.db"), reverse=True):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "created_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "kind": _kind(p.name)})
    return out


def daily_backup_if_due(db, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(team_tz(db))
    if now.hour < cfg.BACKUP_HOUR:
        return None
    day = now.strftime("%Y%m%d")
    marker = db.one("SELECT value FROM settings WHERE key='last_auto_backup' AND scope='global'")
    if marker and json.loads(marker["value"]) == day:
        return None
    b = create_backup("auto", db)
    set_setting(db, "last_auto_backup", day)
    if _pg():
        for old in db.q("SELECT name FROM backups WHERE name LIKE ? ORDER BY created_at DESC, name DESC", ("%-auto.hzb",))[cfg.BACKUP_KEEP:]:
            db.ex("DELETE FROM backups WHERE name=?", (old["name"],))
    else:
        autos = sorted(cfg.BACKUP_DIR.glob("horizon-*-auto.db"), reverse=True)
        for old in autos[cfg.BACKUP_KEEP:]:
            old.unlink(missing_ok=True)
    return b


def db_size(db):
    if _pg():
        return db.val("SELECT pg_database_size(current_database())")
    return cfg.DB_PATH.stat().st_size


@route("GET", "/api/admin/backups", allow=("admin",))
def backups(ctx):
    last = ctx.db.one("SELECT value FROM settings WHERE key='last_auto_backup'")
    return {"items": list_backups(ctx.db), "dir": "tabela “backups” do banco PostgreSQL" if _pg() else str(cfg.BACKUP_DIR),
            "storage": "banco" if _pg() else "disco", "format": "hzb" if _pg() else "db",
            "keep": cfg.BACKUP_KEEP, "auto_enabled": cfg.JOBS_ENABLED, "auto_hour": cfg.BACKUP_HOUR,
            "last_auto": json.loads(last["value"]) if last else None, "db_size": db_size(ctx.db),
            "max_upload_mb": cfg.MAX_UPLOAD_MB}


@route("POST", "/api/admin/backups", allow=("admin",), tx=False)
def backup_now(ctx):
    b = create_backup("manual", ctx.db)
    ctx.db.begin()
    audit(ctx, "backup", "backup", b["name"], f"Backup manual criado ({b['size']} bytes, integridade {b['integrity']})")
    ctx.db.commit()
    return b


def _bk_name(name):
    if not _BK_RX.match(name or ""):
        raise bad("Nome de backup inválido.")
    return name


def _bk_path(name):
    p = cfg.BACKUP_DIR / _bk_name(name)
    if not p.exists():
        raise not_found("Backup")
    return p


def _bk_data_pg(db, name):
    row = db.one("SELECT name, data FROM backups WHERE name=?", (_bk_name(name),))
    if not row:
        raise not_found("Backup")
    return bytes(row["data"])


@route("GET", "/api/admin/backups/{name}", allow=("admin",))
def backup_download(ctx):
    if _pg():
        return Raw(_bk_data_pg(ctx.db, ctx.params["name"]), "application/octet-stream", ctx.params["name"])
    p = _bk_path(ctx.params["name"])
    return Raw(p.read_bytes(), "application/vnd.sqlite3", p.name)


BK_CHUNK = 3 * 1024 * 1024   # hospedagens limitam o tamanho de cada requisição e de cada resposta (Vercel: ~4,5 MB)


@route("GET", "/api/admin/backups/{name}/parte", allow=("admin",))
def backup_download_part(ctx):
    """Download em partes de até 3 MB (?n=0,1,2…); a interface junta as partes no navegador."""
    n = ctx.qint("n", 0, 0, 100000)
    name = _bk_name(ctx.params["name"])
    if _pg():
        chunk = ctx.db.val("SELECT substring(data from ? for ?) FROM backups WHERE name=?", (n * BK_CHUNK + 1, BK_CHUNK, name))
        if chunk is None:
            raise not_found("Backup")
        chunk = bytes(chunk)
    else:
        with open(_bk_path(name), "rb") as fh:
            fh.seek(n * BK_CHUNK)
            chunk = fh.read(BK_CHUNK)
    return Raw(chunk, "application/octet-stream", f"{name}.parte{n}")


_UP_RX = re.compile(r"^[0-9a-f\-]{16,40}$")


def _upload_part(ctx):
    """Recebe uma parte de um envio em partes. Devolve o arquivo completo quando a última parte chega; senão, None."""
    import hashlib
    b = ctx.body
    up_id = str(b.get("upload_id") or "")
    try:
        part, parts = int(b.get("part")), int(b.get("parts"))
    except (TypeError, ValueError):
        raise bad("Envio em partes inválido.")
    if not _UP_RX.match(up_id) or not (1 <= parts <= 200) or not (0 <= part < parts):
        raise bad("Envio em partes inválido.")
    data = ctx.files[0]["data"]
    if len(data) > BK_CHUNK + 1024:
        raise bad("Parte acima do tamanho permitido.")
    db = ctx.db
    db.begin()
    db.ex("DELETE FROM files WHERE entity_type='backup_part' AND entity_id=? AND name=? AND uploaded_by=?", (up_id, f"{part:04d}", ctx.uid))
    db.insert("files", {"id": new_id(), "entity_type": "backup_part", "entity_id": up_id, "name": f"{part:04d}", "mime": "application/octet-stream",
                        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "data": data, "judge_visible": 0, "uploaded_by": ctx.uid, "created_at": now_iso()})
    db.commit()
    have = db.val("SELECT COUNT(*) FROM files WHERE entity_type='backup_part' AND entity_id=? AND uploaded_by=?", (up_id, ctx.uid))
    if have < parts:
        return None
    rows = db.q("SELECT data FROM files WHERE entity_type='backup_part' AND entity_id=? AND uploaded_by=? ORDER BY name", (up_id, ctx.uid))
    db.ex("DELETE FROM files WHERE entity_type='backup_part' AND entity_id=? AND uploaded_by=?", (up_id, ctx.uid))
    return b"".join(bytes(r["data"]) for r in rows)


def _validate_backup_file(path):
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise bad("O arquivo de backup está corrompido (falha na verificação de integridade).")
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"schema_migrations", "users", "activities", "audit_log"} <= tables:
                raise bad("O arquivo não é um backup desta plataforma.")
            if not c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND password_hash IS NOT NULL AND deleted_at IS NULL").fetchone()[0]:
                raise bad("O backup não contém nenhum administrador ativo; a restauração deixaria a plataforma sem acesso.")
        finally:
            c.close()
    except sqlite3.DatabaseError:
        raise bad("O arquivo enviado não é um banco SQLite válido.")


def _read_logical(data):
    from .. import backup_logical as bl
    try:
        return bl.read(data)
    except bl.BackupError as e:
        raise bad(str(e))


@route("POST", "/api/admin/backups/enviar", allow=("admin",), tx=False)
def backup_upload(ctx):
    if not ctx.files:
        raise bad("Nenhum arquivo enviado.")
    if ctx.body.get("parts") and str(ctx.body.get("parts")) != "1":
        data = _upload_part(ctx)
        if data is None:
            return {"pending": True}
    else:
        data = ctx.files[0]["data"]
    if _pg():
        _read_logical(data)
        name = f"horizon-{_stamp()}-enviado.hzb"
        ctx.db.begin()
        ctx.db.insert("backups", {"name": name, "kind": "enviado", "created_at": now_iso(), "size": len(data), "data": data})
        audit(ctx, "backup_enviado", "backup", name, f"Arquivo de backup enviado ({len(data)} bytes)")
        ctx.db.commit()
        return {"name": name, "size": len(data)}
    if not data.startswith(b"SQLite format 3\x00"):
        raise bad("O arquivo enviado não é um banco SQLite válido.")
    cfg.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"horizon-{_stamp()}-enviado.db"
    p = cfg.BACKUP_DIR / name
    p.write_bytes(data)
    try:
        _validate_backup_file(p)
    except ApiError:
        p.unlink(missing_ok=True)
        raise
    ctx.db.begin()
    audit(ctx, "backup_enviado", "backup", name, f"Arquivo de backup enviado ({len(data)} bytes)")
    ctx.db.commit()
    return {"name": name, "size": len(data)}


def restore_logical(db, data, who, source_name, ip=None):
    """PostgreSQL: valida o backup, guarda um backup de segurança do estado atual e substitui o conteúdo, tudo em uma transação."""
    from .. import backup_logical as bl
    _head, tables = _read_logical(data)
    db.begin()
    try:
        safety = _create_backup_pg(db, "pre-restauracao")
        try:
            bl.restore_pg(db, tables, None)
        except bl.BackupError as e:
            raise bad(str(e))
        db.ex("INSERT INTO audit_log (at, user_id, user_name, action, entity_type, entity_id, summary, ip) VALUES (?,?,?,?,?,?,?,?)",
              (now_iso(), who["id"], who["name"], "restaurar", "backup", source_name,
               f"Banco restaurado a partir de {source_name}. Backup de segurança: {safety['name']}", ip))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return safety


@route("POST", "/api/admin/backups/{name}/restaurar", allow=("admin",), tx=False)
def backup_restore(ctx):
    """Restaura um backup: valida o arquivo, cria um backup de segurança do estado atual e substitui o conteúdo do banco."""
    who = {"id": ctx.uid, "name": ctx.user["name"]}
    if _pg():
        data = _bk_data_pg(ctx.db, ctx.params["name"])
        if ctx.body.get("confirm") != "RESTAURAR":
            raise bad("Para confirmar, digite RESTAURAR.", code="confirmacao_obrigatoria")
        safety = restore_logical(ctx.db, data, who, ctx.params["name"], ctx.ip)
        return {"ok": True, "restored": ctx.params["name"], "safety_backup": safety["name"], "relogin": True}
    p = _bk_path(ctx.params["name"])
    if ctx.body.get("confirm") != "RESTAURAR":
        raise bad("Para confirmar, digite RESTAURAR.", code="confirmacao_obrigatoria")
    _validate_backup_file(p)
    safety = create_backup("pre-restauracao")
    src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    dst = sqlite3.connect(str(cfg.DB_PATH), timeout=60)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    invalidate_connections()
    db = get_db()
    migrate(db)  # backups antigos são atualizados para o esquema atual
    db.begin()
    db.ex("DELETE FROM sessions")  # todos precisam entrar novamente após a restauração
    db.ex("INSERT INTO audit_log (at, user_id, user_name, action, entity_type, entity_id, summary, ip) VALUES (?,?,?,?,?,?,?,?)",
          (now_iso(), who["id"], who["name"], "restaurar", "backup", p.name, f"Banco restaurado a partir de {p.name}. Backup de segurança: {safety['name']}", ctx.ip))
    db.commit()
    return {"ok": True, "restored": p.name, "safety_backup": safety["name"], "relogin": True}


@route("DELETE", "/api/admin/backups/{name}", allow=("admin",))
def backup_delete(ctx):
    if _pg():
        if not ctx.db.one("SELECT 1 FROM backups WHERE name=?", (_bk_name(ctx.params["name"]),)):
            raise not_found("Backup")
        ctx.db.ex("DELETE FROM backups WHERE name=?", (ctx.params["name"],))
        audit(ctx, "excluir", "backup", ctx.params["name"], "Backup removido")
        return {"ok": True}
    p = _bk_path(ctx.params["name"])
    p.unlink()
    audit(ctx, "excluir", "backup", p.name, "Arquivo de backup removido")
    return {"ok": True}


@route("GET", "/api/admin/sistema", allow=STAFF)
def system_info(ctx):
    db = ctx.db
    return {"version": APP_VERSION, "migrations": [r["version"] for r in db.q("SELECT version FROM schema_migrations ORDER BY version")],
            "database": ("PostgreSQL " + db.conn.server_version.split(" ")[0]) if db.dialect == "pg" else ("SQLite " + sqlite3.sqlite_version + " (WAL)"),
            "db_size": db_size(db), "serverless": cfg.SERVERLESS,
            "integrations": {"email": cfg.smtp_configured, "push": False, "cron_externo": bool(cfg.CRON_SECRET)},
            "jobs": {"enabled": cfg.JOBS_ENABLED, "interval_seconds": cfg.JOBS_INTERVAL_SECONDS, "last_run": db.val("SELECT MAX(started_at) FROM job_runs")},
            "counts": {t: db.val(f"SELECT COUNT(*) FROM {t}") for t in ("users", "activities", "dependencies", "wbs_elements", "risks", "events", "expenses",
                                                                         "incomes", "movements", "stakeholders", "evidence_entries", "files", "comments", "audit_log", "notifications")},
            "max_upload_mb": cfg.MAX_UPLOAD_MB}
