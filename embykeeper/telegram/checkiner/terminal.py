import asyncio
import base64
import json
import os
import time
import urllib.error
import urllib.request

from pyrogram.errors import RPCError
from pyrogram.types import Message

from embykeeper.config import config as global_config

from . import AnswerBotCheckin

def image_base64_to_data_url(image_base64: str, mime_type: str = "image/jpeg") -> str:
    image_base64 = image_base64.strip()
    if not image_base64:
        raise ValueError("image_base64 is empty.")

    if image_base64.startswith("data:"):
        return image_base64

    base64.b64decode(image_base64, validate=True)
    return f"data:{mime_type};base64,{image_base64}"


def extract_text_from_response(data: dict) -> str:
    choices = data.get("choices", [])
    if not choices:
        return ""

    message = choices[0].get("message", {})
    content = message.get("content")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                elif isinstance(part.get("content"), str):
                    text_parts.append(part["content"])
        return "\n".join(text_parts).strip()

    return ""


def match_inline_option(result: str, options: list) -> str:
    """将 AI 返回值匹配到某一个 inline keyboard 选项；无法唯一匹配则返回 None."""
    if not result or not options:
        return None

    text = result.strip()
    if text in options:
        return text

    cleaned = text.strip("“”\"'。．.：:，, \n\t")
    if cleaned in options:
        return cleaned

    matches = [option for option in options if option and option in text]
    if len(matches) == 1:
        return matches[0]
    return None


def build_chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return normalized + "/chat/completions"


def call_ai_chat_completion(
    prompt: str,
    image_base64: str,
    base_url: str,
    model: str,
    api_key: str,
    log=None,
    image_mime: str = "image/jpeg",
    timeout: float = 120.0,
) -> str:
    data_url = image_base64_to_data_url(image_base64=image_base64, mime_type=image_mime)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "stream": False,
    }

    url = build_chat_completions_url(base_url)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    if log:
        masked_api_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "***"
        log.debug(
            "call_ai_chat_completion request meta: "
            f"url={url}, model={model}, image_mime={image_mime}, timeout={timeout}, "
            f"prompt_len={len(prompt)}, image_base64_len={len(image_base64)}, data_url_len={len(data_url)}"
        )
        log.debug(
            "call_ai_chat_completion request headers: "
            f"Authorization=Bearer {masked_api_key}, Content-Type={headers['Content-Type']}"
        )
        log.debug(f"call_ai_chat_completion request payload: {payload_json}")

    request = urllib.request.Request(
        url=url,
        data=payload_json.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        started_at = time.perf_counter()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            status = getattr(response, "status", response.getcode())
            response_headers = dict(response.headers.items())
            raw = response.read().decode("utf-8")
            if log:
                log.debug(
                    f"call_ai_chat_completion response meta: status={status}, "
                    f"elapsed_ms={elapsed_ms:.2f}, headers={response_headers}"
                )
                log.debug(f"call_ai_chat_completion response raw: {raw}")
            data = json.loads(raw)
            if log:
                log.debug(f"call_ai_chat_completion response parsed: {json.dumps(data, ensure_ascii=False)}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if log:
            log.warning(
                f"call_ai_chat_completion HTTPError: code={exc.code}, reason={exc.reason}, "
                f"url={url}, body={body}"
            )
        raise RuntimeError(f"HTTP error {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        if log:
            log.warning(f"call_ai_chat_completion request failed: {exc.__class__.__name__}: {exc}")
        raise RuntimeError(f"Request failed: {exc}") from exc

    text = extract_text_from_response(data).strip()
    if log:
        log.debug(f"call_ai_chat_completion extracted text: {text}")
    if not text:
        if log:
            log.debug("call_ai_chat_completion extracted text is empty, returning full parsed response JSON.")
        return json.dumps(data, ensure_ascii=False, indent=2)

    return text


class TerminalCheckin(AnswerBotCheckin):
    name = "终点站 AI"
    # bot_username = "EmbyPublicBot"
    bot_username = "my_annunciator_boards_bot"
    bot_checkin_cmd = ["/checkin"]
    bot_text_ignore = ["会话已取消", "没有活跃的会话"]
    bot_checked_keywords = ["今天已签到"]
    skip_service_auth = True
    max_retries = 1
    bot_use_history = 3

    def _get_required_ai_config(self, key: str) -> str:
        value = (self.config or {}).get(key)
        if value is None:
            value = getattr(global_config.checkiner, key, None)
        if not value:
            if key == "ai_api_key":
                value = os.environ.get("MODELSCOPE_ACCESS_TOKEN", "") or os.environ.get("MODELSCOPE_API_KEY", "")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'缺少必要配置: checkiner.{key}')
        return value.strip()

    def _get_optional_ai_config(self, key: str, default: str = None) -> str:
        value = (self.config or {}).get(key)
        if value is None:
            value = getattr(global_config.checkiner, key, None)
        if not isinstance(value, str) or not value.strip():
            return default
        return value.strip()

    def _get_ai_base_url(self) -> str:
        return self._get_optional_ai_config("ai_base_url", "https://api-inference.modelscope.cn/v1")

    def _get_ai_model(self) -> str:
        return self._get_optional_ai_config("ai_model", "moonshotai/Kimi-K2.5")

    def _get_ai_api_key(self) -> str:
        return self._get_required_ai_config("ai_api_key")

    def _get_ai_secondary_base_url(self) -> str:
        return self._get_optional_ai_config("ai_secondary_base_url")

    def _get_ai_secondary_model(self) -> str:
        return self._get_optional_ai_config("ai_secondary_model")

    def _get_ai_secondary_api_key(self) -> str:
        return self._get_optional_ai_config("ai_secondary_api_key")

    def _has_secondary_ai(self) -> bool:
        return bool(
            self._get_ai_secondary_base_url()
            and self._get_ai_secondary_model()
            and self._get_ai_secondary_api_key()
        )

    async def _try_ai_with_retry(
        self,
        prompt: str,
        image_base64: str,
        options: list,
        base_url: str,
        model: str,
        api_key: str,
        label: str,
        max_attempts: int = 3,
    ) -> str:
        for attempt in range(1, max_attempts + 1):
            try:
                raw = (
                    await asyncio.to_thread(
                        call_ai_chat_completion,
                        prompt=prompt,
                        image_base64=image_base64,
                        base_url=base_url,
                        model=model,
                        api_key=api_key,
                        log=self.log,
                    )
                ).strip()
            except Exception as e:
                self.log.warning(
                    f"{label} 调用失败 ({attempt}/{max_attempts}): {e.__class__.__name__}: {e}"
                )
                raw = None

            raw = "调试"
            matched = match_inline_option(raw, options) if raw else None
            if matched:
                self.log.info(f"{label} 解析答案: {matched}.")
                return matched

            if raw:
                self.log.warning(
                    f"{label} 返回结果不符合选项 ({attempt}/{max_attempts}): {raw!r}, 选项={options}"
                )
            if attempt < max_attempts:
                self.log.info(f"{label} 等待 2 秒后进行下一次重试...")
                await asyncio.sleep(2)

        return None

    async def on_photo(self, message: Message):
        """分析传入的验证码图片并点击匹配选项."""
        self.log.debug(f"{message.date} 收到验证码图片")

        if not message.reply_markup:
            return

        keys = [k for r in message.reply_markup.inline_keyboard for k in r]
        options = [k.text for k in keys]
        if len(options) < 2:
            return

        try:
            image = await self.client.download_media(message, in_memory=True)
            if not image:
                self.log.warning("签到失败: 图片下载失败.")
                return await self.fail()

            if hasattr(image, "getvalue"):
                image_bytes = image.getvalue()
            elif isinstance(image, (bytes, bytearray)):
                image_bytes = bytes(image)
            else:
                image_bytes = image.read()

            image_base64 = base64.b64encode(image_bytes).decode("utf-8")
            prompt = (
                "请观察图片内容，从以下选项中选出图片中出现的物品，只返回该物品的名称，"
                f"不要返回任何其他文字。\n\n选项：{'/'.join(options)}"
            )

            result = await self._try_ai_with_retry(
                prompt=prompt,
                image_base64=image_base64,
                options=options,
                base_url=self._get_ai_base_url(),
                model=self._get_ai_model(),
                api_key=self._get_ai_api_key(),
                label="AI",
            )

            if not result and self._has_secondary_ai():
                self.log.warning("主 AI 三次调用均失败, 切换到备用 AI.")
                result = await self._try_ai_with_retry(
                    prompt=prompt,
                    image_base64=image_base64,
                    options=options,
                    base_url=self._get_ai_secondary_base_url(),
                    model=self._get_ai_secondary_model(),
                    api_key=self._get_ai_secondary_api_key(),
                    label="备用 AI",
                )
            elif not result:
                self.log.warning("主 AI 三次调用均失败, 且未配置备用 AI.")

            if not result:
                self.log.warning("签到失败: AI 识别错误.")
                return await self.fail()

            await message.click(result)
        except RPCError:
            self.log.warning("按钮点击失败.")
        except Exception as e:
            self.log.warning(f"签到失败: AI 识别错误 ({e.__class__.__name__}).")
            return await self.fail()
