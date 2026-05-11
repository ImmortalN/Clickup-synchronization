import os
import logging
import requests
from dotenv import load_dotenv

# ==============================
# CONFIG
# ==============================
load_dotenv()

INTERCOM_TOKEN = os.getenv("INTERCOM_ACCESS_TOKEN")

INTERCOM_BASE = "https://api.intercom.io"
INTERCOM_VERSION = "Unstable"

SNIPPET_ID = 2806960
TARGET_FOLDER_ID = 2751260

INTERCOM_OWNER_ID = int(os.getenv("INTERCOM_OWNER_ID", 0))
INTERCOM_AUTHOR_ID = int(os.getenv("INTERCOM_AUTHOR_ID", 0))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

log = logging.getLogger(__name__)

# ==============================
# SESSION
# ==============================
ic = requests.Session()

ic.headers.update({
    "Authorization": f"Bearer {INTERCOM_TOKEN}",
    "Accept": "application/json",
    "Intercom-Version": INTERCOM_VERSION,
    "Content-Type": "application/json"
})

# ==============================
# HELPERS
# ==============================

def get_snippet(snippet_id):
    r = ic.get(f"{INTERCOM_BASE}/content_snippets/{snippet_id}")

    if r.status_code != 200:
        raise Exception(
            f"Failed to fetch snippet: {r.status_code} {r.text}"
        )

    return r.json()


def json_blocks_to_html(json_blocks):
    """
    VERY simplified converter.
    Handles:
    - paragraph
    - heading
    - list
    - code
    """

    html_parts = []

    for block in json_blocks:

        block_type = block.get("type")

        # Paragraph
        if block_type == "paragraph":
            text = block.get("text", "")
            html_parts.append(f"<p>{text}</p>")

        # Heading
        elif block_type == "heading":
            text = block.get("text", "")
            level = block.get("level", 2)
            html_parts.append(f"<h{level}>{text}</h{level}>")

        # Bullet list
        elif block_type == "list":
            items = block.get("items", [])

            list_html = "<ul>"

            for item in items:
                list_html += f"<li>{item}</li>"

            list_html += "</ul>"

            html_parts.append(list_html)

        # Code
        elif block_type == "code":
            code = block.get("text", "")
            html_parts.append(
                f"<pre><code>{code}</code></pre>"
            )

        # Fallback
        else:
            text = block.get("text")

            if text:
                html_parts.append(f"<p>{text}</p>")

    return "\n".join(html_parts)


def create_internal_article(title, body_html):
    payload = {
        "title": title,
        "body": body_html,
        "folder_id": TARGET_FOLDER_ID,
        "owner_id": INTERCOM_OWNER_ID,
        "author_id": INTERCOM_AUTHOR_ID
    }

    r = ic.post(
        f"{INTERCOM_BASE}/internal_articles",
        json=payload
    )

    if r.status_code not in [200, 201]:
        raise Exception(
            f"Failed to create article: "
            f"{r.status_code} {r.text}"
        )

    return r.json()


def delete_snippet(snippet_id):
    r = ic.delete(
        f"{INTERCOM_BASE}/content_snippets/{snippet_id}"
    )

    if r.status_code == 204:
        log.info(f"Snippet {snippet_id} deleted")
        return

    raise Exception(
        f"Failed to delete snippet: "
        f"{r.status_code} {r.text}"
    )

# ==============================
# MAIN
# ==============================

def main():

    log.info(f"Fetching snippet {SNIPPET_ID}")

    snippet = get_snippet(SNIPPET_ID)

    title = snippet.get("title", "Untitled")

    json_blocks = snippet.get("json_blocks", [])

    log.info("Converting json_blocks to HTML")

    body_html = json_blocks_to_html(json_blocks)

    log.info("Creating internal article")

    article = create_internal_article(
        title=title,
        body_html=body_html
    )

    log.info(
        f"Created article ID: {article.get('id')}"
    )

    log.info("Deleting original snippet")

    delete_snippet(SNIPPET_ID)

    log.info("Done")


if __name__ == "__main__":
    main()
