import os
import sys
import time
import html
import logging
import re
import requests
from markdown import markdown
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from datetime import datetime

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"

DEFAULT_FOLDER_ID = 4101985
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
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

def sync_single_article(art, is_force=True):
    """Логика обновления одной конкретной статьи"""
    article_id = art["id"]
    title = art.get("title", "")
    current_folder = art.get("parent_id") or art.get("folder_id")
    
    match = re.search(r'\[([a-zA-Z0-9]+)\]$', title)
    if not match: return False
    
    task_id = match.group(1)
    task_data = get_clickup_task(task_id)

    if task_data == "DELETED":
        log.warning(f"🗑️ Задача {task_id} удалена. Чистим Intercom...")
        ic.delete(f"{INTERCOM_BASE}/internal_articles/{article_id}")
        return True

    if task_data:
        name = task_data.get("name")
        desc = task_data.get("markdown_description") or task_data.get("description") or ""
        
        new_title = f"{name} [{task_id}]"[:255]
        body_content = markdown(process_image_links(desc), extensions=['fenced_code', 'nl2br', 'tables'])
        new_body = f"<h1>{html.escape(name)}</h1>{body_content}"

        if not is_force:
            if art.get("title") == new_title and art.get("body") == new_body:
                return False

        log.info(f"🔄 Обновление: {name}")
        payload = {
            "title": new_title,
            "body": new_body[:50000],
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "folder_id": current_folder
        }
        ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
        return True
    return False

# ==============================
# ГЛАВНЫЙ ПРОЦЕСС
# ==============================

def main():
    if len(sys.argv) == 1: 
        week_number = datetime.now().isocalendar()[1]
        if week_number % 2 != 0:
            log.info("Сегодня нечетная неделя. Пропускаем автоматическую синхронизацию (раз в 2 недели).")
            return
    # Читаем аргументы: 1 - ID папки, 2 - Список ID статей
    target_folder = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else None
    specific_ids = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else None

    # ВАРИАНТ 4: Точечное обновление по ID статей
    if specific_ids:
        ids = [i.strip() for i in specific_ids.split(",")]
        log.info(f"--- РЕЖИМ ТОЧЕЧНОГО ОБНОВЛЕНИЯ: {len(ids)} шт. ---")
        for aid in ids:
            res = ic.get(f"{INTERCOM_BASE}/internal_articles/{aid}")
            if res.status_code == 200:
                sync_single_article(res.json(), is_force=True)
            else:
                log.error(f"Статья {aid} не найдена в Intercom.")
        return

    # ВАРИАНТЫ 1, 2, 3: Работа по папке
    folder_to_scan = target_folder or str(DEFAULT_FOLDER_ID)
    is_force = target_folder is not None # Если указали папку вручную, обновляем всё принудительно
    
    log.info(f"--- СТАРТ СИНХРОНИЗАЦИИ (Folder: {folder_to_scan}, Force: {is_force}) ---")

    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200: break
        
        data = r.json()
        articles = data.get("data", [])
        if not articles: break

        for art in articles:
            current_folder = str(art.get("parent_id") or art.get("folder_id") or "")
            if current_folder == folder_to_scan:
                sync_single_article(art, is_force=is_force)

        if page >= data.get("pages", {}).get("total_pages", 1): break
        page += 1
        time.sleep(0.5)

    log.info("--- СИНХРОНИЗАЦИЯ ЗАВЕРШЕНА ---")

if __name__ == "__main__":
    main()
