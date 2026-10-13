"""Rotinas agendadas no servidor (independentes do navegador): notificações, e-mails, backup diário e limpeza.

Execução: thread em segundo plano iniciada com o servidor (HORIZON_JOBS_INTERVAL_SECONDS) e/ou agendador externo
chamando `python manage.py executar-rotinas` ou POST /api/cron/executar com o segredo HORIZON_CRON_SECRET.
A deduplicação é garantida pelo banco (UNIQUE em notifications.user_id + dedupe_key)."""
import json
import logging
from datetime import datetime, timedelta, timezone

from .config import cfg
from .core import (ApiError, add_days, d, days_between, fmt_date, get_setting, internal_login_ids, notify, route, staff_ids, team_tz, today_local)
from .db import get_db, now_iso
from .mailer import send_mail

log = logging.getLogger("horizon.jobs")


def event_slots(start, created_local, first_days, interval):
    """Datas de alerta de um evento: primeiro alerta (30 dias antes, ou imediatamente se cadastrado depois disso),
    lembretes a cada `interval` dias e aviso final no dia do evento."""
    first = max(add_days(start, -first_days), created_local)
    if first > start:
        return []
    slots, cur = [], first
    while cur < start:
        slots.append(cur)
        cur = add_days(cur, max(1, interval))
    slots.append(start)
    return slots


def _iso_week(day):
    y, w, _ = d(day).isocalendar()
    return f"{y}W{w:02d}"


def generate_notifications(db, now=None):
    now = now or datetime.now(timezone.utc)
    today = today_local(db, now)
    alerts = get_setting(db, "alerts")
    tz = team_tz(db)
    staff = staff_ids(db)
    everyone = internal_login_ids(db)
    can_login = set(everyone)
    made = {"prazo": 0, "atraso": 0, "dependencia": 0, "aprovacao": 0, "evento": 0, "risco": 0, "financeiro": 0}

    def send(kind, users, title, body, et, eid, key):
        for u in dict.fromkeys(u for u in users if u in can_login):
            if notify(db, u, kind, title, body, et, eid, key):
                made[kind] += 1

    seasons = [s["id"] for s in db.q("SELECT id FROM seasons WHERE deleted_at IS NULL AND archived_at IS NULL")]
    from .modules.activities import load_graph, schedule_conflicts
    for sid in seasons:
        acts, deps = load_graph(db, sid)
        parts = {}
        for p in db.q("SELECT p.* FROM activity_participants p JOIN activities a ON a.id=p.activity_id WHERE a.season_id=?", (sid,)):
            parts.setdefault(p["activity_id"], []).append(p["user_id"])
        for a in acts.values():
            if a["status"] in ("concluida", "cancelada") or not a["planned_end"]:
                continue
            people = [u for u in [a["owner_id"]] + parts.get(a["id"], []) if u]
            lead = a["alert_days"] if a["alert_days"] is not None else alerts["delivery_days"]
            left = days_between(today, a["planned_end"])
            if 0 <= left <= lead:
                when = "hoje" if left == 0 else "amanhã" if left == 1 else f"em {left} dias"
                send("prazo", people or staff, f"Entrega vence {when}: {a['title']}", f"Prazo planejado: {fmt_date(a['planned_end'])}.",
                     "activities", a["id"], f"due:{a['id']}:{a['planned_end']}")
            elif left < 0:
                k = (-left - 1) // max(1, alerts["overdue_repeat_days"])
                send("atraso", people + staff, f"Entrega atrasada há {-left} dia(s): {a['title']}",
                     f"Prazo planejado: {fmt_date(a['planned_end'])}. Registre a justificativa e o plano de recuperação."
                     if not a["delay_justification"] else f"Prazo planejado: {fmt_date(a['planned_end'])}. Nova previsão: {fmt_date(a['forecast_end'])}.",
                     "activities", a["id"], f"overdue:{a['id']}:{a['planned_end']}:{k}")
        for c in schedule_conflicts(acts, deps, today):
            s = acts[c["successor_id"]]
            send("dependencia", [s["owner_id"]] + staff, "Dependência pode comprometer o cronograma", c["message"],
                 "activities", s["id"], f"dep:{c['dep_id']}:{c['kind']}:{_iso_week(today)}")
        # aprovações pendentes há mais de N dias
        for a in acts.values():
            if a["approval_status"] in ("aguardando_aprovacao", "reenviada") and a["submitted_at"]:
                sub = datetime.fromisoformat(a["submitted_at"].replace("Z", "+00:00")).astimezone(tz).date().isoformat()
                waiting = days_between(sub, today)
                if waiting >= alerts["approval_reminder_days"]:
                    ap = db.one("SELECT user_id FROM raci WHERE activity_id=? AND role='A'", (a["id"],))
                    k = waiting // max(1, alerts["approval_reminder_days"])
                    send("aprovacao", ([ap["user_id"]] if ap else []) + staff, f"Aprovação pendente há {waiting} dia(s): {a['title']}",
                         "A entrega aguarda decisão do aprovador.", "activities", a["id"], f"apprrem:{a['id']}:{a['submitted_at']}:{k}")
        # eventos
        for e in db.q("SELECT * FROM events WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status='ativo'", (sid,)):
            last_day = e["end_date"] or e["start_date"]
            if today > last_day:
                continue
            created = datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")).astimezone(tz).date().isoformat()
            due = [s for s in event_slots(e["start_date"], created, alerts["event_first_days"], alerts["event_interval_days"]) if s <= today]
            if not due:
                continue
            slot = due[-1]
            left = days_between(today, e["start_date"])
            when = "É hoje" if left == 0 else "Em andamento" if left < 0 else "Falta 1 dia" if left == 1 else f"Faltam {left} dias"
            period = fmt_date(e["start_date"]) + (f" a {fmt_date(e['end_date'])}" if e["end_date"] and e["end_date"] != e["start_date"] else "")
            send("evento", everyone, f"{when}: {e['name']}", f"{period}" + (f" — {e['location']}" if e["location"] else "") + ". Confira o checklist preparatório.",
                 "events", e["id"], f"event:{e['id']}:{e['start_date']}:{slot}")
        # riscos críticos sem plano de ação atualizado
        scale = get_setting(db, "risk_scale")
        top = scale["bands"][-1]
        prev_max = scale["bands"][-2]["max"] if len(scale["bands"]) > 1 else 0
        for r in db.q("SELECT * FROM risks WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL AND status IN ('aberto','em_tratamento','monitorado','ocorrido')", (sid,)):
            if not r["probability"] or not r["impact"] or r["probability"] * r["impact"] <= prev_max:
                continue
            last_action = db.val("SELECT MAX(updated_at) FROM risk_actions WHERE risk_id=?", (r["id"],))
            n_actions = db.val("SELECT COUNT(*) FROM risk_actions WHERE risk_id=?", (r["id"],))
            has_plan = bool((r["mitigation"] or "").strip() or (r["preventive_plan"] or "").strip() or n_actions)
            last = max(x for x in (r["updated_at"], last_action or "") if x)[:10]
            stale = days_between(last, today) >= alerts["risk_stale_days"]
            if not has_plan or stale:
                why = "não possui plano de ação" if not has_plan else f"está sem atualização há {days_between(last, today)} dia(s)"
                send("risco", [r["owner_id"]] + staff, f"Risco {top['name'].lower()} {r['code']}: {r['title']}", f"O risco {why}. Revise o plano de ação.",
                     "risks", r["id"], f"risk:{r['id']}:{'noplan' if not has_plan else 'stale'}:{_iso_week(today)}")
        # financeiro: contas vencidas e recebimentos previstos não confirmados
        for x in db.q("SELECT e.id, e.name, e.due_date, COALESCE(e.total_actual_cents, e.total_planned_cents, 0) total, "
                      "(SELECT COALESCE(SUM(amount_cents),0) FROM movements m WHERE m.origin_type='expense' AND m.origin_id=e.id) paid "
                      "FROM expenses e WHERE e.season_id=? AND e.deleted_at IS NULL AND e.archived_at IS NULL AND e.is_cash=1 "
                      "AND e.status IN ('comprometida','realizada') AND e.due_date IS NOT NULL AND e.due_date < ?", (sid, today)):
            if x["total"] - x["paid"] > 0:
                send("financeiro", staff, f"Despesa vencida sem pagamento confirmado: {x['name']}", f"Vencimento: {fmt_date(x['due_date'])}. Confirme o pagamento ou atualize o lançamento.",
                     "expenses", x["id"], f"expdue:{x['id']}:{x['due_date']}")
        for x in db.q("SELECT i.id, i.description, i.expected_date, i.expected_cents, "
                      "(SELECT COALESCE(SUM(amount_cents),0) FROM movements m WHERE m.origin_type='income' AND m.origin_id=i.id) rec "
                      "FROM incomes i WHERE i.season_id=? AND i.deleted_at IS NULL AND i.archived_at IS NULL AND i.status IN ('prevista','confirmada') "
                      "AND i.expected_date IS NOT NULL AND i.expected_date < ?", (sid, today)):
            if x["expected_cents"] - x["rec"] > 0:
                send("financeiro", staff, f"Recebimento previsto não confirmado: {x['description']}", f"Previsto para {fmt_date(x['expected_date'])}. Confirme o recebimento ou atualize a previsão.",
                     "incomes", x["id"], f"incdue:{x['id']}:{x['expected_date']}")
    return made


def send_pending_emails(db):
    """Envia por e-mail as notificações de quem optou por isso. Sem SMTP configurado, nada é marcado como enviado."""
    if not cfg.smtp_configured:
        return {"enviados": 0, "motivo": "SMTP não configurado"}
    sent = errors = 0
    since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = db.q("SELECT n.*, u.email, u.name FROM notifications n JOIN users u ON u.id=n.user_id "
                "WHERE n.email_status IS NULL AND n.created_at > ? AND u.email IS NOT NULL AND u.active=1 ORDER BY n.created_at LIMIT 100", (since,))
    for n in rows:
        pref = get_setting(db, "notify", n["user_id"])
        if not pref.get("email") or pref.get("kinds", {}).get(n["kind"]) is False:
            db.ex("UPDATE notifications SET email_status='nao_solicitado' WHERE id=?", (n["id"],))
            continue
        link = f"\n\nAcesse: {cfg.PUBLIC_URL}" if cfg.PUBLIC_URL else ""
        ok, err = send_mail(n["email"], f"[Horizon] {n['title']}", f"Olá, {n['name']}.\n\n{n['title']}\n{n['body'] or ''}{link}")
        db.ex("UPDATE notifications SET email_status=? WHERE id=?", ("enviado" if ok else f"erro: {err}"[:200], n["id"]))
        sent += ok
        errors += (not ok)
    return {"enviados": sent, "erros": errors}


def cleanup(db):
    nowi = datetime.now(timezone.utc)
    db.ex("DELETE FROM sessions WHERE expires_at < ?", (nowi.isoformat(),))
    db.ex("DELETE FROM idempotency WHERE created_at < ?", ((nowi - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S"),))
    db.ex("DELETE FROM password_resets WHERE expires_at < ?", ((nowi - timedelta(days=7)).isoformat(),))
    db.ex("DELETE FROM job_runs WHERE id < (SELECT MAX(id) - 500 FROM job_runs)")
    # planilhas enviadas em etapas e nunca usadas em uma importação
    db.ex("DELETE FROM files WHERE entity_type IN ('import_draft', 'backup_part') AND created_at < ?", ((nowi - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),))


def run_all(db=None, now=None, kind="agendada"):
    """Executa todas as rotinas em uma transação e registra a execução em job_runs."""
    db = db or get_db()
    started = now_iso()
    result, ok = {}, 1
    try:
        db.begin()
        result["notificacoes"] = generate_notifications(db, now)
        result["emails"] = send_pending_emails(db)
        cleanup(db)
        db.commit()
    except Exception as exc:
        db.rollback()
        ok, result = 0, {"erro": f"{type(exc).__name__}: {exc}"}
        log.exception("Falha nas rotinas agendadas")
    try:
        from .modules.admin import daily_backup_if_due
        b = daily_backup_if_due(db, now)
        if b:
            result["backup"] = b
    except Exception as exc:
        result["backup_erro"] = str(exc)
        log.exception("Falha no backup automático")
    db.ex("INSERT INTO job_runs (kind, started_at, finished_at, ok, result) VALUES (?,?,?,?,?)",
          (kind, started, now_iso(), ok, json.dumps(result, ensure_ascii=False)))
    return {"ok": bool(ok), **result}


def loop(stop):
    """Laço da thread de rotinas: primeira execução logo após a inicialização e depois a cada intervalo."""
    stop.wait(2)
    while not stop.is_set():
        try:
            run_all(get_db())   # conexão própria desta thread (reaberta se o banco mudar ou a conexão cair)
        except Exception:
            log.exception("Erro no laço de rotinas")
        stop.wait(cfg.JOBS_INTERVAL_SECONDS)
    try:
        get_db().close()
    except Exception:
        pass


def _cron(ctx):
    import hmac
    bearer = (getattr(ctx, "authorization", None) or "")
    bearer = bearer[7:] if bearer.lower().startswith("bearer ") else ""
    secret = bearer or ctx.body.get("secret") or ""
    if not cfg.CRON_SECRET or not hmac.compare_digest(str(secret), cfg.CRON_SECRET):
        raise ApiError(403, "sem_permissao", "Segredo do agendador ausente ou inválido.")
    return run_all(ctx.db, kind="cron-externo")


@route("POST", "/api/cron/executar", public=True, tx=False)
def cron_endpoint(ctx):
    """Para agendadores externos. Exige o segredo: cabeçalho "Authorization: Bearer <segredo>" ou corpo {"secret": ...}."""
    return _cron(ctx)


@route("GET", "/api/cron/executar", public=True, tx=False)
def cron_endpoint_get(ctx):
    """O agendador da Vercel chama por GET e envia "Authorization: Bearer $CRON_SECRET"."""
    return _cron(ctx)


def run_if_stale(db):
    """Sem processo contínuo (Vercel), as rotinas também rodam quando alguém usa a plataforma e a última
    execução é mais antiga que o intervalo configurado. Nunca lança erro para quem fez a requisição."""
    if not (cfg.SERVERLESS and cfg.JOBS_ENABLED) or db.in_tx:
        return False
    try:
        last = db.val("SELECT MAX(started_at) FROM job_runs")
        if last:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds()
            if age < cfg.JOBS_INTERVAL_SECONDS:
                return False
        run_all(db, kind="sob-demanda")
        return True
    except Exception:
        log.exception("Falha nas rotinas sob demanda")
        return False


@route("POST", "/api/admin/rotinas/executar", allow=("admin", "gestor"), tx=False)
def run_now(ctx):
    return run_all(ctx.db, kind="manual")


@route("GET", "/api/admin/rotinas", allow=("admin", "gestor"))
def job_status(ctx):
    rows = ctx.db.q("SELECT * FROM job_runs ORDER BY id DESC LIMIT 20")
    for r in rows:
        r["result"] = json.loads(r["result"]) if r["result"] else None
    return {"enabled": cfg.JOBS_ENABLED, "interval_seconds": cfg.JOBS_INTERVAL_SECONDS, "cron_endpoint": bool(cfg.CRON_SECRET),
            "email": cfg.smtp_configured, "runs": rows}
