"""Telegram 小程序 (WebApp) 签到的请求伪装工具.

将 HTTP 请求尽可能伪装为 iOS Telegram 内置小程序的 WebView 行为:
- TLS/HTTP2 指纹使用 iOS Safari 栈 (与 WKWebView 的 CFNetwork 栈一致);
- UA 使用 WKWebView 默认 UA (无 Safari 后缀), 与 RequestWebView(platform="ios") 匹配;
- API 请求头模拟小程序内 JS fetch 调用 (Sec-Fetch-*, Accept, Origin, Referer);
- 首次加载页面为导航请求语义, 并保留 cookie 与随机延时.
"""

import asyncio
import random
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

from curl_cffi.requests import AsyncSession, RequestsError

from pyrogram.raw.functions.messages import RequestWebView
from pyrogram.raw.functions.users import GetFullUser

from embykeeper.config import config
from embykeeper.utils import get_proxy_str

__ignore__ = True

# iOS Telegram 小程序内嵌 WebView (WKWebView) 的默认 UA
IOS_WEBAPP_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)


class WebAppAuth(NamedTuple):
    page_url: str  # 菜单按钮指向的小程序页面地址
    url: str  # 带 tgWebAppData 片段的授权 URL
    base_url: str  # 小程序站点的源 (scheme://netloc)
    data: str  # tgWebAppData 原始串


async def get_webview_auth(client, bot_username: str, platform: str = "ios") -> WebAppAuth:
    """通过机器人菜单按钮请求小程序授权, 返回授权信息."""
    bot_peer = await client.resolve_peer(bot_username)
    user_full = await client.invoke(GetFullUser(id=bot_peer))
    page_url = user_full.full_user.bot_info.menu_button.url
    url_auth = (
        await client.invoke(RequestWebView(peer=bot_peer, bot=bot_peer, platform=platform, url=page_url))
    ).url
    parsed = urlparse(url_auth)
    data = parse_qs(parsed.fragment).get("tgWebAppData", [""])[0]
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    return WebAppAuth(page_url, url_auth, base_url, data)


def webapp_session(**kw) -> AsyncSession:
    """构造模拟 iOS Telegram 小程序 WebView 的会话 (TLS 指纹与 UA 层伪装)."""
    kw.setdefault("proxy", get_proxy_str(config.proxy, curl=True))
    kw.setdefault("impersonate", "safari184_ios")
    kw.setdefault("allow_redirects", True)
    headers = {
        "User-Agent": IOS_WEBAPP_USER_AGENT,
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
    }
    return AsyncSession(headers=headers, **kw)


def webapp_fetch_headers(base_url: str, extra: dict = None) -> dict:
    """构造模拟小程序内 JS fetch API 调用的请求头."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": base_url,
        "Referer": f"{base_url}/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if extra:
        headers.update(extra)
    return headers


async def load_webapp_page(session: AsyncSession, url: str):
    """模拟 WebView 首次加载小程序页面 (导航请求语义), 以建立 cookie 等状态.

    加载后随机短暂延时, 模拟页面渲染与 JS 初始化耗时.
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        resp = await session.get(url, headers=headers)
    except (RequestsError, OSError):
        return None  # 页面加载失败不影响后续 API 调用
    await asyncio.sleep(random.uniform(0.5, 2.0))
    return resp
