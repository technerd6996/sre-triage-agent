"""Notification adapters: SMTP, Amazon SES, Amazon SNS. Settings are read when used, so a saved change applies at once.
Message bodies and passwords are never logged."""
import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from app.ports.errors import PortError, Unavailable, Unsupported
from app.ports.manifest import Field, Manifest
from app.ports.notifications import CAP_BROADCAST, CAP_RECIPIENTS

log = logging.getLogger("triage.notify")

SMTP_MANIFEST = Manifest(
    port="notifications", provider="smtp", title="SMTP email", capabilities=frozenset({CAP_RECIPIENTS}),
    fields=(Field("notifications.smtp_host", "SMTP host", required=True),
            Field("notifications.smtp_port", "SMTP port", "number", default="587"),
            Field("notifications.smtp_user", "SMTP user"),
            Field("notifications.smtp_password", "SMTP password", "secret"),
            Field("notifications.smtp_from", "SMTP sender")))
SES_MANIFEST = Manifest(
    port="notifications", provider="ses", title="Amazon SES", capabilities=frozenset({CAP_RECIPIENTS}),
    fields=(Field("notifications.ses_from", "SES sender", required=True),
            Field("notifications.aws_region", "AWS region")))
SNS_MANIFEST = Manifest(
    port="notifications", provider="sns", title="Amazon SNS topic", capabilities=frozenset({CAP_BROADCAST}),
    fields=(Field("notifications.sns_topic_arn", "SNS topic ARN", required=True),
            Field("notifications.aws_region", "AWS region")))


def _need(label: str, value: str) -> str:
    if not value:
        raise Unsupported(f"{label} is not set (Configuration pane)")
    return value


def _wrap(e: Exception) -> PortError:
    log.warning("notification failed: %s", type(e).__name__)
    return Unavailable(f"{type(e).__name__}: {str(e)[:120]}")


class SmtpNotifier:
    provider, capabilities = "smtp", SMTP_MANIFEST.capabilities

    def __init__(self, **_):
        pass

    def _host(self) -> str:
        return _need("SMTP host", os.getenv("SMTP_HOST", ""))

    def _port(self) -> int:
        return int(os.getenv("SMTP_PORT") or 587)

    def check(self) -> str:
        host = self._host()
        try:
            with smtplib.SMTP(host, self._port(), timeout=5) as s:
                s.noop()
        except (OSError, smtplib.SMTPException) as e:
            raise _wrap(e) from e
        return "SMTP server reachable (no message sent)"

    def send(self, to: list, cc: list, subject: str, body: str) -> str:
        host = self._host()
        if not to:
            raise Unsupported("No recipients")
        msg = EmailMessage()
        msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER", "")
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid()
        msg.set_content(body)
        try:
            with smtplib.SMTP(host, self._port(), timeout=20) as s:
                s.starttls()
                if os.getenv("SMTP_USER"):
                    s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
                s.send_message(msg, to_addrs=list(to) + list(cc))
        except (OSError, smtplib.SMTPException) as e:
            raise _wrap(e) from e
        return msg["Message-ID"]


class SesNotifier:
    provider, capabilities = "ses", SES_MANIFEST.capabilities

    def __init__(self, **_):
        pass

    def check(self) -> str:
        _need("SES sender", os.getenv("SES_FROM", ""))
        return "SES settings present (no message sent)"

    def send(self, to: list, cc: list, subject: str, body: str) -> str:
        sender = _need("SES sender", os.getenv("SES_FROM", ""))
        if not to:
            raise Unsupported("No recipients")
        try:
            import boto3
            client = boto3.client("ses", region_name=os.getenv("AWS_REGION") or None)
            dest = {"ToAddresses": list(to)}
            if cc:
                dest["CcAddresses"] = list(cc)
            r = client.send_email(Source=sender, Destination=dest,
                                  Message={"Subject": {"Data": subject[:200]}, "Body": {"Text": {"Data": body}}})
        except Exception as e:                                   # noqa: BLE001
            raise _wrap(e) from e
        return r["MessageId"]


class SnsNotifier:
    """SNS delivers to the topic's subscribers, so the To and Cc lists are not used."""
    provider, capabilities = "sns", SNS_MANIFEST.capabilities

    def __init__(self, **_):
        pass

    def check(self) -> str:
        _need("SNS topic ARN", os.getenv("SNS_TOPIC_ARN", ""))
        return "SNS settings present (no message sent)"

    def send(self, to: list, cc: list, subject: str, body: str) -> str:
        topic = _need("SNS topic ARN", os.getenv("SNS_TOPIC_ARN", ""))
        try:
            import boto3
            client = boto3.client("sns", region_name=os.getenv("AWS_REGION") or None)
            r = client.publish(TopicArn=topic, Subject=subject[:100], Message=body)
        except Exception as e:                                   # noqa: BLE001
            raise _wrap(e) from e
        return r["MessageId"]