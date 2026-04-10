import os
import time
import html
import logging
import re
import requests
from markdown import markdown
from dotenv import load_dotenv

# ==============================
# КОНФИГУРАЦИЯ
# ==============================
load_dotenv()

CLICKUP_TOKEN = os.getenv("CLICKUP_API_TOKEN")
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"

OLD_FOLDER_ID = 4101985  
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID"))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Проверка переменных окружения сразу
if not CLICKUP_TOKEN or not INTERCOM_TOKEN:
    log.error("КРИТИЧЕСКАЯ ОШИБКА: Токены не найдены в .env файле!")
    exit()

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
    if not text: return text
    # 1. Очищаем markdown-разметку, оставляя только голые URL
    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        original = url
        
        # --- СПЕЦОБРАБОТКА SNIPBOARD ---
        # Если ссылка со snipboard, мы принудительно добавляем 'i.' и '.jpg'
        # Это превращает страницу в прямую ссылку на файл, которую Intercom ест лучше
        if "snipboard.io" in url and "i.snipboard.io" not in url:
            # Вырезаем ID картинки (все что после последнего слеша)
            img_id = url.split('/')[-1]
            if img_id:
                direct_url = f"https://i.snipboard.io/{img_id}.jpg"
                return f'<img src="{direct_url}" style="max-width:100%;">'

        # --- ПРИОРИТЕТ 1: ПРЯМЫЕ ССЫЛКИ (уже с расширением) ---
        if re.search(r'\.(png|jpe?g|gif|webp|bmp)(\?.*)?$', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'

        # --- ПРИОРИТЕТ 2: MONOSNAP & TAKE.MS ---
        if "monosnap.ai" in url or "take.ms" in url:
            current_url = url
            if "take.ms" in url:
                try:
                    r_head = requests.head(url, timeout=5, allow_redirects=True)
                    current_url = r_head.url
                except: pass
            
            match_id = re.search(r'/(?:file|direct)/([a-zA-Z0-9]+)', current_url)
            if match_id:
                img_id = match_id.group(1)
                api_url = f"https://api.monosnap.ai/file/download?id={img_id}"
                try:
                    r = requests.get(api_url, timeout=10, headers={"Referer": current_url}, allow_redirects=True)
                    if r.status_code == 200 and "api.monosnap.ai" not in r.url:
                        return f'<img src="{r.url}" style="max-width:100%;">'
                except: pass
            return original

        # --- ПРИОРИТЕТ 3: ПАРСИНГ СТРАНИЦ (imgur, prnt.sc и т.д.) ---
        if any(x in url for x in ["imgur.com", "prnt.sc", "prntscr.com", "icecream.me"]):
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                r = requests.get(url, timeout=10, headers=headers)
                soup = BeautifulSoup(r.text, 'lxml')
                img = soup.find('meta', property="og:image") or soup.find('img', class_="no-click")
                src = img.get('content') if img and img.get('content') else (img.get('src') if img else None)
                if src: return f'<img src="{src}" style="max-width:100%;">'
            except: pass
        
        return original

    return re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)

def get_clickup_task_description(task_id):
    try:
        r = cu.get(f"https://api.clickup.com/api/v2/task/{task_id}", params={"include_markdown_description": "true"})
        if r.status_code == 200:
            data = r.json()
            return data.get("name"), data.get("markdown_description") or data.get("description") or ""
    except Exception as e:
        log.error(f"Ошибка ClickUp Task {task_id}: {e}")
    return None, None

# ==============================
# ГЛАВНЫЙ ПРОЦЕСС
# ==============================
def force_update():
    log.info("--- ЗАПУСК ДИАГНОСТИКИ И МИГРАЦИИ ---")
    log.info(f"Целевая папка ID: {OLD_FOLDER_ID}")
    
    page = 1
    total_processed = 0
    found_any_article = False
    
    while True:
        log.info(f"Запрос страницы {page} из Intercom...")
        try:
            r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        except Exception as e:
            log.error(f"Сбой сетевого запроса: {e}")
            break

        if r.status_code != 200:
            log.error(f"API Intercom вернул ошибку {r.status_code}: {r.text}")
            break
            
        data = r.json()
        articles = data.get("data", [])
        
        if not articles:
            log.info("Статьи на этой странице отсутствуют.")
            break
            
        found_any_article = True

        for art in articles:
            article_id = art["id"]
            title = art.get("title", "No Title")
            
            # В unstable ID папки может быть в разных полях
            raw_folder_id = art.get("parent_id") or art.get("folder_id")
            
            # ЛОГ ДЛЯ КАЖДОЙ СТАТЬИ (чтобы понять, почему не проходит фильтр)
            # log.info(f"Вижу статью: '{title}' | Folder ID в API: {raw_folder_id}")

            if raw_folder_id is None or str(raw_folder_id) != str(OLD_FOLDER_ID):
                continue

            log.info(f"🎯 СОВПАДЕНИЕ! Обрабатываем: {title}")
            
            match = re.search(r'\[([a-zA-Z0-9]+)\]$', title)
            if not match:
                log.warning(f"Пропуск: Нет [ID] в названии '{title}'")
                continue
                
            task_id = match.group(1)
            task_name, desc = get_clickup_task_description(task_id)
            
            if not task_name:
                log.warning(f"Задача {task_id} не найдена в ClickUp")
                continue
                
            header_html = f"<h1>{html.escape(task_name)}</h1>"
            main_content = markdown(process_image_links(desc), extensions=['fenced_code', 'nl2br', 'tables'])
            new_body = f"{header_html}{main_content}"
            
            payload = {
                "title": f"{task_name} [{task_id}]"[:255],
                "body": new_body[:50000],
                "owner_id": INTERCOM_OWNER_ID,
                "author_id": INTERCOM_AUTHOR_ID,
                "folder_id": OLD_FOLDER_ID 
            }
            
            upd = ic.put(f"{INTERCOM_BASE}/internal_articles/{article_id}", json=payload)
            if upd.status_code == 200:
                log.info(f"✅ Успешно обновлено: {task_name}")
                total_processed += 1
            else:
                log.error(f"❌ Ошибка обновления {article_id}: {upd.status_code} {upd.text}")

        if page >= data.get("pages", {}).get("total_pages", 1): 
            break
        page += 1
        time.sleep(0.5)

    if not found_any_article:
        log.warning("Intercom не вернул ни одной статьи. Проверь права доступа токена.")

    log.info(f"--- МИГРАЦИЯ ЗАВЕРШЕНА ---")
    log.info(f"Всего принудительно обновлено: {total_processed}")

if __name__ == "__main__":
    try:
        force_update()
    except Exception as e:
        log.error(f"Критическая ошибка выполнения скрипта: {e}")
