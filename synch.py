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

    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        original = url

        # ==================== РАБОЧИЕ ====================
        # Snipboard
        if "snipboard.io/" in url:
            direct = url.replace("https://snipboard.io/", "https://i.snipboard.io/")
            return f'<img src="{direct}" style="max-width:100%;">'

        # Icecream
        if "icecream.me/" in url and "/uploads/" not in url:
            direct = f"https://icecream.me/uploads/{url.split('/')[-1]}.png"
            return f'<img src="{direct}" style="max-width:100%;">'

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
                        return f'<img src="{src}" style="max-width:100%;">'
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
                        return f'<img src="{src}" style="max-width:100%;">'
            except:
                pass

        # GitHub
        if "user-images.githubusercontent.com" in url:
            return f'<img src="{url}" style="max-width:100%;">'

        # ==================== ПРОБЛЕМНЫЕ — ОСТАВЛЯЕМ КАК ТЕКСТ ====================
        if any(x in url for x in ["monosnap.ai", "tppr.me"]):
            log.warning(f"Оставляем как обычную ссылку: {url}")
            return original

        # Остальные прямые ссылки
        if re.search(r'\.(png|jpe?g|gif|webp|bmp)', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'

        return original

    text = re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)
    return text


def task_to_html(task):
    name = task.get("name") or "(Без названия)"
    desc = task.get("description") or ""

    processed = process_image_links(desc)
    body = markdown(processed) if processed else "<p>Нет описания</p>"

    return f"# {html.escape(name)}\n\n{body}"


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
        "locale": "en"
    }

    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)

    log.info(f"Статус: {r.status_code}")
    if r.status_code not in (200, 201):
        log.error(f"Ошибка от Intercom:\n{r.text}")
    else:
        log.info(f"✅ Успешно! ID статьи: {r.json().get('id')}")


def run_test_task():
    log.info(f"=== ТЕСТ ЗАДАЧИ {TEST_TASK_ID} ===")

    r = cu.get(f"https://api.clickup.com/api/v2/task/{TEST_TASK_ID}", 
               params={"include_markdown_description": "true"})

    if r.status_code != 200:
        log.error("Задача не найдена")
        return

    task = r.json()
    task["description"] = task.get("markdown_description") or task.get("description") or ""

    sync_article(task)


if __name__ == "__main__":
    run_test_task()
