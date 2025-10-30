#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ClickUp → Intercom Internal Articles Sync
Исправленная пагинация + защита от дублей
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
# 1. CONFIG
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

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"
CLEANUP_DUPLICATES = os.getenv("CLEANUP_DUPLICATES", "false").lower() == "true"
MOVE_TO_ROOT = os.getenv("MOVE_TO_ROOT", "false").lower() == "true"
DEBUG_SEARCH = os.getenv("DEBUG_SEARCH", "false").lower() == "true"

SPACE_ID = "90125205902"
IGNORED_LIST_IDS = {"901212791461", "901212763746"}
SYNC_STATE_FILE = ".sync_state.json"

# ==============================
# 2. LOGGING
# ==============================
logging.basicConfig(
    level=logging.DEBUG if DEBUG_SEARCH else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

# ==============================
# 3. SESSIONS
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
# 4. UTILS
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
# 5. CLICKUP API
# ==============================
def check_team_access(team_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/team/{team_id}")
    while _rate_limit_sleep(r):
        r = cu.get(f"https://api.clickup.com/api/v2/team/{team_id}")
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
            "order_by": "created", "reverse": "true", "limit": 100,
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
            if lst["id"] in IGNORED_LIST_IDS:
                log.info(f"Skipping list: {lst['name']} (ID: {lst['id']})")
                continue
            yield from fetch_tasks_from_list(lst["id"], updated_after)
    for lst in fetch_folderless_lists(SPACE_ID):
        if lst["id"] in IGNORED_LIST_IDS:
            log.info(f"Skipping list: {lst['name']} (ID: {lst['id']})")
            continue
        yield from fetch_tasks_from_list(lst["id"], updated_after)

# ==============================
# 6. HTML
# ==============================
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    if len(body) > 50_000:
        body = body[:50_000] + "<p><em>Описание урезано</em></p>"
    return f"<h1>{html.escape(name)}</h1>{body}"

# ==============================
# 7. INTERCOM ARTICLES
# ==============================
def load_all_intercom_articles() -> dict[str, int]:
    """
    Load all Intercom internal articles using proper cursor-based pagination.
    Prevents duplicates and infinite loops.
    Returns: dict mapping ClickUp task_id -> Intercom article_id
    """
    log.info("Loading all Intercom articles into memory...")
    task_id_to_article_id = {}
    seen_ids = set()
    total_loaded = 0
    page_num = 1
    cursor = None  # Track the actual last article ID

    while True:
        params = {"per_page": 100}
        if cursor:
            params["starting_after"] = cursor

        try:
            r = ic.get(f"{INTERCOM_BASE}/internal_articles", params=params)
            while _rate_limit_sleep(r):
                r = ic.get(f"{INTERCOM_BASE}/internal_articles", params=params)
            r.raise_for_status()

            articles = r.json().get("data", [])
            if not articles:
                log.info(f"No more articles — loaded {total_loaded} total")
                break

            # Detect potential loop
            first_id = articles[0]["id"]
            if first_id in seen_ids:
                log.warning(f"Detected loop at ID {first_id} — stopping pagination")
                break

            log.debug(f"Page {page_num}: {len(articles)} articles")

            for art in articles:
                art_id = art.get("id")
                if not art_id or art_id in seen_ids:
                    continue
                seen_ids.add(art_id)

                title = art.get("title", "")
                if "[" in title and "]" in title:
                    start = title.rfind("[")
                    end = title.rfind("]")
                    if start < end:
                        task_id = title[start+1:end]
                        task_id_to_article_id[task_id] = art_id
                        total_loaded += 1

            # Set cursor to last article in current batch
            cursor = articles[-1]["id"]
            page_num += 1

        except Exception as e:
            log.error(f"Error loading articles: {e}")
            break

    log.info(f"Loaded {total_loaded} articles with task_id")
    return task_id_to_article_id



# ==============================
# 8. MAIN
# ==============================
def main():
    if CLEANUP_DUPLICATES:
        log.info("Cleaning up duplicates...")
        return
    if MOVE_TO_ROOT:
        log.info("Moving articles to root...")
        return

    # Load existing Intercom articles
    intercom_map = load_all_intercom_articles()

    # Load last sync time
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

    # Save sync state
    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_sync_iso"] = now_iso
    _save_state(state)
    log.info(f"Sync complete — Created: {created}, Skipped: {skipped}, Last sync: {now_iso}")


if __name__ == "__main__":
    main()
