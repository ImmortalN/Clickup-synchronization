import os
import logging
import requests
from dotenv import load_dotenv

# ==== Настройки ====
load_dotenv()

INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "Unstable")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ==== Сессия ====
ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

# ==== Получение всех internal articles ====
def fetch_all_articles(query: str = ""):
    articles = []
    page = 1
    while True:
        params = {"query": query, "page": page}
        r = ic.get(f"{INTERCOM_BASE}/internal_articles/search", params=params)
        r.raise_for_status()
        data = r.json()
        batch = data.get("articles", []) or data.get("data", {}).get("internal_articles", [])
        if not batch:
            break
        articles.extend(batch)
        page += 1
    return articles

# ==== Удаление статьи ====
def delete_internal_article(article_id: str):
    if DRY_RUN:
        logging.info(f"[DRY_RUN] Would delete article {article_id}")
        return
    r = ic.delete(f"{INTERCOM_BASE}/internal_articles/{article_id}")
    if r.status_code == 200:
        logging.info(f"Deleted article {article_id}")
    else:
        logging.error(f"Failed to delete article {article_id}: {r.status_code} {r.text}")

# ==== Главная функция ====
def main():
    logging.info("Fetching all internal articles...")
    articles = fetch_all_articles()
    logging.info(f"Found {len(articles)} articles")
    
    for article in articles:
        article_id = article.get("id")
        title = article.get("title", "(Без названия)")
        logging.info(f"Deleting: {title} (ID: {article_id})")
        delete_internal_article(article_id)
    logging.info("Done.")

if __name__ == "__main__":
    main()
