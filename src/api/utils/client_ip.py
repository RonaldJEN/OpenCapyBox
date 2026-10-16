"""请求来源 IP 解析

登录事件与管理操作审计统一走这里，保证两边记录的来源 IP 口径一致。

代理链由 uvicorn 的 ProxyHeadersMiddleware 解析：按 ``FORWARDED_ALLOW_IPS``
从 X-Forwarded-For 右侧逐个剥掉可信代理，取第一个不可信地址写入
``request.client``。应用层不得自行读取 X-Forwarded-For / X-Real-IP，
否则客户端自带的伪造值会被当成来源 IP。
"""

from fastapi import Request


def get_client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None
