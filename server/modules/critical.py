"""Documento de Gestão — Caminho Crítico do Projeto.

Junta o planejamento real da temporada (atividades, datas, durações e dependências do Gantt/planejamento), a matriz de
riscos e a matriz IMPACTO para:
- calcular o caminho crítico pelo CPM (avanço e retrocesso; ES, EF, LS, LF, folga total e livre) — server/cpm.py;
- prever a conclusão considerando o que já foi realizado (datas reais, progresso e a data de hoje);
- emitir alertas (atividades críticas próximas do prazo ou atrasadas, folga reduzida, riscos críticos em atividades
  críticas, previsão além da data-limite, informações insuficientes);
- relacionar riscos às atividades e estimar o efeito de cada risco na data final;
- simular cenários (atraso sem mitigação, com mitigação, solução alternativa) SEM alterar o planejamento oficial.
  Um cenário só muda datas, durações, atividades e dependências oficiais quando um gestor/administrador o aprova
  (o estado anterior fica guardado no próprio cenário e no histórico).

Nada aqui inventa dados: atividade sem duração ou sem datas suficientes aparece como pendência a completar.
"""
import json
import math
import unicodedata
from datetime import date

from ..cpm import DEP_TYPES, Calendar, CycleError, compute, edge_slack
from ..core import (INTERNAL, STAFF, audit, bad, conflict, forbidden, get_setting, judge_module_guard, not_found, route,
                    today_local, user_names)
from ..db import new_id, now_iso
from ..i18n import _t
from .activities import ACT, lateness
from .risks import RISK, expand_risks

IN_PROGRESS = ("em_andamento", "em_revisao", "aguardando_aprovacao", "bloqueada")
DEFAULT_CFG = {
    "calendar": "corridos",          # "corridos" (todos os dias) ou "uteis" (dias de trabalho, sem feriados)
    "workdays": [0, 1, 2, 3, 4],     # 0 = segunda … 6 = domingo (usado só em dias úteis)
    "holidays": [],                  # AAAA-MM-DD
    "project_start": None,           # vazio = início da temporada ou primeira data planejada
    "deadline": None,                # data-limite; vazio = fim da temporada
    "respect_planned_start": True,   # a data de início planejada vale como "não iniciar antes de"
    "near_days": 7,                  # alerta de prazo próximo para atividades críticas
    "low_float": 3,                  # folga considerada reduzida
}
SCENARIO_KINDS = ("atraso_sem_mitigacao", "mitigacao", "alternativa", "personalizado")
CHANGE_KINDS = ("extra_days", "set_duration", "set_start", "new_activity", "add_dependency", "remove_dependency", "cost")
MAX_CHANGES = 120


def _norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _iso(x):
    return x.isoformat() if isinstance(x, date) else x


# ----------------------------------------------------------------------------- configuração
def season_or_400(db, sid):
    if not sid:
        raise bad(_t("Informe a temporada."))
    s = db.one("SELECT * FROM seasons WHERE id=? AND deleted_at IS NULL", (sid,))
    if not s:
        raise not_found(_t("Temporada"))
    return s


def load_config(db, sid):
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    row = db.one("SELECT data, updated_at, updated_by FROM cpm_config WHERE season_id=?", (sid,))
    if row:
        try:
            cfg.update({k: v for k, v in json.loads(row["data"]).items() if k in DEFAULT_CFG})
        except ValueError:
            pass
        cfg["updated_at"], cfg["updated_by"] = row["updated_at"], row["updated_by"]
    return cfg


def _valid_date(v, label):
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise bad(_t("{0}: data inválida.", label))


def clean_config(body):
    out = {}
    cal = body.get("calendar", "corridos")
    if cal not in ("corridos", "uteis"):
        raise bad(_t("Calendário inválido (use dias corridos ou dias úteis)."))
    out["calendar"] = cal
    wd = body.get("workdays", DEFAULT_CFG["workdays"])
    if not isinstance(wd, list) or not wd or any(not isinstance(x, int) or not 0 <= x <= 6 for x in wd):
        raise bad(_t("Dias de trabalho inválidos: escolha ao menos um dia da semana."))
    out["workdays"] = sorted(set(wd))
    hol = body.get("holidays") or []
    if not isinstance(hol, list) or len(hol) > 400:
        raise bad(_t("Lista de feriados inválida."))
    out["holidays"] = sorted({_valid_date(h, _t("Feriado")) for h in hol if h})
    out["project_start"] = _valid_date(body.get("project_start"), _t("Início do projeto"))
    out["deadline"] = _valid_date(body.get("deadline"), _t("Data-limite"))
    if out["project_start"] and out["deadline"] and out["deadline"] < out["project_start"]:
        raise bad(_t("A data-limite não pode ser anterior ao início do projeto."))
    out["respect_planned_start"] = bool(body.get("respect_planned_start", True))
    for k, lab, hi in (("near_days", _t("Antecedência do alerta de prazo"), 90), ("low_float", _t("Folga reduzida"), 90)):
        v = body.get(k, DEFAULT_CFG[k])
        if not isinstance(v, int) or not 0 <= v <= hi:
            raise bad(_t("{0}: informe um número inteiro de 0 a {1}.", lab, hi))
        out[k] = v
    return out


# ----------------------------------------------------------------------------- rede (dados reais da temporada)
class Network:
    """Atividades e dependências da temporada convertidas em nós/ligações do CPM, com o calendário configurado."""

    def __init__(self, db, season, cfg, today=None):
        self.db, self.season, self.cfg = db, season, cfg
        self.today = today or today_local(db)
        sid = season["id"]
        rows = db.q("SELECT * FROM activities WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (sid,))
        self.all = {a["id"]: a for a in rows}
        self.acts = {i: a for i, a in self.all.items() if a["status"] != "cancelada"}
        self.deps = [x for x in db.q("SELECT d.* FROM dependencies d JOIN activities p ON p.id=d.predecessor_id WHERE p.season_id=?", (sid,))
                     if x["predecessor_id"] in self.acts and x["successor_id"] in self.acts]
        # referências do projeto (cada uma com a origem do valor, para a tela explicar de onde veio)
        dates = sorted(x for a in self.acts.values() for x in (a["planned_start"], a["planned_end"]) if x)
        if cfg.get("project_start"):
            self.start, self.start_src = cfg["project_start"], "configurado"
        elif season.get("start_date"):
            self.start, self.start_src = season["start_date"], "temporada"
        elif dates:
            self.start, self.start_src = dates[0], "primeira_data"
        else:
            self.start, self.start_src = self.today, "hoje"
        if cfg.get("deadline"):
            self.deadline, self.deadline_src = cfg["deadline"], "configurado"
        elif season.get("end_date"):
            self.deadline, self.deadline_src = season["end_date"], "temporada"
        else:
            self.deadline, self.deadline_src = None, "pendente"
        self.cal = Calendar(self.start, cfg.get("calendar", "corridos"), cfg.get("workdays"), cfg.get("holidays"))
        self.T = self.cal.index(self.today)
        self.issues = []
        self.plan_nodes, self.fc_nodes, self.meta = {}, {}, {}
        for a in self.acts.values():
            self._node(a)
        self.edges = []
        for x in self.deps:
            if x["predecessor_id"] in self.plan_nodes and x["successor_id"] in self.plan_nodes:
                self.edges.append({"id": x["id"], "pred": x["predecessor_id"], "succ": x["successor_id"], "type": x["type"], "lag": x["lag_days"]})
            else:
                miss = x["predecessor_id"] if x["predecessor_id"] not in self.plan_nodes else x["successor_id"]
                self.issues.append({"kind": "dependencia_ignorada", "activity_id": miss, "dependency_id": x["id"],
                                    "message": _t("Dependência “{0}” → “{1}” fora do cálculo até “{2}” ter duração definida.",
                                                  self.acts[x["predecessor_id"]]["title"], self.acts[x["successor_id"]]["title"], self.acts[miss]["title"])})

    # duração planejada e restrição de início a partir dos dados gravados
    def plan_duration(self, a):
        if a.get("duration_days") is not None:
            return a["duration_days"], "informada"
        ps, pe = a.get("planned_start"), a.get("planned_end")
        if ps and pe:
            n = self.cal.duration(ps, pe)
            if n is None:
                return None, "datas_invertidas"
            return max(n, 1), "datas"
        if a.get("is_milestone") and (ps or pe):
            return 0, "marco"
        return None, "sem_duracao"

    def _node(self, a):
        dur, src = self.plan_duration(a)
        m = {"dur_src": src}
        self.meta[a["id"]] = m
        if dur is None:
            self.issues.append({"kind": src, "activity_id": a["id"], "message": (
                _t("“{0}”: início planejado depois do término — corrija as datas.", a["title"]) if src == "datas_invertidas" else
                _t("“{0}”: sem duração nem datas de início e término. Informe a duração necessária ou as datas planejadas.", a["title"]))})
            m["in_network"] = False
            return
        m["in_network"] = True
        cal, ps, pe = self.cal, a.get("planned_start"), a.get("planned_end")
        min_es = None
        if self.cfg.get("respect_planned_start", True):
            if ps:
                min_es = cal.index(ps)
            elif pe:
                min_es = cal.index(pe, after=False) - max(dur, 1) + 1 if dur else cal.index(pe)
        if not ps and not pe:
            self.issues.append({"kind": "sem_datas", "activity_id": a["id"],
                                "message": _t("“{0}”: sem datas planejadas — posicionada apenas pelas dependências e pelo início do projeto.", a["title"])})
        elif not ps and dur:
            self.issues.append({"kind": "sem_inicio", "activity_id": a["id"],
                                "message": _t("“{0}”: sem data de início — o início foi deduzido do prazo e da duração.", a["title"])})
        self.plan_nodes[a["id"]] = {"dur": dur, "min_es": min_es}
        # previsão: considera o que já aconteceu e a data de hoje
        T, prog = self.T, a.get("progress") or 0
        if a["status"] == "concluida":
            s, e = a.get("actual_start") or ps, a.get("actual_end") or pe
            if e:
                ef = cal.index(e, after=False) + 1
                es = cal.index(s) if s else ef - dur
                es = min(es, ef)
            else:
                es = min_es if min_es is not None else T - dur
                ef = es + dur
            self.fc_nodes[a["id"]] = {"dur": ef - es, "fixed_es": es, "done": True}
        elif a.get("actual_start") or a["status"] in IN_PROGRESS or prog > 0:
            if a.get("actual_start"):
                es = cal.index(a["actual_start"])
            else:
                es = min(min_es if min_es is not None else T, T)
            remaining = math.ceil(dur * (100 - prog) / 100) if dur else 0
            self.fc_nodes[a["id"]] = {"dur": dur, "fixed_es": es, "min_ef": T + max(remaining, 1 if dur else 0), "started": True}
        else:
            self.fc_nodes[a["id"]] = {"dur": dur, "min_es": max(min_es, T) if min_es is not None else T}

    def copy(self):
        n = object.__new__(Network)
        n.__dict__.update(self.__dict__)
        n.plan_nodes = {k: dict(v) for k, v in self.plan_nodes.items()}
        n.fc_nodes = {k: dict(v) for k, v in self.fc_nodes.items()}
        n.edges = [dict(e) for e in self.edges]
        n.meta = {k: dict(v) for k, v in self.meta.items()}
        n.issues = list(self.issues)
        n.new_acts = dict(getattr(self, "new_acts", {}))
        return n

    def run(self):
        """Executa o CPM do plano e da previsão. Levanta CycleError se as dependências formarem um ciclo."""
        plan = compute(self.plan_nodes, self.edges, start_index=0)
        fc = compute(self.fc_nodes, self.fc_edges(), start_index=0)
        return plan, fc

    def fc_edges(self):
        """Na previsão, o que já aconteceu não é empurrado: ligações para atividades concluídas são ignoradas, e as de
        início (FS/SS) para atividades já iniciadas também (o início real já está registrado)."""
        out = []
        for e in self.edges:
            n = self.fc_nodes[e["succ"]]
            if n.get("done") or (n.get("started") and e["type"] in ("FS", "SS")):
                continue
            out.append(e)
        return out

    # conversões para datas exibidas
    def start_date(self, i):
        return self.cal.date(i).isoformat()

    def finish_date(self, es, ef):
        return self.cal.finish_date(es, ef).isoformat()


# ----------------------------------------------------------------------------- mudanças de cenário
def apply_changes(net, changes):
    """Aplica as mudanças de um cenário numa CÓPIA da rede. Devolve (rede, avisos, custo_total_em_reais)."""
    net = net.copy()
    warns, cost = [], 0.0
    T = net.T

    def ref(x):
        if isinstance(x, str) and x.startswith("new:"):
            return x if x in net.plan_nodes else None
        return x if x in net.plan_nodes else None

    for n, ch in enumerate(changes):
        k = ch.get("kind")
        if k == "new_activity":
            key = "new:" + str(ch.get("key") or n)
            dur = ch.get("duration")
            if dur is None:
                warns.append(_t("Nova atividade “{0}” sem duração: informe a duração para calcular o cenário.", ch.get("title") or "?"))
                continue
            net.plan_nodes[key] = {"dur": int(dur), "min_es": T}   # trabalho novo não começa no passado
            net.fc_nodes[key] = {"dur": int(dur), "min_es": T}
            net.meta[key] = {"dur_src": "cenario", "in_network": True, "new": True}
            net.new_acts[key] = {"id": key, "title": ch.get("title") or _t("Nova atividade"), "area_id": ch.get("area_id"), "owner_id": ch.get("owner_id"),
                                 "status": "nao_iniciada", "progress": 0, "new": True, "note": ch.get("note")}
            cost += float(ch.get("cost") or 0)
    for ch in changes:
        k = ch.get("kind")
        if k in ("extra_days", "set_duration", "set_start"):
            aid = ref(ch.get("activity_id"))
            if not aid:
                warns.append(_t("Mudança ignorada: atividade não encontrada ou fora do cálculo."))
                continue
            p, f = net.plan_nodes[aid], net.fc_nodes[aid]
            if k == "extra_days":
                days = ch.get("days")
                if days is None:
                    warns.append(_t("Informe os dias adicionais de “{0}”.", _title(net, aid)))
                    continue
                if f.get("done"):
                    warns.append(_t("“{0}” já está concluída: dias adicionais só afetam o plano, não a previsão.", _title(net, aid)))
                p["dur"] = max(0, p["dur"] + int(days))
                if not f.get("done"):
                    f["dur"] = max(0, f["dur"] + int(days))
                    if f.get("min_ef") is not None:
                        f["min_ef"] += int(days)
            elif k == "set_duration":
                dur = ch.get("duration")
                if dur is None:
                    warns.append(_t("Informe a nova duração de “{0}”.", _title(net, aid)))
                    continue
                delta = int(dur) - p["dur"]
                p["dur"] = int(dur)
                if not f.get("done"):
                    f["dur"] = int(dur)
                    if f.get("min_ef") is not None:
                        f["min_ef"] = max(T, f["min_ef"] + delta)
            else:
                try:
                    i = net.cal.index(ch.get("date"))
                except (TypeError, ValueError):
                    warns.append(_t("Data inválida na mudança de início de “{0}”.", _title(net, aid)))
                    continue
                p["min_es"] = i
                if not f.get("done") and not f.get("started"):
                    f["min_es"] = max(i, T)
        elif k == "add_dependency":
            pr, su = ref(ch.get("pred")), ref(ch.get("succ"))
            t, lag = ch.get("type") or "FS", int(ch.get("lag") or 0)
            if not pr or not su or pr == su or t not in DEP_TYPES:
                warns.append(_t("Nova dependência ignorada: atividades inválidas."))
                continue
            if any(e["pred"] == pr and e["succ"] == su for e in net.edges):
                warns.append(_t("Dependência já existente: “{0}” → “{1}”.", _title(net, pr), _title(net, su)))
                continue
            net.edges.append({"id": "new-dep-%d" % len(net.edges), "pred": pr, "succ": su, "type": t, "lag": lag, "new": True})
        elif k == "remove_dependency":
            before = len(net.edges)
            net.edges = [e for e in net.edges if e["id"] != ch.get("dependency_id")]
            if len(net.edges) == before:
                warns.append(_t("Dependência a remover não encontrada."))
        elif k == "cost":
            cost += float(ch.get("amount") or 0)
    return net, warns, cost


def _title(net, aid):
    if aid in net.acts:
        return net.acts[aid]["title"]
    return (getattr(net, "new_acts", {}).get(aid) or {}).get("title", "?")


def clean_changes(db, sid, changes):
    if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
        raise bad(_t("Lista de mudanças inválida (máximo de {0}).", MAX_CHANGES))
    out, keys = [], set()
    ids = {r["id"] for r in db.q("SELECT id FROM activities WHERE season_id=? AND deleted_at IS NULL", (sid,))}
    dep_ids = {r["id"] for r in db.q("SELECT d.id FROM dependencies d JOIN activities a ON a.id=d.predecessor_id WHERE a.season_id=?", (sid,))}
    for ch in changes:
        if not isinstance(ch, dict) or ch.get("kind") not in CHANGE_KINDS:
            raise bad(_t("Tipo de mudança inválido."))
        k = ch["kind"]
        c = {"kind": k, "note": str(ch.get("note") or "")[:1000] or None}

        def num(name, lo, hi, required=False):
            v = ch.get(name)
            if v in (None, ""):
                return None
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise bad(_t("Valor numérico inválido em uma das mudanças."))
            if not lo <= v <= hi:
                raise bad(_t("Valor fora do intervalo permitido ({0} a {1}).", lo, hi))
            return v

        def act_ref(name):
            v = ch.get(name)
            if isinstance(v, str) and (v in ids or (v.startswith("new:") and v[4:] in keys)):
                return v
            raise bad(_t("Atividade inválida em uma das mudanças."))

        if k == "new_activity":
            key = str(ch.get("key") or "").strip()[:20] or str(len(keys) + 1)
            if key in keys:
                raise bad(_t("Chave de nova atividade repetida."))
            keys.add(key)
            title = str(ch.get("title") or "").strip()[:200]
            if not title:
                raise bad(_t("Informe o título da nova atividade."))
            c.update({"key": key, "title": title, "duration": num("duration", 0, 3650), "area_id": ch.get("area_id") or None,
                      "owner_id": ch.get("owner_id") or None})
            if c["area_id"] and not db.one("SELECT 1 FROM areas WHERE id=?", (c["area_id"],)):
                raise bad(_t("Área inválida."))
            if c["owner_id"] and not db.one("SELECT 1 FROM users WHERE id=? AND deleted_at IS NULL", (c["owner_id"],)):
                raise bad(_t("Responsável inválido."))
            c["cost"] = _money(ch.get("cost"))
        elif k == "extra_days":
            c.update({"activity_id": act_ref("activity_id"), "days": num("days", -365, 365)})
        elif k == "set_duration":
            c.update({"activity_id": act_ref("activity_id"), "duration": num("duration", 0, 3650)})
        elif k == "set_start":
            c.update({"activity_id": act_ref("activity_id"), "date": _valid_date(ch.get("date"), _t("Nova data de início"))})
            if not c["date"]:
                raise bad(_t("Informe a nova data de início."))
        elif k == "add_dependency":
            t = ch.get("type") or "FS"
            if t not in DEP_TYPES:
                raise bad(_t("Tipo de dependência inválido."))
            c.update({"pred": act_ref("pred"), "succ": act_ref("succ"), "type": t, "lag": num("lag", -365, 365) or 0})
            if c["pred"] == c["succ"]:
                raise bad(_t("Uma atividade não pode depender dela própria."))
        elif k == "remove_dependency":
            if ch.get("dependency_id") not in dep_ids:
                raise bad(_t("Dependência inválida."))
            c["dependency_id"] = ch["dependency_id"]
        elif k == "cost":
            c.update({"amount": _money(ch.get("amount")), "description": str(ch.get("description") or "")[:300]})
        out.append(c)
    return out


def _money(v):
    if v in (None, ""):
        return None
    try:
        v = round(float(v), 2)
    except (TypeError, ValueError):
        raise bad(_t("Valor monetário inválido."))
    if not 0 <= v <= 10_000_000:
        raise bad(_t("Valor monetário fora do intervalo permitido."))
    return v


# ----------------------------------------------------------------------------- análise completa
def analyze(net, plan=None, fc=None):
    if plan is None:
        plan, fc = net.run()
    cal, cfg, today = net.cal, net.cfg, net.today
    users = user_names(net.db)
    rows, by_id = [], {}
    preds, succs = {}, {}
    for e in net.edges:
        succs.setdefault(e["pred"], []).append(e)
        preds.setdefault(e["succ"], []).append(e)
    items = list(net.acts.values()) + list(getattr(net, "new_acts", {}).values())
    for a in items:
        aid = a["id"]
        m = net.meta.get(aid, {"in_network": False, "dur_src": "sem_duracao"})
        is_late, days_late, completed_late = lateness(a, today) if not a.get("new") else (False, 0, False)
        r = {"id": aid, "seq": a.get("seq"), "title": a["title"], "area_id": a.get("area_id"), "owner_id": a.get("owner_id"),
             "owner_name": users.get(a.get("owner_id")), "status": a.get("status"), "progress": a.get("progress") or 0,
             "planned_start": a.get("planned_start"), "planned_end": a.get("planned_end"), "actual_start": a.get("actual_start"),
             "actual_end": a.get("actual_end"), "is_milestone": bool(a.get("is_milestone")), "duration_days": a.get("duration_days"),
             "category": a.get("category"), "description": a.get("description"), "version": a.get("version"), "new": bool(a.get("new")),
             "duration": None, "duration_src": m["dur_src"], "in_network": m["in_network"], "is_late": is_late, "days_late": days_late,
             "completed_late": completed_late, "done": a.get("status") == "concluida",
             "preds": [{"dep_id": e["id"], "id": e["pred"], "type": e["type"], "lag": e["lag"]} for e in preds.get(aid, [])],
             "succs": [{"dep_id": e["id"], "id": e["succ"], "type": e["type"], "lag": e["lag"]} for e in succs.get(aid, [])]}
        if m["in_network"]:
            p, f = plan["nodes"][aid], fc["nodes"][aid]
            r["duration"] = net.plan_nodes[aid]["dur"]
            r["plan"] = {"es": net.start_date(p["es"]), "ef": net.finish_date(p["es"], p["ef"]), "ls": net.start_date(p["ls"]),
                         "lf": net.finish_date(p["ls"], p["lf"]), "tf": p["tf"], "ff": p["ff"], "critical": p["critical"],
                         "i": [p["es"], p["ef"], p["ls"], p["lf"]]}
            delay = f["ef"] - p["ef"]
            r["forecast"] = {"start": net.start_date(f["es"]), "finish": net.finish_date(f["es"], f["ef"]), "tf": f["tf"], "ff": f["ff"],
                             "critical": f["critical"] and not r["done"], "delay": delay,
                             # um atraso só chega ao fim do projeto quando ultrapassa a folga total planejada
                             "impact": max(0, delay - max(p["tf"], 0)), "i": [f["es"], f["ef"]]}
        rows.append(r)
        by_id[aid] = r
    live = [r for r in rows if r["in_network"] and not r["done"]]
    plan_finish = net.finish_date(0, plan["finish"]) if net.plan_nodes else None
    fc_finish = net.finish_date(0, fc["finish"]) if net.fc_nodes else None
    deadline = net.deadline
    margin = None
    if deadline and fc_finish:
        margin = (date.fromisoformat(deadline) - date.fromisoformat(fc_finish)).days
    edges = []
    for e in net.edges:
        pn, sn = plan["nodes"][e["pred"]], plan["nodes"][e["succ"]]
        fp, fs = fc["nodes"][e["pred"]], fc["nodes"][e["succ"]]
        sp, sf = edge_slack(e, pn, sn), edge_slack(e, fp, fs)
        edges.append({"id": e["id"], "pred": e["pred"], "succ": e["succ"], "type": e["type"], "lag": e["lag"], "new": bool(e.get("new")),
                      "plan_slack": sp, "plan_critical": pn["critical"] and sn["critical"] and sp == 0,
                      "fc_slack": sf, "fc_critical": fp["critical"] and fs["critical"] and sf == 0 and not by_id[e["succ"]]["done"]})
    # alertas
    alerts = []
    near = cfg.get("near_days", 7)
    low = cfg.get("low_float", 3)
    for r in live:
        crit = r["plan"]["critical"] or r["forecast"]["critical"]
        due = r["planned_end"] or r["plan"]["ef"]
        if crit and r["is_late"]:
            alerts.append({"level": "crit", "code": "critica_atrasada", "activity_id": r["id"],
                           "message": _t("Atividade crítica atrasada há {0} dia(s): “{1}”.", r["days_late"], r["title"])})
        elif crit and due and 0 <= (date.fromisoformat(due) - date.fromisoformat(today)).days <= near:
            alerts.append({"level": "warn", "code": "critica_proxima", "activity_id": r["id"],
                           "message": _t("Atividade crítica com prazo em {0} dia(s): “{1}”.", (date.fromisoformat(due) - date.fromisoformat(today)).days, r["title"])})
        if not crit and 0 < r["forecast"]["tf"] <= low:
            alerts.append({"level": "warn", "code": "folga_reduzida", "activity_id": r["id"],
                           "message": _t("Folga reduzida: “{0}” pode atrasar no máximo {1} dia(s) sem atrasar o projeto.", r["title"], r["forecast"]["tf"])})
        if r["forecast"]["delay"] > 0 and r["forecast"]["impact"] == 0 and r["plan"]["tf"] > 0 and not crit:
            alerts.append({"level": "info", "code": "atraso_absorvido", "activity_id": r["id"],
                           "message": _t("“{0}” está {1} dia(s) atrasada em relação ao plano, ainda dentro da folga de {2} dia(s).", r["title"], r["forecast"]["delay"], r["plan"]["tf"])})
    if margin is not None and margin < 0:
        alerts.append({"level": "crit", "code": "previsao_apos_limite",
                       "message": _t("A previsão de conclusão ({0}) é {1} dia(s) depois da data-limite ({2}).", fc_finish, -margin, deadline), "dates": [fc_finish, deadline]})
    if not deadline:
        alerts.append({"level": "info", "code": "sem_data_limite", "message": _t("Data-limite do projeto não definida: informe-a na configuração do cálculo.")})
    pend = [i for i in net.issues if i["kind"] in ("sem_duracao", "datas_invertidas")]
    if pend:
        alerts.append({"level": "warn", "code": "informacoes_insuficientes",
                       "message": _t("{0} atividade(s) sem duração ou datas suficientes ficaram fora do cálculo. Complete-as em “Pendências”.", len(pend))})
    # isoladas: sem predecessoras e sem sucessoras (o cálculo funciona, mas a sequência lógica não está definida)
    isolated = [r for r in rows if r["in_network"] and not r["preds"] and not r["succs"]]
    for r in isolated:
        net.issues.append({"kind": "isolada", "activity_id": r["id"],
                           "message": _t("“{0}”: sem predecessoras nem sucessoras — defina a sequência, se houver.", r["title"])})
    # conflitos entre as datas oficiais e as dependências
    for e in net.edges:
        if e.get("new"):
            continue
        p, s = net.acts.get(e["pred"]), net.acts.get(e["succ"])
        if not p or not s or s["status"] == "concluida":
            continue
        need = plan["nodes"][e["succ"]]["es"]
        if s.get("planned_start") and net.cal.index(s["planned_start"]) < need:
            net.issues.append({"kind": "conflito_datas", "activity_id": s["id"], "dependency_id": e["id"],
                               "message": _t("“{0}” está planejada para começar em {1}, antes do permitido pela dependência de “{2}” ({3}).",
                                             s["title"], s["planned_start"], p["title"], net.start_date(need))})
    starts = [r["id"] for r in rows if r["in_network"] and not r["preds"]]
    ends = [r["id"] for r in rows if r["in_network"] and not r["succs"]]
    kpis = {
        "plan_duration": plan["finish"] if net.plan_nodes else None,
        "plan_span_days": ((date.fromisoformat(plan_finish) - date.fromisoformat(net.cal.date(0).isoformat())).days + 1) if plan_finish else None,
        "plan_finish": plan_finish, "forecast_finish": fc_finish, "forecast_delay": (fc["finish"] - plan["finish"]) if net.plan_nodes else None,
        "deadline": deadline, "deadline_src": net.deadline_src, "deadline_margin": margin,
        "activities": len(rows), "in_network": sum(1 for r in rows if r["in_network"]), "pending": len(pend),
        "critical": sum(1 for r in live if r["forecast"]["critical"]), "critical_plan": sum(1 for r in rows if r["in_network"] and r["plan"]["critical"]),
        "late": sum(1 for r in rows if r["is_late"]),
        "min_float": min((r["forecast"]["tf"] for r in live), default=None),
        "min_float_id": min(live, key=lambda r: r["forecast"]["tf"])["id"] if live else None,
    }
    return {"activities": rows, "edges": edges, "alerts": alerts, "issues": net.issues, "kpis": kpis,
            "paths": {"plan": plan["paths"], "forecast": [p for p in fc["paths"] if any(not by_id[i]["done"] for i in p)],
                      "truncated": plan["paths_truncated"] or fc["paths_truncated"]},
            "starts": starts, "ends": ends,
            "reference": {"project_start": net.start, "project_start_src": net.start_src, "calendar_origin": net.cal.date(0).isoformat(),
                          "deadline": deadline, "deadline_src": net.deadline_src, "today": today, "calendar": net.cal.mode,
                          "workdays": sorted(net.cal.workdays), "holidays": sorted(h.isoformat() for h in net.cal.holidays)}}


def risk_rows(db, sid):
    rows = db.q("SELECT * FROM risks WHERE season_id=? AND deleted_at IS NULL AND archived_at IS NULL", (sid,))
    return expand_risks(db, [RISK.ser(r) for r in rows])


def _top_band(db):
    scale = get_setting(db, "risk_scale")
    return scale["bands"][-1]["name"] if scale.get("bands") else None


def critical_risk(r, top):
    lvl = (r.get("original_level") or r.get("level") or "")
    return bool(lvl) and (_norm(lvl) == _norm(top) or _norm(lvl) == "critico")


def attach_risks(db, sid, result):
    """Riscos ligados a cada atividade + alerta de risco crítico em atividade crítica."""
    links = db.q("SELECT l.risk_id, l.entity_id, l.est_delay_days FROM risk_links l JOIN risks r ON r.id=l.risk_id "
                 "WHERE l.entity_type='activity' AND r.deleted_at IS NULL AND r.archived_at IS NULL AND r.season_id=?", (sid,))
    risks = {r["id"]: r for r in risk_rows(db, sid)}
    top = _top_band(db)
    by_act = {}
    for l in links:
        if l["risk_id"] in risks:
            by_act.setdefault(l["entity_id"], []).append(l)
    crit_pairs = set()
    for a in result["activities"]:
        ls = by_act.get(a["id"], [])
        a["risks"] = [{"id": l["risk_id"], "code": risks[l["risk_id"]]["code"], "title": risks[l["risk_id"]]["title"],
                       "level": risks[l["risk_id"]].get("original_level") or risks[l["risk_id"]]["level"], "est_delay_days": l["est_delay_days"],
                       "critical": critical_risk(risks[l["risk_id"]], top)} for l in ls]
        is_crit = a["in_network"] and not a["done"] and (a["plan"]["critical"] or a["forecast"]["critical"])
        for x in a["risks"]:
            if is_crit and x["critical"] and risks[x["id"]]["status"] != "encerrado":
                crit_pairs.add(x["id"])
                result["alerts"].append({"level": "crit", "code": "risco_critico_em_atividade_critica", "activity_id": a["id"], "risk_id": x["id"],
                                         "message": _t("Risco crítico {0} “{1}” ligado à atividade crítica “{2}”.", x["code"], x["title"], a["title"])})
    result["kpis"]["critical_risks"] = len(crit_pairs)
    result["kpis"]["risks_total"] = len(risks)
    return result


def data_updated_at(db, sid):
    vals = [db.val("SELECT MAX(updated_at) FROM activities WHERE season_id=?", (sid,)),
            db.val("SELECT MAX(d.created_at) FROM dependencies d JOIN activities a ON a.id=d.predecessor_id WHERE a.season_id=?", (sid,)),
            db.val("SELECT MAX(updated_at) FROM risks WHERE season_id=?", (sid,)),
            db.val("SELECT updated_at FROM cpm_config WHERE season_id=?", (sid,))]
    vals = [v for v in vals if v]
    return max(vals) if vals else None


def official(ctx, sid):
    season = season_or_400(ctx.db, sid)
    cfg = load_config(ctx.db, sid)
    net = Network(ctx.db, season, cfg)
    try:
        res = analyze(net)
    except CycleError as e:
        names = [net.acts[i]["title"] for i in e.cycle if i in net.acts]
        raise conflict(_t("As dependências formam um ciclo e o caminho crítico não pode ser calculado: {0}. Remova uma das ligações.", " → ".join(names)),
                       code="ciclo", cycle=e.cycle)
    attach_risks(ctx.db, sid, res)
    res["config"] = {k: cfg.get(k) for k in DEFAULT_CFG}
    res["config"]["updated_at"] = cfg.get("updated_at")
    res["computed_at"] = now_iso()
    res["data_updated_at"] = data_updated_at(ctx.db, sid)
    return net, res


def _guard(ctx):
    judge_module_guard(ctx, "caminho_critico")


# ----------------------------------------------------------------------------- rotas: análise e configuração
@route("GET", "/api/cpm")
def get_cpm(ctx):
    _guard(ctx)
    _, res = official(ctx, ctx.query.get("season_id"))
    return res


@route("PUT", "/api/cpm/config", allow=STAFF)
def put_config(ctx):
    sid = ctx.body.get("season_id")
    season_or_400(ctx.db, sid)
    before = load_config(ctx.db, sid)
    data = clean_config(ctx.body.get("config") or {})
    if ctx.db.one("SELECT 1 FROM cpm_config WHERE season_id=?", (sid,)):
        ctx.db.ex("UPDATE cpm_config SET data=?, updated_at=?, updated_by=? WHERE season_id=?", (json.dumps(data), now_iso(), ctx.uid, sid))
    else:
        ctx.db.insert("cpm_config", {"season_id": sid, "data": json.dumps(data), "updated_at": now_iso(), "updated_by": ctx.uid})
    audit(ctx, "configurar", "cpm_config", sid, _t("Configuração do caminho crítico alterada"), before={k: before.get(k) for k in DEFAULT_CFG}, after=data)
    return {"config": data}


# ----------------------------------------------------------------------------- riscos × cronograma
# Palavras-chave para PROPOR vínculos risco → atividade (o sistema só sugere; a equipe confirma).
RISK_HINTS = [
    (("teste", "test"), ("teste", "desempenho do carro", "valida", "pista", "test", "performance report", "track")),
    (("fabricacao", "fabrica", "montagem", "manufactur", "assembly"), ("usinagem", "fabrica", "pintura", "impress", "montagem", "desenho", "carro",
                                                                      "machining", "manufactur", "painting", "printing", "assembly", "drawing", "car")),
    (("fornecedor", "supplier"), ("usinagem", "fabrica", "pintura", "impress", "camiset", "material", "compra", "uniforme",
                                  "machining", "manufactur", "painting", "printing", "shirt", "purchas", "uniform")),
    (("materia",), ("usinagem", "fabrica", "pintura", "impress", "material", "compra", "montagem",
                    "machining", "manufactur", "painting", "printing", "purchas", "assembly")),
    (("regulamento", "regulation"), ("carro", "desenho", "usinagem", "regulamento", "checklist", "car", "drawing", "machining", "regulation")),
    (("patrocin", "orcamento", "sponsor", "budget"), ("patrocin", "captacao", "stakeholder", "orcamento", "financ", "sustentabilidade",
                                                      "sponsor", "fundrais", "budget", "sustainab")),
    (("divulgacao", "marketing", "promotion"), ("marketing", "video", "divulga", "redes", "identidade", "comunica",
                                                "promotion", "social media", "identity", "communicat", "campaign")),
    (("camiset", "shirt"), ("camiset", "identidade", "uniforme", "pit display", "shirt", "identity", "uniform")),
    (("documento", "document"), ("portfolio", "portifolio", "documento", "relatorio", "document", "report")),
    (("acidente", "accident"), ("usinagem", "fabrica", "teste", "pintura", "montagem", "machining", "manufactur", "test", "painting", "assembly")),
    (("comunicacao", "communication"), ("reuniao", "comunica", "alinhamento", "status do projeto", "meeting", "communicat", "alignment", "project status")),
]
# Propostas de continuidade (SUGESTÕES do sistema, não decisões aprovadas), por tema do risco.
PROPOSALS = [
    (("fornecedor", "supplier"), [_t("Acionar um fornecedor alternativo já identificado e comparar prazos."), _t("Antecipar a compra dos itens críticos para o cronograma."),
                       _t("Confirmar por escrito os prazos de entrega com o fornecedor.")]),
    (("materia",), [_t("Avaliar materiais substitutos compatíveis com o regulamento e com o projeto."), _t("Verificar estoque disponível ou empréstimo com a escola e parceiros."),
                    _t("Fazer o levantamento de materiais antes das etapas de fabricação.")]),
    (("fabricacao", "fabrica", "montagem", "manufactur", "assembly"), [_t("Reservar tempo para refabricação e para um novo teste."), _t("Revisar o processo (desenho, impressão, usinagem) antes de repetir a peça."),
                                             _t("Produzir peças reserva das partes mais sujeitas a falhas.")]),
    (("teste", "test"), [_t("Diagnosticar a causa e registrar o resultado do teste."), _t("Reparar e repetir o teste antes das etapas seguintes."),
                  _t("Prever uma janela extra de testes antes da competição.")]),
    (("integrante", "indisponib", "member", "unavailab"), [_t("Redistribuir as tarefas entre integrantes da mesma área."), _t("Definir um substituto para a função crítica."),
                                    _t("Manter o andamento documentado para facilitar a passagem de tarefas.")]),
    (("comunicacao", "communication"), [_t("Fazer uma reunião de alinhamento entre as áreas envolvidas."), _t("Atualizar os registros na plataforma."),
                        _t("Revalidar as entregas afetadas com os responsáveis.")]),
    (("patrocin", "orcamento", "sponsor", "budget"), [_t("Priorizar as despesas essenciais para a competição."), _t("Buscar fontes alternativas (campanhas, apoio da escola, novos parceiros)."),
                                 _t("Adiar itens não essenciais até a confirmação de recursos.")]),
    (("prazo", "competicao", "deadline", "competition"), [_t("Identificar as atividades críticas que garantem a participação."), _t("Reduzir temporariamente o escopo não obrigatório."),
                               _t("Concentrar a equipe nas entregas exigidas pelo regulamento.")]),
    (("regulamento", "regulation"), [_t("Conferir o carro com a checklist do regulamento antes da fabricação final."), _t("Reservar tempo para ajustes antes da inspeção.")]),
    (("documento", "document"), [_t("Recuperar a versão mais recente a partir dos backups e anexos da plataforma."), _t("Padronizar pastas e responsáveis pela guarda dos arquivos.")]),
    (("divulgacao", "marketing", "promotion"), [_t("Reorganizar o calendário de conteúdo com publicações já preparadas."), _t("Dividir a produção de conteúdo entre mais integrantes.")]),
    (("camiset", "shirt"), [_t("Conferir arte, quantidade e prazo antes de liberar a produção."), _t("Ter um fornecedor alternativo para materiais da equipe.")]),
    (("acidente", "accident"), [_t("Interromper a atividade, prestar atendimento e revisar os procedimentos de segurança."), _t("Reprogramar a atividade com supervisão e equipamentos de proteção.")]),
    (("cronograma", "schedule"), [_t("Acompanhar o cronograma semanalmente e estabelecer prazos internos antecipados."), _t("Concentrar esforços nas atividades críticas e de folga reduzida.")]),
]


def _match(title, keys):
    t = _norm(title)
    return any(k in t for k in keys)


@route("GET", "/api/cpm/risks")
def cpm_risks(ctx):
    _guard(ctx)
    if ctx.role == "juiz":
        judge_module_guard(ctx, "riscos")
    sid = ctx.query.get("season_id")
    net, res = official(ctx, sid)
    by_id = {a["id"]: a for a in res["activities"]}
    db = ctx.db
    matrix, scale = get_setting(db, "impact_matrix"), get_setting(db, "risk_scale")
    top = _top_band(db)
    links = {}
    for l in db.q("SELECT * FROM risk_links WHERE entity_type='activity'"):
        links.setdefault(l["risk_id"], []).append(l)
    users = user_names(db)
    base_plan, base_fc = net.run()
    out = []
    for r in risk_rows(db, sid):
        p, i = r.get("probability"), r.get("impact")
        cell = None
        if scale.get("size") == 5 and p and i and 1 <= p <= 5 and 1 <= i <= 5 and matrix.get("cells"):
            cell = {"text": matrix["cells"][p - 1][i - 1], "probability": matrix["probability_labels"][p - 1] if matrix.get("probability_labels") else p,
                    "impact": matrix["impact_labels"][i - 1] if matrix.get("impact_labels") else i}
        linked = []
        extra = {}
        for l in links.get(r["id"], []):
            a = by_id.get(l["entity_id"])
            if not a:
                continue
            linked.append({"activity_id": a["id"], "title": a["title"], "est_delay_days": l["est_delay_days"], "note": l["note"],
                           "in_network": a["in_network"], "done": a["done"],
                           "critical": bool(a["in_network"] and (a["plan"]["critical"] or a["forecast"]["critical"]) and not a["done"]),
                           "tf": a["forecast"]["tf"] if a["in_network"] else None})
            if l["est_delay_days"] and a["in_network"] and not a["done"]:
                extra[a["id"]] = l["est_delay_days"]
        impact = None
        if extra:
            sim, _, _ = apply_changes(net, [{"kind": "extra_days", "activity_id": k, "days": v} for k, v in extra.items()])
            sp, sf = sim.run()
            impact = {"finish_delay": sf["finish"] - base_fc["finish"], "forecast_finish": sim.finish_date(0, sf["finish"]),
                      "plan_delay": sp["finish"] - base_plan["finish"], "activities": len(extra)}
        hints = next((h for k, h in RISK_HINTS if _match(r["title"] + " " + (r.get("cause") or ""), k)), None)
        suggested = []
        linked_ids = {x["activity_id"] for x in linked}
        if hints:
            suggested = [{"activity_id": a["id"], "title": a["title"], "critical": bool(a["in_network"] and a["plan"]["critical"])}
                         for a in res["activities"] if a["id"] not in linked_ids and not a["done"] and not a["new"] and _match(a["title"], hints)][:12]
        elif _match(r["title"], ("cronograma", "prazo")):
            suggested = [{"activity_id": a["id"], "title": a["title"], "critical": True} for a in res["activities"]
                         if a["id"] not in linked_ids and a["in_network"] and not a["done"] and a["forecast"]["critical"]][:12]
        props = next((ps for k, ps in PROPOSALS if _match(r["title"], k)), [])
        out.append({"id": r["id"], "code": r["code"], "title": r["title"], "cause": r.get("cause"), "consequence": r.get("consequence"),
                    "probability": p, "impact": i, "probability_label": scale["probability_labels"][p - 1] if p and p <= len(scale.get("probability_labels", [])) else None,
                    "impact_label": scale["impact_labels"][i - 1] if i and i <= len(scale.get("impact_labels", [])) else None,
                    "original_probability": r.get("original_probability"), "original_impact": r.get("original_impact"),
                    "registered_level": r.get("original_level") or r.get("level"), "computed_level": r.get("level"), "score": r.get("score"),
                    "critical": critical_risk(r, top), "status": r["status"], "mitigation": r.get("mitigation"), "preventive_plan": r.get("preventive_plan"),
                    "contingency_plan": r.get("contingency_plan"), "owner_text": r.get("owner_text"), "owner_name": users.get(r.get("owner_id")),
                    "area_ids": r.get("area_ids", []), "actions": [{"description": x["description"], "status": x["status"], "due_date": x["due_date"]} for x in r.get("actions", [])],
                    "matrix": cell, "linked": linked, "impact_estimate": impact, "suggested_activities": suggested, "proposals": props,
                    "occurred": r["status"] == "ocorrido"})
    out.sort(key=lambda x: (-(x["score"] or 0), x["code"]))
    return {"items": out, "matrix": matrix, "scale": {"probability_labels": scale.get("probability_labels"), "impact_labels": scale.get("impact_labels")},
            "computed_at": res["computed_at"]}


@route("PUT", "/api/cpm/risk-link", allow=STAFF)
def put_risk_link(ctx):
    db, b = ctx.db, ctx.body
    r = db.one("SELECT * FROM risks WHERE id=? AND deleted_at IS NULL", (b.get("risk_id"),))
    a = db.one("SELECT * FROM activities WHERE id=? AND deleted_at IS NULL", (b.get("activity_id"),))
    if not r or not a or r["season_id"] != a["season_id"]:
        raise bad(_t("Risco ou atividade inválidos."))
    cur = db.one("SELECT * FROM risk_links WHERE risk_id=? AND entity_type='activity' AND entity_id=?", (r["id"], a["id"]))
    if b.get("linked") is False:
        if cur:
            db.ex("DELETE FROM risk_links WHERE risk_id=? AND entity_type='activity' AND entity_id=?", (r["id"], a["id"]))
            audit(ctx, "alterar", "risks", r["id"], _t("Vínculo removido: risco {0} × atividade “{1}”", r["code"], a["title"]), before=cur)
        return {"linked": False}
    days = b.get("est_delay_days")
    if days in ("", None):
        days = None
    else:
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise bad(_t("Dias adicionais: informe um número inteiro."))
        if not 0 <= days <= 365:
            raise bad(_t("Dias adicionais: informe um valor de 0 a 365."))
    note = str(b.get("note") or "").strip()[:1000] or None
    if cur:
        db.ex("UPDATE risk_links SET est_delay_days=?, note=? WHERE risk_id=? AND entity_type='activity' AND entity_id=?", (days, note, r["id"], a["id"]))
    else:
        db.ex("INSERT INTO risk_links (risk_id, entity_type, entity_id, est_delay_days, note) VALUES (?,?,?,?,?)", (r["id"], "activity", a["id"], days, note))
    audit(ctx, "alterar", "risks", r["id"], _t("Vínculo: risco {0} × atividade “{1}” (efeito estimado: {2} dia(s))", r["code"], a["title"], days if days is not None else "—"),
          before=cur, after={"est_delay_days": days, "note": note})
    return {"linked": True, "est_delay_days": days, "note": note}


# ----------------------------------------------------------------------------- cenários
def _ser_scenario(s):
    out = dict(s)
    for k in ("changes", "applied_before", "applied_result"):
        if out.get(k):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                out[k] = None
    return out


def _load_scenario(db, sid_):
    s = db.one("SELECT * FROM cpm_scenarios WHERE id=? AND deleted_at IS NULL", (sid_,))
    if not s:
        raise not_found(_t("Cenário"))
    return _ser_scenario(s)


def simulate(ctx, net, base, scen):
    """Calcula um cenário e compara com o planejamento oficial (sem gravar nada)."""
    sim, warns, cost = apply_changes(net, scen.get("changes") or [])
    try:
        res = analyze(sim)
    except CycleError as e:
        names = [_title(sim, i) for i in e.cycle]
        return {"error": _t("As mudanças deste cenário criam um ciclo de dependências: {0}.", " → ".join(names)), "cycle": e.cycle, "warnings": warns}
    k, bk = res["kpis"], base["kpis"]
    base_by = {a["id"]: a for a in base["activities"]}
    moved = []
    for a in res["activities"]:
        b0 = base_by.get(a["id"])
        if a["in_network"] and (a.get("new") or (b0 and b0["in_network"] and (a["plan"]["es"], a["plan"]["ef"]) != (b0["plan"]["es"], b0["plan"]["ef"]))):
            moved.append({"id": a["id"], "title": a["title"], "new": bool(a.get("new")),
                          "from": [b0["plan"]["es"], b0["plan"]["ef"]] if b0 and b0["in_network"] else None, "to": [a["plan"]["es"], a["plan"]["ef"]],
                          "fc_from": b0["forecast"]["finish"] if b0 and b0["in_network"] else None, "fc_to": a["forecast"]["finish"]})
    risk_open = []
    if scen.get("risk_id"):
        r = ctx.db.one("SELECT code, title, status FROM risks WHERE id=?", (scen["risk_id"],))
        if r:
            risk_open.append(r)
    days = lambda a, b: (date.fromisoformat(a) - date.fromisoformat(b)).days if a and b else None
    summary = {"plan_finish": k["plan_finish"], "forecast_finish": k["forecast_finish"],
               "plan_delta_days": days(k["plan_finish"], bk["plan_finish"]), "forecast_delta_days": days(k["forecast_finish"], bk["forecast_finish"]),
               "deadline_margin": k["deadline_margin"], "critical": k["critical"], "extra_cost": round(cost, 2),
               "new_activities": sum(1 for c in scen.get("changes") or [] if c.get("kind") == "new_activity"),
               "changed_activities": len(moved), "warnings": warns, "risks": risk_open,
               "critical_path_changed": [sorted(p) for p in res["paths"]["forecast"]] != [sorted(p) for p in base["paths"]["forecast"]]}
    attach_risks(ctx.db, net.season["id"], res)
    return {"analysis": res, "summary": summary, "moved": moved}


def _can_edit(ctx, s):
    return ctx.role in STAFF or s["created_by"] == ctx.uid


@route("POST", "/api/cpm/simular", allow=INTERNAL, tx=False)
def preview(ctx):
    """Calcula mudanças ainda não salvas (prévia do editor de cenários). Nada é gravado."""
    sid = ctx.body.get("season_id")
    net, base = official(ctx, sid)
    changes = clean_changes(ctx.db, sid, ctx.body.get("changes") or [])
    return simulate(ctx, net, base, {"changes": changes, "risk_id": ctx.body.get("risk_id")})


@route("GET", "/api/cpm/scenarios", allow=INTERNAL)
def list_scenarios(ctx):
    sid = ctx.query.get("season_id")
    net, base = official(ctx, sid)
    rows = [_ser_scenario(s) for s in ctx.db.q("SELECT * FROM cpm_scenarios WHERE season_id=? AND deleted_at IS NULL ORDER BY created_at DESC", (sid,))]
    names = user_names(ctx.db)
    for s in rows:
        s["created_by_name"], s["decided_by_name"] = names.get(s["created_by"]), names.get(s.get("decided_by"))
        s.pop("applied_before", None)
        if s["status"] in ("rascunho", "proposto", "rejeitado"):
            sim = simulate(ctx, net, base, s)
            s["summary"] = sim.get("summary")
            s["error"] = sim.get("error")
    return {"items": rows, "official": {"plan_finish": base["kpis"]["plan_finish"], "forecast_finish": base["kpis"]["forecast_finish"],
                                         "deadline": base["kpis"]["deadline"], "deadline_margin": base["kpis"]["deadline_margin"], "critical": base["kpis"]["critical"]}}


@route("GET", "/api/cpm/scenarios/{id}", allow=INTERNAL)
def get_scenario(ctx):
    s = _load_scenario(ctx.db, ctx.params["id"])
    net, base = official(ctx, s["season_id"])
    names = user_names(ctx.db)
    s["created_by_name"], s["decided_by_name"] = names.get(s["created_by"]), names.get(s.get("decided_by"))
    s["can_edit"] = _can_edit(ctx, s) and s["status"] in ("rascunho", "proposto", "rejeitado")
    s["can_decide"] = ctx.role in STAFF and s["status"] in ("rascunho", "proposto")
    if s["status"] == "aprovado":
        return {"scenario": s, "official": {"kpis": base["kpis"]}}
    sim = simulate(ctx, net, base, s)
    return {"scenario": s, "official": {"kpis": base["kpis"], "paths": base["paths"]}, **sim}


@route("POST", "/api/cpm/scenarios", allow=INTERNAL)
def create_scenario(ctx):
    db, b = ctx.db, ctx.body
    season = season_or_400(db, b.get("season_id"))
    name = str(b.get("name") or "").strip()[:160]
    if not name:
        raise bad(_t("Informe o nome do cenário."), {"name": _t("Campo obrigatório.")})
    kind = b.get("kind") or "personalizado"
    if kind not in SCENARIO_KINDS:
        raise bad(_t("Tipo de cenário inválido."))
    risk_id = b.get("risk_id") or None
    if risk_id and not db.one("SELECT 1 FROM risks WHERE id=? AND season_id=? AND deleted_at IS NULL", (risk_id, season["id"])):
        raise bad(_t("Risco inválido."))
    changes = clean_changes(db, season["id"], b.get("changes") or [])
    if b.get("from_risk") and risk_id and not changes:
        # ponto de partida: os efeitos estimados registrados para o risco (dias adicionais em cada atividade afetada)
        for l in db.q("SELECT * FROM risk_links WHERE risk_id=? AND entity_type='activity'", (risk_id,)):
            changes.append({"kind": "extra_days", "activity_id": l["entity_id"], "days": l["est_delay_days"],
                            "note": None if l["est_delay_days"] is not None else _t("Efeito ainda não estimado: informe os dias.").render("pt-BR")})
        changes = clean_changes(db, season["id"], changes)
    ts = now_iso()
    s = {"id": new_id(), "season_id": season["id"], "name": name, "kind": kind, "description": str(b.get("description") or "").strip()[:4000] or None,
         "risk_id": risk_id, "base_scenario_id": b.get("base_scenario_id") or None, "changes": json.dumps(changes), "status": "rascunho",
         "created_at": ts, "updated_at": ts, "created_by": ctx.uid, "version": 1}
    if s["base_scenario_id"] and not db.one("SELECT 1 FROM cpm_scenarios WHERE id=? AND season_id=?", (s["base_scenario_id"], season["id"])):
        s["base_scenario_id"] = None
    db.insert("cpm_scenarios", s)
    audit(ctx, "criar", "cpm_scenarios", s["id"], _t("Cenário criado: {0}", name))
    return _load_scenario(db, s["id"])


@route("PUT", "/api/cpm/scenarios/{id}", allow=INTERNAL)
def update_scenario(ctx):
    db, b = ctx.db, ctx.body
    s = _load_scenario(db, ctx.params["id"])
    if not _can_edit(ctx, s):
        raise forbidden(_t("Somente quem criou o cenário ou a gestão pode alterá-lo."))
    if s["status"] not in ("rascunho", "proposto", "rejeitado"):
        raise conflict(_t("Cenários aprovados ou arquivados não podem ser alterados."), code="cenario_fechado")
    if b.get("version") != s["version"]:
        raise conflict(_t("Este cenário foi alterado por outra pessoa. Recarregue antes de salvar."), code="versao")
    name = str(b.get("name", s["name"]) or "").strip()[:160]
    if not name:
        raise bad(_t("Informe o nome do cenário."), {"name": _t("Campo obrigatório.")})
    kind = b.get("kind", s["kind"])
    if kind not in SCENARIO_KINDS:
        raise bad(_t("Tipo de cenário inválido."))
    changes = clean_changes(db, s["season_id"], b.get("changes", s["changes"]))
    status = "rascunho" if s["status"] == "rejeitado" else s["status"]
    db.ex("UPDATE cpm_scenarios SET name=?, kind=?, description=?, changes=?, status=?, updated_at=?, version=version+1 WHERE id=?",
          (name, kind, str(b.get("description", s["description"]) or "").strip()[:4000] or None, json.dumps(changes), status, now_iso(), s["id"]))
    audit(ctx, "alterar", "cpm_scenarios", s["id"], _t("Cenário alterado: {0}", name), before={"changes": s["changes"]}, after={"changes": changes})
    return _load_scenario(db, s["id"])


@route("POST", "/api/cpm/scenarios/{id}/duplicar", allow=INTERNAL)
def duplicate_scenario(ctx):
    s = _load_scenario(ctx.db, ctx.params["id"])
    ctx.body = {"season_id": s["season_id"], "name": str(ctx.body.get("name") or _t("{0} (cópia)", s["name"]).render())[:160],
                "kind": ctx.body.get("kind") or s["kind"], "description": s["description"], "risk_id": s["risk_id"],
                "changes": s["changes"], "base_scenario_id": s["id"]}
    return create_scenario(ctx)


@route("POST", "/api/cpm/scenarios/{id}/enviar", allow=INTERNAL)
def submit_scenario(ctx):
    s = _load_scenario(ctx.db, ctx.params["id"])
    if not _can_edit(ctx, s) or s["status"] not in ("rascunho", "rejeitado"):
        raise conflict(_t("Só rascunhos podem ser enviados para aprovação."), code="estado")
    ctx.db.ex("UPDATE cpm_scenarios SET status='proposto', updated_at=?, version=version+1 WHERE id=?", (now_iso(), s["id"]))
    audit(ctx, "submeter", "cpm_scenarios", s["id"], _t("Cenário enviado para aprovação: {0}", s["name"]))
    return _load_scenario(ctx.db, s["id"])


@route("POST", "/api/cpm/scenarios/{id}/rejeitar", allow=STAFF)
def reject_scenario(ctx):
    s = _load_scenario(ctx.db, ctx.params["id"])
    if s["status"] not in ("rascunho", "proposto"):
        raise conflict(_t("Este cenário não está aguardando decisão."), code="estado")
    note = str(ctx.body.get("note") or "").strip()[:2000]
    if not note:
        raise bad(_t("Explique o motivo da rejeição."), {"note": _t("Campo obrigatório.")})
    ctx.db.ex("UPDATE cpm_scenarios SET status='rejeitado', decision_note=?, decided_by=?, decided_at=?, updated_at=?, version=version+1 WHERE id=?",
              (note, ctx.uid, now_iso(), now_iso(), s["id"]))
    audit(ctx, "reprovar", "cpm_scenarios", s["id"], _t("Cenário rejeitado: {0}", s["name"]))
    return _load_scenario(ctx.db, s["id"])


@route("POST", "/api/cpm/scenarios/{id}/arquivar", allow=INTERNAL)
def archive_scenario(ctx):
    s = _load_scenario(ctx.db, ctx.params["id"])
    if not _can_edit(ctx, s) or s["status"] == "aprovado":
        raise conflict(_t("Este cenário não pode ser arquivado."), code="estado")
    ctx.db.ex("UPDATE cpm_scenarios SET status='arquivado', updated_at=?, version=version+1 WHERE id=?", (now_iso(), s["id"]))
    audit(ctx, "arquivar", "cpm_scenarios", s["id"], _t("Cenário arquivado: {0}", s["name"]))
    return _load_scenario(ctx.db, s["id"])


@route("DELETE", "/api/cpm/scenarios/{id}", allow=INTERNAL)
def delete_scenario(ctx):
    s = _load_scenario(ctx.db, ctx.params["id"])
    if not _can_edit(ctx, s) or s["status"] == "aprovado":
        raise conflict(_t("Cenários aprovados ficam no histórico e não podem ser excluídos."), code="estado")
    ctx.db.ex("UPDATE cpm_scenarios SET deleted_at=?, version=version+1 WHERE id=?", (now_iso(), s["id"]))
    audit(ctx, "excluir", "cpm_scenarios", s["id"], _t("Cenário excluído: {0}", s["name"]))
    return {"ok": True}


@route("POST", "/api/cpm/scenarios/{id}/aprovar", allow=STAFF)
def approve_scenario(ctx):
    """Aprova o cenário e aplica as mudanças ao planejamento oficial (datas, durações, novas atividades e dependências).
    Tudo ou nada; os valores anteriores ficam guardados em applied_before e no histórico de cada atividade."""
    db = ctx.db
    s = _load_scenario(db, ctx.params["id"])
    if s["status"] not in ("rascunho", "proposto"):
        raise conflict(_t("Este cenário não está aguardando decisão."), code="estado")
    if ctx.body.get("version") != s["version"]:
        raise conflict(_t("Este cenário foi alterado por outra pessoa. Recarregue antes de decidir."), code="versao")
    note = str(ctx.body.get("note") or "").strip()[:2000] or None
    net, base = official(ctx, s["season_id"])
    sim = simulate(ctx, net, base, s)
    if sim.get("error"):
        raise conflict(sim["error"], code="ciclo")
    if sim["summary"]["warnings"]:
        raise conflict(_t("Resolva as pendências do cenário antes de aprová-lo: {0}", " ".join(str(w.render()) for w in sim["summary"]["warnings"])), code="pendencias")
    res = sim["analysis"]
    cal = net.cal
    before = {"activities": {}, "dependencies_removed": [], "dependencies_added": [], "activities_created": []}
    changes = s["changes"]
    dur_changed = {c["activity_id"] for c in changes if c["kind"] in ("extra_days", "set_duration") and not str(c["activity_id"]).startswith("new:")}
    base_by = {a["id"]: a for a in base["activities"]}
    new_ids = {}
    # 1) novas atividades
    for c in changes:
        if c["kind"] != "new_activity":
            continue
        key = "new:" + c["key"]
        a = next(x for x in res["activities"] if x["id"] == key)
        body = {"season_id": s["season_id"], "title": c["title"], "area_id": c.get("area_id"), "owner_id": c.get("owner_id"),
                "planned_start": a["plan"]["es"], "planned_end": a["plan"]["ef"], "duration_days": c.get("duration"),
                "description": c.get("note"), "status": "nao_iniciada", "progress": 0}
        ctx.body = body
        created = ACT.create(ctx, body)
        db.ex("UPDATE activities SET source=? WHERE id=?", ("cenario:" + s["id"], created["id"]))
        new_ids[key] = created["id"]
        before["activities_created"].append(created["id"])
    # 2) datas das atividades existentes que mudaram no cenário (mantém a duração; a linha de base não é tocada)
    for a in res["activities"]:
        if a.get("new") or not a["in_network"]:
            continue
        b0 = base_by.get(a["id"])
        if not b0 or not b0["in_network"] or a["done"]:
            continue
        orig = net.acts[a["id"]]
        d_es = a["plan"]["i"][0] - b0["plan"]["i"][0]
        d_ef = a["plan"]["i"][1] - b0["plan"]["i"][1]
        if not d_es and not d_ef and a["id"] not in dur_changed:
            continue
        body = {"version": orig["version"]}
        if orig["planned_start"]:
            body["planned_start"] = cal.date(cal.index(orig["planned_start"]) + d_es).isoformat()
        if orig["planned_end"]:
            body["planned_end"] = cal.date(cal.index(orig["planned_end"], after=False) + d_ef).isoformat()
        if a["id"] in dur_changed and (orig["duration_days"] is not None or not (orig["planned_start"] and orig["planned_end"])):
            body["duration_days"] = a["duration"]   # duração informada (sem datas completas, a duração não cabe só nas datas)
        before["activities"][a["id"]] = {k: orig.get(k) for k in ("planned_start", "planned_end", "duration_days")}
        if orig["baseline_start"] is None and orig["baseline_end"] is None:
            body["baseline_start"], body["baseline_end"] = orig["planned_start"], orig["planned_end"]
        ctx.body = body
        ctx._skip_parts = True
        ACT.update(ctx, a["id"], body, action="ajuste_cronograma")
    # 3) dependências
    for c in changes:
        if c["kind"] == "remove_dependency":
            dep = db.one("SELECT * FROM dependencies WHERE id=?", (c["dependency_id"],))
            if dep:
                db.ex("DELETE FROM dependencies WHERE id=?", (dep["id"],))
                before["dependencies_removed"].append(dep)
                audit(ctx, "excluir", "dependencies", dep["id"], _t("Dependência removida pelo cenário “{0}”", s["name"]), before=dep)
        elif c["kind"] == "add_dependency":
            p, q = new_ids.get(c["pred"], c["pred"]), new_ids.get(c["succ"], c["succ"])
            if db.one("SELECT 1 FROM dependencies WHERE predecessor_id=? AND successor_id=?", (p, q)):
                continue
            dep = {"id": new_id(), "predecessor_id": p, "successor_id": q, "type": c["type"], "lag_days": c["lag"], "created_by": ctx.uid, "created_at": now_iso()}
            db.insert("dependencies", dep)
            before["dependencies_added"].append(dep["id"])
            audit(ctx, "criar", "dependencies", dep["id"], _t("Dependência criada pelo cenário “{0}”", s["name"]), after=dep)
    result = {"summary": sim["summary"], "applied_at": now_iso(), "official_before": {"plan_finish": base["kpis"]["plan_finish"], "forecast_finish": base["kpis"]["forecast_finish"]}}
    db.ex("UPDATE cpm_scenarios SET status='aprovado', decision_note=?, decided_by=?, decided_at=?, applied_at=?, applied_before=?, applied_result=?, "
          "updated_at=?, version=version+1 WHERE id=?",
          (note, ctx.uid, now_iso(), now_iso(), json.dumps(before, default=str), json.dumps(result, default=str), now_iso(), s["id"]))
    audit(ctx, "aprovar", "cpm_scenarios", s["id"], _t("Cenário aprovado e aplicado ao planejamento: {0} ({1} atividade(s) reprogramada(s), {2} nova(s))",
                                                       s["name"], len(before["activities"]), len(before["activities_created"])))
    return _load_scenario(db, s["id"])


@route("GET", "/api/cpm/compare", allow=INTERNAL)
def compare(ctx):
    sid = ctx.query.get("season_id")
    ids = [x for x in (ctx.query.get("ids") or "").split(",") if x][:6]
    net, base = official(ctx, sid)
    out = [{"id": None, "name": _t("Planejamento oficial"), "kind": "oficial", "plan_finish": base["kpis"]["plan_finish"],
            "forecast_finish": base["kpis"]["forecast_finish"], "forecast_delta_days": 0, "deadline_margin": base["kpis"]["deadline_margin"],
            "critical": base["kpis"]["critical"], "extra_cost": 0, "new_activities": 0, "changed_activities": 0, "warnings": [], "risks": []}]
    for i in ids:
        s = _load_scenario(ctx.db, i)
        if s["season_id"] != sid:
            continue
        sim = simulate(ctx, net, base, s)
        row = {"id": s["id"], "name": s["name"], "kind": s["kind"], "status": s["status"]}
        if sim.get("error"):
            row["error"] = sim["error"]
        else:
            row.update(sim["summary"])
        out.append(row)
    ok = [r for r in out[1:] if not r.get("error") and r.get("forecast_delta_days") is not None]
    best = min(ok, key=lambda r: (r["forecast_delta_days"], r.get("extra_cost") or 0))["id"] if ok else None
    return {"items": out, "best_id": best}
