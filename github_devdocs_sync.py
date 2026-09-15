#!/usr/bin/env python3
"""
GitHub Dev Docs → Intercom Internal Guides (единый скрипт)

Источники:
  1. Wiki JetFormBuilder (фиксированный список страниц)
  2. Все полезные .md из Crocoblock/developer-documentation

Оптимизации:
  - сравнение Last-Modified источника с updated_at Intercom → skip если актуально
  - пропуск README и коротких оглавлений (только ссылки)
  - ai_chatbot_availability = True (тоггл Service)
"""

import os
import re
import time
import html
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from markdown import markdown
from dotenv import load_dotenv

load_dotenv()

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

TARGET_FOLDER_ID = 5654984

# --- Wiki JetFormBuilder ---
WIKI_PAGES = [
    {"id": "wiki-PHP-Hooks", "slug": "PHP-Hooks", "title": "PHP Hooks"},
    {"id": "wiki-Custom-JS-for-fields", "slug": "Custom-JS-for-fields", "title": "Custom JS for fields"},
    {"id": "wiki-Frontend-Macros---External-Macros", "slug": "Frontend-Macros---External-Macros", "title": "Frontend Macros - External Macros"},
    {"id": "wiki-Frontend-Macros---Field-Attributes", "slug": "Frontend-Macros---Field-Attributes", "title": "Frontend Macros - Field Attributes"},
    {"id": "wiki-Frontend-Macros---Filters", "slug": "Frontend-Macros---Filters", "title": "Frontend Macros - Filters"},
    {"id": "wiki-JS-Hooks", "slug": "JS-Hooks", "title": "JS Hooks"},
    {"id": "wiki-Understanding-reactivity", "slug": "Understanding-reactivity", "title": "Understanding reactivity"},
]

# --- developer-documentation ---
DEVDOCS_OWNER = "Crocoblock"
DEVDOCS_REPO = "developer-documentation"
DEVDOCS_BRANCH = "main"

# [] = все файлы; можно ограничить, например: ["03-jet-form-builder", "01-jet-engine"]
DEVDOCS_INCLUDE_PREFIXES = []

# Буфер в секундах: источник должен быть новее минимум на столько
DATE_BUFFER_SECONDS = 30

# Минимальная длина полезного текста (после очистки от ссылок/заголовков)
MIN_USEFUL_TEXT_LEN = 120

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json",
})

# ==============================
# ДАТЫ
# ==============================

def parse_intercom_ts(value) -> float:
    if value is None:
        return 0.0
    try:
        num = float(value)
        if num > 1e12:
            return num / 1000.0
        return num
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            pass
    return 0.0


def parse_last_modified(header_value: str | None) -> float:
    if not header_value:
        return 0.0
    try:
        dt = parsedate_to_datetime(header_value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def format_ts(ts: float) -> str:
    if not ts:
        return "нет"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return f"invalid({ts})"


def is_source_newer(source_ts: float, intercom_ts: float) -> bool:
    if not source_ts:
        return True  # даты нет — лучше обновить
    return source_ts > intercom_ts + DATE_BUFFER_SECONDS


# ==============================
# ФИЛЬТР README / ОГЛАВЛЕНИЙ
# ==============================

def is_index_or_readme(path: str, content: str) -> bool:
    """
    True = файл не нужен (README или почти пустое оглавление со ссылками).
    """
    name = path.split("/")[-1].lower()

    # 1. Все README
    if name in ("readme.md", "readme.markdown", "readme"):
        return True

    # 2. Очень короткие файлы (скорее всего только ссылки)
    text = re.sub(r"\[.*?\]\(.*?\)", "", content)   # [text](url)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)      # картинки
    text = re.sub(r"#+\s*", "", text)               # заголовки
    text = re.sub(r"[-*+]\s*", "", text)            # маркеры списков
    text = re.sub(r"`{1,3}.*?`{1,3}", "", text, flags=re.DOTALL)  # код
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < MIN_USEFUL_TEXT_LEN:
        return True

    return False


# ==============================
# ВСПОМОГАТЕЛЬНЫЕ
# ==============================

def slugify_path(path: str) -> str:
    s = path.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:80]


def human_title_from_path(path: str) -> str:
    """
    01-jet-engine/01-hooks/01-listings/actions.md
    → JetEngine · Hooks · Listings · Actions
    """
    parts = path.replace(".md", "").replace(".MD", "").split("/")
    clean = []
    for p in parts:
        p = re.sub(r"^\d+-", "", p)
        p = p.replace("-", " ").replace("_", " ").strip()
        if p.lower() in ("readme",):
            continue
        clean.append(p.title())
    return " · ".join(clean) if clean else path


def find_article_by_id(article_id: str):
    page = 1
    while True:
        r = ic.get(
            f"{INTERCOM_BASE}/internal_articles",
            params={"page": page, "per_page": 50},
            timeout=30,
        )
        if r.status_code != 200:
            break
        data = r.json()
        for art in data.get("data", []):
            if f"[{article_id}]" in art.get("title", ""):
                return art
        if page >= data.get("pages", {}).get("total_pages", 1):
            break
        page += 1
        time.sleep(0.12)
    return None


def create_or_update(
    article_id: str,
    title: str,
    body_md: str,
    source_ts: float = 0.0,
    existing: dict | None = None,
) -> str:
    """create / update / skipped / error"""
    if existing is None:
        existing = find_article_by_id(article_id)

    # --- Проверка даты ---
    if existing:
        intercom_ts = parse_intercom_ts(existing.get("updated_at"))
        if not is_source_newer(source_ts, intercom_ts):
            log.info(
                f"⏭ Пропущено (актуально): {title} | "
                f"src: {format_ts(source_ts)} ≤ IC: {format_ts(intercom_ts)}"
            )
            return "skipped"

    body_html = markdown(body_md, extensions=["fenced_code", "nl2br", "tables"])
    new_title = f"{title} [{article_id}]"[:255]
    new_body = f"<h1>{html.escape(title)}</h1>{body_html}"[:100000]

    payload = {
        "title": new_title,
        "body": new_body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "ai_chatbot_availability": True,  # Service / Fin
    }

    if existing:
        payload["folder_id"] = (
            existing.get("parent_id")
            or existing.get("folder_id")
            or TARGET_FOLDER_ID
        )
        r = ic.put(
            f"{INTERCOM_BASE}/internal_articles/{existing['id']}",
            json=payload,
            timeout=30,
        )
        if r.status_code in (200, 201):
            log.info(f"✅ Обновлено: {title}")
            return "updated"
        log.error(f"❌ Update error ({r.status_code}): {r.text[:250]}")
        return "error"
    else:
        payload["folder_id"] = TARGET_FOLDER_ID
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload, timeout=30)
        if r.status_code in (200, 201):
            log.info(f"✅ Создано: {title}")
            return "created"
        log.error(f"❌ Create error ({r.status_code}): {r.text[:250]}")
        return "error"


# ==============================
# ПОЛУЧЕНИЕ КОНТЕНТА + ДАТЫ
# ==============================

def fetch_wiki(slug: str) -> tuple[str | None, float]:
    url = f"https://raw.githubusercontent.com/wiki/Crocoblock/jetformbuilder/{slug}.md"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            log.error(f"Wiki {slug}: HTTP {r.status_code}")
            return None, 0.0
        ts = parse_last_modified(r.headers.get("Last-Modified"))
        return r.text, ts
    except Exception as e:
        log.error(f"Wiki {slug}: {e}")
        return None, 0.0


def get_devdocs_tree() -> list[dict]:
    url = (
        f"https://api.github.com/repos/{DEVDOCS_OWNER}/{DEVDOCS_REPO}"
        f"/git/trees/{DEVDOCS_BRANCH}?recursive=1"
    )
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            log.error(f"Tree error: {r.status_code} {r.text[:200]}")
            return []
        tree = r.json().get("tree", [])
        return [
            item for item in tree
            if item["type"] == "blob"
            and item["path"].lower().endswith((".md", ".markdown"))
            and not item["path"].startswith(".")
        ]
    except Exception as e:
        log.error(f"get_devdocs_tree: {e}")
        return []


def fetch_devdocs_file(path: str) -> tuple[str | None, float]:
    url = (
        f"https://raw.githubusercontent.com/{DEVDOCS_OWNER}/{DEVDOCS_REPO}"
        f"/{DEVDOCS_BRANCH}/{path}"
    )
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            log.warning(f"Не удалось скачать {path}: {r.status_code}")
            return None, 0.0
        ts = parse_last_modified(r.headers.get("Last-Modified"))
        return r.text, ts
    except Exception as e:
        log.warning(f"Ошибка {path}: {e}")
        return None, 0.0


# ==============================
# СИНХРОНИЗАЦИЯ
# ==============================

def sync_wiki() -> dict:
    log.info("=== СИНХРОНИЗАЦИЯ WIKI JetFormBuilder ===")
    stats = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    for page in WIKI_PAGES:
        md, source_ts = fetch_wiki(page["slug"])
        if not md:
            stats["error"] += 1
            continue

        existing = find_article_by_id(page["id"])
        result = create_or_update(
            page["id"], page["title"], md, source_ts=source_ts, existing=existing
        )
        stats[result if result in stats else "error"] += 1
        time.sleep(0.3)

    log.info(f"Wiki готово: {stats}")
    return stats


def sync_devdocs() -> dict:
    log.info("=== СИНХРОНИЗАЦИЯ developer-documentation ===")
    tree = get_devdocs_tree()
    log.info(f"Найдено markdown-файлов: {len(tree)}")

    stats = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    for item in tree:
        path = item["path"]

        # Фильтр по префиксам (если задан)
        if DEVDOCS_INCLUDE_PREFIXES:
            if not any(path.startswith(p) for p in DEVDOCS_INCLUDE_PREFIXES):
                stats["skipped"] += 1
                continue

        md, source_ts = fetch_devdocs_file(path)
        if not md:
            stats["error"] += 1
            continue

        # --- Пропуск README и коротких оглавлений ---
        if is_index_or_readme(path, md):
            log.info(f"⏭ Пропуск (README/оглавление): {path}")
            stats["skipped"] += 1
            continue

        article_id = "devdocs-" + slugify_path(path)
        title = human_title_from_path(path)

        existing = find_article_by_id(article_id)
        result = create_or_update(
            article_id, title, md, source_ts=source_ts, existing=existing
        )
        stats[result if result in stats else "error"] += 1
        time.sleep(0.25)

    log.info(f"DevDocs готово: {stats}")
    return stats


def main():
    log.info("=== СТАРТ GitHub Dev Docs Sync ===")
    log.info(f"Папка Intercom: {TARGET_FOLDER_ID}")

    total = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    for part in (sync_wiki, sync_devdocs):
        stats = part()
        for k in total:
            total[k] += stats.get(k, 0)

    log.info(
        f"=== ВСЁ ЗАВЕРШЕНО | "
        f"создано: {total['created']}, "
        f"обновлено: {total['updated']}, "
        f"пропущено: {total['skipped']}, "
        f"ошибок: {total['error']} ==="
    )


if __name__ == "__main__":
    main()
