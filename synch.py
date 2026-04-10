import os
import time
import html
import logging
import re
import requests
from markdown import markdown
from dotenv import load_dotenv
from bs4 import BeautifulSoup

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"

# ВАЖНО: Переключаемся на Unstable, как ты и заметила
INTERCOM_VERSION = "unstable" 

OLD_FOLDER_ID = 2600835  
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

cu = requests.Session()
cu.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION, # Теперь здесь unstable
    "Content-Type": "application/json"
})

def process_image_links(text: str) -> str:
    if not text: return text
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)
    def transform_url(match):
        url = match.group(0).strip()
        original = url
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
            return original
        if re.search(r'\.(png|jpe?g|gif|webp|bmp)', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'
        return original
    return re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)

def get_clickup_task_description(task_id):
    r = cu.get(f"https://api.clickup.com/api/v2/task/{task_id}", params={"include_markdown_description": "true"})
    if r.status_code == 200:
        data = r.json()
        return data.get("name"), data.get("markdown_description") or data.get("description") or ""
    return None, None

def force_update():
    log.info(f"Начало миграции (API: {INTERCOM_VERSION}). Ищем статьи в папке {OLD_FOLDER_ID}...")
    page = 1
    total_processed = 0
    
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200:
            log.error(f"Ошибка API Intercom: {r.status_code} - {r.text}")
            break
            
        data = r.json()
        articles = data.get("data", [])
        if not articles:
            log.info("Больше статей не найдено.")
            break

        for art in articles:
            article_id = art["id"]
            title = art.get("title", "No Title")
            
            # Получаем folder_id и приводим к числу для сравнения
            raw_folder_id = art.get("parent_id") or art.get("folder_id")
            
            # Лог для диагностики (раскомментируй, если снова будет 0)
            # log.debug(f"Проверка статьи: {title}, folder_id в API: {raw_folder_id}")

            if raw_folder_id is None or int(raw_folder_id) != OLD_FOLDER_ID:
                continue

            match = re.search(r'\[([a-zA-Z0-9]+)\]$', title)
            if not match: continue
                
            task_id = match.group(1)
            log.info(f"🔄 Найдена статья для обновления: {title}")
            
            task_name, desc = get_clickup_task_description(task_id)
            if not task_name: continue
                
            header_html = f"<h1>{html.escape(task_name)}</h1>"
            main_content = markdown(process_image_links(desc), extensions=['fenced_code', 'nl2br', 'tables'])
            new_body = f"{header_html}{main_content}"
            
            payload = {
                "title": f"{task_name} [{task_id}]"[:255],
                "body": new_body[:50000],
                "owner_id": INTERCOM_OWNER_ID,
                "author_id": INTERCOM_AUTHOR_ID,
                "folder_id": OLD_FOLDER_ID 
            }
            
            upd = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
            if upd.status_code == 200:
                log.info(f"✅ Готово: {task_name}")
                total_processed += 1
            else:
                log.error(f"❌ Ошибка {article_id}: {upd.status_code}")

        if page >= data.get("pages", {}).get("total_pages", 1): 
            break
        page += 1
        time.sleep(0.3)

    log.info(f"Миграция завершена. Обновлено статей: {total_processed}")

if __name__ == "__main__":
    force_update()
