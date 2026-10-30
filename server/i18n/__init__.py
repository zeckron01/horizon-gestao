"""Internacionalização do servidor (pt-BR / en).

Princípio: os textos do sistema são escritos em português no código e embrulhados em _t("texto {0}", arg).
_t devolve um Msg, que é um str com o valor em português e que guarda a chave e os argumentos. Por isso:
- tudo o que é gravado no banco (notificações, notas de revisão, histórico, configurações) continua em português;
- comparações e regras de negócio enxergam o texto em português, como antes;
- a tradução só acontece na saída: respostas JSON (localize), mensagens de erro e documentos exportados.
Dados digitados pelos usuários são str comuns e nunca são traduzidos.
"""
import contextvars
import json
import re
from contextlib import contextmanager
from pathlib import Path

LANGS = ("pt-BR", "en")
_LANG = contextvars.ContextVar("hz_lang", default="pt-BR")
_PH = re.compile(r"\{(\d+)\}")
_EN_PATH = Path(__file__).with_name("en.json")
try:
    EN = json.loads(_EN_PATH.read_text(encoding="utf-8"))
except FileNotFoundError:   # dicionário ainda não gerado: tudo fica em português
    EN = {}


def norm_lang(v):
    v = (v or "").strip()
    if v.lower().startswith("en"):
        return "en"
    if v.lower().startswith("pt"):
        return "pt-BR"
    return None


def get_lang():
    return _LANG.get()


def set_lang(lang):
    _LANG.set(norm_lang(lang) or "pt-BR")


@contextmanager
def lang_ctx(lang):
    tok = _LANG.set(norm_lang(lang) or "pt-BR")
    try:
        yield
    finally:
        _LANG.reset(tok)


def _fmt(tpl, args, lang):
    if not args:
        return tpl
    def rep(m):
        i = int(m.group(1))
        if i >= len(args):
            return m.group(0)
        v = args[i]
        if isinstance(v, Msg):
            return v.render(lang)
        return "" if v is None else str(v)
    return _PH.sub(rep, tpl)


class Msg(str):
    """Texto do sistema: vale como o português; .render(lang) devolve a versão no idioma pedido."""

    def __new__(cls, key, args=()):
        o = super().__new__(cls, _fmt(key, args, "pt-BR"))
        o.key, o.args = key, tuple(args)
        return o

    def render(self, lang=None):
        lang = lang or _LANG.get()
        key = getattr(self, "key", str(self))
        args = getattr(self, "args", ())
        tpl = EN.get(key, key) if lang == "en" else key
        return _fmt(tpl, args, lang)

    def __reduce__(self):
        return (Msg, (getattr(self, "key", str(self)), getattr(self, "args", ())))


def _t(key, *args):
    return Msg(key, args)


def localize(obj, lang=None):
    """Troca, em uma estrutura de resposta, cada Msg pelo texto no idioma pedido. str comuns (dados) ficam intactos."""
    lang = lang or _LANG.get()
    if lang != "en":
        return obj   # o valor de um Msg já é o português
    if isinstance(obj, Msg):
        return obj.render(lang)
    if isinstance(obj, dict):
        return {k: localize(v, lang) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [localize(v, lang) for v in obj]
    return obj


# ---------------------------------------------------------------- textos já gravados (ex.: notificações geradas pela rotina)
_TEMPLATES = None


def _templates():
    global _TEMPLATES
    if _TEMPLATES is None:
        out = []
        for k in EN:
            if "{" in k and _PH.search(k):
                parts = _PH.split(k)
                rx, order = "", []
                for n, p in enumerate(parts):
                    if n % 2 == 0:
                        rx += re.escape(p)
                    else:
                        rx += "(.+?)"
                        order.append(int(p))
                out.append((re.compile("^" + rx + "$", re.S), k, order))
        out.sort(key=lambda x: -len(x[1]))
        _TEMPLATES = out
    return _TEMPLATES


def tx(text, lang=None):
    """Tradução de um texto do sistema que foi gravado em português (exato ou por modelo com {n}).
    Use só em campos que o próprio sistema gera (ex.: título de notificação); nunca em dados digitados."""
    lang = lang or _LANG.get()
    if lang != "en" or not text or not isinstance(text, str):
        return text
    if isinstance(text, Msg):
        return text.render(lang)
    if text in EN:
        return EN[text]
    for rx, key, order in _templates():
        m = rx.match(text)
        if m:
            args = [""] * (max(order) + 1 if order else 0)
            for pos, idx in enumerate(order):
                args[idx] = m.group(pos + 1)   # partes variáveis (nomes, títulos) ficam como gravadas
            return _fmt(EN[key], args, lang)
    return text


def rnd(x):
    """Msg -> texto no idioma atual; qualquer outro valor fica como está (usado ao juntar partes de uma mensagem)."""
    return x.render() if isinstance(x, Msg) else x


def tr_now(key, *args):
    """Texto já no idioma atual (para saídas que não são gravadas, como documentos exportados)."""
    return Msg(key, args).render()


# ---------------------------------------------------------------- formatação conforme o idioma (só apresentação)
_EN_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date_l(iso):
    """AAAA-MM-DD -> 29/10/2026 (pt-BR) ou Oct 29, 2026 (en). Não usa fuso: o dia exibido é o dia gravado."""
    if not iso:
        return "—"
    try:
        y, m, d = iso[:10].split("-")
        if _LANG.get() == "en":
            return f"{_EN_MON[int(m) - 1]} {int(d)}, {y}"
        return f"{d}/{m}/{y}"
    except (ValueError, IndexError):
        return iso


def fmt_num_l(v, decimals=2):
    s = f"{v:,.{decimals}f}"
    return s if _LANG.get() == "en" else s.replace(",", "X").replace(".", ",").replace("X", ".")


def money_l(v):
    """Valor em reais: só a apresentação muda (R$ 1.234,56 / BRL 1,234.56); o valor não é convertido."""
    if v is None:
        return "—"
    return ("BRL " if _LANG.get() == "en" else "R$ ") + fmt_num_l(v, 2)


def fmt_dt_l(dt):
    """datetime já no fuso da equipe -> 29/10/2026 14:05 ou Oct 29, 2026 14:05."""
    if _LANG.get() == "en":
        return f"{_EN_MON[dt.month - 1]} {dt.day}, {dt.year} {dt:%H:%M}"
    return dt.strftime("%d/%m/%Y %H:%M")
