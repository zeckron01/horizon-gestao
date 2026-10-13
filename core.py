"""Núcleo da API: erros, contexto, rotas, validação, CRUD genérico, auditoria e configurações."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .db import new_id, now_iso

ROLES = ("admin", "gestor", "membro", "juiz")
STAFF = ("admin", "gestor")
INTERNAL = ("admin", "gestor", "membro")
ROLE_LABELS = {"admin": "Administrador", "gestor": "Gestor do time", "membro": "Integrante", "juiz": "Juiz (somente leitura)"}


# ----------------------------------------------------------------------------- erros
class ApiError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def bad(message, fields=None, code="validacao"):
    return ApiError(422, code, message, **({"fields": fields} if fields else {}))


def forbidden(message="Você não tem permissão para realizar esta operação."):
    return ApiError(403, "sem_permissao", message)


def not_found(what="Registro"):
    return ApiError(404, "nao_encontrado", f"{what} não encontrado(a).")


def conflict(message, **extra):
    return ApiError(409, extra.pop("code", "conflito"), message, **extra)


class Raw:
    """Resposta binária (downloads)."""

    def __init__(self, data, mime, filename=None, inline=False):
        self.data, self.mime, self.filename, self.inline = data, mime, filename, inline


# ----------------------------------------------------------------------------- contexto e rotas
class Ctx:
    def __init__(self, db, user=None, params=None, query=None, body=None, files=None, ip=None, method="GET", path=""):
        self.db, self.user = db, user
        self.params, self.query, self.body = params or {}, query or {}, body or {}
        self.files, self.ip, self.method, self.path = files or [], ip, method, path

    @property
    def uid(self):
        return self.user["id"] if self.user else None

    @property
    def role(self):
        return self.user["role"] if self.user else None

    @property
    def is_staff(self):
        return self.role in STAFF

    def qint(self, name, default=None, lo=None, hi=None):
        v = self.query.get(name)
        if v in (None, ""):
            return default
        try:
            v = int(v)
        except ValueError:
            raise bad(f"Parâmetro '{name}' inválido.")
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v


ROUTES = []


def route(method, path, allow=None, public=False, judge_ok=False, tx=None):
    """Registra uma rota. `allow`: perfis autorizados (padrão: GET = todos autenticados; escrita = admin/gestor).
    O perfil juiz nunca executa métodos de escrita, a menos que a rota declare `judge_ok` (ex.: sair, trocar a própria senha)."""
    if allow is None:
        allow = ROLES if method == "GET" else STAFF
    rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "$")

    def deco(fn):
        ROUTES.append({"method": method, "path": path, "rx": rx, "fn": fn, "allow": tuple(allow),
                       "public": public, "judge_ok": judge_ok, "tx": (method != "GET") if tx is None else tx})
        return fn

    return deco


def match_route(method, path):
    found_path = False
    for r in ROUTES:
        m = r["rx"].match(path)
        if m:
            found_path = True
            if r["method"] == method:
                return r, m.groupdict()
    if found_path:
        raise ApiError(405, "metodo_nao_permitido", "Método não permitido para este endereço.")
    raise ApiError(404, "nao_encontrado", "Endereço não encontrado.")


# ----------------------------------------------------------------------------- configurações
DEFAULT_SETTINGS = {
    "team_name": "Horizon Escuderia",
    "team_subtitle": "STEM Racing — SESI São Miguel do Oeste/SC",
    "timezone": "America/Sao_Paulo",
    "date_format": "DD/MM/AAAA",
    "currency": "BRL",
    "default_season_id": None,
    "theme": {"primary": "#147d14", "accent": "#0f172a", "mode": "claro", "background_file_id": None, "logo_file_id": None},
    "alerts": {"delivery_days": 7, "event_first_days": 30, "event_interval_days": 7,
               "approval_reminder_days": 3, "risk_stale_days": 14, "overdue_repeat_days": 7},
    "risk_scale": {
        "size": 5,
        "probability_labels": ["Raro", "Improvável", "Possível", "Provável", "Muito provável"],
        "impact_labels": ["Muito baixo", "Baixo", "Médio", "Alto", "Muito alto"],
        "bands": [
            {"name": "Baixo", "max": 4, "color": "#3f9142", "hint": "risco pouco preocupante"},
            {"name": "Moderado", "max": 9, "color": "#c99a06", "hint": "precisa ser acompanhado"},
            {"name": "Alto", "max": 15, "color": "#d9690f", "hint": "precisa de ação preventiva"},
            {"name": "Crítico", "max": 25, "color": "#c22f2f", "hint": "precisa de atenção prioritária"},
        ],
    },
    "catalogs": {
        "activity_categories": ["Initiation Process", "Project Schedule", "Budget and Resource Management",
                                "Roles and Responsibilities", "Team & Stakeholder Communications",
                                "Risk Management", "Monitoring & Controlling", "Entrega do cronograma", "Entregável (RACI)"],
        "priorities": ["Baixa", "Média", "Alta", "Crítica"],
        "units": ["Unidade", "Peça", "Peças", "Camisetas", "Banner", "Manual", "Portfólios", "Hora", "Serviço", "Kg", "Metro"],
        "expense_categories": ["Fabricação", "Materiais", "Identidade visual", "Impressão", "Uniformes", "Transporte",
                               "Inscrição", "Alimentação", "Serviços", "Outros"],
        "event_types": ["Competição", "Torneio", "Feira", "Reunião", "Apresentação", "Visita a patrocinador", "Outro"],
        "stakeholder_types": ["Patrocinador", "Apoiador", "Fornecedor", "Instituição de ensino", "Comunidade", "Outro"],
        "contribution_types": ["Financeira", "Material", "Serviço", "Institucional", "Mista"],
        "sponsorship_levels": ["Diamante", "Ouro", "Prata", "Bronze", "Apoio"],
        "payment_methods": ["Pix", "Transferência", "Boleto", "Cartão", "Dinheiro", "Outro"],
        "funding_sources": ["Caixa da equipe", "Patrocínio", "SESI/SENAI", "Campanha", "Doação", "Outro"],
        "counterpart_kinds": ["Exposição da marca", "Divulgação em redes sociais", "Presença em evento",
                              "Agradecimento institucional", "Outro"],
        "risk_categories": ["Cronograma", "Comunicação", "Técnico", "Financeiro", "Pessoas", "Segurança", "Fornecedores", "Marketing", "Outro"],
        "acquisition_types": [
            {"key": "proprio", "label": "Compra com recursos próprios", "is_cash": True},
            {"key": "patrocinio_dinheiro", "label": "Compra com recursos de patrocínio", "is_cash": True},
            {"key": "item_patrocinado", "label": "Item recebido integralmente por patrocínio", "is_cash": False},
            {"key": "doacao", "label": "Doação de material", "is_cash": False},
            {"key": "servico_apoio", "label": "Serviço recebido como apoio", "is_cash": False},
        ],
    },
    "judge_view": {"finance_summary": True, "finance_detail": False, "contacts": False,
                   "modules": {"dashboard": True, "escopo": True, "eap": True, "gantt": True, "planejamento": True,
                               "evidencias": True, "stakeholders": True, "financeiro": True, "riscos": True, "raci": True,
                               "calendario": True, "relatorios": True}},
    "evidence_weights": {"entregas": 25, "marcos": 10, "prazos": 20, "metas": 15, "evidencias": 10, "progresso": 10, "qualidade": 10},
    "governance": {"require_approval_to_complete": False, "reapprove_on_change": True},
    "custom_fields": {"activities": []},
    "report_templates": [],
    "uploads": {"allowed_ext": ["pdf", "png", "jpg", "jpeg", "webp", "gif", "xlsx", "xls", "csv", "docx", "doc",
                                "pptx", "ppt", "txt", "zip", "mp4", "stl", "step", "dxf"]},
    # preferências individuais (escopo "user")
    "ui": {"mode": None, "font_scale": 100, "large_font": False, "reduce_motion": False},
    "notify": {"email": False, "kinds": {}},
}
USER_SCOPED = {"ui", "notify"}
ADMIN_ONLY_SETTINGS = {"judge_view", "governance", "uploads"}


def get_setting(db, key, user_id=None):
    default = DEFAULT_SETTINGS.get(key)
    val = None
    cache = getattr(db, "scache", None)   # cache válido só durante uma requisição (ver app._run)
    if cache is not None:
        if user_id and key in USER_SCOPED:
            if user_id not in cache:
                cache[user_id] = {r["key"]: r["value"] for r in db.q("SELECT key, value FROM settings WHERE scope='user' AND user_id=?", (user_id,))}
            if key in cache[user_id]:
                val = json.loads(cache[user_id][key])
        if val is None:
            if "" not in cache:
                cache[""] = {r["key"]: r["value"] for r in db.q("SELECT key, value FROM settings WHERE scope='global' AND user_id=''")}
            if key in cache[""]:
                val = json.loads(cache[""][key])
    else:
        if user_id and key in USER_SCOPED:
            row = db.one("SELECT value FROM settings WHERE key=? AND scope='user' AND user_id=?", (key, user_id))
            if row:
                val = json.loads(row["value"])
        if val is None:
            row = db.one("SELECT value FROM settings WHERE key=? AND scope='global' AND user_id=''", (key,))
            if row:
                val = json.loads(row["value"])
    if val is None:
        return json.loads(json.dumps(default))
    if isinstance(default, dict) and isinstance(val, dict):
        merged = json.loads(json.dumps(default))
        for k, v in val.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged
    return val


def set_setting(db, key, value, user_id=None, by=None):
    scope = "user" if user_id else "global"
    if getattr(db, "scache", None) is not None:
        db.scache = {}
    db.ex("INSERT INTO settings (key, scope, user_id, value, updated_at, updated_by) VALUES (?,?,?,?,?,?) "
          "ON CONFLICT(key, scope, user_id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
          (key, scope, user_id or "", json.dumps(value, ensure_ascii=False), now_iso(), by))


# ----------------------------------------------------------------------------- datas e dinheiro
def team_tz(db):
    try:
        return ZoneInfo(get_setting(db, "timezone") or "America/Sao_Paulo")
    except Exception:
        return ZoneInfo("America/Sao_Paulo")


def today_local(db, now=None):
    """Data de hoje no fuso da equipe ('AAAA-MM-DD')."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(team_tz(db)).date().isoformat()


def d(s):
    return date.fromisoformat(s)


def add_days(s, n):
    return (d(s) + timedelta(days=n)).isoformat()


def days_between(a, b):
    return (d(b) - d(a)).days


def fmt_date(s):
    if not s:
        return "—"
    try:
        return d(s[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return s


def to_cents(v, label="Valor"):
    if v is None or v == "":
        return None
    try:
        if isinstance(v, str):
            s = v.strip().replace("R$", "").replace(" ", "")
            if "," in s:
                s = s.replace(".", "").replace(",", ".")
            dec = Decimal(s)
        else:
            dec = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise bad(f"{label}: informe um valor monetário válido.")
    if not dec.is_finite():
        raise bad(f"{label}: informe um valor monetário válido.")
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(c):
    return None if c is None else float(Decimal(c) / Decimal(100))


def brl(c):
    if c is None:
        return "—"
    neg = c < 0
    c = abs(int(c))
    s = f"{c // 100:,}".replace(",", ".") + f",{c % 100:02d}"
    return ("-" if neg else "") + "R$ " + s


# ----------------------------------------------------------------------------- auditoria
def audit(ctx, action, entity_type, entity_id=None, summary=None, before=None, after=None):
    if before is not None and after is not None:
        changed = [k for k in after if before.get(k) != after.get(k) and k not in ("updated_at", "version")]
        before = {k: before.get(k) for k in changed}
        after = {k: after.get(k) for k in changed}
        if not changed and action == "alterar":
            return
    u = ctx.user or {}
    ctx.db.ex("INSERT INTO audit_log (at, user_id, user_name, action, entity_type, entity_id, summary, before, after, ip) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (now_iso(), u.get("id"), u.get("name"), action, entity_type, entity_id, summary,
               json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
               json.dumps(after, ensure_ascii=False, default=str) if after is not None else None, ctx.ip))


# ----------------------------------------------------------------------------- validação de campos
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RX = re.compile(r"^\d{2}:\d{2}$")
_EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_COLOR_RX = re.compile(r"^#[0-9a-fA-F]{6}$")


class F:
    """Definição de campo: tipo, rótulo e regras de validação aplicadas no servidor."""

    def __init__(self, kind, label, required=False, max=None, min=None, options=None, ref=None, default=None, filter=False, readonly=False):
        self.kind, self.label, self.required = kind, label, required
        self.max, self.min, self.options, self.ref = max, min, options, ref
        self.default, self.filter, self.readonly = default, filter, readonly

    def col(self, name):
        return name + "_cents" if self.kind == "money" else name

    def parse(self, db, name, v):
        L = self.label
        if isinstance(v, str):
            v = v.strip()
        if v is None or v == "" or (self.kind == "json" and v in ([], {}) and not self.required):
            if self.required:
                raise bad(f"{L}: campo obrigatório.", {name: "Campo obrigatório."})
            return None if self.kind not in ("bool",) else 0
        k = self.kind
        try:
            if k in ("text", "long", "email", "color", "time"):
                if not isinstance(v, str):
                    v = str(v)
                v = v.replace("\x00", "")
                mx = self.max or (200 if k == "text" else 20000 if k == "long" else 200)
                if len(v) > mx:
                    raise bad(f"{L}: máximo de {mx} caracteres.", {name: f"Máximo de {mx} caracteres."})
                if k == "email" and not _EMAIL_RX.match(v):
                    raise bad(f"{L}: e-mail inválido.", {name: "E-mail inválido."})
                if k == "color" and not _COLOR_RX.match(v):
                    raise bad(f"{L}: cor inválida (use o formato #RRGGBB).", {name: "Cor inválida."})
                if k == "time" and not _TIME_RX.match(v):
                    raise bad(f"{L}: horário inválido (HH:MM).", {name: "Horário inválido."})
                return v.lower() if k == "email" else v
            if k == "date":
                if not isinstance(v, str) or not _DATE_RX.match(v):
                    raise ValueError
                date.fromisoformat(v)
                return v
            if k == "int":
                if isinstance(v, bool) or (isinstance(v, float) and v != int(v)):
                    raise ValueError
                v = int(v)
                if (self.min is not None and v < self.min) or (self.max is not None and v > self.max):
                    raise bad(f"{L}: informe um número entre {self.min} e {self.max}.", {name: "Fora do intervalo permitido."})
                return v
            if k == "real":
                v = float(str(v).replace(",", ".")) if isinstance(v, str) else float(v)
                if v != v or v in (float("inf"), float("-inf")) or (self.min is not None and v < self.min):
                    raise ValueError
                return v
            if k == "money":
                c = to_cents(v, L)
                if c < (self.min if self.min is not None else 0):
                    raise bad(f"{L}: o valor não pode ser negativo.", {name: "Valor negativo."})
                if c > 10 ** 12:
                    raise bad(f"{L}: valor acima do limite permitido.", {name: "Valor muito alto."})
                return c
            if k == "bool":
                return 1 if v in (True, 1, "1", "true", "sim") else 0
            if k == "enum":
                if v not in self.options:
                    raise bad(f"{L}: opção inválida.", {name: "Opção inválida."})
                return v
            if k == "ref":
                if not isinstance(v, str):
                    raise ValueError
                extra = " AND deleted_at IS NULL" if self.ref != "movements" else ""
                if not db.one(f"SELECT 1 FROM {self.ref} WHERE id=?{extra}", (v,)):
                    raise bad(f"{L}: registro vinculado não encontrado.", {name: "Registro vinculado não encontrado."})
                return v
            if k == "json":
                s = json.dumps(v, ensure_ascii=False)
                if len(s) > 200000:
                    raise bad(f"{L}: conteúdo muito extenso.")
                return s
        except ApiError:
            raise
        except (ValueError, TypeError):
            raise bad(f"{L}: valor inválido.", {name: "Valor inválido."})
        raise RuntimeError(f"tipo de campo desconhecido: {k}")

    def out(self, v):
        if self.kind == "money":
            return from_cents(v)
        if self.kind == "bool":
            return bool(v)
        if self.kind == "json":
            return json.loads(v) if v else None
        return v

    def meta(self):
        m = {"kind": self.kind, "label": self.label, "required": self.required}
        if self.options:
            m["options"] = list(self.options)
        if self.ref:
            m["ref"] = self.ref
        if self.readonly:
            m["readonly"] = True
        return m


def check_order(data, a, b, la, lb):
    if data.get(a) and data.get(b) and data[a] > data[b]:
        raise bad(f"{la} não pode ser posterior a {lb}.", {b: f"Deve ser igual ou posterior a {la}."})


# ----------------------------------------------------------------------------- CRUD genérico
RESOURCES = {}


class Resource:
    """CRUD com validação, permissão, concorrência otimista (version), exclusão lógica e auditoria."""

    def __init__(self, name, table, label, fields, *, season=False, soft=True, archive=True, write=STAFF,
                 order="created_at", search=(), refs=(), base_filter=None):
        self.name, self.table, self.label, self.fields = name, table, label, fields
        self.season, self.soft, self.archive, self.write = season, soft, archive, write
        self.order, self.search, self.refs = order, search, refs
        self.base_filter = base_filter
        RESOURCES[name] = self

    # ganchos (sobrescritos por instância)
    def validate(self, ctx, data, existing):
        pass

    def after_save(self, ctx, row, existing):
        pass

    def expand(self, ctx, rows):
        return rows

    def can_write(self, ctx, existing):
        return ctx.role in self.write

    def before_delete(self, ctx, existing):
        pass

    # ---------------------------------------------------------------- leitura
    def raw(self, db, id):
        extra = " AND deleted_at IS NULL" if self.soft else ""
        return db.one(f"SELECT * FROM {self.table} WHERE id=?{extra}", (id,))

    def ser(self, row):
        out = {}
        for k, v in row.items():
            if k.endswith("_cents"):
                out[k[:-6]] = from_cents(v)
            elif k in self.fields:
                out[k] = self.fields[k].out(v)
            elif k in ("needs_review",):
                out[k] = bool(v)
            elif k == "custom":
                out[k] = json.loads(v) if v else {}
            elif k not in ("deleted_at", "data"):
                out[k] = v
        return out

    def get(self, ctx, id):
        row = self.raw(ctx.db, id)
        if not row:
            raise not_found(self.label)
        return self.expand(ctx, [self.ser(row)])[0]

    def list(self, ctx, where=None, params=None, limit=None):
        q = ctx.query
        conds, ps = [], []
        if self.soft:
            conds.append("deleted_at IS NULL")
        if self.archive and q.get("archived") not in ("1", "all"):
            conds.append("archived_at IS NULL")
        elif self.archive and q.get("archived") == "1":
            conds.append("archived_at IS NOT NULL")
        if self.season and q.get("season_id"):
            conds.append("season_id = ?")
            ps.append(q["season_id"])
        for name, f in self.fields.items():
            if f.filter and q.get(name) not in (None, ""):
                if q[name] == "__null__":
                    conds.append(f"{f.col(name)} IS NULL")
                else:
                    conds.append(f"{f.col(name)} = ?")
                    ps.append(q[name])
        if q.get("q") and self.search:
            like = "%" + q["q"].replace("%", r"\%").replace("_", r"\_") + "%"
            conds.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in self.search) + ")")
            ps += [like] * len(self.search)
        if self.base_filter:
            conds.append(self.base_filter)
        if where:
            conds.append(where)
            ps += list(params or [])
        w = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = ctx.db.val(f"SELECT COUNT(*) FROM {self.table}{w}", ps)
        lim = limit or ctx.qint("limit", 1000, 1, 5000)
        off = ctx.qint("offset", 0, 0)
        rows = ctx.db.q(f"SELECT * FROM {self.table}{w} ORDER BY {self.order} LIMIT ? OFFSET ?", ps + [lim, off])
        return {"items": self.expand(ctx, [self.ser(r) for r in rows]), "total": total, "limit": lim, "offset": off}

    # ---------------------------------------------------------------- escrita
    def parse(self, ctx, body, existing=None):
        data = {}
        for name, f in self.fields.items():
            if f.readonly:
                continue
            if name in body:
                data[f.col(name)] = f.parse(ctx.db, name, body[name])
            elif existing is None:
                if f.required and f.default is None:
                    raise bad(f"{f.label}: campo obrigatório.", {name: "Campo obrigatório."})
                if f.default is not None:
                    data[f.col(name)] = f.default
        if existing is None and self.season and not data.get("season_id"):
            raise bad("Temporada: campo obrigatório.", {"season_id": "Campo obrigatório."})
        return data

    def create(self, ctx, body, extra=None, quiet=False):
        if not quiet and not self.can_write(ctx, None):
            raise forbidden()
        data = self.parse(ctx, body)
        if extra:
            data.update(extra)
        self.validate(ctx, data, None)
        ts = now_iso()
        row = {"id": new_id(), **data, "created_at": ts, "updated_at": ts, "created_by": ctx.uid}
        ctx.db.insert(self.table, row)
        saved = self.raw(ctx.db, row["id"])
        self.after_save(ctx, saved, None)
        saved = self.raw(ctx.db, row["id"])
        audit(ctx, "criar", self.name, row["id"], self.title_of(saved), after=self.ser(saved))
        return self.expand(ctx, [self.ser(saved)])[0]

    def title_of(self, row):
        return row.get("title") or row.get("name") or row.get("description") or row.get("code") or self.label

    def update(self, ctx, id, body, extra=None, quiet=False, action="alterar"):
        existing = self.raw(ctx.db, id)
        if not existing:
            raise not_found(self.label)
        if not quiet and not self.can_write(ctx, existing):
            raise forbidden()
        version = body.get("version")
        if not isinstance(version, int):
            raise bad("Informe a versão do registro (version) para evitar sobrescritas.", code="versao_obrigatoria")
        data = self.parse(ctx, body, existing)
        if extra:
            data.update(extra)
        merged = {**existing, **data}
        self.validate(ctx, merged, existing)
        data = {k: merged[k] for k in merged if k in data or merged[k] != existing.get(k)}
        data = {k: v for k, v in data.items() if k not in ("id", "created_at", "created_by", "version")}
        data["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in data)
        cur = ctx.db.ex(f"UPDATE {self.table} SET {sets}, version=version+1 WHERE id=? AND version=?",
                        list(data.values()) + [id, version])
        if cur.rowcount == 0:
            raise conflict("Este registro foi alterado por outra pessoa enquanto você editava. "
                           "Revise a versão atual antes de salvar novamente.", code="conflito_versao",
                           current=self.expand(ctx, [self.ser(existing)])[0])
        saved = self.raw(ctx.db, id)
        self.after_save(ctx, saved, existing)
        saved = self.raw(ctx.db, id)
        audit(ctx, action, self.name, id, self.title_of(saved), before=self.ser(existing), after=self.ser(saved))
        return self.expand(ctx, [self.ser(saved)])[0]

    def set_archived(self, ctx, id, flag):
        existing = self.raw(ctx.db, id)
        if not existing:
            raise not_found(self.label)
        if not self.can_write(ctx, existing):
            raise forbidden()
        ctx.db.ex(f"UPDATE {self.table} SET archived_at=?, updated_at=?, version=version+1 WHERE id=?",
                  (now_iso() if flag else None, now_iso(), id))
        audit(ctx, "arquivar" if flag else "desarquivar", self.name, id, self.title_of(existing))
        return self.get(ctx, id)

    def references(self, db, id):
        """Vínculos que impedem a exclusão: lista de (rótulo, quantidade)."""
        found = []
        for table, col, label, *cond in self.refs:
            extra = f" AND {cond[0]}" if cond else ""
            n = db.val(f"SELECT COUNT(*) FROM {table} WHERE {col}=?{extra}", (id,))
            if n:
                found.append({"label": label, "count": n})
        return found

    def remove(self, ctx, id):
        existing = self.raw(ctx.db, id)
        if not existing:
            raise not_found(self.label)
        if not self.can_write(ctx, existing):
            raise forbidden()
        self.before_delete(ctx, existing)
        refs = self.references(ctx.db, id)
        if refs:
            desc = ", ".join(f"{r['count']} {r['label']}" for r in refs)
            raise conflict(f"Não é possível excluir: existem registros vinculados ({desc}). "
                           "Desvincule-os ou arquive este registro para preservar o histórico.",
                           code="vinculos_existentes", references=refs, can_archive=self.archive)
        if self.soft:
            ctx.db.ex(f"UPDATE {self.table} SET deleted_at=?, version=version+1 WHERE id=?", (now_iso(), id))
        else:
            ctx.db.ex(f"DELETE FROM {self.table} WHERE id=?", (id,))
        audit(ctx, "excluir", self.name, id, self.title_of(existing), before=self.ser(existing))
        return {"ok": True}

    def meta(self):
        return {"label": self.label, "fields": {k: f.meta() for k, f in self.fields.items()}}

    def register(self, base=None, methods=("list", "get", "create", "update", "delete", "archive"), read=ROLES, member_write=False):
        base = base or f"/api/{self.name}"
        w = INTERNAL if member_write else self.write
        if "list" in methods:
            route("GET", base, allow=read)(lambda ctx: self.list(ctx))
        if "get" in methods:
            route("GET", base + "/{id}", allow=read)(lambda ctx: self.get(ctx, ctx.params["id"]))
        if "create" in methods:
            route("POST", base, allow=w)(lambda ctx: self.create(ctx, ctx.body))
        if "update" in methods:
            route("PUT", base + "/{id}", allow=w)(lambda ctx: self.update(ctx, ctx.params["id"], ctx.body))
        if "delete" in methods:
            route("DELETE", base + "/{id}", allow=w)(lambda ctx: self.remove(ctx, ctx.params["id"]))
        if "archive" in methods and self.archive:
            route("POST", base + "/{id}/arquivar", allow=w)(lambda ctx: self.set_archived(ctx, ctx.params["id"], True))
            route("POST", base + "/{id}/desarquivar", allow=w)(lambda ctx: self.set_archived(ctx, ctx.params["id"], False))
        return self


def judge_view(ctx):
    return get_setting(ctx.db, "judge_view")


def judge_module_guard(ctx, module):
    """Juízes só acessam os módulos liberados pelo administrador."""
    if ctx.role == "juiz" and not judge_view(ctx)["modules"].get(module, False):
        raise forbidden("Este módulo não está disponível para o perfil de juiz.")


def user_names(db):
    return {r["id"]: r["name"] for r in db.q("SELECT id, name FROM users")}


def notify(db, user_id, kind, title, body, entity_type, entity_id, dedupe_key):
    """Cria uma notificação interna; a chave de deduplicação impede repetições (UNIQUE no banco)."""
    cur = db.ex("INSERT OR IGNORE INTO notifications (id, user_id, kind, title, body, entity_type, entity_id, dedupe_key, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (new_id(), user_id, kind, title, body, entity_type, entity_id, dedupe_key, now_iso()))
    return cur.rowcount == 1


def staff_ids(db):
    return [r["id"] for r in db.q("SELECT id FROM users WHERE role IN ('admin','gestor') AND active=1 AND deleted_at IS NULL AND password_hash IS NOT NULL")]


def internal_login_ids(db):
    return [r["id"] for r in db.q("SELECT id FROM users WHERE role<>'juiz' AND active=1 AND deleted_at IS NULL AND password_hash IS NOT NULL")]
