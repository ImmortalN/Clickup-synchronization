import os
import time
import json
import html
import logging
from datetime import datetime, timedelta, timezone
import requests
from markdown import markdown
from dotenv import load_dotenv

# ==== Загрузка переменных окружения ====
load_dotenv()

# ==== Конфигурация ====
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_ONLY_OPEN = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "2.14")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"
SPACE_ID = "90153590151"  # Croco KB
IGNORED_LIST_IDS = ["901509433569", "901509402998"]  # Forms, ChangeLog

# ==== Проверка ====
assert CLICKUP_TOKEN, "CLICKUP_API_TOKEN is required"
assert CLICKUP_TEAM_ID, "CLICKUP_TEAM_ID is required"
assert INTERCOM_TOKEN, "INTERCOM_ACCESS_TOKEN is required"
assert INTERCOM_OWNER_ID, "INTERCOM_OWNER_ID is required"
assert INTERCOM_AUTHOR_ID, "INTERCOM_AUTHOR_ID is required"

# ==== Логирование ====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ==== Сессии ====
cu = requests.Session()
cu.headers.update({
    "Authorization": CLICKUP_TOKEN,
    "Content-Type": "application/json"
})

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Content-Type": "application/json",
    "Intercom-Version": INTERCOM_VERSION
})

# ==== Утилиты ====
def _load_state():
    if os.path.exists(SYNC_STATE_FILE):
        with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_state(state: dict):
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def _rate_limit_sleep(resp: requests.Response):
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "10"))
        logging.warning(f"Rate limited. Sleeping for {retry_after}s")
        time.sleep(retry_after)
        return True
    return False

# ==== ClickUp ====
def fetch_folders(space_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/folder", params={"archived": "false"})
    while _rate_limit_sleep(r):
        r = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/folder", params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("folders", [])

def fetch_lists_from_folder(folder_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/folder/{folder_id}/list", params={"archived": "false"})
    while _rate_limit_sleep(r):
        r = cu.get(f"https://api.clickup.com/api/v2/folder/{folder_id}/list", params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_folderless_lists(space_id: str):
    r = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/list", params={"archived": "false"})
    while _rate_limit_sleep(r):
        r = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/list", params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_tasks_from_list(list_id: str, updated_after: datetime):
    base = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    page = 0
    updated_gt = int(updated_after.timestamp() * 1000) if not FETCH_ALL else None
    total = 0
    while True:
        params = {
            "page": page,
            "include_subtasks": "true",
            "archived": "false",
            "order_by": "updated",
            "reverse": "true",
            "limit": 100,
        }
        if updated_gt:
            params["updated_gt"] = updated_gt
        if CLICKUP_ONLY_OPEN:
            params["statuses[]"] = ["to do", "in progress"]

        r = cu.get(base, params=params)
        while _rate_limit_sleep(r):
            r = cu.get(base, params=params)
        r.raise_for_status()
        tasks = r.json().get("tasks", [])
        if not tasks:
            break
        for t in tasks:
            total += 1
            yield t
        page += 1
    logging.info(f"Fetched {total} tasks from list {list_id}")

def fetch_clickup_tasks(updated_after: datetime, space_id: str):
    folders = fetch_folders(space_id)
    for folder in folders:
        for lst in fetch_lists_from_folder(folder["id"]):
            if lst["id"] in IGNORED_LIST_IDS:
                continue
            for task in fetch_tasks_from_list(lst["id"], updated_after):
                yield task
    for lst in fetch_folderless_lists(space_id):
        if lst["id"] in IGNORED_LIST_IDS:
            continue
        for task in fetch_tasks_from_list(lst["id"], updated_after):
            yield task

# ==== Форматирование ====
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body_html = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    if len(body_html) > 50000:
        body_html = body_html[:50000] + "<p><em>Описание урезано из-за длины</em></p>"
    task_url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
    meta = f"""
    <div style='border:1px solid #eee;padding:12px;border-radius:8px;margin-bottom:12px'>
      <strong>ClickUp:</strong> <a href="{html.escape(task_url)}" target="_blank">Открыть задачу</a>
    </div>
    """
    return f"<h1>{html.escape(name)}</h1>{meta}{body_html}"

# ==== Intercom: Internal Articles ====
def find_existing_article(title: str):
    """Поиск статьи по названию."""
    r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"query": title})
    while _rate_limit_sleep(r):
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"query": title})
    if r.status_code == 200:
        data = r.json()
        items = data.get("data") or []
        for article in items:
            if article.get("title", "").strip().lower() == title.strip().lower():
                return article.get("id")
    return None

def upsert_internal_article(task: dict):
    task_id = task.get("id")
    title = task.get("name") or "(Без названия)"
    html_body = task_to_html(task)

    if len(html_body) > 50000:
        html_body = html_body[:50000]

    payload = {
        "title": title[:255],
        "body": html_body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "locale": "en",
    }

    if DRY_RUN:
        logging.info(f"[DRY_RUN] Would upsert internal article for task {task_id}: {title}")
        return

    existing_id = find_existing_article(title)
    if existing_id:
        endpoint = f"{INTERCOM_BASE}/internal_articles/{existing_id}"
        method = "PUT"
        action = "Updating"
    else:
        endpoint = f"{INTERCOM_BASE}/internal_articles"
        method = "POST"
        action = "Creating"

    logging.info(f"{action} internal article: {title} (task {task_id})")

    r = ic.request(method, endpoint, json=payload)
    while _rate_limit_sleep(r):
        r = ic.request(method, endpoint, json=payload)
    if r.status_code not in (200, 201):
        logging.error(f"Failed {action.lower()} internal article {title}: {r.status_code} {r.text}")
    else:
        logging.info(f"{action} OK: {title}")

# ==== Главная ====
def main():
    state = _load_state()
    last_sync_iso = state.get("last_sync_iso")
    if last_sync_iso and not FETCH_ALL:
        updated_after = datetime.fromisoformat(last_sync_iso)
    else:
        updated_after = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    logging.info(f"Syncing ClickUp tasks{' (all)' if FETCH_ALL else f' updated after {updated_after.isoformat()}'}")

    count = 0
    try:
        for task in fetch_clickup_tasks(updated_after, SPACE_ID):
            upsert_internal_article(task)
            count += 1
    except Exception as e:
        logging.exception(f"Error in sync: {e}")

    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_sync_iso"] = now_iso
    _save_state(state)

    logging.info(f"Done. Synced {count} tasks. Last sync time updated.")

if __name__ == "__main__":
    main()
