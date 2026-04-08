"""
fetch_sources.py
Hybrid news fetcher for PMBuddy Daily Digest.

Sources:
  - RSS feeds       → Tech News (TechCrunch, The Verge, Ars Technica)
  - HackerNews API  → Tech / startup community signals (no key needed)
  - Dev.to API      → PM & product articles (no key needed)
  - Product Hunt API → Product launches (requires PRODUCTHUNT_API_KEY in daily_sources.txt)

Output: outputs/raw_sources.txt (same format as before — nothing downstream changes)
"""

import re
import requests
import feedparser
from datetime import datetime, timezone

SOURCES_FILE = "inputs/daily_sources.txt"
OUTPUT_FILE = "outputs/raw_sources.txt"

MAX_PER_SOURCE = 10  # articles per source block

SEPARATOR = "\n" + "-" * 60 + "\n\n"


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config():
    """
    Parse daily_sources.txt.
    Returns:
      rss_tech    — list of RSS feed URLs under # Tech News
      rss_pm      — list of RSS feed URLs under # Product & PM
      rss_blogs   — list of RSS feed URLs under # Company Blogs / Engineering
      ph_key      — Product Hunt API key (or None)
    """
    rss_tech, rss_pm, rss_other = [], [], []
    ph_key = None
    current_section = None

    tech_sections = {"tech news", "engineering / systems", "ux / growth"}
    pm_sections = {"product & pm"}
    blog_sections = {"company blogs", "product launches"}

    try:
        with open(SOURCES_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("PRODUCTHUNT_API_KEY="):
                    ph_key = line.split("=", 1)[1].strip()
                    continue
                if line.startswith("#"):
                    current_section = line.lstrip("#").strip().lower()
                    continue
                if line.startswith("http"):
                    if current_section in tech_sections:
                        rss_tech.append(line)
                    elif current_section in pm_sections:
                        rss_pm.append(line)
                    else:
                        rss_other.append(line)
    except FileNotFoundError:
        print(f"Warning: {SOURCES_FILE} not found — using defaults")

    return rss_tech, rss_pm, rss_other, ph_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def source_name_from_url(url):
    """Derive a readable name from a feed URL."""
    domain = re.sub(r"https?://(www\.)?", "", url).split("/")[0]
    overrides = {
        "techcrunch.com": "TechCrunch",
        "theverge.com": "The Verge",
        "feeds.arstechnica.com": "Ars Technica",
        "arstechnica.com": "Ars Technica",
        "wired.com": "Wired",
        "lennysnewsletter.com": "Lenny's Newsletter",
        "mindtheproduct.com": "Mind The Product",
        "review.firstround.com": "First Round Review",
        "blog.pragmaticengineer.com": "Pragmatic Engineer",
        "medium.com": "Netflix Tech Blog",
        "producttalk.org": "Product Talk",
        "engineering.atspotify.com": "Spotify Engineering",
        "stripe.com": "Stripe Blog",
        "shopify.engineering": "Shopify Engineering",
        "openai.com": "OpenAI Blog",
        "a16z.com": "a16z",
        "nngroup.com": "Nielsen Norman Group",
    }
    return overrides.get(domain, domain.split(".")[0].title())


def clean_html(text):
    """Strip HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def truncate(text, limit=220):
    text = text.strip()
    return text[:limit] + "..." if len(text) > limit else text


def format_block(source_name, articles):
    """Render one source block in the raw_sources.txt format."""
    lines = [f"SOURCE: {source_name}", ""]
    if not articles:
        lines.append("  (No articles found)")
    for a in articles:
        lines.append(f"- Title: {a['title']}")
        lines.append(f"  URL: {a['url']}")
        if a.get("date"):
            lines.append(f"  Date: {a['date']}")
        if a.get("summary"):
            lines.append(f"  Summary: {a['summary']}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_rss(feed_url, max_items=MAX_PER_SOURCE):
    """Parse an RSS/Atom feed and return structured articles."""
    feed = feedparser.parse(feed_url)
    articles = []
    for entry in feed.entries[:max_items]:
        title = clean_html(entry.get("title", "")).strip()
        url = entry.get("link", "")
        summary = clean_html(entry.get("summary", "") or entry.get("description", ""))
        summary = truncate(summary)

        # Parse date
        date = ""
        if entry.get("published_parsed"):
            try:
                date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                pass

        if title and url:
            articles.append({"title": title, "url": url, "summary": summary, "date": date})

    return articles


def fetch_hackernews(max_items=MAX_PER_SOURCE):
    """Fetch top stories from the official HackerNews Firebase API."""
    articles = []
    try:
        ids = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=10
        ).json()

        for story_id in ids[:30]:  # check top 30 to get max_items with URLs
            if len(articles) >= max_items:
                break
            try:
                item = requests.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json",
                    timeout=8
                ).json()
                if not item or item.get("type") != "story":
                    continue
                url = item.get("url", "")
                title = item.get("title", "").strip()
                if not url or not title:
                    continue  # skip Ask HN / self-posts without URLs
                score = item.get("score", 0)
                articles.append({
                    "title": title,
                    "url": url,
                    "summary": f"{score} points on Hacker News",
                    "date": datetime.today().strftime("%Y-%m-%d"),
                })
            except Exception:
                continue
    except Exception as e:
        print(f"  HackerNews API failed: {e}")

    return articles


def fetch_devto(tags=("product-management", "startup", "growth"), max_items=MAX_PER_SOURCE):
    """Fetch recent articles from Dev.to filtered by PM-relevant tags."""
    articles = []
    seen = set()
    for tag in tags:
        if len(articles) >= max_items:
            break
        try:
            resp = requests.get(
                "https://dev.to/api/articles",
                params={"tag": tag, "per_page": 10, "top": 1},
                timeout=10
            )
            resp.raise_for_status()
            for item in resp.json():
                if len(articles) >= max_items:
                    break
                url = item.get("url", "")
                title = item.get("title", "").strip()
                if not url or not title or url in seen:
                    continue
                seen.add(url)
                articles.append({
                    "title": title,
                    "url": url,
                    "summary": truncate(item.get("description", "")),
                    "date": (item.get("published_at") or "")[:10],
                })
        except Exception as e:
            print(f"  Dev.to ({tag}) failed: {e}")

    return articles


def fetch_producthunt(api_key, max_items=MAX_PER_SOURCE):
    """Fetch today's top products from Product Hunt GraphQL API."""
    if not api_key or api_key == "your_key_here":
        print("  Product Hunt: no API key configured, skipping.")
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    query = """
    {
      posts(first: %d, order: VOTES, postedAfter: "%s") {
        edges {
          node {
            name
            tagline
            url
            votesCount
            createdAt
          }
        }
      }
    }
    """ % (max_items, today)

    try:
        resp = requests.post(
            "https://api.producthunt.com/v2/api/graphql",
            json={"query": query},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        edges = data.get("data", {}).get("posts", {}).get("edges", [])
        articles = []
        for edge in edges:
            node = edge.get("node", {})
            name = node.get("name", "").strip()
            tagline = node.get("tagline", "").strip()
            url = node.get("url", "")
            votes = node.get("votesCount", 0)
            date = (node.get("createdAt") or "")[:10]
            if name and url:
                articles.append({
                    "title": name,
                    "url": url,
                    "summary": f"{tagline} ({votes} votes)",
                    "date": date,
                })
        return articles
    except Exception as e:
        print(f"  Product Hunt API failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    rss_tech, rss_pm, rss_other, ph_key = load_config()
    blocks = []

    # --- Tech News: RSS feeds ---
    for feed_url in rss_tech:
        name = source_name_from_url(feed_url)
        print(f"Fetching RSS: {name} ...")
        articles = fetch_rss(feed_url)
        blocks.append(format_block(name, articles))
        print(f"  {len(articles)} articles")

    # --- Tech News: Hacker News API ---
    print("Fetching Hacker News API ...")
    hn_articles = fetch_hackernews()
    blocks.append(format_block("Hacker News", hn_articles))
    print(f"  {len(hn_articles)} stories")

    # --- PM & Product: Dev.to API ---
    print("Fetching Dev.to API ...")
    devto_articles = fetch_devto()
    blocks.append(format_block("Dev.to", devto_articles))
    print(f"  {len(devto_articles)} articles")

    # --- PM & Product: RSS feeds ---
    for feed_url in rss_pm:
        name = source_name_from_url(feed_url)
        print(f"Fetching RSS: {name} ...")
        articles = fetch_rss(feed_url)
        blocks.append(format_block(name, articles))
        print(f"  {len(articles)} articles")

    # --- Product Launches: Product Hunt API ---
    print("Fetching Product Hunt API ...")
    ph_articles = fetch_producthunt(ph_key)
    blocks.append(format_block("Product Hunt", ph_articles))
    print(f"  {len(ph_articles)} launches")

    # --- Other RSS (company blogs, engineering) ---
    for feed_url in rss_other:
        name = source_name_from_url(feed_url)
        print(f"Fetching RSS: {name} ...")
        articles = fetch_rss(feed_url)
        blocks.append(format_block(name, articles))
        print(f"  {len(articles)} articles")

    output = SEPARATOR.join(blocks)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
