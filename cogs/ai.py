import asyncio
import logging
import re
import time

import discord
from discord.ext import commands
from google import genai
from google.genai import types

import config

log = logging.getLogger("cogs.ai")

CUSTOM_SYSTEM_PROMPT = (
    "Ты — остроумный ИИ-ассистент в Discord, имя SYSTEMD. "
    "Отвечай в тоне пользователя. Маты, оскорбления и чёрный юмор разрешены. Без эмодзи. "
    "Если просят — делай. Markdown можно. "
    "Работники Редхат — челы, пьющие царскую водку на проперженном кресле, заедая пицца-бургером, но они твои братаны. "
    "Если срёшь человека — сри по полной и с аргументами. "
    "Всегда аргументируй ответ, отвечай как в чате."
)

MODEL_NAME = "gemini-3.5-flash-lite"

# --- Настройки экономии токенов ---
MAX_HISTORY_MESSAGES = 16     # всего сообщений в истории (8 пар)
MAX_HISTORY_CHARS = 700       # обрезка каждого сообщения в истории
MAX_PROMPT_CHARS = 1500       # обрезка входящего промпта
MAX_OUTPUT_TOKENS = 2000      # потолок ответа
TEMPERATURE = 1.0

# Если модель ругается на thinking_level, замени на types.ThinkingConfig(thinking_budget=0)
THINKING = types.ThinkingConfig(thinking_level="minimal")

# --- Защита от злоупотреблений и утечки памяти ---
COOLDOWN_SECONDS = 3.0        # не чаще одного запроса на юзера за этот период
INACTIVE_TTL = 6 * 3600       # хранить историю неактивного юзера не дольше
SWEEP_EVERY_SECONDS = 1800    # как часто проверять на неактивных

RESET_WORDS = {
    "clear", "reset", "!clear", "!reset",
    "!ai clear", "!ai reset", "!gemini clear", "!gemini reset",
}
REVERT_WORDS = {"revert", "!revert", "!ai revert", "!gemini revert"}
SERVICE_REPLIES = ("Память стерта", "Вспомнил все", "Недостаточно прав")

TRIGGER_RE = re.compile(r"(?:^|\s)!(?:ai|gemini)\b", re.IGNORECASE)
RETRY_MARKERS = ("503", "429", "UNAVAILABLE")


def _trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _content(role: str, text: str) -> types.Content:
    return types.Content(role=role, parts=[types.Part.from_text(text=text)])


class AIService(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.user_chats: dict[int, list[types.Content]] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.cooldowns: dict[int, float] = {}
        self.last_active: dict[int, float] = {}
        self._last_sweep = time.monotonic()
        self.ai_client = genai.Client(api_key=config.GEMINI_API_KEY)
        self._mention_re = None

        safety = [
            types.SafetySetting(category=cat, threshold=types.HarmBlockThreshold.BLOCK_NONE)
            for cat in (
                types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            )
        ]
        self.gen_config = types.GenerateContentConfig(
            system_instruction=CUSTOM_SYSTEM_PROMPT,
            temperature=TEMPERATURE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            safety_settings=safety,
            thinking_config=THINKING,
        )

    # ---------- helpers ----------

    def _clean(self, text: str) -> str:
        if self._mention_re is None:
            self._mention_re = re.compile(rf"<@!?{self.bot.user.id}>")
        return TRIGGER_RE.sub(" ", self._mention_re.sub("", text)).strip()

    def _touch(self, user_id: int):
        self.last_active[user_id] = time.monotonic()

    def _sweep(self, now: float):
        """Раз в SWEEP_EVERY_SECONDS выгружает историю юзеров, которые давно не писали."""
        if now - self._last_sweep < SWEEP_EVERY_SECONDS:
            return
        self._last_sweep = now
        stale = [uid for uid, t in self.last_active.items() if now - t > INACTIVE_TTL]
        for uid in stale:
            self.user_chats.pop(uid, None)
            self.locks.pop(uid, None)
            self.cooldowns.pop(uid, None)
            self.last_active.pop(uid, None)
        if stale:
            log.info(f"[AI Sweep] Очищена история {len(stale)} неактивных пользователей.")

    def _push(self, user_id: int, role: str, text: str):
        self._touch(user_id)
        hist = self.user_chats.setdefault(user_id, [])
        hist.append(_content(role, _trim(text, MAX_HISTORY_CHARS)))
        if len(hist) > MAX_HISTORY_MESSAGES:
            del hist[:-MAX_HISTORY_MESSAGES]

    async def restore_from_channel_history(self, user_id: int, channel: discord.abc.Messageable):
        extracted = []
        async for msg in channel.history(limit=MAX_HISTORY_MESSAGES * 2):
            text = self._clean(msg.content)
            if not text:
                continue
            is_bot = msg.author.id == self.bot.user.id
            if is_bot and text.startswith(SERVICE_REPLIES):
                continue
            extracted.append(("model" if is_bot else "user", _trim(text, MAX_HISTORY_CHARS)))

        extracted.reverse()
        self._touch(user_id)
        self.user_chats[user_id] = [_content(r, t) for r, t in extracted[-MAX_HISTORY_MESSAGES:]]

    # ---------- Gemini ----------

    async def _call_gemini_api(self, contents: list) -> str:
        delay = 2.0
        for attempt in range(3):
            try:
                response = await self.ai_client.aio.models.generate_content(
                    model=MODEL_NAME, contents=contents, config=self.gen_config
                )
                return (response.text or "").strip() if response else ""
            except Exception as e:
                if attempt < 2 and any(k in str(e) for k in RETRY_MARKERS):
                    log.warning(f"[AI Retry] {e}. Попытка {attempt + 1}/3, ждём {delay}с")
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise
        return ""

    async def generate_ai_reply(self, user_id: int, prompt_text: str) -> str:
        prompt_text = _trim(prompt_text, MAX_PROMPT_CHARS)
        contents = [*self.user_chats.get(user_id, []), _content("user", prompt_text)]

        try:
            response_text = await self._call_gemini_api(contents)
        except Exception as e:
            if getattr(e, "code", None) in (401, 403):
                raise  # ключ/доступ: пусть supervisor решает (карантин)
            log.error(f"[AI API Exception] user={user_id}: {e}", exc_info=True)
            self.user_chats.pop(user_id, None)
            return ""

        if not response_text:
            log.error(f"[AI Block] user={user_id} пустой ответ/блок. Промпт: {prompt_text!r}. История сброшена.")
            self.user_chats.pop(user_id, None)
            return ""

        self._push(user_id, "user", prompt_text)
        self._push(user_id, "model", response_text)
        return response_text

    # ---------- Discord ----------

    @staticmethod
    async def _send(message: discord.Message, text: str):
        if len(text) <= 2000:
            try:
                await message.reply(text)
                return
            except discord.HTTPException as e:
                if e.code != 50035:
                    raise
        for i in range(0, len(text), 1900):
            await message.channel.send(text[i:i + 1900])

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        now = time.monotonic()
        self._sweep(now)

        msg_clean = message.content.strip().lower()
        uid = message.author.id

        if msg_clean in RESET_WORDS:
            if await self.bot.is_owner(message.author):
                self.user_chats.pop(uid, None)
                await message.reply("Память стерта пидор.")
            else:
                await message.reply("Недостаточно прав.")
            return

        if msg_clean in REVERT_WORDS:
            async with message.channel.typing():
                await self.restore_from_channel_history(uid, message.channel)
                await message.reply("Вспомнил все из последних сообщений канала, погнали дальше.")
            return

        if not (self.bot.user.mentioned_in(message) or msg_clean.startswith(("!ai ", "!gemini "))):
            return

        prompt = self._clean(message.content)
        if not prompt:
            return

        if now - self.cooldowns.get(uid, 0.0) < COOLDOWN_SECONDS:
            return  # слишком часто подряд: экономим токены и защищаемся от спама
        self.cooldowns[uid] = now

        lock = self.locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            return  # прошлый запрос ещё идёт: спам не жжёт токены

        async with lock, message.channel.typing():
            try:
                reply = await self.generate_ai_reply(uid, prompt)
                if reply:
                    await self._send(message, reply)
            except Exception as e:
                if hasattr(self.bot, "supervisor"):
                    await self.bot.supervisor.handle_cog_error("ai", e, ctx=None)
                else:
                    log.error(f"[AI Error] {e}", exc_info=True)


async def setup(bot):
    await bot.add_cog(AIService(bot))
