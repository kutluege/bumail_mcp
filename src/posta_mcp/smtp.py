"""SMTP submission over verified TLS. There is no plaintext path.

* ``security = "ssl"``      -> ``smtplib.SMTP_SSL`` (implicit TLS).
* ``security = "starttls"`` -> plain connect, EHLO, **require** the STARTTLS extension (refuse
  otherwise, never fall back), ``starttls``, EHLO again, and only then log in.

Login happens only on an encrypted channel.  Mail is always sent with ``sendmail`` using the
envelope from ``compose.envelope_recipients`` and the Bcc-free bytes from ``compose.send_bytes``;
``send_message`` is never used because it would put Bcc on the wire.

Errors are turned into :class:`SmtpError` with short messages that never contain the password
(or anything else from the credentials), so they are safe to show to the client.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from posta_mcp import compose, credentials, tls
from posta_mcp.config import Config
from posta_mcp.logs import redact

TIMEOUT = 30.0


class SmtpError(RuntimeError):
    """A failed or refused send. The message is short and secret-free."""


def _server_text(exc: smtplib.SMTPResponseException) -> str:
    raw = exc.smtp_error
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return f"{exc.smtp_code} {redact(text).strip()[:120]}"


def _open(config: Config, context: ssl.SSLContext, timeout: float) -> smtplib.SMTP:
    host, port = config.smtp.host, config.smtp.port
    if config.smtp.security == "ssl":
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        client.ehlo()
        if not client.has_extn("starttls"):
            raise SmtpError(
                "The SMTP server does not offer STARTTLS; refusing to send without encryption."
            )
        client.starttls(context=context)
        client.ehlo()  # capabilities (incl. AUTH) must be re-read after the upgrade
    except BaseException:
        _close(client)
        raise
    return client


def _close(client: smtplib.SMTP) -> None:
    try:
        client.quit()
    except Exception:  # noqa: BLE001 - best effort; the connection may already be gone
        try:
            client.close()
        except Exception:  # noqa: BLE001, S110
            pass


def send_raw(config: Config, sender: str, recipients: list[str], data: bytes) -> list[str]:
    """Submit exactly ``data`` for ``recipients`` as ``sender``.

    Returns the recipients the server refused while accepting the others (empty normally).
    Raises :class:`SmtpError` if nothing was sent.
    """
    if not recipients:
        raise SmtpError("No recipients; nothing was sent.")
    try:
        password = credentials.get_password(config.login)
    except credentials.CredentialsError as exc:
        raise SmtpError(str(exc)) from None
    where = f"{config.smtp.host}:{config.smtp.port}"
    context = tls.make_ssl_context(config.ca_file)
    client: smtplib.SMTP | None = None
    try:
        client = _open(config, context, TIMEOUT)
        client.login(config.login, password)
        refused = client.sendmail(sender, recipients, data)
    except SmtpError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise SmtpError(f"SMTP login was rejected by {where} ({exc.smtp_code}).") from None
    except smtplib.SMTPRecipientsRefused as exc:
        detail = ", ".join(f"{addr} ({code})" for addr, (code, _msg) in exc.recipients.items())
        raise SmtpError(f"The server refused every recipient: {detail}.") from None
    except smtplib.SMTPSenderRefused as exc:
        raise SmtpError(f"The server refused the sender address ({exc.smtp_code}).") from None
    except smtplib.SMTPResponseException as exc:
        raise SmtpError(f"SMTP server {where} replied {_server_text(exc)}.") from None
    except smtplib.SMTPNotSupportedError:
        raise SmtpError(f"SMTP server {where} does not support authentication.") from None
    except (ssl.SSLError, ssl.CertificateError) as exc:
        reason = getattr(exc, "reason", None) or type(exc).__name__
        raise SmtpError(
            f"TLS handshake with {where} failed ({reason}); the certificate must be valid for "
            "the host (set POSTA_CA_FILE for a private CA)."
        ) from None
    except smtplib.SMTPServerDisconnected:
        raise SmtpError(f"SMTP server {where} closed the connection.") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise SmtpError(f"SMTP to {where} failed ({type(exc).__name__}).") from None
    finally:
        if client is not None:
            _close(client)
    return sorted(refused)


def send(config: Config, msg: EmailMessage) -> list[str]:
    """Send ``msg`` (Bcc recipients get it, but no Bcc header is transmitted).

    Returns recipients refused by the server when others were accepted. Raises
    :class:`SmtpError` on failure.
    """
    return send_raw(config, config.email, compose.envelope_recipients(msg), compose.send_bytes(msg))
