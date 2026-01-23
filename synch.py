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

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_ONLY_OPEN = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))

INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "Unstable")
INTERCOM_OWNER_ID = os.getenv("INTERCOM_OWNER_ID")
INTERCOM_AUTHOR_ID = os.getenv("INTERCOM_AUTHOR_ID")

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"
DEBUG_SEARCH = os.getenv("DEBUG_SEARCH", "false").lower() == "true"

SPACE_ID = "90125205902"
IGNORED_LIST_IDS = {"901212791461", "901212763746"}
SYNC_STATE_FILE = ".sync_state.json"

# Для теста: Ограничить количество задач из ClickUp (например, 200)
MAX_TASKS_FOR_TEST = 200

# ==============================
# 2. ПРОВЕРКА
# ==============================
required_vars = ["CLICKUP_API_TOKEN", "CLICKUP_TEAM_ID", "INTERCOM_ACCESS_TOKEN", "INTERCOM_OWNER_ID", "INTERCOM_AUTHOR_ID"]
missing = [v for v in required_vars if os.getenv(v) is None]
if missing:
    print(f"ERROR: Missing: {', '.join(missing)}")
    raise SystemExit(1)

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
# 6. CLICKUP
# ==============================
def check_team_access(team_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/team/{team_id}")
    while _rate_limit_sleep(r): r = cu.get(f"https://api.clickup.com/api/v2/team/{team_id}")
    r.raise_for_status()
    log.info(f"Team access OK: {r.json()['team']['name']}")

def fetch_folders(space_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/folder", params={"archived": "false"})
    while _rate_limit_sleep(r): r = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/folder", params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("folders", [])

def fetch_lists_from_folder(folder_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/folder/{folder_id}/list", params={"archived": "false"})
    while _rate_limit_sleep(r): r = cu.get(f"https://api.clickup.com/api/v2/folder/{folder_id}/list", params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_folderless_lists(space_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/list", params={"archived": "false"})
    while _rate_limit_sleep(r): r = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/list", params={"archived": "false"})
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
        while _rate_limit_sleep(r): r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params=params)
        r.raise_for_status()
        batch = r.json().get("tasks", [])
        if not batch: break
        for t in batch:
            t["description"] = t.get("markdown_description") or t.get("description") or ""
            yield t
        page += 1

def fetch_clickup_tasks(updated_after: datetime):
    task_count = 0
    for folder in fetch_folders(SPACE_ID):
        for lst in fetch_lists_from_folder(folder["id"]):
            if lst["id"] in IGNORED_LIST_IDS: continue
            for task in fetch_tasks_from_list(lst["id"], updated_after):
                if task_count >= MAX_TASKS_FOR_TEST:
                    log.info(f"Reached test limit of {MAX_TASKS_FOR_TEST} tasks — stopping fetch.")
                    return
                yield task
                task_count += 1
    for lst in fetch_folderless_lists(SPACE_ID):
        if lst["id"] in IGNORED_LIST_IDS: continue
        for task in fetch_tasks_from_list(lst["id"], updated_after):
            if task_count >= MAX_TASKS_FOR_TEST:
                log.info(f"Reached test limit of {MAX_TASKS_FOR_TEST} tasks — stopping fetch.")
                return
            yield task
            task_count += 1

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
# 8. ЗАГРУЗКА ВСЕХ СТАТЕЙ ЧЕРЕЗ pages.next
# ==============================
def load_all_articles_with_pages() -> dict[str, int]:
    log.info("Loading ALL Intercom articles using cursor-based pagination...")
    task_id_to_article_id = {}
    base_url = f"{INTERCOM_BASE}/internal_articles"
    params = {"per_page": 100}
    page_num = 1
    total_loaded = 0

    while True:
        try:
            log.debug(f"Requesting page {page_num} with params: {params}")
            r = ic.get(base_url, params=params)
            while _rate_limit_sleep(r):
                time.sleep(2)
                r = ic.get(base_url, params=params)

            if r.status_code != 200:
                log.error(f"HTTP {r.status_code} at page {page_num}: {r.text}")
                break

            data = r.json()
            log.debug(f"Response structure (page {page_num}): {json.dumps(data, indent=2)}")

            # Articles обычно в data["data"]
            articles = data.get("data", [])
            log.info(f"Page {page_num}: loaded {len(articles)} articles, total so far: {total_loaded + len(articles)}")

            for art in articles:
                title = art.get("title", "")
                if "[" in title and "]" in title:
                    start = title.rfind("[")
                    end = title.rfind("]")
                    if start < end:
                        task_id = title[start+1:end].strip()
                        if task_id:
                            if task_id in task_id_to_article_id:
                                log.warning(f"Duplicate task_id '{task_id}' → possible conflict")
                            task_id_to_article_id[task_id] = art["id"]
                            log.debug(f"Mapped '{task_id}' → article {art['id']}")

            total_loaded += len(articles)

            if not articles:
                log.info("No more articles — done.")
                break

            # Pagination: ищем cursor
            pages = data.get("pages", {})
            next_cursor = None
            if pages:
                next_obj = pages.get("next", {})
                if next_obj:
                    next_cursor = next_obj.get("cursor")
                    log.debug(f"Found next cursor: {next_cursor}")

            if not next_cursor:
                log.info("No next cursor found — pagination complete.")
                break

            # Для следующей страницы
            params = {"per_page": 100, "cursor": next_cursor}
            page_num += 1
            time.sleep(1.5)  # пауза от rate limit

        except Exception as e:
            log.error(f"Error on page {page_num}: {e}")
            break

    log.info(f"Successfully loaded {len(task_id_to_article_id)} articles with task_id (from {total_loaded} total fetched)")
    if len(task_id_to_article_id) == 0:
        log.warning("No articles with [task_id] format found in Intercom — check titles or pagination")
    return task_id_to_article_id

# ==============================
# 9. СОЗДАНИЕ ИЛИ ОБНОВЛЕНИЕ СТАТЬИ
# ==============================
def sync_internal_article(task: dict, intercom_map: dict) -> int | None:
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

    if task_id in intercom_map:
        art_id = intercom_map[task_id]
        if DRY_RUN:
            log.info(f"[DRY_RUN] Would update: {title} (ID {art_id})")
            return art_id

        log.info(f"Updating: {title} (ID {art_id})")
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{art_id}", json=payload)
        while _rate_limit_sleep(r):
            r = ic.put(f"{INTERCOM_BASE}/internal_articles/{art_id}", json=payload)

        if r.status_code in (200, 201):
            log.info(f"Updated: {title} (ID {art_id})")
            return art_id
        else:
            log.error(f"Update failed: {r.status_code} {r.text}")
            return None
    else:
        if DRY_RUN:
            log.info(f"[DRY_RUN] Would create: {title}")
            return None

        log.info(f"Creating: {title}")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
        while _rate_limit_sleep(r):
            r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

        if r.status_code in (200, 201):
            art_id = r.json().get("id")
            log.info(f"Created: {title} (ID {art_id})")
            intercom_map[task_id] = art_id
            return art_id
        else:
            log.error(f"Create failed: {r.status_code} {r.text}")
            return None

# ==============================
# 10. MAIN
# ==============================
def main():
    # 1. Загружаем ВСЕ статьи через pages.next
    intercom_map = load_all_articles_with_pages()

    # 2. Синхронизация
    state = _load_state()
    last_sync_iso = state.get("last_sync_iso")
    updated_after = datetime.fromisoformat(last_sync_iso) if last_sync_iso and not FETCH_ALL else datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    log.info(f"Syncing tasks updated after {updated_after.isoformat()}")

    try:
        check_team_access(CLICKUP_TEAM_ID)
    except Exception as e:
        log.error(f"Team check failed: {e}")
        return

    created = updated = skipped = 0
    for task in fetch_clickup_tasks(updated_after):
        result_id = sync_internal_article(task, intercom_map)
        if result_id:
            # Корректировка счёта: если был в map изначально — update, иначе create
            if task["id"] in intercom_map and result_id == intercom_map[task["id"]]:
                updated += 1
            else:
                created += 1
        else:
            skipped += 1

    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_sync_iso"] = now_iso
    _save_state(state)

    log.info(f"Sync complete — Created: {created}, Updated: {updated}, Skipped: {skipped}, Last sync: {now_iso}")

if __name__ == "__main__":
    main()
