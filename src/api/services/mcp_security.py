"""Security boundary for server-supplied MCP endpoints and credentials."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.api.services.secret_crypto import decrypt_secret, encrypt_secret


class McpSecurityError(ValueError):
    """Raised when an MCP endpoint or credential violates platform policy."""


@dataclass(frozen=True)
class ResolvedMcpEndpoint:
    """A validated endpoint together with the exact TCP destinations it resolved to.

    ``url`` intentionally retains the original hostname.  The runtime uses that
    hostname for HTTP routing and TLS SNI/certificate verification, while its
    network backend connects only to ``addresses``.  Keeping both values is what
    closes the DNS-rebinding window between validation and the actual socket.
    """

    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]
    network_authorization: "McpNetworkAuthorization | None" = None


@dataclass(frozen=True)
class PersonalMcpNetworkPolicy:
    """Normalized administrator exceptions for personal MCP endpoints."""

    domain_suffixes: tuple[str, ...] = ()
    cidrs: tuple[str, ...] = ()
    version: int = 0

    @property
    def networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(ipaddress.ip_network(value) for value in self.cidrs)


@dataclass(frozen=True)
class McpNetworkAuthorization:
    """Non-secret evidence explaining why a personal endpoint was allowed."""

    scheme: str
    hostname: str
    addresses: tuple[str, ...]
    matched_domain_suffix: str | None = None
    matched_cidrs: tuple[str, ...] = ()

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "hostname": self.hostname,
            "addresses": list(self.addresses),
            "matched_domain_suffix": self.matched_domain_suffix,
            "matched_cidrs": list(self.matched_cidrs),
        }


_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.google",
}
_MAX_MCP_DNS_ADDRESSES = 8
_MAX_MCP_HEADERS = 32
_MAX_MCP_HEADER_NAME_BYTES = 128
_MAX_MCP_HEADER_VALUE_BYTES = 32 * 1024
_MAX_SANITIZED_MCP_ERROR_CHARS = 500
_HTTP_FIELD_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_MCP_ERROR_URL_RE = re.compile(
    r"https?://[^\s<>\"'\x00-\x1f\x7f]+",
    re.IGNORECASE,
)
_MCP_ERROR_BEARER_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
# IPv6 transition/translation ranges can carry an IPv4 destination that is
# different from the apparently public IPv6 address.  In particular, Python
# versions supported by this project report addresses in 64:ff9b::/96 as
# ``is_global=True`` even when the embedded IPv4 address is loopback, private,
# or link-local.  Reject these ambiguous encodings rather than letting NAT64,
# 6to4, Teredo, or IPv4-mapped routing bypass the IPv4 SSRF policy.
_BLOCKED_IPV6_TRANSITION_NETWORKS = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)
_PERSONAL_NEVER_ALLOW_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("2001::/32"),
    ipaddress.ip_network("2002::/16"),
    *_BLOCKED_IPV6_TRANSITION_NETWORKS,
)
_MAX_PERSONAL_NETWORK_POLICY_ENTRIES = 100
_BLOCKED_HEADER_NAMES = {
    "accept-encoding",
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def mcp_url_without_query(value: str) -> str:
    """Return the public endpoint identity without query credentials.

    Official MCP URLs are administrator-owned configuration.  A query string
    can itself be an API credential, so user-facing catalog projections expose
    only the scheme, origin and path.  Invalid legacy rows fail closed instead
    of echoing their original value.
    """

    parsed = urlsplit(str(value or ""))
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path or "/", "", "")
    )


def sanitize_mcp_exception(
    exc: BaseException,
    *,
    url: str | None = None,
    headers: Mapping[str, str] | None = None,
    include_exception_type: bool = False,
    max_chars: int = _MAX_SANITIZED_MCP_ERROR_CHARS,
) -> str:
    """Build bounded MCP diagnostics without endpoint or credential leakage.

    Both catalog discovery/runtime failures and explicit connection probes use
    this single boundary.  Besides exact configured values, it removes any
    URL and Bearer credential reflected by a remote server.  Control/format
    characters are flattened so diagnostics cannot inject terminal or log
    lines.
    """

    try:
        message = str(exc)
    except Exception:  # pragma: no cover - defensive third-party exception
        message = "remote MCP request failed"
    sensitive_values: set[str] = set()
    if url:
        sensitive_values.add(str(url))
    for raw_value in (headers or {}).values():
        value = str(raw_value or "")
        if not value:
            continue
        sensitive_values.add(value)
        if value.lower().startswith("bearer ") and value[7:]:
            sensitive_values.add(value[7:])
    for sensitive in sorted(sensitive_values, key=len, reverse=True):
        message = message.replace(sensitive, "[REDACTED]")

    message = _MCP_ERROR_BEARER_RE.sub("Bearer [REDACTED]", message)
    message = _MCP_ERROR_URL_RE.sub("[REDACTED_URL]", message)
    message = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char
        for char in message
    )
    message = " ".join(message.split()) or "remote MCP request failed"
    if include_exception_type:
        exception_type = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            str(type(exc).__name__),
        )[:100] or "McpError"
        message = f"{exception_type}: {message}"

    safe_limit = max(1, int(max_chars))
    if len(message) > safe_limit:
        if safe_limit <= 3:
            return message[:safe_limit]
        return message[: safe_limit - 3] + "..."
    return message


def _is_non_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # ``is_global`` alone is insufficient for IPv6 transition addresses.  For
    # example, 64:ff9b::a9fe:a9fe embeds 169.254.169.254 but is classified as
    # global by some supported Python releases.  ``is_reserved`` is checked
    # independently for the same cross-version reason.
    if not address.is_global or address.is_reserved:
        return True
    if isinstance(address, ipaddress.IPv6Address):
        if any(address in network for network in _BLOCKED_IPV6_TRANSITION_NETWORKS):
            return True
        if address.ipv4_mapped is not None:
            return True
        if address.sixtofour is not None:
            return True
        if address.teredo is not None:
            return True
    return False


def _is_personal_never_allowed_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if (
        any(
            address.version == network.version and address in network
            for network in _PERSONAL_NEVER_ALLOW_NETWORKS
        )
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        return True
    if isinstance(address, ipaddress.IPv6Address):
        if any(address in network for network in _BLOCKED_IPV6_TRANSITION_NETWORKS):
            return True
        if address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None:
            return True
    return False


def normalize_personal_mcp_network_policy(
    domain_suffixes: list[str] | tuple[str, ...] | None,
    cidrs: list[str] | tuple[str, ...] | None,
    *,
    version: int = 0,
) -> PersonalMcpNetworkPolicy:
    """Validate, normalize and bound the global personal-MCP policy."""

    raw_domains = list(domain_suffixes or ())
    raw_cidrs = list(cidrs or ())
    if len(raw_domains) + len(raw_cidrs) > _MAX_PERSONAL_NETWORK_POLICY_ENTRIES:
        raise McpSecurityError("个人 MCP 网络白名单最多允许 100 条")

    domains: set[str] = set()
    for raw_value in raw_domains:
        value = str(raw_value or "").strip().rstrip(".")
        if not value or "*" in value or "://" in value or "/" in value:
            raise McpSecurityError("个人 MCP 域名白名单格式非法")
        try:
            normalized = value.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise McpSecurityError("个人 MCP 域名白名单 IDNA 编码非法") from exc
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            pass
        else:
            raise McpSecurityError("IP 地址必须配置为 CIDR")
        if len(normalized) > 253 or len(normalized.split(".")) < 2:
            raise McpSecurityError("个人 MCP 域名白名单至少需要两个标签")
        if normalized in _BLOCKED_HOSTNAMES:
            raise McpSecurityError("不能将本机或云元数据域名加入个人 MCP 白名单")
        domains.add(normalized)

    normalized_cidrs: set[str] = set()
    for raw_value in raw_cidrs:
        value = str(raw_value or "").strip()
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise McpSecurityError(f"个人 MCP CIDR 格式非法: {value}") from exc
        if (network.version == 4 and network.prefixlen < 8) or (
            network.version == 6 and network.prefixlen < 16
        ):
            raise McpSecurityError("个人 MCP CIDR 范围过宽")
        if any(
            network.version == blocked.version and network.overlaps(blocked)
            for blocked in _PERSONAL_NEVER_ALLOW_NETWORKS
        ):
            raise McpSecurityError("个人 MCP CIDR 包含永久禁止访问的地址")
        normalized_cidrs.add(str(network))

    return PersonalMcpNetworkPolicy(
        domain_suffixes=tuple(sorted(domains)),
        cidrs=tuple(
            sorted(
                normalized_cidrs,
                key=lambda item: (
                    ipaddress.ip_network(item).version,
                    int(ipaddress.ip_network(item).network_address),
                    ipaddress.ip_network(item).prefixlen,
                ),
            )
        ),
        version=max(0, int(version)),
    )


def _matching_domain_suffix(
    hostname: str,
    policy: PersonalMcpNetworkPolicy | None,
) -> str | None:
    if policy is None:
        return None
    matches = [
        suffix
        for suffix in policy.domain_suffixes
        if hostname == suffix or hostname.endswith(f".{suffix}")
    ]
    return max(matches, key=len) if matches else None


def authorize_personal_mcp_endpoint(
    *,
    scheme: str,
    hostname: str,
    addresses: tuple[str, ...],
    policy: PersonalMcpNetworkPolicy | None,
) -> McpNetworkAuthorization | None:
    """Authorize resolved personal-MCP destinations or fail closed."""

    if hostname in _BLOCKED_HOSTNAMES:
        raise McpSecurityError("个人 MCP 不能访问本机或云元数据地址")
    parsed_addresses = tuple(ipaddress.ip_address(value) for value in addresses)
    for address in parsed_addresses:
        if _is_personal_never_allowed_ip(address):
            raise McpSecurityError("个人 MCP 主机解析到永久禁止访问的地址")

    domain_match = _matching_domain_suffix(hostname, policy)
    networks = () if policy is None else policy.networks
    matched_cidrs: set[str] = set()
    uncovered_non_public = False
    # A domain match authorizes HTTP only for the private addresses it is
    # meant to reach. A public address needs its own explicit CIDR entry, so
    # whitelisting an internal-sounding domain can't silently grant a
    # plaintext-HTTP exception for whatever public address it resolves to.
    all_addresses_http_authorized = bool(parsed_addresses)
    for address in parsed_addresses:
        matching = [
            network
            for network in networks
            if address.version == network.version and address in network
        ]
        if matching:
            matched_cidrs.add(str(max(matching, key=lambda item: item.prefixlen)))
        non_public = _is_non_public_ip(address)
        if non_public and domain_match is None and not matching:
            uncovered_non_public = True
        if not matching and not (non_public and domain_match is not None):
            all_addresses_http_authorized = False
    if uncovered_non_public:
        raise McpSecurityError("MCP 主机解析到未被管理员白名单授权的非公网地址")
    if scheme != "https" and not all_addresses_http_authorized:
        raise McpSecurityError("个人 MCP 的 HTTP 连接必须命中 CIDR 白名单，或命中域名白名单且解析地址为私网地址")

    policy_used = bool(
        scheme != "https"
        or any(_is_non_public_ip(address) for address in parsed_addresses)
    )
    if not policy_used:
        return None
    return McpNetworkAuthorization(
        scheme=scheme,
        hostname=hostname,
        addresses=tuple(str(address) for address in parsed_addresses),
        matched_domain_suffix=domain_match,
        matched_cidrs=tuple(sorted(matched_cidrs)),
    )


def _resolve_addresses(
    hostname: str,
    port: int,
    *,
    allow_private_network: bool,
    scheme: str = "https",
    personal_network_policy: PersonalMcpNetworkPolicy | None = None,
) -> tuple[str, tuple[str, ...], McpNetworkAuthorization | None]:
    try:
        wire_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise McpSecurityError("MCP 主机名 IDNA 编码非法") from exc
    try:
        records = socket.getaddrinfo(
            wire_hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise McpSecurityError("MCP 主机名无法解析") from exc

    addresses: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not record or not record[4]:
            continue
        try:
            address = str(ipaddress.ip_address(record[4][0]))
        except ValueError as exc:
            raise McpSecurityError("MCP 主机解析结果非法") from exc
        if (
            not allow_private_network
            and personal_network_policy is None
            and _is_non_public_ip(ipaddress.ip_address(address))
        ):
            raise McpSecurityError("MCP 主机解析到非公网地址")
        if address not in seen and len(addresses) < _MAX_MCP_DNS_ADDRESSES:
            seen.add(address)
            addresses.append(address)

    if not addresses:
        raise McpSecurityError("MCP 主机名没有可用地址")
    authorization = None
    if not allow_private_network and personal_network_policy is not None:
        authorization = authorize_personal_mcp_endpoint(
            scheme=scheme,
            hostname=wire_hostname,
            addresses=tuple(addresses),
            policy=personal_network_policy,
        )
    return wire_hostname, tuple(addresses), authorization


def validate_mcp_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Normalize custom headers and reject request-smuggling primitives."""

    if len(headers or {}) > _MAX_MCP_HEADERS:
        raise McpSecurityError(f"MCP headers 最多允许 {_MAX_MCP_HEADERS} 项")
    normalized: dict[str, str] = {}
    seen_names: set[str] = set()
    for raw_name, raw_value in (headers or {}).items():
        name = str(raw_name).strip()
        value = str(raw_value)
        if (
            not name
            or len(name.encode("utf-8")) > _MAX_MCP_HEADER_NAME_BYTES
            or _HTTP_FIELD_NAME.fullmatch(name) is None
        ):
            raise McpSecurityError("MCP header 名称非法")
        folded_name = name.casefold()
        if folded_name in seen_names:
            raise McpSecurityError(f"MCP header 名称不能重复（忽略大小写）: {name}")
        seen_names.add(folded_name)
        if name.lower() in _BLOCKED_HEADER_NAMES:
            raise McpSecurityError(f"不允许配置 MCP header: {name}")
        try:
            encoded_value = value.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise McpSecurityError(f"MCP header {name} 必须使用 HTTP 可编码字符") from exc
        if len(encoded_value) > _MAX_MCP_HEADER_VALUE_BYTES:
            raise McpSecurityError(f"MCP header {name} 值过长")
        if "\r" in value or "\n" in value:
            raise McpSecurityError(f"MCP header {name} 包含非法换行")
        if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value):
            raise McpSecurityError(f"MCP header {name} 包含非法控制字符")
        normalized[name] = value
    return normalized


def validate_mcp_url(
    value: str,
    *,
    allow_private_network: bool = False,
    allow_insecure_http: bool = False,
    resolve_dns: bool = False,
    personal_network_policy: PersonalMcpNetworkPolicy | None = None,
) -> str:
    """Validate and normalize a Streamable HTTP endpoint.

    Personal servers call this with both allow flags false. Official servers
    can opt into either exception explicitly. ``resolve_dns`` is retained for
    validation-only callers; connection paths must use ``resolve_mcp_endpoint``
    so the validated addresses can be pinned to the actual sockets.
    """

    raw = str(value or "").strip()
    if not raw:
        raise McpSecurityError("MCP URL 不能为空")
    if len(raw) > 4096:
        raise McpSecurityError("MCP URL 过长")

    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "http"}:
        raise McpSecurityError("仅支持 Streamable HTTP 的 http/https URL")
    if (
        scheme != "https"
        and not allow_insecure_http
        and personal_network_policy is None
    ):
        raise McpSecurityError("MCP URL 必须使用 HTTPS")
    if not parsed.hostname:
        raise McpSecurityError("MCP URL 缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise McpSecurityError("MCP URL 不能包含用户名或密码")
    if parsed.fragment:
        raise McpSecurityError("MCP URL 不能包含 fragment")

    hostname = parsed.hostname.rstrip(".").lower()
    if not allow_private_network and hostname in _BLOCKED_HOSTNAMES:
        raise McpSecurityError("个人 MCP 不能访问本机、内网或云元数据地址")

    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not allow_private_network:
        if personal_network_policy is None and _is_non_public_ip(literal_ip):
            raise McpSecurityError("个人 MCP 只能访问公网地址")
        if personal_network_policy is not None:
            authorize_personal_mcp_endpoint(
                scheme=scheme,
                hostname=hostname,
                addresses=(str(literal_ip),),
                policy=personal_network_policy,
            )

    try:
        port = parsed.port
    except ValueError as exc:
        raise McpSecurityError("MCP URL 端口非法") from exc
    if port is not None and not (1 <= port <= 65535):
        raise McpSecurityError("MCP URL 端口非法")

    if resolve_dns:
        _resolve_addresses(
            hostname,
            port or (443 if scheme == "https" else 80),
            allow_private_network=allow_private_network,
            scheme=scheme,
            personal_network_policy=personal_network_policy,
        )

    # urlsplit lower-cases hostname through .hostname but preserves the raw
    # netloc. Rebuild it without credentials and with a normalized host.
    bracketed_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{bracketed_host}:{port}" if port is not None else bracketed_host
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def resolve_mcp_endpoint(
    value: str,
    *,
    allow_private_network: bool = False,
    allow_insecure_http: bool = False,
    personal_network_policy: PersonalMcpNetworkPolicy | None = None,
) -> ResolvedMcpEndpoint:
    """Validate an MCP URL and capture the addresses used by the TCP connector.

    Resolution is performed exactly once here.  Callers must connect through a
    pinned network backend instead of resolving ``hostname`` again.
    """

    url = validate_mcp_url(
        value,
        allow_private_network=allow_private_network,
        allow_insecure_http=allow_insecure_http,
        resolve_dns=False,
        personal_network_policy=personal_network_policy,
    )
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    wire_hostname, addresses, authorization = _resolve_addresses(
        hostname,
        port,
        allow_private_network=allow_private_network,
        scheme=parsed.scheme,
        personal_network_policy=personal_network_policy,
    )
    return ResolvedMcpEndpoint(
        url=url,
        hostname=wire_hostname,
        port=port,
        addresses=addresses,
        network_authorization=authorization,
    )


def credential_payload(
    auth_type: str,
    *,
    bearer_token: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Build a validated credential payload, or ``None`` for no auth."""

    if auth_type == "none":
        if bearer_token is not None or headers is not None:
            raise McpSecurityError("auth_type=none 不能携带凭证")
        return None
    if auth_type == "bearer":
        token = str(bearer_token or "").strip()
        if not token:
            raise McpSecurityError("Bearer 认证需要 bearer_token")
        try:
            encoded_token = token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise McpSecurityError("Bearer token 必须使用 ASCII 字符") from exc
        if len(encoded_token) > _MAX_MCP_HEADER_VALUE_BYTES:
            raise McpSecurityError("Bearer token 过长")
        if any(ord(char) < 33 or ord(char) == 127 for char in token):
            raise McpSecurityError("Bearer token 包含非法字符")
        return {"bearer_token": token}
    if auth_type == "headers":
        normalized = validate_mcp_headers(headers)
        if not normalized:
            raise McpSecurityError("Headers 认证至少需要一个 header")
        return {"headers": normalized}
    raise McpSecurityError("不支持的 MCP auth_type")


def encrypt_credential(payload: dict[str, Any]) -> str:
    return encrypt_secret(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def decrypt_credential(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(decrypt_secret(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise McpSecurityError("MCP 凭证无法解密") from exc
    if not isinstance(payload, dict):
        raise McpSecurityError("MCP 凭证格式无效")
    return payload


def credential_headers(auth_type: str, encrypted_secret: str | None) -> dict[str, str]:
    """Resolve an encrypted credential into outbound HTTP headers."""

    if auth_type == "none":
        return {}
    if not encrypted_secret:
        raise McpSecurityError("MCP 凭证尚未配置")
    payload = decrypt_credential(encrypted_secret)
    if auth_type == "bearer":
        token = payload.get("bearer_token")
        if not isinstance(token, str) or not token:
            raise McpSecurityError("Bearer 凭证格式无效")
        return {"Authorization": f"Bearer {token}"}
    if auth_type == "headers":
        headers = payload.get("headers")
        if not isinstance(headers, dict):
            raise McpSecurityError("Headers 凭证格式无效")
        return validate_mcp_headers({str(k): str(v) for k, v in headers.items()})
    raise McpSecurityError("不支持的 MCP auth_type")


def credential_header_names(auth_type: str, encrypted_secret: str | None) -> list[str]:
    """Return non-secret header names for safe API serialization."""

    if not encrypted_secret:
        return []
    try:
        if auth_type == "bearer":
            credential_headers(auth_type, encrypted_secret)
            return ["Authorization"]
        if auth_type == "headers":
            return sorted(credential_headers(auth_type, encrypted_secret), key=str.lower)
    except McpSecurityError:
        return []
    return []
