"""通知：邮件（SMTP）+ 群机器人 Webhook（企业微信 / 飞书 / 钉钉 / Slack）。

两者都是可选的，未配置时静默跳过；发送失败只记日志，不影响业务流程。
"""
import logging
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import parseaddr

import requests

log = logging.getLogger('cmcgood')

PREFIX = '[代码仓管理] '


def send_email(cfg, to, subject, body):
    to = [addr for addr in dict.fromkeys(to or []) if addr]
    if not to or not cfg.get('SMTP_HOST') or not cfg.get('MAIL_FROM'):
        return False
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = Header(PREFIX + subject, 'utf-8')
    msg['From'] = cfg['MAIL_FROM']
    msg['To'] = ', '.join(to)
    try:
        host, port = cfg['SMTP_HOST'], cfg['SMTP_PORT']
        if cfg['SMTP_SECURITY'] == 'ssl':
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            if cfg['SMTP_SECURITY'] == 'starttls':
                server.starttls(context=ssl.create_default_context())
        with server:
            if cfg.get('SMTP_USER'):
                server.login(cfg['SMTP_USER'], cfg['SMTP_PASSWORD'])
            server.sendmail(parseaddr(cfg['MAIL_FROM'])[1], to, msg.as_string())
        return True
    except Exception:
        log.exception('发送邮件失败: %s -> %s', subject, to)
        return False


def send_webhook(cfg, text):
    url = cfg.get('WEBHOOK_URL')
    if not url:
        return False
    text = PREFIX + text
    kind = cfg.get('WEBHOOK_TYPE', 'wecom')
    if kind == 'feishu':
        payload = {'msg_type': 'text', 'content': {'text': text}}
    elif kind == 'slack':
        payload = {'text': text}
    else:  # wecom / dingtalk 格式相同
        payload = {'msgtype': 'text', 'text': {'content': text}}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception:
        log.exception('发送 Webhook 失败')
        return False


def link(cfg, path):
    """拼接站点链接；未配置 SITE_URL 时返回空串。"""
    return cfg['SITE_URL'] + path if cfg.get('SITE_URL') else ''
