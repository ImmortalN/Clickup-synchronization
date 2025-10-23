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
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "Unstable")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"
SPACE_ID = "90125205902"

# Списки, которые нужно игнорировать
IGNORED_LIST_IDS = ["901212791461", "901212763746"]  # FORM и Changelog

# ==== Проверка обязательных переменных ====
for var_name in ["CLICKUP_TOKEN", "CLICKUP_TEAM_ID", "INTERCOM_TOKEN", "INTERCOM_OWNER_ID", "INTERCOM_AUTHOR_ID", "SPACE_ID"]:
    assert globals()[var_name], f"{var_name} is required"

# ==== Логирование ====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ==== Сессии ====
cu = requests.Session()
cu.headers.update({
    "Authorization": CLICKUP_TOKEN,
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

# ==== Вспомогательные функции ====
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

# ==== ClickUp: Проверка доступа к команде ====
def check_team_access(team_id: str):
    base = f"https://api.clickup.com/api/v2/team/{team_id}"
    logging.info(f"Checking access to team {team_id}")
    r = cu.get(base)
    while _rate_limit_sleep(r):
        r = cu.get(base)
    r.raise_for_status()
    logging.info(f"Team access OK: {r.json().get('team', {}).get('name')}")

# ==== ClickUp: Получение папок и списков ====
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

# ==== ClickUp: Получение задач ====
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
        r = cu.get(base, params=params)
        while _rate_limit_sleep(r):
            r = cu.get(base, params=params)
        r.raise_for_status()
        tasks = r.json().get("tasks", [])
        if not tasks:
            break
        for t in tasks:
            t['description'] = t.get('markdown_description') or t.get('description') or ""
            yield t
        page += 1

# ==== Главная функция для получения всех задач с игнорированием списков ====
def fetch_clickup_tasks(updated_after: datetime, space_id: str):
    folders = fetch_folders(space_id)
    for folder in folders:
        for lst in fetch_lists_from_folder(folder["id"]):
            if lst["id"] in IGNORED_LIST_IDS:
                logging.info(f"[IGNORED LIST] {lst['name']} ({lst['id']})")
                continue
            for task in fetch_tasks_from_list(lst["id"], updated_after):
                yield task
    for lst in fetch_folderless_lists(space_id):
        if lst["id"] in IGNORED_LIST_IDS:
            logging.info(f"[IGNORED LIST] {lst['name']} ({lst['id']})")
            continue
        for task in fetch_tasks_from_list(lst["id"], updated_after):
            yield task

# ==== Преобразование задачи в HTML для Intercom ====
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body_html = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    if len(body_html) > 50000:
        body_html = body_html[:50000] + "<p><em>Описание урезано из-за длины</em></p>"
    return f"<h1>{html.escape(name)}</h1>{body_html}"

# ==== Intercom: Поиск, создание и обновление статей ====
def find_existing_article(title: str):
    endpoint = f"{INTERCOM_BASE}/internal_articles/search"
    params = {"query": title}
    r = ic.get(endpoint, params=params)
    while _rate_limit_sleep(r):
        r = ic.get(endpoint, params=params)
    r.raise_for_status()
    data = r.json()
    articles = data.get("articles", []) or data.get("data", {}).get("internal_articles", [])
    return articles[0] if articles else None

def create_internal_article(task: dict):
    title = task.get("name") or "(Без названия)"
    if DRY_RUN:
        logging.info(f"[DRY_RUN] Would create article: {title}")
        return None
    payload = {
        "title": title[:255],
        "body": task_to_html(task),
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "locale": "ru",
    }
    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    while _rate_limit_sleep(r):
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    if r.status_code in (200, 201):
        logging.info(f"Created article: {title}")
        return r.json().get("id")
    logging.error(f"Failed to create article {title}: {r.status_code} {r.text}")
    return None

def update_internal_article(article_id: str, task: dict):
    title = task.get("name") or "(Без названия)"
    if DRY_RUN:
        logging.info(f"[DRY_RUN] Would update article {article_id}: {title}")
        return
    payload = {
        "title": title[:255],
        "body": task_to_html(task),
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "locale": "ru",
    }
    r = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
    while _rate_limit_sleep(r):
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
    if r.status_code in (200, 201):
        logging.info(f"Updated article: {title}")
    else:
        logging.error(f"Failed to update article {title}: {r.status_code} {r.text}")

def upsert_internal_article(task: dict):
    title = task.get("name") or "(Без названия)"
    existing = find_existing_article(title)
    if existing:
        update_internal_article(existing["id"], task)
    else:
        create_internal_article(task)

# ==== Главный процесс ====
def main():
    state = _load_state()
    last_sync_iso = state.get("last_sync_iso")
    updated_after = datetime.fromisoformat(last_sync_iso) if last_sync_iso and not FETCH_ALL else datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

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

    state["last_sync_iso"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)
    logging.info(f"Done. Synced {count} tasks. Last sync: {state['last_sync_iso']}")

if __name__ == "__main__":
    main()
