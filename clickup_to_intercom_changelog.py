import os
import html
import logging
import requests
import time
from dotenv import load_dotenv
from collections import defaultdict

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
    """Получаем все главные задачи из списка"""
    params = {
        "subtasks": "false",
        "include_closed": "true",
        "order_by": "created",
        "reverse": "true"
    }
    r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params=params)
    if r.status_code == 200:
        tasks = r.json().get("tasks", [])
        log.info(f"Получено {len(tasks)} главных задач из ClickUp")
        return tasks
    else:
        log.error(f"ClickUp error {r.status_code}: {r.text}")
        return []

def get_clickup_task(task_id):
    """Получаем одну задачу со всеми сабтасками"""
    r = cu.get(f"https://api.clickup.com/api/v2/task/{task_id}", 
               params={"subtasks": "true", "include_subtasks": "true"})
    if r.status_code == 200:
        return r.json()
    else:
        log.error(f"Не удалось получить задачу {task_id}")
        return None

def find_article_by_title(title_prefix):
    """Ищем статью по названию релиза"""
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", 
                  params={"page": page, "per_page": 50})
        if r.status_code != 200:
            break
        articles = r.json().get("data", [])
        for art in articles:
            if title_prefix.lower() in art.get("title", "").lower():
                return art
        if page >= r.json().get("pages", {}).get("total_pages", 1):
            break
        page += 1
        time.sleep(0.3)
    return None

def create_or_update_release_guide(main_task):
    name = main_task.get("name", "Без названия")
    task_id = main_task.get("id")
    subtasks = main_task.get("subtasks", [])
    
    # Название гайда: "JetBooking 3.8.1 release"
    new_title = f"{name} release [{task_id}]"[:255]
    
    # Контент — только названия сабтасков (без статуса)
    lines = [f"<h1>{html.escape(name)} release</h1>"]
    
    if subtasks:
        lines.append("<ul>")
        for sub in subtasks:
            sub_name = html.escape(sub.get("name", "").strip())
            if sub_name:
                lines.append(f"  <li>{sub_name}</li>")
        lines.append("</ul>")
    else:
        lines.append("<p><em>В релизе нет подзадач.</em></p>")
    
    body = "\n".join(lines)
    
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
        log.info(f"🔄 Обновляем: {new_title}")
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{art_id}", json=payload)
    else:
        log.info(f"✨ Создаём: {new_title}")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    
    if r.status_code in (200, 201):
        log.info("✅ Успешно")
        if not existing and r.json().get("id"):
            log.info(f"   → https://app.intercom.com/a/articles/{r.json()['id']}")
    else:
        log.error(f"❌ Intercom ошибка {r.status_code}: {r.text}")

def main():
    # Можно передать аргументы: python script.py FOLDER_ID LIST_ID
    target_folder = sys.argv[1] if len(sys.argv) > 1 else None
    list_id = sys.argv[2] if len(sys.argv) > 2 else "901212763746"
    
    if target_folder and target_folder.isdigit():
        global DEFAULT_FOLDER_ID
        DEFAULT_FOLDER_ID = int(target_folder)
    
    log.info(f"Запуск синхронизации ченжлогов в папку Intercom: {DEFAULT_FOLDER_ID}")
    
    tasks = get_tasks_from_list(list_id)
    
    if not tasks:
        log.error("Не найдено задач в ClickUp")
        return
    
    for task in tasks:
        full_task = get_clickup_task(task["id"])
        if full_task:
            create_or_update_release_guide(full_task)
            time.sleep(1.2)  # небольшой пауза для rate limit
    
    log.info("🎉 Синхронизация завершена!")

if __name__ == "__main__":
    import sys
    main()
