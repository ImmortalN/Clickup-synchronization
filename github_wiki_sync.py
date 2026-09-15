import os, re, time, html, logging, requests
from markdown import markdown
from dotenv import load_dotenv

load_dotenv()
INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"
INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

TARGET_FOLDER_ID = 5654984   # ваша папка

# Стабильные ID + точные имена страниц wiki
WIKI_PAGES = [
    {"id": "wiki-PHP-Hooks", "slug": "PHP-Hooks", "title": "PHP Hooks"},
    {"id": "wiki-Custom-JS-for-fields", "slug": "Custom-JS-for-fields", "title": "Custom JS for fields"},
    {"id": "wiki-Frontend-Macros---External-Macros", "slug": "Frontend-Macros---External-Macros", "title": "Frontend Macros - External Macros"},
    {"id": "wiki-Frontend-Macros---Field-Attributes", "slug": "Frontend-Macros---Field-Attributes", "title": "Frontend Macros - Field Attributes"},
    {"id": "wiki-Frontend-Macros---Filters", "slug": "Frontend-Macros---Filters", "title": "Frontend Macros - Filters"},
    {"id": "wiki-JS-Hooks", "slug": "JS-Hooks", "title": "JS Hooks"},
    {"id": "wiki-Understanding-reactivity", "slug": "Understanding-reactivity", "title": "Understanding reactivity"},
]

ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

def get_wiki_markdown(slug: str) -> str | None:
    url = f"https://raw.githubusercontent.com/wiki/Crocoblock/jetformbuilder/{slug}.md"
    r = requests.get(url, timeout=20)
    if r.status_code == 200:
        return r.text
    logging.error(f"Не удалось получить {slug}: {r.status_code}")
    return None

def find_article_by_id(wiki_id: str):
    page = 1
    while True:
        r = ic.get(f"{INTERCOM_BASE}/internal_articles", params={"page": page, "per_page": 50})
        if r.status_code != 200:
            break
        data = r.json()
        for art in data.get("data", []):
            if f"[{wiki_id}]" in art.get("title", ""):
                return art
        if page >= data.get("pages", {}).get("total_pages", 1):
            break
        page += 1
        time.sleep(0.15)
    return None

def create_or_update(page: dict):
    md = get_wiki_markdown(page["slug"])
    if not md:
        return "error"

    body_html = markdown(md, extensions=["fenced_code", "nl2br", "tables"])
    new_title = f"{page['title']} [{page['id']}]"[:255]
    new_body = f"<h1>{html.escape(page['title'])}</h1>{body_html}"[:100000]

    existing = find_article_by_id(page["id"])

    if existing:
        payload = {
            "title": new_title,
            "body": new_body,
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "folder_id": existing.get("parent_id") or existing.get("folder_id") or TARGET_FOLDER_ID,
        }
        r = ic.put(f"{INTERCOM_BASE}/internal_articles/{existing['id']}", json=payload, timeout=30)
        if r.status_code in (200, 201):
            logging.info(f"✅ Обновлено: {page['title']}")
            return "updated"
        logging.error(f"❌ Ошибка обновления: {r.status_code} {r.text[:200]}")
        return "error"
    else:
        payload = {
            "title": new_title,
            "body": new_body,
            "owner_id": INTERCOM_OWNER_ID,
            "author_id": INTERCOM_AUTHOR_ID,
            "folder_id": TARGET_FOLDER_ID,
        }
        r = ic.post(f"{INTERCOM_BASE}/internal_articles", json=payload, timeout=30)
        if r.status_code in (200, 201):
            logging.info(f"✅ Создано: {page['title']}")
            return "created"
        logging.error(f"❌ Ошибка создания: {r.status_code} {r.text[:200]}")
        return "error"

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    stats = {"created": 0, "updated": 0, "error": 0}
    for page in WIKI_PAGES:
        result = create_or_update(page)
        stats[result if result in stats else "error"] += 1
        time.sleep(0.3)
    logging.info(f"Готово: {stats}")

if __name__ == "__main__":
    main()
