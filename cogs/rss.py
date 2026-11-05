import asyncio
import logging
import os
import time

import aiofiles
import aiohttp
import discord
import feedparser
from discord.ext import commands

import config

log = logging.getLogger("cogs.rss")

SEEN_LIMIT = 5000            # сколько ссылок храним в sent-файле
ENTRIES_PER_FEED = 5
SEED_ENTRIES_PER_FEED = 3    # сколько запостить с каждого фида при первом запуске (пустая база)
REQUEST_TIMEOUT = 20         # было 300с: один зависший фид блокировал весь цикл на 5 минут
MAX_LOOP_ERRORS = 5
RATE_LIMIT_PAUSE = 3 * 3600
FETCH_CONCURRENCY = 3        # сколько фидов тянем одновременно вместо строго по одному

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36 SystemDBot/1.0"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml; q=0.9, */*; q=0.8",
}

NOT_MODIFIED = object()
RATE_LIMITED = object()

_bg_tasks: set[asyncio.Task] = set()


class RSSService(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.seen: set[str] = set()
        self.session: aiohttp.ClientSession | None = None
        self.bg_task: asyncio.Task | None = None
        self.etags: dict[str, str] = {}
        self.modifieds: dict[str, str] = {}
        self.disabled_until: dict[str, float] = {}
        self._fetch_sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        self._save_lock = asyncio.Lock()

    async def cog_load(self):
        await self.load_seen()
        if not self.seen:
            log.info(
                f"[RSS] Пустая база ссылок: для каждого фида запостим "
                f"не больше {SEED_ENTRIES_PER_FEED} последних новостей."
            )

        self.session = aiohttp.ClientSession(
            headers=HEADERS, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        )
        self.bg_task = asyncio.create_task(self.check_news())

    async def cog_unload(self):
        if self.bg_task:
            self.bg_task.cancel()
        if self.session:
            await self.session.close()

    # ---------- хранилище ----------

    async def load_seen(self):
        if not os.path.exists(config.SENT_FILE):
            return
        async with aiofiles.open(config.SENT_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in (await f.read()).splitlines() if ln.strip()]

        if len(lines) > SEEN_LIMIT:  # файл не должен расти вечно
            lines = lines[-SEEN_LIMIT:]
            async with aiofiles.open(config.SENT_FILE, "w", encoding="utf-8") as f:
                await f.write("\n".join(lines) + "\n")
        self.seen = set(lines)

    async def save_seen(self, links: list[str]):
        """Одна запись на пачку ссылок вместо открытия файла на каждую. Лок — фиды теперь тянутся параллельно."""
        async with self._save_lock:
            async with aiofiles.open(config.SENT_FILE, "a", encoding="utf-8") as f:
                await f.write("\n".join(links) + "\n")

    # ---------- сеть ----------

    async def fetch_feed(self, url: str):
        req_headers = {}
        if url in self.etags:
            req_headers["If-None-Match"] = self.etags[url]
        if url in self.modifieds:
            req_headers["If-Modified-Since"] = self.modifieds[url]

        try:
            async with self.session.get(url, headers=req_headers) as response:
                if response.status == 429:
                    return RATE_LIMITED
                if response.status == 304:
                    return NOT_MODIFIED
                if response.status != 200:
                    log.warning(f"[RSS] Ошибка {url}: статус {response.status}")
                    return None

                if etag := response.headers.get("ETag"):
                    self.etags[url] = etag
                if modified := response.headers.get("Last-Modified"):
                    self.modifieds[url] = modified

                text = await response.text()
        except Exception as e:
            log.error(f"[RSS] Ошибка соединения при запросе к {url}: {e}")
            return None

        if not text.strip():
            return None
        # feedparser синхронный и жрёт CPU: выносим из event loop
        return await asyncio.to_thread(feedparser.parse, text)

    # ---------- постинг ----------

    async def _post_news(self, title: str, link: str):
        """pizdelkasliv.py удалён: раньше новость уходила ему через dispatch,
        теперь постим напрямую по каналам из config.CHANNEL_IDS."""
        message = f"**{title}**\n{link}"
        channels = [
            ch for cid in config.CHANNEL_IDS
            if isinstance(ch := self.bot.get_channel(cid), discord.TextChannel)
        ]
        if not channels:
            log.warning("[RSS] Ни один канал из CHANNEL_IDS не найден/не доступен боту.")
            return

        results = await asyncio.gather(
            *(ch.send(message) for ch in channels), return_exceptions=True
        )
        for ch, res in zip(channels, results):
            if isinstance(res, Exception):
                log.error(f"[RSS] Ошибка отправки в канал {ch.id}: {res}")

    # ---------- основной цикл ----------

    async def _process_url(self, url: str):
        async with self._fetch_sem:  # не больше FETCH_CONCURRENCY фидов одновременно
            until = self.disabled_until.get(url)
            if until:
                if time.time() < until:
                    return
                del self.disabled_until[url]

            feed = await self.fetch_feed(url)

        if feed is RATE_LIMITED:
            self.disabled_until[url] = time.time() + RATE_LIMIT_PAUSE
            log.warning(f"[RSS] Лимит запросов (429) для {url}. Отключаем парсинг на 3 часа.")
            return
        if feed is NOT_MODIFIED or feed is None or not getattr(feed, "entries", None):
            return

        new_items = []
        for entry in feed.entries[:ENTRIES_PER_FEED]:
            link = getattr(entry, "link", None)
            if link and link not in self.seen:
                self.seen.add(link)
                new_items.append((getattr(entry, "title", "Без названия"), link))

        if not new_items:
            return

        await self.save_seen([link for _, link in new_items])

        # Если ВСЕ просмотренные записи фида оказались новыми — это либо фид видим
        # впервые (только что добавили в config.RSS_URLS), либо бот долго не проверял
        # его (простой/рестарт). В обоих случаях не флудим — остальное уже помечено seen.
        checked = min(len(feed.entries), ENTRIES_PER_FEED)
        is_fresh_or_catchup = len(new_items) == checked
        to_post = new_items[:SEED_ENTRIES_PER_FEED] if is_fresh_or_catchup else new_items

        # Старые новости первыми, чтобы в канале порядок был хронологический
        for title, link in reversed(to_post):
            await self._post_news(title, link)

    async def _escalate(self, error: Exception):
        if hasattr(self.bot, "supervisor"):
            await self.bot.supervisor.handle_cog_error("rss", error)

    async def check_news(self):
        await self.bot.wait_until_ready()
        errors = 0

        while not self.bot.is_closed():
            try:
                # FETCH_CONCURRENCY ограничивает число одновременных запросов,
                # отдельные сбои не прерывают обработку остальных фидов
                results = await asyncio.gather(
                    *(self._process_url(url) for url in config.RSS_URLS),
                    return_exceptions=True,
                )
                for url, res in zip(config.RSS_URLS, results):
                    if isinstance(res, Exception):
                        log.error(f"[RSS] Сбой при обработке {url}: {res}", exc_info=res)

                errors = 0
                await asyncio.sleep(config.CHECK_INTERVAL)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                errors += 1
                log.error(f"[RSS] Сбой в цикле RSS ({errors}/{MAX_LOOP_ERRORS}): {e}", exc_info=e)
                if errors >= MAX_LOOP_ERRORS:
                    # Эскалация в ОТДЕЛЬНОЙ задаче: reload вызывает cog_unload, который
                    # отменяет bg_task, и иначе мы бы отменили сами себя посреди перезагрузки.
                    task = asyncio.create_task(self._escalate(e))
                    _bg_tasks.add(task)
                    task.add_done_callback(_bg_tasks.discard)
                    return
                await asyncio.sleep(min(30 * errors, 300))


async def setup(bot):
    await bot.add_cog(RSSService(bot))
