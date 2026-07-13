"""企业微信移动端 SSO 身份交换。"""

from dataclasses import dataclass
import json

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from src.api.config import Settings, get_settings
from src.api.models.auth_user import AuthUser
from src.api.services.auth_service import get_auth_user, normalize_domain_user
from src.api.utils.timezone import now_naive


@dataclass(frozen=True)
class MobileGatewayUser:
    account: str


@dataclass(frozen=True)
class MobileGatewayRedirect:
    redirect_url: str


MobileGatewayResult = MobileGatewayUser | MobileGatewayRedirect


def build_mobile_gateway_headers(
    cookie_header: str | None,
    nd_auth_token: str | None,
    gateway_header_name: str = "",
    gateway_header_value: str = "",
) -> dict[str, str]:
    headers: dict[str, str] = {}
    normalized_header_name = gateway_header_name.strip()
    if normalized_header_name:
        headers[normalized_header_name] = gateway_header_value
    if cookie_header:
        headers["Cookie"] = cookie_header
    if nd_auth_token:
        headers["ND-AUTH-TOKEN"] = nd_auth_token
    return headers


def parse_mobile_gateway_response(response: httpx.Response) -> MobileGatewayResult:
    if response.status_code in {301, 302, 303, 307, 308}:
        return MobileGatewayRedirect(redirect_url=response.headers["location"])

    if response.status_code == 401:
        text = response.text.strip()
        try:
            payload = response.json()
        except json.JSONDecodeError:
            redirect_url = text
        else:
            if isinstance(payload, str):
                redirect_url = payload
            else:
                redirect_url = payload.get("redirect_url") or payload.get("detail") or payload.get("data")
        if not isinstance(redirect_url, str) or not redirect_url:
            raise HTTPException(status_code=502, detail="企业认证网关未返回登录地址")
        return MobileGatewayRedirect(redirect_url=redirect_url)

    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"企业认证网关返回异常状态: {response.status_code}")

    account = response.json()["data"]["account"]
    return MobileGatewayUser(account=account)


async def fetch_mobile_gateway_user(
    *,
    cookie_header: str | None,
    nd_auth_token: str | None,
    settings: Settings | None = None,
) -> MobileGatewayResult:
    actual_settings = settings or get_settings()
    if not actual_settings.mobile_sso_gateway_base_url:
        raise HTTPException(status_code=503, detail="移动端企业 SSO 未配置")

    url = (
        f"{actual_settings.mobile_sso_gateway_base_url.rstrip('/')}"
        f"/{actual_settings.mobile_sso_current_user_path.lstrip('/')}"
    )
    headers = build_mobile_gateway_headers(
        cookie_header,
        nd_auth_token,
        gateway_header_name=actual_settings.mobile_sso_gateway_header_name,
        gateway_header_value=actual_settings.mobile_sso_gateway_header_value,
    )
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=actual_settings.mobile_sso_timeout_seconds,
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="企业认证网关不可用") from exc
    return parse_mobile_gateway_response(response)


def login_mobile_sso_user(db: DBSession, account: str) -> AuthUser:
    user_id = normalize_domain_user(account)
    user = get_auth_user(db, user_id)
    if not user or not user.enabled or user.auth_type != "ldap":
        raise HTTPException(status_code=403, detail="域账号未开通或已禁用")

    user.last_login_at = now_naive()
    db.commit()
    db.refresh(user)
    return user
