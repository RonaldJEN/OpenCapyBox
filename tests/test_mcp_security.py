"""Security boundary tests for user-configured MCP endpoints and secrets."""

import socket
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.api.schemas.mcp import UserMcpServerCreate
from src.api.services.mcp_security import (
    McpSecurityError,
    credential_payload,
    credential_headers,
    decrypt_credential,
    encrypt_credential,
    mcp_url_without_query,
    normalize_personal_mcp_network_policy,
    resolve_mcp_endpoint,
    sanitize_mcp_exception,
    validate_mcp_headers,
    validate_mcp_url,
)
from src.api.services import secret_crypto


def test_personal_mcp_requires_public_https():
    with pytest.raises(McpSecurityError, match="HTTPS"):
        validate_mcp_url("http://example.com/mcp")
    with pytest.raises(McpSecurityError, match="公网"):
        validate_mcp_url("https://127.0.0.1/mcp")
    with pytest.raises(McpSecurityError, match="本机"):
        validate_mcp_url("https://localhost/mcp")


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::a9fe:a9fe",  # NAT64 -> 169.254.169.254 cloud metadata
        "64:ff9b::a00:1",  # NAT64 -> 10.0.0.1 private network
        "64:ff9b:1::a9fe:a9fe",  # local-use NAT64 prefix
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "2002:7f00:1::",  # 6to4 -> 127.0.0.1
    ],
)
def test_personal_mcp_rejects_ipv6_transition_addresses(address):
    with pytest.raises(McpSecurityError, match="公网"):
        validate_mcp_url(f"https://[{address}]/mcp")


def test_personal_mcp_accepts_ordinary_public_ipv6_literal():
    assert (
        validate_mcp_url("https://[2606:4700:4700::1111]/mcp")
        == "https://[2606:4700:4700::1111]/mcp"
    )


def test_personal_network_policy_normalizes_suffixes_and_cidrs():
    policy = normalize_personal_mcp_network_policy(
        ["Company.CC.com.", "company.cc.com"],
        ["10.20.0.0/16", "fd00:1234::/48"],
        version=7,
    )

    assert policy.domain_suffixes == ("company.cc.com",)
    assert policy.cidrs == ("10.20.0.0/16", "fd00:1234::/48")
    assert policy.version == 7


def test_personal_policy_domain_suffix_is_label_bounded(monkeypatch):
    policy = normalize_personal_mcp_network_policy(["company.cc.com"], [])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.1.5", 80)),
        ],
    )

    allowed = resolve_mcp_endpoint(
        "http://mcp.company.cc.com/mcp",
        personal_network_policy=policy,
    )
    assert allowed.network_authorization is not None
    assert allowed.network_authorization.matched_domain_suffix == "company.cc.com"

    with pytest.raises(McpSecurityError, match="白名单"):
        resolve_mcp_endpoint(
            "http://evilcompany.cc.com/mcp",
            personal_network_policy=policy,
        )


def test_personal_policy_domain_match_does_not_authorize_http_for_public_address(monkeypatch):
    policy = normalize_personal_mcp_network_policy(["company.cc.com"], [])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
        ],
    )

    with pytest.raises(McpSecurityError, match="CIDR"):
        resolve_mcp_endpoint(
            "http://mcp.company.cc.com/mcp",
            personal_network_policy=policy,
        )

    endpoint = resolve_mcp_endpoint(
        "https://mcp.company.cc.com/mcp",
        personal_network_policy=policy,
    )
    assert endpoint.network_authorization is None


def test_personal_policy_cidr_allows_private_https_and_http(monkeypatch):
    policy = normalize_personal_mcp_network_policy([], ["10.20.0.0/16"])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.9.8", 80)),
        ],
    )

    endpoint = resolve_mcp_endpoint(
        "http://mcp.internal.example/mcp",
        personal_network_policy=policy,
    )
    assert endpoint.network_authorization is not None
    assert endpoint.network_authorization.matched_cidrs == ("10.20.0.0/16",)


@pytest.mark.parametrize(
    "cidr",
    [
        "0.0.0.0/0",
        "100.100.100.200/32",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
    ],
)
def test_personal_policy_rejects_overwide_or_permanently_blocked_cidrs(cidr):
    with pytest.raises(McpSecurityError):
        normalize_personal_mcp_network_policy([], [cidr])


def test_personal_domain_policy_never_allows_loopback(monkeypatch):
    policy = normalize_personal_mcp_network_policy(["company.cc.com"], [])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(McpSecurityError, match="永久禁止"):
        resolve_mcp_endpoint(
            "https://mcp.company.cc.com/mcp",
            personal_network_policy=policy,
        )


def test_personal_domain_policy_never_allows_cloud_metadata(monkeypatch):
    policy = normalize_personal_mcp_network_policy(["company.cc.com"], [])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.100.100.200", 443)),
        ],
    )
    with pytest.raises(McpSecurityError, match="永久禁止"):
        resolve_mcp_endpoint(
            "https://mcp.company.cc.com/mcp",
            personal_network_policy=policy,
        )


def test_official_policy_can_explicitly_allow_private_http():
    assert validate_mcp_url(
        "http://127.0.0.1:8080/mcp",
        allow_private_network=True,
        allow_insecure_http=True,
    ) == "http://127.0.0.1:8080/mcp"


def test_mcp_url_rejects_embedded_credentials_and_fragments():
    with pytest.raises(McpSecurityError, match="用户名或密码"):
        validate_mcp_url("https://user:pass@example.com/mcp")
    with pytest.raises(McpSecurityError, match="fragment"):
        validate_mcp_url("https://example.com/mcp#secret")


def test_public_mcp_url_projection_removes_query_credentials():
    assert mcp_url_without_query(
        "https://mcp.example.com:8443/v1/tools?api_key=TOPSECRET&tenant=one"
    ) == "https://mcp.example.com:8443/v1/tools"
    assert mcp_url_without_query("not-a-valid-endpoint?api_key=TOPSECRET") == ""


def test_mcp_exception_sanitizer_removes_urls_credentials_and_controls():
    endpoint = "https://mcp.example.com/v1/tools?api_key=TOPSECRET"
    error = RuntimeError(
        f"request {endpoint} failed with X-Key=HEADERSECRET; "
        "Authorization: Bearer REFLECTEDTOKEN\r\n\x00injected "
        + ("x" * 1000)
    )

    safe = sanitize_mcp_exception(
        error,
        url=endpoint,
        headers={"X-Key": "HEADERSECRET"},
        include_exception_type=True,
    )

    assert "TOPSECRET" not in safe
    assert "HEADERSECRET" not in safe
    assert "REFLECTEDTOKEN" not in safe
    assert endpoint not in safe
    assert "\r" not in safe
    assert "\n" not in safe
    assert "\x00" not in safe
    assert "REDACTED" in safe
    assert safe.startswith("RuntimeError: ")
    assert len(safe) <= 500


def test_custom_headers_reject_transport_control_headers():
    with pytest.raises(McpSecurityError, match="Host"):
        validate_mcp_headers({"Host": "internal.example"})
    with pytest.raises(McpSecurityError, match="Accept-Encoding"):
        validate_mcp_headers({"Accept-Encoding": "gzip"})
    with pytest.raises(McpSecurityError, match="Proxy-Connection"):
        validate_mcp_headers({"Proxy-Connection": "keep-alive"})
    with pytest.raises(McpSecurityError, match="换行"):
        validate_mcp_headers({"X-Test": "ok\r\nInjected: yes"})


def test_custom_headers_reject_case_insensitive_duplicate_names():
    duplicate_headers = {
        "Authorization": "Bearer first",
        "authorization": "Bearer second",
    }
    with pytest.raises(McpSecurityError, match="忽略大小写"):
        validate_mcp_headers(duplicate_headers)
    with pytest.raises(ValidationError, match="忽略大小写"):
        UserMcpServerCreate.model_validate({
            "name": "duplicate-headers",
            "url": "https://example.com/mcp",
            "auth_type": "headers",
            "headers": duplicate_headers,
        })


@pytest.mark.parametrize(
    "headers",
    [
        {"Bad Header": "value"},
        {"X-Test": "nul\x00value"},
        {"X-Test": "🙂"},
        {f"X-{'a' * 128}": "value"},
        {f"X-{index}": "value" for index in range(33)},
    ],
)
def test_custom_headers_reject_values_httpx_cannot_safely_encode(headers):
    with pytest.raises(McpSecurityError):
        validate_mcp_headers(headers)


@pytest.mark.parametrize("token", ["has space", "line\nbreak", "令牌"])
def test_bearer_tokens_reject_invalid_http_header_values(token):
    with pytest.raises(McpSecurityError):
        credential_payload("bearer", bearer_token=token)


def test_resolved_endpoint_captures_only_validated_tcp_addresses(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )

    endpoint = resolve_mcp_endpoint("https://Example.COM/mcp")

    assert endpoint.url == "https://example.com/mcp"
    assert endpoint.hostname == "example.com"
    assert endpoint.port == 443
    assert endpoint.addresses == ("93.184.216.34",)


def test_resolved_endpoint_rejects_mixed_public_and_private_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),
        ],
    )

    with pytest.raises(McpSecurityError, match="非公网"):
        resolve_mcp_endpoint("https://example.com/mcp")


def test_resolved_endpoint_bounds_pinned_dns_candidates(monkeypatch):
    records = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"8.8.8.{index}", 443))
        for index in range(1, 11)
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: records)

    endpoint = resolve_mcp_endpoint("https://example.com/mcp")

    assert endpoint.addresses == tuple(f"8.8.8.{index}" for index in range(1, 9))


def test_resolved_endpoint_uses_wire_idna_hostname(monkeypatch):
    seen = {}

    def fake_getaddrinfo(host, *args, **kwargs):
        seen["host"] = host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    endpoint = resolve_mcp_endpoint("https://例子.测试/mcp")

    assert seen["host"] == "xn--fsqu00a.xn--0zwm56d"
    assert endpoint.hostname == seen["host"]


def test_encrypted_credential_never_contains_plaintext():
    encrypted = encrypt_credential({"bearer_token": "super-secret-token"})
    assert "super-secret-token" not in encrypted
    assert decrypt_credential(encrypted) == {"bearer_token": "super-secret-token"}
    assert credential_headers("bearer", encrypted) == {
        "Authorization": "Bearer super-secret-token"
    }


def test_secret_crypto_never_falls_back_to_auth_key_in_production(monkeypatch):
    monkeypatch.setattr(
        secret_crypto,
        "get_settings",
        lambda: SimpleNamespace(
            debug=False,
            auth_secret_key="production-auth-secret-that-is-long-enough",
            mcp_secret_key="",
        ),
    )

    with pytest.raises(RuntimeError, match="cannot fall back to AUTH_SECRET_KEY"):
        secret_crypto._fernet()
