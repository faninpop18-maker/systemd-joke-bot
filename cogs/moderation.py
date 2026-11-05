import logging
import time
from collections import deque
from datetime import timedelta

import discord
from discord.ext import commands

import config

logger = logging.getLogger("cogs.moderation")

SPAM_INTERVAL_SECONDS = 60
MAX_REPEATED_MESSAGES = 3
SWEEP_EVERY_SECONDS = 300
PER_USER_WINDOW_LIMIT = 50  # потолок записей на юзера внутри окна

# Подписанные ссылки Discord CDN протухают (параметр ex=). Лучше положить в config.REDHAT_VIDEO_URL
# или залить видео в закреп и брать постоянную ссылку.
DEFAULT_REDHAT_VIDEO = (
    "https://cdn.discordapp.com/attachments/1520382246161485855/1521852916670009416/"
    "GCC_SystemD_nJZSQDGXOLg.webm?ex=6a465779&is=6a4505f9&hm=7faa83cfcef23e5b0d9564923ce127275507b9a13333dc44aa2c540ceedff870&"
)
REDHAT_VIDEO = getattr(config, "REDHAT_VIDEO_URL", DEFAULT_REDHAT_VIDEO)
REDHAT_INSULT_REPLY = "пиздабол ебучий сосунок ебанный ебальник завали у нас за такое в RedHat пиздят"


class ModerationService(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # user_id -> deque[(monotonic_time, нормализованный_текст, message)]
        self.history: dict[int, deque] = {}
        self._last_sweep = time.monotonic()

    def _sweep(self, now: float):
        """Раз в SWEEP_EVERY_SECONDS выкидываем неактивных юзеров, чтобы кэш не рос бесконечно."""
        if now - self._last_sweep < SWEEP_EVERY_SECONDS:
            return
        self._last_sweep = now
        stale = [
            uid for uid, dq in self.history.items()
            if not dq or now - dq[-1][0] > SPAM_INTERVAL_SECONDS
        ]
        for uid in stale:
            del self.history[uid]

    async def _punish_spam(self, author: discord.abc.User, messages: list[discord.Message]):
        # Массовое удаление работает только в пределах одного канала
        by_channel: dict[int, tuple] = {}
        for m in messages:
            by_channel.setdefault(m.channel.id, (m.channel, []))[1].append(m)

        for channel, msgs in by_channel.values():
            try:
                if len(msgs) == 1:
                    await msgs[0].delete()
                else:
                    await channel.delete_messages(msgs)
            except (discord.Forbidden, discord.HTTPException) as err:
                logger.warning(f"[MODERATION] Не удалось удалить спам-сообщения: {err}")

        if isinstance(author, discord.Member):
            try:
                await author.timeout(timedelta(minutes=30), reason="Спам по каналам")
            except discord.Forbidden:
                logger.warning(f"[MODERATION] Недостаточно прав для таймаута {author}")
            except discord.HTTPException as err:
                logger.error(f"[MODERATION] Ошибка выдачи таймаута: {err}")

        try:
            await messages[-1].channel.send(f"{author.mention} Бро редхат такой хуйней не гордиться")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        channel_id = getattr(config, "WELCOME_CHANNEL_ID", None)
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            welcome_gif = getattr(config, "WELCOME_GIF", "")
            await channel.send(f"{member.mention}\n{welcome_gif}".strip())
        except discord.Forbidden:
            logger.error(f"[MODERATION] Нет прав для отправки приветствия в канал {channel_id}")
        except Exception as e:
            logger.error(f"[MODERATION] Ошибка приветствия: {e}")
            if hasattr(self.bot, "supervisor"):
                await self.bot.supervisor.handle_cog_error("moderation", e, ctx=None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()
        if not content:
            return
        msg_clean = content.lower()

        try:
            # --- Анти-спам ---
            now = time.monotonic()
            self._sweep(now)

            dq = self.history.setdefault(message.author.id, deque(maxlen=PER_USER_WINDOW_LIMIT))
            while dq and now - dq[0][0] > SPAM_INTERVAL_SECONDS:
                dq.popleft()
            dq.append((now, msg_clean, message))

            dups = [m for _, key, m in dq if key == msg_clean]
            if len(dups) >= MAX_REPEATED_MESSAGES:
                self.history[message.author.id] = deque(
                    (x for x in dq if x[1] != msg_clean), maxlen=PER_USER_WINDOW_LIMIT
                )
                await self._punish_spam(message.author, dups)
                return

            # --- Кастомные триггеры ---
            if "redhat говно" in msg_clean:
                await message.channel.send(REDHAT_INSULT_REPLY)
            elif "редхат" in msg_clean:
                await message.channel.send(REDHAT_VIDEO)

        except Exception as e:
            logger.error(f"[MODERATION Error] {e}", exc_info=e)
            if hasattr(self.bot, "supervisor"):
                await self.bot.supervisor.handle_cog_error("moderation", e, ctx=None)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationService(bot))
