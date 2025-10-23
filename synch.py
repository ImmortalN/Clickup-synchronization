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
DELETE_MODE = os.getenv("DELETE_MODE", "false").lower() == "true"  # Режим удаления
CLEANUP_DUPLICATES = os.getenv("CLEANUP_DUPLICATES", "false").lower() == "true"  # Очистка дублей
SPACE_ID = "90125205902"  # Правильный ID пространства

IGNORED_LIST_IDS = ["901212791461", "901212763746"]  # FORM и Changelog (обновлённые ID)

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
def fetch_tasks_from_list(list_id: str, updated_after: datetime = None):
    base = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    page = 0
    updated_gt = int(updated_after.timestamp() * 1000) if updated_after and not FETCH_ALL else None
    tasks = []
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
            tasks.append(t)
        page += 1
    return tasks

# ==== Главная функция для получения всех задач ====
def fetch_clickup_tasks(updated_after: datetime, space_id: str):
    folders = fetch_folders(space_id)
    for folder in folders:
        folder_id = folder.get("id")
        lists = fetch_lists_from_folder(folder_id)
        for lst in lists:
            list_id = lst.get("id")
            if list_id in IGNORED_LIST_IDS:
                logging.info(f"Skipping ignored list: {lst.get('name')} (ID: {list_id})")
                continue
            for task in fetch_tasks_from_list(list_id, updated_after):
                yield task
    folderless_lists = fetch_folderless_lists(space_id)
    for lst in folderless_lists:
        list_id = lst.get("id")
        if list_id in IGNORED_LIST_IDS:
            logging.info(f"Skipping ignored list: {lst.get('name')} (ID: {list_id})")
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

# ==== Intercom: Поиск существующей статьи по title (оригинальный + с [task_id]) ====
def find_existing_article(title: str, task_id: str):
    search_queries = [title, f"{title} [{task_id}]"]
    endpoint = f"{INTERCOM_BASE}/internal_articles/search"
    for query in search_queries:
        params = {"query": query}
        try:
            r = ic.get(endpoint, params=params)
            logging.info(f"Search response for '{query}': status {r.status_code}, body: {r.text[:500]}...")
            while _rate_limit_sleep(r):
                r = ic.get(endpoint, params=params)
                logging.info(f"Retry search: status {r.status_code}, body: {r.text[:500]}...")
            r.raise_for_status()
            data = r.json()
            articles = data.get("articles", [])
            if articles:
                logging.info(f"Found article for '{title}' via '{query}': ID {articles[0]['id']}")
                return articles[0]
        except Exception as e:
            logging.error(f"Search error for '{query}': {e}")
            continue
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

# ==== Intercom: upsert ====
def upsert_internal_article(task: dict):
    title = task.get("name") or "(Без названия)"
    existing_article = find_existing_article(title, task.get("id"))
    if existing_article:
        update_internal_article(existing_article["id"], task)
    else:
        create_internal_article(task)

# ==== Функция удаления гайдов из ignored lists (тестовая версия с лимитом 10) ====
def delete_guides_from_ignored_lists():
    logging.info("Starting test deletion from ignored lists (limited to 10 tasks per list)")
    deleted_count = 0
    for list_id in IGNORED_LIST_IDS:
        tasks = fetch_tasks_from_list(list_id)
        tasks = tasks[:TEST_LIMIT_TASKS]  # Ограничение для теста
        for task in tasks:
            title = task.get("name") or "(Без названия)"
            task_id = task.get("id")
            existing_article = find_existing_article(title, task_id)
            if existing_article:
                endpoint = f"{INTERCOM_BASE}/internal_articles/{existing_article['id']}"
                if DRY_RUN:
                    logging.info(f"[DRY_RUN] Would delete: {title} (ID: {existing_article['id']})")
                    continue
                r = ic.delete(endpoint)
                logging.info(f"Delete response: status {r.status_code}, body: {r.text[:500]}...")
                while _rate_limit_sleep(r):
                    r = ic.delete(endpoint)
                    logging.info(f"Retry delete: status {r.status_code}, body: {r.text[:500]}...")
                if r.status_code in (200, 204):
                    logging.info(f"Deleted: {title} (ID: {existing_article['id']})")
                    deleted_count += 1
                else:
                    logging.error(f"Delete failed for {title}: {r.status_code} {r.text}")
            else:
                logging.info(f"No guide found for {title} (task {task_id})")
    logging.info(f"Test deletion done: Deleted {deleted_count} guides from limited tasks")

# ==== Функция очистки дублей ====
def cleanup_duplicates():
    logging.info("Starting duplicate cleanup")
    endpoint = f"{INTERCOM_BASE}/internal_articles"
    r = ic.get(endpoint)
    r.raise_for_status()
    articles = r.json().get("articles", [])
    title_to_ids = {}
    for article in articles:
        clean_title = article.get("title").rsplit('[', 1)[0].strip() if '[' in article.get("title") else article.get("title")
        if clean_title not in title_to_ids:
            title_to_ids[clean_title] = []
        title_to_ids[clean_title].append(article["id"])
    deleted = 0
    for title, ids in title_to_ids.items():
        if len(ids) > 1:
            to_delete = ids[1:]  # Оставляем первый
            for del_id in to_delete:
                endpoint = f"{INTERCOM_BASE}/internal_articles/{del_id}"
                if DRY_RUN:
                    logging.info(f"[DRY_RUN] Would delete duplicate: {title} (ID: {del_id})")
                    continue
                r = ic.delete(endpoint)
                if r.status_code in (200, 204):
                    logging.info(f"Deleted duplicate: {title} (ID: {del_id})")
                    deleted += 1
                else:
                    logging.error(f"Delete failed for duplicate {title}: {r.status_code} {r.text}")
    logging.info(f"Cleanup done: Deleted {deleted} duplicates")

# ==== Главный процесс ====
def main():
    if DELETE_MODE:
        delete_guides_from_ignored_lists()
        return
    if CLEANUP_DUPLICATES:
        cleanup_duplicates()
        return

    state = _load_state()
    last_sync_iso = state.get("last_sync_iso")
    if last_sync_iso and not FETCH_ALL:
        updated_after = datetime.fromisoformat(last_sync_iso)
    else:
        updated_after = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    logging.info(f"Syncing tasks after {updated_after.isoformat()} from space {SPACE_ID}")
    
    try:
        check_team_access(CLICKUP_TEAM_ID)
    except Exception as e:
        logging.error(f"Team access check failed: {e}")
        return
    
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
