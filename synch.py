import os
import time
import json
import html
import logging
from datetime import datetime, timedelta, timezone
import requests
from markdown import markdown
from dotenv import load_dotenv

load_dotenv()

# ==== Конфигурация ====
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
SPACE_ID = os.getenv("SPACE_ID", "90153590151")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "2.11"

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "true").lower() == "true"

print("=== DEBUG ===")
print(f"CLICKUP_TOKEN: {'OK' if CLICKUP_TOKEN else 'MISSING'}")
assert CLICKUP_TOKEN and INTERCOM_TOKEN
print("✅ OK!")

logging.basicConfig(level=logging.INFO)

# ==== СЕССИИ (ИСПРАВЛЕНО!) ====
cu = requests.Session()
cu.headers.update({
    "Authorization": f"Bearer {CLICKUP_TOKEN}",  # ← ТВОЙ ФИКС!
    "Content-Type": "application/json"
})

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

# ==== ТВОЯ task_to_html() ====
def task_to_html(task):
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body_html = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    
    status = (task.get("status") or {}).get("status")
    assignees = ", ".join(a.get("username") or a.get("email") or str(a.get("id")) for a in task.get("assignees", [])) or "—"
    priority = (task.get("priority") or {}).get("priority") or (task.get("priority") or {}).get("label") or "—"
    due = task.get("due_date")
    due_str = datetime.fromtimestamp(int(due)/1000, tz=timezone.utc).strftime("%Y-%m-%d") if due else "—"
    task_url = task.get("url") or f"https://app.clickup.com/t/{task.get('id')}"
    
    meta = f"""
    <div style='border:1px solid #eee;padding:12px;border-radius:8px;margin-bottom:12px'>
    <div><strong>Статус:</strong> {html.escape(str(status or '—'))}</div>
    <div><strong>Исполнители:</strong> {html.escape(assignees)}</div>
    <div><strong>Приоритет:</strong> {html.escape(str(priority))}</div>
    <div><strong>Дедлайн:</strong> {html.escape(due_str)}</div>
    <div><strong>ClickUp:</strong> <a href="{html.escape(task_url)}" target="_blank">открыть задачу</a></div>
    </div>
    """
    return f"<h1>{html.escape(name)}</h1>" + meta + body_html

# ==== 2-ЭТАПНАЯ ЗАГРУЗКА ====
def get_full_task(task_id):
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    r = cu.get(url)
    if r.status_code == 200:
        task = r.json().get("task", {})
        desc_len = len(task.get('description', ''))
        print(f"   📄 Task {task_id}: description = {desc_len} символов")
        if desc_len > 0:
            print(f"      Первые 100: {task.get('description')[:100]}...")
        return task
    print(f"❌ Task {task_id}: {r.status_code}")
    return None

def fetch_lists(space_id):
    folders = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/folder?archived=false").json()["folders"]
    all_lists = []
    for folder in folders:
        lists = cu.get(f"https://api.clickup.com/api/v2/folder/{folder['id']}/list?archived=false").json()["lists"]
        all_lists.extend(lists)
    folderless = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/list?archived=false").json()["lists"]
    all_lists.extend(folderless)
    return all_lists

def create_article(task):
    html_body = task_to_html(task)
    title = task.get("name")
    print(f"📝 '{title}' | HTML: {len(html_body)} символов")
    
    payload = {"title": title[:255], "body": html_body, "locale": "en", "state": "published"}
    r = ic.post(f"{INTERCOM_BASE}/articles", json=payload)
    print(f"   Статус: {r.status_code}")
    
    if r.status_code in (200, 201):
        print(f"✅ СОЗДАНА!")
        return True
    print(f"❌ {r.text[:100]}")
    return False

# ==== MAIN (ТЕСТ: ПЕРВЫЕ 3 ЗАДАЧ) ====
def main():
    print("🚀 СИНХРОНИЗАЦИЯ С BEARER!")
    
    lists = fetch_lists(SPACE_ID)[:1]  # Первый список
    print(f"📋 Списоков: {len(lists)}")
    
    # Базовые задачи
    tasks_url = f"https://api.clickup.com/api/v2/list/{lists[0]['id']}/task?limit=3"
    basic_tasks = cu.get(tasks_url).json()["tasks"]
    print(f"📋 Базовых задач: {len(basic_tasks)}")
    
    # Полные задачи
    full_tasks = []
    for task in basic_tasks:
        full_task = get_full_task(task["id"])
        if full_task:
            full_tasks.append(full_task)
    
    # Создаём статьи
    count = 0
    for task in full_tasks:
        if create_article(task):
            count += 1
    
    print(f"🎉 СОЗДАНО: {count} статей!")

if __name__ == "__main__":
    main()
