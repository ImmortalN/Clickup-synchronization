import os
import logging
import requests
from dotenv import load_dotenv

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"  # Для работы с Internal Articles часто требуется Unstable или 2.11+

INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

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
# ФУНКЦИИ КОНВЕРТАЦИИ
# ==============================

def json_blocks_to_html(blocks):
    """Конвертирует json_blocks Intercom в HTML строку"""
    html_output = ""
    for block in blocks:
        block_type = block.get("type")
        content = block.get("content", "")

        if block_type == "paragraph":
            html_output += f"<p>{content}</p>"
        elif block_type == "heading":
            html_output += f"<h2>{content}</h2>"
        elif block_type == "unordered_list":
            items = "".join([f"<li>{item}</li>" for item in block.get("items", [])])
            html_output += f"<ul>{items}</ul>"
        elif block_type == "ordered_list":
            items = "".join([f"<li>{item}</li>" for item in block.get("items", [])])
            html_output += f"ol>{items}</ol>"
        elif block_type == "code":
            html_output += f"<pre><code>{content}</code></pre>"
        # Можно добавить другие типы (image, button) при необходимости
    return html_output

def process_conversion(snippet_id, folder_id):
    # 1. Получаем сниппет
    log.info(f"📥 Получение сниппета {snippet_id}...")
    res = ic.get(f"{INTERCOM_BASE}/content_snippets/{snippet_id}")
    if res.status_code != 200:
        log.error(f"Не удалось найти сниппет: {res.text}")
        return

    snippet_data = res.json()
    title = snippet_data.get("name")
    # Сниппеты хранят контент в body.content_blocks
    blocks = snippet_data.get("body", {}).get("content_blocks", [])
    
    # 2. Конвертируем в HTML
    html_body = json_blocks_to_html(blocks)
    if not html_body:
        # Если блоки пустые, пробуем взять из value (иногда в старых API)
        html_body = snippet_data.get("value", "")

    # 3. Создаем Internal Guide
    log.info(f"📤 Создание Internal Guide: {title}...")
    payload = {
        "title": title,
        "body": html_body,
        "author_id": INTERCOM_AUTHOR_ID,
        "parent_id": int(folder_id), # В API internal_articles используется parent_id или folder_id
        "parent_type": "folder"
    }
    
    create_res = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    
    if create_res.status_code in [200, 201]:
        new_guide = create_res.json()
        log.info(f"✅ Гайд создан! ID: {new_guide.get('id')}")
        
        # 4. Удаляем сниппет
        log.info(f"🗑️ Удаление сниппета {snippet_id}...")
        del_res = ic.delete(f"{INTERCOM_BASE}/content_snippets/{snippet_id}")
        
        if del_res.status_code == 204:
            log.info("✨ Сниппет успешно удален.")
        else:
            log.warning(f"⚠️ Гайд создан, но сниппет не удален: {del_res.status_code} {del_res.text}")
    else:
        log.error(f"❌ Ошибка создания гайда: {create_res.status_code} {create_res.text}")

if __name__ == "__main__":
    TEST_SNIPPET_ID = "2806960"
    TARGET_FOLDER_ID = "2751260"
    process_conversion(TEST_SNIPPET_ID, TARGET_FOLDER_ID)
