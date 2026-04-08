import os
import subprocess
from datetime import date, datetime
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)

CLAUDE_CMD = r"C:\Users\user\AppData\Roaming\npm\claude.cmd"
DIGESTS_DIR = "outputs/digests"

# Tool definitions: id → name, prompt file, input placeholder
TOOLS = {
    "prd":            {"name": "PRD Generator",                    "prompt": "prompts/generate_prd.md",       "placeholder": "Describe your feature idea. The rougher the better."},
    "user-stories":   {"name": "User Stories & Acceptance Criteria","prompt": None,                            "placeholder": "Describe the feature you need user stories for."},
    "roadmap":        {"name": "Roadmap Generator",                 "prompt": None,                            "placeholder": "Describe your product goals and timeline."},
    "meeting":        {"name": "Meeting Summary",                   "prompt": "prompts/summarize_meeting.md",  "placeholder": "Paste your raw meeting notes here."},
    "status-updates": {"name": "Status Updates",                    "prompt": None,                            "placeholder": "Describe what your team shipped, what's in progress, and any blockers."},
    "prioritization": {"name": "Prioritization Engine",             "prompt": "prompts/prioritize.md",         "placeholder": "List the features or initiatives you need to prioritize."},
    "experimentation":{"name": "Experimentation",                   "prompt": None,                            "placeholder": "Describe the hypothesis you want to test."},
    "feedback":       {"name": "Feedback Analyzer",                 "prompt": None,                            "placeholder": "Paste user feedback, reviews, or survey responses here."},
    "analytics":      {"name": "Analytics Assistant",               "prompt": None,                            "placeholder": "Describe the metric or trend you want to understand."},
    "competitor":     {"name": "Competitor & Market Analyzer",      "prompt": None,                            "placeholder": "Name the competitor or market you want analyzed."},
    "digest":         {"name": "Daily Digest",                      "prompt": "prompts/pm_daily_digest.md",    "placeholder": ""},
    "learning":       {"name": "Learning Coach",                    "prompt": None,                            "placeholder": "What PM skill or topic do you want to learn about?"},
}

# Sidebar structure: categories with ordered tool ids
CATEGORIES = [
    {"name": "Product Thinking & Definition",     "tools": ["prd", "user-stories", "roadmap"]},
    {"name": "Execution & Delivery",              "tools": ["meeting", "status-updates"]},
    {"name": "Decision Making & Prioritization",  "tools": ["prioritization", "experimentation"]},
    {"name": "Insights & Intelligence",           "tools": ["feedback", "analytics", "competitor", "digest"]},
    {"name": "Learning & Growth",                 "tools": ["learning"]},
]


# ---------------------------------------------------------------------------
# Digest history helpers
# ---------------------------------------------------------------------------

def save_digest(content, for_date=None):
    """Save digest content to outputs/digests/YYYY-MM-DD.txt."""
    os.makedirs(DIGESTS_DIR, exist_ok=True)
    filename = (for_date or date.today()).strftime("%Y-%m-%d") + ".txt"
    path = os.path.join(DIGESTS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def load_digest(for_date):
    """Load a saved digest by date string (YYYY-MM-DD). Returns content or None."""
    path = os.path.join(DIGESTS_DIR, f"{for_date}.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return None


def list_digest_dates():
    """Return sorted list of saved digest date strings, newest first."""
    if not os.path.isdir(DIGESTS_DIR):
        return []
    dates = []
    for fname in os.listdir(DIGESTS_DIR):
        if fname.endswith(".txt") and fname != ".gitkeep":
            dates.append(fname.replace(".txt", ""))
    dates.sort(reverse=True)
    return dates


def format_date_label(date_str):
    """Convert YYYY-MM-DD to a human-readable label like 'Today', 'Yesterday', 'Apr 7'."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = date.today()
        delta = (today - d).days
        if delta == 0:
            return "Today"
        elif delta == 1:
            return "Yesterday"
        else:
            return d.strftime("%b") + " " + str(d.day)
    except ValueError:
        return date_str


# ---------------------------------------------------------------------------
# Claude helpers
# ---------------------------------------------------------------------------

def run_claude(prompt_file, input_text):
    """Read a prompt file, substitute {{input}}, and send to Claude CLI."""
    with open(prompt_file, "r") as f:
        prompt = f.read()

    full_prompt = prompt.replace("{{input}}", input_text)

    result = subprocess.run(
        [CLAUDE_CMD, "--print"],
        input=full_prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )

    if result.returncode != 0:
        return f"Error: {result.stderr}"

    return result.stdout


def run_digest():
    """
    Daily Digest pipeline:
      1. Run fetch_sources.py to pull articles into outputs/raw_sources.txt
      2. Run Claude with the digest prompt
      3. Save result to outputs/digests/YYYY-MM-DD.txt
    Returns (output, error) — one of them will always be None.
    """

    # Step 1: Fetch sources
    print("Running fetch_sources.py ...")
    try:
        fetch = subprocess.run(
            ["python", "scripts/fetch_sources.py"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return None, "Fetching sources timed out. Check your internet connection and try again."
    except Exception as e:
        return None, f"Could not run fetch_sources.py: {e}"

    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip() or "Unknown error"
        return None, f"Source fetch failed:\n{detail}"

    # Step 2: Read the fetched articles
    raw_sources_path = "outputs/raw_sources.txt"
    try:
        with open(raw_sources_path, "r", encoding="utf-8") as f:
            raw_sources = f.read().strip()
    except FileNotFoundError:
        return None, f"{raw_sources_path} was not created. fetch_sources.py may have failed silently."

    if not raw_sources:
        return None, "Fetched sources file is empty. Check inputs/daily_sources.txt and try again."

    # Step 3: Run Claude
    print("Running Claude digest prompt ...")
    try:
        output = run_claude("prompts/pm_daily_digest.md", raw_sources)
    except subprocess.TimeoutExpired:
        return None, "Claude took too long to respond (>5 min). Try again."
    except FileNotFoundError:
        return None, "Claude CLI not found. Check that it is installed and CLAUDE_CMD is correct."

    # Step 4: Save to history
    save_digest(output)
    print(f"Digest saved for {date.today()}")

    return output, None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    tool_id = request.args.get("tool", "prd")
    tool = TOOLS.get(tool_id, TOOLS["prd"])

    output = None
    user_input = ""
    error = None
    digest_dates = []
    active_digest_date = None

    if tool_id == "digest":
        digest_dates = list_digest_dates()

        if request.method == "POST":
            # Generate a fresh digest
            output, error = run_digest()
            active_digest_date = date.today().strftime("%Y-%m-%d")

        else:
            # Load a specific date if requested, else load most recent
            requested_date = request.args.get("date")
            if requested_date and requested_date in digest_dates:
                output = load_digest(requested_date)
                active_digest_date = requested_date
            elif digest_dates:
                output = load_digest(digest_dates[0])
                active_digest_date = digest_dates[0]

    else:
        if request.method == "POST":
            user_input = request.form.get("user_input", "").strip()

            if not tool["prompt"]:
                error = "This tool is coming soon."
            elif user_input:
                try:
                    output = run_claude(tool["prompt"], user_input)
                except subprocess.TimeoutExpired:
                    error = "Claude took too long to respond."
                except FileNotFoundError:
                    error = "Claude CLI not found."

    return render_template(
        "index.html",
        categories=CATEGORIES,
        tools=TOOLS,
        active_tool_id=tool_id,
        tool=tool,
        output=output,
        user_input=user_input,
        error=error,
        digest_dates=digest_dates,
        active_digest_date=active_digest_date,
        format_date_label=format_date_label,
    )


if __name__ == "__main__":
    app.run(debug=True)
