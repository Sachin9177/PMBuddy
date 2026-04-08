import os
import subprocess
from flask import Flask, render_template, request

app = Flask(__name__)

CLAUDE_CMD = r"C:\Users\user\AppData\Roaming\npm\claude.cmd"

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
    "digest":         {"name": "Daily Digest",                      "prompt": "prompts/pm_daily_digest.md",    "placeholder": "Optional: Enter a domain or topic (e.g. 'Domain: AI and SaaS'). Sources will be fetched automatically."},
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
        timeout=120,
    )

    if result.returncode != 0:
        return f"Error: {result.stderr}"

    return result.stdout


def run_digest(user_input):
    """
    Daily Digest pipeline:
      1. Run fetch_sources.py to pull articles into outputs/raw_sources.txt
      2. Read that file as the Claude input
      3. Optionally prepend a domain preference from the user
      4. Run Claude with the digest prompt
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

    # Step 3: Build Claude input — domain preference (if any) + fetched articles
    if user_input:
        claude_input = f"{user_input}\n\n---\n\n{raw_sources}"
    else:
        claude_input = raw_sources

    # Step 4: Run Claude
    print("Running Claude digest prompt ...")
    try:
        output = run_claude("prompts/pm_daily_digest.md", claude_input)
        return output, None
    except subprocess.TimeoutExpired:
        return None, "Claude took too long to respond. Try again."
    except FileNotFoundError:
        return None, "Claude CLI not found. Check that it is installed and CLAUDE_CMD is correct."


@app.route("/", methods=["GET", "POST"])
def index():
    tool_id = request.args.get("tool", "prd")
    tool = TOOLS.get(tool_id, TOOLS["prd"])

    output = None
    user_input = ""
    error = None

    if request.method == "POST":
        user_input = request.form.get("user_input", "").strip()

        if not tool["prompt"]:
            error = "This tool is coming soon."

        elif tool_id == "digest":
            # Digest has its own pipeline — runs fetch_sources.py first
            output, error = run_digest(user_input)

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
    )


if __name__ == "__main__":
    app.run(debug=True)
