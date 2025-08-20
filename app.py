# app.py
import os
import time
import hmac
import hashlib
import logging
import requests
from flask import Flask, request, jsonify, abort

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# env
CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
CLICKUP_SPACE_ID = os.getenv("CLICKUP_SPACE_ID")  # optional
CLICKUP_WEBHOOK_SECRET = os.getenv("CLICKUP_WEBHOOK_SECRET")  # set after creating webhook
INTERCOM_ACCESS_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_SOURCE_ID = os.getenv("INTERCOM_SOURCE_ID")  # optional
INTERCOM_API_BASE = os.getenv("INTERCOM_API_BASE", "https://api.intercom.io")

if not CLICKUP_API_TOKEN or not CLICKUP_TEAM_ID or not INTERCOM_ACCESS_TOKEN:
    app.logger.warning("Some required env vars are missing. Make sure CLICKUP_API_TOKEN, CLICKUP_TEAM_ID and INTERCOM_ACCESS_TOKEN are set.")

headers_clickup = {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}
headers_intercom = {
    "Authorization": f"Bearer {INTERCOM_ACCESS_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# -------------------------
# Helpers: ClickUp
# -------------------------
def fetch_task(task_id):
    """Получить полную таску по id"""
    url = f"https://api.clickup.com/api/v2/task/{task_id}"
    r = requests.get(url, headers=headers_clickup, timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_tasks_for_space(team_id, space_id=None):
    """Получить все задачи (постранично) через endpoint team/{team_id}/task.
       Если space_id указан, добавим фильтр space_ids[].
    """
    tasks = []
    page = 0
    while True:
        params = {"page": page, "subtasks": "true", "include_closed": "true"}
        if space_id:
            params["space_ids[]"] = space_id
        url = f"https://api.clickup.com/api/v2/team/{team_id}/task"
        r = requests.get(url, headers=headers_clickup, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        page_tasks = data.get("tasks", [])
        if not page_tasks:
            break
        tasks.extend(page_tasks)
        page += 1
        # небольшая пауза, чтобы быть вежливыми к API
        time.sleep(0.1)
    return tasks

def create_clickup_webhook(team_id, target_url, events=None, space_id=None):
    """Создать webhook в ClickUp (возвращает объект webhook с secret)."""
    if events is None:
        events = ["taskCreated", "taskUpdated", "taskDeleted"]
    payload = {"endpoint": target_url, "events": events}
    # ограничение: можно указать один фильтр по месту (space_id, folder_id или list_id)
    if space_id:
        payload["space_id"] = space_id
    url = f"https://api.clickup.com/api/v2/team/{team_id}/webhook"
    r = requests.post(url, headers=headers_clickup, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()

# -------------------------
# Helpers: Intercom AI content
# -------------------------
def ensure_intercom_source():
    """Если INTERCOM_SOURCE_ID не задан, создаём content import source и возвращаем id."""
    global INTERCOM_SOURCE_ID
    if INTERCOM_SOURCE_ID:
        return int(INTERCOM_SOURCE_ID)
    payload = {"sync_behavior": "api", "url": "https://app.clickup.com/"}
    r = requests.post(f"{INTERCOM_API_BASE}/ai/content_import_sources", headers=headers_intercom, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    INTERCOM_SOURCE_ID = str(data.get("id"))
    app.logger.info("Created Intercom content import source id=%s", INTERCOM_SOURCE_ID)
    return int(INTERCOM_SOURCE_ID)

def sync_task_to_intercom(task):
    """Создать или обновить External Page в Intercom по задаче ClickUp."""
    source_id = ensure_intercom_source()
    external_id = task.get("id")
    title = task.get("name") or f"Task {external_id}"
    # ClickUp может хранить текст в text_content или description
    description = task.get("text_content") or task.get("description") or ""
    # приводим в простой html
    html = f"<h1>{escape_html(title)}</h1><p>{escape_html(description)}</p>"
    url = f"https://app.clickup.com/t/{external_id}"
    payload = {
        "external_id": external_id,
        "source_id": source_id,
        "title": title,
        "html": html,
        "url": url,
        "ai_agent_availability": True,
        "ai_copilot_availability": True,
        "locale": "en"
    }
    r = requests.post(f"{INTERCOM_API_BASE}/ai/external_pages", headers=headers_intercom, json=payload, timeout=15)
    # Intercom возвращает 200/201 при успехе
    if r.status_code >= 400:
        app.logger.error("Intercom sync failed: %s %s", r.status_code, r.text)
    return r.status_code, r.json() if r.content else {}

def escape_html(s):
    if not s:
        return ""
    import html
    return html.escape(str(s)).replace("\n", "<br/>")

def find_intercom_external_page_id(source_id, external_id):
    """Ищем internal id external page по external_id+source_id (постранично)."""
    page = 1
    while True:
        r = requests.get(f"{INTERCOM_API_BASE}/ai/external_pages", headers=headers_intercom, params={"page": page, "per_page": 50}, timeout=15)
        r.raise_for_status()
        data = r.json()
        for item in data.get("data", []):
            if str(item.get("external_id")) == str(external_id) and int(item.get("source_id", 0)) == int(source_id):
                return item.get("id")
        pages = data.get("pages") or {}
        if not pages or page >= pages.get("total_pages", 0):
            break
        page += 1
    return None

def delete_intercom_external_page_by_external_id(source_id, external_id):
    pid = find_intercom_external_page_id(source_id, external_id)
    if not pid:
        app.logger.info("External page not found for external_id=%s", external_id)
        return False
    r = requests.delete(f"{INTERCOM_API_BASE}/ai/external_pages/{pid}", headers=headers_intercom, timeout=15)
    if r.status_code in (200, 204):
        app.logger.info("Deleted external page id=%s", pid)
        return True
    app.logger.error("Failed to delete external page %s: %s", pid, r.text)
    return False

# -------------------------
# ClickUp webhook signature verification
# -------------------------
def verify_clickup_signature(raw_body: bytes, secret: str) -> bool:
    if not secret:
        # если нет секрета — не можем верифицировать
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    header_sig = request.headers.get("X-Signature", "")
    return hmac.compare_digest(digest, header_sig)

# -------------------------
# Routes
# -------------------------
@app.route("/sync", methods=["GET"])
def sync_all_tasks():
    """Ручной запуск полной синхронизации задач ClickUp -> Intercom."""
    try:
        tasks = fetch_tasks_for_space(CLICKUP_TEAM_ID, CLICKUP_SPACE_ID)
    except Exception as e:
        app.logger.exception("Failed to fetch tasks: %s", e)
        return jsonify({"error": str(e)}), 500

    results = []
    for t in tasks:
        code, resp = sync_task_to_intercom(t)
        results.append({"task": t.get("name"), "id": t.get("id"), "status": code})
    return jsonify({"count": len(results), "results": results})

@app.route("/clickup/webhook", methods=["POST"])
def clickup_webhook():
    """Приём вебхуков ClickUp."""
    raw = request.get_data()
    # Проверка подписи (если секрет известен)
    if CLICKUP_WEBHOOK_SECRET:
        ok = verify_clickup_signature(raw, CLICKUP_WEBHOOK_SECRET)
        if not ok:
            app.logger.warning("Invalid ClickUp signature")
            return jsonify({"error": "invalid signature"}), 401

    data = request.json or {}
    event = data.get("event")
    # Примеры событий: taskCreated, taskUpdated, taskDeleted
    app.logger.info("ClickUp webhook event=%s", event)

    if event in ("taskCreated", "taskUpdated"):
        task_id = data.get("task_id") or (data.get("task") or {}).get("id")
        if not task_id:
            return jsonify({"error": "no task id"}), 400
        try:
            task = fetch_task(task_id)
            code, resp = sync_task_to_intercom(task)
            return jsonify({"status": code, "intercom": resp})
        except Exception as e:
            app.logger.exception("Error processing task %s: %s", task_id, e)
            return jsonify({"error": str(e)}), 500

    if event == "taskDeleted":
        task_id = data.get("task_id")
        if not task_id:
            return jsonify({"error": "no task id"}), 400
        # удаляем external page из Intercom (если есть)
        source_id = ensure_intercom_source()
        ok = delete_intercom_external_page_by_external_id(source_id, task_id)
        return jsonify({"deleted": ok})

    # игнорируем другие события
    return jsonify({"received": True}), 200

@app.route("/create_clickup_webhook", methods=["POST"])
def create_clickup_webhook_route():
    """Опционально: создать webhook в ClickUp через API (вызывается один раз)."""
    target_url = request.json.get("target_url")
    space_id = request.json.get("space_id") or CLICKUP_SPACE_ID
    if not target_url:
        return jsonify({"error": "target_url required"}), 400
    try:
        result = create_clickup_webhook(CLICKUP_TEAM_ID, target_url, space_id=space_id)
        # ClickUp возвращает secret в объекте webhook -> сохраните его в настройках хоста
        return jsonify(result)
    except Exception as e:
        app.logger.exception("Failed to create webhook: %s", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    # для локальной разработки
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
