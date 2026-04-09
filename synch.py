import os
import html
import logging
import re
import requests
from markdown import markdown
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_LIST_ID = "ТВОЙ_LIST_ID"  # Укажи ID списка, из которого берем 10 задач
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))
INTERCOM_FOLDER_ID = 4101985

# ==============================
# ЛОГИРОВАНИЕ
# ==============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Сессии
cu = requests.Session()
cu.headers.update({"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"})

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": "2.14",
    "Content-Type": "application/json"
})

# ==============================
# ОБРАБОТКА СКРИНШОТОВ
# ==============================
def process_image_links(text: str) -> str:
    if not text: return text
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        # MONOSNAP
        if "monosnap.ai" in url:
            match_id = re.search(r'file/([a-zA-Z0-9]+)', url)
            if match_id:
                api_url = f"https://api.monosnap.ai/file/download?id={match_id.group(1)}"
                try:
                    r = requests.get(api_url, timeout=10, headers=headers, allow_redirects=True)
                    if r.status_code == 200 and "api.monosnap.ai" not in r.url:
                        return f'<img src="{r.url}" style="max-width:100%;">'
                except: pass
        
        # TPPR.ME (через прокси weserv)
        if "tppr.me/" in url:
            try:
                r = requests.get(url, timeout=10, headers=headers)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    meta = soup.find('meta', property="og:image") or soup.find('meta', name="twitter:image:src")
                    if meta and meta.get('content'):
                        direct = meta['content']
                        proxy_url = f"https://images.weserv.nl/?url={direct.replace('https://', '')}"
                        return f'<img src="{proxy_url}" style="max-width:100%;">'
            except: pass

        # Остальные (Imgur, Prntsc и прямые ссылки)
        if any(x in url for x in ["imgur.com", "prnt.sc", "prntscr.com"]):
            try:
                r = requests.get(url, timeout=10, headers=headers)
                soup = BeautifulSoup(r.text, 'lxml')
                img = soup.find('meta', property="og:image") or soup.find('img', class_="no-click")
                src = img.get('content') if img.get('content') else img.get('src')
                if src: return f'<img src="{src}" style="max-width:100%;">'
            except: pass

        if re.search(r'\.(png|jpe?g|gif|webp)', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'
        
        return f'<a href="{url}">{url}</a>'

    return re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)

# ==============================
# ЛОГИКА СИНХРОНИЗАЦИИ
# ==============================

def find_existing_article(task_id):
    """Ищет статью в Intercom, у которой в заголовке есть [ID задачи]"""
    search_query = f"[{task_id}]"
    payload = {
        "query": {
            "field": "title",
            "operator": "CONTAINS",
            "value": search_query
        }
    }
    r = ic.post(f"{INTERCOM_BASE}/internal_articles/search", json=payload)
    if r.status_code == 200:
        data = r.json()
        if data.get('data'):
            return data['data'][0]['id'] # Возвращаем ID первой найденной статьи
    return None

def sync_task_to_intercom(task):
    task_id = task["id"]
    task_name = task.get("name", "Untitled")
    title = f"{task_name} [{task_id}]"
    
    # Контент
    desc = task.get("markdown_description") or task.get("description") or ""
    body = f"<h1>{html.escape(task_name)}</h1>\n\n{markdown(process_image_links(desc), extensions=['nl2br'])}"
    
    existing_id = find_existing_article(task_id)
    
    payload = {
        "title": title,
        "body": body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": INTERCOM_FOLDER_ID
    }

    if existing_id:
        # ОБНОВЛЕНИЕ
        log.info(f"Обновление существующего гайда {existing_id} для задачи {task_id}")
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{existing_id}", json=payload)
    else:
        # СОЗДАНИЕ
        log.info(f"Создание нового гайда для задачи {task_id}")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

    if r.status_code in (200, 201):
        log.info(f"✅ Успех для {task_id}")
    else:
        log.error(f"❌ Ошибка {task_id}: {r.text}")

def run_sync():
    log.info("=== ЗАПУСК СИНХРОНИЗАЦИИ (ЛИМИТ 10) ===")
    # Получаем список из 10 последних задач
    r = cu.get(f"https://api.clickup.com/api/v2/list/{CLICKUP_LIST_ID}/task", 
               params={"limit": 10, "include_markdown_description": "true"})
    
    if r.status_code != 200:
        log.error(f"Не удалось получить задачи из ClickUp: {r.text}")
        return

    tasks = r.json().get("tasks", [])
    for task in tasks:
        sync_task_to_intercom(task)
    log.info("=== СИНХРОНИЗАЦИЯ ЗАВЕРШЕНА ===")

if __name__ == "__main__":
    run_sync()
