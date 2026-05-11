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
# Попробуем версию 2.11, она стабильнее для статей
INTERCOM_VERSION = "2.11" 

INTERCOM_AUTHOR_ID = os.getenv("INTERCOM_AUTHOR_ID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

def json_blocks_to_html(blocks):
    """Конвертирует структуру блоков Intercom в HTML"""
    html_output = ""
    if not blocks:
        return ""
    
    for block in blocks:
        b_type = block.get("type")
        content = block.get("content", "")
        
        if b_type == "paragraph":
            html_output += f"<p>{content}</p>"
        elif b_type == "heading":
            html_output += f"<h2>{content}</h2>"
        elif b_type in ["unordered_list", "ordered_list"]:
            tag = "ul" if b_type == "unordered_list" else "ol"
            items = "".join([f"<li>{item}</li>" for item in block.get("items", [])])
            html_output += f"<{tag}>{items}</{tag}>"
        elif b_type == "code":
            html_output += f"<pre><code>{content}</code></pre>"
    return html_output

def migrate():
    # Используем только ID сниппета
    snippet_id = "2806960"
    
    log.info(f"📥 Запрос сниппета {snippet_id}...")
    res = ic.get(f"{INTERCOM_BASE}/content_snippets/{snippet_id}")
    
    if res.status_code != 200:
        log.error(f"Ошибка доступа (код {res.status_code}): {res.text}")
        log.error("Проверьте, включен ли scope 'Content Snippets' в настройках вашего Intercom App.")
        return

    data = res.json()
    name = data.get("name", "Migrated Guide")
    # Извлекаем контент (в разных версиях API может быть в 'body' или 'value')
    blocks = data.get("body", {}).get("content_blocks", [])
    
    html_body = json_blocks_to_html(blocks)
    if not html_body:
        html_body = data.get("value", "<p>No content found</p>")

    log.info(f"📤 Создание гайда на основе сниппета...")
    
    payload = {
        "title": name,
        "body": html_body,
        "author_id": int(INTERCOM_AUTHOR_ID) if INTERCOM_AUTHOR_ID else None,
        "state": "published" # Сразу публикуем
    }

    create_res = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    
    if create_res.status_code in [200, 201]:
        guide_id = create_res.json().get("id")
        log.info(f"✅ Гайд успешно создан (ID: {guide_id})")
        
        # Удаляем старый сниппет
        log.info(f"🗑️ Удаление сниппета {snippet_id}...")
        del_res = ic.delete(f"{INTERCOM_BASE}/content_snippets/{snippet_id}")
        if del_res.status_code == 204:
            log.info("✨ Готово: гайд создан, сниппет удален.")
        else:
            log.warning(f"Сниппет не удален (код {del_res.status_code})")
    else:
        log.error(f"❌ Не удалось создать гайд: {create_res.text}")

if __name__ == "__main__":
    migrate()
