import os
import time
import json
import html
import logging
from datetime import datetime, timedelta, timezone
import requests
from markdown import markdown
from dotenv import load_dotenv

# ==== Загрузка переменных окружения ====
load_dotenv()

# ==== Конфигурация ====
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
SPACE_ID = os.getenv("SPACE_ID")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "2.11")  # stable
LOOKBACK_HOURS = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))
CLICKUP_ONLY_OPEN = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
SYNC_STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"

IGNORED_LIST_IDS = ["901509433569", "901509402998"]

# ==== Проверка обязательных переменных ====
assert CLICKUP_TOKEN, "CLICKUP_API_TOKEN is required"
assert CLICKUP_TEAM_ID, "CLICKUP_TEAM_ID is required"
assert SPACE_ID, "SPACE_ID is required"
assert INTERCOM_TOKEN, "INTERCOM_ACCESS_TOKEN is required"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ==== Сессии ====
cu = requests.Session()
cu.headers.update({
    "Authorization": CLICKUP_TOKEN,
    "Content-Type": "application/json"
})
cu.timeout = 10

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})
ic.timeout = 10

# ==== Утилиты ====
def _load_state():
    if os.path.exists(SYNC_STATE_FILE):
        with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_state(state):
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def _rate_limit_sleep(resp):
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 10))
        logging.warning(f"Rate limited. Sleeping {retry_after}s")
        time.sleep(retry_after)
        return True
    return False

# ==== ClickUp: папки и списки ====
def fetch_folders(space_id):
    url = f"https://api.clickup.com/api/v2/space/{space_id}/folder"
    params = {"archived": "false"}
    r = cu.get(url, params=params)
    while _rate_limit_sleep(r): r = cu.get(url, params=params)
    r.raise_for_status()
    return r.json().get("folders", [])

def fetch_lists_from_folder(folder_id):
    url = f"https://api.clickup.com/api/v2/folder/{folder_id}/list"
    params = {"archived": "false"}
    r = cu.get(url, params=params)
    while _rate_limit_sleep(r): r = cu.get(url, params=params)
    r.raise_for_status()
    return r.json().get("lists", [])

def fetch_folderless_lists(space_id):
    url = f"https://api.clickup.com/api/v2/space/{space_id}/list"
    params = {"archived": "false"}
    r = cu.get(url, params=params)
    while _rate_limit_sleep(r): r = cu.get(url, params=params)
    r.raise_for_status()
    return r.json().get("lists", [])

# ==== ClickUp: задачи ====
def fetch_tasks_from_list(list_id, updated_after):
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    page = 0
    updated_gt = int(updated_after.timestamp() * 1000) if updated_after else None

    while True:
        params = {
            "page": page,
            "limit": 100,
            "archived": "false",
            "include_subtasks": "true",
            "subtasks": "true",
            "order_by": "updated",
            "reverse": "true"
        }
        if updated_gt:
            params["updated_gt"] = updated_gt
        if CLICKUP_ONLY_OPEN:
            params["statuses[]"] = ["to do", "in progress"]

        r = cu.get(url, params=params)
        while _rate_limit_sleep(r): r = cu.get(url, params=params)
        r.raise_for_status()

        tasks = r.json().get("tasks", [])
        if not tasks:
            break

        for t in tasks:
            yield t
        page += 1

# ==== ClickUp: получение полного task с description ====
def get_full_task(task_id):
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    r = cu.get(url)
    while _rate_limit_sleep(r): r = cu.get(url)
    r.raise_for_status()
    task = r.json().get("task", {})
    return task

# ==== Intercom: преобразование задачи в HTML ====
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
    <div><strong>ClickUp:</strong> <a href="{html.escape(task_url)}" target="_blank" rel="noopener">открыть задачу</a></div>
    </div>
    """
    title_html = f"<h1>{html.escape(name)}</h1>"
    return title_html + meta + body_html

# ==== Intercom: создание статьи ====
def create_article(task):
    title = task.get("name") or "(Без названия)"
    html_body = task_to_html(task)
    logging.info(f"📝 Создаём статью: {title}")

    payload = {
        "title": title[:255],
        "body": html_body,
        "locale": "en",
        "state": "published"
    }

    if DRY_RUN:
        logging.info(f"[DRY_RUN] Не создаём статью")
        return None

    r = ic.post(f"{INTERCOM_BASE}/articles", json=payload)
    logging.info(f"Статус: {r.status_code}")
    if r.status_code in (200, 201):
        return r.json().get("id")
    else:
        logging.error(f"❌ Ошибка: {r.status_code} {r.text[:200]}")
        return None

# ==== Главный процесс ====
def fetch_clickup_tasks(updated_after, space_id):
    total = 0
    # Папки
    for folder in fetch_folders(space_id):
        folder_id = folder.get("id")
        lists = fetch_lists_from_folder(folder_id)
        for lst in lists:
            list_id = lst.get("id")
            if list_id in IGNORED_LIST_IDS:
                continue
            for t in fetch_tasks_from_list(list_id, updated_after):
                yield get_full_task(t.get("id"))
                total += 1

    # Списки без папки
    for lst in fetch_folderless_lists(space_id):
        list_id = lst.get("id")
        if list_id in IGNORED_LIST_IDS:
            continue
        for t in fetch_tasks_from_list(list_id, updated_after):
            yield get_full_task(t.get("id"))
            total += 1
    logging.info(f"✅ Всего задач: {total}")

def main():
    state = _load_state()
    last_sync_iso = state.get("last_sync_iso")
    if last_sync_iso and not FETCH_ALL:
        updated_after = datetime.fromisoformat(last_sync_iso)
    else:
        updated_after = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    logging.info(f"🚀 НАЧИНАЕМ СИНХРОНИЗАЦИЮ задач ClickUp")
    count = 0
    try:
        for task in fetch_clickup_tasks(updated_after, SPACE_ID):
            try:
                article_id = create_article(task)
                if article_id:
                    count += 1
                time.sleep(0.1)  # rate limit
            except Exception as e:
                logging.exception(f"Failed task {task.get('id')}: {e}")
                continue
    except Exception as e:
        logging.exception(f"Ошибка при fetch_clickup_tasks: {e}")

    state["last_sync_iso"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)
    logging.info(f"🎉 СИНХРОНИЗАЦИЯ ЗАВЕРШЕНА! Создано статей: {count}")

if __name__ == "__main__":
    main()
