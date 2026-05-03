import asyncio
import random
import re
from pyrogram.types import Message
from pyrogram.errors import MessageIdInvalid

from . import BotCheckin


class MICUNCheckin(BotCheckin):
    name = "MICUN 股东会"
    bot_username = "micu_user_bot"
    bot_checkin_cmd = "/start"
    bot_checked_keywords = ["已经签到过了"]
    bot_use_captcha = False
    max_retries = 1

    async def message_handler(self, client, message: Message):
        if message.caption and "你好鸭" in message.caption and message.reply_markup:
            keys = [k.text for r in message.reply_markup.inline_keyboard for k in r]
            for k in keys:
                if "签到" in k:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    try:
                        await message.click(k)
                    except (TimeoutError, MessageIdInvalid):
                        pass
                    return
            else:
                self.log.warning(f"签到失败: 账户错误.")
                return await self.fail()
        if message.caption and "答错" in message.caption and message.reply_markup:
            match = re.search(r"(\d+\s*[-+]\s*\d+)", message.caption)
            if match:
                expression = match.group(1).replace(" ", "")
                try:
                    captcha = str(eval(expression))
                    self.log.debug(f"计算验证码: {expression} = {captcha}.")
                    await asyncio.sleep(random.uniform(2, 4))
                    await message.click(captcha)
                    return
                except Exception as e:
                    self.log.warning(f"计算验证码失败: {e}.")

        await super().message_handler(client, message)
