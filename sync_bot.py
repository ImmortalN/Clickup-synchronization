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
        original_url = url

        # === Imgur SINGLE ===
        if "imgur.com" in url and "/a/" not in url and "/gallery/" not in url:
            img_id_match = re.search(r'imgur\.com/([a-zA-Z0-9]+)', url)
            if img_id_match:
                direct = f"https://i.imgur.com/{img_id_match.group(1)}.jpg"
                log.debug(f"Imgur single → {direct}")
                return f'<img src="{direct}" style="max-width:100%;">'

        # === Imgur ALBUM / GALLERY ===
        if "imgur.com/a/" in url or "imgur.com/gallery/" in url:
            try:
                log.info(f"Обрабатываем Imgur альбом: {url}")
                r = requests.get(url, timeout=12, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                if r.status_code != 200:
                    return url

                soup = BeautifulSoup(r.text, 'html.parser')

                # Основные селекторы для Imgur альбомов
                images = []
                # Вариант 1: data-src / src в img
                for img in soup.find_all('img', attrs={'data-src': True}):
                    src = img.get('data-src') or img.get('src')
                    if src and 'i.imgur.com' in src:
                        images.append(src)

                # Вариант 2: ссылки с i.imgur.com
                for link in soup.find_all('a', href=True):
                    href = link.get('href')
                    if href and 'i.imgur.com' in href and re.search(r'\.(jpg|png|gif|webp)', href):
                        images.append(href)

                # Убираем дубликаты
                images = list(dict.fromkeys(images))

                if images:
                    html_images = []
                    for img_url in images[:8]:  # лимит 8 картинок, чтобы не раздуть статью
                        if not img_url.startswith('http'):
                            img_url = 'https:' + img_url if img_url.startswith('//') else img_url
                        html_images.append(f'<img src="{img_url}" style="max-width:100%; margin: 10px 0;">')
                    
                    log.info(f"Найдено {len(images)} картинок в альбоме")
                    return ''.join(html_images)  # возвращаем все картинки сразу

            except Exception as e:
                log.warning(f"Не удалось распарсить Imgur альбом {url}: {e}")

            return url  # fallback

        # === icecream.me (уже работает) ===
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

        # === Остальные обработчики (snipboard, прямые ссылки, monosnap) ===
        if "snipboard.io" in url and "i.snipboard.io" not in url:
            img_id = url.split('/')[-1]
            if img_id:
                return f'<img src="https://i.snipboard.io/{img_id}.jpg" style="max-width:100%;">'

        if re.search(r'\.(png|jpe?g|gif|webp|bmp)(\?.*)?$', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'

        if "monosnap.ai" in url or "take.ms" in url:
            # ... твой старый код monosnap ...
            current_url = url
            if "take.ms" in url:
                try:
                    r_head = requests.head(url, timeout=5, allow_redirects=True)
                    current_url = r_head.url
                except:
                    pass
            match_id = re.search(r'/(?:file|direct)/([a-zA-Z0-9]+)', current_url)
            if match_id:
                img_id = match_id.group(1)
                api_url = f"https://api.monosnap.ai/file/download?id={img_id}"
                try:
                    r = requests.get(api_url, timeout=10, headers={"Referer": current_url})
                    if r.status_code == 200 and "api.monosnap.ai" not in r.url:
                        return f'<img src="{r.url}" style="max-width:100%;">'
                except:
                    pass

        return url

    text = re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)
    return text

def get_clickup_task(task_id):
    try:
        r = cu.get(f"https://api.clickup.com/api/v2/task/{task_id}", params={"include_markdown_description": "true"})
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 404:
            return "DELETED"
    except: pass
    return None

def find_article_by_task_id(task_id):
    """Ищет существующую статью в Intercom по ID задачи ClickUp в заголовке"""
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200: break
        data = r.json()
        articles = data.get("data", [])
        if not articles: break
        
        for art in articles:
            if f"[{task_id}]" in art.get("title", ""):
                return art
        
        if page >= data.get("pages", {}).get("total_pages", 1): break
        page += 1
        time.sleep(0.3)
    return None

def sync_single_article(art, is_force=True):
    article_id = art["id"]
    title = art.get("title", "")
    current_folder = art.get("parent_id") or art.get("folder_id")
    
    match = re.search(r'\[([a-zA-Z0-9]+)\]$', title)
    if not match: return False
    
    task_id = match.group(1)
    task_data = get_clickup_task(task_id)

    if task_data == "DELETED":
        log.warning(f"🗑️ Задача {task_id} удалена. Чистим Intercom...")
        ic.delete(f"{INTERCOM_BASE}/internal_articles/{article_id}")
        return True

    if task_data:
        name = task_data.get("name")
        desc = task_data.get("markdown_description") or task_data.get("description") or ""
        
        new_title = f"{name} [{task_id}]"[:255]
        body_content = markdown(process_image_links(desc), extensions=['fenced_code', 'nl2br', 'tables'])
        new_body = f"<h1>{html.escape(name)}</h1>{body_content}"

        if not is_force:
            if art.get("title") == new_title and art.get("body") == new_body:
                return False

        log.info(f"🔄 Обновление: {name}")
        payload = {
            "title": new_title,
            "body": new_body[:100000],
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "folder_id": current_folder
        }
        ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
        return True
    return False

def create_or_update_by_clickup_id(task_id, target_folder_id=None):
    """Создает новую статью или обновляет существующую с проверкой ответа API"""
    task_data = get_clickup_task(task_id)
    if not task_data or task_data == "DELETED":
        log.error(f"❌ Ошибка: Задача ClickUp {task_id} не найдена.")
        return

    name = task_data.get("name")
    desc = task_data.get("markdown_description") or task_data.get("description") or ""
    new_title = f"{name} [{task_id}]"[:255]
    body_content = markdown(process_image_links(desc), extensions=['fenced_code', 'nl2br', 'tables'])
    new_body = f"<h1>{html.escape(name)}</h1>{body_content}"
    folder_id = int(target_folder_id) if target_folder_id and str(target_folder_id).isdigit() else DEFAULT_FOLDER_ID

    payload = {
        "title": new_title,
        "body": new_body[:50000],
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID,
        "folder_id": folder_id
    }

    existing_art = find_article_by_task_id(task_id)
    
    if existing_art:
        log.info(f"🔄 Обновление статьи {existing_art['id']}: {new_title}")
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{existing_art['id']}", json=payload)
        if r.status_code in [200, 201]:
            log.info("✅ Успешно обновлено")
        else:
            log.error(f"❌ Ошибка API ({r.status_code}): {r.text}")
    else:
        log.info(f"✨ Создание новой статьи: {new_title}")
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload)
        if r.status_code in [200, 201]:
            log.info(f"✅ Успешно создано. ID: {r.json().get('id')}")
        else:
            log.error(f"❌ Ошибка при создании ({r.status_code}): {r.text}")

# ==============================
# ГЛАВНЫЙ ПРОЦЕСС
# ==============================

def main():
    # Аргументы: 1-FolderID, 2-IntercomArtIDs, 3-ClickUpTaskID
    target_folder = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else None
    specific_ids = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else None
    clickup_task_id = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3].strip() else None

    # Сценарий 1: Прямое создание/обновление по ID задачи ClickUp
    if clickup_task_id:
        log.info(f"--- РЕЖИМ ОДНОЙ ЗАДАЧИ CLICKUP: {clickup_task_id} ---")
        create_or_update_by_clickup_id(clickup_task_id, target_folder)
        return

    # Сценарий 2: Точечное обновление по ID статей Intercom
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
    if not target_folder:
        week_number = datetime.now().isocalendar()[1]
        if week_number % 2 != 0:
            log.info("Сегодня нечетная неделя. Пропускаем автоматику.")
            return

    folder_to_scan = target_folder or str(DEFAULT_FOLDER_ID)
    is_force = target_folder is not None 
    
    log.info(f"--- СТАРТ СИНХРОНИЗАЦИИ ПАПКИ (ID: {folder_to_scan}) ---")

    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200: break
        
        data = r.json()
        articles = data.get("data", [])
        if not articles: break

        for art in articles:
            current_folder = str(art.get("parent_id") or art.get("folder_id") or "")
            if current_folder == folder_to_scan:
                sync_single_article(art, is_force=is_force)

        if page >= data.get("pages", {}).get("total_pages", 1): break
        page += 1
        time.sleep(0.5)

    log.info("--- СИНХРОНИЗАЦИЯ ЗАВЕРШЕНА ---")

if __name__ == "__main__":
    main()
