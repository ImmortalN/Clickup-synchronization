import os
import html
import logging
import requests
import time
import sys
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

def get_all_tasks_from_source(source_id):
    """Получаем ВСЕ задачи (улучшенная пагинация)"""
    all_tasks = []
    page = 0
    
    while True:
        params = {
            "subtasks": "false",
            "include_closed": "true",
            "order_by": "created",
            "reverse": "true",
            "page": page
        }
        
        # Пробуем List
        r = cu.get(f"https://api.clickup.com/api/v2/list/{source_id}/task", params=params)
        if r.status_code != 200:
            # Пробуем View
            r = cu.get(f"https://api.clickup.com/api/v2/view/{source_id}/task", params=params)
        
        if r.status_code == 200:
            data = r.json()
            tasks = data.get("tasks", [])
            all_tasks.extend(tasks)
            log.info(f"Получено {len(tasks)} задач (страница {page})")
            
            # Если меньше 100 — это последняя страница
            if len(tasks) < 100:
                break
            page += 1
            time.sleep(0.6)
        else:
            log.error(f"ClickUp ошибка: {r.status_code} — {r.text}")
            break
    
    log.info(f"✅ Всего загружено {len(all_tasks)} задач из ClickUp")
    return all_tasks

def get_existing_articles_in_folder(folder_id):
    """Загружаем гайды ТОЛЬКО из нужной папки"""
    existing = {}
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", 
                  params={
                      "page": page, 
                      "per_page": 50,
                      "folder_id": folder_id   # Фильтр по папке
                  })
        if r.status_code != 200:
            log.warning(f"Intercom error {r.status_code}")
            break
            
        data = r.json()
        for art in data.get("data", []):
            title = art.get("title", "")
            clean_name = title.split(" release [")[0].strip()
            existing[clean_name.lower()] = art
        
        if page >= data.get("pages", {}).get("total_pages", 1):
            break
        page += 1
        time.sleep(0.4)
    
    log.info(f"Загружено {len(existing)} гайдов из папки {folder_id}")
    return existing

def create_release_guide(main_task, existing_articles):
    name = main_task.get("name", "").strip()
    if not name:
        return
    
    if name.lower() in existing_articles:
        log.info(f"⏭️ Пропускаем (уже существует): {name}")
        return
    
    task_id = main_task.get("id")
    subtasks = main_task.get("subtasks", [])
    
    new_title = f"{name} release [{task_id}]"[:255]
    
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
    
    log.info(f"✨ Создаём новый гайд: {name}")
    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    
    if r.status_code in (200, 201):
        log.info("✅ Успешно создан")
    else:
        log.error(f"❌ Intercom ошибка {r.status_code}: {r.text}")

def main():
    folder_id = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_FOLDER_ID)
    source_id = sys.argv[2] if len(sys.argv) > 2 else "8cjzjmb-30872"
    
    current_folder = int(folder_id) if str(folder_id).isdigit() else DEFAULT_FOLDER_ID
    
    log.info(f"Запуск синхронизации в папку Intercom: {current_folder}")
    log.info(f"Источник ClickUp: {source_id}")
    
    tasks = get_all_tasks_from_source(source_id)
    if not tasks:
        log.error("Не найдено задач в ClickUp.")
        return
    
    existing_articles = get_existing_articles_in_folder(current_folder)
    
    for task in tasks:
        full_task = get_clickup_task(task["id"])
        if full_task:
            create_release_guide(full_task, existing_articles)
            time.sleep(1.0)
    
    log.info("🎉 Синхронизация завершена!")

def get_clickup_task(task_id):
    r = cu.get(f"https://api.clickup.com/api/v2/task/{task_id}", 
               params={"subtasks": "true", "include_subtasks": "true"})
    if r.status_code == 200:
        return r.json()
    log.warning(f"Не удалось загрузить детали задачи {task_id}")
    return None

if __name__ == "__main__":
    main()
