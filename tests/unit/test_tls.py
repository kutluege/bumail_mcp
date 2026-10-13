import ssl

import pytest

from posta_mcp.tls import make_ssl_context


@pytest.mark.parametrize("use_ca_file", [False, True])
def test_verification_always_on(use_ca_file):
    # any readable PEM bundle works to exercise the "CA file added" path
    ca = ssl.get_default_verify_paths().cafile if use_ca_file else None
    if use_ca_file and not ca:
        pytest.skip("no system CA file to load on this platform")
    ctx = make_ssl_context(ca)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_no_way_to_disable_verification():
    import inspect

    assert list(inspect.signature(make_ssl_context).parameters) == ["ca_file"]


def test_bad_ca_file_raises(tmp_path):
    bogus = tmp_path / "ca.pem"
    bogus.write_text("not a certificate")
    with pytest.raises(ssl.SSLError):
        make_ssl_context(str(bogus))
