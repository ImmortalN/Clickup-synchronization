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
# Используем Unstable, так как он успешно отдал сниппет в прошлый раз
INTERCOM_VERSION = "Unstable"

# Берем ID из ваших секретов/окружения
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Настраиваем сессию как в вашем основном коде
ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

def parse_intercom_blocks(blocks):
    """
    Преобразует блоки сниппета в HTML. 
    Добавлена поддержка разных типов контента Intercom.
    """
    html_output = ""
    if not blocks:
        return ""
    
    for block in blocks:
        b_type = block.get("type")
        content = block.get("text") or block.get("content") or ""
        
        if b_type == "paragraph":
            html_output += f"<p>{content}</p>"
        elif b_type == "heading":
            html_output += f"<h2>{content}</h2>"
        elif b_type == "code":
            html_output += f"<pre><code>{content}</code></pre>"
        elif b_type in ["unordered_list", "ordered_list"]:
            tag = "ul" if b_type == "unordered_list" else "ol"
            items = "".join([f"<li>{item}</li>" for item in block.get("items", [])])
            html_output += f"<{tag}>{items}</{tag}>"
        elif b_type == "image":
            url = block.get("url")
            html_output += f'<img src="{url}" style="max-width:100%;">'
            
    return html_output

def migrate():
    # НОВЫЙ ID СНИППЕТА
    snippet_id = "4166254"
    target_folder_id = 2751260
    
    log.info(f"📥 Запрос сниппета {snippet_id}...")
    
    res = ic.get(f"{INTERCOM_BASE}/content_snippets/{snippet_id}")
    
    if res.status_code != 200:
        log.error(f"❌ Ошибка получения сниппета: {res.status_code} {res.text}")
        return

    snippet_data = res.json()
    
    # Пытаемся получить название из разных полей
    name = snippet_data.get("name") or snippet_data.get("title") or "Untitled Snippet"
    
    # В Unstable сниппеты часто лежат в body -> content_blocks
    body_data = snippet_data.get("body", {})
    blocks = body_data.get("content_blocks", [])
    
    html_content = parse_intercom_blocks(blocks)
    
    # Если блоки пустые, пробуем взять сырое значение 'value' (как в старых сниппетах)
    if not html_content:
        html_content = snippet_data.get("value") or ""
    
    if not html_content or html_content == "":
        log.error("❌ Контент сниппета пуст. Проверьте структуру JSON в логах GitHub.")
        # Выводим структуру для отладки
        log.info(f"DEBUG JSON: {snippet_data}")
        return

    log.info(f"📤 Создание Internal Guide: '{name}' в папке {target_folder_id}")
    
    # Формируем payload в стиле вашего sync_bot.py
    payload = {
        "title": name,
        "body": html_content,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "parent_id": target_folder_id,
        "parent_type": "folder",
        "state": "published"
    }

    create_res = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    
    if create_res.status_code in [200, 201]:
        new_guide = create_res.json()
        log.info(f"✅ Гайд успешно создан! ID: {new_guide.get('id')}")
        log.info("ℹ️ Удаление сниппета пропущено (режим теста).")
    else:
        log.error(f"❌ Ошибка создания гайда: {create_res.status_code} {create_res.text}")

if __name__ == "__main__":
    migrate()
