"""
fetch_sources.py
Reads URLs from inputs/daily_sources.txt, extracts article titles and links,
and saves structured output to outputs/raw_sources.txt.
"""

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

INPUT_FILE = "inputs/daily_sources.txt"
OUTPUT_FILE = "outputs/raw_sources.txt"
MAX_ITEMS = 8


def get_source_name(url):
    """Derive a readable source name from the domain."""
    domain = urlparse(url).netloc.replace("www.", "")
    # Use the first part of the domain, title-cased
    name = domain.split(".")[0].replace("-", " ").title()
    # Fix known names
    overrides = {
        "Techcrunch": "TechCrunch",
        "Producthunt": "Product Hunt",
        "A16Z": "a16z",
        "Lennysnewsletter": "Lenny's Newsletter",
        "Theverge": "The Verge",
        "Pragmaticengineer": "Pragmatic Engineer",
    }
    return overrides.get(name, name)


def fetch_articles(url):
    """Fetch a listing page and extract article titles, URLs, and summaries."""
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    articles = []
    seen_urls = set()

    # Find article links via headings (h1/h2/h3 containing or wrapped by <a>)
    candidates = []
    for heading in soup.find_all(["h1", "h2", "h3"]):
        a = heading.find("a", href=True) or (
            heading.parent if heading.parent and heading.parent.name == "a" else None
        )
        if a and a.get("href"):
            candidates.append((heading.get_text(strip=True), a["href"], heading))

    # Fallback: any <a> with substantial link text
    if not candidates:
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if len(text) > 30:
                candidates.append((text, a["href"], a))

    for title, href, tag in candidates:
        if not title or len(title) < 15:
            continue

        article_url = urljoin(base_url, href)

        # Skip non-article URLs
        if article_url in seen_urls:
            continue
        if not article_url.startswith("http") or "#" in href:
            continue

        seen_urls.add(article_url)

        # Look for a nearby excerpt in parent elements
        summary = ""
        parent = tag.parent
        for _ in range(4):
            if parent is None:
                break
            p = parent.find("p")
            if p:
                text = p.get_text(strip=True)
                if len(text) > 40:
                    summary = text[:220] + ("..." if len(text) > 220 else "")
                    break
            parent = parent.parent

        articles.append({"title": title, "url": article_url, "summary": summary})

        if len(articles) >= MAX_ITEMS:
            break

    return articles


def format_block(name, source_url, articles):
    """Format one source block in readable structured text."""
    lines = [f"SOURCE: {name}", ""]
    if not articles:
        lines.append("  (No articles found)")
    for item in articles:
        lines.append(f"- Title: {item['title']}")
        lines.append(f"  URL: {item['url']}")
        if item["summary"]:
            lines.append(f"  Summary: {item['summary']}")
        lines.append("")
    return "\n".join(lines)


def main():
    with open(INPUT_FILE, "r") as f:
        urls = [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]

    if not urls:
        print("No URLs found in", INPUT_FILE)
        return

    blocks = []

    for url in urls:
        name = get_source_name(url)
        print(f"Fetching {name} ...")
        try:
            articles = fetch_articles(url)
            blocks.append(format_block(name, url, articles))
            print(f"  {len(articles)} articles extracted")
        except Exception as e:
            blocks.append(f"SOURCE: {name}\n\n  ERROR: {e}\n")
            print(f"  Failed: {e}")

    separator = "\n" + "-" * 60 + "\n\n"
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(separator.join(blocks))

    print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
