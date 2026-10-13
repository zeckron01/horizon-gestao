"""Envio de e-mail via SMTP (opcional). Sem SMTP configurado, nada é enviado e isso é informado na interface."""
import smtplib
import ssl
from email.message import EmailMessage

from .config import cfg


def send_mail(to, subject, text):
    if not cfg.smtp_configured:
        return False, "SMTP não configurado"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg.SMTP_FROM, to, subject
    msg.set_content(text)
    try:
        if cfg.SMTP_TLS == "ssl":
            srv = smtplib.SMTP_SSL(cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=15, context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(cfg.SMTP_HOST, cfg.SMTP_PORT, timeout=15)
            if cfg.SMTP_TLS == "starttls":
                srv.starttls(context=ssl.create_default_context())
        with srv:
            if cfg.SMTP_USER:
                srv.login(cfg.SMTP_USER, cfg.SMTP_PASSWORD)
            srv.send_message(msg)
        return True, None
    except Exception as exc:  # erro reportado a quem chamou; nunca derruba a requisição
        return False, f"{type(exc).__name__}: {exc}"
