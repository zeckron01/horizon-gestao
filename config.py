"""Configuração por variáveis de ambiente (ver .env.example). Nenhum segredo fica no código."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Config:
    def __init__(self):
        self.reload()

    def reload(self):
        e = os.environ
        self.DATA_DIR = Path(e.get("HORIZON_DATA_DIR", ROOT / "data")).resolve()
        self.DB_PATH = Path(e.get("HORIZON_DB_PATH", self.DATA_DIR / "horizon.db")).resolve()
        # PostgreSQL (ex.: Neon). Se definido, substitui o arquivo SQLite. Aceita os nomes criados pela integração Vercel+Neon.
        self.DATABASE_URL = (e.get("HORIZON_DATABASE_URL") or e.get("DATABASE_URL_UNPOOLED") or e.get("POSTGRES_URL_NON_POOLING")
                             or e.get("DATABASE_URL") or e.get("POSTGRES_URL") or "").strip()
        self.SERVERLESS = bool(e.get("VERCEL")) or e.get("HORIZON_SERVERLESS") == "1"
        self.BACKUP_DIR = Path(e.get("HORIZON_BACKUP_DIR", self.DATA_DIR / "backups")).resolve()
        self.HOST = e.get("HOST", "0.0.0.0")
        self.PORT = _int("PORT", 8000)
        # "auto": cookie Secure quando a requisição chegou por HTTPS (inclusive atrás de proxy).
        self.COOKIE_SECURE = e.get("HORIZON_COOKIE_SECURE", "auto").lower()
        self.SESSION_HOURS = _int("HORIZON_SESSION_HOURS", 12)
        self.SETUP_TOKEN = e.get("HORIZON_SETUP_TOKEN", "")
        self.JOBS_ENABLED = e.get("HORIZON_JOBS_ENABLED", "1") == "1"
        self.JOBS_INTERVAL_SECONDS = max(5, _int("HORIZON_JOBS_INTERVAL_SECONDS", 900))
        # A Vercel envia "Authorization: Bearer $CRON_SECRET" nas chamadas agendadas quando a variável CRON_SECRET existe.
        self.CRON_SECRET = e.get("HORIZON_CRON_SECRET") or e.get("CRON_SECRET") or ""
        self.BACKUP_KEEP = _int("HORIZON_BACKUP_KEEP", 14)
        self.BACKUP_HOUR = _int("HORIZON_BACKUP_HOUR", 3)
        self.MAX_UPLOAD_MB = _int("HORIZON_MAX_UPLOAD_MB", 10)
        if self.SERVERLESS:
            self.MAX_UPLOAD_MB = min(self.MAX_UPLOAD_MB, 4)   # a Vercel recusa requisições acima de ~4,5 MB
            if "HORIZON_BACKUP_KEEP" not in e:
                self.BACKUP_KEEP = 7                           # backups ficam dentro do banco: guardar menos cópias
        self.PUBLIC_URL = e.get("HORIZON_PUBLIC_URL", "").rstrip("/")
        self.SMTP_HOST = e.get("SMTP_HOST", "")
        self.SMTP_PORT = _int("SMTP_PORT", 587)
        self.SMTP_USER = e.get("SMTP_USER", "")
        self.SMTP_PASSWORD = e.get("SMTP_PASSWORD", "")
        self.SMTP_FROM = e.get("SMTP_FROM", "")
        self.SMTP_TLS = e.get("SMTP_TLS", "starttls").lower()  # starttls | ssl | none
        self.TRUST_PROXY = e.get("HORIZON_TRUST_PROXY", "1") == "1"

    @property
    def smtp_configured(self):
        return bool(self.SMTP_HOST and self.SMTP_FROM)


cfg = Config()
