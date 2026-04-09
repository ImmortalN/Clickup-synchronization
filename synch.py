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
CLICKUP_LIST_ID = "ТВОЙ_ID_СПИСКА"  # Обязательно подставь ID своего списка
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "2.14")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))
INTERCOM_FOLDER_ID = 4101985

# ==============================
# ЛОГИРОВАНИЕ
# ==============================
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Сессии для запросов
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
# ОБРАБОТКА СКРИНШОТОВ
# ==============================
def process_image_links(text: str) -> str:
    if not text:
        return text

    # Очистка Markdown: [link](url) -> url
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        original = url
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"}

        # --- MONOSNAP ---
        if "monosnap.ai" in url:
            log.debug(f"Обработка Monosnap: {url}")
            match_id = re.search(r'file/([a-zA-Z0-9]+)', url)
            if match_id:
                img_id = match_id.group(1)
                api_url = f"https://api.monosnap.ai/file/download?id={img_id}"
                m_headers = headers.copy()
                m_headers["Referer"] = url
                try:
                    r = requests.get(api_url, timeout=15, headers=m_headers, allow_redirects=True)
                    if r.status_code == 200 and "api.monosnap.ai" not in r.url:
                        return f'<img src="{r.url}" style="max-width:100%;">'
                except Exception as e:
                    log.error(f"Ошибка Monosnap: {e}")
            return original

        # --- TPPR.ME (PROXY MODE) ---
        if "tppr.me/" in url:
            log.debug(f"Обработка Tppr (Proxy): {url}")
            try:
                r = requests.get(url, timeout=10, headers=headers)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    meta = soup.find('meta', property="og:image") or soup.find('meta', name="twitter:image:src")
                    if meta and meta.get('content'):
                        src = meta['content']
                        proxy_url = f"https://images.weserv.nl/?url={src.replace('https://', '')}"
                        return f'<img src="{proxy_url}" style="max-width:100%;">'
            except Exception as e:
                log.error(f"Ошибка Tppr: {e}")
            return f'<a href="{url}">{url}</a>'

        # --- SNIPBOARD ---
        if "snipboard.io/" in url:
            direct = url.replace("https://snipboard.io/", "https://i.snipboard.io/")
            if not direct.endswith(('.jpg', '.png')): direct += ".jpg"
            return f'<img src="{direct}" style="max-width:100%;">'

        # --- ICECREAM ---
        if "icecream.me/" in url and "/uploads/" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://icecream.me/uploads/{img_id}.png" style="max-width:100%;">'

        # --- IMGUR ---
        if "imgur.com/" in url and "i.imgur.com" not in url:
            try:
                r = requests.get(url, timeout=10, headers=headers)
                soup = BeautifulSoup(r.text, 'lxml')
                meta = soup.find('meta', property="og:image")
                if meta: return f'<img src="{meta["content"].split("?")[0]}" style="max-width:100%;">'
            except: pass
            return original

        # --- PRNT.SC ---
        if "prnt.sc/" in url or "prntscr.com/" in url:
            try:
                r = requests.get(url, timeout=10, headers=headers)
                soup = BeautifulSoup(r.text, 'lxml')
                img = soup.find('img', class_="no-click") or soup.find('img', id="screenshot-image")
                if img and img.get('src'):
                    src = img['src']
                    if src.startswith('//'): src = 'https:' + src
                    return f'<img src="{src}" style="max-width:100%;">'
            except: pass
            return original

        # Прямые ссылки
        if re.search(r'\.(png|jpe?g|gif|webp|bmp)', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'

        return original

    text = re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)
    return text

# ==============================
# СИНХРОНИЗАЦИЯ
# ==============================

def find_article_in_intercom(task_id):
    """Ищем статью, в заголовке которой есть [ID задачи]"""
    log.debug(f"Поиск существующей статьи для задачи {task_id}...")
    payload = {
        "query": {
            "field": "title",
            "operator": "CONTAINS",
            "value": f"[{task_id}]"
        }
    }
    try:
        r = ic.post(f"{INTERCOM_BASE}/internal_articles/search", json=payload)
        if r.status_code == 200:
            data = r.json().get('data', [])
            if data:
                log.info(f"Найдена существующая статья: {data[0]['id']}")
                return data[0]['id']
    except Exception as e:
        log.error(f"Ошибка поиска: {e}")
    return None

def sync_task(task):
    task_id = task["id"]
    name = task.get("name") or "Untitled"
    title = f"{name} [{task_id}]"[:255]
    
    desc = task.get("markdown_description") or task.get("description") or ""
    html_body = f"<h1>{html.escape(name)}</h1>\n\n"
    html_body += markdown(process_image_links(desc), extensions=['nl2br']) if desc else "<p>Нет описания</p>"

    existing_id = find_article_in_intercom(task_id)
    
    payload = {
        "title": title,
        "body": html_body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": INTERCOM_FOLDER_ID,
        "locale": "en"
    }

    if existing_id:
        log.info(f"Обновление гайда {existing_id}...")
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{existing_id}", json=payload)
    else:
        log.info(f"Создание нового гайда...")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

    if r.status_code in (200, 201):
        log.info(f"✅ Успешно синхронизировано: {task_id}")
    else:
        log.error(f"❌ Ошибка {task_id}: {r.text}")

def run_sync_limit_10():
    log.info("=== ЗАПУСК СИНХРОНИЗАЦИИ (10 ЗАДАЧ) ===")
    
    # Получаем 10 задач из списка
    url = f"https://api.clickup.com/api/v2/list/{CLICKUP_LIST_ID}/task"
    params = {"limit": 10, "include_markdown_description": "true"}
    
    r = cu.get(url, params=params)
    if r.status_code != 200:
        log.error(f"Не удалось получить задачи: {r.text}")
        return

    tasks = r.json().get("tasks", [])
    log.info(f"Найдено задач для обработки: {len(tasks)}")
    
    for task in tasks:
        sync_task(task)
    
    log.info("=== ВСЁ ГОТОВО ===")

if __name__ == "__main__":
    run_sync_limit_10()
