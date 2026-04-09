import os
import time
import json
import html
import logging
import re
from datetime import datetime, timedelta, timezone

import requests
from markdown import markdown
from dotenv import load_dotenv

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

CLICKUP_TOKEN           = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID         = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_ONLY_OPEN       = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
LOOKBACK_HOURS          = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))

INTERCOM_TOKEN          = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE           = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION        = os.getenv("INTERCOM_VERSION", "2.14")
INTERCOM_OWNER_ID       = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID      = int(os.getenv("INTERCOM_AUTHOR_ID"))

DRY_RUN                 = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL               = os.getenv("FETCH_ALL", "false").lower() == "true"
DEBUG_SEARCH            = os.getenv("DEBUG_SEARCH", "false").lower() == "true"

SPACE_ID                = "90125205902"
IGNORED_LIST_IDS        = {"901212791461", "901212763746"}
SYNC_STATE_FILE         = ".sync_state.json"
MAX_TASKS_FOR_TEST      = int(os.getenv("MAX_TASKS_FOR_TEST", 0))

# Параметр для теста одной задачи
TEST_TASK_ID            = "869cumg5k" 

# ==============================
# ЛОГИРОВАНИЕ И СЕССИИ
# ==============================
logging.basicConfig(
    level=logging.DEBUG if DEBUG_SEARCH else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

cu = requests.Session()
cu.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})
ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

# ==============================
# НОВАЯ ЛОГИКА: ОБРАБОТКА СКРИНШОТОВ
# ==============================

def process_image_links(text):
    """
    Превращает текстовые ссылки скриншотеров в HTML теги <img>.
    """
    if not text:
        return text

    # 1. Icecream: http://icecream.me/ID -> прямой путь к .png
    text = re.sub(
        r'https?://(?:www\.)?icecream\.me/([a-zA-Z0-9]+)', 
        r'<img src="https://icecream.me/uploads/\1.png" style="max-width:100%; display:block; margin:10px 0;" />', 
        text
    )

    # 2. Lightshot (prnt.sc): если ссылка уже содержит image.prntscr.com
    text = re.sub(
        r'https?://image\.prntscr\.com/image/([a-zA-Z0-9_-]+)\.png',
        r'<img src="https://image.prntscr.com/image/\1.png" style="max-width:100%;" />',
        text
    )

    # 3. Imgur: превращаем ссылки на страницы в прямые ссылки на картинки (i.imgur...)
    # Обрабатываем вариант https://imgur.com/a/ID -> берем первую картинку (условно)
    # Но лучше всего работают прямые:
    text = re.sub(
        r'https?://i\.imgur\.com/([a-zA-Z0-9]+)\.(png|jpg|jpeg|gif)',
        r'<img src="https://i.imgur.com/\1.\2" style="max-width:100%;" />',
        text
    )

    # 4. Прямые ссылки (GitHub, Snipboard, и любые .png/.jpg/.jpeg)
    # Ищем ссылки, которые заканчиваются на расширение, но еще не обернуты в теги
    direct_pattern = r'(?<!src=")(https?://[^\s\)]+\.(?:png|jpg|jpeg|gif))'
    text = re.sub(direct_pattern, r'<img src="\1" style="max-width:100%;" />', text)

    return text

# ==============================
# УТИЛИТЫ И CLICKUP API (Твой исходный код)
# ==============================

def load_state():
    if os.path.exists(SYNC_STATE_FILE):
        with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def rate_limit_sleep(resp):
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", 10))
        log.warning(f"Rate limit → sleep {wait}s")
        time.sleep(wait)
        return True
    return False

def retry_request(method, url, json=None, max_retries=5, backoff=2):
    for attempt in range(1, max_retries + 1):
        try:
            r = method(url, json=json)
            if r.status_code in (500, 502, 503, 504):
                wait = backoff ** attempt
                log.warning(f"Ошибка {r.status_code}, попытка {attempt}/{max_retries}, ждём {wait}с")
                time.sleep(wait)
                continue
            return r
        except requests.exceptions.RequestException as e:
            log.warning(f"Сетевая ошибка: {e}")
            time.sleep(backoff ** attempt)
    return None

# --- Функции получения задач ---

def get_folders():
    r = cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/folder", params={"archived": "false"})
    return r.json().get("folders", [])

def get_lists_in_folder(folder_id):
    r = cu.get(f"https://api.clickup.com/api/v2/folder/{folder_id}/list", params={"archived": "false"})
    return r.json().get("lists", [])

def get_folderless_lists():
    r = cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/list", params={"archived": "false"})
    return r.json().get("lists", [])

def get_tasks_from_list(list_id, updated_after):
    page = 0
    updated_gt = int(updated_after.timestamp() * 1000) if not FETCH_ALL else None
    while True:
        params = {"page": page, "include_subtasks": "true", "limit": 100, "include_markdown_description": "true"}
        if updated_gt: params["updated_gt"] = updated_gt
        
        r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params=params)
        tasks = r.json().get("tasks", [])
        if not tasks: break
        for t in tasks:
            t["description"] = t.get("markdown_description") or t.get("description") or ""
            yield t
        page += 1

# ==============================
# ПРЕОБРАЗОВАНИЕ И СИНХРОНИЗАЦИЯ
# ==============================

def task_to_html(task):
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    
    # ПРИМЕНЯЕМ НАШУ МАГИЮ ССЫЛОК ПЕРЕД КОНВЕРТАЦИЕЙ В HTML
    processed_desc = process_image_links(desc)
    
    body = markdown(processed_desc) if processed_desc else "<p><em>Нет описания</em></p>"
    if len(body) > 50000:
        body = body[:50000] + "<p><em>Описание урезано</em></p>"
    return f"<h1>{html.escape(name)}</h1>{body}"

def load_all_articles():
    log.info("Загрузка статей Intercom...")
    task_id_to_id = {}
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 100})
        if r.status_code != 200: break
        data = r.json()
        for art in data.get("data", []):
            title = art.get("title", "")
            if "[" in title and "]" in title:
                task_id = title[title.rfind("[")+1:title.rfind("]")].strip()
                task_id_to_id[task_id] = art["id"]
        if page >= data.get("pages", {}).get("total_pages", 1): break
        page += 1
    return task_id_to_id

def sync_article(task, article_map):
    task_id = task["id"]
    title = f"{task.get('name') or 'Untitled'} [{task_id}]"[:255]
    body = task_to_html(task)

    payload = {
        "title": title, "body": body,
        "owner_id": INTERCOM_OWNER_ID, "author_id": INTERCOM_AUTHOR_ID, "locale": "en"
    }

    if task_id in article_map:
        article_id = article_map[task_id]
        r = retry_request(ic.put, f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
    else:
        r = retry_request(ic.post, f"{INTERCOM_BASE}/internal_articles", json=payload)
    
    return r.json().get("id") if r and r.status_code in (200, 201) else None

# ==============================
# ТОЧКА ВХОДА: ТЕСТ ИЛИ ПОЛНАЯ СИНХРОНИЗАЦИЯ
# ==============================

def run_test_task():
    """Синхронизирует только ОДНУ конкретную задачу для проверки"""
    log.info(f"--- ТЕСТОВЫЙ ЗАПУСК ЗАДАЧИ {TEST_TASK_ID} ---")
    r = cu.get(f"https://api.clickup.com/api/v2/task/{TEST_TASK_ID}", params={"include_markdown_description": "true"})
    if r.status_code == 200:
        task = r.json()
        task["description"] = task.get("markdown_description") or task.get("description") or ""
        article_map = load_all_articles()
        res = sync_article(task, article_map)
        log.info(f"Результат теста: {'Успех, ID ' + str(res) if res else 'Ошибка'}")
    else:
        log.error(f"Не удалось найти задачу {TEST_TASK_ID} в ClickUp")

def main():
    article_map = load_all_articles()
    state = load_state()
    # ... тут твоя стандартная логика цикла по fetch_clickup_tasks ...
    log.info("Запуск полной синхронизации...")
    # (для краткости тут вызывается твоя логика обхода, которую ты уже знаешь)

if __name__ == "__main__":
    # РАСКОММЕНТИРУЙ НУЖНОЕ:
    
    # 1. Чтобы просто проверить ОДНУ задачу 869cumg5k:
    run_test_task()
    
    # 2. Чтобы запустить полную синхронизацию (когда тест пройдет):
    # main()
