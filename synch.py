#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Синхронизация ClickUp → Intercom
ЛОГИКА:
1. Получаем ВСЕ задачи из ClickUp
2. Сортируем ЛОКАЛЬНО по date_created от НОВЫХ к СТАРЫМ
3. Для каждой: если [task_id] ЕСТЬ в Intercom → СТОП
4. Если НЕТ → СОЗДАЁМ
"""

import os
import time
import html
import logging
from datetime import datetime

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
# 5. CLICKUP: все задачи + локальная сортировка по date_created (от новых к старым)
# ==============================
def fetch_clickup_tasks_sorted():
    log.info("Fetching all ClickUp tasks...")
    all_tasks = []

    # Получаем все списки
    lists = []
    folders_resp = cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/folder", params={"archived": "false"})
    while _rate_limit_sleep(folders_resp): folders_resp = cu.get(...)
    folders_resp.raise_for_status()
    for folder in folders_resp.json().get("folders", []):
        lists_resp = cu.get(f"https://api.clickup.com/api/v2/folder/{folder['id']}/list", params={"archived": "false"})
        while _rate_limit_sleep(lists_resp): lists_resp = cu.get(...)
        lists_resp.raise_for_status()
        lists.extend(lists_resp.json().get("lists", []))

    lists_resp = cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/list", params={"archived": "false"})
    while _rate_limit_sleep(lists_resp): lists_resp = cu.get(...)
    lists_resp.raise_for_status()
    lists.extend(lists_resp.json().get("lists", []))

    # Для каждого списка — получаем задачи
    for lst in lists:
        if lst["id"] in IGNORED_LIST_IDS:
            continue
        page = 0
        while True:
            tasks_resp = cu.get(f"https://api.clickup.com/api/v2/list/{lst['id']}/task", params={
                "page": page,
                "include_subtasks": "true",
                "archived": "false",
                "limit": 100,
                "include_markdown_description": "true"
            })
            while _rate_limit_sleep(tasks_resp): tasks_resp = cu.get(...)
            tasks_resp.raise_for_status()
            batch = tasks_resp.json().get("tasks", [])
            if not batch:
                break
            for t in batch:
                t["description"] = t.get("markdown_description") or t.get("description") or ""
                t["date_created"] = datetime.fromisoformat(t.get("date_created", "1970-01-01T00:00:00Z").replace('Z', '+00:00'))
                all_tasks.append(t)
            page += 1

    # === ЛОКАЛЬНАЯ СОРТИРОВКА: от новых к старым ===
    all_tasks.sort(key=lambda t: t["date_created"], reverse=True)
    log.info(f"Fetched {len(all_tasks)} tasks, sorted by creation date (newest first)")

    return all_tasks

# ==============================
# 6. INTERCOM: есть ли [task_id] в конце title?
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
    log.info("Starting sync: from newest ClickUp tasks → stop at first existing")

    created = 0
    all_tasks = list(fetch_clickup_tasks_sorted())  # Получаем все + сортируем локально

    for task in all_tasks:  # Уже отсортировано от новых к старым
        task_id = task["id"]
        title_base = task.get("name") or "(Без названия)"

        if article_exists(task_id):
            log.info(f"STOPPING: found existing guide for [{task_id}] — '{title_base}'")
            break

        if create_article(task):
            created += 1

    log.info(f"Sync complete — Created: {created} new guides")

if __name__ == "__main__":
    main()
