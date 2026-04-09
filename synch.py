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
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "2.14")   # Рекомендуется 2.14
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FETCH_ALL = os.getenv("FETCH_ALL", "false").lower() == "true"
DEBUG_SEARCH = os.getenv("DEBUG_SEARCH", "false").lower() == "true"

# Тестовая задача
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
# ОБРАБОТКА СКРИНШОТОВ
# ==============================
def process_image_links(text: str) -> str:
    if not text:
        return text

    # Убираем markdown ссылки
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        original_url = url

        # Icecream
        if "icecream.me/" in url and "/uploads/" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://icecream.me/uploads/{img_id}.png" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'

        # Monosnap
        if "monosnap.ai/file/" in url and "api.monosnap.ai" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://api.monosnap.ai/file/download?id={img_id}" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'

        # tppr.me
        if "tppr.me/" in url and "media.tppr.me" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://media.tppr.me/uploads/{img_id}.jpg" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'

        # Imgur
        if "imgur.com/" in url and "i.imgur.com" not in url:
            try:
                r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    img = soup.find('img', src=re.compile(r'i\.imgur\.com'))
                    if img and img.get('src'):
                        direct = img['src']
                        if direct.startswith('//'):
                            direct = 'https:' + direct
                        return f'<img src="{direct}" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'
            except Exception as e:
                log.debug(f"Imgur parse error: {e}")
            return original_url

        # prnt.sc
        if "prnt.sc/" in url or "prntscr.com/" in url:
            try:
                r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    img_tag = soup.find('img', class_="no-click") or soup.find('img', id="screenshot-image")
                    if img_tag and img_tag.get('src'):
                        direct = img_tag['src']
                        if direct.startswith('//'):
                            direct = 'https:' + direct
                        return f'<img src="{direct}" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'
            except Exception as e:
                log.debug(f"prnt.sc parse error: {e}")

        # Уже прямые ссылки
        if re.search(r'\.(png|jpg|jpeg|gif|webp|bmp)(\?.*)?$', url.lower()):
            return f'<img src="{url}" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'

        return original_url

    url_pattern = r'https?://[^\s\)\'\"<>]+'
    text = re.sub(url_pattern, transform_url, text)
    text = text.replace(' /><img', ' /><br><br><img')

    return text


# ==============================
# УТИЛИТЫ
# ==============================
def retry_request(method, url, json_data=None, max_retries=5):
    for attempt in range(1, max_retries + 1):
        try:
            r = method(url, json=json_data)
            if r.status_code in (500, 502, 503, 504):
                wait = 2 ** attempt
                log.warning(f"Серверная ошибка {r.status_code}, ждём {wait}с...")
                time.sleep(wait)
                continue
            return r
        except Exception as e:
            log.warning(f"Сетевая ошибка: {e}")
            time.sleep(2 ** attempt)
    return None


# ==============================
# РАБОТА С INTERCOM
# ==============================
def load_all_articles():
    log.info("Загружаем существующие статьи в Intercom...")
    task_id_to_id = {}
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 100})
        if r.status_code != 200:
            break
        data = r.json()
        for art in data.get("data", []):
            title = art.get("title", "")
            if "[" in title and "]" in title:
                task_id = title[title.rfind("[") + 1:title.rfind("]")].strip()
                task_id_to_id[task_id] = art["id"]
        if page >= data.get("pages", {}).get("total_pages", 1):
            break
        page += 1
    return task_id_to_id


def task_to_html(task):
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""

    processed_desc = process_image_links(desc)

    # ЖЁСТКОЕ ОГРАНИЧЕНИЕ РАЗМЕРА
    if len(processed_desc) > 25000:
        processed_desc = processed_desc[:25000] + "\n\n... (описание урезано)"

    body = markdown(processed_desc) if processed_desc else "<p><em>Нет описания</em></p>"

    if len(body) > 45000:
        body = body[:45000] + "<p><em>Описание урезано Intercom</em></p>"

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
        r = retry_request(ic.put, f"{INTERCOM_BASE}/internal_articles/{article_id}", json_data=payload)
    else:
        log.info(f"Создаём новую статью {task_id}")
        r = retry_request(ic.post, f"{INTERCOM_BASE}/internal_articles", json_data=payload)

    if r:
        log.info(f"Intercom ответил кодом: {r.status_code}")
        if r.status_code in (200, 201):
            log.info("✅ Статья успешно сохранена")
            return r.json().get("id")
        else:
            log.error(f"❌ Ошибка {r.status_code}: {r.text[:500]}")
    else:
        log.error("❌ Нет ответа от Intercom")

    return None


# ==============================
# ТЕСТОВАЯ ФУНКЦИЯ
# ==============================
def run_test_task():
    log.info(f"=== ТЕСТОВЫЙ ЗАПУСК ЗАДАЧИ {TEST_TASK_ID} ===")

    r = cu.get(
        f"https://api.clickup.com/api/v2/task/{TEST_TASK_ID}",
        params={"include_markdown_description": "true"}
    )

    if r.status_code != 200:
        log.error(f"Задача {TEST_TASK_ID} не найдена")
        return

    task = r.json()
    task["description"] = task.get("markdown_description") or task.get("description") or ""

    article_map = load_all_articles()
    result_id = sync_article(task, article_map)

    if result_id:
        log.info(f"✅ УСПЕШНО! ID статьи в Intercom: {result_id}")
    else:
        log.error("❌ Не удалось создать/обновить статью")


# ==============================
# ЗАПУСК
# ==============================
if __name__ == "__main__":
    run_test_task()          # ← сейчас работает тест
    # main()                 # ← раскомментируй позже
