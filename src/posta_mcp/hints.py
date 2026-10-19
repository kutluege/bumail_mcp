"""Plain-Turkish advice for failures, for users who never see a terminal (Claude Desktop).

A failure is classified into a *kind* (``"auth"``, ``"dns"``, ...) where it happens: the IMAP/
SMTP/credential layers pass ``kind=`` when they raise their own errors (``MailboxError``,
``SmtpError``, ``CredentialsError``), using :func:`classify` on the low-level exception. Later,
:func:`explain` turns that kind into one short Turkish explanation plus what to do, and
:func:`with_hint` puts it in front of the existing English technical detail, so debugging still
works.

The advice texts are fixed strings: they never contain passwords, message content or any value
from the failure. The technical tail is passed through ``logs.redact``.
"""

from __future__ import annotations

import imaplib
import smtplib
import socket
import ssl
from collections.abc import Iterable

from imapclient import exceptions as imap_exc

from posta_mcp.logs import redact

DESKTOP_SETTINGS = "Ayarlar > Uzantılar > Boğaziçi Postanesi"
SETUP_COMMAND = "uvx posta-mcp setup"
_WHERE_TO_FIX = f"Claude Desktop: {DESKTOP_SETTINGS}; Claude Code: `{SETUP_COMMAND}`"
_NO_CHAT_PASSWORD = "Parolanı sohbete yazma."

HINTS: dict[str, str] = {
    "auth": (
        "Giriş başarısız: parola ya da kullanıcı adı hatalı olabilir. Parolanın süresi dolduysa "
        "mail.bogazici.edu.tr/password adresinden değiştir, sonra parolayı burada da güncelle "
        f"({_WHERE_TO_FIX}). {_NO_CHAT_PASSWORD}"
    ),
    "no_password": (
        "Parola bulunamadı. Claude Desktop kullanıyorsan parolanı "
        f"{DESKTOP_SETTINGS} bölümüne gir. Claude Code kullanıyorsan bir terminalde "
        f"`{SETUP_COMMAND}` komutunu çalıştır. {_NO_CHAT_PASSWORD}"
    ),
    "keychain": (
        "Parola, bilgisayarın parola deposundan (anahtar zinciri) okunamadı ya da oraya "
        f"kaydedilemedi. Claude Desktop kullanıyorsan parolanı {DESKTOP_SETTINGS} bölümüne gir. "
        f"Claude Code kullanıyorsan `{SETUP_COMMAND}` komutunu yeniden çalıştır. "
        f"{_NO_CHAT_PASSWORD}"
    ),
    "dns": (
        "Posta sunucusu bulunamadı. Önce internet bağlantını kontrol et. İnternet çalışıyorsa "
        "sunucu adı yanlış olabilir: ayarlardaki e-posta adresinin doğru yazıldığından emin ol "
        "(öğrenciler için ad.soyad@std.bogazici.edu.tr)."
    ),
    "timeout": (
        "Posta sunucusu zamanında yanıt vermedi. İnternet bağlantını kontrol et. Kampüs "
        "dışındaysan BU-VPN'e bağlanıp yeniden dene. Sorun sürerse sunucu geçici olarak "
        "yanıt vermiyor olabilir; biraz sonra tekrar dene."
    ),
    "unreachable": (
        "Posta sunucusuna bağlanılamadı. İnternet bağlantını kontrol et. Kampüs dışındaysan "
        "BU-VPN'e bağlanıp yeniden dene. Sorun sürerse sunucu geçici olarak kapalı olabilir; "
        "biraz sonra tekrar dene."
    ),
    "tls": (
        "Güvenli bağlantı kurulamadı: sunucunun sertifikası doğrulanamadı. Bağlı olduğun ağ "
        "(örneğin kafe, otel ya da ortak Wi-Fi) bağlantının arasına giriyor olabilir. Başka bir "
        "ağdan (örneğin telefonunun internetinden) ya da BU-VPN ile dene. Sertifika "
        "doğrulamasını kapatmaya çalışma: bu güvenli değildir ve bu programda böyle bir ayar "
        "yoktur."
    ),
    "insecure": (
        "Sunucu şifreli bağlantı (STARTTLS) sunmadığı için giriş yapılmadı; parolan şifresiz "
        "gönderilmez. Ayarlardaki sunucu adı ya da bağlantı türü yanlış olabilir."
    ),
    "unsupported_move": (
        "Bu posta sunucusu postaları güvenli biçimde taşımayı (MOVE/UIDPLUS) desteklemiyor. "
        "Başka postaların silinmesine yol açabileceği için taşıma, çöpe atma ve arşivleme "
        "yapılmadı; hiçbir şey değişmedi. Bu bir ayar hatası değil, sunucunun bir sınırlaması. "
        "Bu işlemi posta uygulamandan (örneğin web postasından) yapabilirsin."
    ),
    "recipient": (
        "Sunucu alıcı adresini kabul etmedi; hiçbir şey gönderilmedi. Adresin doğru yazıldığını "
        "kontrol et."
    ),
}

_TECHNICAL_MAX = 300


def classify(exc: BaseException) -> str | None:
    """Kind of a low-level failure (imapclient/imaplib, smtplib, ssl, socket), or None.

    Exceptions that already carry a ``kind`` (MailboxError, SmtpError, CredentialsError) return
    it unchanged.
    """
    if (kind := getattr(exc, "kind", None)) is not None:
        return kind
    if isinstance(exc, imap_exc.LoginError | smtplib.SMTPAuthenticationError):
        return "auth"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "recipient"
    if isinstance(exc, ssl.SSLCertVerificationError):  # also ssl.CertificateError (an alias)
        return "tls"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, TimeoutError):  # socket.timeout is an alias since Python 3.10
        return "timeout"
    if isinstance(exc, imaplib.IMAP4.abort | ConnectionError):  # refused, reset, aborted
        return "unreachable"
    if isinstance(exc, smtplib.SMTPException | ssl.SSLError):
        return None  # a server reply or TLS protocol detail: the technical text says more
    if isinstance(exc, OSError):  # e.g. "network is unreachable", no route to host
        return "unreachable"
    return None


def explain(exc: BaseException) -> str | None:
    """Short Turkish explanation and action for `exc`, or None when there is no specific advice."""
    kind = classify(exc)
    return HINTS.get(kind) if kind else None


def with_hint(exc: BaseException, technical: str, *, fallback: str | None = None) -> str:
    """The Turkish advice (or `fallback`) followed by the redacted English `technical` detail."""
    detail = redact(technical).strip()[:_TECHNICAL_MAX]
    advice = explain(exc) or fallback
    return f"{advice}\n(Teknik ayrıntı: {detail})" if advice else detail


# ---------------------------------------------------------------------- unconfigured server

_FIELD_NAMES = {
    "email": "e-posta adresi",
    "imap.host": "IMAP sunucu adresi",
    "smtp.host": "SMTP sunucu adresi",
}

SETUP_STEPS = (
    f"Claude Desktop kullanıyorsan: {DESKTOP_SETTINGS} bölümünde e-posta adresini (örneğin "
    "ad.soyad@std.bogazici.edu.tr) ve parolanı gir, sonra uzantıyı kapatıp yeniden aç. "
    f"Claude Code kullanıyorsan: bir terminalde `{SETUP_COMMAND}` komutunu çalıştır, sonra "
    f"Claude Code'da `/mcp` ile sunucuyu yeniden bağla. {_NO_CHAT_PASSWORD}"
)


def setup_problem(*, error: BaseException | None = None, missing: Iterable[str] = ()) -> str:
    """Turkish description of why the server cannot use the mailbox yet (value-free).

    `error` is a ConfigError (its message names fields, never values); `missing` are setting
    names from ``Config.missing()``.
    """
    if error is not None:
        detail = redact(str(error)).strip()[:_TECHNICAL_MAX]
        return (
            "Ayarlarda bir sorun var, bu yüzden posta kutuna bağlanılmadı. E-posta adresinin "
            f"doğru yazıldığını kontrol et. (Teknik ayrıntı: {detail})"
        )
    names = [_FIELD_NAMES.get(m, m) for m in missing]
    return f"Kurulum tamamlanmamış: {', '.join(names) or 'gerekli ayarlar'} eksik."
