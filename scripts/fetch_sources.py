"""
fetch_sources.py
Reads URLs from inputs/daily_sources.txt, fetches the page text,
and saves everything to outputs/raw_sources.txt.
"""

import requests
from bs4 import BeautifulSoup

INPUT_FILE  = "inputs/daily_sources.txt"
OUTPUT_FILE = "outputs/raw_sources.txt"

def fetch_text(url):
    """Fetch a URL and return the visible text content."""
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Remove script and style tags — they add noise, not content
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()

    # Extract visible text and clean up extra whitespace
    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def main():
    # Read URLs from input file (skip blank lines and comments)
    with open(INPUT_FILE, "r") as f:
        urls = [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]

    if not urls:
        print("No URLs found in", INPUT_FILE)
        return

    results = []

    for url in urls:
        print(f"Fetching: {url}")
        try:
            text = fetch_text(url)
            results.append(f"Source: {url}\n{'='*60}\n{text}\n")
            print(f"  Done ({len(text)} characters)")
        except Exception as e:
            results.append(f"Source: {url}\n{'='*60}\nERROR: {e}\n")
            print(f"  Failed: {e}")

    # Write all results to output file
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(f"\n{'---'*20}\n\n".join(results))

    print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
