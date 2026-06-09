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
    all_tasks = []
    page = 0
    max_pages = 50  # Немного увеличим лимит страниц для надежности
    
    while page < max_pages:
        params = {
            "subtasks": "false",
            "include_closed": "true",
            "order_by": "created",
            "reverse": "true",
            "page": page,
            "include_timl": "true"  # Включаем задачи, которые находятся в нескольких списках
        }
        
        log.info(f"Запрос страницы {page} из ClickUp...")
        page_has_tasks = False
        
        # Пробуем оба эндпоинта
        for endpoint in [f"list/{source_id}/task", f"view/{source_id}/task"]:
            url = f"https://api.clickup.com/api/v2/{endpoint}"
            try:
                r = cu.get(url, params=params)
                
                if r.status_code == 200:
                    data = r.json()
                    tasks = data.get("tasks", [])
                    
                    if tasks:
                        all_tasks.extend(tasks)
                        log.info(f"✅ Получено {len(tasks)} задач со страницы {page} ({endpoint})")
                        page_has_tasks = True
                    break  # Если эндпоинт успешно ответил, второй не дергаем
                    
                elif r.status_code == 400:
                    continue  # Пробуем следующий эндпоинт
            except Exception as e:
                log.error(f"Ошибка запроса к {endpoint}: {e}")
                continue
        
        # Если ни один эндпоинт не вернул задач для этой страницы — значит, мы точно дошли до конца
        if not page_has_tasks:
            log.info(f"На странице {page} задач больше нет. Завершаем сбор.")
            break
            
        page += 1
        time.sleep(0.8)
    
    log.info(f"✅ Всего загружено {len(all_tasks)} задач")
    return all_tasks

def get_existing_articles_in_folder(folder_id):
    existing = {}
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", 
                  params={"page": page, "per_page": 50, "folder_id": folder_id})
        if r.status_code != 200:
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
    if not name or name.lower() in existing_articles:
        if name:
            log.info(f"⏭️ Пропускаем (уже есть в Intercom): {name}")
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
    
    body = "\n".join(lines)
    
    payload = {
        "title": new_title,
        "body": body[:50000],
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": DEFAULT_FOLDER_ID
    }
    
    log.info(f"✨ Создаём: {name}")
    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    
    if r.status_code in (200, 201):
        log.info("✅ Создан")
    else:
        log.error(f"❌ Intercom ошибка {r.status_code}: {r.text}")

def main():
    folder_id = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_FOLDER_ID)
    source_id = sys.argv[2] if len(sys.argv) > 2 else "8cjzjmb-30872"
    
    current_folder = int(folder_id) if str(folder_id).isdigit() else DEFAULT_FOLDER_ID
    
    log.info(f"Запуск → Папка: {current_folder} | Source: {source_id}")
    
    tasks = get_all_tasks_from_source(source_id)
    
    if not tasks:
        log.error("Не удалось загрузить задачи")
        return
    
    existing_articles = get_existing_articles_in_folder(current_folder)
    
    for task in tasks:
        full_task = get_clickup_task(task["id"])
        if full_task:
            create_release_guide(full_task, existing_articles)
            time.sleep(1.0)
    
    log.info("🎉 Готово!")

def get_clickup_task(task_id):
    r = cu.get(f"https://api.clickup.com/api/v2/task/{task_id}", 
               params={"subtasks": "true", "include_subtasks": "true"})
    return r.json() if r.status_code == 200 else None

if __name__ == "__main__":
    main()
