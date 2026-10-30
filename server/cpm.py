"""Método do Caminho Crítico (CPM) — cálculo puro, sem banco de dados (testado em tests/test_cpm.py).

Convenções (documentadas também na tela):
- O tempo é contado em "períodos" do calendário do projeto: dias corridos ou dias úteis (dias da semana de trabalho,
  sem os feriados cadastrados).
- ES (início mais cedo) é o índice do PRIMEIRO período de trabalho da atividade; EF (término mais cedo) = ES + duração
  é o índice do período SEGUINTE ao último dia de trabalho. Assim uma atividade de 1 dia que começa em 10/06 tem
  ES = 10/06 e termina em 10/06 (a data exibida de término é EF − 1). Marcos têm duração 0 (início = término).
- LS = LF − duração; folga total = LS − ES = LF − EF; folga livre = quanto a atividade pode atrasar sem atrasar o
  início mais cedo de nenhuma sucessora imediata.
- Dependências: FS (término→início), SS (início→início), FF (término→término) e SF (início→término), com defasagem
  (lag) em períodos, positiva ou negativa.
"""
from datetime import date, timedelta

DEP_TYPES = ("FS", "SS", "FF", "SF")


class CycleError(Exception):
    def __init__(self, cycle):
        super().__init__("ciclo")
        self.cycle = cycle


# ----------------------------------------------------------------------------- calendário
def d(s):
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


class Calendar:
    """Converte datas em índices de período e vice-versa.
    mode='corridos': todo dia conta; mode='uteis': só os dias da semana em `workdays` (0 = segunda) que não são feriados."""

    def __init__(self, origin, mode="corridos", workdays=(0, 1, 2, 3, 4), holidays=()):
        self.mode = "uteis" if mode == "uteis" else "corridos"
        self.workdays = set(workdays) if workdays else {0, 1, 2, 3, 4}
        self.holidays = {d(h) for h in holidays or ()}
        o = d(origin)
        if self.mode == "uteis":
            while not self.is_work(o):
                o += timedelta(days=1)
        self.origin = o
        self._dates = [o]          # índice -> data (só dias de trabalho)
        self._index = {o: 0}

    def is_work(self, x):
        if self.mode == "corridos":
            return True
        x = d(x)
        return x.weekday() in self.workdays and x not in self.holidays

    def _extend(self, upto_index=None, upto_date=None):
        guard = 0
        while (upto_index is not None and len(self._dates) <= upto_index) or (upto_date is not None and self._dates[-1] < upto_date):
            nxt = self._dates[-1] + timedelta(days=1)
            while not self.is_work(nxt):
                nxt += timedelta(days=1)
                guard += 1
                if guard > 200000:
                    raise ValueError("calendário sem dias de trabalho")
            self._index[nxt] = len(self._dates)
            self._dates.append(nxt)

    def index(self, x, after=True):
        """Índice do período que contém a data. Em dias úteis, um dia não útil vai para o próximo dia útil
        (after=True, usado para inícios) ou para o anterior (after=False, usado para términos)."""
        x = d(x)
        if self.mode == "corridos":
            return (x - self.origin).days
        if x < self.origin:   # antes da origem: conta para trás
            n, y = 0, self.origin
            while y > x:
                y -= timedelta(days=1)
                if self.is_work(y):
                    n -= 1
            if not self.is_work(x) and after:
                n += 1
            return n
        self._extend(upto_date=x)
        y = x
        if after:
            while not self.is_work(y):
                y += timedelta(days=1)
            self._extend(upto_date=y)
            return self._index[y]
        while not self.is_work(y) and y > self.origin:
            y -= timedelta(days=1)
        return self._index.get(y, 0)

    def date(self, i):
        if self.mode == "corridos":
            return self.origin + timedelta(days=i)
        if i < 0:
            y, n = self.origin, 0
            while n > i:
                y -= timedelta(days=1)
                if self.is_work(y):
                    n -= 1
            return y
        self._extend(upto_index=i)
        return self._dates[i]

    def duration(self, start, end):
        """Períodos de trabalho entre duas datas, contando as duas (início e término no mesmo dia = 1)."""
        s, e = d(start), d(end)
        if e < s:
            return None
        if self.mode == "corridos":
            return (e - s).days + 1
        n, y = 0, s
        while y <= e:
            if self.is_work(y):
                n += 1
            y += timedelta(days=1)
        return n

    def start_date(self, es):
        return self.date(es)

    def finish_date(self, es, ef):
        """Data do último dia de trabalho (EF − 1); para marcos (duração 0) é a própria data de início."""
        return self.date(ef - 1) if ef > es else self.date(es)


# ----------------------------------------------------------------------------- ordem topológica e ciclos
def topo_order(node_ids, edges):
    """Ordem topológica estável (Kahn). Se houver ciclo, levanta CycleError com os nós de um ciclo."""
    succ, indeg = {n: [] for n in node_ids}, {n: 0 for n in node_ids}
    for e in edges:
        succ[e["pred"]].append(e["succ"])
        indeg[e["succ"]] += 1
    ready = [n for n in node_ids if indeg[n] == 0]
    order = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for s in succ[n]:
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(s)
    if len(order) != len(node_ids):
        raise CycleError(find_cycle([n for n in node_ids if indeg[n] > 0], succ))
    return order


def find_cycle(candidates, succ):
    cand = set(candidates)
    color = {}
    stack_path = []

    def dfs(n):
        color[n] = 1
        stack_path.append(n)
        for s in succ.get(n, []):
            if s not in cand:
                continue
            if color.get(s) == 1:
                return stack_path[stack_path.index(s):] + [s]
            if not color.get(s):
                r = dfs(s)
                if r:
                    return r
        color[n] = 2
        stack_path.pop()
        return None

    for n in candidates:
        if not color.get(n):
            r = dfs(n)
            if r:
                return r
    return list(candidates)


def would_cycle(node_ids, edges, pred, succ):
    try:
        topo_order(list(node_ids), list(edges) + [{"pred": pred, "succ": succ, "type": "FS", "lag": 0}])
        return False
    except CycleError:
        return True


# ----------------------------------------------------------------------------- CPM
def _start_bound(e, p, dur_s):
    """Menor ES permitido para a sucessora, dado o ES/EF da predecessora `p`."""
    t, lag = e["type"], e["lag"]
    if t == "FS":
        return p["ef"] + lag
    if t == "SS":
        return p["es"] + lag
    if t == "FF":
        return p["ef"] + lag - dur_s
    return p["es"] + lag - dur_s   # SF


def _finish_bound(e, s, dur_p):
    """Maior LF permitido para a predecessora, dado o LS/LF da sucessora `s`."""
    t, lag = e["type"], e["lag"]
    if t == "FS":
        return s["ls"] - lag
    if t == "SS":
        return s["ls"] - lag + dur_p
    if t == "FF":
        return s["lf"] - lag
    return s["lf"] - lag + dur_p   # SF


def edge_slack(e, p, s):
    """Folga da ligação: quanto a predecessora pode atrasar antes de empurrar a sucessora (0 = ligação determinante)."""
    t, lag = e["type"], e["lag"]
    if t == "FS":
        return s["es"] - (p["ef"] + lag)
    if t == "SS":
        return s["es"] - (p["es"] + lag)
    if t == "FF":
        return s["ef"] - (p["ef"] + lag)
    return s["ef"] - (p["es"] + lag)


def compute(nodes, edges, start_index=0, finish_ref=None, max_paths=30):
    """Passadas de avanço e retrocesso.

    nodes: {id: {"dur": int >= 0, "min_es": int|None (não iniciar antes de), "fixed_es": int|None (início real, não se move),
                 "min_ef": int|None (término não antes de — usado na previsão de atividades em andamento)}}
    edges: [{"pred", "succ", "type" in FS/SS/FF/SF, "lag": int}]
    finish_ref: data final de referência (índice) para o retrocesso; padrão = maior EF (término mais cedo do projeto).
    Retorna {"nodes": {id: {es, ef, ls, lf, tf, ff, dur, critical}}, "finish": int, "paths": [[ids]], "order": [...]}.
    """
    ids = list(nodes)
    order = topo_order(ids, edges)
    preds, succs = {n: [] for n in ids}, {n: [] for n in ids}
    for e in edges:
        preds[e["succ"]].append(e)
        succs[e["pred"]].append(e)
    r = {}
    for n in order:
        nd = nodes[n]
        dur = max(0, int(nd.get("dur") or 0))
        if nd.get("fixed_es") is not None:
            es = nd["fixed_es"]
        else:
            es = start_index
            if nd.get("min_es") is not None:
                es = max(es, nd["min_es"])
            for e in preds[n]:
                es = max(es, _start_bound(e, r[e["pred"]], dur))
        ef = es + dur
        if nd.get("min_ef") is not None and nd["min_ef"] > ef:
            ef = nd["min_ef"]
        r[n] = {"es": es, "ef": ef, "dur": ef - es}
    finish = max((x["ef"] for x in r.values()), default=start_index)
    ref = finish if finish_ref is None else finish_ref
    for n in reversed(order):
        x = r[n]
        lf = ref
        for e in succs[n]:
            lf = min(lf, _finish_bound(e, r[e["succ"]], x["dur"]))
        x["lf"], x["ls"] = lf, lf - x["dur"]
        x["tf"] = x["ls"] - x["es"]
    for n in ids:
        x = r[n]
        if succs[n]:
            x["ff"] = min(edge_slack(e, x, r[e["succ"]]) for e in succs[n])
        else:
            x["ff"] = ref - x["ef"]
        x["critical"] = x["tf"] <= 0 if finish_ref is None else x["tf"] <= (ref - finish)
    # caminhos críticos: sequências de atividades críticas ligadas por ligações determinantes (folga da ligação = 0)
    crit = {n for n in ids if r[n]["critical"]}
    drive = {n: [e["succ"] for e in succs[n] if e["succ"] in crit and edge_slack(e, r[n], r[e["succ"]]) == 0] for n in crit}
    has_in = {s for n in crit for s in drive[n]}
    starts = [n for n in order if n in crit and n not in has_in]
    paths, truncated = [], False

    def walk(n, acc):
        nonlocal truncated
        if len(paths) >= max_paths:
            truncated = True
            return
        nxt = drive[n]
        if not nxt:
            if r[n]["ef"] == finish or finish_ref is not None:
                paths.append(acc)
            return
        for s in nxt:
            walk(s, acc + [s])

    for s in starts:
        walk(s, [s])
    return {"nodes": r, "finish": finish, "ref": ref, "paths": paths, "paths_truncated": truncated, "order": order}
