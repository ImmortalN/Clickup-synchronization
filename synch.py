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

# ==== Конфигурация из окружения ====
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
SPACE_ID = os.getenv("SPACE_ID", "90153590151")
CLICKUP_ONLY_OPEN = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))

INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "2.11")
INTERCOM_SOURCE_ID = int(os.getenv("INTERCOM_SOURCE_ID"))

SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"

IGNORED_LIST_IDS = ["901509433569", "901509402998"]

# ==== Логирование ====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ==== Сессии ====
cu = requests.Session()
cu.headers.update({
    "Authorization": f"Bearer {CLICKUP_TOKEN}",
    "Content-Type": "application/json"
})
cu.timeout = 10

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})
ic.timeout = 10

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

# ==== ClickUp: Получение папок и списков ====
def fetch_folders(space_id: str):
    base = f"https://api.clickup.com/api/v2/space/{space_id}/folder"
    r = cu.get(base, params={"archived": "false"})
    while _rate_limit_sleep(r):
        r = cu.get(base, params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("folders", [])

def fetch_lists_from_folder(folder_id: str):
    base = f"https://api.clickup.com/api/v2/folder/{folder_id}/list"
    r = cu.get(base, params={"archived": "false"})
    while _rate_limit_sleep(r):
        r = cu.get(base, params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_folderless_lists(space_id: str):
    base = f"https://api.clickup.com/api/v2/space/{space_id}/list"
    r = cu.get(base, params={"archived": "false"})
    while _rate_limit_sleep(r):
        r = cu.get(base, params={"archived": "false"})
    r.raise_for_status()
    return r.json().get("lists", [])

# ==== ClickUp: Получение задач из списка ====
def fetch_tasks_from_list(list_id: str, updated_after: datetime):
    base = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    page = 0
    total = 0
    updated_gt = int(updated_after.timestamp() * 1000) if not FETCH_ALL else None

    while True:
        params = {
            "page": page,
            "limit": 100,
            "include_subtasks": "true",
            "subtasks": "true",
            "archived": "false",
            "order_by": "updated",
            "reverse": "true"
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

# ==== Получение всех задач из пространства ====
def fetch_clickup_tasks(updated_after: datetime, space_id: str):
    total_tasks = 0
    # Папки
    for folder in fetch_folders(space_id):
        folder_id = folder.get("id")
        folder_name = folder.get("name")
        logging.info(f"Processing folder: {folder_name} (ID: {folder_id})")
        for lst in fetch_lists_from_folder(folder_id):
            list_id = lst.get("id")
            list_name = lst.get("name")
            if list_id in IGNORED_LIST_IDS:
                logging.info(f"Skipping ignored list: {list_name} (ID: {list_id})")
                continue
            logging.info(f"Processing list: {list_name} (ID: {list_id})")
            for task in fetch_tasks_from_list(list_id, updated_after):
                total_tasks += 1
                yield task

    # Списки без папки
    for lst in fetch_folderless_lists(space_id):
        list_id = lst.get("id")
        list_name = lst.get("name")
        if list_id in IGNORED_LIST_IDS:
            continue
        logging.info(f"Processing folderless list: {list_name} (ID: {list_id})")
        for task in fetch_tasks_from_list(list_id, updated_after):
            total_tasks += 1
            yield task

    logging.info(f"Total fetched tasks: {total_tasks}")

# ==== Преобразование задачи в HTML для Intercom ====
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body_html = markdown(desc) if desc else "<p><em>Нет описания</em></p>"

    status = (task.get("status") or {}).get("status") or "—"
    assignees = ", ".join(a.get("username") or a.get("email") or str(a.get("id")) for a in task.get("assignees", [])) or "—"
    priority = (task.get("priority") or {}).get("priority") or (task.get("priority") or {}).get("label") or "—"
    due = task.get("due_date")
    due_str = datetime.fromtimestamp(int(due)/1000, tz=timezone.utc).strftime("%Y-%m-%d") if due else "—"
    task_url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"

    meta = f"""
    <div style='border:1px solid #eee;padding:12px;border-radius:8px;margin-bottom:12px'>
        <div><strong>Статус:</strong> {html.escape(status)}</div>
        <div><strong>Исполнители:</strong> {html.escape(assignees)}</div>
        <div><strong>Приоритет:</strong> {html.escape(str(priority))}</div>
        <div><strong>Дедлайн:</strong> {html.escape(due_str)}</div>
        <div><strong>ClickUp:</strong> <a href="{html.escape(task_url)}" target="_blank" rel="noopener">открыть задачу</a></div>
    </div>
    """

    title_html = f"<h1>{html.escape(name)}</h1>"
    return title_html + meta + body_html

# ==== Intercom: создание / обновление External Page ====
def upsert_external_page(task: dict):
    task_id = task.get("id")
    title = task.get("name") or "(Без названия)"
    try:
        html_body = task_to_html(task)
        if len(html_body) > 50000:
            logging.warning(f"Task {task_id}: HTML too large, truncating to 50,000 chars")
            html_body = html_body[:50000]

        payload = {
            "title": title[:255],
            "html": html_body,
            "external_id": task_id,
            "source_id": INTERCOM_SOURCE_ID,
            "ai_agent_availability": True,
            "ai_copilot_availability": True,
            "locale": "en",
        }
        url = task.get("url")
        if url:
            payload["url"] = url

        if DRY_RUN:
            logging.info(f"[DRY_RUN] Would upsert External Page for task {task_id}: {title}")
            return

        r = ic.post(f"{INTERCOM_BASE}/ai/external_pages", json=payload)
        while _rate_limit_sleep(r):
            r = ic.post(f"{INTERCOM_BASE}/ai/external_pages", json=payload)
        r.raise_for_status()
        logging.info(f"Upserted: {title} (task {task_id})")

    except Exception as e:
        logging.error(f"Failed to upsert task {task_id}: {e}")

# ==== Главный процесс ====
def main():
    state = _load_state()
    last_sync_iso = state.get("last_sync_iso")
    if last_sync_iso and not FETCH_ALL:
        updated_after = datetime.fromisoformat(last_sync_iso)
    else:
        updated_after = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    logging.info(f"Syncing ClickUp tasks{' (all tasks)' if FETCH_ALL else f' updated after {updated_after}'} from space {SPACE_ID}")

    count = 0
    try:
        for task in fetch_clickup_tasks(updated_after, SPACE_ID):
            try:
                upsert_external_page(task)
                count += 1
                time.sleep(0.1)  # Rate limit
            except Exception as e:
                logging.exception(f"Failed to process task {task.get('id')}: {e}")
                continue
    except Exception as e:
        logging.exception(f"Error in fetch_clickup_tasks: {e}")

    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_sync_iso"] = now_iso
    _save_state(state)
    logging.info(f"Done. Synced items: {count}. New last_sync_iso = {now_iso}")

if __name__ == "__main__":
    main()
