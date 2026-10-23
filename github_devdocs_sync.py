"""
GitHub Dev Docs + Gists → Intercom Internal Guides (единый скрипт)

Источники:
  1. Wiki JetFormBuilder (фиксированный список страниц) → папка TARGET_FOLDER_ID
  2. Все полезные .md из Crocoblock/developer-documentation → папка TARGET_FOLDER_ID
  3. Публичные Gists Crocoblock → папка GISTS_FOLDER_ID (5990385)

Оптимизации:
  - сравнение Last-Modified / updated_at источника с updated_at Intercom → skip если актуально
  - пропуск README и коротких оглавлений (только ссылки)
  - ai_chatbot_availability = True (тоггл Service)

ТЕСТОВЫЙ РЕЖИМ ДЛЯ GISTS:
  GISTS_TEST_MODE = True  → синхронизируется только один гист (GISTS_TEST_ID)
  Когда всё ок — поставь GISTS_TEST_MODE = False
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

TARGET_FOLDER_ID = 5654984          # wiki + developer-documentation
GISTS_FOLDER_ID = 5990385           # гисты Crocoblock

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

# --- Gists Crocoblock ---
GISTS_USERNAME = "Crocoblock"
# ТЕСТ: True = только один гист; False = все публичные гисты пользователя
GISTS_TEST_MODE = False
# ID гиста для теста (https://gist.github.com/Crocoblock/5f588a0d8bbb5ebe73633cfe5eedd5df)
GISTS_TEST_ID = "5f588a0d8bbb5ebe73633cfe5eedd5df"

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


def parse_iso_ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
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


# Кэш: article_id (то, что в [brackets] в title) → объект статьи Intercom
_articles_index: dict[str, dict] | None = None


def load_articles_index(force: bool = False) -> dict[str, dict]:
    """
    Один раз загружает ВСЕ internal articles и строит словарь по [id] в title.
    Без этого find_article_by_id на каждый гист листает весь каталог заново → часы.
    """
    global _articles_index
    if _articles_index is not None and not force:
        return _articles_index

    index: dict[str, dict] = {}
    page = 1
    total_pages = 1
    log.info("📥 Загрузка индекса Intercom internal articles…")

    while page <= total_pages:
        try:
            r = ic.get(
                f"{INTERCOM_BASE}/internal_articles",
                params={"page": page, "per_page": 50},
                timeout=30,
            )
        except Exception as e:
            log.error(f"index page {page}: {e}")
            break

        if r.status_code != 200:
            log.error(f"index page {page}: HTTP {r.status_code}")
            break

        data = r.json()
        total_pages = data.get("pages", {}).get("total_pages", page) or page

        for art in data.get("data", []):
            title = art.get("title") or ""
            # все [id] в title (обычно один)
            for m in re.finditer(r"\[([^\]]+)\]", title):
                index[m.group(1)] = art

        if page % 10 == 0 or page == total_pages:
            log.info(f"  …страница {page}/{total_pages}, в индексе: {len(index)}")
        page += 1
        time.sleep(0.05)

    _articles_index = index
    log.info(f"✅ Индекс готов: {len(index)} статей")
    return index


def find_article_by_id(article_id: str):
    """O(1) поиск по заранее загруженному индексу."""
    idx = load_articles_index()
    return idx.get(article_id)


def index_put(article_id: str, art: dict):
    """Обновить индекс после create/update (чтобы не перечитывать всё)."""
    if _articles_index is not None and art:
        _articles_index[article_id] = art


def create_or_update(
    article_id: str,
    title: str,
    body_md: str = "",
    source_ts: float = 0.0,
    existing: dict | None = None,
    folder_id: int | None = None,
    body_html: str | None = None,
) -> str:
    """create / update / skipped / error.

    Передай либо body_md (будет прогнан через markdown),
    либо готовый body_html (для гистов — чтобы сохранить переносы в коде).
    """
    target_folder = folder_id if folder_id is not None else TARGET_FOLDER_ID

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

    if body_html is None:
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
            or target_folder
        )
        r = ic.put(
            f"{INTERCOM_BASE}/internal_articles/{existing['id']}",
            json=payload,
            timeout=30,
        )
        if r.status_code in (200, 201):
            log.info(f"✅ Обновлено: {title}")
            try:
                index_put(article_id, r.json())
            except Exception:
                pass
            return "updated"
        log.error(f"❌ Update error ({r.status_code}): {r.text[:250]}")
        return "error"
    else:
        payload["folder_id"] = target_folder
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload, timeout=30)
        if r.status_code in (200, 201):
            log.info(f"✅ Создано: {title}")
            try:
                index_put(article_id, r.json())
            except Exception:
                pass
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
# GISTS
# ==============================

def _lang_to_fence(lang: str | None) -> str:
    if not lang:
        return ""
    mapping = {
        "PHP": "php",
        "JavaScript": "javascript",
        "TypeScript": "typescript",
        "CSS": "css",
        "HTML": "html",
        "Markdown": "markdown",
        "Python": "python",
        "Shell": "bash",
        "JSON": "json",
        "YAML": "yaml",
        "Text": "",
    }
    return mapping.get(lang, lang.lower() if lang else "")


def fetch_gist(gist_id: str) -> dict | None:
    """Возвращает полный объект гиста или None."""
    url = f"https://api.github.com/gists/{gist_id}"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            log.error(f"Gist {gist_id}: HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        log.error(f"Gist {gist_id}: {e}")
        return None


def list_user_gists(username: str, max_pages: int = 20) -> list[dict]:
    """Список публичных гистов пользователя (id, description, updated_at)."""
    gists = []
    page = 1
    while page <= max_pages:
        url = f"https://api.github.com/users/{username}/gists"
        try:
            r = requests.get(url, params={"per_page": 100, "page": page}, timeout=30)
            if r.status_code != 200:
                log.error(f"List gists page {page}: {r.status_code}")
                break
            batch = r.json()
            if not batch:
                break
            gists.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            time.sleep(0.2)
        except Exception as e:
            log.error(f"list_user_gists: {e}")
            break
    return gists


def gist_to_html(gist: dict) -> str:
    """
    Собирает HTML-тело гайда из гиста.
    Переносы строк → <br>, табы → 4 пробела.
    Так Intercom не схлопывает код.
    """
    parts = []
    desc = (gist.get("description") or "").strip()
    html_url = gist.get("html_url") or ""

    if desc:
        parts.append(f"<p>{html.escape(desc)}</p>")

    if html_url:
        safe_url = html.escape(html_url, quote=True)
        parts.append(
            f'<p><strong>Source:</strong> '
            f'<a href="{safe_url}">{html.escape(html_url)}</a></p>'
        )

    files = gist.get("files") or {}
    for fname, fobj in files.items():
        content = fobj.get("content") or ""
        # Нормализуем переносы и табы
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        content = content.replace("\t", "    ")
        content = content.rstrip("\n")
        lang = _lang_to_fence(fobj.get("language"))
        cls = f' class="language-{html.escape(lang)}"' if lang else ""
        safe_name = html.escape(fname)
        # escape + каждый \n → <br>\n (Intercom сохраняет <br>)
        safe_code = html.escape(content).replace("\n", "<br>\n")
        parts.append(f"<h3><code>{safe_name}</code></h3>")
        parts.append(
            '<pre style="white-space:pre-wrap; overflow-x:auto; '
            'background:#f6f8fa; padding:12px; border-radius:6px; '
            'font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; '
            'font-size:13px; line-height:1.45;">'
            f"<code{cls}>{safe_code}</code></pre>"
        )

    return "\n".join(parts)



def sync_gists() -> dict:
    log.info("=== СИНХРОНИЗАЦИЯ Gists Crocoblock → folder %s ===", GISTS_FOLDER_ID)
    if GISTS_TEST_MODE:
        log.info("⚠ ТЕСТОВЫЙ РЕЖИМ: только гист %s", GISTS_TEST_ID)

    stats = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    # Индекс Intercom один раз (а не на каждый гист)
    load_articles_index()

    if GISTS_TEST_MODE:
        listed = [{"id": GISTS_TEST_ID, "description": "", "updated_at": ""}]
    else:
        listed = list_user_gists(GISTS_USERNAME)
        log.info("Найдено публичных гистов: %s", len(listed))

    for i, item in enumerate(listed, 1):
        gist_id = item["id"]
        article_id = f"gist-{gist_id}"
        list_ts = parse_iso_ts(item.get("updated_at"))
        existing = find_article_by_id(article_id)

        # Ранний skip по дате из списка (без скачивания тела гиста)
        if existing and not GISTS_TEST_MODE:
            intercom_ts = parse_intercom_ts(existing.get("updated_at"))
            if list_ts and not is_source_newer(list_ts, intercom_ts):
                log.info(
                    f"⏭ [{i}/{len(listed)}] актуально: {article_id}"
                )
                stats["skipped"] += 1
                continue

        gist = fetch_gist(gist_id)
        if not gist:
            stats["error"] += 1
            continue

        source_ts = parse_iso_ts(gist.get("updated_at")) or list_ts
        title = (gist.get("description") or item.get("description") or "").strip()
        if not title:
            title = f"Gist {gist_id[:8]}"
        if len(title) > 180:
            title = title[:177] + "…"

        body_html = gist_to_html(gist)
        if not body_html or len(body_html) < 20:
            log.info("⏭ Пропуск (пустой контент): %s", gist_id)
            stats["skipped"] += 1
            continue

        # В тестовом режиме всегда обновляем
        if GISTS_TEST_MODE and existing:
            source_ts = parse_intercom_ts(existing.get("updated_at")) + DATE_BUFFER_SECONDS + 10

        result = create_or_update(
            article_id,
            title,
            body_md="",
            source_ts=source_ts,
            existing=existing,
            folder_id=GISTS_FOLDER_ID,
            body_html=body_html,
        )
        stats[result if result in stats else "error"] += 1

        if i % 25 == 0:
            log.info(f"… прогресс {i}/{len(listed)} | {stats}")

        time.sleep(0.12)  # было 0.3

    log.info(f"Gists готово: {stats}")
    return stats



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
    import sys

    # Режим: без аргументов / "all" → wiki + devdocs + gists
    #         "gists" → только гисты (для теста)
    mode = (sys.argv[1] if len(sys.argv) > 1 else "all").strip().lower()

    log.info("=== СТАРТ GitHub Dev Docs + Gists Sync ===")
    log.info(f"Режим: {mode}")
    log.info(f"Папка Intercom (wiki/devdocs): {TARGET_FOLDER_ID}")
    log.info(f"Папка Intercom (gists): {GISTS_FOLDER_ID}")
    log.info(f"GISTS_TEST_MODE = {GISTS_TEST_MODE}")

    # Один раз на весь прогон (wiki + devdocs + gists)
    load_articles_index(force=True)

    total = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    if mode == "gists":
        parts = (sync_gists,)
    else:
        parts = (sync_wiki, sync_devdocs, sync_gists)

    for part in parts:
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
