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
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_ONLY_OPEN = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "2.14")  # Исправлено на 2.14 для internal articles
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))  # ID владельца статей
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))  # ID автора статей
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"
SPACE_ID = "90153590151"  # Hardcoded for "Croco KB"

IGNORED_LIST_IDS = ["901509433569", "901509402998"]  # Forms и ChangeLog

# ==== Проверка обязательных переменных ====
assert CLICKUP_TOKEN, "CLICKUP_API_TOKEN is required"
assert CLICKUP_TEAM_ID, "CLICKUP_TEAM_ID is required"
assert INTERCOM_TOKEN, "INTERCOM_ACCESS_TOKEN is required"
assert INTERCOM_OWNER_ID, "INTERCOM_OWNER_ID is required"
assert INTERCOM_AUTHOR_ID, "INTERCOM_AUTHOR_ID is required"
assert SPACE_ID, "SPACE_ID must be set"

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

# ==== ClickUp: Получение папок из пространства ====
def fetch_folders(space_id: str):
    base = f"https://api.clickup.com/api/v2/space/{space_id}/folder"
    params = {"archived": "false"}
    logging.info(f"Fetching folders from {base}")
    r = cu.get(base, params=params)
    logging.info(f"Folders response: status {r.status_code}, body: {r.text[:200]}...")
    while _rate_limit_sleep(r):
        r = cu.get(base, params=params)
    r.raise_for_status()
    return r.json().get("folders", [])

# ==== ClickUp: Получение списков из папки ====
def fetch_lists_from_folder(folder_id: str):
    base = f"https://api.clickup.com/api/v2/folder/{folder_id}/list"
    params = {"archived": "false"}
    logging.info(f"Fetching lists from folder {folder_id}")
    r = cu.get(base, params=params)
    logging.info(f"Lists response: status {r.status_code}, body: {r.text[:200]}...")
    while _rate_limit_sleep(r):
        r = cu.get(base, params=params)
    r.raise_for_status()
    return r.json().get("lists", [])

# ==== ClickUp: Получение списков без папки ====
def fetch_folderless_lists(space_id: str):
    base = f"https://api.clickup.com/api/v2/space/{space_id}/list"
    params = {"archived": "false"}
    logging.info(f"Fetching folderless lists from {base}")
    r = cu.get(base, params=params)
    logging.info(f"Folderless lists response: status {r.status_code}, body: {r.text[:200]}...")
    while _rate_limit_sleep(r):
        r = cu.get(base, params=params)
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
            "include_subtasks": "true",
            "archived": "false",
            "order_by": "updated",
            "reverse": "true",
            "subtasks": "true",
            "limit": 100,
        }
        if updated_gt:
            params["updated_gt"] = updated_gt
        if CLICKUP_ONLY_OPEN:
            params["statuses[]"] = ["to do", "in progress"]
        logging.info(f"Fetching tasks from list {list_id}, page {page}")
        r = cu.get(base, params=params)
        logging.info(f"Tasks response for list {list_id}: status {r.status_code}, body: {r.text[:200]}...")
        while _rate_limit_sleep(r):
            r = cu.get(base, params=params)
        r.raise_for_status()
        batch = r.json().get("tasks", [])
        if not batch:
            break
        for t in batch:
            total += 1
            yield t
        page += 1
    logging.info(f"Fetched {total} tasks from list {list_id}")

# ==== Получение полной задачи с description ====
def get_full_task(task_id: str):
    base = f"https://api.clickup.com/api/v2/task/{task_id}"
    params = {"include_subtasks": "true"}
    logging.info(f"Fetching full task {task_id}")
    r = cu.get(base, params=params)
    logging.info(f"Full task response: status {r.status_code}, body: {r.text[:200]}...")
    while _rate_limit_sleep(r):
        r = cu.get(base, params=params)
    r.raise_for_status()
    return r.json()

# ==== Главная функция для получения всех задач ====
def fetch_clickup_tasks(updated_after: datetime, space_id: str):
    total_tasks = 0
    folders = fetch_folders(space_id)
    for folder in folders:
        folder_id = folder.get("id")
        folder_name = folder.get("name")
        logging.info(f"Processing folder: {folder_name} (ID: {folder_id})")
        lists = fetch_lists_from_folder(folder_id)
        for lst in lists:
            list_id = lst.get("id")
            list_name = lst.get("name")
            if list_id in IGNORED_LIST_IDS:
                logging.info(f"Skipping ignored list: {list_name} (ID: {list_id})")
                continue
            logging.info(f"Processing list: {list_name} (ID: {list_id})")
            for task in fetch_tasks_from_list(list_id, updated_after):
                full_task = get_full_task(task["id"])
                total_tasks += 1
                yield full_task
    folderless_lists = fetch_folderless_lists(space_id)
    for lst in folderless_lists:
        list_id = lst.get("id")
        list_name = lst.get("name")
        if list_id in IGNORED_LIST_IDS:
            logging.info(f"Skipping ignored list: {list_name} (ID: {list_id})")
            continue
        logging.info(f"Processing folderless list: {list_name} (ID: {list_id})")
        for task in fetch_tasks_from_list(list_id, updated_after):
            full_task = get_full_task(task["id"])
            total_tasks += 1
            yield full_task
    logging.info(f"Total fetched tasks: {total_tasks}")

# ==== Преобразование задачи в HTML для Intercom ====
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body_html = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    if len(body_html) > 50000:
        body_html = body_html[:50000] + "<p><em>Описание урезано из-за длины</em></p>"
    status = (task.get("status") or {}).get("status")
    assignees = ", ".join(a.get("username") or a.get("email") or str(a.get("id")) for a in task.get("assignees", [])) or "—"
    priority = (task.get("priority") or {}).get("priority") or (task.get("priority") or {}).get("label") or "—"
    due = task.get("due_date")
    due_str = datetime.fromtimestamp(int(due)/1000, tz=timezone.utc).strftime("%Y-%m-%d") if due else "—"
    task_url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
    meta = f"""
    <div style='border:1px solid #eee;padding:12px;border-radius:8px;margin-bottom:12px'>
    <div><strong>Статус:</strong> {html.escape(str(status or '—'))}</div>
    <div><strong>Исполнители:</strong> {html.escape(assignees)}</div>
    <div><strong>Приоритет:</strong> {html.escape(str(priority))}</div>
    <div><strong>Дедлайн:</strong> {html.escape(due_str)}</div>
    <div><strong>ClickUp:</strong> <a href="{html.escape(task_url)}" target="_blank" rel="noopener">открыть задачу</a></div>
    </div>
    """
    title_html = f"<h1>{html.escape(name)}</h1>"
    return title_html + meta + body_html

# ==== Intercom: Поиск существующей статьи ====
def find_existing_article(title: str):
    """Поиск статьи по названию."""
    endpoint = f"{INTERCOM_BASE}/internal_articles/search"
    params = {"query": title}
    try:
        r = ic.get(endpoint, params=params)
        while _rate_limit_sleep(r):
            r = ic.get(endpoint, params=params)
        r.raise_for_status()
        data = r.json()
        articles = data.get("articles", [])
        if articles:
            logging.info(f"Found existing article: {title} (ID: {articles[0]['id']})")
            return articles[0]
        return None
    except Exception as e:
        logging.error(f"Error searching for article '{title}': {e}")
        return None

# ==== Intercom: Создание новой статьи ====
def create_internal_article(task: dict):
    task_id = task.get("id")
    endpoint = f"{INTERCOM_BASE}/internal_articles"
    title = task.get("name") or "(Без названия)"
    try:
        html_body = task_to_html(task)
        if len(html_body) > 50000:
            logging.warning(f"Task {task_id}: HTML too large, truncating to 50,000 chars")
            html_body = html_body[:50000]
        
        payload = {
            "title": title[:255],
            "body": html_body,
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "locale": "en",
        }
        
        if DRY_RUN:
            logging.info(f"[DRY_RUN] Would CREATE Internal Article for task {task_id}: {title}")
            return None
        
        logging.info(f"Creating Internal Article for task {task_id}: {title}")
        r = ic.post(endpoint, json=payload)
        logging.info(f"Intercom CREATE response for task {task_id}: status {r.status_code}, body: {r.text[:200]}...")
        
        while _rate_limit_sleep(r):
            r = ic.post(endpoint, json=payload)
            logging.info(f"Retry CREATE response for task {task_id}: status {r.status_code}")
        
        if r.status_code not in (200, 201):
            logging.error(f"Intercom CREATE failed for task {task_id}: {r.status_code} {r.text}")
            r.raise_for_status()
        
        result = r.json()
        logging.info(f"Created Internal Article: {title} (ID: {result.get('id')})")
        return result.get('id')
        
    except Exception as e:
        logging.error(f"Failed to create article for task {task_id}: {e}")
        return None

# ==== Intercom: Обновление существующей статьи ====
def update_internal_article(article_id: str, task: dict):
    task_id = task.get("id")
    endpoint = f"{INTERCOM_BASE}/internal_articles/{article_id}"
    title = task.get("name") or "(Без названия)"
    try:
        html_body = task_to_html(task)
        if len(html_body) > 50000:
            logging.warning(f"Task {task_id}: HTML too large, truncating to 50,000 chars")
            html_body = html_body[:50000]
        
        payload = {
            "title": title[:255],
            "body": html_body,
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "locale": "en",
        }
        
        if DRY_RUN:
            logging.info(f"[DRY_RUN] Would UPDATE Internal Article {article_id} for task {task_id}: {title}")
            return
        
        logging.info(f"Updating Internal Article {article_id} for task {task_id}: {title}")
        r = ic.put(endpoint, json=payload)
        logging.info(f"Intercom UPDATE response for task {task_id}: status {r.status_code}, body: {r.text[:200]}...")
        
        while _rate_limit_sleep(r):
            r = ic.put(endpoint, json=payload)
            logging.info(f"Retry UPDATE response for task {task_id}: status {r.status_code}")
        
        if r.status_code not in (200, 201):
            logging.error(f"Intercom UPDATE failed for task {task_id}: {r.status_code} {r.text}")
            r.raise_for_status()
        
        logging.info(f"Updated Internal Article {article_id}: {title}")
        
    except Exception as e:
        logging.error(f"Failed to update article {article_id} for task {task_id}: {e}")

# ==== Intercom: upsert Internal Article (создание/обновление) ====
def upsert_internal_article(task: dict):
    task_id = task.get("id")
    title = task.get("name") or "(Без названия)"
    
    # Поиск существующей статьи
    existing_article = find_existing_article(title)
    
    if existing_article:
        # Обновляем существующую статью
        article_id = existing_article.get("id")
        update_internal_article(article_id, task)
    else:
        # Создаем новую статью
        create_internal_article(task)

# ==== Главный процесс ====
def main():
    state = _load_state()
    last_sync_iso = state.get("last_sync_iso")
    if last_sync_iso and not FETCH_ALL:
        updated_after = datetime.fromisoformat(last_sync_iso)
    else:
        updated_after = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    
    logging.info(f"Syncing ClickUp tasks{' (all tasks)' if FETCH_ALL else f' updated after {updated_after.isoformat()}'} from space {SPACE_ID}")
    count = 0
    
    try:
        for task in fetch_clickup_tasks(updated_after, SPACE_ID):
            try:
                upsert_internal_article(task)
                count += 1
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
