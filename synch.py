import os
import time
import json
import html
import logging
from datetime import datetime, timedelta, timezone
import requests
from markdown import markdown
from dotenv import load_dotenv

# ==== Загрузка переменных ====
load_dotenv()

# ==== КОНФИГУРАЦИЯ ====
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
SPACE_ID = os.getenv("SPACE_ID", "90153590151")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", "5475435"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", "5475435"))
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "2.14")

LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))
CLICKUP_ONLY_OPEN = os.getenv("CLICKUP_ONLY_OPEN", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "true").lower() == "true"  # Для первого запуска
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")

IGNORED_LIST_IDS = ["901509433569", "901509402998"]

# ==== ПРОВЕРКА ПЕРЕМЕННЫХ ====
print("=== DEBUG: Проверка переменных ===")
print(f"CLICKUP_TOKEN: {'OK' if CLICKUP_TOKEN else 'MISSING'} (длина: {len(CLICKUP_TOKEN) if CLICKUP_TOKEN else 0})")
print(f"CLICKUP_TEAM_ID: {CLICKUP_TEAM_ID}")
print(f"SPACE_ID: {SPACE_ID}")
print(f"INTERCOM_TOKEN: {'OK' if INTERCOM_TOKEN else 'MISSING'}")

assert CLICKUP_TOKEN, "❌ CLICKUP_API_TOKEN отсутствует!"
assert INTERCOM_TOKEN, "❌ INTERCOM_ACCESS_TOKEN отсутствует!"
print("✅ Все переменные OK!")

# ==== ЛОГИРОВАНИЕ ====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ==== СЕССИИ ====
cu = requests.Session()
cu.headers.update({
    "Authorization": CLICKUP_TOKEN,
    "Content-Type": "application/json"
})
cu.timeout = 30

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})
ic.timeout = 30

# ==== УТИЛИТЫ ====
def _rate_limit_sleep(resp):
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 10))
        logging.warning(f"Rate limit. Ждём {retry_after}с")
        time.sleep(retry_after)
        return True
    return False

def _load_state():
    try:
        if os.path.exists(SYNC_STATE_FILE):
            with open(SYNC_STATE_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return {}

def _save_state(state):
    with open(SYNC_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ==== ТЕСТ ТОКЕНА CLICKUP (ОБЯЗАТЕЛЬНО!) ====
def test_clickup_token():
    print("🔍 Тестируем ClickUp токен...")
    r = cu.get("https://api.clickup.com/api/v2/team")
    print(f"   Статус: {r.status_code}")
    print(f"   Ответ: {r.text[:100]}...")
    
    while _rate_limit_sleep(r):
        r = cu.get("https://api.clickup.com/api/v2/team")
    
    if r.status_code == 200:
        teams = r.json().get("teams", [])
        print(f"✅ ClickUp OK! Найдено команд: {len(teams)}")
        return True
    else:
        print(f"❌ ClickUp FAILED: {r.status_code}")
        raise ValueError(f"Неверный токен: {r.text}")

# ==== CLICKUP ФУНКЦИИ ====
def fetch_folders(space_id):
    url = f"https://api.clickup.com/api/v2/space/{space_id}/folder"
    params = {"archived": "false"}
    print(f"📁 Получаем папки из {space_id}...")
    
    r = cu.get(url, params=params)
    print(f"   Статус: {r.status_code}")
    
    while _rate_limit_sleep(r):
        r = cu.get(url, params=params)
    
    r.raise_for_status()
    folders = r.json().get("folders", [])
    print(f"   Найдено папок: {len(folders)}")
    return folders

def fetch_lists_from_folder(folder_id):
    url = f"https://api.clickup.com/api/v2/folder/{folder_id}/list"
    params = {"archived": "false"}
    
    r = cu.get(url, params=params)
    while _rate_limit_sleep(r):
        r = cu.get(url, params=params)
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_folderless_lists(space_id):
    url = f"https://api.clickup.com/api/v2/space/{space_id}/list"
    params = {"archived": "false"}
    
    r = cu.get(url, params=params)
    while _rate_limit_sleep(r):
        r = cu.get(url, params=params)
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_tasks_from_list(list_id, updated_after):
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    page = 0
    
    while True:
        params = {
            "page": page,
            "limit": 100,
            "include_subtasks": "true",
            "archived": "false",
            "subtasks": "true"
        }
        
        r = cu.get(url, params=params)
        while _rate_limit_sleep(r):
            r = cu.get(url, params=params)
        r.raise_for_status()
        
        tasks = r.json().get("tasks", [])
        if not tasks:
            break
            
        for task in tasks:
            yield task
            
        page += 1

def fetch_clickup_tasks(updated_after, space_id):
    total = 0
    
    # Папки
    folders = fetch_folders(space_id)
    for folder in folders:
        lists = fetch_lists_from_folder(folder["id"])
        for lst in lists:
            if lst["id"] in IGNORED_LIST_IDS:
                continue
            print(f"📋 Список: {lst['name']} ({len(list(fetch_tasks_from_list(lst['id'], updated_after)))} задач)")
            for task in fetch_tasks_from_list(lst["id"], updated_after):
                total += 1
                yield task
    
    # Без папки
    folderless = fetch_folderless_lists(space_id)
    for lst in folderless:
        if lst["id"] in IGNORED_LIST_IDS:
            continue
        for task in fetch_tasks_from_list(lst["id"], updated_after):
            total += 1
            yield task
    
    print(f"✅ Всего задач: {total}")

# ==== INTERCOM ФУНКЦИИ (УПРОЩЁННЫЕ) ====
def create_internal_article(task):
    title = task.get("name", "Без названия")
    print(f"📝 Создаём статью: {title}")
    
    payload = {
        "title": title[:255],
        "body": f"<h1>{title}</h1><p>Синхронизировано из ClickUp</p>",
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "locale": "en"
    }
    
    if DRY_RUN:
        print(f"   [DRY_RUN] НЕ создаём")
        return "test_id"
    
    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    print(f"   Статус: {r.status_code}")
    if r.status_code in (200, 201):
        print(f"✅ Статья создана!")
        return r.json().get("id")
    else:
        print(f"❌ Ошибка: {r.text}")
        return None

# ==== ГЛАВНАЯ ФУНКЦИЯ ====
def main():
    print("🚀 НАЧИНАЕМ СИНХРОНИЗАЦИЮ!")
    
    # 1. Тест токена
    test_clickup_token()
    
    # 2. Получаем задачи
    updated_after = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS) if not FETCH_ALL else None
    tasks = list(fetch_clickup_tasks(updated_after, SPACE_ID))
    
    # 3. Создаём статьи
    count = 0
    for task in tasks[:3]:  # Первые 3 для теста
        article_id = create_internal_article(task)
        if article_id:
            count += 1
    
    print(f"🎉 СИНХРОНИЗАЦИЯ ЗАВЕРШЕНА! Создано статей: {count}")
    
    # Сохраняем состояние
    _save_state({"last_sync_iso": datetime.now(timezone.utc).isoformat()})

if __name__ == "__main__":
    main()
