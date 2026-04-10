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
    articles = get_articles_from_folder(OLD_FOLDER_ID)
    
    for art in articles:
        article_id = art["id"]
        title = art["title"]
        
        # Вытаскиваем Task ID из заголовка [task_id]
        match = re.search(r'\[([a-zA-Z0-9]+)\]$', title)
        if not match:
            log.warning(f"Пропуск: В заголовке '{title}' не найден ID задачи ClickUp")
            continue
            
        task_id = match.group(1)
        log.info(f"Принудительное обновление статьи {article_id} (Task: {task_id})")
        
        # 1. Берем данные из ClickUp
        task_name, desc = get_clickup_task_description(task_id)
        if not task_name:
            log.error(f"Не удалось найти задачу {task_id} в ClickUp")
            continue
            
        # 2. Формируем новый HTML (с исправленными картинками и кодом)
        header_html = f"<h1>{html.escape(task_name)}</h1>"
        main_content = markdown(
            process_image_links(desc), 
            extensions=['fenced_code', 'nl2br', 'tables']
        )
        new_body = f"{header_html}{main_content}"
        
        # 3. Отправляем в Intercom (БЕЗ проверки на изменения)
        payload = {
            "title": f"{task_name} [{task_id}]"[:255],
            "body": new_body[:50000],
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "folder_id": OLD_FOLDER_ID  # Оставляем в той же папке или меняем на TARGET_FOLDER_ID
        }
        
        update_r = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
        if update_r.status_code == 200:
            log.info(f"✅ Статья '{task_name}' успешно обновлена")
        else:
            log.error(f"❌ Ошибка обновления статьи {article_id}: {update_r.text}")
        
        time.sleep(0.5) # Небольшая пауза для лимитов API

if __name__ == "__main__":
    force_update()
