import os
import json
import logging
import requests
import time
from dotenv import load_dotenv

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"

INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

TARGET_FOLDER_ID = 2751260

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

# ==============================
# ПАРСЕР ХТМЛ
# ==============================

def parse_intercom_blocks(blocks):
    html_output = ""
    for block in blocks:
        b_type = block.get("type")
        text = block.get("text", "")
        items = block.get("items", [])
        
        if b_type == "heading":
            html_output += f"<h1>{text}</h1>"
        elif b_type == "subheading":
            html_output += f"<h2>{text}</h2>"
        elif b_type == "subheading3":
            html_output += f"<h3>{text}</h3>"
        elif b_type == "paragraph":
            html_output += f"<p>{text}</p>"
        elif b_type == "orderedList":
            li_items = "".join([f"<li>{item}</li>" for item in items])
            html_output += f"<ol>{li_items}</ol>"
        elif b_type == "unorderedList":
            li_items = "".join([f"<li>{item}</li>" for item in items])
            html_output += f"<ul>{li_items}</ul>"
            
    return html_output

# ==============================
# ОСНОВНАЯ ЛОГИКА
# ==============================

def fetch_all_snippets():
    """Получает список всех сниппетов в аккаунте"""
    log.info("🔍 Получение списка всех сниппетов...")
    all_snippets = []
    url = f"{INTERCOM_BASE}/content_snippets"
    
    while url:
        res = ic.get(url)
        if res.status_code != 200:
            log.error(f"Не удалось получить список сниппетов: {res.text}")
            break
        
        data = res.json()
        all_snippets.extend(data.get("data", []))
        
        # Обработка пагинации
        pages = data.get("pages", {})
        url = pages.get("next")
        
    log.info(f"Найдено сниппетов: {len(all_snippets)}")
    return all_snippets

def migrate_all():
    snippets = fetch_all_snippets()
    
    for snip_summary in snippets:
        snippet_id = snip_summary["id"]
        
        # 1. Получаем полные данные каждого сниппета
        log.info(f"--- Обработка сниппета ID: {snippet_id} ---")
        res = ic.get(f"{INTERCOM_BASE}/content_snippets/{snippet_id}")
        
        if res.status_code != 200:
            log.error(f"❌ Не удалось получить данные сниппета {snippet_id}: {res.text}")
            continue
            
        snippet_data = res.json()
        title = snippet_data.get("title") or snippet_data.get("name") or f"Snippet {snippet_id}"
        json_blocks_raw = snippet_data.get("json_blocks")
        
        if not json_blocks_raw:
            log.warning(f"⚠️ У сниппета '{title}' нет json_blocks. Пропускаем.")
            continue
            
        try:
            blocks = json.loads(json_blocks_raw)
        except:
            log.error(f"❌ Ошибка парсинга JSON для сниппета '{title}'")
            continue
            
        html_content = parse_intercom_blocks(blocks)
        
        # 2. Создаем гайд
        log.info(f"📤 Создание гайда: '{title}'...")
        payload = {
            "title": title,
            "body": html_content,
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "folder_id": int(TARGET_FOLDER_ID),
            "parent_id": int(TARGET_FOLDER_ID),
            "parent_type": "folder",
            "state": "published"
        }
        
        create_res = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
        
        if create_res.status_code in [200, 201]:
            log.info(f"✅ Успешно! ID нового гайда: {create_res.json().get('id')}")
        else:
            log.error(f"❌ Ошибка при создании гайда '{title}': {create_res.text}")
            
        # Небольшая пауза, чтобы не упереться в лимиты API Intercom
        time.sleep(0.3)

if __name__ == "__main__":
    migrate_all()
