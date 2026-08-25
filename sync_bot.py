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
from datetime import datetime, timezone

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

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"
}

SCREENSHOT_HOSTS = (
    "imgur.com", "icecream.me", "snipboard.io",
    "monosnap.ai", "take.ms",
)

# ==============================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==============================

def is_image_url(url: str) -> bool:
    if not url:
        return False
    return bool(re.search(r'\.(png|jpe?g|gif|webp|bmp)(\?.*)?$', url.lower()))


def is_screenshot_url(url: str) -> bool:
    return any(host in url for host in SCREENSHOT_HOSTS) or is_image_url(url)


def make_img_tag(src: str, alt: str = "Screenshot") -> str:
    return (
        f'<img src="{html.escape(src, quote=True)}" '
        f'style="max-width:100%; margin:12px 0; display:block;" '
        f'alt="{html.escape(alt)}">'
    )


def make_link_tag(url: str, text: str = None) -> str:
    """Кликабельная ссылка. text=None → показываем сам URL."""
    label = text if text is not None else url
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def fallback_link(url: str, link_text: str = None) -> str:
    """
    Если картинку получить не удалось:
    - был якорный текст → оставляем <a>текст</a>
    - была просто ссылка → оставляем ссылку с URL как текстом
    """
    if link_text:
        return make_link_tag(url, link_text)
    return make_link_tag(url, url)


def resolve_monosnap(url: str, link_text: str = None) -> str:
    """Пробуем <img>. При неудаче — исходная ссылка / якорный текст."""
    try:
        current_url = url

        if "take.ms" in url:
            try:
                r_head = requests.head(url, timeout=8, allow_redirects=True, headers=DEFAULT_HEADERS)
                current_url = r_head.url or url
                log.info(f"Monosnap short link: {url} → {current_url}")
            except Exception as e:
                log.warning(f"Monosnap take.ms resolve failed ({url}): {e}")

        match_id = re.search(r'/(?:file|direct)/([a-zA-Z0-9]+)', current_url)
        if not match_id:
            match_id = re.search(r'monosnap\.ai/(?:file|image)/([a-zA-Z0-9]+)', current_url)

        if match_id:
            img_id = match_id.group(1)
            api_url = f"https://api.monosnap.ai/file/download?id={img_id}"
            try:
                r = requests.get(
                    api_url,
                    timeout=15,
                    allow_redirects=True,
                    headers={**DEFAULT_HEADERS, "Referer": current_url},
                )
                final = (r.url or "").strip()

                if r.status_code == 200 and final and "api.monosnap.ai" not in final:
                    log.info(f"✅ Monosnap image: {final[:120]}")
                    return make_img_tag(final)

                if r.history:
                    last = r.history[-1].headers.get("Location") or final
                    if last and "api.monosnap.ai" not in last:
                        log.info(f"✅ Monosnap image (redirect): {last[:120]}")
                        return make_img_tag(last)

            except Exception as e:
                log.warning(f"Monosnap API failed ({img_id}): {e}")

        if is_image_url(current_url):
            return make_img_tag(current_url)

    except Exception as e:
        log.warning(f"Monosnap error {url}: {e}")

    log.info(f"Monosnap: оставляем исходную ссылку → {url}")
    return fallback_link(url, link_text)


def resolve_imgur(url: str, link_text: str = None) -> str:
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

            r = requests.get(url, timeout=15, headers=DEFAULT_HEADERS)
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
            html_images = [make_img_tag(img_url) for img_url in images[:8]]
            log.info(f"✅ Imgur: вставлено {len(images)} изображений")
            return "".join(html_images)

    except Exception as e:
        log.warning(f"Imgur error {url}: {e}")

    return fallback_link(url, link_text)


def resolve_icecream(url: str, link_text: str = None) -> str:
    try:
        r_head = requests.head(url, timeout=10, allow_redirects=True, headers=DEFAULT_HEADERS)
        final_url = r_head.url
        if is_image_url(final_url):
            return make_img_tag(final_url)

        r = requests.get(url, timeout=10, headers=DEFAULT_HEADERS)
        soup = BeautifulSoup(r.text, 'html.parser')
        img_tag = soup.find('img', src=re.compile(r'upload|cdn|images', re.I))
        if img_tag and img_tag.get('src'):
            src = img_tag['src']
            if not src.startswith('http'):
                src = 'https://icecream.me' + (src if src.startswith('/') else '/' + src)
            return make_img_tag(src)
    except Exception as e:
        log.warning(f"Icecream error {url}: {e}")
    return fallback_link(url, link_text)


def transform_url(url: str, link_text: str = None) -> str:
    """
    url — адрес
    link_text — якорный текст из [text](url), либо None если была голая ссылка
    """
    url = url.strip()

    if "imgur.com" in url:
        return resolve_imgur(url, link_text)

    if "icecream.me" in url:
        return resolve_icecream(url, link_text)

    if "snipboard.io" in url and "i.snipboard.io" not in url:
        img_id = url.rstrip('/').split('/')[-1]
        if img_id:
            return make_img_tag(f"https://i.snipboard.io/{img_id}.jpg")
        return fallback_link(url, link_text)

    if "monosnap.ai" in url or "take.ms" in url:
        return resolve_monosnap(url, link_text)

    if is_image_url(url):
        return make_img_tag(url)

    # Обычная ссылка (не скриншот)
    return fallback_link(url, link_text)


def process_image_links(text: str) -> str:
    """
    Правила:
    1) [текст](url) — обычная ссылка → <a href="url">текст</a>
    2) [текст](screenshot-url) — пробуем картинку; если нет → <a>текст</a>
    3) голый screenshot-url — пробуем картинку; если нет → <a>url</a>
    4) голый обычный url → <a>url</a>
    """
    if not text:
        return text

    # 1. Markdown-ссылки [text](url)
    def replace_md_link(match):
        link_text = match.group(1)
        url = match.group(2).strip()
        return transform_url(url, link_text=link_text)

    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', replace_md_link, text)

    # 2. Голые URL (не внутри уже созданных href/src)
    def replace_bare_url(match):
        return transform_url(match.group(0), link_text=None)

    text = re.sub(
        r'(?<![="\'>])(https?://[^\s\)\'\"<>]+)',
        replace_bare_url,
        text
    )

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
    if value is None:
        return 0.0

    try:
        num = float(value)
    except (TypeError, ValueError):
        num = None

    if num is not None:
        if num > 1e12:
            return num / 1000.0
        return num

    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            pass

    return 0.0


def format_ts(ts):
    if not ts:
        return "нет"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OSError, OverflowError):
        return f"invalid({ts})"


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

        if clickup_ts > intercom_ts + 10:
            should_update = True
            log.info(
                f"📅 ClickUp новее (CU: {format_ts(clickup_ts)} > IC: {format_ts(intercom_ts)}) → {name}"
            )
        else:
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


def main():
    target_folder = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else None
    specific_ids = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else None
    clickup_task_ids_raw = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3].strip() else None

    if clickup_task_ids_raw:
        task_ids = [tid.strip() for tid in clickup_task_ids_raw.split(",") if tid.strip()]
        log.info(f"--- РЕЖИМ ЗАДАЧ CLICKUP: {len(task_ids)} шт. → {task_ids} ---")
        for task_id in task_ids:
            log.info(f"\n=== Синхронизация задачи {task_id} ===")
            try:
                create_or_update_by_clickup_id(task_id, target_folder)
            except Exception as e:
                log.error(f"Ошибка при синхронизации {task_id}: {e}")
        return

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

    is_scheduled = os.getenv("IS_SCHEDULED", "").lower() in ("true", "1", "yes")

    if is_scheduled and not target_folder:
        week_number = datetime.now().isocalendar()[1]
        if week_number % 2 != 0:
            log.info("Сегодня нечетная неделя. Пропускаем автоматику.")
            return

    folder_to_scan = target_folder or str(DEFAULT_FOLDER_ID)
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
