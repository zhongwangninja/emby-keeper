import asyncio
import base64
import os
import random
from typing import Optional

import emoji
import httpx
from pyrogram.errors import RPCError
from pyrogram.types import Message

from embykeeper.config import config as global_config

from . import AnswerBotCheckin


class TerminalZhipuCheckin(AnswerBotCheckin):
    name = "终点站"
    bot_username = "EmbyPublicBot"
    bot_checkin_cmd = ["/checkin"]
    bot_text_ignore = ["会话已取消", "没有活跃的会话"]
    bot_checked_keywords = ["今天已签到"]
    max_retries = 1
    bot_use_history = 3

    def _get_zhipu_config(self, key: str) -> str:
        value = (self.config or {}).get(key)
        if value is None:
            value = getattr(global_config.checkiner, key, None)
        if not value:
            env_key = {"zhipu_api_key": "ZHIPU_API_KEY", "zhipu_model": "ZHIPU_VISION_MODEL"}.get(key, "")
            value = os.environ.get(env_key, "")
        return value

    def _get_zhipu_api_key(self) -> str:
        return self._get_zhipu_config("zhipu_api_key")

    def _get_zhipu_model(self) -> str:
        return self._get_zhipu_config("zhipu_model") or "glm-4v-flash"

    async def _zhipu_visual(self, message: Message, options_cleaned: list[str]) -> Optional[str]:
        """
        调用智谱视觉大模型识别图片内容，并从候选项中返回一个最匹配的结果。
        返回值必须是 options_cleaned 中的某一个。
        """
        if not self._get_zhipu_api_key():
            self.log.warning("未设置 ZHIPU_API_KEY 环境变量或配置项.")
            return None

        image_data = await self.client.download_media(message, in_memory=True)
        if hasattr(image_data, "getvalue"):
            image_bytes = image_data.getvalue()
        else:
            image_bytes = image_data

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        image_url = "data:image/jpeg;base64," + image_b64

        prompt = (
            "请根据图片内容，从候选项中选出最匹配的一项。\n"
            "候选项如下：\n"
            + "\n".join("- " + x for x in options_cleaned)
            + "\n\n"
            "要求：\n"
            "1. 只能输出候选项中的一个；\n"
            "2. 不要解释；\n"
            "3. 不要输出多余文字；\n"
            "4. 如果不确定，也必须从候选项中选最可能的一项；\n"
            "5. 必须输出候选中的一个。"
        )

        payload = {
            "model": self._get_zhipu_model(),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }
            ],
            # 将 0 改为 0.01，避免智谱 API 因极端值报错或返回空内容
            "temperature": 0.1,
            "max_tokens": 32,
            "thinking": {
                "type": "disabled"
            }
        }

        headers = {
            "Authorization": "Bearer " + self._get_zhipu_api_key(),
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

                # 新增：打印接口完整的返回内容，如果再出现“无效结果”，可以通过这条日志查明原因
                self.log.debug(f"智谱接口完整返回内容: {data}")

                result = data["choices"][0]["message"]["content"].strip()
                result = (
                    result.replace("“", "")
                    .replace("”", "")
                    .replace('"', "")
                    .replace("'", "")
                    .replace("。", "")
                    .replace("，", "")
                    .replace(",", "")
                    .replace("：", "")
                    .replace(":", "")
                    .replace(" ", "")
                    .replace("\n", "")
                    .strip()
                )

                self.log.debug("智谱提取出的结果: %s" % result)

                if result in options_cleaned:
                    return result

                for option in options_cleaned:
                    if option in result:
                        return option

                self.log.warning("智谱返回了无效结果: %s" % result)
                return None

        except Exception as e:
            self.log.warning("智谱视觉识别失败: %s" % e)
            return None

    async def on_photo(self, message: Message):
        """分析传入的验证码图片并返回验证码。"""
        if not message.reply_markup:
            return

        clean = lambda o: emoji.replace_emoji(o, "").replace(" ", "")
        keys = [k for r in message.reply_markup.inline_keyboard for k in r]
        options = [k.text for k in keys]
        options_cleaned = [clean(o) for o in options]

        if len(options) < 2:
            return

        result = None
        for i in range(3):
            result = await self._zhipu_visual(message, options_cleaned)
            if result:
                self.log.debug("已通过智谱解析答案: %s." % result)
                break
            else:
                self.log.warning("智谱解析失败, 正在重试解析 (%s/3)." % (i + 1))
                # 新增：重试前等待 3 秒，防止触发 429 Too Many Requests
                if i < 2:
                    self.log.info("等待 3 秒后进行下一次重试...")
                    await asyncio.sleep(3)

        if not result:
            self.log.warning("签到失败: 验证码识别错误.")
            return await self.fail()

        if result not in options_cleaned:
            self.log.warning("签到失败: 返回了无效结果: %s" % result)
            return await self.fail()

        result = options[options_cleaned.index(result)]

        await asyncio.sleep(random.uniform(0.5, 1.5))
        try:
            await message.click(result)
        except RPCError:
            self.log.warning("按钮点击失败.")