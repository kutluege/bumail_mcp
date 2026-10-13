"""TLS context. Verification is always on; there is deliberately no way to turn it off."""

from __future__ import annotations

import ssl


def make_ssl_context(ca_file: str | None = None) -> ssl.SSLContext:
    """Default verifying context; `ca_file` (tests) is *added* to the trusted CAs."""
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context
