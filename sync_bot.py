import os
import sys
import time
import html
import logging
import re
import json
import requests
from markdown import markdown
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from bs4 import BeautifulSoup

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"

# Значения по умолчанию
DEFAULT_FOLDER_ID = 4101985
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))
SYNC_STATE_FILE = ".sync_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Сессии
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
# ЛОГИКА ОБРАБОТКИ
# ==============================

def process_image_links(text: str) -> str:
    if not text: return text
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        if "snipboard.io" in url and "i.snipboard.io" not in url:
            img_id = url.split('/')[-1]
            if img_id: return f'<img src="https://i.snipboard.io/{img_id}.jpg" style="max-width:100%;">'

        if re.search(r'\.(png|jpe?g|gif|webp|bmp)(\?.*)?$', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'

        if "monosnap.ai" in url or "take.ms" in url:
            current_url = url
            if "take.ms" in url:
                try:
                    r_head = requests.head(url, timeout=5, allow_redirects=True)
                    current_url = r_head.url
                except: pass
            match_id = re.search(r'/(?:file|direct)/([a-zA-Z0-9]+)', current_url)
            if match_id:
                img_id = match_id.group(1)
                api_url = f"https://api.monosnap.ai/file/download?id={img_id}"
                try:
                    r = requests.get(api_url, timeout=10, headers={"Referer": current_url}, allow_redirects=True)
                    if r.status_code == 200 and "api.monosnap.ai" not in r.url:
                        return f'<img src="{r.url}" style="max-width:100%;">'
                except: pass
        return url

    return re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)

def get_clickup_task(task_id):
    try:
        r = cu.get(f"https://api.clickup.com/api/v2/task/{task_id}", params={"include_markdown_description": "true"})
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 404:
            return "DELETED"
    except: pass
    return None

def main():
    # Проверяем аргумент папки при запуске
    target_folder = str(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else str(DEFAULT_FOLDER_ID)
    is_force_mode = len(sys.argv) > 1
    
    log.info(f"--- СТАРТ СИНХРОНИЗАЦИИ (Folder: {target_folder}, Force: {is_force_mode}) ---")

    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200: break
        
        data = r.json()
        articles = data.get("data", [])
        if not articles: break

        for art in articles:
            article_id = art["id"]
            title = art.get("title", "")
            current_folder = str(art.get("parent_id") or art.get("folder_id") or "")

            if current_folder != target_folder:
                continue

            match = re.search(r'\[([a-zA-Z0-9]+)\]$', title)
            if not match: continue
            
            task_id = match.group(1)
            task_data = get_clickup_task(task_id)

            if task_data == "DELETED":
                log.warning(f"🗑️ Задача {task_id} удалена. Чистим Intercom...")
                ic.delete(f"{INTERCOM_BASE}/internal_articles/{article_id}")
                continue

            if task_data:
                name = task_data.get("name")
                desc = task_data.get("markdown_description") or task_data.get("description") or ""
                
                new_title = f"{name} [{task_id}]"[:255]
                body_content = markdown(process_image_links(desc), extensions=['fenced_code', 'nl2br', 'tables'])
                new_body = f"<h1>{html.escape(name)}</h1>{body_content}"

                # Если не форсированный режим, проверяем изменения
                if not is_force_mode:
                    if art.get("title") == new_title and art.get("body") == new_body:
                        continue

                log.info(f"🔄 Обновление: {name}")
                payload = {
                    "title": new_title,
                    "body": new_body[:50000],
                    "owner_id": INTERCOM_OWNER_ID,
                    "author_id": INTERCOM_AUTHOR_ID,
                    "folder_id": int(target_folder)
                }
                ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)

        if page >= data.get("pages", {}).get("total_pages", 1): break
        page += 1
        time.sleep(0.5)

    log.info("--- СИНХРОНИЗАЦИЯ ЗАВЕРШЕНА ---")

if __name__ == "__main__":
    main()
