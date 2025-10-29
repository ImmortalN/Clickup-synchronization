#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import html
import logging
from datetime import datetime, timedelta, timezone

import requests
from markdown import markdown
from dotenv import load_dotenv

# ==============================
# 1. Загрузка конфигурации
# ==============================
load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_ONLY_OPEN = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))

INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "Unstable")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))

# ID папки в Intercom, куда помещать новые статьи (можно оставить None)
INTERCOM_PARENT_ID = os.getenv("INTERCOM_PARENT_ID")  # пример: "123456"

SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"

SPACE_ID = "90125205902"
IGNORED_LIST_IDS = {"901212791461", "901212763746"}  # FORM и Changelog

# ---------- обязательные переменные ----------
required = [
    CLICKUP_TOKEN, CLICKUP_TEAM_ID, INTERCOM_TOKEN,
    INTERCOM_OWNER_ID, INTERCOM_AUTHOR_ID
]
assert all(required), "One or more required env vars are missing"

# ==============================
# 2. Логирование
# ==============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

# Для отладки поиска – включаем DEBUG при необходимости
if os.getenv("DEBUG_SEARCH", "false").lower() == "true":
    logging.getLogger().setLevel(logging.DEBUG)

# ==============================
# 3. HTTP-сессии
# ==============================
cu = requests.Session()
cu.headers.update({
    "Authorization": CLICKUP_TOKEN,
    "Content-Type": "application/json"
})
cu.timeout = 15

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})
ic.timeout = 15

# ==============================
# 4. Утилиты
# ==============================
def _load_state() -> dict:
    if os.path.exists(SYNC_STATE_FILE):
        with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_state(state: dict):
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def _rate_limit_sleep(resp: requests.Response) -> bool:
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", "10"))
        log.warning(f"Rate-limited – sleeping {wait}s")
        time.sleep(wait)
        return True
    return False

# ==============================
# 5. ClickUp API
# ==============================
def check_team_access(team_id: str):
    url = f"https://api.clickup.com/api/v2/team/{team_id}"
    r = cu.get(url)
    while _rate_limit_sleep(r):
        r = cu.get(url)
    r.raise_for_status()
    log.info(f"Team access OK: {r.json()['team']['name']}")

def fetch_folders(space_id: str):
    url = f"https://api.clickup.com/api/v2/space/{space_id}/folder"
    r = cu.get(url, params={"archived": "false"})
    while _rate_limit_sleep(r):
        r = cu.get(url, params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("folders", [])

def fetch_lists_from_folder(folder_id: str):
    url = f"https://api.clickup.com/api/v2/folder/{folder_id}/list"
    r = cu.get(url, params={"archived": "false"})
    while _rate_limit_sleep(r):
        r = cu.get(url, params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_folderless_lists(space_id: str):
    url = f"https://api.clickup.com/api/v2/space/{space_id}/list"
    r = cu.get(url, params={"archived": "false"})
    while _rate_limit_sleep(r):
        r = cu.get(url, params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_tasks_from_list(list_id: str, updated_after: datetime):
    """Генератор задач (включая подзадачи)."""
    page = 0
    updated_gt = int(updated_after.timestamp() * 1000) if not FETCH_ALL else None

    while True:
        params = {
            "page": page,
            "include_subtasks": "true",
            "archived": "false",
            "order_by": "updated",
            "reverse": "true",
            "subtasks": "true",
            "limit": 100,
            "include_markdown_description": "true",
        }
        if updated_gt:
            params["updated_gt"] = updated_gt
        if CLICKUP_ONLY_OPEN:
            params["statuses[]"] = ["to do", "in progress"]

        r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params=params)
        while _rate_limit_sleep(r):
            r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params=params)
        r.raise_for_status()

        batch = r.json().get("tasks", [])
        if not batch:
            break

        for t in batch:
            desc = t.get("markdown_description") or t.get("description") or ""
            t["description"] = desc
            yield t

        page += 1

def fetch_clickup_tasks(updated_after: datetime):
    """Все задачи из пространства, кроме игнорируемых списков."""
    # ---- папки ----
    for folder in fetch_folders(SPACE_ID):
        for lst in fetch_lists_from_folder(folder["id"]):
            if lst["id"] in IGNORED_LIST_IDS:
                log.info(f"Skipping ignored list: {lst['name']} (ID: {lst['id']})")
                continue
            yield from fetch_tasks_from_list(lst["id"], updated_after)

    # ---- списки без папки ----
    for lst in fetch_folderless_lists(SPACE_ID):
        if lst["id"] in IGNORED_LIST_IDS:
            log.info(f"Skipping ignored list: {lst['name']} (ID: {lst['id']})")
            continue
        yield from fetch_tasks_from_list(lst["id"], updated_after)

# ==============================
# 6. Преобразование в HTML
# ==============================
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    if len(body) > 50_000:
        body = body[:50_000] + "<p><em>Описание урезано из-за длины</em></p>"
    return f"<h1>{html.escape(name)}</h1>{body}"

# ==============================
# 7. Intercom API
# ==============================
def find_existing_article(title: str, task_id: str):
    """
    Ищет internal article с точным совпадением title [task_id].
    Возвращает объект статьи или None.
    """
    unique_title = f"{title} [{task_id}]".strip()
    log.debug(f"Searching Intercom for title: '{unique_title}'")

    url = f"{INTERCOM_BASE}/internal_articles"
    while url:
        r = ic.get(url)
        while _rate_limit_sleep(r):
            r = ic.get(url)
        r.raise_for_status()
        data = r.json()

        for art in data.get("data", []):
            art_title = art.get("title", "").strip()
            if art_title == unique_title:
                log.info(f"Found existing article: '{unique_title}' (ID: {art['id']})")
                return art
            else:
                log.debug(f"  ≠ '{art_title}'")

        url = data.get("pages", {}).get("next")

    log.debug(f"No article found for '{unique_title}'")
    return None


def create_internal_article(task: dict):
    """
    Создаёт новую internal article.
    Возвращает ID созданной статьи или None.
    """
    task_id = task["id"]
    title_base = task.get("name") or "(Без названия)"
    title = f"{title_base} [{task_id}]"[:255]

    html_body = task_to_html(task)
    if len(html_body) > 50_000:
        html_body = html_body[:50_000]

    payload = {
        "title": title,
        "body": html_body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "locale": "en",
    }
    if INTERCOM_PARENT_ID:
        payload["parent_id"] = int(INTERCOM_PARENT_ID)

    if DRY_RUN:
        log.info(f"[DRY_RUN] Would create: {title}")
        return None

    log.info(f"Creating new article: {title}")
    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    while _rate_limit_sleep(r):
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

    if r.status_code in (200, 201):
        art_id = r.json().get("id")
        log.info(f"Created article ID {art_id}")
        return art_id
    else:
        log.error(f"Create failed ({r.status_code}): {r.text}")
        return None


def upsert_internal_article(task: dict) -> tuple[int, int]:
    """
    Пытается найти статью → если нет – создаёт.
    Возвращает (created_count, skipped_count)
    """
    task_id = task["id"]
    title_base = task.get("name") or "(Без названия)"

    existing = find_existing_article(title_base, task_id)
    if existing:
        log.info(f"Skipping existing: {title_base} (Intercom ID: {existing['id']})")
        return 0, 1

    create_internal_article(task)
    return 1, 0


def cleanup_duplicates():
    """Удаляет дубли (оставляет статью с наименьшим ID)."""
    if DRY_RUN:
        log.info("[DRY_RUN] Would run duplicate cleanup")
        return

    log.info("Starting duplicate cleanup...")
    articles = []
    url = f"{INTERCOM_BASE}/internal_articles"
    while url:
        r = ic.get(url)
        r.raise_for_status()
        data = r.json()
        articles.extend(data.get("data", []))
        url = data.get("pages", {}).get("next")

    title_to_ids: dict[str, list[int]] = {}
    for art in articles:
        full = art.get("title", "").strip()
        # убираем [task_id] часть, если она есть
        base = full.split(" [", 1)[0] if "[" in full else full
        title_to_ids.setdefault(base, []).append(art["id"])

    deleted = 0
    for base, ids in title_to_ids.items():
        if len(ids) <= 1:
            continue
        keep = min(ids)          # оставляем самый «старый» ID
        for del_id in [i for i in ids if i != keep]:
            dr = ic.delete(f"{INTERCOM_BASE}/internal_articles/{del_id}")
            if dr.status_code in (200, 204):
                log.info(f"Deleted duplicate ID {del_id} ('{base}')")
                deleted += 1
            else:
                log.error(f"Failed delete {del_id}: {dr.status_code}")

    log.info(f"Cleanup finished – removed {deleted} duplicates")


# ==============================
# 8. Главный цикл
# ==============================
def main():
    if os.getenv("CLEANUP_DUPLICATES", "false").lower() == "true":
        cleanup_duplicates()
        return

    state = _load_state()
    last_sync_iso = state.get("last_sync_iso")

    if last_sync_iso and not FETCH_ALL:
        updated_after = datetime.fromisoformat(last_sync_iso)
    else:
        updated_after = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    log.info(f"Syncing tasks updated after {updated_after.isoformat()} (space {SPACE_ID})")

    try:
        check_team_access(CLICKUP_TEAM_ID)
    except Exception as e:
        log.error(f"Team access check failed: {e}")
        return

    created_total = skipped_total = 0

    for task in fetch_clickup_tasks(updated_after):
        try:
            created, skipped = upsert_internal_article(task)
            created_total += created
            skipped_total += skipped
        except Exception as e:
            log.exception(f"Error processing task {task.get('id')}: {e}")

    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_sync_iso"] = now_iso
    _save_state(state)

    log.info(
        f"Sync finished – created {created_total}, skipped {skipped_total}. "
        f"Last sync timestamp saved: {now_iso}"
    )


if __name__ == "__main__":
    main()
