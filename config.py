import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
except ImportError:
    # Запасной вариант без зависимостей: main.py импортирует config ДО авто-установки pip,
    # так что падать из-за отсутствия python-dotenv нельзя.
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана переменная окружения {name}. "
            f"Добавь её в {ENV_PATH} (пример в .env.example)."
        )
    return value


# --- Секреты (из .env) ---
TOKEN = _require("DISCORD_TOKEN")
GEMINI_API_KEY = _require("GEMINI_API_KEY")

# --- Обычные настройки ---
CHANNEL_IDS = [
    YOURID,
    YOURID,
]

WELCOME_CHANNEL_ID = YOURID
WELCOME_GIF = "https://klipy.com/gifs/goida-radost"

RSS_URLS = [
    # --- Дистрибутивы / официальные новости ---
    "https://www.phoronix.com/rss.php",
    "https://archlinux.org/feeds/news/",
    "https://www.gentoo.org/feeds/news.xml",
    # "https://distrowatch.com/news/dw.xml",
    "https://micronews.debian.org/feeds/feed.rss",
    "https://blog.linuxmint.com/?feed=rss2",
    "https://news.opensuse.org/category/announcements/feed/",
    "https://fedoramagazine.org/feed/",
    "https://forum.endeavouros.com/c/important-notifications/125.rss",
    # --- Общие Linux-новости (добавлено) ---
    "https://www.omgubuntu.co.uk/feed",   # Ubuntu-новости, активен в 2026
    "https://itsfoss.com/feed/",          # гайды + новости, не проверял саму ссылку живьём
    "https://www.tecmint.com/feed/",      # туториалы + новости
    "https://9to5linux.com/feed/",        # не проверял саму ссылку живьём
    "https://linuxiac.com/feed/",         # не проверял саму ссылку живьём
]

CHECK_INTERVAL = 300
SENT_FILE = "sent_links.txt"
