#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Синхронизация ClickUp → Intercom
ЛОГИКА: от новых к старым → до первого существующего гайда → СТОП
- task_id всегда в конце: "Title [task_id]"
- Минимум запросов
- Никаких дублей
"""

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
# 1. КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "Unstable")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

SPACE_ID = "90125205902"
IGNORED_LIST_IDS = {"901212791461", "901212763746"}
SYNC_STATE_FILE = ".sync_state.json"

# ==============================
# 2. ЛОГИРОВАНИЕ
# ==============================
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

# ==============================
# 3. СЕССИИ
# ==============================
cu = requests.Session()
cu.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

# ==============================
# 4. УТИЛИТЫ
# ==============================
def _load_state() -> dict:
    if os.path.exists(SYNC_STATE_FILE):
        with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_state(state: dict):
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def _rate_limit_sleep(resp: requests.Response) -> bool:
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", "10"))
        log.warning(f"Rate limited — sleeping {wait}s")
        time.sleep(wait)
        return True
    return False

# ==============================
# 5. CLICKUP: задачи от новых к старым
# ==============================
def fetch_clickup_tasks():
    for folder in cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/folder", params={"archived": "false"}).json().get("folders", []):
        for lst in cu.get(f"https://api.clickup.com/api/v2/folder/{folder['id']}/list", params={"archived": "false"}).json().get("lists", []):
            if lst["id"] in IGNORED_LIST_IDS: continue
            yield from _fetch_tasks_from_list(lst["id"])
    for lst in cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/list", params={"archived": "false"}).json().get("lists", []):
        if lst["id"] in IGNORED_LIST_IDS: continue
        yield from _fetch_tasks_from_list(lst["id"])

def _fetch_tasks_from_list(list_id: str):
    page = 0
    while True:
        r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params={
            "page": page,
            "include_subtasks": "true",
            "archived": "false",
            "order_by": "created",
            "reverse": "true",  # ← ОТ НОВЫХ К СТАРЫМ
            "limit": 100,
            "include_markdown_description": "true"
        })
        while _rate_limit_sleep(r): r = cu.get(...)
        r.raise_for_status()
        tasks = r.json().get("tasks", [])
        if not tasks: break
        for t in tasks:
            t["description"] = t.get("markdown_description") or t.get("description") or ""
            yield t
        page += 1

# ==============================
# 6. INTERCOM: поиск по [task_id] в конце
# ==============================
def article_exists(task_id: str) -> bool:
    marker = f"[{task_id}]"
    url = f"{INTERCOM_BASE}/internal_articles"
    params = {"per_page": 100}

    while True:
        r = ic.get(url, params=params)
        while _rate_limit_sleep(r):
            time.sleep(2)
            r = ic.get(url, params=params)
        if r.status_code != 200:
            log.error(f"HTTP {r.status_code} while checking existence")
            return False

        articles = r.json().get("data", [])
        if not articles:
            return False

        for art in articles:
            if art.get("title", "").endswith(marker):
                log.info(f"FOUND existing: {art['title']} (ID: {art['id']})")
                return True

        params["starting_after"] = articles[-1]["id"]

# ==============================
# 7. HTML + СОЗДАНИЕ
# ==============================
def task_to_html(task: dict) -> str:
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""
    body = markdown(desc) if desc else "<p><em>Нет описания</em></p>"
    return f"<h1>{html.escape(name)}</h1>{body}"[:50_000]

def create_article(task: dict) -> int | None:
    task_id = task["id"]
    title = f"{task.get('name') or '(Без названия)'} [{task_id}]"
    body = task_to_html(task)

    payload = {
        "title": title,
        "body": body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "locale": "en",
    }

    if DRY_RUN:
        log.info(f"[DRY_RUN] Would create: {title}")
        return None

    log.info(f"Creating: {title}")
    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    while _rate_limit_sleep(r):
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

    if r.status_code in (200, 201):
        art_id = r.json().get("id")
        log.info(f"Created ID: {art_id}")
        return art_id
    else:
        log.error(f"Create failed: {r.status_code} {r.text}")
        return None

# ==============================
# 8. MAIN: от новых → до первого существующего
# ==============================
def main():
    log.info("Starting sync: from newest ClickUp tasks → stop at first existing guide")

    created = 0
    for task in fetch_clickup_tasks():
        task_id = task["id"]
        title_base = task.get("name") or "(Без названия)"

        # === ПРОВЕРКА: есть ли [task_id] в Intercom? ===
        if article_exists(task_id):
            log.info(f"STOPPING: found existing guide for task_id [{task_id}]")
            break

        # === СОЗДАЁМ ===
        if create_article(task):
            created += 1

    # === Обновляем время последнего синка ===
    state = _load_state()
    state["last_sync_iso"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    log.info(f"Sync complete — Created: {created} new guides")

if __name__ == "__main__":
    main()
