import os
import sys
import time
import html
import logging
import re
import requests
from markdown import markdown
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from datetime import datetime

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"

DEFAULT_FOLDER_ID = 4101985
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

# ==============================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==============================

def process_image_links(text: str) -> str:
    if not text:
        return text

    # Убираем markdown-ссылки
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()

        # === Imgur SINGLE + ALBUM (без API) ===
        if "imgur.com" in url:
            try:
                log.info(f"Обрабатываем Imgur: {url}")

                album_match = re.search(r'imgur\.com/(?:a|gallery)/([a-zA-Z0-9]+)', url)
                images = []

                if album_match:
                    album_hash = album_match.group(1)
                    candidates = [
                        f"https://i.imgur.com/{album_hash}.jpg",
                        f"https://i.imgur.com/{album_hash}.png",
                        f"https://i.imgur.com/{album_hash}_d.jpg",
                        f"https://i.imgur.com/{album_hash}_1.jpg",
                        f"https://i.imgur.com/{album_hash}_2.jpg",
                        f"https://i.imgur.com/{album_hash}_3.jpg",
                    ]
                    images.extend(candidates)

                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"
                    }
                    r = requests.get(url, timeout=15, headers=headers)
                    soup = BeautifulSoup(r.text, 'html.parser')

                    for tag in soup.find_all(['img', 'source', 'a']):
                        for attr in ['src', 'data-src', 'href', 'data-url']:
                            val = tag.get(attr)
                            if val and 'i.imgur.com' in val and val not in images:
                                if not val.startswith('http'):
                                    val = 'https:' + val if val.startswith('//') else val
                                images.append(val)
                else:
                    single_match = re.search(r'imgur\.com/([a-zA-Z0-9]{5,})', url)
                    if single_match:
                        h = single_match.group(1)
                        images.append(f"https://i.imgur.com/{h}.jpg")

                images = list(dict.fromkeys([u for u in images if u]))

                if images:
                    html_images = []
                    for img_url in images[:8]:
                        html_images.append(
                            f'<img src="{img_url}" style="max-width:100%; margin:12px 0; display:block;" alt="Screenshot from ClickUp">'
                        )
                    log.info(f"✅ Imgur: найдено и вставлено {len(images)} ссылок")
                    return "".join(html_images)

            except Exception as e:
                log.warning(f"Imgur error {url}: {e}")

            return url

        # === icecream.me ===
        if "icecream.me" in url:
            try:
                r_head = requests.head(url, timeout=10, allow_redirects=True)
                final_url = r_head.url
                if re.search(r'\.(png|jpe?g|gif|webp)', final_url.lower()):
                    return f'<img src="{final_url}" style="max-width:100%;">'

                r = requests.get(url, timeout=10)
                soup = BeautifulSoup(r.text, 'html.parser')
                img_tag = soup.find('img', src=re.compile(r'upload|cdn|images', re.I))
                if img_tag and img_tag.get('src'):
                    src = img_tag['src']
                    if not src.startswith('http'):
                        src = 'https://icecream.me' + (src if src.startswith('/') else '/' + src)
                    return f'<img src="{src}" style="max-width:100%;">'
            except Exception as e:
                log.warning(f"Icecream error {url}: {e}")
            return url

        # === snipboard, прямые ссылки, monosnap ===
        if "snipboard.io" in url and "i.snipboard.io" not in url:
            img_id = url.split('/')[-1]
            if img_id:
                return f'<img src="https://i.snipboard.io/{img_id}.jpg" style="max-width:100%;">'

        if re.search(r'\.(png|jpe?g|gif|webp|bmp)(\?.*)?$', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'

        if "monosnap.ai" in url or "take.ms" in url:
            current_url = url
            if "take.ms" in url:
                try:
                    r_head = requests.head(url, timeout=5, allow_redirects=True)
                    current_url = r_head.url
                except Exception:
                    pass
            match_id = re.search(r'/(?:file|direct)/([a-zA-Z0-9]+)', current_url)
            if match_id:
                img_id = match_id.group(1)
                api_url = f"https://api.monosnap.ai/file/download?id={img_id}"
                try:
                    r = requests.get(api_url, timeout=10, headers={"Referer": current_url})
                    if r.status_code == 200 and "api.monosnap.ai" not in r.url:
                        return f'<img src="{r.url}" style="max-width:100%;">'
                except Exception:
                    pass

        return url

    text = re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)
    return text


def get_clickup_task(task_id):
    try:
        r = cu.get(
            f"https://api.clickup.com/api/v2/task/{task_id}",
            params={"include_markdown_description": "true"},
            timeout=20
        )
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 404:
            return "DELETED"
        else:
            log.warning(f"ClickUp API {r.status_code} для задачи {task_id}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Ошибка получения задачи ClickUp {task_id}: {e}")
    return None


def find_article_by_task_id(task_id):
    """Ищет существующую статью в Intercom по ID задачи ClickUp в заголовке"""
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200:
            break
        data = r.json()
        articles = data.get("data", [])
        if not articles:
            break

        for art in articles:
            if f"[{task_id}]" in art.get("title", ""):
                return art

        if page >= data.get("pages", {}).get("total_pages", 1):
            break
        page += 1
        time.sleep(0.3)
    return None


def parse_timestamp(value):
    """Преобразует updated_at / date_updated в unix timestamp (секунды)"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        # ClickUp отдаёт миллисекунды, Intercom обычно секунды
        return value / 1000 if value > 1e12 else float(value)
    if isinstance(value, str):
        try:
            # ISO формат
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            try:
                return float(value)
            except Exception:
                return 0
    return 0


def sync_single_article(art, is_force=True):
    article_id = art["id"]
    title = art.get("title", "")
    current_folder = art.get("parent_id") or art.get("folder_id")

    match = re.search(r'\[([a-zA-Z0-9]+)\]$', title)
    if not match:
        return False

    task_id = match.group(1)
    task_data = get_clickup_task(task_id)

    if task_data == "DELETED":
        log.warning(f"🗑️ Задача {task_id} удалена. Чистим Intercom...")
        try:
            ic.delete(f"{INTERCOM_BASE}/internal_articles/{article_id}")
            log.info(f"✅ Статья {article_id} удалена")
        except Exception as e:
            log.error(f"Ошибка удаления статьи {article_id}: {e}")
        return True

    if not task_data:
        log.warning(f"⚠ Не удалось получить задачу ClickUp {task_id}, пропускаем")
        return False

    name = task_data.get("name") or ""
    desc = task_data.get("markdown_description") or task_data.get("description") or ""

    new_title = f"{name} [{task_id}]"[:255]
    body_content = markdown(process_image_links(desc), extensions=['fenced_code', 'nl2br', 'tables'])
    new_body = f"<h1>{html.escape(name)}</h1>{body_content}"

    should_update = is_force

    if not is_force:
        clickup_ts = parse_timestamp(task_data.get("date_updated"))
        intercom_ts = parse_timestamp(art.get("updated_at"))

        # Если задача в ClickUp обновлялась позже, чем статья в Intercom — обновляем
        if clickup_ts > intercom_ts + 10:  # небольшой буфер 10 сек
            should_update = True
            log.info(
                f"📅 ClickUp новее (CU: {datetime.fromtimestamp(clickup_ts).isoformat()} > "
                f"IC: {datetime.fromtimestamp(intercom_ts).isoformat() if intercom_ts else 'нет'}) → {name}"
            )
        else:
            # Fallback: сравнение контента
            title_same = art.get("title") == new_title
            body_same = art.get("body") == new_body

            if title_same and body_same:
                log.info(f"⏭ Пропущено (дата и контент без изменений): {name}")
                return False
            else:
                should_update = True
                reason = []
                if not title_same:
                    reason.append("title")
                if not body_same:
                    reason.append("body")
                log.info(f"📝 Контент отличается ({', '.join(reason)}) → обновляем: {name}")

    if should_update:
        log.info(f"🔄 Обновление: {name}")
        payload = {
            "title": new_title,
            "body": new_body[:100000],
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "folder_id": current_folder
        }
        try:
            r = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload, timeout=30)
            if r.status_code in (200, 201):
                log.info(f"✅ Успешно обновлено: {name}")
                return True
            else:
                log.error(f"❌ Ошибка обновления {article_id} ({r.status_code}): {r.text[:300]}")
                return False
        except Exception as e:
            log.error(f"❌ Исключение при обновлении {article_id}: {e}")
            return False

    return False


def create_or_update_by_clickup_id(task_id, target_folder_id=None):
    """Создает новую статью или обновляет существующую с проверкой ответа API"""
    task_data = get_clickup_task(task_id)
    if not task_data or task_data == "DELETED":
        log.error(f"❌ Ошибка: Задача ClickUp {task_id} не найдена.")
        return

    name = task_data.get("name") or ""
    desc = task_data.get("markdown_description") or task_data.get("description") or ""
    new_title = f"{name} [{task_id}]"[:255]
    body_content = markdown(process_image_links(desc), extensions=['fenced_code', 'nl2br', 'tables'])
    new_body = f"<h1>{html.escape(name)}</h1>{body_content}"
    folder_id = int(target_folder_id) if target_folder_id and str(target_folder_id).isdigit() else DEFAULT_FOLDER_ID

    payload = {
        "title": new_title,
        "body": new_body[:100000],
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": folder_id
    }

    existing_art = find_article_by_task_id(task_id)

    if existing_art:
        log.info(f"🔄 Обновление статьи {existing_art['id']}: {new_title}")
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{existing_art['id']}", json=payload, timeout=30)
        if r.status_code in (200, 201):
            log.info("✅ Успешно обновлено")
        else:
            log.error(f"❌ Ошибка API ({r.status_code}): {r.text[:300]}")
    else:
        log.info(f"✨ Создание новой статьи: {new_title}")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload, timeout=30)
        if r.status_code in (200, 201):
            log.info(f"✅ Успешно создано. ID: {r.json().get('id')}")
        else:
            log.error(f"❌ Ошибка при создании ({r.status_code}): {r.text[:300]}")


# ==============================
# ГЛАВНЫЙ ПРОЦЕСС
# ==============================

def main():
    # Аргументы: 1-FolderID, 2-IntercomArtIDs, 3-ClickUpTaskID
    target_folder = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else None
    specific_ids = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else None
    clickup_task_id = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3].strip() else None

    # Сценарий 1: Прямое создание/обновление по ID задачи ClickUp (всегда force)
    if clickup_task_id:
        log.info(f"--- РЕЖИМ ОДНОЙ ЗАДАЧИ CLICKUP: {clickup_task_id} ---")
        create_or_update_by_clickup_id(clickup_task_id, target_folder)
        return

    # Сценарий 2: Точечное обновление по ID статей Intercom (всегда force)
    if specific_ids:
        ids = [i.strip() for i in specific_ids.split(",")]
        log.info(f"--- РЕЖИМ ТОЧЕЧНОГО ОБНОВЛЕНИЯ INTERCOM: {len(ids)} шт. ---")
        for aid in ids:
            res = ic.get(f"{INTERCOM_BASE}/internal_articles/{aid}")
            if res.status_code == 200:
                sync_single_article(res.json(), is_force=True)
            else:
                log.error(f"Статья {aid} не найдена в Intercom.")
        return

    # Сценарий 3: Массовая синхронизация по папке
    # Проверка чётной недели только для автоматического (scheduled) запуска
    is_scheduled = os.getenv("IS_SCHEDULED", "").lower() in ("true", "1", "yes")

    if is_scheduled and not target_folder:
        week_number = datetime.now().isocalendar()[1]
        if week_number % 2 != 0:
            log.info("Сегодня нечетная неделя. Пропускаем автоматику.")
            return

    folder_to_scan = target_folder or str(DEFAULT_FOLDER_ID)

    # Полная синхронизация всегда использует умную проверку (date_updated + content)
    # force=True только в сценариях 1 и 2 выше
    is_force = False

    log.info(f"--- СТАРТ СИНХРОНИЗАЦИИ ПАПКИ (ID: {folder_to_scan}) | force={is_force} | scheduled={is_scheduled} ---")

    updated_count = 0
    skipped_count = 0
    page = 1

    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200:
            log.error(f"Ошибка получения списка статей Intercom: {r.status_code}")
            break

        data = r.json()
        articles = data.get("data", [])
        if not articles:
            break

        for art in articles:
            current_folder = str(art.get("parent_id") or art.get("folder_id") or "")
            if current_folder == folder_to_scan:
                result = sync_single_article(art, is_force=is_force)
                if result:
                    updated_count += 1
                else:
                    skipped_count += 1

        if page >= data.get("pages", {}).get("total_pages", 1):
            break
        page += 1
        time.sleep(0.5)

    log.info(f"--- СИНХРОНИЗАЦИЯ ЗАВЕРШЕНА | обновлено: {updated_count}, пропущено: {skipped_count} ---")


if __name__ == "__main__":
    main()
