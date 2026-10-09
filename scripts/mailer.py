#!/usr/bin/env python3
"""Manda el mail del digest por SMTP (solo stdlib; reemplaza a una GitHub Action de terceros).

Todo entra por variables de entorno, así el workflow nunca interpola texto externo
(títulos de PRs, etc.) dentro de un script de shell:

  SMTP_PASSWORD  App Password de Gmail (si falta, avisa y sale sin error)
  MAIL_USER      cuenta que autentica y envía
  MAIL_TO        destinatario (por defecto, MAIL_USER)
  EMAIL_SUBJECT  asunto
  EMAIL_HTML     cuerpo HTML
  SUBJECT_PREFIX / HTML_PREFIX   opcionales (banner de test)
  DIFF_URL / RUN_URL             opcionales, van al pie
"""

import html
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formataddr


def html_to_text(s):
    """Versión de texto plano para clientes sin HTML: conserva los links como 'texto (url)'."""
    s = re.sub(r'(?is)<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|li|h3|div)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def footer_html(diff_url="", run_url=""):
    links = []
    if diff_url:
        links.append(f'<a href="{html.escape(diff_url)}">See what changed</a>')
    if run_url:
        links.append(f'<a href="{html.escape(run_url)}">Run log</a>')
    if not links:
        return ""
    return f'<p style="font-size:12px;color:#888">{" · ".join(links)}</p>'


def build_message(*, subject, html_body, sender, to, sender_name="GitHub Actions"):
    msg = EmailMessage()
    msg["Subject"] = " ".join(subject.split())  # sin saltos de línea: no hay header injection
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = to
    msg.set_content(html_to_text(html_body))
    msg.add_alternative(html_body, subtype="html")
    return msg


def send(msg, user, password, host="smtp.gmail.com", port=465, smtp_ssl=smtplib.SMTP_SSL):
    with smtp_ssl(host, port, timeout=30) as server:
        server.login(user, password)
        server.send_message(msg)


def main(env=None, smtp_ssl=smtplib.SMTP_SSL):
    env = os.environ if env is None else env
    password = env.get("SMTP_PASSWORD", "")
    if not password:
        print("SMTP_PASSWORD no está configurado — no se manda el mail.")
        return 0
    user = env["MAIL_USER"]
    body = env.get("HTML_PREFIX", "") + env.get("EMAIL_HTML", "")
    body += footer_html(env.get("DIFF_URL", ""), env.get("RUN_URL", ""))
    msg = build_message(
        subject=env.get("SUBJECT_PREFIX", "") + env.get("EMAIL_SUBJECT", ""),
        html_body=body,
        sender=user,
        to=env.get("MAIL_TO") or user,
    )
    send(msg, user, password, smtp_ssl=smtp_ssl)
    print(f"Mail enviado: {msg['Subject']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
