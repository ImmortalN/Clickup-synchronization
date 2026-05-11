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

# ID папки для гайдов
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
# ПАРСЕР КОНТЕНТА
# ==============================

def parse_intercom_blocks(blocks):
    """Преобразование JSON-блоков в HTML"""
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
# ОСНОВНОЙ ПРОЦЕСС
# ==============================

def run_migration():
    log.info("🚀 Запуск полной миграции с удалением сниппетов...")
    
    url = f"{INTERCOM_BASE}/content_snippets"
    params = {"per_page": 50}
    
    processed_count = 0

    while True:
        res = ic.get(url, params=params)
        if res.status_code != 200:
            log.error(f"❌ Ошибка получения списка: {res.text}")
            break
            
        data = res.json()
        snippets = data.get("data", [])
        
        if not snippets:
            log.info("Список сниппетов пуст.")
            break

        for snip_summary in snippets:
            snippet_id = snip_summary["id"]
            
            # 1. Получаем полные данные сниппета
            full_res = ic.get(f"{INTERCOM_BASE}/content_snippets/{snippet_id}")
            if full_res.status_code != 200:
                log.error(f"⚠️ Ошибка получения ID {snippet_id}, пропускаем.")
                continue
                
            snip = full_res.json()
            title = snip.get("title") or snip.get("name") or "Untitled"
            json_blocks_raw = snip.get("json_blocks")
            
            if not json_blocks_raw:
                continue

            try:
                blocks = json.loads(json_blocks_raw)
                html_body = parse_intercom_blocks(blocks)
            except:
                log.error(f"❌ Ошибка парсинга JSON в сниппете '{title}'")
                continue

            # 2. Создаем гайд
            payload = {
                "title": title,
                "body": html_body,
                "owner_id": INTERCOM_OWNER_ID,
                "author_id": INTERCOM_AUTHOR_ID,
                "folder_id": int(TARGET_FOLDER_ID),
                "parent_id": int(TARGET_FOLDER_ID),
                "parent_type": "folder",
                "state": "published"
            }
            
            create_res = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
            
            if create_res.status_code in [200, 201]:
                processed_count += 1
                log.info(f"✅ [{processed_count}] Создан гайд: {title}")
                
                # 3. Удаляем сниппет после успешного создания гайда
                del_res = ic.delete(f"{INTERCOM_BASE}/content_snippets/{snippet_id}")
                
                if del_res.status_code == 204:
                    log.info(f"🗑️ Сниппет '{title}' удален.")
                elif del_res.status_code == 422 and "content_has_procedure_dependencies" in del_res.text:
                    log.warning(f"⚠️ Нельзя удалить '{title}': используется в Fin Procedures.")
                else:
                    log.error(f"❌ Ошибка удаления сниппета '{title}': {del_res.status_code}")
            else:
                log.error(f"❌ Ошибка создания гайда '{title}': {create_res.text}")
            
            time.sleep(0.3)

        # 4. Логика перехода к следующей странице (Cursor Pagination)
        pagination = data.get("pages", {})
        next_page = pagination.get("next")
        
        # В некоторых версиях API next это объект с starting_after, в других - прямая ссылка
        if isinstance(next_page, dict) and next_page.get("starting_after"):
            params["starting_after"] = next_page.get("starting_after")
            log.info(f"--- Переход к следующей странице (курсор: {params['starting_after']}) ---")
        elif isinstance(next_page, str) and next_page != "":
            # Если это прямая ссылка
            url = next_page
            params = {} # Параметры уже в ссылке
        else:
            log.info("🏁 Все сниппеты обработаны.")
            break

if __name__ == "__main__":
    run_migration()
