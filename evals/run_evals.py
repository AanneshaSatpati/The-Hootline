#!/usr/bin/env python3
"""Eval runner — exercises actual app endpoints and produces a pass/fail report.

Usage:
    python evals/run_evals.py [--base-url URL] [--grade] [--filter PATTERN]

Without --grade: runs all eval tasks and checks assertions locally (no AI needed).
With --grade: also sends failing results to Claude for root-cause analysis.
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

import requests
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EVALS_DIR = Path(__file__).parent / "tasks"
REPORT_PATH = Path(__file__).parent / "report.json"


def load_tasks(filter_pattern: str = "") -> list[dict]:
    """Load all eval tasks from YAML files."""
    tasks = []
    for yaml_file in sorted(EVALS_DIR.glob("*.yaml")):
        with open(yaml_file) as f:
            file_tasks = yaml.safe_load(f)
            if isinstance(file_tasks, list):
                tasks.extend(file_tasks)
    if filter_pattern:
        tasks = [t for t in tasks if re.search(filter_pattern, t["id"])]
    return tasks


def run_setup(base_url: str, setup_steps: list[dict]) -> None:
    """Run setup steps before a test."""
    for step in setup_steps:
        method = step.get("method", "GET").upper()
        url = f"{base_url}{step['endpoint']}"
        try:
            if method == "POST":
                form_data = step.get("request", {}).get("form", {})
                requests.post(url, data=form_data, timeout=30)
            else:
                requests.get(url, timeout=30)
        except Exception as e:
            logger.warning("Setup step failed: %s %s — %s", method, url, e)
        # Brief pause between setup steps
        time.sleep(0.2)


def check_assertions(task: dict, response: requests.Response) -> list[dict]:
    """Check all assertions against the response. Returns list of failures."""
    failures = []
    assertions = task.get("assertions", [])

    for assertion in assertions:
        # Status code check
        if "status" in assertion:
            expected = assertion["status"]
            if isinstance(expected, list):
                if response.status_code not in expected:
                    failures.append({
                        "assertion": f"status in {expected}",
                        "actual": response.status_code,
                        "message": f"Expected status in {expected}, got {response.status_code}",
                    })
            else:
                if response.status_code != expected:
                    failures.append({
                        "assertion": f"status == {expected}",
                        "actual": response.status_code,
                        "message": f"Expected status {expected}, got {response.status_code}",
                    })
            continue

        # Body contains check
        if "body_contains" in assertion:
            text = assertion["body_contains"]
            op = assertion.get("operator", "contains")
            if op == "not_contains":
                if text in response.text:
                    failures.append({
                        "assertion": f"body not_contains '{text}'",
                        "actual": f"found '{text}' in body",
                        "message": f"Body should NOT contain '{text}'",
                    })
            else:
                if text not in response.text:
                    failures.append({
                        "assertion": f"body contains '{text}'",
                        "actual": response.text[:200],
                        "message": f"Body should contain '{text}'",
                    })
            continue

        # JSON array check
        if "json_is_array" in assertion:
            try:
                data = response.json()
                if not isinstance(data, list):
                    failures.append({
                        "assertion": "json_is_array",
                        "actual": type(data).__name__,
                        "message": f"Expected JSON array, got {type(data).__name__}",
                    })
            except Exception as e:
                failures.append({
                    "assertion": "json_is_array",
                    "actual": str(e),
                    "message": f"Failed to parse JSON: {e}",
                })
            continue

        # JSON path checks
        if "json_path" in assertion:
            path = assertion["json_path"]
            operator = assertion.get("operator", "exists")
            expected_value = assertion.get("value")

            try:
                data = response.json()
            except Exception as e:
                failures.append({
                    "assertion": f"json_path '{path}'",
                    "actual": str(e),
                    "message": f"Failed to parse JSON: {e}",
                })
                continue

            # Navigate the JSON path
            actual = _resolve_json_path(data, path)

            if operator == "exists":
                if actual is _MISSING:
                    failures.append({
                        "assertion": f"json_path '{path}' exists",
                        "actual": "missing",
                        "message": f"Expected '{path}' to exist in response",
                    })
            elif operator == "not_exists":
                if actual is not _MISSING:
                    failures.append({
                        "assertion": f"json_path '{path}' not_exists",
                        "actual": actual,
                        "message": f"Expected '{path}' to NOT exist",
                    })
            elif operator == "eq":
                if actual is _MISSING:
                    failures.append({
                        "assertion": f"json_path '{path}' == {expected_value!r}",
                        "actual": "missing",
                        "message": f"Expected '{path}' to equal {expected_value!r}, but path is missing",
                    })
                elif actual != expected_value:
                    failures.append({
                        "assertion": f"json_path '{path}' == {expected_value!r}",
                        "actual": actual,
                        "message": f"Expected {expected_value!r}, got {actual!r}",
                    })
            elif operator == "contains":
                if actual is _MISSING:
                    failures.append({
                        "assertion": f"json_path '{path}' contains '{expected_value}'",
                        "actual": "missing",
                        "message": f"Path '{path}' is missing",
                    })
                elif expected_value not in str(actual):
                    failures.append({
                        "assertion": f"json_path '{path}' contains '{expected_value}'",
                        "actual": actual,
                        "message": f"Expected to contain '{expected_value}', got {actual!r}",
                    })
            elif operator == "gt":
                if actual is _MISSING:
                    failures.append({
                        "assertion": f"json_path '{path}' > {expected_value}",
                        "actual": "missing",
                        "message": f"Path '{path}' is missing",
                    })
                elif not (actual > expected_value):
                    failures.append({
                        "assertion": f"json_path '{path}' > {expected_value}",
                        "actual": actual,
                        "message": f"Expected > {expected_value}, got {actual}",
                    })
            elif operator == "lt":
                if actual is _MISSING:
                    failures.append({
                        "assertion": f"json_path '{path}' < {expected_value}",
                        "actual": "missing",
                        "message": f"Path '{path}' is missing",
                    })
                elif not (actual < expected_value):
                    failures.append({
                        "assertion": f"json_path '{path}' < {expected_value}",
                        "actual": actual,
                        "message": f"Expected < {expected_value}, got {actual}",
                    })
            elif operator == "matches":
                if actual is _MISSING:
                    failures.append({
                        "assertion": f"json_path '{path}' matches '{expected_value}'",
                        "actual": "missing",
                        "message": f"Path '{path}' is missing",
                    })
                elif not re.match(expected_value, str(actual)):
                    failures.append({
                        "assertion": f"json_path '{path}' matches '{expected_value}'",
                        "actual": actual,
                        "message": f"Expected to match '{expected_value}', got {actual!r}",
                    })

    return failures


class _MissingSentinel:
    """Sentinel for missing JSON paths."""
    pass

_MISSING = _MissingSentinel()


def _resolve_json_path(data, path: str):
    """Resolve a dot-notation JSON path. Returns _MISSING if not found."""
    if path == "":
        return data

    # Handle array index notation like [0].id
    parts = re.split(r'\.(?![^\[]*\])', path)
    current = data

    for part in parts:
        if current is _MISSING:
            return _MISSING

        # Array index: [0]
        array_match = re.match(r'^\[(\d+)\](.*)$', part)
        if array_match:
            idx = int(array_match.group(1))
            rest = array_match.group(2).lstrip('.')
            if not isinstance(current, list) or idx >= len(current):
                return _MISSING
            current = current[idx]
            if rest:
                current = _resolve_json_path(current, rest)
            continue

        # Dict key with optional array index: key[0]
        key_match = re.match(r'^(\w+)\[(\d+)\](.*)$', part)
        if key_match:
            key = key_match.group(1)
            idx = int(key_match.group(2))
            rest = key_match.group(3).lstrip('.')
            if not isinstance(current, dict) or key not in current:
                return _MISSING
            current = current[key]
            if not isinstance(current, list) or idx >= len(current):
                return _MISSING
            current = current[idx]
            if rest:
                current = _resolve_json_path(current, rest)
            continue

        # Simple dict key
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        else:
            return _MISSING

    return current


def run_task(base_url: str, task: dict) -> dict:
    """Run a single eval task and return the result."""
    task_id = task["id"]
    method = task.get("method", "GET").upper()
    endpoint = task["endpoint"]
    url = f"{base_url}{endpoint}"

    # Run setup steps if any
    setup = task.get("setup", [])
    if setup:
        run_setup(base_url, setup)

    # Make the request
    try:
        request_config = task.get("request", {})
        form_data = request_config.get("form", {})

        if method == "POST":
            response = requests.post(url, data=form_data, timeout=30)
        elif method == "GET":
            response = requests.get(url, timeout=30)
        elif method == "PUT":
            response = requests.put(url, data=form_data, timeout=30)
        elif method == "DELETE":
            response = requests.delete(url, timeout=30)
        else:
            return {
                "id": task_id,
                "pass": False,
                "error": f"Unsupported method: {method}",
                "failures": [],
            }
    except requests.ConnectionError as e:
        return {
            "id": task_id,
            "pass": False,
            "error": f"Connection failed: {e}",
            "failures": [],
        }
    except requests.Timeout:
        return {
            "id": task_id,
            "pass": False,
            "error": "Request timed out (30s)",
            "failures": [],
        }
    except Exception as e:
        return {
            "id": task_id,
            "pass": False,
            "error": f"Request error: {e}",
            "failures": [],
        }

    # Check assertions
    failures = check_assertions(task, response)

    return {
        "id": task_id,
        "pass": len(failures) == 0,
        "status_code": response.status_code,
        "failures": failures,
        "response_snippet": response.text[:500] if failures else "",
    }


def grade_with_claude(results: list[dict], tasks: list[dict]) -> str | None:
    """Use Claude to analyze failures and suggest root causes."""
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — skipping AI grading")
        return None

    failing = [r for r in results if not r["pass"]]
    if not failing:
        return None

    task_map = {t["id"]: t for t in tasks}
    context_parts = []
    for result in failing:
        task = task_map.get(result["id"], {})
        context_parts.append(
            f"### {result['id']} (FAIL)\n"
            f"Feature: {task.get('feature', '?')}\n"
            f"Type: {task.get('type', '?')}\n"
            f"Description: {task.get('description', '').strip()}\n"
            f"Endpoint: {task.get('method', 'GET')} {task.get('endpoint', '?')}\n"
            f"Status code: {result.get('status_code', '?')}\n"
            f"Error: {result.get('error', '')}\n"
            f"Failures: {json.dumps(result.get('failures', []), indent=2)}\n"
            f"Response: {result.get('response_snippet', '')[:300]}\n"
        )

    prompt = (
        "You are a QA engineer analyzing eval failures for The Hootline, "
        "a daily podcast generator built with FastAPI.\n\n"
        "Here are the failing eval tasks:\n\n"
        + "\n".join(context_parts)
        + "\n\nFor each failure, provide:\n"
        "1. Root cause (1 sentence)\n"
        "2. Severity (critical / warning / info)\n"
        "3. Suggested fix (1-2 sentences)\n\n"
        "Be concise. Focus on actionable insights."
    )

    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as e:
        logger.error("Claude grading failed: %s", e)
        return None


def main():
    parser = argparse.ArgumentParser(description="Run Hootline eval tasks")
    parser.add_argument("--base-url", default="http://localhost:8000",
                        help="Base URL of the running app (default: http://localhost:8000)")
    parser.add_argument("--grade", action="store_true",
                        help="Use Claude to analyze failures")
    parser.add_argument("--filter", default="",
                        help="Regex pattern to filter task IDs")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")

    # Check if app is running
    try:
        requests.get(f"{base_url}/health", timeout=5)
    except requests.ConnectionError:
        logger.error("Cannot connect to %s — is the app running?", base_url)
        sys.exit(1)

    tasks = load_tasks(args.filter)
    if not tasks:
        logger.error("No eval tasks found (filter: %r)", args.filter)
        sys.exit(1)

    logger.info("Running %d eval tasks against %s", len(tasks), base_url)

    results = []
    for task in tasks:
        result = run_task(base_url, task)
        status = "PASS" if result["pass"] else "FAIL"
        logger.info("  %s %s", status, task["id"])
        if not result["pass"]:
            for f in result.get("failures", []):
                logger.info("    -> %s", f["message"])
            if result.get("error"):
                logger.info("    -> ERROR: %s", result["error"])
        results.append(result)
        # Brief pause between tasks
        time.sleep(0.1)

    # Summary
    passed = sum(1 for r in results if r["pass"])
    failed = len(results) - passed
    logger.info("")
    logger.info("=" * 60)
    logger.info("RESULTS: %d passed, %d failed, %d total", passed, failed, len(results))
    logger.info("=" * 60)

    # Group by feature
    task_map = {t["id"]: t for t in tasks}
    by_feature: dict[str, list[dict]] = {}
    for r in results:
        feature = task_map.get(r["id"], {}).get("feature", "unknown")
        by_feature.setdefault(feature, []).append(r)

    for feature, feature_results in sorted(by_feature.items()):
        fp = sum(1 for r in feature_results if r["pass"])
        ff = len(feature_results) - fp
        status = "ALL PASS" if ff == 0 else f"{ff} FAIL"
        logger.info("  %-30s %d/%d (%s)", feature, fp, len(feature_results), status)

    # AI grading
    grading_report = None
    if args.grade and failed > 0:
        logger.info("")
        logger.info("Running Claude analysis on %d failures...", failed)
        grading_report = grade_with_claude(results, tasks)
        if grading_report:
            logger.info("")
            logger.info("--- Claude Analysis ---")
            logger.info(grading_report)

    # Save report
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base_url,
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "results": results,
        "grading": grading_report,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    logger.info("")
    logger.info("Report saved to %s", REPORT_PATH)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
