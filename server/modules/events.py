"""Eventos e calendário (eventos + prazos de entregas)."""
from ..core import F, Resource, add_days, bad, check_order, judge_module_guard, route, today_local

EVENT_STATUS = ("ativo", "cancelado", "realizado")


class Events(Resource):
    def validate(self, ctx, e, old):
        check_order(e, "start_date", "end_date", "Data de início", "Data de término")


EVENT = Events("events", "events", "Evento", {
    "season_id": F("ref", "Temporada", required=True, ref="seasons"),
    "name": F("text", "Nome", required=True, max=200),
    "type": F("text", "Tipo", max=80, filter=True),
    "description": F("long", "Descrição"),
    "start_date": F("date", "Data de início", required=True),
    "end_date": F("date", "Data de término"),
    "start_time": F("time", "Horário"),
    "location": F("text", "Local", max=200),
    "owner_id": F("ref", "Responsável", ref="users", filter=True),
    "area_id": F("ref", "Área envolvida", ref="areas", filter=True),
    "objectives": F("long", "Objetivos"),
    "checklist": F("json", "Checklist preparatório"),
    "notes": F("long", "Observações"),
    "status": F("enum", "Situação", options=EVENT_STATUS, default="ativo", filter=True),
}, season=True, order="start_date, name", search=("name", "description", "location"))


@route("GET", "/api/events")
def list_events(ctx):
    judge_module_guard(ctx, "calendario")
    return EVENT.list(ctx)


EVENT.register(methods=("get", "create", "update", "delete", "archive"))


@route("GET", "/api/calendar")
def calendar(ctx):
    """Itens do calendário no intervalo: eventos e prazos/marcos de atividades."""
    judge_module_guard(ctx, "calendario")
    db, q = ctx.db, ctx.query
    sid = q.get("season_id")
    today = today_local(db)
    start, end = q.get("from") or add_days(today, -45), q.get("to") or add_days(today, 120)
    if start > end:
        raise bad("Intervalo de datas inválido.")
    items = []
    for e in db.q("SELECT * FROM events WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND start_date<=? AND COALESCE(end_date,start_date)>=? ORDER BY start_date", (sid, end, start)):
        items.append({"kind": "evento", "id": e["id"], "title": e["name"], "start": e["start_date"], "end": e["end_date"] or e["start_date"],
                      "time": e["start_time"], "location": e["location"], "status": e["status"], "area_id": e["area_id"], "type": e["type"]})
    for a in db.q("SELECT id, title, planned_end, status, area_id, is_milestone, owner_id FROM activities WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL "
                  "AND planned_end BETWEEN ? AND ? AND status<>'cancelada' ORDER BY planned_end", (sid, start, end)):
        late = a["status"] != "concluida" and a["planned_end"] < today
        items.append({"kind": "marco" if a["is_milestone"] else "prazo", "id": a["id"], "title": a["title"], "start": a["planned_end"], "end": a["planned_end"],
                      "status": a["status"], "area_id": a["area_id"], "late": late, "owner_id": a["owner_id"]})
    return {"today": today, "from": start, "to": end, "items": items}
