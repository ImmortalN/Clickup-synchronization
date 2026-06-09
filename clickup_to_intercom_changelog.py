import os
import sys
import time
import html
import logging
import re
import requests
from markdown import markdown
from dotenv import load_dotenv
from datetime import datetime
from collections import defaultdict

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"
DEFAULT_FOLDER_ID = 4725328  # Твоя папка
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

def get_tasks_from_list(list_id_or_view):
    """Получаем задачи из list или view (поддержка твоего view ID)"""
    params = {
        "include_closed": "true",
        "subtasks": "true",
        "order_by": "created",
        "reverse": "true"
    }
    
    # Пробуем как list
    r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id_or_view}/task", params=params)
    if r.status_code == 200:
        return r.json().get("tasks", [])
    
    # Пробуем как view
    r = cu.get(f"https://api.clickup.com/api/v2/view/{list_id_or_view}/task", params=params)
    if r.status_code == 200:
        return r.json().get("tasks", [])
    
    log.error(f"Не удалось получить задачи из {list_id_or_view}: {r.status_code}")
    return []

def find_article_by_title(title_prefix):
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200: break
        data = r.json()
        articles = data.get("data", [])
        if not articles: break
       
        for art in articles:
            if title_prefix.lower() in art.get("title", "").lower():
                return art
       
        if page >= data.get("pages", {}).get("total_pages", 1): break
        page += 1
        time.sleep(0.3)
    return None

def create_or_update_release_guide(release_task, subtasks, target_folder_id=None):
    name = release_task.get("name", "Unknown Release")
    task_id = release_task.get("id")
    new_title = f"{name} [{task_id}]"[:255]
    
    # Контент — просто список сабтасков
    content_lines = ["<ul>"]
    for sub in subtasks:
        sub_name = html.escape(sub.get("name", ""))
        sub_status = html.escape(sub.get("status", {}).get("status", "unknown"))
        content_lines.append(f"<li>{sub_name} <em>({sub_status})</em></li>")
    content_lines.append("</ul>")
    
    body_content = "\n".join(content_lines)
    new_body = f"<h1>{html.escape(name)}</h1>{body_content}"
    
    folder_id = int(target_folder_id) if target_folder_id else DEFAULT_FOLDER_ID
    
    payload = {
        "title": new_title,
        "body": new_body[:50000],
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": folder_id
    }
    
    existing = find_article_by_title(name)
    if existing:
        art_id = existing["id"]
        log.info(f"🔄 Обновляем: {new_title}")
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{art_id}", json=payload)
    else:
        log.info(f"✨ Создаём: {new_title}")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    
    if r.status_code in [200, 201]:
        log.info("✅ Успешно")
    else:
        log.error(f"❌ Ошибка {r.status_code}: {r.text}")

def main():
    target_folder = sys.argv[1] if len(sys.argv) > 1 else None
    view_id = sys.argv[2] if len(sys.argv) > 2 else "8cjzjmb-30872"  # твой view
    
    log.info(f"Запуск синхронизации ченжлога из ClickUp view: {view_id}")
    
    tasks = get_tasks_from_list(view_id)
    if not tasks:
        log.error("Задачи не найдены!")
        return
    
    # Группируем: main task → его subtasks
    releases = defaultdict(list)
    main_tasks = {}
    
    for task in tasks:
        if task.get("parent"):
            releases[task["parent"]["id"]].append(task)
        else:
            main_tasks[task["id"]] = task
    
    log.info(f"Найдено релизов: {len(main_tasks)}")
    
    for rid, rtask in main_tasks.items():
        subs = releases.get(rid, [])
        if subs:
            create_or_update_release_guide(rtask, subs, target_folder)
            time.sleep(1)  # rate limit
    
    log.info("Готово!")

if __name__ == "__main__":
    main()
