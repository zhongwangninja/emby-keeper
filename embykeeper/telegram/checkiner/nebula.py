from json import JSONDecodeError
from datetime import datetime, timezone

from curl_cffi.requests import RequestsError

from embykeeper.runinfo import RunStatus
from embykeeper.utils import format_timedelta_human, show_exception

from . import BotCheckin
from ._webapp import get_webview_auth, load_webapp_page, webapp_fetch_headers, webapp_session


class NebulaCheckin(BotCheckin):
    name = "Nebula"
    bot_username = "Nebula_Account_bot"
    max_retries = 1
    additional_auth = ["prime"]

    async def send_checkin(self, **kw):
        auth = await get_webview_auth(self.client, self.bot_username)

        url_info = f"{auth.base_url}/api/v1/tg/info"
        url_checkin = f"{auth.base_url}/api/v1/tg/checkin"

        headers = webapp_fetch_headers(auth.base_url, extra={"X-Initdata": auth.data})

        try:
            async with webapp_session() as session:
                # 模拟小程序 WebView 首次加载页面
                await load_webapp_page(session, auth.page_url)

                # 先获取用户信息
                resp_info = await session.get(url_info, headers=headers)
                info_results = resp_info.json()

                if info_results.get("message") != "Success":
                    self.log.info("签到失败: 账户错误.")
                    return await self.fail(message="账户错误")

                # 获取当前余额和下次签到时间
                current_balance = info_results["data"]["balance"]
                next_checkin_time = datetime.fromisoformat(
                    info_results["data"]["next_check_in"].split(".")[0].replace("Z", "+00:00")
                ).replace(tzinfo=timezone.utc)

                # 检查是否可以签到
                if next_checkin_time > datetime.now(timezone.utc):
                    # 获取今天24点的时间
                    today_end = datetime.now(timezone.utc).replace(
                        hour=23, minute=59, second=59, microsecond=999999
                    )
                    if next_checkin_time <= today_end:
                        # 今天还可以签到, 等待到指定时间
                        sleep = next_checkin_time - datetime.now(timezone.utc)
                        self.log.info(f"即将在 {format_timedelta_human(sleep)} 后重试.")
                        # 将UTC时间转换为本地时间并移除时区信息
                        local_next_time = next_checkin_time.astimezone().replace(tzinfo=None)
                        self.ctx.next_time = local_next_time
                        return await self.finish(RunStatus.RESCHEDULE, "等待重新尝试签到")
                    else:
                        # 今天已经不能签到了
                        self.log.info("今日已经签到过了.")
                        return await self.finish(RunStatus.NONEED, "今日已签到")

                # 执行签到
                resp = await session.post(url_checkin, headers=headers)
                results = resp.json()
                message = results["message"]
                if any(s in message for s in ("未找到用户", "权限错误")):
                    self.log.info("签到失败: 账户错误.")
                    return await self.fail(message="账户错误")
                if "Failed" in message:
                    self.log.info("签到失败.")
                    return await self.retry()
                elif "Success" in message:
                    self.log.info(
                        f"[yellow]签到成功[/]: + {results['data']['coin']} 分 -> {current_balance + results['data']['coin']} 分."
                    )
                    return await self.finish(RunStatus.SUCCESS, "签到成功")
                else:
                    self.log.warning(f"接收到异常返回信息: {results}")
                    return await self.retry()
        except (RequestsError, OSError, JSONDecodeError) as e:
            self.log.info(f"签到失败: 无法连接签到页面 ({e.__class__.__name__}).")
            show_exception(e)
            return await self.retry()
