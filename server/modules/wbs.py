"""Estrutura Analítica do Projeto (EAP): árvore com códigos hierárquicos automáticos e progresso consolidado."""
from ..core import F, Resource, audit, bad, check_order, conflict, from_cents, judge_module_guard, not_found, route
from ..db import now_iso


class Wbs(Resource):
    def validate(self, ctx, w, old):
        check_order(w, "start_date", "end_date", "Início", "Término")
        if w.get("parent_id"):
            p = ctx.db.one("SELECT season_id FROM wbs_elements WHERE id=? AND deleted_at IS NULL", (w["parent_id"],))
            if not p or p["season_id"] != w["season_id"]:
                raise bad("O elemento superior pertence a outra temporada.")
            if old and is_descendant(ctx.db, w["parent_id"], old["id"]):
                raise bad("Um elemento não pode ser movido para dentro de si mesmo.")
        if not old and not w.get("sort"):
            w["sort"] = (ctx.db.val("SELECT COALESCE(MAX(sort),0) FROM wbs_elements WHERE season_id=? AND parent_id IS ? AND deleted_at IS NULL",
                                    (w["season_id"], w.get("parent_id"))) or 0) + 1


WBS = Wbs("wbs", "wbs_elements", "Elemento da EAP", {
    "season_id": F("ref", "Temporada", required=True, ref="seasons"),
    "parent_id": F("ref", "Elemento superior", ref="wbs_elements"),
    "name": F("text", "Nome", required=True, max=250),
    "description": F("long", "Descrição"),
    "area_id": F("ref", "Área", ref="areas"),
    "owner_id": F("ref", "Responsável", ref="users"),
    "duration_days": F("int", "Duração estimada (dias)", min=0, max=3650),
    "start_date": F("date", "Início"),
    "end_date": F("date", "Término"),
    "cost_estimate": F("money", "Custo estimado"),
    "acceptance_criteria": F("long", "Critérios de aceitação"),
    "manual_progress": F("int", "Progresso manual (%)", min=0, max=100),
}, season=True, archive=False, order="sort, name")


def is_descendant(db, node_id, ancestor_id):
    seen = set()
    while node_id and node_id not in seen:
        if node_id == ancestor_id:
            return True
        seen.add(node_id)
        r = db.one("SELECT parent_id FROM wbs_elements WHERE id=?", (node_id,))
        node_id = r["parent_id"] if r else None
    return False


def build_tree(db, season_id):
    """Lista em pré-ordem com código hierárquico, nível, progresso e custo consolidados."""
    rows = [WBS.ser(r) for r in db.q("SELECT * FROM wbs_elements WHERE season_id=? AND deleted_at IS NULL ORDER BY sort, name", (season_id,))]
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)
    acts = {}
    for a in db.q("SELECT wbs_id, progress, status FROM activities WHERE season_id=? AND wbs_id IS NOT NULL AND deleted_at IS NULL AND archived_at IS NULL", (season_id,)):
        acts.setdefault(a["wbs_id"], []).append(a)
    risks = {}
    for l in db.q("SELECT l.entity_id, COUNT(*) n FROM risk_links l JOIN risks r ON r.id=l.risk_id WHERE l.entity_type='wbs' AND r.deleted_at IS NULL GROUP BY l.entity_id"):
        risks[l["entity_id"]] = l["n"]
    out = []

    def walk(parent, prefix, level):
        for n, node in enumerate(by_parent.get(parent, []), 1):
            node["code"] = f"{prefix}.{n}" if prefix else str(n)
            node["level"] = level
            out.append(node)
            idx = len(out) - 1
            walk(node["id"], node["code"], level + 1)
            kids = by_parent.get(node["id"], [])
            linked = [a for a in acts.get(node["id"], []) if a["status"] != "cancelada"]
            node["activity_count"], node["risk_count"], node["child_count"] = len(linked), risks.get(node["id"], 0), len(kids)
            own_cost = node.get("cost_estimate")
            if kids:
                vals = [k["progress"] for k in kids]
                if linked:
                    vals.append(sum(a["progress"] for a in linked) / len(linked))
                node["progress"] = round(sum(vals) / len(vals)) if vals else 0
                node["leaves"] = sum(k["leaves"] for k in kids)
                node["leaves_with_data"] = sum(k["leaves_with_data"] for k in kids)
                kc = [k["cost_rollup"] for k in kids if k["cost_rollup"] is not None]
                node["cost_rollup"] = round(sum(kc), 2) if kc else own_cost
            else:
                if linked:
                    node["progress"], has = round(sum(a["progress"] for a in linked) / len(linked)), True
                elif node.get("manual_progress") is not None:
                    node["progress"], has = node["manual_progress"], True
                else:
                    node["progress"], has = 0, False
                node["leaves"], node["leaves_with_data"] = 1, 1 if has else 0
                node["cost_rollup"] = own_cost
            _ = idx

    walk(None, "", 1)
    return out


@route("GET", "/api/wbs")
def list_wbs(ctx):
    judge_module_guard(ctx, "eap")
    if not ctx.query.get("season_id"):
        raise bad("Informe a temporada.")
    return {"items": build_tree(ctx.db, ctx.query["season_id"])}


WBS.register(methods=("create", "update"))


@route("GET", "/api/wbs/{id}")
def get_wbs(ctx):
    judge_module_guard(ctx, "eap")
    w = WBS.get(ctx, ctx.params["id"])
    tree = {n["id"]: n for n in build_tree(ctx.db, w["season_id"])}
    w.update({k: tree[w["id"]][k] for k in ("code", "level", "progress", "cost_rollup", "activity_count", "child_count")})
    w["activities"] = ctx.db.q("SELECT id, title, status, progress, planned_start, planned_end, owner_id FROM activities WHERE wbs_id=? AND deleted_at IS NULL", (w["id"],))
    w["risks"] = ctx.db.q("SELECT r.id, r.code, r.title FROM risks r JOIN risk_links l ON l.risk_id=r.id WHERE l.entity_type='wbs' AND l.entity_id=? AND r.deleted_at IS NULL", (w["id"],))
    return w


@route("POST", "/api/wbs/{id}/mover")
def move_wbs(ctx):
    db = ctx.db
    w = WBS.raw(db, ctx.params["id"])
    if not w:
        raise not_found("Elemento da EAP")
    parent = ctx.body.get("parent_id") or None
    if parent:
        p = WBS.raw(db, parent)
        if not p or p["season_id"] != w["season_id"]:
            raise bad("Destino inválido.")
        if is_descendant(db, parent, w["id"]):
            raise bad("Um elemento não pode ser movido para dentro de si mesmo ou de um de seus descendentes.")
    pos = ctx.body.get("position")
    sibs = [r["id"] for r in db.q("SELECT id FROM wbs_elements WHERE season_id=? AND parent_id IS ? AND deleted_at IS NULL AND id<>? ORDER BY sort, name",
                                  (w["season_id"], parent, w["id"]))]
    pos = len(sibs) if not isinstance(pos, int) or isinstance(pos, bool) else max(0, min(pos, len(sibs)))
    sibs.insert(pos, w["id"])
    db.ex("UPDATE wbs_elements SET parent_id=?, updated_at=?, version=version+1 WHERE id=?", (parent, now_iso(), w["id"]))
    for i, sid in enumerate(sibs, 1):
        db.ex("UPDATE wbs_elements SET sort=? WHERE id=?", (i, sid))
    audit(ctx, "mover", "wbs", w["id"], w["name"], before={"parent_id": w["parent_id"], "sort": w["sort"]}, after={"parent_id": parent, "sort": pos + 1})
    return {"items": build_tree(db, w["season_id"])}


@route("DELETE", "/api/wbs/{id}")
def delete_wbs(ctx):
    db = ctx.db
    w = WBS.raw(db, ctx.params["id"])
    if not w:
        raise not_found("Elemento da EAP")
    ids, stack = [], [w["id"]]
    while stack:
        n = stack.pop()
        ids.append(n)
        stack += [r["id"] for r in db.q("SELECT id FROM wbs_elements WHERE parent_id=? AND deleted_at IS NULL", (n,))]
    marks = ",".join("?" * len(ids))
    n_act = db.val(f"SELECT COUNT(*) FROM activities WHERE wbs_id IN ({marks}) AND deleted_at IS NULL", ids)
    if (len(ids) > 1 or n_act) and ctx.query.get("cascade") != "1":
        refs = [r for r in ({"label": "subelemento(s)", "count": len(ids) - 1}, {"label": "atividade(s) vinculada(s)", "count": n_act}) if r["count"]]
        raise conflict("Este elemento possui " + " e ".join(f"{r['count']} {r['label']}" for r in refs) +
                       ". Confirme para excluir toda a ramificação; as atividades serão apenas desvinculadas (não excluídas).",
                       code="vinculos_existentes", references=refs, can_cascade=True, can_archive=False)
    db.ex(f"UPDATE activities SET wbs_id=NULL WHERE wbs_id IN ({marks})", ids)
    db.ex(f"DELETE FROM risk_links WHERE entity_type='wbs' AND entity_id IN ({marks})", ids)
    db.ex(f"UPDATE wbs_elements SET deleted_at=?, version=version+1 WHERE id IN ({marks})", [now_iso()] + ids)
    audit(ctx, "excluir", "wbs", w["id"], f"{w['name']} ({len(ids)} elemento(s); {n_act} atividade(s) desvinculada(s))", before=WBS.ser(w))
    return {"ok": True, "removed": len(ids), "unlinked_activities": n_act}
