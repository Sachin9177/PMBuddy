import json
import os
import re
import subprocess
from datetime import date, datetime
import markdown as md
from flask import Flask, render_template, request, redirect, url_for
from config import run_claude, MAX_INPUT_CHARS

app = Flask(__name__)

@app.template_filter("md_render")
def md_render_filter(text):
    """Jinja filter: render markdown string to HTML."""
    return md.markdown(text or "", extensions=["nl2br", "tables"])
DIGESTS_DIR  = "outputs/digests"
CONTEXTS_DIR = "data/contexts"


# ---------------------------------------------------------------------------
# CSV preprocessing
# ---------------------------------------------------------------------------

def preprocess_csv(content, filename="data.csv"):
    """
    Convert raw CSV text into a structured statistical summary for Claude.
    Returns (summary_text, info_string) where info_string describes what was
    processed (e.g. "5,243 rows × 8 columns"). On failure, returns the
    original content and an empty info string so the tool degrades gracefully.
    """
    try:
        import pandas as pd
        import io

        df = pd.read_csv(io.StringIO(content))
        rows, cols = df.shape
        lines = []

        # ── Header ──────────────────────────────────────────────────────────
        lines.append(f"## Dataset: {filename}")
        lines.append(f"- **Rows**: {rows:,}")
        lines.append(f"- **Columns**: {cols}")
        lines.append("")

        # ── Column overview table ────────────────────────────────────────────
        lines.append("## Column Summary")
        lines.append("| Column | Type | Non-null | Nulls |")
        lines.append("|--------|------|----------|-------|")
        for col in df.columns:
            dtype    = str(df[col].dtype)
            non_null = int(df[col].count())
            nulls    = int(df[col].isna().sum())
            lines.append(f"| {col} | {dtype} | {non_null:,} | {nulls:,} |")
        lines.append("")

        # ── Numeric statistics ───────────────────────────────────────────────
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        if numeric_cols:
            lines.append("## Numeric Column Statistics")
            for col in numeric_cols:
                s = df[col].dropna()
                if s.empty:
                    continue
                lines.append(f"### {col}")
                lines.append(
                    f"- Min: {s.min():,.2f} | Max: {s.max():,.2f} | "
                    f"Mean: {s.mean():,.2f} | Median: {s.median():,.2f} | "
                    f"Std: {s.std():,.2f}"
                )
                nulls = int(df[col].isna().sum())
                if nulls:
                    lines.append(f"- Nulls: {nulls:,}")
            lines.append("")

        # ── Categorical breakdowns ───────────────────────────────────────────
        cat_cols = df.select_dtypes(include="object").columns.tolist()
        if cat_cols:
            lines.append("## Categorical Columns")
            for col in cat_cols:
                unique = df[col].nunique()
                lines.append(f"### {col} ({unique:,} unique values)")
                if unique <= 30:
                    top = df[col].value_counts().head(10)
                    for val, cnt in top.items():
                        pct = cnt / rows * 100
                        lines.append(f"- {val}: {cnt:,} ({pct:.1f}%)")
                else:
                    lines.append("- High cardinality — top 5 most frequent values:")
                    top = df[col].value_counts().head(5)
                    for val, cnt in top.items():
                        pct = cnt / rows * 100
                        lines.append(f"  - {val}: {cnt:,} ({pct:.1f}%)")
            lines.append("")

        # ── Date range detection ─────────────────────────────────────────────
        for col in df.columns:
            if any(kw in col.lower() for kw in ("date", "time", "day", "week", "month")):
                try:
                    parsed = pd.to_datetime(df[col], errors="coerce")
                    if parsed.notna().sum() > rows * 0.8:
                        mn = parsed.min().strftime("%Y-%m-%d")
                        mx = parsed.max().strftime("%Y-%m-%d")
                        span = (parsed.max() - parsed.min()).days
                        lines.append(f"## Date Range (column: {col})")
                        lines.append(f"- From **{mn}** to **{mx}** ({span} days)")
                        lines.append("")
                        break
                except Exception:
                    pass

        # ── Sample rows ──────────────────────────────────────────────────────
        lines.append("## Sample Data (first 5 rows)")
        lines.append("```")
        lines.append(df.head(5).to_csv(index=False).strip())
        lines.append("```")

        summary = "\n".join(lines)
        info    = f"{rows:,} rows, {cols} columns"
        return summary, info

    except Exception:
        return content, ""
ACTIVE_CONTEXT_FILE = "data/active_context.txt"

# Auto-save directories for tools that generate persistent output
TOOL_OUTPUT_DIRS = {
    "prd":     "outputs/prds",
    "meeting": "outputs/meetings",
}

ANALYTICS_DIR = "outputs/analytics"
FEEDBACK_DIR  = "outputs/feedback"

# Fields shown in the context create/edit form
CONTEXT_FIELDS = [
    {"key": "name",           "label": "Context Name",    "placeholder": "e.g. Acme Corp — Analytics",               "required": True,  "type": "input"},
    {"key": "industry",       "label": "Industry",        "placeholder": "e.g. B2B SaaS, Fintech, Healthcare",        "required": False, "type": "input"},
    {"key": "product",        "label": "Product",         "placeholder": "Describe what your product does and for whom", "required": False, "type": "textarea"},
    {"key": "users",          "label": "Target Users",    "placeholder": "e.g. Data analysts, SMB finance teams",     "required": False, "type": "textarea"},
    {"key": "stage",          "label": "Stage",           "placeholder": "e.g. Seed, Series B, Enterprise",           "required": False, "type": "input"},
    {"key": "business_model", "label": "Business Model",  "placeholder": "e.g. Annual SaaS contracts, Usage-based",   "required": False, "type": "input"},
    {"key": "key_metrics",    "label": "Key Metrics",     "placeholder": "e.g. DAU, 30-day retention, ARR",           "required": False, "type": "input"},
    {"key": "team_structure", "label": "Team Structure",  "placeholder": "e.g. 2 PMs, 8 engineers, 1 designer",      "required": False, "type": "input"},
    {"key": "competitors",    "label": "Competitors",     "placeholder": "e.g. Salesforce, HubSpot, Pipedrive",       "required": False, "type": "input"},
    {"key": "notes",          "label": "Notes",           "placeholder": "Anything else Claude should know",           "required": False, "type": "textarea"},
]

# Tool definitions: id → name, prompt file, input placeholder
TOOLS = {
    "prd":            {"name": "PRD Generator",                    "prompt": "prompts/generate_prd.md",       "placeholder": "Describe your feature idea. The rougher the better."},
    "user-stories":   {"name": "User Stories & Acceptance Criteria","prompt": None,                            "placeholder": "Describe the feature you need user stories for."},
    "roadmap":        {"name": "Roadmap Generator",                 "prompt": None,                            "placeholder": "Describe your product goals and timeline."},
    "meeting":        {"name": "Meeting Summary",                   "prompt": "prompts/summarize_meeting.md",  "placeholder": "Paste your raw meeting notes here.", "max_chars": 40_000},
    "status-updates": {"name": "Status Updates",                    "prompt": None,                            "placeholder": "Describe what your team shipped, what's in progress, and any blockers."},
    "prioritization": {"name": "Prioritization Engine",             "prompt": "prompts/prioritize.md",         "placeholder": "List the features or initiatives you need to prioritize."},
    "experimentation":{"name": "Experimentation",                   "prompt": None,                            "placeholder": "Describe the hypothesis you want to test."},
    "feedback":       {"name": "Feedback Analyzer",                 "prompt": "prompts/analyze_feedback.md",   "placeholder": "Paste user reviews, support tickets, NPS comments, or survey responses. The more you paste, the better the patterns.", "max_chars": 30_000},
    "analytics":      {"name": "Analytics Assistant",               "prompt": "prompts/analytics_assistant.md","placeholder": "Describe what you're seeing in your data — metric movements, funnel numbers, A/B results, retention drops. Include any relevant context like recent releases or date ranges.", "max_chars": 30_000},
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
# Tool output helpers (Meeting Summary — simple flat files)
# ---------------------------------------------------------------------------

def save_tool_output(tool_id, raw_content, title_hint=""):
    """Save raw markdown for non-PRD tools. Returns filename."""
    directory = TOOL_OUTPUT_DIRS.get(tool_id)
    if not directory:
        return None
    os.makedirs(directory, exist_ok=True)
    filename = datetime.now().strftime("%Y%m%d_%H%M%S") + ".md"
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as f:
        if title_hint:
            short = title_hint.replace("\n", " ").strip()[:80]
            f.write(f"<!-- title: {short} -->\n")
        f.write(raw_content)
    return filename


def get_output_title(tool_id, filename):
    """Read the title hint from the HTML comment on line 1 of a saved file."""
    directory = TOOL_OUTPUT_DIRS.get(tool_id)
    if not directory:
        return None
    path = os.path.join(directory, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        if first_line.startswith("<!-- title:") and first_line.endswith("-->"):
            return first_line[len("<!-- title:"):].rstrip("-->").strip()
    except (FileNotFoundError, IOError):
        pass
    return None


def load_tool_output(tool_id, filename):
    """Load a saved output file by filename. Returns raw markdown or None."""
    directory = TOOL_OUTPUT_DIRS.get(tool_id)
    if not directory:
        return None
    path = os.path.join(directory, filename)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return None


def list_tool_outputs(tool_id):
    """Return list of (filename, label) tuples for a tool, newest first."""
    directory = TOOL_OUTPUT_DIRS.get(tool_id)
    if not directory or not os.path.isdir(directory):
        return []
    files = [f for f in os.listdir(directory) if f.endswith(".md") and f != ".gitkeep"]
    files.sort(reverse=True)
    return [(f, format_output_label(tool_id, f)) for f in files]


def format_output_label(tool_id, filename):
    """Return a human-readable label for a saved output tab."""
    title = get_output_title(tool_id, filename)
    try:
        dt = datetime.strptime(filename.replace(".md", ""), "%Y%m%d_%H%M%S")
        today = date.today()
        if dt.date() == today:
            date_label = "Today"
        elif (today - dt.date()).days == 1:
            date_label = "Yesterday"
        else:
            date_label = dt.strftime("%b ") + str(dt.day)
        timestamp_label = date_label
    except ValueError:
        timestamp_label = filename
    if title:
        short = title[:40] + "..." if len(title) > 40 else title
        return short
    return timestamp_label


# ---------------------------------------------------------------------------
# PRD helpers — versioned threads
# ---------------------------------------------------------------------------

PRD_DIR = "outputs/prds"


def _prd_date_label(dt):
    today = date.today()
    if dt.date() == today:
        return "Today"
    elif (today - dt.date()).days == 1:
        return "Yesterday"
    return dt.strftime("%b ") + str(dt.day)


def save_prd(raw_content, title_hint="", thread_id=None, instruction=None):
    """Save a PRD version. Creates a new thread if thread_id is None.
    Returns (thread_id, version_number).
    """
    os.makedirs(PRD_DIR, exist_ok=True)
    now = datetime.now()
    file_stem = now.strftime("%Y%m%d_%H%M%S")
    filename   = file_stem + ".md"
    filepath   = os.path.join(PRD_DIR, filename)

    # Write the markdown file
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(raw_content)

    if thread_id:
        # Append a new version to an existing thread
        meta_path = os.path.join(PRD_DIR, thread_id + ".json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                thread = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            thread = {"id": thread_id, "title": title_hint, "created": now.isoformat(), "versions": []}
        version_num = len(thread["versions"]) + 1
        thread["versions"].append({
            "file": filename,
            "version": version_num,
            "instruction": instruction or "",
            "created": now.isoformat(),
        })
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(thread, f, indent=2, ensure_ascii=False)
        return thread_id, version_num
    else:
        # Create a new thread
        new_thread_id = file_stem
        title = title_hint.replace("\n", " ").strip()[:80] if title_hint else "Untitled PRD"
        thread = {
            "id": new_thread_id,
            "title": title,
            "created": now.isoformat(),
            "versions": [
                {"file": filename, "version": 1, "instruction": "", "created": now.isoformat()}
            ],
        }
        meta_path = os.path.join(PRD_DIR, new_thread_id + ".json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(thread, f, indent=2, ensure_ascii=False)
        return new_thread_id, 1


def get_prd_thread(thread_id):
    """Load and return a PRD thread dict, or None if not found."""
    meta_path = os.path.join(PRD_DIR, thread_id + ".json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_prd_version(thread, version_num=None):
    """Return raw markdown for the given version (latest if None)."""
    versions = thread.get("versions", [])
    if not versions:
        return None
    if version_num:
        entry = next((v for v in versions if v["version"] == version_num), versions[-1])
    else:
        entry = versions[-1]
    filepath = os.path.join(PRD_DIR, entry["file"])
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _migrate_orphan_prd(md_filename):
    """Create a thread JSON for a legacy .md file that has no thread metadata."""
    stem = md_filename.replace(".md", "")
    meta_path = os.path.join(PRD_DIR, stem + ".json")
    if os.path.exists(meta_path):
        return  # already has metadata

    # Try to read title hint from HTML comment on line 1
    md_path = os.path.join(PRD_DIR, md_filename)
    title = "Untitled PRD"
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        if first_line.startswith("<!-- title:") and first_line.endswith("-->"):
            title = first_line[len("<!-- title:"):].rstrip("-->").strip() or title
    except IOError:
        pass

    # Parse timestamp from filename for created date
    try:
        dt = datetime.strptime(stem, "%Y%m%d_%H%M%S")
        created = dt.isoformat()
    except ValueError:
        created = datetime.now().isoformat()

    thread = {
        "id": stem,
        "title": title,
        "created": created,
        "versions": [{"file": md_filename, "version": 1, "instruction": "", "created": created}],
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(thread, f, indent=2, ensure_ascii=False)


def list_prd_threads():
    """Return list of thread dicts sorted newest first.
    Auto-migrates any legacy .md files that lack thread metadata.
    """
    if not os.path.isdir(PRD_DIR):
        return []

    # Migrate orphaned .md files first
    all_files = os.listdir(PRD_DIR)
    json_stems = {f.replace(".json", "") for f in all_files if f.endswith(".json")}
    for fname in all_files:
        if fname.endswith(".md") and fname != ".gitkeep":
            stem = fname.replace(".md", "")
            if stem not in json_stems:
                _migrate_orphan_prd(fname)

    # Now read all thread JSON files
    threads = []
    for fname in os.listdir(PRD_DIR):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(PRD_DIR, fname), "r", encoding="utf-8") as f:
                    threads.append(json.load(f))
            except (json.JSONDecodeError, IOError):
                pass
    threads.sort(key=lambda t: t.get("created", ""), reverse=True)
    return threads


def prd_thread_label(thread):
    """Human-readable label for a PRD thread in the history dropdown."""
    title = thread.get("title", "Untitled PRD")
    short = title[:45] + "…" if len(title) > 45 else title
    v_count = len(thread.get("versions", []))
    v_label = f"v{v_count}" if v_count > 1 else "v1"
    try:
        dt = datetime.fromisoformat(thread["created"])
        date_label = _prd_date_label(dt)
    except (KeyError, ValueError):
        date_label = ""
    return f"{short} ({v_label}){' · ' + date_label if date_label else ''}"


# ---------------------------------------------------------------------------
# Analytics helpers — conversational sessions
# ---------------------------------------------------------------------------

def save_analytics_session(user_content, assistant_response):
    """Create a new analytics session JSON. Returns session_id."""
    os.makedirs(ANALYTICS_DIR, exist_ok=True)
    now = datetime.now()
    session_id = now.strftime("%Y%m%d_%H%M%S")
    title = user_content.replace("\n", " ").strip()[:80]
    session = {
        "id": session_id,
        "title": title,
        "created": now.isoformat(),
        "messages": [
            {"role": "user",      "content": user_content,       "created": now.isoformat()},
            {"role": "assistant", "content": assistant_response,  "created": now.isoformat()},
        ],
    }
    with open(os.path.join(ANALYTICS_DIR, session_id + ".json"), "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2, ensure_ascii=False)
    return session_id


def get_analytics_session(session_id):
    """Load an analytics session dict by ID, or None if not found."""
    path = os.path.join(ANALYTICS_DIR, session_id + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def append_analytics_turn(session_id, user_message, assistant_response):
    """Append a Q&A turn to an existing analytics session."""
    path = os.path.join(ANALYTICS_DIR, session_id + ".json")
    with open(path, "r", encoding="utf-8") as f:
        session = json.load(f)
    now = datetime.now().isoformat()
    session["messages"].append({"role": "user",      "content": user_message,        "created": now})
    session["messages"].append({"role": "assistant", "content": assistant_response,   "created": now})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2, ensure_ascii=False)


def list_analytics_sessions():
    """Return all analytics sessions sorted newest first."""
    if not os.path.isdir(ANALYTICS_DIR):
        return []
    sessions = []
    for fname in os.listdir(ANALYTICS_DIR):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(ANALYTICS_DIR, fname), "r", encoding="utf-8") as f:
                    sessions.append(json.load(f))
            except (json.JSONDecodeError, IOError):
                pass
    sessions.sort(key=lambda s: s.get("created", ""), reverse=True)
    return sessions


def analytics_session_label(session):
    """Human-readable label for an analytics session in the history dropdown."""
    title = session.get("title", "Untitled Analysis")
    short = title[:50] + "…" if len(title) > 50 else title
    n_questions = len([m for m in session.get("messages", []) if m["role"] == "user"])
    q_label = f"{n_questions} question{'s' if n_questions != 1 else ''}"
    try:
        dt = datetime.fromisoformat(session["created"])
        date_label = _prd_date_label(dt)
    except (KeyError, ValueError):
        date_label = ""
    return f"{short} ({q_label}){' · ' + date_label if date_label else ''}"


def build_analytics_followup_prompt(session, new_message):
    """Build a single prompt containing the full conversation history + new question."""
    lines = [
        "You are a data analyst and Product Manager continuing an ongoing data analysis conversation.",
        "The full conversation history is below. Answer the follow-up with that full context in mind.",
        "Be focused and concise — address what is new or changed. Do not repeat the prior analysis unless asked.",
        "",
        "=== CONVERSATION HISTORY ===",
        "",
    ]
    for msg in session.get("messages", []):
        role = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"{role}:\n{msg['content']}")
        lines.append("")
    lines += [
        "=== FOLLOW-UP QUESTION ===",
        "",
        new_message,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Feedback helpers — conversational sessions
# ---------------------------------------------------------------------------

def save_feedback_session(user_content, assistant_response):
    """Create a new feedback analysis session. Returns session_id."""
    os.makedirs(FEEDBACK_DIR, exist_ok=True)
    now = datetime.now()
    session_id = now.strftime("%Y%m%d_%H%M%S")
    title = user_content.replace("\n", " ").strip()[:80]
    session = {
        "id": session_id,
        "title": title,
        "created": now.isoformat(),
        "messages": [
            {"role": "user",      "content": user_content,      "created": now.isoformat()},
            {"role": "assistant", "content": assistant_response, "created": now.isoformat()},
        ],
    }
    with open(os.path.join(FEEDBACK_DIR, session_id + ".json"), "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2, ensure_ascii=False)
    return session_id


def get_feedback_session(session_id):
    """Load a feedback session dict by ID, or None if not found."""
    path = os.path.join(FEEDBACK_DIR, session_id + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def append_feedback_turn(session_id, user_message, assistant_response):
    """Append a Q&A turn to an existing feedback session."""
    path = os.path.join(FEEDBACK_DIR, session_id + ".json")
    with open(path, "r", encoding="utf-8") as f:
        session = json.load(f)
    now = datetime.now().isoformat()
    session["messages"].append({"role": "user",      "content": user_message,        "created": now})
    session["messages"].append({"role": "assistant", "content": assistant_response,   "created": now})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2, ensure_ascii=False)


def list_feedback_sessions():
    """Return all feedback sessions sorted newest first."""
    if not os.path.isdir(FEEDBACK_DIR):
        return []
    sessions = []
    for fname in os.listdir(FEEDBACK_DIR):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(FEEDBACK_DIR, fname), "r", encoding="utf-8") as f:
                    sessions.append(json.load(f))
            except (json.JSONDecodeError, IOError):
                pass
    sessions.sort(key=lambda s: s.get("created", ""), reverse=True)
    return sessions


def feedback_session_label(session):
    """Human-readable label for a feedback session in the history dropdown."""
    title = session.get("title", "Untitled Analysis")
    short = title[:50] + "…" if len(title) > 50 else title
    n_questions = len([m for m in session.get("messages", []) if m["role"] == "user"])
    q_label = f"{n_questions} question{'s' if n_questions != 1 else ''}"
    try:
        dt = datetime.fromisoformat(session["created"])
        date_label = _prd_date_label(dt)
    except (KeyError, ValueError):
        date_label = ""
    return f"{short} ({q_label}){' · ' + date_label if date_label else ''}"


def build_feedback_followup_prompt(session, new_message):
    """Build a single prompt containing the full feedback conversation + new question."""
    lines = [
        "You are a Product Manager analyst continuing an ongoing user feedback analysis conversation.",
        "The full conversation history is below. Answer the follow-up with that full context in mind.",
        "Be focused and concise — address what is new or changed. Do not repeat the prior analysis unless asked.",
        "",
        "=== CONVERSATION HISTORY ===",
        "",
    ]
    for msg in session.get("messages", []):
        role = "User" if msg["role"] == "user" else "Assistant"
        lines.append(f"{role}:\n{msg['content']}")
        lines.append("")
    lines += [
        "=== FOLLOW-UP QUESTION ===",
        "",
        new_message,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def make_context_id(name):
    """Convert a context name to a safe filename ID."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:50]


def list_contexts():
    """Return all saved context dicts sorted by name."""
    if not os.path.isdir(CONTEXTS_DIR):
        return []
    contexts = []
    for fname in os.listdir(CONTEXTS_DIR):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(CONTEXTS_DIR, fname), "r", encoding="utf-8") as f:
                    contexts.append(json.load(f))
            except (json.JSONDecodeError, IOError):
                pass
    contexts.sort(key=lambda c: c.get("name", "").lower())
    return contexts


def get_active_context():
    """Return the active context dict, or None if not set / file missing."""
    if not os.path.exists(ACTIVE_CONTEXT_FILE):
        return None
    try:
        with open(ACTIVE_CONTEXT_FILE, "r", encoding="utf-8") as f:
            context_id = f.read().strip()
        if not context_id:
            return None
        path = os.path.join(CONTEXTS_DIR, f"{context_id}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except (IOError, json.JSONDecodeError):
        pass
    return None


def set_active_context(context_id):
    """Write the active context ID to disk."""
    os.makedirs("data", exist_ok=True)
    with open(ACTIVE_CONTEXT_FILE, "w", encoding="utf-8") as f:
        f.write(context_id)


def save_context_data(context_dict):
    """Persist a context dict to disk."""
    os.makedirs(CONTEXTS_DIR, exist_ok=True)
    path = os.path.join(CONTEXTS_DIR, f"{context_dict['id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(context_dict, f, indent=2, ensure_ascii=False)


def delete_context_data(context_id):
    """Delete a context file and clear active context if it was the deleted one."""
    path = os.path.join(CONTEXTS_DIR, f"{context_id}.json")
    if os.path.exists(path):
        os.remove(path)
    if os.path.exists(ACTIVE_CONTEXT_FILE):
        with open(ACTIVE_CONTEXT_FILE, "r", encoding="utf-8") as f:
            active_id = f.read().strip()
        if active_id == context_id:
            with open(ACTIVE_CONTEXT_FILE, "w", encoding="utf-8") as f:
                f.write("")


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

    active_context = get_active_context()
    all_contexts   = list_contexts()

    output = None
    raw_output = None
    user_input = ""
    error = None
    digest_dates = []
    active_digest_date = None
    output_history = []
    active_file = None

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

    elif tool_id == "prd":
        # ── PRD: versioned thread storage ──────────────────────────────────
        prd_threads     = list_prd_threads()
        active_thread   = None
        thread_versions = []

        if request.method == "POST":
            action = request.form.get("action", "generate")

            if action == "refine":
                thread_id   = request.form.get("thread_id", "").strip()
                instruction = request.form.get("refinement_instruction", "").strip()
                thread      = get_prd_thread(thread_id) if thread_id else None
                source_raw  = load_prd_version(thread) if thread else None

                if not instruction:
                    error = "Please describe what you'd like to change."
                elif not source_raw:
                    error = "Could not load the PRD to refine."
                else:
                    refine_prompt = (
                        "You are refining an existing PRD based on a specific instruction.\n\n"
                        "Current PRD:\n\n"
                        f"{source_raw}\n\n"
                        "---\n\n"
                        f"Refinement instruction: {instruction}\n\n"
                        "Return the complete updated PRD. Preserve sections that don't need to change. "
                        "Apply the instruction precisely. Return only the PRD content — no preamble, no explanation."
                    )
                    try:
                        from config import CLAUDE_CMD
                        result = subprocess.run(
                            [CLAUDE_CMD, "--print"],
                            input=refine_prompt,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            timeout=300,
                        )
                        if result.returncode != 0:
                            error = f"Error: {result.stderr}"
                        else:
                            raw_output = result.stdout
                            save_prd(raw_output, thread_id=thread_id, instruction=instruction)
                            active_thread   = get_prd_thread(thread_id)
                            thread_versions = active_thread.get("versions", [])
                            output          = md.markdown(raw_output, extensions=["nl2br", "tables"])
                            prd_threads     = list_prd_threads()
                    except subprocess.TimeoutExpired:
                        error = "Claude took too long to respond."
                    except FileNotFoundError:
                        error = "Claude CLI not found."

                if error and thread:
                    active_thread   = thread
                    thread_versions = thread.get("versions", [])
                    raw_output = source_raw
                    if raw_output:
                        output = md.markdown(raw_output, extensions=["nl2br", "tables"])

            else:
                # Generate new PRD
                user_input = request.form.get("user_input", "").strip()
                if not tool["prompt"]:
                    error = "This tool is coming soon."
                elif len(user_input) > MAX_INPUT_CHARS:
                    error = f"Input too long ({len(user_input):,} characters). Please trim to under {MAX_INPUT_CHARS:,} characters."
                elif user_input:
                    try:
                        raw_output = run_claude(tool["prompt"], user_input, context=active_context)
                        new_thread_id, _ = save_prd(raw_output, title_hint=user_input)
                        active_thread   = get_prd_thread(new_thread_id)
                        thread_versions = active_thread.get("versions", [])
                        output          = md.markdown(raw_output, extensions=["nl2br", "tables"])
                        prd_threads     = list_prd_threads()
                    except subprocess.TimeoutExpired:
                        error = "Claude took too long to respond."
                    except FileNotFoundError:
                        error = "Claude CLI not found."

        else:
            # Load a thread by ID (and optional version number)
            requested_thread = request.args.get("thread")
            requested_version = request.args.get("version", type=int)
            if requested_thread:
                thread = get_prd_thread(requested_thread)
                if thread:
                    active_thread   = thread
                    thread_versions = thread.get("versions", [])
                    raw_output = load_prd_version(thread, version_num=requested_version)
                    if raw_output:
                        output = md.markdown(raw_output, extensions=["nl2br", "tables"])

        return render_template(
            "index.html",
            categories=CATEGORIES,
            tools=TOOLS,
            active_tool_id=tool_id,
            tool=tool,
            output=output,
            user_input=user_input,
            error=error,
            digest_dates=[],
            active_digest_date=None,
            format_date_label=format_date_label,
            max_input_chars=MAX_INPUT_CHARS,
            output_history=[],
            active_file=None,
            raw_output=raw_output,
            active_context=active_context,
            all_contexts=all_contexts,
            # PRD-specific
            prd_threads=prd_threads,
            active_thread=active_thread,
            thread_versions=thread_versions,
            prd_thread_label=prd_thread_label,
            active_version=requested_version if request.method == "GET" else None,
            # Analytics (empty for PRD)
            analytics_sessions=[],
            active_session=None,
            analytics_session_label=analytics_session_label,
            # Feedback (empty for PRD)
            feedback_sessions=[],
            active_feedback_session=None,
            feedback_session_label=feedback_session_label,
            csv_info="",
        )

    elif tool_id == "analytics":
        # ── Analytics: conversational sessions ─────────────────────────────
        tool_max_chars      = tool.get("max_chars", MAX_INPUT_CHARS)
        analytics_sessions  = list_analytics_sessions()
        active_session      = None
        csv_info            = ""  # populated when a CSV is preprocessed

        if request.method == "POST":
            action = request.form.get("action", "new_analysis")

            if action == "followup":
                session_id  = request.form.get("session_id", "").strip()
                followup_msg = request.form.get("followup_message", "").strip()
                session     = get_analytics_session(session_id) if session_id else None

                if not followup_msg:
                    error = "Please enter a follow-up question."
                    active_session = session
                elif not session:
                    error = "Could not load the session."
                else:
                    prompt = build_analytics_followup_prompt(session, followup_msg)
                    try:
                        from config import CLAUDE_CMD
                        result = subprocess.run(
                            [CLAUDE_CMD, "--print"],
                            input=prompt,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            timeout=300,
                        )
                        if result.returncode != 0:
                            error = f"Error: {result.stderr}"
                            active_session = session
                        else:
                            append_analytics_turn(session_id, followup_msg, result.stdout.strip())
                            active_session    = get_analytics_session(session_id)
                            analytics_sessions = list_analytics_sessions()
                    except subprocess.TimeoutExpired:
                        error = "Claude took too long to respond."
                        active_session = session
                    except FileNotFoundError:
                        error = "Claude CLI not found."
                        active_session = session

            else:
                # New analysis — file upload or textarea
                uploaded_file = request.files.get("meeting_file")
                if uploaded_file and uploaded_file.filename:
                    try:
                        raw_bytes  = uploaded_file.read().decode("utf-8", errors="replace").strip()
                        fname      = uploaded_file.filename
                        if fname.lower().endswith(".csv"):
                            user_input, csv_info = preprocess_csv(raw_bytes, fname)
                        else:
                            user_input = raw_bytes
                    except Exception:
                        error = "Could not read the uploaded file."
                else:
                    user_input = request.form.get("user_input", "").strip()

                if not error:
                    if len(user_input) > tool_max_chars:
                        error = f"Input too long ({len(user_input):,} chars). Please trim to under {tool_max_chars:,}."
                    elif user_input:
                        try:
                            response = run_claude(tool["prompt"], user_input, context=active_context)
                            session_id    = save_analytics_session(user_input, response.strip())
                            active_session = get_analytics_session(session_id)
                            analytics_sessions = list_analytics_sessions()
                        except subprocess.TimeoutExpired:
                            error = "Claude took too long to respond."
                        except FileNotFoundError:
                            error = "Claude CLI not found."
        else:
            requested_session = request.args.get("session")
            if requested_session:
                active_session = get_analytics_session(requested_session)

        return render_template(
            "index.html",
            categories=CATEGORIES,
            tools=TOOLS,
            active_tool_id=tool_id,
            tool=tool,
            output=None,
            user_input=user_input,
            error=error,
            digest_dates=[],
            active_digest_date=None,
            format_date_label=format_date_label,
            max_input_chars=tool_max_chars,
            output_history=[],
            active_file=None,
            raw_output=None,
            active_context=active_context,
            all_contexts=all_contexts,
            prd_threads=[],
            active_thread=None,
            thread_versions=[],
            prd_thread_label=prd_thread_label,
            active_version=None,
            analytics_sessions=analytics_sessions,
            active_session=active_session,
            analytics_session_label=analytics_session_label,
            feedback_sessions=[],
            active_feedback_session=None,
            feedback_session_label=feedback_session_label,
            csv_info=csv_info,
        )

    elif tool_id == "feedback":
        # ── Feedback: conversational sessions ──────────────────────────────
        tool_max_chars        = tool.get("max_chars", MAX_INPUT_CHARS)
        feedback_sessions     = list_feedback_sessions()
        active_feedback_session = None
        csv_info              = ""  # populated when a CSV is preprocessed

        if request.method == "POST":
            action = request.form.get("action", "new_analysis")

            if action == "followup":
                session_id   = request.form.get("session_id", "").strip()
                followup_msg = request.form.get("followup_message", "").strip()
                session      = get_feedback_session(session_id) if session_id else None

                if not followup_msg:
                    error = "Please enter a follow-up question."
                    active_feedback_session = session
                elif not session:
                    error = "Could not load the session."
                else:
                    prompt = build_feedback_followup_prompt(session, followup_msg)
                    try:
                        from config import CLAUDE_CMD
                        result = subprocess.run(
                            [CLAUDE_CMD, "--print"],
                            input=prompt,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            timeout=300,
                        )
                        if result.returncode != 0:
                            error = f"Error: {result.stderr}"
                            active_feedback_session = session
                        else:
                            append_feedback_turn(session_id, followup_msg, result.stdout.strip())
                            active_feedback_session = get_feedback_session(session_id)
                            feedback_sessions       = list_feedback_sessions()
                    except subprocess.TimeoutExpired:
                        error = "Claude took too long to respond."
                        active_feedback_session = session
                    except FileNotFoundError:
                        error = "Claude CLI not found."
                        active_feedback_session = session

            else:
                # New feedback analysis — file upload or textarea
                uploaded_file = request.files.get("meeting_file")
                if uploaded_file and uploaded_file.filename:
                    try:
                        raw_bytes = uploaded_file.read().decode("utf-8", errors="replace").strip()
                        fname     = uploaded_file.filename
                        if fname.lower().endswith(".csv"):
                            user_input, csv_info = preprocess_csv(raw_bytes, fname)
                        else:
                            user_input = raw_bytes
                    except Exception:
                        error = "Could not read the uploaded file."
                else:
                    user_input = request.form.get("user_input", "").strip()

                if not error:
                    if len(user_input) > tool_max_chars:
                        error = f"Input too long ({len(user_input):,} chars). Please trim to under {tool_max_chars:,}."
                    elif user_input:
                        try:
                            response = run_claude(tool["prompt"], user_input, context=active_context)
                            session_id            = save_feedback_session(user_input, response.strip())
                            active_feedback_session = get_feedback_session(session_id)
                            feedback_sessions     = list_feedback_sessions()
                        except subprocess.TimeoutExpired:
                            error = "Claude took too long to respond."
                        except FileNotFoundError:
                            error = "Claude CLI not found."
        else:
            requested_session = request.args.get("session")
            if requested_session:
                active_feedback_session = get_feedback_session(requested_session)

        return render_template(
            "index.html",
            categories=CATEGORIES,
            tools=TOOLS,
            active_tool_id=tool_id,
            tool=tool,
            output=None,
            user_input=user_input,
            error=error,
            digest_dates=[],
            active_digest_date=None,
            format_date_label=format_date_label,
            max_input_chars=tool_max_chars,
            output_history=[],
            active_file=None,
            raw_output=None,
            active_context=active_context,
            all_contexts=all_contexts,
            prd_threads=[],
            active_thread=None,
            thread_versions=[],
            prd_thread_label=prd_thread_label,
            active_version=None,
            analytics_sessions=[],
            active_session=None,
            analytics_session_label=analytics_session_label,
            feedback_sessions=feedback_sessions,
            active_feedback_session=active_feedback_session,
            feedback_session_label=feedback_session_label,
            csv_info=csv_info,
        )

    else:
        tool_max_chars = tool.get("max_chars", MAX_INPUT_CHARS)
        output_history = list_tool_outputs(tool_id)

        if request.method == "POST":
            # File upload takes priority over textarea for meeting tool
            uploaded_file = request.files.get("meeting_file")
            if uploaded_file and uploaded_file.filename:
                try:
                    user_input = uploaded_file.read().decode("utf-8", errors="replace").strip()
                except Exception:
                    error = "Could not read the uploaded file."
            else:
                user_input = request.form.get("user_input", "").strip()

            # Select prompt based on input mode (meeting tool only)
            input_mode = request.form.get("input_mode", "notes")
            if tool_id == "meeting" and input_mode == "transcript":
                prompt_file = "prompts/summarize_transcript.md"
            else:
                prompt_file = tool["prompt"]

            if not error:
                if not tool["prompt"]:
                    error = "This tool is coming soon."
                elif len(user_input) > tool_max_chars:
                    error = f"Input too long ({len(user_input):,} characters). Please trim to under {tool_max_chars:,} characters."
                elif user_input:
                    try:
                        raw_output = run_claude(prompt_file, user_input, context=active_context)
                        active_file = save_tool_output(tool_id, raw_output, title_hint=user_input)
                        output = md.markdown(raw_output, extensions=["nl2br", "tables"])
                        output_history = list_tool_outputs(tool_id)
                    except subprocess.TimeoutExpired:
                        error = "Claude took too long to respond."
                    except FileNotFoundError:
                        error = "Claude CLI not found."
        else:
            tool_max_chars = tool.get("max_chars", MAX_INPUT_CHARS)
            requested_file = request.args.get("file")
            filenames = [f for f, _ in output_history]
            if requested_file and requested_file in filenames:
                raw_output = load_tool_output(tool_id, requested_file)
                if raw_output:
                    output = md.markdown(raw_output, extensions=["nl2br", "tables"])
                    active_file = requested_file

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
        max_input_chars=tool.get("max_chars", MAX_INPUT_CHARS),
        output_history=output_history,
        active_file=active_file,
        raw_output=raw_output,
        active_context=active_context,
        all_contexts=all_contexts,
        # PRD-specific (empty for non-PRD tools)
        prd_threads=[],
        active_thread=None,
        thread_versions=[],
        prd_thread_label=prd_thread_label,
        active_version=None,
        # Analytics (empty for non-analytics tools)
        analytics_sessions=[],
        active_session=None,
        analytics_session_label=analytics_session_label,
        # Feedback (empty for non-feedback tools)
        feedback_sessions=[],
        active_feedback_session=None,
        feedback_session_label=feedback_session_label,
        csv_info="",
    )


# ---------------------------------------------------------------------------
# Context management routes
# ---------------------------------------------------------------------------

@app.route("/contexts", methods=["GET", "POST"])
def contexts_page():
    active_context = get_active_context()
    all_contexts   = list_contexts()
    edit_context   = None
    error          = None
    success        = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            error = "Context Name is required."
        else:
            original_id = request.form.get("original_id", "").strip()
            context_id  = original_id if original_id else make_context_id(name)
            context_dict = {"id": context_id, "name": name}
            for field in CONTEXT_FIELDS:
                key = field["key"]
                if key == "name":
                    continue
                value = request.form.get(key, "").strip()
                if value:
                    context_dict[key] = value
            save_context_data(context_dict)
            all_contexts = list_contexts()
            success = f"Context \"{name}\" saved."
    else:
        edit_id = request.args.get("edit")
        if edit_id:
            path = os.path.join(CONTEXTS_DIR, f"{edit_id}.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    edit_context = json.load(f)

    return render_template(
        "contexts.html",
        categories=CATEGORIES,
        tools=TOOLS,
        active_context=active_context,
        all_contexts=all_contexts,
        edit_context=edit_context,
        context_fields=CONTEXT_FIELDS,
        error=error,
        success=success,
    )


@app.route("/contexts/parse", methods=["POST"])
def parse_context():
    """Parse a free-form company description into structured context fields using Claude."""
    description = request.form.get("description", "").strip()
    if not description:
        return {"error": "No description provided."}, 400

    field_keys = [f["key"] for f in CONTEXT_FIELDS]
    keys_json  = json.dumps({k: "..." for k in field_keys}, indent=2)

    prompt = (
        "Extract company and product context from the description below.\n"
        "Return ONLY valid JSON using these exact keys (omit any you cannot confidently infer):\n\n"
        f"{keys_json}\n\n"
        "Rules:\n"
        "- name: a short label like 'Acme Corp' or 'Acme — Analytics'\n"
        "- Keep values concise (one sentence max per field)\n"
        "- Do NOT invent information not present in the description\n"
        "- Return raw JSON only — no markdown fences, no explanation\n\n"
        f"Description:\n{description}"
    )

    from config import CLAUDE_CMD
    result = subprocess.run(
        [CLAUDE_CMD, "--print"],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    if result.returncode != 0:
        return {"error": result.stderr or "Claude call failed."}, 500

    raw = result.stdout.strip()
    # Strip markdown fences if Claude wraps output anyway
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "Could not parse Claude's response as JSON.", "raw": raw}, 500

    # Only return keys we actually use
    filtered = {k: v for k, v in parsed.items() if k in field_keys and isinstance(v, str) and v.strip()}
    return filtered


@app.route("/contexts/<context_id>/activate", methods=["POST"])
def activate_context(context_id):
    set_active_context(context_id)
    return redirect(request.referrer or "/")


@app.route("/contexts/<context_id>/delete", methods=["POST"])
def delete_context_route(context_id):
    delete_context_data(context_id)
    return redirect("/contexts")


if __name__ == "__main__":
    app.run(debug=True)
