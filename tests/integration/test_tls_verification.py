"""Non-negotiable 3: TLS verification is always on; a CA file is only ever *added*."""

from __future__ import annotations

import smtplib
import socket
import ssl

import imapclient
import pytest


def _plain_default_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()  # system trust store only, no test CA
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    return ctx


def test_imaps_without_ca_file_fails(greenmail):
    with pytest.raises(ssl.SSLCertVerificationError):
        imapclient.IMAPClient(
            greenmail.host, port=greenmail.imaps_port, ssl=True,
            ssl_context=_plain_default_context(), timeout=15,
        )  # fmt: skip


def test_smtps_without_ca_file_fails(greenmail):
    with pytest.raises(ssl.SSLCertVerificationError):
        smtplib.SMTP_SSL(
            greenmail.host, greenmail.smtps_port, context=_plain_default_context(), timeout=15
        )


def test_imaps_with_ca_file_succeeds(greenmail):
    c = imapclient.IMAPClient(
        greenmail.host, port=greenmail.imaps_port, ssl=True,
        ssl_context=greenmail.ssl_context(), timeout=15,
    )  # fmt: skip
    try:
        c.login(greenmail.student, greenmail.password(greenmail.student))
        assert c.has_capability("UIDPLUS")
    finally:
        c.logout()


def test_smtps_with_ca_file_succeeds(greenmail):
    with smtplib.SMTP_SSL(
        greenmail.host, greenmail.smtps_port, context=greenmail.ssl_context(), timeout=15
    ) as s:
        assert s.ehlo()[0] == 250


def test_ip_address_in_san_is_accepted(greenmail):
    ctx = greenmail.ssl_context()
    with (
        socket.create_connection(("127.0.0.1", greenmail.imaps_port), timeout=15) as raw,
        ctx.wrap_socket(raw, server_hostname="127.0.0.1") as s,
    ):
        assert s.version() is not None


@pytest.mark.parametrize("port_attr", ["imaps_port", "smtps_port"])
def test_hostname_not_in_san_fails_even_with_ca(greenmail, port_attr):
    """The CA is trusted, but the name we dial is not in the certificate: must still fail."""
    ctx = greenmail.ssl_context()
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
    with (
        socket.create_connection(("127.0.0.1", getattr(greenmail, port_attr)), timeout=15) as raw,
        pytest.raises(ssl.SSLCertVerificationError) as exc,
    ):
        ctx.wrap_socket(raw, server_hostname="mail.not-in-san.test")
    assert "mismatch" in str(exc.value).lower() or "hostname" in str(exc.value).lower()


def test_product_tls_context_is_strict(greenmail):
    """posta_mcp.tls.make_ssl_context: verify on, CA file added, wrong/missing CA fails."""
    tls = pytest.importorskip("posta_mcp.tls")
    ctx = tls.make_ssl_context(str(greenmail.ca_file))
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    with (
        socket.create_connection((greenmail.host, greenmail.imaps_port), timeout=15) as raw,
        ctx.wrap_socket(raw, server_hostname=greenmail.host) as s,
    ):
        assert s.version() is not None
    no_ca = tls.make_ssl_context(None)
    assert no_ca.verify_mode == ssl.CERT_REQUIRED and no_ca.check_hostname
    with (
        socket.create_connection((greenmail.host, greenmail.imaps_port), timeout=15) as raw,
        pytest.raises(ssl.SSLCertVerificationError),
    ):
        no_ca.wrap_socket(raw, server_hostname=greenmail.host)
