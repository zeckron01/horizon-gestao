"""Pesquisa global: devolve apenas o que o perfil do usuário pode consultar."""
from ..core import INTERNAL, judge_view, route


@route("GET", "/api/search")
def search(ctx):
    db, q, sid = ctx.db, (ctx.query.get("q") or "").strip(), ctx.query.get("season_id")
    if len(q) < 2:
        return {"items": []}
    like = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    jv = judge_view(ctx)
    mods = jv["modules"]
    judge = ctx.role == "juiz"

    def ok(m):
        return not judge or mods.get(m)
    out = []

    def add(kind, label, rows, route_, sub=None):
        for r in rows:
            out.append({"kind": kind, "kind_label": label, "id": r["id"], "title": r["t"], "subtitle": r.get("s") if sub is None else sub, "route": route_})
    if ok("planejamento") or ok("gantt"):
        add("activities", "Atividade", db.q("SELECT id, title t, status s FROM activities WHERE season_id=? AND deleted_at IS NULL AND (title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\') LIMIT 15", (sid, like, like)), "planejamento")
    if ok("eap"):
        add("wbs", "EAP", db.q("SELECT id, name t, NULL s FROM wbs_elements WHERE season_id=? AND deleted_at IS NULL AND name LIKE ? ESCAPE '\\' LIMIT 10", (sid, like)), "eap")
    if ok("riscos"):
        add("risks", "Risco", db.q("SELECT id, code || ' — ' || title t, status s FROM risks WHERE season_id=? AND deleted_at IS NULL AND (title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' OR code LIKE ? ESCAPE '\\') LIMIT 10", (sid, like, like, like)), "riscos")
    if ok("calendario"):
        add("events", "Evento", db.q("SELECT id, name t, start_date s FROM events WHERE season_id=? AND deleted_at IS NULL AND (name LIKE ? ESCAPE '\\' OR location LIKE ? ESCAPE '\\') LIMIT 10", (sid, like, like)), "calendario")
    if ok("stakeholders"):
        add("stakeholders", "Stakeholder", db.q("SELECT id, name t, status s FROM stakeholders WHERE season_id=? AND deleted_at IS NULL AND (name LIKE ? ESCAPE '\\' OR purpose LIKE ? ESCAPE '\\') LIMIT 10", (sid, like, like)), "stakeholders")
    add("users", "Pessoa", db.q("SELECT id, name t, position s FROM users WHERE deleted_at IS NULL AND active=1 AND role<>'juiz' AND name LIKE ? ESCAPE '\\' LIMIT 10", (like,)), "usuarios" if ctx.role in ("admin", "gestor") else "raci")
    if not judge or (mods.get("financeiro") and jv["finance_detail"]):
        add("expenses", "Despesa", db.q("SELECT id, name t, supplier s FROM expenses WHERE season_id=? AND deleted_at IS NULL AND (name LIKE ? ESCAPE '\\' OR supplier LIKE ? ESCAPE '\\') LIMIT 10", (sid, like, like)), "financeiro")
    cond = " AND judge_visible=1" if judge else ""
    for f in db.q(f"SELECT id, name, entity_type, entity_id FROM files WHERE deleted_at IS NULL AND entity_type NOT IN ('branding','areas','import') AND name LIKE ? ESCAPE '\\'{cond} LIMIT 10", (like,)):
        from .comments_files import JUDGE_MODULE
        if judge and not mods.get(JUDGE_MODULE.get(f["entity_type"], "")):
            continue
        out.append({"kind": "files", "kind_label": "Documento", "id": f["id"], "title": f["name"], "subtitle": None, "route": "arquivo", "entity_type": f["entity_type"], "entity_id": f["entity_id"]})
    return {"items": out}
