def process_image_links(text: str) -> str:
    if not text:
        return text

    text = re.sub(r'\[.*?\]\((https?://.*?)\)', r'\1', text)

    def transform_url(match):
        url = match.group(0).strip()
        original = url

        # ==================== MONOSNAP ====================
        if "monosnap.ai/file/" in url and "api.monosnap.ai" not in url:
            img_id = url.split('/')[-1]
            direct = f"https://api.monosnap.ai/file/download?id={img_id}"
            return f'<img src="{direct}" style="max-width:100%;">'

        # ==================== SNIPBOARD ====================
        if "snipboard.io/" in url:
            direct = url.replace("https://snipboard.io/", "https://i.snipboard.io/")
            return f'<img src="{direct}" style="max-width:100%;">'

        # ==================== ICECREAM ====================
        if "icecream.me/" in url and "/uploads/" not in url:
            direct = f"https://icecream.me/uploads/{url.split('/')[-1]}.png"
            return f'<img src="{direct}" style="max-width:100%;">'

        # ==================== IMGUR ====================
        if "imgur.com/" in url and "i.imgur.com" not in url:
            try:
                r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    img = soup.find('img', src=re.compile(r'i\.imgur\.com'))
                    if img and img.get('src'):
                        src = img['src']
                        if src.startswith('//'):
                            src = 'https:' + src
                        return f'<img src="{src}" style="max-width:100%;">'
            except:
                pass
            return original

        # ==================== PRNT.SC ====================
        if "prnt.sc/" in url or "prntscr.com/" in url:
            try:
                r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'lxml')
                    img = soup.find('img', class_="no-click") or soup.find('img', id="screenshot-image")
                    if img and img.get('src'):
                        src = img['src']
                        if src.startswith('//'):
                            src = 'https:' + src
                        return f'<img src="{src}" style="max-width:100%;">'
            except:
                pass

        # GitHub
        if "user-images.githubusercontent.com" in url:
            return f'<img src="{url}" style="max-width:100%;">'

        # tppr.me — пока оставляем как текст (можно потом доработать)
        if "tppr.me/" in url:
            return original

        # Остальные прямые
        if re.search(r'\.(png|jpe?g|gif|webp|bmp)', url.lower()):
            return f'<img src="{url}" style="max-width:100%;">'

        return original

    text = re.sub(r'https?://[^\s\)\'\"<>]+', transform_url, text)
    return text
