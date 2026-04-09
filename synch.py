import os
import time
import json
import html
import logging
import re
from datetime import datetime, timezone
import requests
from markdown import markdown
from dotenv import load_dotenv
from bs4 import BeautifulSoup

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
SPACE_ID = "90125205902"
IGNORED_LIST_IDS = {"901212791461", "901212763746"}

INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "2.14")
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"
DEBUG_SEARCH = os.getenv("DEBUG_SEARCH", "false").lower() == "true"

TEST_TASK_ID = "869cumg5k"
SYNC_STATE_FILE = ".sync_state.json"

# ==============================
# ЛОГИРОВАНИЕ
# ==============================
logging.basicConfig(
    level=logging.DEBUG if DEBUG_SEARCH else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
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
# ОБРАБОТКА ИЗОБРАЖЕНИЙ
# ==============================
def process_image_links(text: str) -> str:
    if not text:
        return text

    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        original = url

        # Icecream
        if "icecream.me/" in url and "/uploads/" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://icecream.me/uploads/{img_id}.png" style="max-width:100%; height:auto; display:block; margin:12px 0;" />'

        # Monosnap
        if "monosnap.ai/file/" in url and "api.monosnap.ai" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://api.monosnap.ai/file/download?id={img_id}" style="max-width:100%; height:auto; display:block; margin:12px 0;" />'

        # tppr.me
        if "tppr.me/" in url and "media.tppr.me" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://media.tppr.me/uploads/{img_id}.jpg" style="max-width:100%; height:auto; display:block; margin:12px 0;" />'

        # Imgur
        if "imgur.com/" in url and "i.imgur.com" not in url:
            try:
                r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    img = soup.find('img', src=re.compile(r'i\.imgur\.com'))
                    if img and img.get('src'):
                        src = img['src']
                        if src.startswith('//'): src = 'https:' + src
                        return f'<img src="{src}" style="max-width:100%; height:auto; display:block; margin:12px 0;" />'
            except:
                pass
            return original

        # prnt.sc
        if "prnt.sc/" in url or "prntscr.com/" in url:
            try:
                r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    img = soup.find('img', class_="no-click") or soup.find('img', id="screenshot-image")
                    if img and img.get('src'):
                        src = img['src']
                        if src.startswith('//'): src = 'https:' + src
                        return f'<img src="{src}" style="max-width:100%; height:auto; display:block; margin:12px 0;" />'
            except:
                pass

        # Прямые ссылки
        if re.search(r'\.(png|jpe?g|gif|webp|bmp)(\?.*)?$', url.lower()):
            return f'<img src="{url}" style="max-width:100%; height:auto; display:block; margin:12px 0;" />'

        return original

    text = re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)
    text = text.replace(' /><img', ' /><br><br><img')
    return text


# ==============================
# INTERCOM
# ==============================
def load_all_articles():
    log.info("Загружаем существующие статьи в Intercom...")
    task_id_to_id = {}
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 100})
        if r.status_code != 200:
            break
        for art in r.json().get("data", []):
            title = art.get("title", "")
            if "[" in title and "]" in title:
                tid = title[title.rfind("[")+1:title.rfind("]")].strip()
                task_id_to_id[tid] = art["id"]
        if page >= r.json().get("pages", {}).get("total_pages", 1):
            break
        page += 1
    return task_id_to_id


def task_to_html(task):
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""

    processed = process_image_links(desc)

    # Ограничение размера
    if len(processed) > 20000:
        processed = processed[:20000] + "\n\n... (урезано)"

    body = markdown(processed) if processed else "<p><em>Нет описания</em></p>"

    if len(body) > 40000:
        body = body[:40000] + "<p><em>Описание урезано</em></p>"

    return f"<h1>{html.escape(name)}</h1>{body}"


def sync_article(task, article_map):
    task_id = task["id"]
    title = f"{task.get('name') or 'Untitled'} [{task_id}]"[:255]
    body = task_to_html(task)

    log.info(f"Размер body: {len(body)} символов")

    payload = {
        "title": title,
        "body": body,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "locale": "en"
    }

    if task_id in article_map:
        article_id = article_map[task_id]
        log.info(f"Обновляем статью {task_id}")
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
    else:
        log.info(f"Создаём новую статью {task_id}")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

    if r:
        log.info(f"Intercom код ответа: {r.status_code}")
        if r.status_code in (200, 201):
            log.info("✅ Успешно!")
            return r.json().get("id")
        else:
            log.error(f"❌ ОШИБКА {r.status_code}: {r.text}")
    else:
        log.error("❌ Нет ответа от сервера")

    return None


# ==============================
# ТЕСТ
# ==============================
def run_test_task():
    log.info(f"=== ТЕСТ ЗАДАЧИ {TEST_TASK_ID} ===")

    r = cu.get(f"https://api.clickup.com/api/v2/task/{TEST_TASK_ID}", 
               params={"include_markdown_description": "true"})

    if r.status_code != 200:
        log.error("Задача не найдена")
        return

    task = r.json()
    task["description"] = task.get("markdown_description") or task.get("description") or ""

    article_map = load_all_articles()
    sync_article(task, article_map)


if __name__ == "__main__":
    run_test_task()
