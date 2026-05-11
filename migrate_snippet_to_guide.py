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

# Для сниппетов часто нужна более старая версия, попробуем переключать ее на лету
SNIPPET_API_VERSION = "2.10" 
GUIDE_API_VERSION = "2.15"

INTERCOM_AUTHOR_ID = os.getenv("INTERCOM_AUTHOR_ID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
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

ddef migrate():
    snippet_id = "2806960"
    target_folder_id = 2751260
    
    # 1. ПОЛУЧЕНИЕ СНИППЕТА
    log.info(f"📥 Запрос сниппета {snippet_id}...")
    
    # Пытаемся вызвать через версию 2.3 — она самая стабильная для сниппетов
    snippet_headers = {
        "Intercom-Version": "2.3",
        "Authorization": f"Bearer {INTERCOM_TOKEN}",
        "Accept": "application/json"
    }
    
    res = requests.get(
        f"{INTERCOM_BASE}/content_snippets/{snippet_id}", 
        headers=snippet_headers
    )
    
    # Если 2.3 не сработает, пробуем Unstable
    if res.status_code != 200:
        log.info("Версия 2.3 не подошла, пробуем Unstable...")
        snippet_headers["Intercom-Version"] = "Unstable"
        res = requests.get(
            f"{INTERCOM_BASE}/content_snippets/{snippet_id}", 
            headers=snippet_headers
        )

    if res.status_code != 200:
        log.error(f"❌ Ошибка доступа: {res.status_code} {res.text}")
        return

    # 2. Создаем Internal Guide (используем версию 2.15 и folder_id)
    log.info(f"📤 Создание гайда в папке {target_folder_id}...")
    
    payload = {
        "title": name,
        "body": html_body,
        "author_id": int(INTERCOM_AUTHOR_ID) if INTERCOM_AUTHOR_ID else None,
        "parent_id": target_folder_id,
        "parent_type": "folder",
        "state": "published"
    }

    headers = {"Intercom-Version": GUIDE_API_VERSION}
    create_res = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload, headers=headers)
    
    if create_res.status_code in [200, 201]:
        guide_id = create_res.json().get("id")
        log.info(f"✅ Гайд успешно создан (ID: {guide_id})")
        
        # 3. Удаляем сниппет
        log.info(f"🗑️ Удаление сниппета {snippet_id}...")
        headers = {"Intercom-Version": SNIPPET_API_VERSION}
        del_res = ic.delete(f"{INTERCOM_BASE}/content_snippets/{snippet_id}", headers=headers)
        
        if del_res.status_code == 204:
            log.info("✨ Готово: гайд создан в папке, сниппет удален.")
        else:
            log.warning(f"Сниппет не удален (код {del_res.status_code}): {del_res.text}")
    else:
        log.error(f"❌ Не удалось создать гайд: {create_res.text}")

if __name__ == "__main__":
    migrate()
