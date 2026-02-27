#!/usr/bin/env python
import argparse
import json
import logging
import os
import re
import sys
import warnings
from datetime import datetime
from pathlib import Path
from time import sleep

from bot.crew import Bot

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pysbd")
logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
logging.getLogger("litellm").setLevel(logging.CRITICAL)

# Hard provider caps enforced regardless of environment overrides.
HARD_LIMITS = {
    "rpm": 30,
    "rpd": 14400,
    "tpm": int(os.getenv("LLM_PROVIDER_TPM_CAP", "6000")),
    "tpd": 500000,
}


# Create a starter output skeleton on first run.
def _ensure_output_file_exists() -> None:
    """Auto-create output.md when missing."""
    output_path = Path("output.md")
    if output_path.exists():
        return
    output_path.write_text(
        (
            "# Travel Plan: <Destination>\n\n"
            "## Destination Overview\n\n"
            "## Budget Breakdown\n\n"
            "## Day-wise Itinerary\n\n"
            "## Validation Summary\n"
            "- Budget status: <Under / At / Over budget>\n"
            "- Assumptions: <key assumptions>\n"
            "- Risk factors: <key risks>\n"
        ),
        encoding="utf-8",
    )


# Clear previous report so each run writes a fresh final document.
def _reset_final_output_file() -> None:
    """Ensure final report always overwrites previous content."""
    Path("output.md").write_text("", encoding="utf-8")


# Ensure required top-level sections exist even if LLM output is partial.
def _ensure_required_output_sections(inputs: dict) -> None:
    """Guarantee required sections exist in output.md."""
    output_path = Path("output.md")
    content = output_path.read_text(encoding="utf-8") if output_path.exists() else ""

    required_sections = [
        (
            r"(?im)^\s*#?\s*travel\s*plan\s*:",
            f"# Travel Plan: {inputs.get('destination', '<Destination>')}",
        ),
        (r"(?im)^\s*##\s*destination\s*overview\s*:?\s*$", "## Destination Overview"),
        (
            r"(?im)^\s*##\s*budget\s*breakdown\s*:?\s*$",
            "## Budget Breakdown",
        ),
        (r"(?im)^\s*##\s*day-wise\s*itinerary\s*:?\s*$", "## Day-wise Itinerary"),
        (
            r"(?im)^\s*##\s*validation\s*summary\s*:?\s*$",
            "## Validation Summary\n- Budget status: <Under / At / Over budget>\n- Assumptions: <key assumptions>\n- Risk factors: <key risks>",
        ),
    ]

    # Append only the sections that are absent.
    missing_lines = [line for pattern, line in required_sections if not re.search(pattern, content)]
    if missing_lines:
        suffix = ("\n\n" if content.strip() else "") + "\n".join(missing_lines) + "\n"
        output_path.write_text(content + suffix, encoding="utf-8")


# Parse numeric totals from markdown budget rows.
def _extract_budget_total_from_markdown(content: str) -> float | None:
    """Extract budget total from markdown table rows when available."""
    # Prefer Grand Total if present, otherwise use Total.
    for label in ("Grand Total", "Total"):
        pattern = rf"(?im)^\|\s*{re.escape(label)}\s*\|\s*([^|]+?)\s*\|"
        match = re.search(pattern, content)
        if not match:
            continue
        amount_text = re.sub(r"[^\d.\-]", "", match.group(1))
        if not amount_text:
            continue
        try:
            return float(amount_text)
        except ValueError:
            continue
    return None


# Fill unresolved validation placeholders with concrete defaults.
def _upsert_validation_summary(content: str, inputs: dict) -> str:
    """Replace placeholder validation values with concrete defaults."""
    total_estimate = _extract_budget_total_from_markdown(content)
    budget_cap = float(inputs.get("budget", 0))

    # Derive budget status from computed total vs user budget cap.
    status = "Unknown"
    if total_estimate is not None and budget_cap > 0:
        if total_estimate < budget_cap:
            status = "Under budget"
        elif total_estimate > budget_cap:
            status = "Over budget"
        else:
            status = "At budget"

    replacements = {
        r"(?im)^- Budget status:\s*<[^>]+>\s*$": f"- Budget status: {status}",
        r"(?im)^- Assumptions:\s*<[^>]+>\s*$": "- Assumptions: Cost estimates may vary by season, availability, and booking timing.",
        r"(?im)^- Risk factors:\s*<[^>]+>\s*$": "- Risk factors: Price fluctuations, attraction closures, and transport delays can affect this plan.",
        r"(?im)^\|\s*Budget status\s*\|\s*<[^>]+>\s*\|\s*$": f"| Budget status | {status} |",
        r"(?im)^\|\s*Assumptions\s*\|\s*<[^>]+>\s*\|\s*$": "| Assumptions | Cost estimates may vary by season, availability, and booking timing. |",
        r"(?im)^\|\s*Risk factors\s*\|\s*<[^>]+>\s*\|\s*$": "| Risk factors | Price fluctuations, attraction closures, and transport delays can affect this plan. |",
    }

    updated = content
    for pattern, replacement in replacements.items():
        updated = re.sub(pattern, replacement, updated)
    return updated


# Keep execution log file available for external tooling/hooks.
def _ensure_execution_log_file() -> None:
   
    log_path = Path("logs/execution.log")
    txt_log_path = Path("logs/execution.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        return
    if txt_log_path.exists():
        log_path.write_text(txt_log_path.read_text(encoding="utf-8"), encoding="utf-8")
        return
    log_path.write_text("", encoding="utf-8")


# Convert date-range input into inclusive trip-day count.
def _parse_trip_days(travel_dates: str) -> int:
    """Parse `YYYY-MM-DD to YYYY-MM-DD`; return inclusive day count with safe fallback."""
    try:
        parts = [p.strip() for p in travel_dates.split("to")]
        if len(parts) != 2:
            return 3
        start = datetime.strptime(parts[0], "%Y-%m-%d")
        end = datetime.strptime(parts[1], "%Y-%m-%d")
        if end < start:
            return 3
        return (end - start).days + 1
    except ValueError:
        return 3


# Build normalized runtime inputs from args/env/interactive prompts.
def _build_inputs_from_args() -> dict:
    parser = argparse.ArgumentParser(description="Run AI Travel Planner crew")
    parser.add_argument("--destination", default=os.getenv("TRAVEL_DESTINATION", "Kyoto, Japan"))
    parser.add_argument("--travel-dates", default=os.getenv("TRAVEL_DATES", "2026-04-10 to 2026-04-14"))
    parser.add_argument("--budget", type=float, default=float(os.getenv("TRAVEL_BUDGET", "1500")))
    parser.add_argument(
        "--preferences",
        default=os.getenv("TRAVEL_PREFERENCES", "culture, food, walking, low-cost local experiences"),
    )
    parser.add_argument("--currency", default=os.getenv("TRAVEL_CURRENCY", "USD"))
    parser.add_argument("--interactive", action="store_true", help="Prompt for inputs in terminal")

    args, _ = parser.parse_known_args()

    # Auto-enable prompt mode when launched manually without flags.
    auto_interactive = len(sys.argv) == 1 and sys.stdin.isatty()
    if args.interactive or auto_interactive:
        default_destination = args.destination
        default_dates = args.travel_dates
        default_budget = args.budget
        default_currency = args.currency
        default_preferences = args.preferences

        print("Enter travel planner inputs (press Enter to keep default/example value):")
        destination = (
            input(f"Destination [example: Bali, Indonesia]: ").strip()
            or default_destination
        )
        travel_dates = (
            input(f"Travel Dates [example: 2026-06-10 to 2026-06-14]: ").strip()
            or default_dates
        )
        budget_str = input(f"Budget [example: 900]: ").strip()
        currency = input(f"Currency [example: USD]: ").strip() or default_currency
        preferences = (
            input(
                f"Preferences [example: beaches, local food, temples]: "
            ).strip()
            or default_preferences
        )
        try:
            budget = float(budget_str) if budget_str else float(default_budget)
        except ValueError:
            budget = float(default_budget)

        args.destination = destination
        args.travel_dates = travel_dates
        args.budget = budget
        args.currency = currency
        args.preferences = preferences

    trip_days = _parse_trip_days(args.travel_dates)

    return {
        "destination": args.destination,
        "travel_dates": args.travel_dates,
        "budget": args.budget,
        "preferences": args.preferences,
        "currency": args.currency,
        "trip_days": trip_days,
        "current_year": str(datetime.now().year),
    }


# Resolve and create quota state path.
def _quota_file() -> Path:
    path = Path("logs/quota_usage.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# Load persisted quota counters with safe defaults.
def _load_quota_state() -> dict:
    path = _quota_file()
    if not path.exists():
        return {"days": {}, "minutes": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"days": {}, "minutes": {}}
    return {
        "days": loaded.get("days", {}),
        "minutes": loaded.get("minutes", {}),
    }


# Persist quota counters after each successful run.
def _save_quota_state(state: dict) -> None:
    _quota_file().write_text(json.dumps(state, indent=2), encoding="utf-8")


# Rough token estimate to guard against hard TPM limits.
def _estimate_tokens_for_inputs(inputs: dict) -> int:
    payload_size = len(json.dumps(inputs))
    estimated = max(800, payload_size // 2)
    configured = int(os.getenv("LLM_EST_TOKENS_PER_RUN", str(estimated)))
    return min(configured, HARD_LIMITS["tpm"])


# Apply configured limits while respecting hard caps.
def _effective_limit(env_name: str, hard_cap: int) -> int:
    return min(int(os.getenv(env_name, str(hard_cap))), hard_cap)


# Enforce daily/per-minute request and token budgets.
def _check_quota(inputs: dict) -> None:
    requests_per_run = _effective_limit("LLM_EST_REQUESTS_PER_RUN", HARD_LIMITS["rpm"])
    tokens_per_run = _estimate_tokens_for_inputs(inputs)
    daily_limit = _effective_limit("LLM_DAILY_LIMIT", HARD_LIMITS["rpd"])
    daily_token_limit = _effective_limit("LLM_DAILY_TOKEN_LIMIT", HARD_LIMITS["tpd"])

    if requests_per_run > HARD_LIMITS["rpm"]:
        raise Exception(f"Per-run request estimate exceeds hard RPM cap ({HARD_LIMITS['rpm']}).")
    if tokens_per_run > HARD_LIMITS["tpm"]:
        raise Exception(f"Per-run token estimate exceeds hard TPM cap ({HARD_LIMITS['tpm']}).")

    today = datetime.now().strftime("%Y-%m-%d")
    this_minute = datetime.now().strftime("%Y-%m-%d %H:%M")
    state = _load_quota_state()

    day_entry = state["days"].get(today, {"requests": 0, "tokens": 0})
    minute_entry = state["minutes"].get(this_minute, {"requests": 0, "tokens": 0})

    used_daily_requests = int(day_entry.get("requests", 0))
    used_daily_tokens = int(day_entry.get("tokens", 0))
    used_minute_requests = int(minute_entry.get("requests", 0))
    used_minute_tokens = int(minute_entry.get("tokens", 0))

    # Fail fast for daily limits.
    if used_daily_requests + requests_per_run > daily_limit:
        raise Exception(
            f"Daily LLM request limit reached: {used_daily_requests}/{daily_limit}. "
            "Try again tomorrow or increase quota."
        )
    if used_daily_tokens + tokens_per_run > daily_token_limit:
        raise Exception(
            f"Daily LLM token limit reached: {used_daily_tokens}/{daily_token_limit}. "
            "Try again tomorrow or reduce token usage."
        )
    # Soft-throttle minute limits instead of failing immediately.
    if used_minute_requests + requests_per_run > HARD_LIMITS["rpm"]:
        # Throttle until next minute window to avoid hard-failing on RPM.
        seconds_to_next_minute = max(1, 60 - datetime.now().second)
        print(
            f"Per-minute request limit near cap ({used_minute_requests}/{HARD_LIMITS['rpm']}). "
            f"Sleeping {seconds_to_next_minute}s to stay within limits..."
        )
        sleep(seconds_to_next_minute)
        return _check_quota(inputs)
    if used_minute_tokens + tokens_per_run > HARD_LIMITS["tpm"]:
        # Throttle until next minute window to avoid hard-failing on TPM.
        seconds_to_next_minute = max(1, 60 - datetime.now().second)
        print(
            f"Per-minute token limit near cap ({used_minute_tokens}/{HARD_LIMITS['tpm']}). "
            f"Sleeping {seconds_to_next_minute}s to stay within limits..."
        )
        sleep(seconds_to_next_minute)
        return _check_quota(inputs)


# Record estimated usage into daily and minute windows.
def _record_usage(inputs: dict) -> None:
    requests_per_run = _effective_limit("LLM_EST_REQUESTS_PER_RUN", HARD_LIMITS["rpm"])
    tokens_per_run = _estimate_tokens_for_inputs(inputs)
    today = datetime.now().strftime("%Y-%m-%d")
    this_minute = datetime.now().strftime("%Y-%m-%d %H:%M")
    state = _load_quota_state()

    day_entry = state["days"].get(today, {"requests": 0, "tokens": 0})
    day_entry["requests"] = int(day_entry.get("requests", 0)) + requests_per_run
    day_entry["tokens"] = int(day_entry.get("tokens", 0)) + tokens_per_run
    state["days"][today] = day_entry

    minute_entry = state["minutes"].get(this_minute, {"requests": 0, "tokens": 0})
    minute_entry["requests"] = int(minute_entry.get("requests", 0)) + requests_per_run
    minute_entry["tokens"] = int(minute_entry.get("tokens", 0)) + tokens_per_run
    state["minutes"][this_minute] = minute_entry

    # Keep recent minute windows only.
    minute_keys = sorted(state["minutes"].keys())
    if len(minute_keys) > 120:
        for old_key in minute_keys[:-120]:
            state["minutes"].pop(old_key, None)

    _save_quota_state(state)


# Detect provider rate-limit style failures.
def _is_rate_limit_error(err: Exception) -> bool:
    message = str(err).lower()
    return "429" in message or "rate limit" in message or "quota" in message


# Extract provider-advised retry wait when available.
def _extract_retry_seconds(message: str) -> int | None:
    # Matches provider hints like "try again in 8.57s".
    match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", message.lower())
    if not match:
        return None
    try:
        return max(2, int(float(match.group(1))) + int(os.getenv("LLM_RETRY_BUFFER_SECONDS", "2")))
    except ValueError:
        return None


# Retry kickoff with exponential backoff on rate-limit errors.
def _kickoff_with_backoff(inputs: dict):
    max_attempts = int(os.getenv("LLM_MAX_RETRIES", "3"))
    initial_sleep = int(os.getenv("LLM_BACKOFF_SECONDS", "10"))
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            return Bot().crew().kickoff(inputs=inputs)
        except Exception as e:
            last_error = e
            if not _is_rate_limit_error(e) or attempt == max_attempts:
                break
            wait_seconds = _extract_retry_seconds(str(e)) or (initial_sleep * (2 ** (attempt - 1)))
            print(f"Rate limit hit, retrying in {wait_seconds}s (attempt {attempt}/{max_attempts})...")
            sleep(wait_seconds)

    raise Exception(f"Crew kickoff failed after retries: {last_error}")


# Primary local entrypoint for standard runs.
def run():
    """Run the travel planner crew."""
    _ensure_output_file_exists()
    inputs = _build_inputs_from_args()
    try:
        _reset_final_output_file()
        _check_quota(inputs)
        result = _kickoff_with_backoff(inputs)
        _record_usage(inputs)
        _ensure_required_output_sections(inputs)
        # Final cleanup pass to replace unresolved placeholders.
        output_path = Path("output.md")
        output_path.write_text(
            _upsert_validation_summary(output_path.read_text(encoding="utf-8"), inputs),
            encoding="utf-8",
        )
        _ensure_execution_log_file()
        print(result)
    except Exception as e:
        raise Exception(f"An error occurred while running the crew: {e}")


# CrewAI training mode entrypoint.
def train():
    """Train the crew for a given number of iterations."""
    inputs = _build_inputs_from_args()
    try:
        Bot().crew().train(n_iterations=int(sys.argv[1]), filename=sys.argv[2], inputs=inputs)
    except Exception as e:
        raise Exception(f"An error occurred while training the crew: {e}")


# Replay a prior run from a selected task id.
def replay():
    """Replay the crew execution from a specific task."""
    try:
        Bot().crew().replay(task_id=sys.argv[1])
    except Exception as e:
        raise Exception(f"An error occurred while replaying the crew: {e}")


# CrewAI test mode entrypoint.
def test():
    """Test crew execution and return the results."""
    inputs = _build_inputs_from_args()
    try:
        Bot().crew().test(n_iterations=int(sys.argv[1]), eval_llm=sys.argv[2], inputs=inputs)
    except Exception as e:
        raise Exception(f"An error occurred while testing the crew: {e}")


# Trigger-based entrypoint for automation/webhook flows.
def run_with_trigger():
    """Run the crew with trigger payload."""
    _ensure_output_file_exists()
    if len(sys.argv) < 2:
        raise Exception("No trigger payload provided. Please provide JSON payload as argument.")

    try:
        trigger_payload = json.loads(sys.argv[1])
    except json.JSONDecodeError:
        raise Exception("Invalid JSON payload provided as argument")

    # Merge trigger payload into the same input envelope.
    inputs = _build_inputs_from_args()
    inputs["crewai_trigger_payload"] = trigger_payload

    try:
        _reset_final_output_file()
        _check_quota(inputs)
        result = _kickoff_with_backoff(inputs)
        _record_usage(inputs)
        _ensure_required_output_sections(inputs)
        output_path = Path("output.md")
        output_path.write_text(
            _upsert_validation_summary(output_path.read_text(encoding="utf-8"), inputs),
            encoding="utf-8",
        )
        _ensure_execution_log_file()
        return result
    except Exception as e:
        raise Exception(f"An error occurred while running the crew with trigger: {e}")


if __name__ == "__main__":
    run()
