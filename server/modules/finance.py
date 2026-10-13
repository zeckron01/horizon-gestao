"""Orçamento e fluxo de caixa.

Princípios (ver docs/REGRAS.md):
- Planejado, realizado e pago são grandezas separadas. "Pago" e "recebido" vêm exclusivamente dos movimentos de caixa.
- Itens recebidos por patrocínio/doação (is_cash = 0) registram valor estimado do apoio, mas nunca geram saída de caixa.
- Patrocínio prometido/confirmado não é dinheiro em caixa: só entra no saldo quando há movimento de recebimento.
- Movimentos confirmados não são apagados: corrige-se por estorno rastreável.
"""
from ..core import (F, STAFF, Resource, audit, bad, check_order, conflict, forbidden, from_cents, get_setting, judge_view,
                    not_found, route, to_cents, today_local, user_names)
from ..db import new_id, now_iso

EXP_STATUS = ("planejada", "solicitada", "comprometida", "realizada", "cancelada")
EXP_STATUS_LABEL = {"planejada": "Planejada", "solicitada": "Solicitada", "comprometida": "Comprometida", "realizada": "Realizada", "cancelada": "Cancelada"}
INC_STATUS = ("prevista", "confirmada", "cancelada")
INC_STATUS_LABEL = {"prevista": "Prevista", "confirmada": "Confirmada (a receber)", "cancelada": "Cancelada"}


def acquisition_types(db):
    return {t["key"]: t for t in get_setting(db, "catalogs")["acquisition_types"]}


def _guard(ctx, detail=True):
    if ctx.role == "juiz":
        jv = judge_view(ctx)
        if not jv["modules"].get("financeiro") or not (jv["finance_detail"] if detail else (jv["finance_summary"] or jv["finance_detail"])):
            raise forbidden("As informações financeiras detalhadas não estão liberadas para o perfil de juiz.")


def paid_map(db, origin_type, ids):
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["origin_id"]: r for r in db.q(
        f"SELECT origin_id, COALESCE(SUM(amount_cents),0) total, MAX(CASE WHEN amount_cents>0 THEN date END) last_date, COUNT(*) n "
        f"FROM movements WHERE origin_type=? AND origin_id IN ({marks}) GROUP BY origin_id", [origin_type] + ids)}


def _mul(qty, unit):
    return None if qty is None or unit is None else int(round(qty * unit))


class Expenses(Resource):
    def validate(self, ctx, e, old):
        db = ctx.db
        types = acquisition_types(db)
        if e.get("acquisition_type") not in types:
            raise bad("Tipo de aquisição inválido.", {"acquisition_type": "Opção inválida."})
        e["is_cash"] = 1 if types[e["acquisition_type"]]["is_cash"] else 0
        check_order(e, "request_date", "purchase_date", "Data da solicitação", "Data da compra")
        for kind, lab in (("planned", "planejado"), ("actual", "real")):
            calc = _mul(e.get("quantity"), e.get(f"unit_{kind}_cents"))
            tot = e.get(f"total_{kind}_cents")
            sent = f"total_{kind}" in ctx.body and ctx.body[f"total_{kind}"] not in (None, "")
            changed_inputs = old is None or any(e.get(k) != old.get(k) for k in ("quantity", f"unit_{kind}_cents"))
            if calc is not None and (tot is None or (changed_inputs and not sent)):
                e[f"total_{kind}_cents"] = calc
            elif calc is not None and tot is not None and sent and abs(calc - tot) > 1:
                raise bad(f"Valor total {lab} ({from_cents(tot):.2f}) diferente de quantidade × valor unitário ({from_cents(calc):.2f}). Corrija um dos campos.",
                          {f"total_{kind}": "Diferente de quantidade × valor unitário."}, code="total_inconsistente")
        if e["status"] == "realizada" and e.get("total_actual_cents") is None:
            raise bad("Informe o valor realizado para marcar a despesa como realizada.", {"total_actual": "Obrigatório para despesas realizadas."})
        if old:
            paid = db.val("SELECT COALESCE(SUM(amount_cents),0) FROM movements WHERE origin_type='expense' AND origin_id=?", (old["id"],))
            if paid:
                if not e["is_cash"]:
                    raise bad("Esta despesa possui pagamentos registrados; estorne-os antes de mudar para um tipo sem saída de caixa.")
                if e["status"] == "cancelada":
                    raise bad("Esta despesa possui pagamentos confirmados. Estorne os pagamentos antes de cancelá-la.", code="possui_pagamentos")
                total = e.get("total_actual_cents") if e.get("total_actual_cents") is not None else e.get("total_planned_cents") or 0
                if total < paid:
                    raise bad("O valor total não pode ficar abaixo do que já foi pago. Estorne o pagamento excedente primeiro.")

    def before_delete(self, ctx, e):
        if ctx.db.val("SELECT COUNT(*) FROM movements WHERE origin_type='expense' AND origin_id=?", (e["id"],)):
            raise conflict("Esta despesa possui movimentos de caixa. Para preservar o histórico, estorne os pagamentos e cancele a despesa em vez de excluí-la.",
                           code="possui_movimentos", can_archive=True)

    def expand(self, ctx, rows):
        pm = paid_map(ctx.db, "expense", [r["id"] for r in rows])
        for r in rows:
            p = pm.get(r["id"], {"total": 0, "last_date": None, "n": 0})
            total_c = to_cents(r["total_actual"]) if r.get("total_actual") is not None else to_cents(r.get("total_planned")) or 0
            r["is_cash"] = bool(r["is_cash"])
            r["paid"] = from_cents(p["total"])
            r["payment_date"] = p["last_date"]
            r["movement_count"] = p["n"]
            cash_due = r["is_cash"] and r["status"] != "cancelada"
            r["pending"] = from_cents(max(0, total_c - p["total"])) if cash_due else 0.0
            r["payment_state"] = ("nao_se_aplica" if not r["is_cash"] else "paga" if total_c > 0 and p["total"] >= total_c else
                                  "parcial" if p["total"] > 0 else "nao_paga")
        return rows


EXP = Expenses("expenses", "expenses", "Despesa", {
    "season_id": F("ref", "Temporada", required=True, ref="seasons"),
    "name": F("text", "Item ou serviço", required=True, max=200),
    "description": F("long", "Descrição detalhada"),
    "category": F("text", "Categoria", max=80, filter=True),
    "area_id": F("ref", "Área beneficiada", ref="areas", filter=True),
    "responsible_id": F("ref", "Responsável pelo lançamento", ref="users"),
    "request_date": F("date", "Data da solicitação"),
    "purchase_date": F("date", "Data da compra"),
    "due_date": F("date", "Data de vencimento"),
    "supplier": F("text", "Fornecedor", max=160),
    "funding_source": F("text", "Fonte do recurso", max=120),
    "payment_method": F("text", "Método de pagamento", max=60),
    "quantity": F("real", "Quantidade", min=0),
    "unit": F("text", "Unidade de medida", max=40),
    "unit_planned": F("money", "Valor unitário planejado"),
    "unit_actual": F("money", "Valor unitário real"),
    "total_planned": F("money", "Valor total planejado"),
    "total_actual": F("money", "Valor total realizado"),
    "acquisition_type": F("text", "Tipo de aquisição", default="proprio", filter=True),
    "stakeholder_id": F("ref", "Patrocinador relacionado", ref="stakeholders", filter=True),
    "status": F("enum", "Situação", options=EXP_STATUS, default="planejada", filter=True),
    "notes": F("long", "Observações"),
}, season=True, order="COALESCE(purchase_date, request_date, created_at) DESC, name", search=("name", "description", "supplier"))


class Incomes(Resource):
    def validate(self, ctx, i, old):
        if old and (old.get("import_source") or "").startswith("stakeholder:auto") and not getattr(ctx, "_sync", False):
            if i["expected_cents"] != old["expected_cents"] or i["stakeholder_id"] != old["stakeholder_id"]:
                raise bad("Esta receita é gerada a partir do valor confirmado do stakeholder. Altere o valor no cadastro do stakeholder.")
        if old and i["status"] == "cancelada":
            if ctx.db.val("SELECT COALESCE(SUM(amount_cents),0) FROM movements WHERE origin_type='income' AND origin_id=?", (old["id"],)):
                raise bad("Esta receita possui recebimentos confirmados. Estorne-os antes de cancelá-la.", code="possui_recebimentos")

    def before_delete(self, ctx, i):
        if ctx.db.val("SELECT COUNT(*) FROM movements WHERE origin_type='income' AND origin_id=?", (i["id"],)):
            raise conflict("Esta receita possui movimentos de caixa. Estorne os recebimentos e cancele a receita em vez de excluí-la.",
                           code="possui_movimentos", can_archive=True)

    def expand(self, ctx, rows):
        pm = paid_map(ctx.db, "income", [r["id"] for r in rows])
        for r in rows:
            p = pm.get(r["id"], {"total": 0, "last_date": None, "n": 0})
            exp_c = to_cents(r["expected"]) or 0
            r["received"] = from_cents(p["total"])
            r["received_date"] = p["last_date"]
            r["movement_count"] = p["n"]
            r["to_receive"] = from_cents(max(0, exp_c - p["total"])) if r["status"] != "cancelada" else 0.0
            r["receipt_state"] = "recebida" if exp_c > 0 and p["total"] >= exp_c else "parcial" if p["total"] > 0 else "nao_recebida"
            r["auto"] = (r.get("import_source") or "").startswith("stakeholder:auto")
        return rows


INC = Incomes("incomes", "incomes", "Receita", {
    "season_id": F("ref", "Temporada", required=True, ref="seasons"),
    "description": F("text", "Descrição", required=True, max=200),
    "purpose": F("long", "Finalidade", max=2000),
    "source": F("text", "Fonte do recurso", max=120),
    "stakeholder_id": F("ref", "Patrocinador", ref="stakeholders", filter=True),
    "area_id": F("ref", "Área beneficiada", ref="areas", filter=True),
    "expected_date": F("date", "Data prevista"),
    "expected": F("money", "Valor previsto", required=True),
    "method": F("text", "Método de recebimento", max=60),
    "status": F("enum", "Situação", options=INC_STATUS, default="prevista", filter=True),
    "notes": F("long", "Observações"),
}, season=True, order="COALESCE(expected_date, created_at) DESC", search=("description", "purpose", "source"))


def _detail_get(res):
    def h(ctx):
        _guard(ctx)
        r = res.get(ctx, ctx.params["id"])
        names = user_names(ctx.db)
        r["movements"] = [ser_mov(m, names) for m in ctx.db.q("SELECT * FROM movements WHERE origin_type=? AND origin_id=? ORDER BY date, created_at",
                                                              ("expense" if res is EXP else "income", r["id"]))]
        return r
    return h


def _list(res):
    def h(ctx):
        _guard(ctx)
        return res.list(ctx)
    return h


route("GET", "/api/expenses")(_list(EXP))
route("GET", "/api/incomes")(_list(INC))
route("GET", "/api/expenses/{id}")(_detail_get(EXP))
route("GET", "/api/incomes/{id}")(_detail_get(INC))
EXP.register(methods=("create", "update", "delete", "archive"))
INC.register(methods=("create", "update", "delete", "archive"))


def ser_mov(m, names=None):
    out = dict(m)
    out["amount"] = from_cents(out.pop("amount_cents"))
    if names is not None:
        out["created_by_name"] = names.get(m["created_by"])
    return out


def _mov_input(ctx, max_cents=None, what="pagamento"):
    db = ctx.db
    amount = to_cents(ctx.body.get("amount"), "Valor")
    if not amount or amount <= 0:
        raise bad(f"Informe um valor de {what} maior que zero.", {"amount": "Valor inválido."})
    date_ = F("date", "Data", required=True).parse(db, "date", ctx.body.get("date"))
    if date_ > today_local(db):
        raise bad(f"A data do {what} não pode estar no futuro. Lançamentos futuros são previsões, não movimentos de caixa.", {"date": "Data futura."})
    if max_cents is not None and amount > max_cents:
        raise bad(f"O valor informado ({from_cents(amount):.2f}) é maior que o saldo pendente ({from_cents(max_cents):.2f}).", {"amount": "Acima do valor pendente."})
    return amount, date_, F("text", "Método", max=60).parse(db, "method", ctx.body.get("method")), F("long", "Observação", max=1000).parse(db, "note", ctx.body.get("note"))


@route("POST", "/api/expenses/{id}/pagamentos")
def pay_expense(ctx):
    db = ctx.db
    e = EXP.raw(db, ctx.params["id"])
    if not e:
        raise not_found("Despesa")
    if not e["is_cash"]:
        raise bad("Itens recebidos por patrocínio ou doação não geram saída de caixa; não é possível registrar pagamento.", code="sem_saida_de_caixa")
    if e["status"] in ("planejada", "cancelada"):
        raise bad("Só é possível pagar despesas solicitadas, comprometidas ou realizadas. Atualize a situação da despesa.", code="situacao_invalida")
    total = e["total_actual_cents"] if e["total_actual_cents"] is not None else e["total_planned_cents"]
    if not total:
        raise bad("Informe o valor total da despesa antes de registrar o pagamento.")
    paid = db.val("SELECT COALESCE(SUM(amount_cents),0) FROM movements WHERE origin_type='expense' AND origin_id=?", (e["id"],))
    amount, date_, method, note = _mov_input(ctx, total - paid)
    m = {"id": new_id(), "season_id": e["season_id"], "date": date_, "direction": "saida", "amount_cents": amount, "origin_type": "expense",
         "origin_id": e["id"], "method": method or e["payment_method"], "note": note, "reverses_id": None, "created_by": ctx.uid, "created_at": now_iso()}
    db.insert("movements", m)
    audit(ctx, "pagamento", "expenses", e["id"], f"{e['name']} — pagamento de R$ {from_cents(amount):.2f}", after=ser_mov(m))
    return _detail_get(EXP)(ctx)


@route("POST", "/api/incomes/{id}/recebimentos")
def receive_income(ctx):
    db = ctx.db
    i = INC.raw(db, ctx.params["id"])
    if not i:
        raise not_found("Receita")
    if i["status"] == "cancelada":
        raise bad("Receita cancelada não pode receber valores.", code="situacao_invalida")
    amount, date_, method, note = _mov_input(ctx, None, "recebimento")
    m = {"id": new_id(), "season_id": i["season_id"], "date": date_, "direction": "entrada", "amount_cents": amount, "origin_type": "income",
         "origin_id": i["id"], "method": method or i["method"], "note": note, "reverses_id": None, "created_by": ctx.uid, "created_at": now_iso()}
    db.insert("movements", m)
    if i["status"] == "prevista":
        db.ex("UPDATE incomes SET status='confirmada', updated_at=?, version=version+1 WHERE id=?", (now_iso(), i["id"]))
    audit(ctx, "recebimento", "incomes", i["id"], f"{i['description']} — recebimento de R$ {from_cents(amount):.2f}", after=ser_mov(m))
    return _detail_get(INC)(ctx)


@route("POST", "/api/movements/{id}/estornar")
def reverse_movement(ctx):
    db = ctx.db
    m = db.one("SELECT * FROM movements WHERE id=?", (ctx.params["id"],))
    if not m:
        raise not_found("Movimento")
    if m["reverses_id"]:
        raise bad("Um estorno não pode ser estornado. Registre um novo lançamento.")
    if db.one("SELECT 1 FROM movements WHERE reverses_id=?", (m["id"],)):
        raise conflict("Este movimento já foi estornado.")
    reason = str(ctx.body.get("reason") or "").strip()
    if not reason:
        raise bad("Informe o motivo do estorno.", {"reason": "Campo obrigatório."})
    r = {"id": new_id(), "season_id": m["season_id"], "date": today_local(db), "direction": m["direction"], "amount_cents": -m["amount_cents"],
         "origin_type": m["origin_type"], "origin_id": m["origin_id"], "method": m["method"], "note": f"Estorno: {reason}"[:1000],
         "reverses_id": m["id"], "created_by": ctx.uid, "created_at": now_iso()}
    db.insert("movements", r)
    audit(ctx, "estorno", "expenses" if m["origin_type"] == "expense" else "incomes", m["origin_id"],
          f"Estorno de R$ {from_cents(m['amount_cents']):.2f} — {reason}", before=ser_mov(m), after=ser_mov(r))
    return {"ok": True, "movement": ser_mov(r)}


@route("GET", "/api/movements")
def list_movements(ctx):
    _guard(ctx)
    names = user_names(ctx.db)
    rows = ctx.db.q("SELECT m.*, COALESCE(e.name, i.description) origin_name, (SELECT id FROM movements r WHERE r.reverses_id=m.id) reversed_by "
                    "FROM movements m LEFT JOIN expenses e ON m.origin_type='expense' AND e.id=m.origin_id "
                    "LEFT JOIN incomes i ON m.origin_type='income' AND i.id=m.origin_id WHERE m.season_id=? ORDER BY m.date DESC, m.created_at DESC",
                    (ctx.query.get("season_id"),))
    return {"items": [ser_mov(m, names) for m in rows]}


# ----------------------------------------------------------------------------- consolidação
def _month(s):
    return s[:7] if s else None


def month_range(a, b):
    out, (y, m) = [], (int(a[:4]), int(a[5:7]))
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def finance_summary(db, season_id, date_from=None, date_to=None, area_id=None):
    """Todos os números do módulo financeiro, em centavos convertidos para reais na saída."""
    season = db.one("SELECT * FROM seasons WHERE id=?", (season_id,))
    if not season:
        raise not_found("Temporada")
    today = today_local(db)
    exps = db.q("SELECT * FROM expenses WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (season_id,))
    incs = db.q("SELECT * FROM incomes WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (season_id,))
    if area_id:
        exps = [e for e in exps if e["area_id"] == area_id]
        incs = [i for i in incs if i["area_id"] == area_id]
    eid, iid = {e["id"]: e for e in exps}, {i["id"]: i for i in incs}
    movs = [m for m in db.q("SELECT * FROM movements WHERE season_id=? ORDER BY date, created_at", (season_id,))
            if (m["origin_type"] == "expense" and m["origin_id"] in eid) or (m["origin_type"] == "income" and m["origin_id"] in iid)]
    paid_by, rec_by = {}, {}
    for m in movs:
        tgt = paid_by if m["origin_type"] == "expense" else rec_by
        tgt[m["origin_id"]] = tgt.get(m["origin_id"], 0) + m["amount_cents"]

    def in_period(day):
        return (not date_from or (day and day >= date_from)) and (not date_to or (day and day <= date_to))

    def etotal(e):
        return e["total_actual_cents"] if e["total_actual_cents"] is not None else (e["total_planned_cents"] or 0)

    live = [e for e in exps if e["status"] != "cancelada"]
    cash = [e for e in live if e["is_cash"]]
    kind_items = [e for e in live if not e["is_cash"]]
    ref_date = lambda e: e["purchase_date"] or e["due_date"] or e["request_date"]  # noqa: E731
    cash_p = [e for e in cash if not (date_from or date_to) or in_period(ref_date(e))]
    planned_items = sum(e["total_planned_cents"] or 0 for e in cash_p)
    committed = sum(etotal(e) for e in cash_p if e["status"] in ("comprometida", "realizada"))
    realized = sum(e["total_actual_cents"] or 0 for e in cash_p if e["status"] == "realizada")
    realized_planned = sum(e["total_planned_cents"] or 0 for e in cash_p if e["status"] == "realizada")
    movs_p = [m for m in movs if in_period(m["date"])]
    paid = sum(m["amount_cents"] for m in movs_p if m["direction"] == "saida")
    received = sum(m["amount_cents"] for m in movs_p if m["direction"] == "entrada")
    payable = sum(max(0, etotal(e) - paid_by.get(e["id"], 0)) for e in cash if e["status"] in ("comprometida", "realizada"))
    live_inc = [i for i in incs if i["status"] != "cancelada"]
    confirmed = sum(i["expected_cents"] for i in live_inc if i["status"] == "confirmada")
    receivable = sum(max(0, i["expected_cents"] - rec_by.get(i["id"], 0)) for i in live_inc if i["status"] == "confirmada")
    forecast_only = sum(max(0, i["expected_cents"] - rec_by.get(i["id"], 0)) for i in live_inc if i["status"] == "prevista")
    in_kind = sum(etotal(e) for e in kind_items)
    in_kind_unvalued = sum(1 for e in kind_items if not etotal(e))
    budget = season["budget_planned_cents"] if not area_id else planned_items
    goal = season["fundraising_goal_cents"]
    total_paid_all = sum(m["amount_cents"] for m in movs if m["direction"] == "saida")
    total_rec_all = sum(m["amount_cents"] for m in movs if m["direction"] == "entrada")
    opening = season["opening_balance_cents"] if not area_id else 0

    def group(items, keyf, valf):
        g = {}
        for x in items:
            k = keyf(x) or "(não informado)"
            g[k] = g.get(k, 0) + valf(x)
        return [{"key": k, "value": from_cents(v)} for k, v in sorted(g.items(), key=lambda kv: -kv[1]) if v]

    areas = {a["id"]: a["name"] for a in db.q("SELECT id, name FROM areas")}
    types = acquisition_types(db)
    by_area = {}
    for e in live:
        k = areas.get(e["area_id"], "(sem área)")
        row = by_area.setdefault(k, {"key": k, "area_id": e["area_id"], "planned": 0, "realized": 0, "paid": 0, "in_kind": 0})
        if e["is_cash"]:
            row["planned"] += e["total_planned_cents"] or 0
            row["realized"] += (e["total_actual_cents"] or 0) if e["status"] == "realizada" else 0
            row["paid"] += paid_by.get(e["id"], 0)
        else:
            row["in_kind"] += etotal(e)
    by_area = [{**r, **{k: from_cents(r[k]) for k in ("planned", "realized", "paid", "in_kind")}} for r in by_area.values()]

    # fluxo de caixa mensal (somente movimentos efetivos) + projeções separadas
    dates = [m["date"] for m in movs] + [e["due_date"] or e["purchase_date"] for e in cash] \
        + [i["expected_date"] for i in live_inc] + [season["start_date"], today]
    dates = [x for x in dates if x]
    months = month_range(min(dates)[:7], max(max(dates)[:7], today[:7])) if dates else []
    if len(months) > 60:
        months = months[-60:]
    flow, bal = [], opening
    first_m = months[0] if months else None
    bal += sum((m["amount_cents"] if m["direction"] == "entrada" else -m["amount_cents"]) for m in movs if first_m and m["date"][:7] < first_m)
    for mo in months:
        ins = sum(m["amount_cents"] for m in movs if m["direction"] == "entrada" and m["date"][:7] == mo)
        outs = sum(m["amount_cents"] for m in movs if m["direction"] == "saida" and m["date"][:7] == mo)
        exp_in = sum(max(0, i["expected_cents"] - rec_by.get(i["id"], 0)) for i in live_inc if _month(i["expected_date"]) == mo)
        exp_out = sum(max(0, etotal(e) - paid_by.get(e["id"], 0)) for e in cash if _month(e["due_date"] or e["purchase_date"]) == mo)
        pay = sum(max(0, etotal(e) - paid_by.get(e["id"], 0)) for e in cash if e["status"] in ("comprometida", "realizada") and _month(e["due_date"] or e["purchase_date"]) == mo)
        recv = sum(max(0, i["expected_cents"] - rec_by.get(i["id"], 0)) for i in live_inc if i["status"] == "confirmada" and _month(i["expected_date"]) == mo)
        row = {"month": mo, "opening": bal, "inflow": ins, "outflow": outs, "net": ins - outs, "closing": bal + ins - outs,
               "expected_income": exp_in, "expected_expense": exp_out, "payable": pay, "receivable": recv, "is_future": mo > today[:7]}
        bal = row["closing"]
        flow.append({k: (from_cents(v) if isinstance(v, int) and k != "month" and not isinstance(v, bool) else v) for k, v in row.items()})
    undated_out = sum(max(0, etotal(e) - paid_by.get(e["id"], 0)) for e in cash if not (e["due_date"] or e["purchase_date"]))
    undated_in = sum(max(0, i["expected_cents"] - rec_by.get(i["id"], 0)) for i in live_inc if not i["expected_date"])

    sponsors = {s["id"]: s["name"] for s in db.q("SELECT id, name FROM stakeholders WHERE season_id=?", (season_id,))}
    rec_by_sponsor = {}
    for m in movs_p:
        if m["direction"] == "entrada":
            k = sponsors.get(iid[m["origin_id"]]["stakeholder_id"], iid[m["origin_id"]]["source"] or "Outras fontes")
            rec_by_sponsor[k] = rec_by_sponsor.get(k, 0) + m["amount_cents"]
    c = from_cents
    return {
        "season_id": season_id, "today": today, "period": {"from": date_from, "to": date_to}, "area_id": area_id,
        "budget": {
            "planned_total": c(budget), "planned_items": c(planned_items), "committed": c(committed), "realized": c(realized), "paid": c(paid),
            "payable": c(payable), "remaining": c(budget - committed), "deviation": c(realized - realized_planned),
            "execution_pct": round(realized * 100 / budget, 1) if budget else None,
            "in_kind_value": c(in_kind), "in_kind_unvalued": in_kind_unvalued,
        },
        "funding": {
            "goal": c(goal), "received": c(received), "confirmed": c(confirmed), "receivable": c(receivable), "forecast": c(forecast_only),
            "confirmed_pct": round(confirmed * 100 / goal, 1) if goal else None,
            "received_pct": round(total_rec_all * 100 / goal, 1) if goal else None,
        },
        "cash": {"opening": c(opening), "received_total": c(total_rec_all), "paid_total": c(total_paid_all), "balance": c(opening + total_rec_all - total_paid_all)},
        "cashflow": flow, "undated": {"expected_expense": c(undated_out), "expected_income": c(undated_in)},
        "by_category": group(cash_p, lambda e: e["category"], etotal),
        "by_area": by_area,
        "by_supplier": group(cash_p, lambda e: e["supplier"], etotal),
        "by_source": group(cash_p, lambda e: e["funding_source"], etotal),
        "by_acquisition": group(live, lambda e: types.get(e["acquisition_type"], {}).get("label", e["acquisition_type"]), etotal),
        "received_by_sponsor": [{"key": k, "value": c(v)} for k, v in sorted(rec_by_sponsor.items(), key=lambda kv: -kv[1]) if v],
        "counts": {"expenses": len(live), "incomes": len(live_inc), "movements": len(movs)},
        "formulas": {
            "Fluxo de caixa líquido": "entradas efetivamente recebidas − saídas efetivamente pagas",
            "Saldo final": "saldo inicial + fluxo de caixa líquido (o saldo inicial de cada mês é o saldo final do mês anterior)",
            "Comprometido": "soma das despesas em dinheiro comprometidas ou realizadas (valor real, ou o planejado quando ainda não há valor real)",
            "Realizado": "soma do valor real das despesas em dinheiro com situação Realizada",
            "Pago": "soma dos movimentos de saída de caixa (pagamentos menos estornos)",
            "A pagar": "valor das despesas comprometidas/realizadas ainda não pago",
            "Orçamento restante": "orçamento total planejado − comprometido",
            "Desvio": "valor realizado − valor planejado das despesas já realizadas",
            "Execução do orçamento": "realizado ÷ orçamento total planejado",
            "Captado ainda não recebido": "receitas confirmadas − recebimentos registrados",
            "Apoio não financeiro": "valor estimado dos itens e serviços recebidos por patrocínio/doação (não entra no caixa)",
        },
    }


@route("GET", "/api/finance/resumo")
def finance_resumo(ctx):
    _guard(ctx, detail=False)
    q = ctx.query
    if not q.get("season_id"):
        raise bad("Informe a temporada.")
    return finance_summary(ctx.db, q["season_id"], q.get("from") or None, q.get("to") or None, q.get("area_id") or None)
