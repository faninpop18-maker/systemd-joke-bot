import asyncio
import logging
import os
import time

import aiohttp
import discord

try:
    # Реальные исключения SDK google-genai (а не google.api_core, которые он не кидает)
    from google.genai import errors as genai_errors
    GENAI_API_ERROR = genai_errors.APIError
except ImportError:
    GENAI_API_ERROR = None

log = logging.getLogger("supervisor")

IGNORE_EXCEPTIONS = (discord.NotFound, discord.Forbidden)
FATAL_CODES = {401, 403}
TRANSIENT_CODES = {429, 500, 502, 503, 504}

_bg_tasks: set[asyncio.Task] = set()


def classify(error: BaseException) -> str:
    """ignore | fatal | transient | unexpected"""
    if isinstance(error, IGNORE_EXCEPTIONS):
        return "ignore"

    code = None
    if isinstance(error, discord.HTTPException):
        code = error.status
    elif GENAI_API_ERROR and isinstance(error, GENAI_API_ERROR):
        code = getattr(error, "code", None)

    if code in FATAL_CODES:
        return "fatal"
    if code in TRANSIENT_CODES:
        return "transient"
    # Прочие HTTP-ошибки (400 и т.п.) перезагрузкой кога не лечатся
    if isinstance(error, (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError)):
        return "transient"
    if GENAI_API_ERROR and isinstance(error, GENAI_API_ERROR):
        return "transient"
    return "unexpected"


class SystemdSupervisor:
    MAX_FAILURES = 3
    BASE_DELAY = 2.0
    MAX_DELAY = 60.0
    MIN_RELOAD_INTERVAL = 10.0  # защита от перезагрузки на каждое сообщение при горячей ошибке

    def __init__(self, bot):
        self.bot = bot
        self.service_states: dict[str, dict] = {}
        self.known_services: set[str] = set()
        self._lock = asyncio.Lock()

    def register_known(self, cog_names) -> None:
        """Вызывается ботом после сканирования cogs/, чтобы не бить по диску на каждой ошибке."""
        self.known_services.update(cog_names)

    def _state(self, cog: str) -> dict:
        return self.service_states.setdefault(
            cog,
            {
                "failures": 0,
                "opened_at": 0.0,
                "last_ok": 0.0,
                "quarantined": False,
                "in_progress": False,
                "retry_task": None,
            },
        )

    def _is_known(self, cog_name: str) -> bool:
        if self.known_services:
            return cog_name in self.known_services
        # До первого скана cogs/ (маловероятно, но на всякий случай) — запасной путь по диску
        return os.path.isfile(os.path.join("cogs", f"{cog_name}.py"))

    def revive_service(self, cog_name: str) -> bool:
        """Снимает карантин и сбрасывает счётчики ошибок."""
        st = self.service_states.get(cog_name)
        if not st:
            return False
        st.update(quarantined=False, failures=0, in_progress=False, opened_at=0.0, last_ok=0.0)
        log.info("[%s] Сервис вручную выведен из карантина.", cog_name)
        return True

    async def _quarantine(self, cog_name: str):
        async with self._lock:
            self._state(cog_name)["quarantined"] = True

    def _schedule_retry(self, cog_name: str, st: dict, delay: float):
        task = st["retry_task"]
        if task and not task.done():
            return  # уже запланирован, второй не нужен
        task = asyncio.create_task(self._delayed_retry(cog_name, delay))
        st["retry_task"] = task
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)

    async def _delayed_retry(self, cog_name: str, delay: float):
        await asyncio.sleep(delay)
        log.info("[%s] Повторная попытка перезапуска после backoff...", cog_name)
        await self.reload_service(cog_name, reason="backoff_retry")

    async def handle_cog_error(self, cog_name: str, error: Exception, ctx=None):
        """Классификация и обработка исключений из команд и фоновых задач."""
        if not self._is_known(cog_name):
            log.warning("[SUPERVISOR] Ошибка от неизвестного источника '%s': %r", cog_name, error)
            return

        kind = classify(error)

        if kind == "ignore":
            log.debug("[%s] Игнорируемое исключение: %r", cog_name, error)
        elif kind == "fatal":
            log.critical("[%s] Фатальная ошибка доступа (%r). В КАРАНТИН!", cog_name, error)
            await self._quarantine(cog_name)
        elif kind == "transient":
            # Сбой на стороне сети/API. Перезагрузка кога тут не поможет и только сотрёт его состояние.
            log.warning("[%s] Временный сбой (%r). Модуль продолжает работу.", cog_name, error)
        else:
            log.error("[%s] Непредвиденная ошибка в модуле: %r", cog_name, error, exc_info=error)
            await self.reload_service(cog_name, reason="unexpected_exception")

    def _register_failure(self, cog_name: str, st: dict):
        st["failures"] += 1
        st["opened_at"] = time.monotonic()
        if st["failures"] >= self.MAX_FAILURES:
            st["quarantined"] = True
            log.critical("[%s] Превышен лимит ошибок (%d/%d). В КАРАНТИН.", cog_name, st["failures"], self.MAX_FAILURES)
        else:
            log.error("[%s] Ошибка при перезагрузке (%d/%d).", cog_name, st["failures"], self.MAX_FAILURES)

    async def reload_service(self, cog_name: str, reason: str = "unknown") -> bool:
        """Безопасный перезапуск сервиса с backoff."""
        manual = reason.startswith("manual")

        async with self._lock:
            st = self._state(cog_name)

            if st["quarantined"]:
                log.error("[%s] Модуль в КАРАНТИНЕ. Перезапуск отменён.", cog_name)
                return False
            if st["in_progress"]:
                log.warning("[%s] Перезапуск уже выполняется. Отмена.", cog_name)
                return False

            now = time.monotonic()
            if not manual and now - st["last_ok"] < self.MIN_RELOAD_INTERVAL:
                log.debug("[%s] Недавно уже перезапускался, пропускаю (%s).", cog_name, reason)
                return False

            if st["failures"] > 0:
                delay = min(self.BASE_DELAY * 2 ** (st["failures"] - 1), self.MAX_DELAY)
                remaining = delay - (now - st["opened_at"])
                if remaining > 0:
                    log.info("[%s] Backoff ещё не истёк (%.1fс). Отложенный запуск.", cog_name, remaining)
                    self._schedule_retry(cog_name, st, remaining)
                    return False

            st["in_progress"] = True

        try:
            loader = getattr(self.bot, "load_or_repair_cog", None)
            if loader:
                ok = await loader(cog_name)
            else:
                await self.bot.reload_extension(f"cogs.{cog_name}")
                ok = True
        except Exception as e:
            log.error("[%s] Исключение при перезагрузке: %r", cog_name, e)
            ok = False

        async with self._lock:
            st["in_progress"] = False
            if ok:
                st.update(failures=0, opened_at=0.0, last_ok=time.monotonic())
                log.info("[%s] Сервис успешно перезагружен (причина: %s)", cog_name, reason)
                return True
            self._register_failure(cog_name, st)
            return False
