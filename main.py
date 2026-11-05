import asyncio
import importlib
import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler

import discord
from discord.ext import commands

import config
from supervisor import SystemdSupervisor

COGS_DIR = "cogs"
REQ_FILE = "requirements.txt"

LOCAL_ERRORS = (
    commands.CommandNotFound,
    commands.CheckFailure,
    commands.MissingRequiredArgument,
    commands.BadArgument,
)


def setup_logging() -> logging.Logger:
    """Один root-логгер: файл с ротацией + консоль. Шумные библиотеки приглушены."""
    os.makedirs("logs", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        "logs/bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console], force=True)
    for noisy in ("discord.http", "httpx", "httpcore", "google_genai", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("systemd")


logger = setup_logging()


def _pip_install() -> bool:
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", REQ_FILE, "--no-cache-dir"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error("[SYSTEMD] Превышен таймаут установки pip (120с).")
        return False
    except Exception as e:
        logger.error(f"[SYSTEMD] Ошибка при вызове pip: {e}")
        return False

    if res.returncode != 0:
        logger.error(f"[SYSTEMD] Ошибка pip install: {res.stderr}")
        return False
    return True


async def try_pip_install_requirements() -> bool:
    """pip крутится в отдельном потоке, чтобы не вешать event loop."""
    if not os.path.exists(REQ_FILE):
        logger.error(f"[SYSTEMD] Файл {REQ_FILE} не найден.")
        return False

    logger.warning("[SYSTEMD] Запуск авто-установки зависимостей из requirements.txt...")
    ok = await asyncio.to_thread(_pip_install)
    if ok:
        importlib.invalidate_caches()
        logger.info("[SYSTEMD] Зависимости успешно восстановлены!")
    return ok


def _root_cause(err: BaseException) -> BaseException:
    return getattr(err, "original", None) or err.__cause__ or err


def _clean_name(name: str) -> str:
    return name.replace(".service", "").replace("cogs.", "").replace(".py", "")


class AdminCog(commands.Cog):
    """Админ-команды. Доступны только владельцу бота."""

    def __init__(self, bot: "SystemdBot"):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return await self.bot.is_owner(ctx.author)

    @commands.command(name="reload")
    async def reload_service(self, ctx, service_name: str = None):
        """Перезапуск указанного модуля или всех сразу."""
        if not service_name:
            await self.bot.load_all_cogs()
            await ctx.send("Все сервисы перезапущены.")
            return

        name = _clean_name(service_name)
        if await self.bot.supervisor.reload_service(name, reason="manual_reload"):
            await ctx.send(f"Сервис `{name}.service` успешно перезапущен.")
        else:
            await ctx.send(f"Не удалось перезапустить `{name}.service` (модуль в карантине или ожидает backoff).")

    @commands.command(name="revive")
    async def revive_service(self, ctx, service_name: str):
        """Сброс карантина и запуск сервиса."""
        name = _clean_name(service_name)
        self.bot.supervisor.revive_service(name)
        if await self.bot.supervisor.reload_service(name, reason="manual_revive"):
            await ctx.send(f"Сервис `{name}.service` выведен из карантина и запущен.")
        else:
            await ctx.send(f"Не удалось запустить `{name}.service` после вывода из карантина.")

    @commands.command(name="restart")
    async def restart_bot(self, ctx):
        """Полный перезапуск процесса бота."""
        await ctx.send("Перезапускаю процесс бота...")
        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)


class SystemdBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        # Отключаем события, которые боту не нужны: меньше трафика и CPU
        for name in (
            "typing", "presences", "voice_states", "invites", "webhooks",
            "integrations", "emojis_and_stickers", "guild_scheduled_events", "reactions",
        ):
            setattr(intents, name, False)

        super().__init__(
            command_prefix="!",
            intents=intents,
            max_messages=None,                # кэш сообщений боту не нужен
            chunk_guilds_at_startup=False,    # не качаем всех участников на старте
            # Модель не должна уметь пинговать @everyone / роли
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.supervisor = SystemdSupervisor(self)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, LOCAL_ERRORS):
            return
        cause = getattr(error, "original", error)
        cog_name = ctx.cog.__class__.__module__.split(".")[-1] if ctx.cog else None
        if cog_name and cog_name != "__main__":
            await self.supervisor.handle_cog_error(cog_name, cause, ctx=ctx)
        else:
            logger.error(f"[SYSTEMD] Ошибка команды: {cause!r}", exc_info=cause)

    async def _safe_load_extension(self, ext_path: str):
        if ext_path in self.extensions:
            await self.reload_extension(ext_path)
        else:
            await self.load_extension(ext_path)

    async def load_or_repair_cog(self, cog_name: str) -> bool:
        ext_path = f"cogs.{cog_name}"
        for attempt in (1, 2):
            try:
                await self._safe_load_extension(ext_path)
                logger.info(f"[SYSTEMD] Сервис {cog_name}.service успешно запущен.")
                return True
            except Exception as err:
                cause = _root_cause(err)

                if isinstance(cause, SyntaxError):
                    logger.critical(f"[SYSTEMD] Синтаксическая ошибка в {cog_name}.py: {cause}.")
                    return False

                if isinstance(cause, ImportError) and attempt == 1:
                    logger.error(f"[SYSTEMD] Сбой зависимостей в {cog_name}.service: {cause}")
                    if await try_pip_install_requirements():
                        continue
                    return False

                logger.error(f"[SYSTEMD] Ошибка запуска {cog_name}.service: {cause!r}", exc_info=err)
                return False
        return False

    async def load_all_cogs(self):
        os.makedirs(COGS_DIR, exist_ok=True)
        names = [
            filename[:-3]
            for filename in sorted(os.listdir(COGS_DIR))
            if filename.endswith(".py") and not filename.startswith("__")
        ]
        self.supervisor.register_known(names)
        for name in names:
            await self.load_or_repair_cog(name)

    async def setup_hook(self):
        await self.add_cog(AdminCog(self))
        await self.load_all_cogs()

    async def on_ready(self):
        logger.info(f"[SYSTEMD] Бот {self.user} успешно запущен и готов к работе!")


async def main():
    max_retries = 10
    retry_delay = 5

    for attempt in range(1, max_retries + 1):
        # Закрытого бота перезапускать нельзя, поэтому на каждую попытку новый экземпляр
        bot = SystemdBot()
        try:
            async with bot:
                await bot.start(config.TOKEN)
            return
        except discord.LoginFailure:
            logger.critical("[SYSTEMD] Неверный токен бота в config.py!")
            sys.exit(1)
        except Exception as e:
            logger.error(f"[SYSTEMD] Сбой сети/бота (Попытка {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                sys.exit(1)
            await asyncio.sleep(retry_delay)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[SYSTEMD] Работа бота остановлена вручную.")
