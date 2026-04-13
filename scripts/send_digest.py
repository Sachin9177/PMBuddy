"""
send_digest.py
PMBuddy Daily Digest — scheduled runner + email sender.

Pipeline:
  1. If today's digest already exists on disk → skip fetch + Claude
  2. Otherwise: fetch sources → run Claude → save digest
  3. Convert markdown to HTML
  4. Send via Gmail SMTP

Designed to be run by Windows Task Scheduler at 7:30 AM daily.
Can also be run manually: python scripts/send_digest.py
"""

import os
import re
import smtplib
import subprocess
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown

# Project root on sys.path so we can import config.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import run_claude, CLAUDE_CMD

# ---------------------------------------------------------------------------
# Paths (all relative to project root — Task Scheduler sets working dir)
# ---------------------------------------------------------------------------

EMAIL_CONFIG_FILE = "inputs/email_config.txt"
SOURCES_CONFIG_FILE = "inputs/daily_sources.txt"
PROMPT_FILE = "prompts/pm_daily_digest.md"
RAW_SOURCES_FILE = "outputs/raw_sources.txt"
DIGESTS_DIR = "outputs/digests"
LOG_FILE = "outputs/digest_schedule.log"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(level, message):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{level}] {message}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_email_config():
    """
    Parse email_config.txt.
    Returns a dict with SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, TO_EMAIL.
    Raises FileNotFoundError or ValueError with a clear message on bad config.
    """
    if not os.path.exists(EMAIL_CONFIG_FILE):
        raise FileNotFoundError(
            f"{EMAIL_CONFIG_FILE} not found. "
            "Copy inputs/email_config.txt and fill in your Gmail details."
        )

    config = {}
    with open(EMAIL_CONFIG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip()

    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "TO_EMAIL"]
    missing = [k for k in required if not config.get(k)]
    if missing:
        raise ValueError(f"Missing fields in {EMAIL_CONFIG_FILE}: {', '.join(missing)}")

    placeholder_check = [config.get("SMTP_USER", ""), config.get("SMTP_PASSWORD", "")]
    if "your.email@gmail.com" in placeholder_check or "xxxx" in config.get("SMTP_PASSWORD", ""):
        raise ValueError(
            f"{EMAIL_CONFIG_FILE} still has placeholder values. "
            "Fill in your real Gmail address and App Password."
        )

    config["SMTP_PORT"] = int(config["SMTP_PORT"])
    return config


# ---------------------------------------------------------------------------
# Digest pipeline (mirrors app.py logic, standalone)
# ---------------------------------------------------------------------------

def digest_path_for_today():
    return os.path.join(DIGESTS_DIR, date.today().strftime("%Y-%m-%d") + ".txt")


def load_existing_digest(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def fetch_sources():
    log("INFO", "Running fetch_sources.py ...")
    result = subprocess.run(
        ["python", "scripts/fetch_sources.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Unknown error"
        raise RuntimeError(f"fetch_sources.py failed:\n{detail}")
    log("INFO", "Sources fetched successfully.")


def run_claude_digest():
    log("INFO", "Reading raw sources ...")
    with open(RAW_SOURCES_FILE, "r", encoding="utf-8") as f:
        raw_sources = f.read().strip()

    if not raw_sources:
        raise RuntimeError(f"{RAW_SOURCES_FILE} is empty after fetch.")

    log("INFO", "Running Claude digest prompt ...")
    output = run_claude(PROMPT_FILE, raw_sources)

    if output.startswith("Error:"):
        raise RuntimeError(f"Claude failed:\n{output}")

    return output


def save_digest(content):
    os.makedirs(DIGESTS_DIR, exist_ok=True)
    path = digest_path_for_today()
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    log("INFO", f"Digest saved to {path}")
    return path


# ---------------------------------------------------------------------------
# Email builder
# ---------------------------------------------------------------------------

SECTION_COLORS = {
    "📰": "#3b82f6",   # Tech News — blue
    "🚀": "#f59e0b",   # Launches — amber
    "📘": "#6366f1",   # Case Study — indigo
    "🧠": "#8b5cf6",   # Framework — violet
    "🔥": "#f97316",   # Insight — orange
    "⚡": "#10b981",   # Tip — green
}

DEFAULT_SECTION_COLOR = "#cbd5e1"


def get_section_color(heading):
    for emoji, color in SECTION_COLORS.items():
        if emoji in heading:
            return color
    return DEFAULT_SECTION_COLOR


def markdown_to_html_email(digest_md):
    """
    Convert the digest markdown into a self-contained HTML email.
    Uses inline styles throughout (Gmail strips <head> styles).
    Splits on ### headings to apply per-section color coding.
    """
    d = date.today()
    today_label = d.strftime("%A, %B") + " " + str(d.day) + ", " + str(d.year)

    # Split on ### headings, preserving the heading in each chunk
    normalized = digest_md.replace("\r\n", "\n").replace("\r", "\n")
    parts = re.split(r"(?=^###\s)", normalized, flags=re.MULTILINE)
    parts = [p.strip() for p in parts if p.strip()]

    sections_html = ""
    for part in parts:
        heading_match = re.match(r"^###\s+(.+)$", part, re.MULTILINE)
        if not heading_match:
            continue
        heading_text = heading_match.group(1).strip()
        heading_line = heading_match.group(0)
        body_md = part[part.index(heading_line) + len(heading_line):].strip()

        color = get_section_color(heading_text)
        body_html = markdown.markdown(body_md, extensions=["nl2br"])

        sections_html += f"""
        <div style="margin-bottom:24px; background:#ffffff; border:1px solid #e2e8f0;
                    border-left:4px solid {color}; border-radius:8px; padding:20px 24px;">
            <div style="font-size:15px; font-weight:700; color:#0f172a;
                        margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid #f1f5f9;">
                {heading_text}
            </div>
            <div style="font-size:13px; line-height:1.75; color:#1e293b;">
                {body_html}
            </div>
        </div>
        """

    app_url = f"http://localhost:5000/?tool=digest&date={date.today().strftime('%Y-%m-%d')}"

    html = f"""
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0; padding:0; background:#f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;">
  <div style="max-width:640px; margin:0 auto; padding:32px 16px;">

    <!-- Header -->
    <div style="background:#111827; border-radius:10px 10px 0 0; padding:20px 28px; margin-bottom:0;">
      <div style="font-size:18px; font-weight:700; color:#ffffff; letter-spacing:-0.01em;">PMBuddy</div>
      <div style="font-size:12px; color:#6b7280; margin-top:2px;">Daily Digest &mdash; {today_label}</div>
    </div>

    <!-- Body -->
    <div style="background:#f8fafc; padding:20px 0;">
      {sections_html}
    </div>

    <!-- Footer -->
    <div style="text-align:center; padding:16px 0 8px; font-size:11px; color:#94a3b8;">
      <a href="{app_url}" style="color:#3b82f6; text-decoration:none;">View in browser</a>
      &nbsp;&middot;&nbsp; Generated by PMBuddy
    </div>

  </div>
</body>
</html>
"""
    return html


def build_plain_text(digest_md):
    """Fallback plain-text version of the email."""
    today_label = date.today().strftime("%A, %B %d, %Y")
    return f"PMBuddy Daily Digest — {today_label}\n\n{digest_md}"


# ---------------------------------------------------------------------------
# Email sender
# ---------------------------------------------------------------------------

def send_email(config, digest_md):
    subject = f"PMBuddy Daily Digest — {date.today().strftime('%A, %B %d, %Y')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["SMTP_USER"]
    msg["To"] = config["TO_EMAIL"]

    plain = build_plain_text(digest_md)
    html = markdown_to_html_email(digest_md)

    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    log("INFO", f"Connecting to {config['SMTP_HOST']}:{config['SMTP_PORT']} ...")
    with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"]) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
        server.sendmail(config["SMTP_USER"], config["TO_EMAIL"], msg.as_string())

    log("INFO", f"Email sent to {config['TO_EMAIL']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log("INFO", "=== PMBuddy Digest Runner starting ===")

    # Step 1: Load email config (fail early if misconfigured)
    try:
        config = load_email_config()
    except (FileNotFoundError, ValueError) as e:
        log("ERROR", str(e))
        sys.exit(1)

    # Step 2: Check if today's digest already exists
    digest_path = digest_path_for_today()
    if os.path.exists(digest_path):
        log("INFO", f"Digest already exists for today ({digest_path}), skipping generation.")
        digest_content = load_existing_digest(digest_path)
    else:
        # Step 2a: Fetch sources
        try:
            fetch_sources()
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            log("ERROR", f"Source fetch failed: {e}")
            sys.exit(1)

        # Step 2b: Run Claude
        try:
            digest_content = run_claude_digest()
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            log("ERROR", f"Claude step failed: {e}")
            sys.exit(1)

        # Step 2c: Save to disk
        save_digest(digest_content)

    # Step 3: Send email
    try:
        send_email(config, digest_content)
    except smtplib.SMTPAuthenticationError:
        log("ERROR", "Gmail authentication failed. Check SMTP_USER and SMTP_PASSWORD in email_config.txt.")
        log("ERROR", "Make sure you are using a Gmail App Password, not your regular Gmail password.")
        log("ERROR", "Generate one at: myaccount.google.com/apppasswords")
        sys.exit(1)
    except smtplib.SMTPException as e:
        log("ERROR", f"SMTP error: {e}")
        sys.exit(1)
    except Exception as e:
        log("ERROR", f"Unexpected error sending email: {e}")
        sys.exit(1)

    log("INFO", "=== Done ===")


if __name__ == "__main__":
    main()
