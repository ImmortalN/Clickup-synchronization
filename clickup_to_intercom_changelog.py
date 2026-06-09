import os
import html
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"

DEFAULT_FOLDER_ID = 4725328
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
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

def get_tasks_from_list(list_id):
    """Получаем задачи из List"""
    params = {
        "subtasks": "true",
        "include_closed": "true",
        "order_by": "created",
        "reverse": "true",
        "page": 0
    }
    
    r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params=params)
    if r.status_code == 200:
        tasks = r.json().get("tasks", [])
        log.info(f"Получено {len(tasks)} задач из списка")
        return tasks
    else:
        log.error(f"ClickUp error {r.status_code}: {r.text}")
        return []

def get_clickup_task(task_id):
    """Получаем полную задачу с сабтасками"""
    r = cu.get(f"https://api.clickup.com/api/v2/task/{task_id}", 
               params={"subtasks": "true"})
    if r.status_code == 200:
        return r.json()
    return None

def find_article_by_title(title_prefix):
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200: 
            break
        articles = r.json().get("data", [])
        for art in articles:
            if title_prefix.lower() in art.get("title", "").lower():
                return art
        if page >= r.json().get("pages", {}).get("total_pages", 1):
            break
        page += 1
    return None

def create_test_guide(main_task_id):
    task = get_clickup_task(main_task_id)
    if not task:
        log.error("Не удалось получить главную задачу")
        return
    
    name = task.get("name", "Без названия")
    subtasks = task.get("subtasks", [])
    
    log.info(f"Релиз: {name}")
    log.info(f"Найдено сабтасков: {len(subtasks)}")
    
    # Формируем HTML-контент
    lines = [f"<h1>{html.escape(name)}</h1>"]
    lines.append("<ul>")
    for sub in subtasks:
        sub_name = html.escape(sub.get("name", ""))
        status = html.escape(sub.get("status", {}).get("status", "unknown"))
        lines.append(f"<li>{sub_name} <em>({status})</em></li>")
    lines.append("</ul>")
    
    body = "\n".join(lines)
    new_title = f"{name} [{main_task_id}]"[:255]
    
    payload = {
        "title": new_title,
        "body": body[:50000],
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": DEFAULT_FOLDER_ID
    }
    
    existing = find_article_by_title(name)
    
    if existing:
        art_id = existing["id"]
        log.info(f"🔄 Обновляем статью {art_id}")
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{art_id}", json=payload)
    else:
        log.info(f"✨ Создаём новую статью: {new_title}")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    
    if r.status_code in (200, 201):
        log.info("✅ УСПЕШНО СОЗДАНО / ОБНОВЛЕНО!")
    else:
        log.error(f"❌ Intercom ошибка {r.status_code}: {r.text}")

if __name__ == "__main__":
    LIST_ID = "901212763746"          # ← твой List ID
    tasks = get_tasks_from_list(LIST_ID)
    
    if tasks:
        # Берём первую главную задачу (самый свежий релиз)
        first_main_task = tasks[0]    # можно поменять индекс
        log.info(f"Тестируем релиз: {first_main_task['name']}")
        create_test_guide(first_main_task["id"])
    else:
        log.error("Задачи не найдены в списке")
