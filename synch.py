import os
import time
import json
import logging
from dotenv import load_dotenv
import requests

load_dotenv()

INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")
INTERCOM_BASE = os.getenv("INTERCOM_REGION", "https://api.intercom.io").rstrip("/")
INTERCOM_VERSION = os.getenv("INTERCOM_VERSION", "Unstable")

# Логирование
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Сессия
ic = requests.Session()
ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})
ic.timeout = 15

def rate_limit_sleep(resp):
    if resp.status_code == 429:
        wait = int(resp.headers.get("Retry-After", "10"))
        log.warning(f"Rate limited → sleep {wait}s")
        time.sleep(wait)
        return True
    return False

def test_pagination():
    base_url = f"{INTERCOM_BASE}/internal_articles"
    params = {"per_page": 25}
    page_num = 1
    total_articles = 0

    while True:
        log.info(f"Запрос страницы {page_num} | params: {params}")

        r = ic.get(base_url, params=params)
        while rate_limit_sleep(r):
            r = ic.get(base_url, params=params)

        if r.status_code != 200:
            log.error(f"Ошибка {r.status_code}: {r.text}")
            break

        data = r.json()

        # Выводим структуру ответа (можно закомментировать, когда надоест)
        # log.debug(json.dumps(data, indent=2, ensure_ascii=False))

        articles = data.get("data", [])
        page_count = len(articles)
        total_articles += page_count

        log.info(f"Страница {page_num}: {page_count} статей | всего загружено: {total_articles}")

        # Пагинация
        pages = data.get("pages", {})
        next_cursor = None

        if pages and "next" in pages:
            next_obj = pages["next"]
            if isinstance(next_obj, dict) and "starting_after" in next_obj:
                next_cursor = next_obj["starting_after"]
            elif isinstance(next_obj, str) and next_obj:
                next_cursor = next_obj

        if next_cursor:
            log.info(f"Найден cursor для следующей страницы: {next_cursor[:40]}...")  # укорачиваем для читаемости
            params = {"per_page": 100, "starting_after": next_cursor}
            page_num += 1
            time.sleep(2)  # пауза от rate limit
        else:
            log.info("Cursor не найден → конец пагинации")
            log.info(f"Итого загружено статей: {total_articles}")
            if "total_count" in data:
                log.info(f"API сообщает total_count: {data['total_count']}")
            break

if __name__ == "__main__":
    test_pagination()
