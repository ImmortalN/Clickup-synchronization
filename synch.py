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
# КОНФИГУРАЦИЯ ДЛЯ МИГРАЦИИ
# ==============================
load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "2.14"

# Папки из твоего запроса
OLD_FOLDER_ID = 2600835  # Откуда берем и принудительно обновляем
TARGET_FOLDER_ID = 4101985 # (Опционально) если нужно переместить, но пока оставим в старой

INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))

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
# ЛОГИКА ОБРАБОТКИ (Та же, что и раньше)
# ==============================
def process_image_links(text: str) -> str:
    if not text: return text
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        original = url
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

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

# ==============================
# ОСНОВНАЯ ЛОГИКА ОБНОВЛЕНИЯ
# ==============================

def get_articles_from_folder(folder_id):
    """Поиск всех статей в конкретной папке через Intercom API"""
    log.info(f"Ищем статьи в папке {folder_id}...")
    articles = []
    # Используем поиск по folder_id
    url = f"{INTERCOM_BASE}/internal_articles/search"
    payload = {"folder_id": folder_id}
    
    r = ic.get(url, params=payload)
    if r.status_code == 200:
        data = r.json()
        articles = data.get("data", [])
        log.info(f"Найдено статей для обновления: {len(articles)}")
    else:
        log.error(f"Ошибка поиска: {r.text}")
    return articles

def get_clickup_task_description(task_id):
    """Получаем свежее описание из ClickUp"""
    r = cu.get(f"https://api.clickup.com/api/v2/task/{task_id}", params={"include_markdown_description": "true"})
    if r.status_code == 200:
        data = r.json()
        return data.get("name"), data.get("markdown_description") or data.get("description") or ""
    return None, None

def force_update():
    log.info(f"Загрузка всех статей для фильтрации по папке {OLD_FOLDER_ID}...")
    
    page = 1
    total_processed = 0
    
    while True:
        # Получаем список всех внутренних статей (как в твоем рабочем скрипте)
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        
        if r.status_code != 200:
            log.error(f"Ошибка при загрузке страницы {page}: {r.text}")
            break
            
        data = r.json()
        articles = data.get("data", [])
        
        if not articles:
            break

        for art in articles:
            # ПРОВЕРКА: Если статья не в той папке, которую мы чиним — пропускаем
            if art.get("folder_id") != OLD_FOLDER_ID:
                continue

            article_id = art["id"]
            title = art["title"]
            
            # Вытаскиваем Task ID
            match = re.search(r'\[([a-zA-Z0-9]+)\]$', title)
            if not match:
                continue
                
            task_id = match.group(1)
            log.info(f"Найдена статья в папке {OLD_FOLDER_ID}: {title} (ID: {article_id})")
            
            # 1. Берем описание из ClickUp
            task_name, desc = get_clickup_task_description(task_id)
            if not task_name:
                log.warning(f"Задача {task_id} не найдена в ClickUp, пропускаем.")
                continue
                
            # 2. Формируем HTML
            header_html = f"<h1>{html.escape(task_name)}</h1>"
            main_content = markdown(
                process_image_links(desc), 
                extensions=['fenced_code', 'nl2br', 'tables']
            )
            new_body = f"{header_html}{main_content}"
            
            # 3. Принудительно обновляем
            payload = {
                "title": f"{task_name} [{task_id}]"[:255],
                "body": new_body[:50000],
                "owner_id": INTERCOM_OWNER_ID,
                "author_id": INTERCOM_AUTHOR_ID,
                "folder_id": OLD_FOLDER_ID 
            }
            
            upd = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
            if upd.status_code == 200:
                log.info(f"✅ Успешно обновлено: {task_name}")
                total_processed += 1
            else:
                log.error(f"❌ Ошибка обновления {article_id}: {upd.text}")

        # Проверка следующей страницы
        total_pages = data.get("pages", {}).get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.2) # Чтобы не спамить API

    log.info(f"Миграция завершена. Всего обновлено статей: {total_processed}")

if __name__ == "__main__":
    force_update()
