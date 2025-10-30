#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Синхронизация ClickUp → Intercom
ЛОГИКА:
1. Берём задачи из ClickUp от НОВЫХ к СТАРЫМ
2. Для каждой:
   - Если [task_id] ЕСТЬ в Intercom → СТОП
   - Если НЕТ → СОЗДАЁМ
3. Никаких дат, состояний, сложностей
"""

import os
import time
import html
import logging

import requests
from markdown import markdown
from dotenv import load_dotenv

# ==============================
# 1. КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "Unstable")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

SPACE_ID = "90125205902"
IGNORED_LIST_IDS = {"901212791461", "901212763746"}

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
    # Все списки
    lists = []
    for folder in cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/folder", params={"archived": "false"}).json().get("folders", []):
        lists.extend(cu.get(f"https://api.clickup.com/api/v2/folder/{folder['id']}/list", params={"archived": "false"}).json().get("lists", []))
    lists.extend(cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/list", params={"archived": "false"}).json().get("lists", []))

    for lst in lists:
        if lst["id"] in IGNORED_LIST_IDS:
            continue
        page = 0
        while True:
            r = cu.get(f"https://api.clickup.com/api/v2/list/{lst['id']}/task", params={
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
# 6. INTERCOM: есть ли [task_id]?
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

def create_article(task: dict) -> bool:
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
        return True

    log.info(f"Creating: {title}")
    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    while _rate_limit_sleep(r):
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

    if r.status_code in (200, 201):
        log.info(f"Created ID: {r.json().get('id')}")
        return True
    else:
        log.error(f"Create failed: {r.status_code} {r.text}")
        return False

# ==============================
# 8. MAIN: от новых → до первого существующего
# ==============================
def main():
    log.info("Starting sync: from newest → stop at first existing")

    created = 0
    for task in fetch_clickup_tasks():
        task_id = task["id"]
        title_base = task.get("name") or "(Без названия)"

        # === ПРОВЕРКА ===
        if article_exists(task_id):
            log.info(f"STOPPING: found existing guide for [{task_id}]")
            break

        # === СОЗДАНИЕ ===
        if create_article(task):
            created += 1

    log.info(f"Sync complete — Created: {created} new guides")

if __name__ == "__main__":
    main()
