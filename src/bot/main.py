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

HARD_LIMITS = {
    "rpm": 30,
    "rpd": 14400,
    "tpm": int(os.getenv("LLM_PROVIDER_TPM_CAP", "6000")),
    "tpd": 500000,
}


def _ensure_output_file_exists() -> None:
    """Auto-create output.md when missing."""
    output_path = Path("output.md")
    if output_path.exists():
        return
    output_path.write_text(
        (
            "Travel Plan: <Destination>\n"
            "Budget Breakdown: Accommodation, Food, Transport, Activities, Total\n"
            "Day-wise Itinerary\n"
            "Validation Summary: Budget status, Assumptions, Risk factors\n"
        ),
        encoding="utf-8",
    )


def _reset_final_output_file() -> None:
    """Ensure final report always overwrites previous content."""
    Path("output.md").write_text("", encoding="utf-8")


def _ensure_required_output_sections(inputs: dict) -> None:
    """Guarantee required sections exist in output.md."""
    output_path = Path("output.md")
    content = output_path.read_text(encoding="utf-8") if output_path.exists() else ""

    required_sections = [
        (r"(?im)^\s*travel\s*plan\s*:", f"Travel Plan: {inputs.get('destination', '<Destination>')}"),
        (
            r"(?im)^\s*budget\s*breakdown\s*:",
            "Budget Breakdown: Accommodation, Food, Transport, Activities, Total",
        ),
        (r"(?im)^\s*day-wise\s*itinerary\s*:?", "Day-wise Itinerary"),
        (
            r"(?im)^\s*validation\s*summary\s*:",
            "Validation Summary: Budget status, Assumptions, Risk factors",
        ),
    ]

    missing_lines = [line for pattern, line in required_sections if not re.search(pattern, content)]
    if missing_lines:
        suffix = ("\n\n" if content.strip() else "") + "\n".join(missing_lines) + "\n"
        output_path.write_text(content + suffix, encoding="utf-8")


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

    auto_interactive = len(sys.argv) == 1 and sys.stdin.isatty()
    if args.interactive or auto_interactive:
        default_destination = args.destination
        default_dates = args.travel_dates
        default_budget = args.budget
        default_currency = args.currency
        default_preferences = args.preferences

        print("Enter travel planner inputs (press Enter to keep default/example value):")
        destination = (
            input(f"Destination [example: Bali, Indonesia] ({default_destination}): ").strip()
            or default_destination
        )
        travel_dates = (
            input(f"Travel Dates [example: 2026-06-10 to 2026-06-14] ({default_dates}): ").strip()
            or default_dates
        )
        budget_str = input(f"Budget [example: 900] ({default_budget}): ").strip()
        currency = input(f"Currency [example: USD] ({default_currency}): ").strip() or default_currency
        preferences = (
            input(
                f"Preferences [example: beaches, local food, temples] ({default_preferences}): "
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


def _quota_file() -> Path:
    path = Path("logs/quota_usage.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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


def _save_quota_state(state: dict) -> None:
    _quota_file().write_text(json.dumps(state, indent=2), encoding="utf-8")


def _estimate_tokens_for_inputs(inputs: dict) -> int:
    payload_size = len(json.dumps(inputs))
    estimated = max(800, payload_size // 2)
    configured = int(os.getenv("LLM_EST_TOKENS_PER_RUN", str(estimated)))
    return min(configured, HARD_LIMITS["tpm"])


def _effective_limit(env_name: str, hard_cap: int) -> int:
    return min(int(os.getenv(env_name, str(hard_cap))), hard_cap)


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
    if used_minute_requests + requests_per_run > HARD_LIMITS["rpm"]:
        raise Exception(
            f"Per-minute request limit reached: {used_minute_requests}/{HARD_LIMITS['rpm']}. "
            "Wait a minute and retry."
        )
    if used_minute_tokens + tokens_per_run > HARD_LIMITS["tpm"]:
        raise Exception(
            f"Per-minute token limit reached: {used_minute_tokens}/{HARD_LIMITS['tpm']}. "
            "Wait a minute and retry."
        )


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


def _is_rate_limit_error(err: Exception) -> bool:
    message = str(err).lower()
    return "429" in message or "rate limit" in message or "quota" in message


def _extract_retry_seconds(message: str) -> int | None:
    # Matches provider hints like "try again in 8.57s".
    match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", message.lower())
    if not match:
        return None
    try:
        return max(2, int(float(match.group(1))) + int(os.getenv("LLM_RETRY_BUFFER_SECONDS", "2")))
    except ValueError:
        return None


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
        print(result)
    except Exception as e:
        raise Exception(f"An error occurred while running the crew: {e}")


def train():
    """Train the crew for a given number of iterations."""
    inputs = _build_inputs_from_args()
    try:
        Bot().crew().train(n_iterations=int(sys.argv[1]), filename=sys.argv[2], inputs=inputs)
    except Exception as e:
        raise Exception(f"An error occurred while training the crew: {e}")


def replay():
    """Replay the crew execution from a specific task."""
    try:
        Bot().crew().replay(task_id=sys.argv[1])
    except Exception as e:
        raise Exception(f"An error occurred while replaying the crew: {e}")


def test():
    """Test crew execution and return the results."""
    inputs = _build_inputs_from_args()
    try:
        Bot().crew().test(n_iterations=int(sys.argv[1]), eval_llm=sys.argv[2], inputs=inputs)
    except Exception as e:
        raise Exception(f"An error occurred while testing the crew: {e}")


def run_with_trigger():
    """Run the crew with trigger payload."""
    _ensure_output_file_exists()
    if len(sys.argv) < 2:
        raise Exception("No trigger payload provided. Please provide JSON payload as argument.")

    try:
        trigger_payload = json.loads(sys.argv[1])
    except json.JSONDecodeError:
        raise Exception("Invalid JSON payload provided as argument")

    inputs = _build_inputs_from_args()
    inputs["crewai_trigger_payload"] = trigger_payload

    try:
        _reset_final_output_file()
        _check_quota(inputs)
        result = _kickoff_with_backoff(inputs)
        _record_usage(inputs)
        _ensure_required_output_sections(inputs)
        return result
    except Exception as e:
        raise Exception(f"An error occurred while running the crew with trigger: {e}")


if __name__ == "__main__":
    run()
