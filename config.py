"""
config.py
Shared configuration and utilities for PMBuddy.
Both app.py and scripts/send_digest.py import from here.
"""

import subprocess

CLAUDE_CMD = r"C:\Users\user\AppData\Roaming\npm\claude.cmd"

MAX_INPUT_CHARS = 12_000  # hard cap on textarea input sent to Claude


def build_context_block(context):
    """Convert an active context dict into a prompt preamble."""
    if not context:
        return ""

    field_labels = [
        ("industry",       "Industry"),
        ("product",        "Product"),
        ("users",          "Target users"),
        ("stage",          "Stage"),
        ("business_model", "Business model"),
        ("key_metrics",    "Key metrics"),
        ("team_structure", "Team structure"),
        ("competitors",    "Competitors"),
        ("notes",          "Notes"),
    ]

    lines = [
        f"You are a Product Manager at: {context.get('name', '')}",
        "",
    ]
    for key, label in field_labels:
        value = context.get(key, "").strip()
        if value:
            lines.append(f"{label}: {value}")

    lines += [
        "",
        "Use this context throughout your response. Apply relevant industry norms, "
        "user expectations, and business constraints where appropriate.",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def run_claude(prompt_file, input_text, context=None):
    """Read a prompt file, substitute {{input}}, and send to Claude CLI.
    If context is provided, prepend a context block before the prompt.
    """
    with open(prompt_file, "r", encoding="utf-8") as f:
        prompt = f.read()

    full_prompt = prompt.replace("{{input}}", input_text)

    if context:
        full_prompt = build_context_block(context) + full_prompt

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
