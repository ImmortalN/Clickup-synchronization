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
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "2.14")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))
INTERCOM_FOLDER_ID = 4101985

TEST_TASK_ID = "869cumg5k"

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
        
        # Общие заголовки для парсинга
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
        }

        # ==================== MONOSNAP ====================
        if "monosnap.ai" in url:
            log.debug(f"Обработка Monosnap: {url}")
            match_id = re.search(r'file/([a-zA-Z0-9]+)', url)
            if match_id:
                img_id = match_id.group(1)
                api_url = f"https://api.monosnap.ai/file/download?id={img_id}"
                monosnap_headers = headers.copy()
                monosnap_headers["Referer"] = url
                try:
                    r = requests.get(api_url, timeout=15, headers=monosnap_headers, allow_redirects=True)
                    if r.status_code == 200 and "api.monosnap.ai" not in r.url:
                        log.debug(f"--- УСПЕХ MONOSNAP --- Прямой URL: {r.url}")
                        return f'<img src="{r.url}" style="max-width:100%;">'
                except Exception as e:
                    log.error(f"Ошибка Monosnap: {e}")
            return original

        # ==================== TPPR.ME ====================
        if "tppr.me/" in url:
            log.debug(f"Обработка Tppr: {url}")
            try:
                r = requests.get(url, timeout=10, headers=headers)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    meta_img = soup.find('meta', property="og:image") or soup.find('meta', name="twitter:image:src")
                    if meta_img and meta_img.get('content'):
                        direct_url = meta_img['content']
                        
                        # Пропускаем через images.weserv.nl, чтобы Intercom не видел домен tppr
                        # Это превратит ссылку в нечто вроде: images.weserv.nl/?url=tppr.s3...
                        proxy_url = f"https://images.weserv.nl/?url={direct_url.replace('https://', '')}"
                        
                        log.debug(f"--- УСПЕХ TPPR (PROXY) --- {proxy_url}")
                        return f'<img src="{proxy_url}" style="max-width:100%;">'
            except Exception as e:
                log.error(f"Ошибка Tppr: {e}")
            return f'<a href="{url}">{url}</a>'

        # ==================== SNIPBOARD ====================
        if "snipboard.io/" in url:
            direct = url.replace("https://snipboard.io/", "https://i.snipboard.io/")
            if not direct.endswith(('.jpg', '.png')): direct += ".jpg"
            return f'<img src="{direct}" style="max-width:100%;">'

        # ==================== ICECREAM ====================
        if "icecream.me/" in url and "/uploads/" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://icecream.me/uploads/{img_id}.png" style="max-width:100%;">'

        # ==================== IMGUR ====================
        if "imgur.com/" in url and "i.imgur.com" not in url:
            try:
                r = requests.get(url, timeout=10, headers=headers)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    meta_img = soup.find('meta', property="og:image")
                    if meta_img:
                        src = meta_img['content'].split("?")[0]
                        return f'<img src="{src}" style="max-width:100%;">'
            except: pass
            return original

        # ==================== PRNT.SC ====================
        if "prnt.sc/" in url or "prntscr.com/" in url:
            try:
                r = requests.get(url, timeout=10, headers=headers)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    img = soup.find('img', class_="no-click") or soup.find('img', id="screenshot-image")
                    if img and img.get('src'):
                        src = img['src']
                        if src.startswith('//'): src = 'https:' + src
                        return f'<img src="{src}" style="max-width:100%;">'
            except: pass
            return original

        # Прямые ссылки (GitHub и др.)
        if "user-images.githubusercontent.com" in url or re.search(r'\.(png|jpe?g|gif|webp|bmp)', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'

        return original

    # Поиск всех URL в тексте
    text = re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)
    return text


def task_to_html(task):
    name = task.get("name") or "(Без названия)"
    # Улучшенная логика получения описания
    desc = task.get("markdown_description") or task.get("description") or ""

    if desc:
        processed = process_image_links(desc)
        body = markdown(processed, extensions=['nl2br'])
    else:
        body = "<p>Нет описания</p>"

    return f"<h1>{html.escape(name)}</h1>\n\n{body}"


def sync_article(task):
    task_id = task["id"]
    title = f"{task.get('name') or 'Untitled'} [{task_id}]"[:255]
    body = task_to_html(task)

    log.info(f"Размер body: {len(body)} символов")

    payload = {
        "title": title,
        "body": body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": INTERCOM_FOLDER_ID,
        "locale": "en"
    }

    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

    log.info(f"Статус ответа Intercom: {r.status_code}")
    if r.status_code not in (200, 201):
        log.error(f"Ошибка публикации в Intercom:\n{r.text}")
    else:
        article_id = r.json().get('id')
        log.info(f"✅ Успешно синхронизировано! ID статьи: {article_id} в папке {INTERCOM_FOLDER_ID}")


def run_test_task():
    log.info(f"=== ЗАПУСК ТЕСТА ДЛЯ ЗАДАЧИ {TEST_TASK_ID} ===")

    r = cu.get(f"https://api.clickup.com/api/v2/task/{TEST_TASK_ID}", 
               params={"include_markdown_description": "true"})

    if r.status_code != 200:
        log.error(f"Задача {TEST_TASK_ID} не найдена в ClickUp")
        return

    task = r.json()
    sync_article(task)

if __name__ == "__main__":
    run_test_task()
