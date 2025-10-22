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
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
SPACE_ID = os.getenv("SPACE_ID", "90153590151")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", "5475435"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", "5475435"))
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = "2.11"  # Stable Articles

LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))
CLICKUP_ONLY_OPEN = os.getenv("CLICKUP_ONLY_OPEN", "false").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "true").lower() == "true"
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")

IGNORED_LIST_IDS = ["901509433569", "901509402998"]

# ==== ПРОВЕРКА ====
print("=== DEBUG: Проверка переменных ===")
print(f"CLICKUP_TOKEN: {'OK' if CLICKUP_TOKEN else 'MISSING'}")
print(f"INTERCOM_TOKEN: {'OK' if INTERCOM_TOKEN else 'MISSING'}")
assert CLICKUP_TOKEN and INTERCOM_TOKEN
print("✅ Все переменные OK!")

logging.basicConfig(level=logging.INFO)

# ==== СЕССИИ ====
cu = requests.Session()
cu.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})
cu.timeout = 30

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})
ic.timeout = 30

# ==== УТИЛИТЫ (из твоего кода) ====
def _rate_limit_sleep(resp):
    if resp.status_code == 429:
        time.sleep(int(resp.headers.get("Retry-After", 10)))
        return True
    return False

def _load_state():
    try:
        if os.path.exists(SYNC_STATE_FILE):
            with open(SYNC_STATE_FILE, "r") as f:
                return json.load(f)
    except: pass
    return {}

def _save_state(state):
    with open(SYNC_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ==== CLICKUP ФУНКЦИИ (из твоего кода) ====
def fetch_folders(space_id):
    url = f"https://api.clickup.com/api/v2/space/{space_id}/folder?archived=false"
    r = cu.get(url)
    while _rate_limit_sleep(r): r = cu.get(url)
    r.raise_for_status()
    return r.json().get("folders", [])

def fetch_lists_from_folder(folder_id):
    url = f"https://api.clickup.com/api/v2/folder/{folder_id}/list?archived=false"
    r = cu.get(url)
    while _rate_limit_sleep(r): r = cu.get(url)
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_folderless_lists(space_id):
    url = f"https://api.clickup.com/api/v2/space/{space_id}/list?archived=false"
    r = cu.get(url)
    while _rate_limit_sleep(r): r = cu.get(url)
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_tasks_from_list(list_id, updated_after):
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    page = 0
    while True:
        params = {"page": page, "limit": 100, "include_subtasks": "true", "archived": "false"}
        r = cu.get(url, params=params)
        while _rate_limit_sleep(r): r = cu.get(url, params=params)
        r.raise_for_status()
        tasks = r.json().get("tasks", [])
        if not tasks: break
        for task in tasks: yield task
        page += 1

def fetch_clickup_tasks(updated_after, space_id):
    total = 0
    folders = fetch_folders(space_id)
    for folder in folders:
        lists = fetch_lists_from_folder(folder["id"])
        for lst in lists:
            if lst["id"] in IGNORED_LIST_IDS: continue
            for task in fetch_tasks_from_list(lst["id"], updated_after):
                total += 1
                yield task
    folderless = fetch_folderless_lists(space_id)
    for lst in folderless:
        if lst["id"] in IGNORED_LIST_IDS: continue
        for task in fetch_tasks_from_list(lst["id"], updated_after):
            total += 1
            yield task
    print(f"✅ Всего задач: {total}")

# ==== ТВОЯ ИДЕАЛЬНАЯ task_to_html() ====
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body_html = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    if len(body_html) > 50000:
        body_html = body_html[:50000] + "<p><em>Описание урезано из-за длины</em></p>"
    
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
    <div><strong>ClickUp:</strong> <a href="{html.escape(task_url)}" target="_blank" rel="noopener">открыть задачу</a></div>
    </div>
    """
    title_html = f"<h1>{html.escape(name)}</h1>"
    return title_html + meta + body_html

# ==== INTERCOM ARTICLES (stable) ====
def create_article(task):
    title = task.get("name", "Без названия")
    html_body = task_to_html(task)
    print(f"📝 '{title}' | Описание: {len(task.get('description', ''))} символов")
    
    payload = {
        "title": title[:255],
        "body": html_body,
        "locale": "en",
        "state": "published"
    }
    
    r = ic.post(f"{INTERCOM_BASE}/articles", json=payload)
    print(f"   Статус: {r.status_code}")
    
    if r.status_code in (200, 201):
        print(f"✅ Создана!")
        return r.json().get("id")
    else:
        print(f"❌ {r.text[:100]}")
        return None

# ==== MAIN ====
def main():
    print("🚀 СИНХРОНИЗАЦИЯ!")
    test_clickup_token()  # Твой тест
    
    tasks = list(fetch_clickup_tasks(None, SPACE_ID))
    count = 0
    
    # ТЕСТ: ПЕРВЫЕ 5 ЗАДАЧ
    for task in tasks[:5]:
        if create_article(task):
            count += 1
    
    print(f"🎉 Создано: {count}/5 статей")
    _save_state({"last_sync_iso": datetime.now(timezone.utc).isoformat()})

def test_clickup_token():
    r = cu.get("https://api.clickup.com/api/v2/team")
    print(f"🔍 ClickUp: {r.status_code} | Команд: {len(r.json().get('teams', []))}")
    assert r.status_code == 200

if __name__ == "__main__":
    main()
