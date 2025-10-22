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

# ==== КОНФИГУРАЦИЯ ====
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
SPACE_ID = os.getenv("SPACE_ID", "90153590151")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "2.11"

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "true").lower() == "true"

# ==== ПРОВЕРКА ====
print("=== DEBUG ===")
print(f"CLICKUP_TOKEN: {'OK' if CLICKUP_TOKEN else 'MISSING'}")
print(f"INTERCOM_TOKEN: {'OK' if INTERCOM_TOKEN else 'MISSING'}")
assert CLICKUP_TOKEN and INTERCOM_TOKEN
print("✅ OK!")

# ==== СЕССИИ ====
cu = requests.Session()
cu.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

# ==== ТВОЯ task_to_html() ====
def task_to_html(task):
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body_html = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    if len(body_html) > 50000:
        body_html = body_html[:50000] + "<p><em>Описание урезано</em></p>"
    
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

# ==== НОВЫЕ ФУНКЦИИ ====
def get_full_task(task_id):
    """Получает ПОЛНУЮ задачу с description"""
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    r = cu.get(url)
    if r.status_code == 200:
        task = r.json().get("task", {})
        print(f"   📄 Task {task_id}: description = {len(task.get('description', ''))} символов")
        return task
    return None

def fetch_lists(space_id):
    """Получает ВСЕ списки"""
    folders = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/folder?archived=false").json().get("folders", [])
    all_lists = []
    
    # Папки
    for folder in folders:
        lists = cu.get(f"https://api.clickup.com/api/v2/folder/{folder['id']}/list?archived=false").json().get("lists", [])
        all_lists.extend(lists)
    
    # Без папки
    folderless = cu.get(f"https://api.clickup.com/api/v2/space/{space_id}/list?archived=false").json().get("lists", [])
    all_lists.extend(folderless)
    
    print(f"📋 Найдено списков: {len(all_lists)}")
    return all_lists

def create_article(task):
    html_body = task_to_html(task)
    title = task.get("name", "Без названия")
    print(f"📝 '{title}' | HTML: {len(html_body)} символов")
    
    payload = {
        "title": title[:255],
        "body": html_body,
        "locale": "en",
        "state": "published"
    }
    
    r = ic.post(f"{INTERCOM_BASE}/articles", json=payload)
    print(f"   Статус: {r.status_code}")
    
    if r.status_code in (200, 201):
        print(f"✅ СОЗДАНА!")
        return True
    print(f"❌ {r.text[:100]}")
    return False

# ==== MAIN ====
def main():
    print("🚀 СИНХРОНИЗАЦИЯ С DESCRIPTION!")
    
    # 1. Получаем списки
    lists = fetch_lists(SPACE_ID)
    
    # 2. Для каждого списка - получаем задачи (БЕЗ description)
    all_tasks = []
    for lst in lists[:2]:  # Первые 2 списка для теста
        print(f"\n📋 Список: {lst['name']}")
        tasks_url = f"https://api.clickup.com/api/v2/list/{lst['id']}/task"
        r = cu.get(tasks_url, params={"limit": 5})  # Первые 5 задач
        batch = r.json().get("tasks", [])
        print(f"   Базовых задач: {len(batch)}")
        
        # 3. Для КАЖДОЙ задачи - получаем ПОЛНУЮ с description
        for task in batch:
            full_task = get_full_task(task["id"])
            if full_task:
                all_tasks.append(full_task)
    
    print(f"\n✅ Всего полных задач: {len(all_tasks)}")
    
    # 4. Создаём статьи
    count = 0
    for task in all_tasks:
        if create_article(task):
            count += 1
    
    print(f"🎉 СОЗДАНО: {count} статей с description!")

if __name__ == "__main__":
    main()
