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
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")  # Для проверки доступа
CLICKUP_ONLY_OPEN = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "Unstable")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"
SPACE_ID = "90125205902"  # Правильный ID пространства

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
    "Authorization": CLICKUP_TOKEN,  # Без Bearer для личного токена
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

# ==== Проверка доступа к team (для диагностики) ====
def check_team_access(team_id: str):
    base = f"https://api.clickup.com/api/v2/team/{team_id}"
    logging.info(f"Checking access to team {team_id}")
    r = cu.get(base)
    logging.info(f"Team check response: status {r.status_code}, body: {r.text[:500]}...")
    while _rate_limit_sleep(r):
        r = cu.get(base)
        logging.info(f"Retry team check: status {r.status_code}, body: {r.text[:500]}...")
    r.raise_for_status()
    logging.info(f"Team access OK: {r.json().get('team', {}).get('name')}")

# ==== ClickUp: Получение папок из пространства ====
def fetch_folders(space_id: str):
    base = f"https://api.clickup.com/api/v2/space/{space_id}/folder"
    params = {"archived": "false"}
    logging.info(f"Fetching folders from {base}")
    r = cu.get(base, params=params)
    logging.info(f"Folders response: status {r.status_code}, body: {r.text[:500]}...")
    while _rate_limit_sleep(r):
        r = cu.get(base, params=params)
        logging.info(f"Retry folders: status {r.status_code}, body: {r.text[:500]}...")
    r.raise_for_status()
    return r.json().get("folders", [])

# ==== ClickUp: Получение списков из папки ====
def fetch_lists_from_folder(folder_id: str):
    base = f"https://api.clickup.com/api/v2/folder/{folder_id}/list"
    params = {"archived": "false"}
    logging.info(f"Fetching lists from folder {folder_id}")
    r = cu.get(base, params=params)
    logging.info(f"Lists response: status {r.status_code}, body: {r.text[:500]}...")
    while _rate_limit_sleep(r):
        r = cu.get(base, params=params)
        logging.info(f"Retry lists: status {r.status_code}, body: {r.text[:500]}...")
    r.raise_for_status()
    return r.json().get("lists", [])

# ==== ClickUp: Получение списков без папки ====
def fetch_folderless_lists(space_id: str):
    base = f"https://api.clickup.com/api/v2/space/{space_id}/list"
    params = {"archived": "false"}
    logging.info(f"Fetching folderless lists from {base}")
    r = cu.get(base, params=params)
    logging.info(f"Folderless lists response: status {r.status_code}, body: {r.text[:500]}...")
    while _rate_limit_sleep(r):
        r = cu.get(base, params=params)
        logging.info(f"Retry folderless lists: status {r.status_code}, body: {r.text[:500]}...")
    r.raise_for_status()
    return r.json().get("lists", [])

# ==== ClickUp: Получение задач из списка ====
def fetch_tasks_from_list(list_id: str, updated_after: datetime):
    base = f"https://api.clickup.com/api/v2/list/{list_id}/task"
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
            "include_markdown_description": "true"
        }
        if updated_gt:
            params["updated_gt"] = updated_gt
        if CLICKUP_ONLY_OPEN:
            params["statuses[]"] = ["to do", "in progress"]
        logging.info(f"Fetching tasks from list {list_id}, page {page}")
        r = cu.get(base, params=params)
        logging.info(f"Tasks response: status {r.status_code}, body: {r.text[:500]}...")
        while _rate_limit_sleep(r):
            r = cu.get(base, params=params)
            logging.info(f"Retry tasks: status {r.status_code}, body: {r.text[:500]}...")
        r.raise_for_status()
        batch = r.json().get("tasks", [])
        if not batch:
            break
        for t in batch:
            desc = t.get('markdown_description') or t.get('description') or ""
            t['description'] = desc  # Fallback
            yield t
        page += 1

# ==== Главная функция для получения всех задач ====
def fetch_clickup_tasks(updated_after: datetime, space_id: str):
    folders = fetch_folders(space_id)
    for folder in folders:
        folder_id = folder.get("id")
        lists = fetch_lists_from_folder(folder_id)
        for lst in lists:
            list_id = lst.get("id")
            if list_id in IGNORED_LIST_IDS:
                continue
            for task in fetch_tasks_from_list(list_id, updated_after):
                yield task
    folderless_lists = fetch_folderless_lists(space_id)
    for lst in folderless_lists:
        list_id = lst.get("id")
        if list_id in IGNORED_LIST_IDS:
            continue
        for task in fetch_tasks_from_list(list_id, updated_after):
            yield task

# ==== Преобразование задачи в HTML для Intercom ====
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body_html = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    if len(body_html) > 50000:
        body_html = body_html[:50000] + "<p><em>Описание урезано из-за длины</em></p>"
    return f"<h1>{html.escape(name)}</h1>{body_html}"

# ==== Intercom: Поиск существующей статьи по title ====
def find_existing_article(title: str):
    endpoint = f"{INTERCOM_BASE}/internal_articles/search"
    params = {"query": title}
    try:
        r = ic.get(endpoint, params=params)
        logging.info(f"Search response for '{title}': status {r.status_code}, body: {r.text[:500]}...")
        while _rate_limit_sleep(r):
            r = ic.get(endpoint, params=params)
            logging.info(f"Retry search: status {r.status_code}, body: {r.text[:500]}...")
        r.raise_for_status()
        data = r.json()
        articles = data.get("articles", []) or data.get("data", {}).get("internal_articles", [])
        if articles:
            logging.info(f"Found article: {title} (ID: {articles[0]['id']})")
            return articles[0]
        return None
    except Exception as e:
        logging.error(f"Search error for '{title}': {e}")
        return None

# ==== Intercom: Создание статьи ====
def create_internal_article(task: dict):
    task_id = task.get("id")
    endpoint = f"{INTERCOM_BASE}/internal_articles"
    title = task.get("name") or "(Без названия)"
    try:
        html_body = task_to_html(task)
        if len(html_body) > 50000:
            html_body = html_body[:50000]
        payload = {
            "title": title[:255],
            "body": html_body,
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "locale": "ru",
        }
        if DRY_RUN:
            logging.info(f"[DRY_RUN] Would create: {title}")
            return None
        logging.info(f"Creating: {title}")
        r = ic.post(endpoint, json=payload)
        logging.info(f"Create response: status {r.status_code}, body: {r.text[:500]}...")
        while _rate_limit_sleep(r):
            r = ic.post(endpoint, json=payload)
            logging.info(f"Retry create: status {r.status_code}, body: {r.text[:500]}...")
        if r.status_code in (200, 201):
            result = r.json()
            logging.info(f"Created: {title} (ID: {result.get('id')})")
            return result.get('id')
        else:
            logging.error(f"Create failed: {r.status_code} {r.text}")
            return None
    except Exception as e:
        logging.error(f"Create error for {task_id}: {e}")
        return None

# ==== Intercom: Обновление статьи ====
def update_internal_article(article_id: str, task: dict):
    task_id = task.get("id")
    endpoint = f"{INTERCOM_BASE}/internal_articles/{article_id}"
    title = task.get("name") or "(Без названия)"
    try:
        html_body = task_to_html(task)
        if len(html_body) > 50000:
            html_body = html_body[:50000]
        payload = {
            "title": title[:255],
            "body": html_body,
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "locale": "ru",
        }
        if DRY_RUN:
            logging.info(f"[DRY_RUN] Would update {article_id}: {title}")
            return
        logging.info(f"Updating {article_id}: {title}")
        r = ic.put(endpoint, json=payload)
        logging.info(f"Update response: status {r.status_code}, body: {r.text[:500]}...")
        while _rate_limit_sleep(r):
            r = ic.put(endpoint, json=payload)
            logging.info(f"Retry update: status {r.status_code}, body: {r.text[:500]}...")
        if r.status_code in (200, 201):
            logging.info(f"Updated: {title}")
        else:
            logging.error(f"Update failed: {r.status_code} {r.text}")
    except Exception as e:
        logging.error(f"Update error for {task_id}: {e}")

# ==== Intercom: upsert (только создание новых, без обновлений) ====
def upsert_internal_article(task: dict):
    title = task.get("name") or "(Без названия)"
    existing_article = find_existing_article(title)
    if existing_article:
        logging.info(f"Skipping existing article: {title} (ID: {existing_article['id']})")  # Пропускаем обновление
    else:
        create_internal_article(task)

# ==== Главный процесс ====
def main():
    state = _load_state()
    last_sync_iso = state.get("last_sync_iso")
    if last_sync_iso and not FETCH_ALL:
        updated_after = datetime.fromisoformat(last_sync_iso)
    else:
        updated_after = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    logging.info(f"Syncing tasks after {updated_after.isoformat()} from space {SPACE_ID}")
    
    # Проверка доступа к team
    try:
        check_team_access(CLICKUP_TEAM_ID)
    except Exception as e:
        logging.error(f"Team access check failed: {e}")
        return  # Прерываем, если нет доступа
    
    count = 0
    try:
        for task in fetch_clickup_tasks(updated_after, SPACE_ID):
            try:
                upsert_internal_article(task)
                count += 1
            except Exception as e:
                logging.exception(f"Failed task {task.get('id')}: {e}")
    except Exception as e:
        logging.exception(f"Fetch error: {e}")
    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_sync_iso"] = now_iso
    _save_state(state)
    logging.info(f"Done. Synced {count} items. Last sync: {now_iso}")

if __name__ == "__main__":
    main()
