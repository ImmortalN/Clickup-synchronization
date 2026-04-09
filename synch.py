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
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID")
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

# Для теста одной задачи
TEST_TASK_ID = "869cumg5k"
MAX_TASKS_FOR_TEST = int(os.getenv("MAX_TASKS_FOR_TEST", 0))

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
# ОБРАБОТКА СКРИНШОТОВ — ГЛАВНАЯ ФУНКЦИЯ
# ==============================
def process_image_links(text: str) -> str:
    if not text:
        return text

    # Убираем markdown-ссылки [текст](url) → оставляем только url
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        original_url = url

        # ==================== ICECREAM ====================
        if "icecream.me/" in url and "/uploads/" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://icecream.me/uploads/{img_id}.png" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'

        # ==================== MONOSNAP ====================
        if "monosnap.ai/file/" in url and "api.monosnap.ai" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://api.monosnap.ai/file/download?id={img_id}" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'

        # ==================== TPPR.ME ====================
        if "tppr.me/" in url and "media.tppr.me" not in url:
            img_id = url.split('/')[-1]
            return f'<img src="https://media.tppr.me/uploads/{img_id}.jpg" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'

        # ==================== IMGUR (включая альбомы) ====================
        if "imgur.com/" in url and "i.imgur.com" not in url:
            try:
                r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    # Ищем первую большую картинку в альбоме или посте
                    img = soup.find('img', src=re.compile(r'i\.imgur\.com'))
                    if img and img.get('src'):
                        direct = img['src']
                        if direct.startswith('//'):
                            direct = 'https:' + direct
                        return f'<img src="{direct}" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'
            except Exception as e:
                log.debug(f"Imgur parse error for {url}: {e}")
            # Если не получилось — оставляем оригинал
            return original_url

        # ==================== PRNT.SC / LIGHTSHOT ====================
        if "prnt.sc/" in url or "prntscr.com/" in url:
            try:
                r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    img_tag = soup.find('img', class_="no-click") or soup.find('img', id="screenshot-image")
                    if img_tag and img_tag.get('src'):
                        direct = img_tag['src']
                        if direct.startswith('//'):
                            direct = 'https:' + direct
                        return f'<img src="{direct}" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'
            except Exception as e:
                log.debug(f"prnt.sc parse error for {url}: {e}")

        # ==================== УЖЕ ПРЯМЫЕ ССЫЛКИ ====================
        if re.search(r'\.(png|jpg|jpeg|gif|webp|bmp)(\?.*)?$', url.lower()):
            return f'<img src="{url}" style="max-width:100%; height:auto; display:block; margin:15px 0;" />'

        # Если ничего не подошло — возвращаем как есть
        return original_url

    # Заменяем все URL в тексте
    url_pattern = r'https?://[^\s\)\'\"<>]+'
    text = re.sub(url_pattern, transform_url, text)

    # Разделяем картинки красивым отступом
    text = text.replace(' /><img', ' /><br><br><img')

    return text


# ==============================
# УТИЛИТЫ CLICKUP
# ==============================
def load_state():
    if os.path.exists(SYNC_STATE_FILE):
        with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

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
        except requests.exceptions.RequestException as e:
            log.warning(f"Сетевая ошибка: {e}")
            time.sleep(2 ** attempt)
    return None


def get_tasks_from_list(list_id, updated_after=None):
    page = 0
    while True:
        params = {
            "page": page,
            "include_subtasks": "true",
            "limit": 100,
            "include_markdown_description": "true"
        }
        if updated_after and not FETCH_ALL:
            params["updated_gt"] = int(updated_after.timestamp() * 1000)

        r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params=params)
        if r.status_code != 200:
            log.error(f"Ошибка при получении задач из списка {list_id}: {r.status_code}")
            break

        tasks = r.json().get("tasks", [])
        if not tasks:
            break

        for t in tasks:
            t["description"] = t.get("markdown_description") or t.get("description") or ""
            yield t

        page += 1


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
    body = markdown(processed_desc) if processed_desc else "<p><em>Нет описания</em></p>"

    if len(body) > 50000:
        body = body[:50000] + "<p><em>Описание урезано</em></p>"

    return f"<h1>{html.escape(name)}</h1>{body}"


def sync_article(task, article_map):
    task_id = task["id"]
    title = f"{task.get('name') or 'Untitled'} [{task_id}]"[:255]
    body = task_to_html(task)

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

    if r and r.status_code in (200, 201):
        return r.json().get("id")
    else:
        log.error(f"Ошибка при сохранении в Intercom: {r.status_code if r else 'No response'}")
        return None


# ==============================
# ТЕСТОВЫЙ ЗАПУСК (одна задача)
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
        log.info(f"✅ УСПЕШНО! Статья в Intercom: {result_id}")
    else:
        log.error("❌ Ошибка при создании/обновлении статьи")


# ==============================
# ОСНОВНОЙ ЗАПУСК (полная синхронизация)
# ==============================
def main():
    log.info("Запуск полной синхронизации ClickUp → Intercom")
    article_map = load_all_articles()
    state = load_state()

    # Здесь можно добавить обход всех списков, если нужно
    # (пока оставил только тестовый режим — раскомментируй, когда тест пройдёт)

    log.info("Полная синхронизация пока отключена. Используй run_test_task()")


if __name__ == "__main__":
    # === ВЫБЕРИ, ЧТО ЗАПУСКАТЬ ===
    run_test_task()          # ← сейчас активно (тест одной задачи)

    # main()                 # ← раскомментируй, когда тест пройдёт успешно
