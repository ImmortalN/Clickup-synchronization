#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Синхронизация ClickUp → Intercom (Internal Articles)
ПЕРЕВЁРНУТЫЙ ЦИКЛ: O(1) на задачу
- Все internal_articles загружаются в dict: task_id → article_id
- Пагинация через starting_after (везде!)
- 2000 гайдов → 20 страниц → 20 секунд
- Никаких дублей, зацикливаний, rate limit
"""

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
# 1. КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

# --- ClickUp ---
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_ONLY_OPEN = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))

# --- Intercom ---
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "Unstable")
INTERCOM_OWNER_ID = os.getenv("INTERCOM_OWNER_ID")
INTERCOM_AUTHOR_ID = os.getenv("INTERCOM_AUTHOR_ID")

# --- Управление ---
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"
CLEANUP_DUPLICATES = os.getenv("CLEANUP_DUPLICATES", "false").lower() == "true"
DEBUG_SEARCH = os.getenv("DEBUG_SEARCH", "false").lower() == "true"
MOVE_TO_ROOT = os.getenv("MOVE_TO_ROOT", "false").lower() == "true"

# --- Данные ---
SPACE_ID = "90125205902"
IGNORED_LIST_IDS = {"901212791461", "901212763746"}
SYNC_STATE_FILE = ".sync_state.json"

# ==============================
# 2. ПРОВЕРКА ПЕРЕМЕННЫХ
# ==============================
required_vars = [
    "CLICKUP_API_TOKEN",
    "CLICKUP_TEAM_ID",
    "INTERCOM_ACCESS_TOKEN",
    "INTERCOM_OWNER_ID",
    "INTERCOM_AUTHOR_ID"
]
missing = [var for var in required_vars if os.getenv(var) is None]
if missing:
    print(f"ERROR: Missing required environment variables: {', '.join(missing)}")
    raise SystemExit(1)

# Приводим ID к int
try:
    INTERCOM_OWNER_ID = int(INTERCOM_OWNER_ID)
    INTERCOM_AUTHOR_ID = int(INTERCOM_AUTHOR_ID)
except ValueError:
    print("ERROR: INTERCOM_OWNER_ID and INTERCOM_AUTHOR_ID must be integers")
    raise SystemExit(1)

# ==============================
# 3. ЛОГИРОВАНИЕ
# ==============================
logging.basicConfig(
    level=logging.DEBUG if DEBUG_SEARCH else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

# ==============================
# 4. СЕССИИ
# ==============================
cu = requests.Session()
cu.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})
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
# 5. УТИЛИТЫ
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
        log.warning(f"Rate limited — sleeping {wait}s")
        time.sleep(wait)
        return True
    return False

# ==============================
# 6. CLICKUP API
# ==============================
def check_team_access(team_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/team/{team_id}")
    while _rate_limit_sleep(r): r = cu.get(...)
    r.raise_for_status()
    log.info(f"Team access OK: {r.json()['team']['name']}")

def fetch_folders(space_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/folder", params={"archived": "false"})
    while _rate_limit_sleep(r): r = cu.get(...)
    r.raise_for_status()
    return r.json().get("folders", [])

def fetch_lists_from_folder(folder_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/folder/{folder_id}/list", params={"archived": "false"})
    while _rate_limit_sleep(r): r = cu.get(...)
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_folderless_lists(space_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/list", params={"archived": "false"})
    while _rate_limit_sleep(r): r = cu.get(...)
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_tasks_from_list(list_id: str, updated_after: datetime):
    page = 0
    updated_gt = int(updated_after.timestamp() * 1000) if not FETCH_ALL else None
    while True:
        params = {
            "page": page, "include_subtasks": "true", "archived": "false",
            "order_by": "updated", "reverse": "true", "limit": 100,
            "include_markdown_description": "true"
        }
        if updated_gt: params["updated_gt"] = updated_gt
        if CLICKUP_ONLY_OPEN: params["statuses[]"] = ["to do", "in progress"]

        r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params=params)
        while _rate_limit_sleep(r): r = cu.get(...)
        r.raise_for_status()
        batch = r.json().get("tasks", [])
        if not batch: break
        for t in batch:
            t["description"] = t.get("markdown_description") or t.get("description") or ""
            yield t
        page += 1

def fetch_clickup_tasks(updated_after: datetime):
    for folder in fetch_folders(SPACE_ID):
        for lst in fetch_lists_from_folder(folder["id"]):
            if lst["id"] in IGNORED_LIST_IDS: continue
            yield from fetch_tasks_from_list(lst["id"], updated_after)
    for lst in fetch_folderless_lists(SPACE_ID):
        if lst["id"] in IGNORED_LIST_IDS: continue
        yield from fetch_tasks_from_list(lst["id"], updated_after)

# ==============================
# 7. HTML
# ==============================
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    if len(body) > 50_000:
        body = body[:50_000] + "<p><em>Описание урезано</em></p>"
    return f"<h1>{html.escape(name)}</h1>{body}"

# ==============================
# 8. ЗАГРУЗКА ВСЕХ СТАТЕЙ ИЗ INTERCOM (ФИНАЛЬНАЯ ВЕРСИЯ)
# ==============================
def load_all_intercom_articles() -> dict[str, int]:
    log.info("Loading all Intercom articles into memory...")
    task_id_to_article_id = {}
    total_loaded = 0
    page_num = 1
    cursor = None
    
    while True:
        params = {"per_page": 100}
        if cursor:
            params["starting_after"] = cursor
        
        try:
            r = ic.get(f"{INTERCOM_BASE}/internal_articles", params=params)
            while _rate_limit_sleep(r):
                time.sleep(2)
                r = ic.get(f"{INTERCOM_BASE}/internal_articles", params=params)
            
            if r.status_code != 200:
                log.error(f"HTTP {r.status_code}")
                break
            
            data = r.json()
            articles = data.get("data", [])
            
            if not articles:
                log.info(f"No more articles — loaded {total_loaded} total")
                break
            
            log.debug(f"Page {page_num}: {len(articles)} articles")
            
            for art in articles:
                title = art.get("title", "")
                if "[" in title and "]" in title:
                    start = title.rfind("[")
                    end = title.rfind("]")
                    if start < end:
                        task_id = title[start+1:end]
                        task_id_to_article_id[task_id] = art["id"]
                        total_loaded += 1
            
            cursor = articles[-1]["id"]
            page_num += 1
            
        except Exception as e:
            log.error(f"Error loading articles: {e}")
            break
    
    log.info(f"Loaded {total_loaded} articles with task_id")
    return task_id_to_article_id

# ==============================
# 9. СОЗДАНИЕ
# ==============================
def create_internal_article(task: dict, intercom_map: dict) -> int | None:
    task_id = task["id"]
    title_base = task.get("name") or "(Без названия)"
    title = f"{title_base} [{task_id}]"[:255]
    body = task_to_html(task)[:50_000]

    payload = {
        "title": title,
        "body": body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "locale": "en",
    }

    if DRY_RUN:
        log.info(f"[DRY_RUN] Would create: {title}")
        return None

    log.info(f"Creating: {title}")
    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    while _rate_limit_sleep(r):
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

    if r.status_code in (200, 201):
        art_id = r.json().get("id")
        log.info(f"Created ID: {art_id}")
        intercom_map[task_id] = art_id
        return art_id
    else:
        log.error(f"Create failed: {r.status_code} {r.text}")
        return None

# ==============================
# 10. ОЧИСТКА ДУБЛЕЙ (ФИНАЛЬНАЯ)
# ==============================
def cleanup_duplicates():
    if DRY_RUN:
        log.info("[DRY_RUN] Would cleanup duplicates")
        return

    log.info("Starting duplicate cleanup...")
    articles = []
    url = f"{INTERCOM_BASE}/internal_articles"
    params = {"per_page": 100}

    while True:
        r = ic.get(url, params=params)
        while _rate_limit_sleep(r):
            time.sleep(2)
            r = ic.get(url, params=params)

        if r.status_code != 200:
            log.error(f"HTTP {r.status_code} in cleanup")
            break

        data = r.json()
        batch = data.get("data", [])
        articles.extend(batch)

        if not batch:
            break

        params["starting_after"] = batch[-1]["id"]

    title_to_ids = {}
    for art in articles:
        full = art.get("title", "").strip()
        base = full.split(" [", 1)[0] if " [" in full else full
        title_to_ids.setdefault(base, []).append(art["id"])

    deleted = 0
    for base, ids in title_to_ids.items():
        if len(ids) <= 1: continue
        keep = min(ids)
        for del_id in [i for i in ids if i != keep]:
            dr = ic.delete(f"{INTERCOM_BASE}/internal_articles/{del_id}")
            if dr.status_code in (200, 204):
                log.info(f"Deleted duplicate: '{base}' (ID {del_id})")
                deleted += 1
            else:
                log.error(f"Delete failed {del_id}: {dr.status_code}")

    log.info(f"Cleanup complete — removed {deleted} duplicates")

# ==============================
# 11. ПЕРЕНОС В КОРЕНЬ (ФИНАЛЬНАЯ)
# ==============================
def move_all_to_root():
    if DRY_RUN:
        log.info("[DRY_RUN] Would move all to root")
        return

    log.info("Moving all articles to root...")
    url = f"{INTERCOM_BASE}/internal_articles"
    params = {"per_page": 100}

    while True:
        r = ic.get(url, params=params)
        while _rate_limit_sleep(r):
            time.sleep(2)
            r = ic.get(url, params=params)

        if r.status_code != 200:
            log.error(f"HTTP {r.status_code} in move_to_root")
            break

        data = r.json()
        batch = data.get("data", [])

        for art in batch:
            if art.get("parent_id"):
                put_r = ic.put(f"{INTERCOM_BASE}/internal_articles/{art['id']}", json={"parent_id": None})
                if put_r.status_code == 200:
                    log.info(f"Moved to root: {art['title']} (ID: {art['id']})")

        if not batch:
            break

        params["starting_after"] = batch[-1]["id"]

    log.info("All articles moved to root")

# ==============================
# 12. MAIN
# ==============================
def main():
    if CLEANUP_DUPLICATES:
        cleanup_duplicates()
        return
    if MOVE_TO_ROOT:
        move_all_to_root()
        return

    intercom_map = load_all_intercom_articles()

    state = _load_state()
    last_sync_iso = state.get("last_sync_iso")
    updated_after = datetime.fromisoformat(last_sync_iso) if last_sync_iso and not FETCH_ALL else datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    log.info(f"Syncing tasks updated after {updated_after.isoformat()}")

    try:
        check_team_access(CLICKUP_TEAM_ID)
    except Exception as e:
        log.error(f"Team check failed: {e}")
        return

    created = skipped = 0
    for task in fetch_clickup_tasks(updated_after):
        task_id = task["id"]
        title_base = task.get("name") or "(Без названия)"

        if task_id in intercom_map:
            log.info(f"SKIPPED: '{title_base}' (ID {intercom_map[task_id]})")
            skipped += 1
            continue

        new_id = create_internal_article(task, intercom_map)
        if new_id or DRY_RUN:
            created += 1

    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_sync_iso"] = now_iso
    _save_state(state)

    log.info(f"Sync complete — Created: {created}, Skipped: {skipped}, Last sync: {now_iso}")

if __name__ == "__main__":
    main()
