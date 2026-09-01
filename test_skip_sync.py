"""
Тестовый скрипт: sync только по списку ClickUp task IDs с skip актуальных.
Не трогает полный обход Space.

Запуск через GitHub Actions (script=sync_smart) или локально:
  python test_skip_sync.py "" "" "taskid1,taskid2,taskid3"

FORCE_UPDATE=true — обновить даже если актуально.
"""
import os
import sys
import time
import html
import logging
import re
import requests
from markdown import markdown
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"
DEFAULT_FOLDER_ID = 4101985
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("test_skip_sync")

cu = requests.Session()
cu.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})
ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json",
})


def parse_timestamp(value):
    if value is None:
        return 0.0
    try:
        num = float(value)
        return num / 1000.0 if num > 1e12 else num
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return 0.0


def format_ts(ts):
    if not ts:
        return "нет"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return f"invalid({ts})"


def get_clickup_task(task_id):
    try:
        r = cu.get(
            f"https://api.clickup.com/api/v2/task/{task_id}",
            params={"include_markdown_description": "true"},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return "DELETED"
        log.warning(f"ClickUp API {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"get_clickup_task error: {e}")
    return None


def find_article_by_task_id(task_id):
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200:
            break
        data = r.json()
        articles = data.get("data", [])
        if not articles:
            break
        for art in articles:
            if f"[{task_id}]" in art.get("title", ""):
                return art
        if page >= data.get("pages", {}).get("total_pages", 1):
            break
        page += 1
        time.sleep(0.3)
    return None


def create_or_update_smart(task_id, target_folder_id=None, force=False):
    task_data = get_clickup_task(task_id)
    if not task_data or task_data == "DELETED":
        log.error(f"❌ Task {task_id} not found")
        return "error"

    name = task_data.get("name") or ""
    desc = task_data.get("markdown_description") or task_data.get("description") or ""
    new_title = f"{name} [{task_id}]"[:255]
    # без process_image_links — для теста skip/update достаточно plain markdown
    body_content = markdown(desc, extensions=["fenced_code", "nl2br", "tables"])
    new_body = f"<h1>{html.escape(name)}</h1>{body_content}"
    folder_id = (
        int(target_folder_id)
        if target_folder_id and str(target_folder_id).isdigit()
        else DEFAULT_FOLDER_ID
    )

    payload = {
        "title": new_title,
        "body": new_body[:100000],
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": folder_id,
    }

    existing = find_article_by_task_id(task_id)

    if not existing:
        log.info(f"✨ CREATE: {new_title}")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload, timeout=30)
        if r.status_code in (200, 201):
            log.info(f"✅ created id={r.json().get('id')}")
            return "created"
        log.error(f"❌ create {r.status_code}: {r.text[:300]}")
        return "error"

    clickup_ts = parse_timestamp(task_data.get("date_updated"))
    intercom_ts = parse_timestamp(existing.get("updated_at"))

    if not force and clickup_ts <= intercom_ts + 10:
        log.info(
            f"⏭ SKIP (up-to-date): {name} | CU {format_ts(clickup_ts)} ≤ IC {format_ts(intercom_ts)}"
        )
        return "skipped"

    reason = "force" if force else f"CU newer ({format_ts(clickup_ts)} > {format_ts(intercom_ts)})"
    log.info(f"🔄 UPDATE {existing['id']}: {new_title} | {reason}")
    r = ic.put(f"{INTERCOM_BASE}/internal_articles/{existing['id']}", json=payload, timeout=30)
    if r.status_code in (200, 201):
        log.info("✅ updated")
        return "updated"
    log.error(f"❌ update {r.status_code}: {r.text[:300]}")
    return "error"


def main():
    target_folder = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else None
    # argv[2] ignored (article_ids) for compatibility with workflow
    clickup_task_ids_raw = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3].strip() else None

    if not clickup_task_ids_raw:
        log.error("Нужны ClickUp task IDs в 3-м аргументе: python test_skip_sync.py '' '' 'id1,id2'")
        sys.exit(1)

    task_ids = [t.strip() for t in clickup_task_ids_raw.split(",") if t.strip()]
    force = os.getenv("FORCE_UPDATE", "").lower() in ("true", "1", "yes")
    log.info(f"=== test_skip_sync | tasks={len(task_ids)} force={force} ===")
    stats = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    for tid in task_ids:
        log.info(f"\n--- {tid} ---")
        try:
            result = create_or_update_smart(tid, target_folder, force=force)
            stats[result if result in stats else "error"] += 1
        except Exception as e:
            stats["error"] += 1
            log.error(f"exception: {e}")
        time.sleep(0.3)

    log.info(
        f"=== DONE | created={stats['created']} updated={stats['updated']} "
        f"skipped={stats['skipped']} error={stats['error']} ==="
    )


if __name__ == "__main__":
    main()
