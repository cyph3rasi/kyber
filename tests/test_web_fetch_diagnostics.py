from __future__ import annotations

from kyber.agent.tools.web import _failure_hint, _tls_verify_source, _tls_verify_target


def test_failure_hint_dns_resolution() -> None:
    hint = _failure_hint(
        error_type="ConnectError",
        error_text="[Errno -3] Temporary failure in name resolution",
        dns_ok=False,
        scheme="https",
    )
    assert "DNS resolution failed" in hint


def test_failure_hint_tls_cert() -> None:
    hint = _failure_hint(
        error_type="ConnectError",
        error_text="[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
        dns_ok=True,
        scheme="https",
    )
    assert "TLS certificate verification failed" in hint


def test_failure_hint_http_status() -> None:
    hint = _failure_hint(
        error_type="HTTPStatusError",
        error_text="Client error",
        dns_ok=True,
        scheme="https",
        status_code=404,
    )
    assert "404" in hint


def test_tls_verify_source_uses_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/custom-ca.pem")
    assert _tls_verify_source() == "env:SSL_CERT_FILE"


def test_tls_verify_target_uses_env_override(monkeypatch) -> None:
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/ca.pem")
    # clear cached resolver so env update is respected
    _tls_verify_target.cache_clear()
    assert _tls_verify_target() == "/tmp/ca.pem"
