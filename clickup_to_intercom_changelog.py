import os
import html
import logging
import requests
import time
import sys
import re
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"
DEFAULT_FOLDER_ID = 4725328
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

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


def parse_task_name(name: str):
    name = name.strip()
    match = re.match(r'^([A-Za-z][A-Za-z0-9\s]*?)\s*(?:v)?(\d+\.\d+(?:\.\d+)?)', name, re.IGNORECASE)
    if match:
        product = match.group(1).strip()
        version = match.group(2)
        return product, version
    return name, None


def get_all_tasks_from_source(source_id):
    all_tasks = []
    page = 0
    max_pages = 50

    while page < max_pages:
        params = {
            "subtasks": "false",
            "include_closed": "true",
            "order_by": "created",
            "reverse": "true",
            "page": page,
            "include_timl": "true"
        }

        page_has_tasks = False
        for endpoint in [f"list/{source_id}/task", f"view/{source_id}/task"]:
            url = f"https://api.clickup.com/api/v2/{endpoint}"
            try:
                r = cu.get(url, params=params)
                if r.status_code == 200:
                    tasks = r.json().get("tasks", [])
                    if tasks:
                        all_tasks.extend(tasks)
                        log.info(f"✅ Получено {len(tasks)} задач (страница {page})")
                        page_has_tasks = True
                    break
            except Exception as e:
                log.error(f"Ошибка {endpoint}: {e}")

        if not page_has_tasks:
            break
        page += 1
        time.sleep(0.8)

    # Жёсткая сортировка: самые новые сверху
    all_tasks.sort(key=lambda t: int(t.get("date_created") or 0), reverse=True)
    
    log.info(f"✅ Всего задач: {len(all_tasks)} (отсортировано по дате, новые сверху)")
    return all_tasks


def get_existing_articles_in_folder(folder_id):
    existing = {}
    page = 1
    target_folder_str = str(folder_id)

    log.info(f"Загружаем существующие гайды...")

    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200:
            log.error(f"Intercom error: {r.status_code}")
            break

        articles = r.json().get("data", [])
        if not articles:
            break

        for art in articles:
            if str(art.get("folder_id") or "") == target_folder_str:
                title = art.get("title", "").strip()
                existing[title.lower()] = art

        if page >= r.json().get("pages", {}).get("total_pages", 1):
            break
        page += 1
        time.sleep(0.2)

    log.info(f"✅ Найдено гайдов: {len(existing)}")
    return existing


def get_latest_body(article_id):
    r = ic.get(f"{INTERCOM_BASE}/internal_articles/{article_id}")
    return r.json().get('body', '') if r.status_code == 200 else ''


def build_release_section(version: str, date_str: str, subtasks):
    lines = [f"<h2>{version} ({date_str})</h2>"]
    if subtasks:
        lines.append("<ul>")
        for sub in subtasks:
            sub_name = html.escape(sub.get("name", "").strip())
            if sub_name:
                lines.append(f"  <li>{sub_name}</li>")
        lines.append("</ul>")
    lines.append("<br>")
    return "\n".join(lines)


def rebuild_changelog(article_id: str, releases: list):
    """Полностью перестраивает гайд с правильной сортировкой (новые сверху)"""
    current_body = get_latest_body(article_id)
    
    # Извлекаем заголовок
    if "<h1>" in current_body:
        h1_part = current_body.split("</h1>", 1)[0] + "</h1>"
    else:
        h1_part = "<h1>Changelog</h1>"

    # Собираем все секции
    sections = [h1_part + "\n\n"]
    for version, date_str, subtasks in releases:
        sections.append(build_release_section(version, date_str, subtasks))

    new_body = "".join(sections)
    
    r = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json={"body": new_body[:50000]})
    if r.status_code in (200, 201, 204):
        log.info(f"✅ Гайд полностью обновлён ({len(releases)} релизов, новые сверху)")
        return True
    else:
        log.error(f"❌ Ошибка обновления: {r.status_code}")
        return False


def process_release_task(main_task, product_releases):
    name = main_task.get("name", "").strip()
    if not name:
        return

    product, version = parse_task_name(name)
    subtasks = main_task.get("subtasks", [])
    date_created_ms = main_task.get("date_created")
    date_str = datetime.fromtimestamp(int(date_created_ms) / 1000).strftime('%d.%m.%Y') if date_created_ms else ""

    if version:
        product_releases[product].append((version, date_str, subtasks))
    else:
        # Отдельный гайд для задач без версии
        log.info(f"Создаём отдельный гайд: {name}")
        body = f"<h1>{html.escape(name)}</h1>"
        if subtasks:
            body += "<ul>" + "".join(f"<li>{html.escape(s.get('name',''))}</li>" for s in subtasks if s.get('name')) + "</ul>"
        payload = {
            "title": name,
            "body": body,
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "folder_id": DEFAULT_FOLDER_ID
        }
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
        if r.status_code in (200, 201):
            log.info("✅ Отдельный гайд создан")


def main():
    folder_id = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_FOLDER_ID)
    source_id = sys.argv[2] if len(sys.argv) > 2 else "8cjzjmb-30872"

    current_folder = int(folder_id) if str(folder_id).isdigit() else DEFAULT_FOLDER_ID

    log.info(f"🚀 Запуск синхронизации Changelog'ов | Folder: {current_folder} | Source: {source_id}")

    tasks = get_all_tasks_from_source(source_id)
    if not tasks:
        log.error("Нет задач")
        return

    existing_articles = get_existing_articles_in_folder(current_folder)

    # Группируем релизы по продукту
    product_releases = defaultdict(list)

    for task in tasks:
        full_task = get_clickup_task(task["id"])
        if full_task:
            process_release_task(full_task, product_releases)
            time.sleep(0.8)

    # Обновляем каждый changelog один раз
    for product, releases in product_releases.items():
        if not releases:
            continue
            
        # Сортируем: новые версии сверху (по дате)
        releases.sort(key=lambda x: datetime.strptime(x[1], '%d.%m.%Y'), reverse=True)
        
        changelog_title = f"{product} Changelog"
        clean_key = changelog_title.lower()

        article_id, _, _ = get_or_create_changelog(product, existing_articles)  # создаст если нет
        if article_id:
            rebuild_changelog(article_id, releases)

    log.info("🎉 Синхронизация завершена!")


def get_clickup_task(task_id):
    r = cu.get(f"https://api.clickup.com/api/v2/task/{task_id}",
               params={"subtasks": "true", "include_subtasks": "true"})
    return r.json() if r.status_code == 200 else None


def get_or_create_changelog(product: str, existing_articles):
    changelog_title = f"{product} Changelog"
    clean_key = changelog_title.lower()

    if clean_key in existing_articles:
        art = existing_articles[clean_key]
        log.info(f"✅ Обновляем существующий → {changelog_title}")
        return art['id'], art.get('body', ''), art

    log.info(f"Создаём новый changelog → {changelog_title}")
    payload = {
        "title": changelog_title,
        "body": f"<h1>{changelog_title}</h1>",
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": DEFAULT_FOLDER_ID
    }
    r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
    if r.status_code in (200, 201):
        data = r.json()
        existing_articles[clean_key] = data
        return data['id'], data.get('body', ''), data
    return None, None, None


if __name__ == "__main__":
    main()
