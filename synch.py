import os
import time
import json
import html
import logging
from datetime import datetime, timedelta, timezone

import requests
from markdown import markdown
from dotenv import load_dotenv

# ==============================
# КОНФИГУРАЦИЯ (берётся из .env)
# ==============================
load_dotenv()

CLICKUP_TOKEN           = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID         = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_ONLY_OPEN       = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
LOOKBACK_HOURS          = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))

INTERCOM_TOKEN          = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE           = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION        = os.getenv("INTERCOM_VERSION", "2.14")
INTERCOM_OWNER_ID       = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID      = int(os.getenv("INTERCOM_AUTHOR_ID"))

DRY_RUN                 = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL               = os.getenv("FETCH_ALL", "false").lower() == "true"
DEBUG_SEARCH            = os.getenv("DEBUG_SEARCH", "false").lower() == "true"

SPACE_ID                = "90125205902"
IGNORED_LIST_IDS        = {"901212791461", "901212763746"}
SYNC_STATE_FILE         = ".sync_state.json"

# Ограничение для теста (0 = без ограничения)
MAX_TASKS_FOR_TEST      = int(os.getenv("MAX_TASKS_FOR_TEST", 0))

# ==============================
# ПРОВЕРКА ОБЯЗАТЕЛЬНЫХ ПЕРЕМЕННЫХ
# ==============================
required = ["CLICKUP_API_TOKEN", "CLICKUP_TEAM_ID", "INTERCOM_ACCESS_TOKEN", "INTERCOM_OWNER_ID", "INTERCOM_AUTHOR_ID"]
missing = [v for v in required if not os.getenv(v)]
if missing:
    print(f"ERROR: Отсутствуют переменные: {', '.join(missing)}")
    exit(1)

# ==============================
# ЛОГИРОВАНИЕ
# ==============================
logging.basicConfig(
    level=logging.DEBUG if DEBUG_SEARCH else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

# ==============================
# СЕССИИ
# ==============================
cu = requests.Session()
cu.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})
cu.timeout = 15

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})
ic.timeout = 15

# ==============================
# УТИЛИТЫ
# ==============================
def load_state():
    if os.path.exists(SYNC_STATE_FILE):
        with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def rate_limit_sleep(resp):
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", 10))
        log.warning(f"Rate limit → sleep {wait}s")
        time.sleep(wait)
        return True
    return False

# ==============================
# CLICKUP — получение задач
# ==============================
def check_team_access():
    r = cu.get(f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}")
    while rate_limit_sleep(r): r = cu.get(f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}")
    r.raise_for_status()
    log.info(f"Team OK: {r.json()['team']['name']}")

def get_folders():
    r = cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/folder", params={"archived": "false"})
    while rate_limit_sleep(r): r = cu.get(...)
    r.raise_for_status()
    return r.json().get("folders", [])

def get_lists_in_folder(folder_id):
    r = cu.get(f"https://api.clickup.com/api/v2/folder/{folder_id}/list", params={"archived": "false"})
    while rate_limit_sleep(r): r = cu.get(...)
    r.raise_for_status()
    return r.json().get("lists", [])

def get_folderless_lists():
    r = cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/list", params={"archived": "false"})
    while rate_limit_sleep(r): r = cu.get(...)
    r.raise_for_status()
    return r.json().get("lists", [])

def get_tasks_from_list(list_id, updated_after):
    page = 0
    updated_gt = int(updated_after.timestamp() * 1000) if not FETCH_ALL else None
    while True:
        params = {
            "page": page,
            "include_subtasks": "true",
            "archived": "false",
            "order_by": "updated",
            "reverse": "true",
            "limit": 100,
            "include_markdown_description": "true"
        }
        if updated_gt:
            params["updated_gt"] = updated_gt
        if CLICKUP_ONLY_OPEN:
            params["statuses[]"] = ["to do", "in progress"]

        r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params=params)
        while rate_limit_sleep(r): r = cu.get(...)
        r.raise_for_status()

        tasks = r.json().get("tasks", [])
        if not tasks:
            break

        for t in tasks:
            t["description"] = t.get("markdown_description") or t.get("description") or ""
            yield t

        page += 1

def fetch_clickup_tasks(updated_after):
    count = 0
    for folder in get_folders():
        for lst in get_lists_in_folder(folder["id"]):
            if lst["id"] in IGNORED_LIST_IDS: continue
            for task in get_tasks_from_list(lst["id"], updated_after):
                if MAX_TASKS_FOR_TEST > 0 and count >= MAX_TASKS_FOR_TEST:
                    log.info(f"Ограничение теста: {MAX_TASKS_FOR_TEST} задач — остановка")
                    return
                yield task
                count += 1

    for lst in get_folderless_lists():
        if lst["id"] in IGNORED_LIST_IDS: continue
        for task in get_tasks_from_list(lst["id"], updated_after):
            if MAX_TASKS_FOR_TEST > 0 and count >= MAX_TASKS_FOR_TEST:
                log.info(f"Ограничение теста: {MAX_TASKS_FOR_TEST} задач — остановка")
                return
            yield task
            count += 1

# ==============================
# Преобразование задачи в HTML
# ==============================
def task_to_html(task):
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    if len(body) > 50000:
        body = body[:50000] + "<p><em>Описание урезано</em></p>"
    return f"<h1>{html.escape(name)}</h1>{body}"

# ==============================
# Загрузка всех статей Intercom
# ==============================
def load_all_articles():
    log.info("Загрузка всех Internal Articles из Intercom (page-based)")
    task_id_to_id = {}
    url = f"{INTERCOM_BASE}/internal_articles"
    per_page = 100
    page = 1

    while True:
        params = {"page": page, "per_page": per_page}
        r = ic.get(url, params=params)
        while rate_limit_sleep(r):
            r = ic.get(url, params=params)

        if r.status_code != 200:
            log.error(f"Ошибка {r.status_code} на странице {page}: {r.text}")
            break

        data = r.json()
        articles = data.get("data", [])
        log.info(f"Страница {page}: {len(articles)} статей")

        for art in articles:
            title = art.get("title", "")
            if "[" in title and "]" in title:
                start = title.rfind("[")
                end = title.rfind("]")
                if start < end:
                    task_id = title[start+1:end].strip()
                    if task_id:
                        task_id_to_id[task_id] = art["id"]

        pages = data.get("pages", {})
        total_pages = pages.get("total_pages", 1)

        if page >= total_pages or not articles:
            log.info(f"Конец пагинации: страница {page} из {total_pages}")
            break

        page += 1
        time.sleep(1.2)

    log.info(f"Всего загружено статей с task_id: {len(task_id_to_id)} (всего получено: {data.get('total_count', 'неизвестно')})")
    return task_id_to_id

# ==============================
# Синхронизация одной статьи
# ==============================
def sync_article(task, article_map):
    task_id = task["id"]
    title = f"{task.get('name', '(Без названия)')} [{task_id}]"[:255]
    body = task_to_html(task)[:50000]

    payload = {
        "title": title,
        "body": body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "locale": "en",
    }

    if task_id in article_map:
        article_id = article_map[task_id]

        # Проверяем, изменилось ли содержимое
        r = ic.get(f"{INTERCOM_BASE}/internal_articles/{article_id}")
        if r.status_code == 200:
            curr = r.json()
            if curr.get("title") == title and curr.get("body") == body:
                log.info(f"Пропуск (без изменений): {title} (ID {article_id})")
                return article_id

        log.info(f"Обновление: {title} (ID {article_id})")
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
        while rate_limit_sleep(r):
            r = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)

        if r.status_code in (200, 201):
            log.info(f"Обновлено успешно: {title}")
            return article_id
        else:
            log.error(f"Ошибка обновления: {r.status_code} {r.text}")
            return None

    else:
        log.info(f"Создание: {title}")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
        while rate_limit_sleep(r):
            r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

        if r.status_code in (200, 201):
            new_id = r.json().get("id")
            log.info(f"Создано успешно: {title} (ID {new_id})")
            article_map[task_id] = new_id
            return new_id
        else:
            log.error(f"Ошибка создания: {r.status_code} {r.text}")
            return None

# ==============================
# ОСНОВНАЯ ФУНКЦИЯ
# ==============================
def main():
    article_map = load_all_articles()

    state = load_state()
    last_iso = state.get("last_sync_iso")
    since = datetime.fromisoformat(last_iso) if last_iso and not FETCH_ALL else datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    log.info(f"Синхронизация задач, обновлённых после {since.isoformat()}")

    check_team_access()

    created = updated = skipped = 0
    for task in fetch_clickup_tasks(since):
        result = sync_article(task, article_map)
        if result:
            if task["id"] in article_map and result == article_map[task["id"]]:
                updated += 1
            else:
                created += 1
        else:
            skipped += 1

    now_iso = datetime.now(timezone.utc).isoformat()
    state["last_sync_iso"] = now_iso
    save_state(state)

    log.info(f"Синхронизация завершена — Создано: {created}, Обновлено: {updated}, Пропущено: {skipped}, Последняя: {now_iso}")

if __name__ == "__main__":
    main()
