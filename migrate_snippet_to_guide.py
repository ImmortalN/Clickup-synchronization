import os
import json
import logging
import requests
from dotenv import load_dotenv

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"

# Эти ID должны быть в GitHub Secrets
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Сессия для запросов
ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

def parse_intercom_blocks(blocks):
    """
    Превращает блоки сниппета в чистый HTML.
    Обрабатывает типы из вашего лога: heading, paragraph, subheading, subheading3, orderedList, unorderedList.
    """
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

def migrate():
    # Используем новый ID сниппета из логов
    snippet_id = "4166254"
    target_folder_id = 2751260
    
    log.info(f"📥 Запрос сниппета {snippet_id}...")
    res = ic.get(f"{INTERCOM_BASE}/content_snippets/{snippet_id}")
    
    if res.status_code != 200:
        log.error(f"❌ Ошибка получения сниппета: {res.status_code} {res.text}")
        return

    snippet_data = res.json()
    
    # В логе заголовок лежит в 'title', а контент в 'json_blocks' (как строка)
    title = snippet_data.get("title") or "Migrated Snippet"
    json_blocks_raw = snippet_data.get("json_blocks")
    
    if not json_blocks_raw:
        log.error("❌ Поле json_blocks отсутствует в ответе API.")
        return

    try:
        # Десериализуем строку в список объектов
        blocks = json.loads(json_blocks_raw)
    except Exception as e:
        log.error(f"❌ Ошибка десериализации json_blocks: {e}")
        return

    # Конвертируем блоки в HTML
    html_content = parse_intercom_blocks(blocks)
    
    if not html_content:
        log.error("❌ После парсинга блоков контент оказался пустым.")
        return

    log.info(f"📤 Создание Internal Guide: '{title}' в папку {target_folder_id}")
    
    payload = {
        "title": title,
        "body": html_content,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "parent_id": target_folder_id,
        "parent_type": "folder",
        "state": "published"
    }

    create_res = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    
    if create_res.status_code in [200, 201]:
        log.info(f"✅ Гайд успешно создан! ID: {create_res.json().get('id')}")
        log.info("ℹ️ Удаление сниппета пропущено по вашей просьбе.")
    else:
        log.error(f"❌ Ошибка создания гайда: {create_res.status_code} {create_res.text}")

if __name__ == "__main__":
    migrate()
