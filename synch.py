import os
import time
import json
import html
import logging
import re
from datetime import datetime, timedelta, timezone

import requests
from markdown import markdown
from dotenv import load_dotenv
from bs4 import BeautifulSoup

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

CLICKUP_TOKEN           = os.getenv("CLICKUP_API_TOKEN")
CLICKUP_TEAM_ID         = os.getenv("CLICKUP_TEAM_ID")
SPACE_ID                = "90125205902"
IGNORED_LIST_IDS        = {"901212791461", "901212763746"}

INTERCOM_TOKEN          = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE           = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION        = os.getenv("INTERCOM_VERSION", "2.14")
INTERCOM_OWNER_ID       = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID      = int(os.getenv("INTERCOM_AUTHOR_ID"))
INTERCOM_FOLDER_ID      = 4101985

LOOKBACK_HOURS          = int(os.getenv("CLICKUP_UPDATED_LOOKBACK_HOURS", "24"))
FETCH_ALL               = os.getenv("FETCH_ALL", "false").lower() == "true"
CLICKUP_ONLY_OPEN       = os.getenv("CLICKUP_ONLY_OPEN", "true").lower() == "true"
MAX_TASKS_FOR_TEST      = int(os.getenv("MAX_TASKS_FOR_TEST", 0))
SYNC_STATE_FILE         = ".sync_state.json"

# ==============================
# ЛОГИРОВАНИЕ И СЕССИИ
# ==============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
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
    if not text: return text
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        if "monosnap.ai" in url:
            match_id = re.search(r'file/([a-zA-Z0-9]+)', url)
            if match_id:
                api_url = f"https://api.monosnap.ai/file/download?id={match_id.group(1)}"
                try:
                    r = requests.get(api_url, timeout=10, headers={"Referer": url}, allow_redirects=True)
                    if r.status_code == 200 and "api.monosnap.ai" not in r.url:
                        return f'<img src="{r.url}" style="max-width:100%;">'
                except: pass

        if "tppr.me/" in url:
            try:
                r = requests.get(url, timeout=10, headers=headers)
                soup = BeautifulSoup(r.text, 'lxml')
                meta = soup.find('meta', property="og:image") or soup.find('meta', name="twitter:image:src")
                if meta and meta.get('content'):
                    proxy_url = f"https://images.weserv.nl/?url={meta['content'].replace('https://', '')}"
                    return f'<img src="{proxy_url}" style="max-width:100%;">'
            except: pass

        if any(x in url for x in ["imgur.com", "prnt.sc", "prntscr.com", "snipboard.io", "icecream.me"]):
            try:
                r = requests.get(url, timeout=10, headers=headers)
                soup = BeautifulSoup(r.text, 'lxml')
                img = soup.find('meta', property="og:image") or soup.find('img', class_="no-click")
                src = img.get('content') if img and img.get('content') else (img.get('src') if img else None)
                if src: return f'<img src="{src}" style="max-width:100%;">'
            except: pass

        if re.search(r'\.(png|jpe?g|gif|webp)', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'
        
        return f'<a href="{url}">{url}</a>'

    return re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)

# ==============================
# CLICKUP ФУНКЦИИ (ВОЗВРАЩЕНЫ)
# ==============================
def get_folders():
    r = cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/folder", params={"archived": "false"})
    return r.json().get("folders", [])

def get_lists_in_folder(folder_id):
    r = cu.get(f"https://api.clickup.com/api/v2/folder/{folder_id}/list", params={"archived": "false"})
    return r.json().get("lists", [])

def get_folderless_lists():
    r = cu.get(f"https://api.clickup.com/api/v2/space/{SPACE_ID}/list", params={"archived": "false"})
    return r.json().get("lists", [])

def get_tasks_from_list(list_id, updated_after):
    page = 0
    updated_gt = int(updated_after.timestamp() * 1000) if not FETCH_ALL else None
    while True:
        params = {"page": page, "include_subtasks": "true", "limit": 100, "include_markdown_description": "true"}
        if updated_gt: params["updated_gt"] = updated_gt
        if CLICKUP_ONLY_OPEN: params["statuses[]"] = ["to do", "in progress"]

        r = cu.get(f"https://api.clickup.com/api/v2/list/{list_id}/task", params=params)
        tasks = r.json().get("tasks", [])
        if not tasks: break
        for t in tasks: yield t
        page += 1

def fetch_clickup_tasks(updated_after):
    count = 0
    # Папки
    for folder in get_folders():
        for lst in get_lists_in_folder(folder["id"]):
            if lst["id"] in IGNORED_LIST_IDS: continue
            for task in get_tasks_from_list(lst["id"], updated_after):
                if MAX_TASKS_FOR_TEST > 0 and count >= MAX_TASKS_FOR_TEST: return
                yield task
                count += 1
    # Списки без папок
    for lst in get_folderless_lists():
        if lst["id"] in IGNORED_LIST_IDS: continue
        for task in get_tasks_from_list(lst["id"], updated_after):
            if MAX_TASKS_FOR_TEST > 0 and count >= MAX_TASKS_FOR_TEST: return
            yield task
            count += 1

# ==============================
# INTERCOM ФУНКЦИИ
# ==============================
def retry_request(method, url, json=None, max_retries=5):
    for attempt in range(1, max_retries + 1):
        try:
            r = method(url, json=json)
            if r.status_code in (500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            return r
        except:
            time.sleep(2 ** attempt)
    return None

def load_all_articles():
    log.info("Загрузка всех статей из Intercom...")
    task_id_to_id = {}
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 100})
        if r.status_code != 200: break
        data = r.json()
        for art in data.get("data", []):
            title = art.get("title", "")
            match = re.search(r'\[([a-zA-Z0-9]+)\]$', title)
            if match: task_id_to_id[match.group(1)] = art["id"]
        if page >= data.get("pages", {}).get("total_pages", 1): break
        page += 1
    return task_id_to_id

def sync_article(task, article_map):
    task_id = task["id"]
    name = task.get("name") or "Untitled"
    title = f"{name} [{task_id}]"[:255]
    desc = task.get("markdown_description") or task.get("description") or ""
    
    body = f"<h1>{html.escape(name)}</h1>"
    body += markdown(process_image_links(desc), extensions=['nl2br']) if desc else "<p>Нет описания</p>"

    payload = {
        "title": title, "body": body[:50000],
        "owner_id": INTERCOM_OWNER_ID, "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": INTERCOM_FOLDER_ID, "locale": "en"
    }

    if task_id in article_map:
        article_id = article_map[task_id]
        r_get = ic.get(f"{INTERCOM_BASE}/internal_articles/{article_id}")
        if r_get.status_code == 200:
            curr = r_get.json()
            if curr.get("title") == title and curr.get("body") == body:
                log.info(f"Пропуск (нет изменений): {title}")
                return article_id
        log.info(f"Обновление: {title}")
        r = retry_request(ic.put, f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
    else:
        log.info(f"Создание: {title}")
        r = retry_request(ic.post, f"{INTERCOM_BASE}/internal_articles", json=payload)

    return r.json().get("id") if r and r.status_code in (200, 201) else None

# ==============================
# ЗАПУСК
# ==============================
def main():
    article_map = load_all_articles()
    
    # Определяем время синхронизации
    if os.path.exists(SYNC_STATE_FILE):
        with open(SYNC_STATE_FILE, "r") as f:
            last_iso = json.load(f).get("last_sync_iso")
    else: last_iso = None

    since = datetime.fromisoformat(last_iso) if last_iso and not FETCH_ALL else datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    log.info(f"Синхронизация задач после {since.isoformat()}")

    for task in fetch_clickup_tasks(since):
        sync_article(task, article_map)

    with open(SYNC_STATE_FILE, "w") as f:
        json.dump({"last_sync_iso": datetime.now(timezone.utc).isoformat()}, f)
    log.info("Синхронизация завершена!")

if __name__ == "__main__":
    main()
