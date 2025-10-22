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
LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "Unstable")  # ИСПРАВЛЕНО: Unstable для internal articles
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"
SPACE_ID = "90153590151"

IGNORED_LIST_IDS = ["901509433569", "901509402998"]

# ==== Проверка обязательных переменных ====
assert CLICKUP_TOKEN, "CLICKUP_API_TOKEN is required"
assert INTERCOM_TOKEN, "INTERCOM_ACCESS_TOKEN is required"
assert INTERCOM_OWNER_ID, "INTERCOM_OWNER_ID is required"
assert INTERCOM_AUTHOR_ID, "INTERCOM_AUTHOR_ID is required"

# ==== Логирование ====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ==== Сессии ====
cu = requests.Session()
cu.headers.update({
    "Authorization": CLICKUP_TOKEN,  # УБРАНО Bearer - ClickUp не требует его
    "Content-Type": "application/json"
})

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
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

# ==== ClickUp: Получение всех задач напрямую из пространства (ОПТИМИЗИРОВАНО) ====
def fetch_clickup_tasks(space_id: str, updated_after: datetime):
    """Получаем задачи напрямую из пространства - быстрее и проще"""
    base = f"https://api.clickup.com/api/v2/space/{space_id}/task"
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
            "custom_fields": "true",  # Для полного описания
            "tags[]": [],  # Чтобы получить все теги
        }
        if updated_gt:
            params["updated_gt"] = updated_gt
            
        logging.info(f"Fetching tasks from space {space_id}, page {page}")
        r = cu.get(base, params=params)
        while _rate_limit_sleep(r):
            r = cu.get(base, params=params)
        r.raise_for_status()
        
        data = r.json()
        batch = data.get("tasks", [])
        if not batch:
            break
            
        for task in batch:
            # Проверяем, не из ли игнорируемых листов
            list_id = task.get("list", {}).get("id")
            if list_id in IGNORED_LIST_IDS:
                logging.info(f"Skipping ignored list task: {task.get('name')}")
                continue
                
            total += 1
            yield task  # Уже полная задача с description
            
        page += 1
    
    logging.info(f"Fetched {total} tasks from space {space_id}")

# ==== УПРОЩЕННАЯ функция для HTML (ТОЛЬКО title + description) ====
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    
    # ПРОСТОЙ HTML: только название и описание
    body_html = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    
    # Ограничиваем длину
    if len(body_html) > 50000:
        body_html = body_html[:50000] + "<p><em>Описание урезано</em></p>"
    
    # Добавляем ссылку на задачу
    task_url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
    footer = f"""
    <hr>
    <p><small><a href="{html.escape(task_url)}" target="_blank">🔗 Открыть в ClickUp</a></small></p>
    """
    
    return f"<h1>{html.escape(name)}</h1>{body_html}{footer}"

# ==== Intercom: Поиск статьи по external_id (НАДЕЖНЕЕ) ====
def find_article_by_external_id(external_id: str):
    """Поиск статьи по external_id - более надежно чем по названию"""
    endpoint = f"{INTERCOM_BASE}/internal_articles/search"
    params = {"query": f"external_id:{external_id}"}
    try:
        r = ic.get(endpoint, params=params)
        while _rate_limit_sleep(r):
            r = ic.get(endpoint, params=params)
        r.raise_for_status()
        
        data = r.json()
        articles = data.get("articles", [])
        if articles:
            logging.info(f"Found existing article by external_id {external_id}: ID {articles[0]['id']}")
            return articles[0]
        return None
    except Exception as e:
        logging.error(f"Error searching article by external_id '{external_id}': {e}")
        return None

# ==== Intercom: Создание статьи ====
def create_internal_article(task: dict):
    task_id = task.get("id")
    endpoint = f"{INTERCOM_BASE}/internal_articles"
    title = task.get("name") or "(Без названия)"
    
    try:
        html_body = task_to_html(task)
        
        payload = {
            "title": title[:255],
            "body": html_body,
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "locale": "en",
            "external_id": task_id,  # КЛЮЧЕВОЕ: для синхронизации
        }
        
        if DRY_RUN:
            logging.info(f"[DRY_RUN] Would CREATE: {title}")
            return None
        
        logging.info(f"Creating article: {title}")
        r = ic.post(endpoint, json=payload)
        logging.info(f"CREATE response: {r.status_code}")
        
        while _rate_limit_sleep(r):
            r = ic.post(endpoint, json=payload)
        
        if r.status_code in (200, 201):
            result = r.json()
            logging.info(f"✅ CREATED: {title} (ID: {result.get('id')})")
            return result.get('id')
        else:
            logging.error(f"❌ CREATE failed: {r.status_code} {r.text[:200]}")
            return None
            
    except Exception as e:
        logging.error(f"❌ CREATE error for {task_id}: {e}")
        return None

# ==== Intercom: Обновление статьи ====
def update_internal_article(article_id: str, task: dict):
    task_id = task.get("id")
    endpoint = f"{INTERCOM_BASE}/internal_articles/{article_id}"
    title = task.get("name") or "(Без названия)"
    
    try:
        html_body = task_to_html(task)
        
        payload = {
            "title": title[:255],
            "body": html_body,
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "locale": "en",
            "external_id": task_id,
        }
        
        if DRY_RUN:
            logging.info(f"[DRY_RUN] Would UPDATE {article_id}: {title}")
            return
        
        logging.info(f"Updating article {article_id}: {title}")
        r = ic.put(endpoint, json=payload)
        logging.info(f"UPDATE response: {r.status_code}")
        
        while _rate_limit_sleep(r):
            r = ic.put(endpoint, json=payload)
        
        if r.status_code in (200, 201):
            logging.info(f"✅ UPDATED: {title}")
        else:
            logging.error(f"❌ UPDATE failed: {r.status_code} {r.text[:200]}")
            
    except Exception as e:
        logging.error(f"❌ UPDATE error for {task_id}: {e}")

# ==== Главная функция upsert ====
def upsert_internal_article(task: dict):
    task_id = task.get("id")
    title = task.get("name") or "(Без названия)"
    
    # Ищем по external_id
    existing = find_article_by_external_id(task_id)
    
    if existing:
        update_internal_article(existing["id"], task)
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
    
    logging.info(f"🚀 Starting sync after {updated_after.isoformat()}")
    count = 0
    
    try:
        for task in fetch_clickup_tasks(SPACE_ID, updated_after):
            try:
                upsert_internal_article(task)
                count += 1
                logging.info(f"Processed {count}: {task.get('name')[:50]}...")
            except Exception as e:
                logging.error(f"Failed task {task.get('id')}: {e}")
                continue
    except Exception as e:
        logging.error(f"Sync error: {e}")
    
    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_sync_iso"] = now_iso
    _save_state(state)
    logging.info(f"✅ Done! Synced {count} articles")

if __name__ == "__main__":
    main()
