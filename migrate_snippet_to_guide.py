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
INTERCOM_AUTHOR_ID = os.getenv("INTERCOM_AUTHOR_ID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

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
    snippet_id = "2806960"
    target_folder_id = 2751260
    
    # 1. ПОЛУЧЕНИЕ СНИППЕТА
    log.info(f"📥 Запрос сниппета {snippet_id}...")
    
    success_res = None
    # Список версий для перебора
    versions_to_try = ["2.3", "Unstable", "2.11"]
    
    for v in versions_to_try:
        headers = {
            "Intercom-Version": v,
            "Authorization": f"Bearer {INTERCOM_TOKEN}",
            "Accept": "application/json"
        }
        try:
            res = requests.get(f"{INTERCOM_BASE}/content_snippets/{snippet_id}", headers=headers, timeout=10)
            if res.status_code == 200:
                log.info(f"✅ Успешно получен сниппет через версию API {v}")
                success_res = res.json()
                used_version = v
                break
            else:
                log.warning(f"Версия {v} не подошла (код {res.status_code})")
        except Exception as e:
            log.error(f"Ошибка при запросе версии {v}: {e}")

    if not success_res:
        log.error("❌ Не удалось получить сниппет ни через одну версию API.")
        return

    # Извлекаем данные ПОСЛЕ успешного получения
    name = success_res.get("name", "Migrated Guide")
    blocks = success_res.get("body", {}).get("content_blocks", [])
    
    html_body = json_blocks_to_html(blocks)
    if not html_body:
        # Пытаемся взять из альтернативных полей, если блоки пустые
        html_body = success_res.get("value") or success_res.get("content") or "<p>No content</p>"

    # 2. СОЗДАНИЕ ГАЙДА
    log.info(f"📤 Создание гайда в папке {target_folder_id}...")
    
    guide_payload = {
        "title": name,
        "body": html_body,
        "author_id": int(INTERCOM_AUTHOR_ID) if INTERCOM_AUTHOR_ID else None,
        "parent_id": target_folder_id,
        "parent_type": "folder",
        "state": "published"
    }

    # Для создания гайда используем 2.15 (ваша основная версия)
    guide_headers = {
        "Intercom-Version": "2.15",
        "Authorization": f"Bearer {INTERCOM_TOKEN}",
        "Content-Type": "application/json"
    }

    create_res = requests.post(f"{INTERCOM_BASE}/internal_articles", json=guide_payload, headers=guide_headers)
    
    if create_res.status_code in [200, 201]:
        guide_id = create_res.json().get("id")
        log.info(f"✅ Гайд успешно создан (ID: {guide_id})")
        
        # 3. УДАЛЕНИЕ СНИППЕТА
        log.info(f"🗑️ Удаление сниппета {snippet_id}...")
        # Используем ту же версию, которая сработала на GET
        del_headers = {"Intercom-Version": used_version, "Authorization": f"Bearer {INTERCOM_TOKEN}"}
        del_res = requests.delete(f"{INTERCOM_BASE}/content_snippets/{snippet_id}", headers=del_headers)
        
        if del_res.status_code == 204:
            log.info("✨ Готово: гайд создан, сниппет удален.")
        else:
            log.warning(f"Сниппет не удален (код {del_res.status_code}): {del_res.text}")
    else:
        log.error(f"❌ Не удалось создать гайд: {create_res.text}")

if __name__ == "__main__":
    migrate()
