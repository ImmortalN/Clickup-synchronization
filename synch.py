import os
import html
import logging
import re
import requests
from markdown import markdown
from dotenv import load_dotenv
from bs4 import BeautifulSoup  # Нужна установка: pip install beautifulsoup4 lxml

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

TEST_TASK_ID = "869cumg5k"

# ==============================
# ЛОГИРОВАНИЕ
# ==============================
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s: %(message)s")
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
# ОБРАБОТКА СКРИНШОТОВ
# ==============================
def process_image_links(text: str) -> str:
    if not text:
        return text

    # Убираем Markdown обертку [link](url) -> url
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        original = url

        # ==================== MONOSNAP ====================
        if "monosnap.ai" in url:
            log.debug(f"Обработка Monosnap: {url}")
            match = re.search(r'file/([a-zA-Z0-9]+)', url)
            if match:
                img_id = match.group(1)
                api_url = f"https://api.monosnap.ai/file/download?id={img_id}"
                
                # Локальные заголовки, чтобы избежать ошибки 'not defined'
                local_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
                    "Referer": url
                }
                
                try:
                    # Делаем запрос к API, чтобы получить финальный URL файла после редиректа
                    # Monosnap перенаправит нас на store.monosnap.com или s3
                    r = requests.get(api_url, timeout=15, headers=local_headers, allow_redirects=True)
                    
                    if r.status_code == 200:
                        direct = r.url
                        # Если мы ушли с домена api.monosnap.ai — значит, получили прямую ссылку на файл
                        if "api.monosnap.ai" not in direct:
                            log.debug(f"--- УСПЕХ MONOSNAP --- Прямой URL: {direct}")
                            return f'<img src="{direct}" style="max-width:100%;">'
                        else:
                            log.warning("Не удалось получить редирект на файл, остался API URL")
                    else:
                        log.warning(f"API Monosnap ответило кодом {r.status_code}")
                except Exception as e:
                    log.error(f"Ошибка при получении прямой ссылки Monosnap: {e}")
            
            return original
            
        # ==================== SNIPBOARD ====================
        if "snipboard.io/" in url:
            direct = url.replace("https://snipboard.io/", "https://i.snipboard.io/")
            if not direct.endswith(('.jpg', '.png')): direct += ".jpg"
            return f'<img src="{direct}" style="max-width:100%;">'

        # ==================== ICECREAM ====================
        if "icecream.me/" in url and "/uploads/" not in url:
            img_id = url.split('/')[-1]
            direct = f"https://icecream.me/uploads/{img_id}.png"
            return f'<img src="{direct}" style="max-width:100%;">'

        # ==================== IMGUR ====================
        if "imgur.com/" in url and "i.imgur.com" not in url:
            try:
                r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
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
                r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    img = soup.find('img', class_="no-click") or soup.find('img', id="screenshot-image")
                    if img and img.get('src'):
                        src = img['src']
                        if src.startswith('//'): src = 'https:' + src
                        return f'<img src="{src}" style="max-width:100%;">'
            except: pass
            return original

        # ==================== TPPR.ME ====================
        # ==================== TPPR.ME ====================
        if "tppr.me/" in url:
            log.debug(f"Обработка Tppr: {url}")
            try:
                local_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"}
                r = requests.get(url, timeout=10, headers=local_headers)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    meta_img = soup.find('meta', property="og:image") or soup.find('meta', name="twitter:image:src")
                    if meta_img and meta_img.get('content'):
                        src = meta_img['content']
                        # Подмена для Intercom
                        if "tppr.s3.eu-central-1.amazonaws.com" in src:
                            src = src.replace("tppr.s3.eu-central-1.amazonaws.com", "media.tppr.me")
                        log.debug(f"--- УСПЕХ TPPR --- {src}")
                        return f'<img src="{src}" style="max-width:100%;">'
            except Exception as e:
                log.error(f"Ошибка Tppr: {e}")
            return f'<a href="{url}">{url}</a>'


def task_to_html(task):
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""

    processed = process_image_links(desc)
    # Используем расширение nl2br для сохранения переносов строк
    body = markdown(processed, extensions=['nl2br']) if processed else "<p>Нет описания</p>"

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
        "folder_id": 4101985,
        "locale": "en"
    }

    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

    log.info(f"Статус ответа Intercom: {r.status_code}")
    if r.status_code not in (200, 201):
        log.error(f"Ошибка публикации в Intercom:\n{r.text}")
    else:
        article_id = r.json().get('id')
        log.info(f"✅ Успешно синхронизировано! ID статьи: {article_id}")


def run_test_task():
    log.info(f"=== ЗАПУСК ТЕСТА ДЛЯ ЗАДАЧИ {TEST_TASK_ID} ===")

    r = cu.get(f"https://api.clickup.com/api/v2/task/{TEST_TASK_ID}", 
               params={"include_markdown_description": "true"})

    if r.status_code != 200:
        log.error(f"Задача {TEST_TASK_ID} не найдена")
        return

    task = r.json()
    
    # Пытаемся взять Markdown, если его нет - обычное описание, если и его нет - пустую строку
    md_desc = task.get("markdown_description")
    text_desc = task.get("description")
    
    task["description"] = md_desc if md_desc else (text_desc if text_desc else "")
    
    if not task["description"]:
        log.warning("ВНИМАНИЕ: Описание задачи в ClickUp пустое!")

    sync_article(task)


