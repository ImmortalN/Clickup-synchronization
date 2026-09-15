#!/usr/bin/env python3
"""
GitHub Dev Docs → Intercom Internal Guides
Объединяет:
  - Wiki JetFormBuilder (несколько ключевых страниц)
  - Все markdown-файлы из Crocoblock/developer-documentation
"""

import os
import re
import time
import html
import logging
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

# --- Wiki JetFormBuilder (фиксированный список) ---
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

# Можно ограничить только нужные папки (оставь пустым = все)
# Пример: ["03-jet-form-builder", "01-jet-engine"]
DEVDOCS_INCLUDE_PREFIXES = []   # [] = синхронизируем всё

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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==============================

def slugify_path(path: str) -> str:
    """Делает стабильный id из пути файла."""
    s = path.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:80]  # ограничение длины


def human_title_from_path(path: str) -> str:
    """
    01-jet-engine/01-hooks/01-listings/actions.md
    → JetEngine · Hooks · Listings · Actions
    """
    parts = path.replace(".md", "").replace(".MD", "").split("/")
    clean = []
    for p in parts:
        # убираем номер в начале (01-, 02- ...)
        p = re.sub(r"^\d+-", "", p)
        p = p.replace("-", " ").replace("_", " ").strip()
        if p.lower() in ("readme",):
            continue
        clean.append(p.title())
    return " · ".join(clean) if clean else path


def get_wiki_markdown(slug: str) -> str | None:
    url = f"https://raw.githubusercontent.com/wiki/Crocoblock/jetformbuilder/{slug}.md"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            return r.text
        log.error(f"Wiki {slug}: HTTP {r.status_code}")
    except Exception as e:
        log.error(f"Wiki {slug}: {e}")
    return None


def get_devdocs_tree() -> list[dict]:
    """Получаем все файлы репозитория через Git Trees API (recursive)."""
    url = f"https://api.github.com/repos/{DEVDOCS_OWNER}/{DEVDOCS_REPO}/git/trees/{DEVDOCS_BRANCH}?recursive=1"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            log.error(f"Не удалось получить дерево: {r.status_code} {r.text[:200]}")
            return []
        tree = r.json().get("tree", [])
        # только markdown-файлы
        md_files = [
            item for item in tree
            if item["type"] == "blob"
            and item["path"].lower().endswith((".md", ".markdown"))
            and not item["path"].startswith(".")
        ]
        return md_files
    except Exception as e:
        log.error(f"Ошибка get_devdocs_tree: {e}")
        return []


def get_raw_content(path: str) -> str | None:
    url = f"https://raw.githubusercontent.com/{DEVDOCS_OWNER}/{DEVDOCS_REPO}/{DEVDOCS_BRANCH}/{path}"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            return r.text
        log.warning(f"Не удалось скачать {path}: {r.status_code}")
    except Exception as e:
        log.warning(f"Ошибка скачивания {path}: {e}")
    return None


def find_article_by_id(article_id: str):
    """Ищем internal article по [id] в конце title."""
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


def create_or_update(article_id: str, title: str, body_md: str) -> str:
    """
    Создаёт или обновляет internal article.
    Возвращает: created | updated | error
    """
    body_html = markdown(body_md, extensions=["fenced_code", "nl2br", "tables"])
    new_title = f"{title} [{article_id}]"[:255]
    new_body = f"<h1>{html.escape(title)}</h1>{body_html}"[:100000]

    existing = find_article_by_id(article_id)

    payload = {
        "title": new_title,
        "body": new_body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "ai_chatbot_availability": True,  # Service / Fin
        # "ai_copilot_availability": True,  # раскомментируй при необходимости
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
# ОСНОВНЫЕ РЕЖИМЫ
# ==============================

def sync_wiki():
    log.info("=== СИНХРОНИЗАЦИЯ WIKI JetFormBuilder ===")
    stats = {"created": 0, "updated": 0, "error": 0}

    for page in WIKI_PAGES:
        md = get_wiki_markdown(page["slug"])
        if not md:
            stats["error"] += 1
            continue
        result = create_or_update(page["id"], page["title"], md)
        stats[result if result in stats else "error"] += 1
        time.sleep(0.35)

    log.info(f"Wiki готово: {stats}")
    return stats


def sync_devdocs():
    log.info("=== СИНХРОНИЗАЦИЯ developer-documentation ===")
    tree = get_devdocs_tree()
    log.info(f"Найдено markdown-файлов: {len(tree)}")

    stats = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    for item in tree:
        path = item["path"]

        # фильтр по префиксам (если задан)
        if DEVDOCS_INCLUDE_PREFIXES:
            if not any(path.startswith(p) for p in DEVDOCS_INCLUDE_PREFIXES):
                stats["skipped"] += 1
                continue

        # пропускаем слишком общие README в корне папок (по желанию)
        # if path.lower().endswith("readme.md") and path.count("/") <= 1:
        #     stats["skipped"] += 1
        #     continue

        md = get_raw_content(path)
        if not md:
            stats["error"] += 1
            continue

        article_id = "devdocs-" + slugify_path(path)
        title = human_title_from_path(path)

        result = create_or_update(article_id, title, md)
        stats[result if result in stats else "error"] += 1
        time.sleep(0.3)

    log.info(f"DevDocs готово: {stats}")
    return stats


def main():
    log.info("=== СТАРТ GitHub Dev Docs Sync ===")
    log.info(f"Папка Intercom: {TARGET_FOLDER_ID}")

    total = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    # 1. Wiki
    wiki_stats = sync_wiki()
    for k in total:
        total[k] += wiki_stats.get(k, 0)

    # 2. developer-documentation
    dev_stats = sync_devdocs()
    for k in total:
        total[k] += dev_stats.get(k, 0)

    log.info(
        f"=== ВСЁ ЗАВЕРШЕНО | "
        f"создано: {total['created']}, "
        f"обновлено: {total['updated']}, "
        f"пропущено: {total['skipped']}, "
        f"ошибок: {total['error']} ==="
    )


if __name__ == "__main__":
    main()
