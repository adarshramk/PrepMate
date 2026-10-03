"""
services/llm.py
AWS Bedrock Converse integration with explicit rule-based fallback.

Public API
----------
generate_roadmap(app, target_role, skills, hours_per_day, duration_weeks)
    -> dict with keys: title, source, failure_reason, milestones

The function NEVER raises: any Bedrock failure causes a transparent fallback.
API credentials are read from Flask config only — never logged or returned to
the client.

Roadmap dict schema
-------------------
{
    "title":          str,
    "source":         "ai" | "fallback",
    "failure_reason": None | "no_credentials" | "auth" | "config" | "transient" | "parse",
    "milestones": [
        {
            "week_number":  int,
            "title":        str,
            "priority":     "high" | "medium" | "low",
            "category":     str,          # must be in CATEGORIES
            "description":  str,
            "daily_tasks":  list[str],    # 3-5 concrete daily actions
            "position":     int,
        },
        ...
    ]
}

failure_reason values
---------------------
None            – Bedrock succeeded (source will be "ai")
"no_credentials" – boto3 found no credentials anywhere in the chain
"auth"          – Credentials exist but are invalid, expired, or missing IAM
                  permissions (AccessDeniedException / InvalidClientTokenId /
                  ExpiredTokenException / UnrecognizedClientException).
                  Requires AWS account action to fix.
"config"        – Request was valid but rejected due to configuration issues:
                  wrong model ID, model not enabled in Bedrock console, or
                  wrong region.  Requires AWS console or env-var fix.
"transient"     – Service unavailable or rate-limited. Retry later.
"parse"         – Bedrock responded successfully but returned non-JSON or an
                  empty milestone list.  Likely a prompt or model regression.
"""

from __future__ import annotations
import json
import logging
from typing import Literal

from models.tasks import CATEGORIES

logger = logging.getLogger(__name__)

# ── Failure reason type ────────────────────────────────────────────────────

FailureReason = Literal[
    "no_credentials", "auth", "config", "transient", "parse"
] | None


# ── Public entry point ─────────────────────────────────────────────────────

def generate_roadmap(
    app,
    target_role: str,
    skills: str,
    hours_per_day: float,
    duration_weeks: int,
) -> dict:
    """
    Try Bedrock first. Fall back silently on any failure.
    Returns a roadmap dict as described in the module docstring.
    The 'failure_reason' key is None on success, or a category string on
    failure — never contains credentials or sensitive data.
    """
    cfg = app.config

    # Read explicit credentials; may be empty — that is fine.
    # When both are absent, boto3 discovers credentials via its standard
    # chain (env vars, ~/.aws/credentials, IAM role, etc.).
    key_id  = cfg.get("AWS_ACCESS_KEY_ID", "").strip()
    key_sec = cfg.get("AWS_SECRET_ACCESS_KEY", "").strip()

    failure_reason: FailureReason = None

    try:
        result = _call_bedrock(
            region=cfg.get("BEDROCK_REGION", "us-east-1"),
            model_id=cfg.get("BEDROCK_MODEL_ID", "amazon.nova-micro-v1:0"),
            key_id=key_id,
            key_sec=key_sec,
            timeout=int(cfg.get("BEDROCK_TIMEOUT", 30)),
            target_role=target_role,
            skills=skills,
            hours_per_day=hours_per_day,
            duration_weeks=duration_weeks,
        )
        if result:
            # Success — failure_reason is None, source is 'ai'
            return result
        # _call_bedrock returned None: model responded but output was unusable
        failure_reason = "parse"
        logger.warning(
            "Bedrock response could not be parsed; using fallback. "
            "failure_reason=parse"
        )
    except Exception as exc:      # noqa: BLE001
        failure_reason = _categorise_bedrock_error(exc)
        # Log at WARNING: exception type + AWS error code (never credentials)
        _log_bedrock_failure(exc, failure_reason)

    fallback = _rule_based_fallback(target_role, skills, hours_per_day, duration_weeks)
    fallback["failure_reason"] = failure_reason
    return fallback


# ── Error categorisation ───────────────────────────────────────────────────

# AWS ClientError codes → failure_reason category
_AUTH_CODES = frozenset({
    "AccessDeniedException",
    "InvalidClientTokenId",
    "ExpiredTokenException",
    "UnrecognizedClientException",
    "AuthFailure",
})

_CONFIG_CODES = frozenset({
    "ValidationException",
    "ResourceNotFoundException",
    "ModelNotReadyException",
    "ModelErrorException",
})

_TRANSIENT_CODES = frozenset({
    "ServiceUnavailableException",
    "ThrottlingException",
    "ModelTimeoutException",
    "InternalServerException",
})


def _categorise_bedrock_error(exc: Exception) -> FailureReason:
    """
    Map an exception to one of the documented failure_reason categories.
    Never inspects credential values — only exception type and AWS error Code.
    """
    try:
        import botocore.exceptions as bce
    except ImportError:
        return "transient"

    if isinstance(exc, (bce.NoCredentialsError,)):
        return "no_credentials"

    if isinstance(exc, bce.PartialCredentialsError):
        # One of key_id/key_sec was set but not the other — misconfiguration
        return "auth"

    if isinstance(exc, bce.ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _AUTH_CODES:
            return "auth"
        if code in _CONFIG_CODES:
            return "config"
        if code in _TRANSIENT_CODES:
            return "transient"
        # Unknown ClientError code — treat as config since it's a request issue
        return "config"

    if isinstance(exc, (bce.ConnectTimeoutError, bce.ReadTimeoutError,
                        bce.EndpointConnectionError)):
        return "transient"

    # Catch-all for unexpected exception types
    return "transient"


def _log_bedrock_failure(exc: Exception, failure_reason: FailureReason) -> None:
    """
    Log a Bedrock failure at WARNING level.
    Logs: exception class, AWS error Code (safe — no credentials in error codes),
    and the failure_reason category.  Never logs credentials, ARNs, or messages.
    """
    exc_name = type(exc).__name__

    # Extract AWS error Code if this is a ClientError — it is safe to log
    aws_code: str = ""
    try:
        import botocore.exceptions as bce
        if isinstance(exc, bce.ClientError):
            aws_code = exc.response.get("Error", {}).get("Code", "")
    except Exception:  # noqa: BLE001
        pass

    if aws_code:
        logger.warning(
            "Bedrock call failed, using fallback. "
            "exc=%s aws_code=%s failure_reason=%s",
            exc_name, aws_code, failure_reason,
        )
    else:
        logger.warning(
            "Bedrock call failed, using fallback. "
            "exc=%s failure_reason=%s",
            exc_name, failure_reason,
        )


# ── Bedrock path ───────────────────────────────────────────────────────────

def _call_bedrock(
    *,
    region: str,
    model_id: str,
    key_id: str,
    key_sec: str,
    timeout: int,
    target_role: str,
    skills: str,
    hours_per_day: float,
    duration_weeks: int,
) -> dict | None:
    """
    Call AWS Bedrock Converse API.
    Returns a validated roadmap dict, or None if the response is unusable.
    Raises on network/auth errors (caller catches and falls back).
    """
    import boto3  # imported lazily — only when Bedrock is attempted
    from botocore.config import Config as BotocoreConfig

    # Only supply explicit credentials when both are provided.
    # When either is absent, omit them so boto3 walks its standard
    # credential chain (env vars, ~/.aws/credentials, IAM role, etc.).
    session_kwargs: dict = {"region_name": region}
    if key_id and key_sec:
        session_kwargs["aws_access_key_id"] = key_id
        session_kwargs["aws_secret_access_key"] = key_sec

    session = boto3.Session(**session_kwargs)
    client = session.client(
        "bedrock-runtime",
        config=BotocoreConfig(
            read_timeout=timeout,
            connect_timeout=10,
            retries={"max_attempts": 1},
        ),
    )

    prompt = _build_prompt(target_role, skills, hours_per_day, duration_weeks)

    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={
            "maxTokens": 2048,
            "temperature": 0.4,
            "topP": 0.9,
        },
    )

    text = response["output"]["message"]["content"][0]["text"]
    return _parse_and_validate(text, target_role, duration_weeks)


def _build_prompt(
    target_role: str,
    skills: str,
    hours_per_day: float,
    duration_weeks: int,
) -> str:
    categories_list = "\n".join(f"  - {c}" for c in CATEGORIES)
    return f"""You are a placement preparation coach. Generate a structured study roadmap as JSON.

Student profile:
- Target role: {target_role}
- Current skills: {skills}
- Study hours per day: {hours_per_day}
- Preparation duration: {duration_weeks} weeks

Return ONLY valid JSON with this exact structure (no markdown, no explanation):
{{
  "title": "<short descriptive title for this roadmap>",
  "milestones": [
    {{
      "week_number": <int 1-{duration_weeks}>,
      "title": "<milestone title>",
      "priority": "<high|medium|low>",
      "category": "<exactly one of the categories below>",
      "description": "<2-3 sentence explanation>",
      "daily_tasks": ["<task 1>", "<task 2>", "<task 3>"]
    }}
  ]
}}

Allowed categories (use the exact string):
{categories_list}

Rules:
- Generate {min(duration_weeks * 2, 20)} milestones spread across the {duration_weeks} weeks.
- Each milestone must have exactly 3-5 daily_tasks.
- Cover all 5 categories proportionally; weight toward the target role.
- Mark foundational topics as high priority; advanced as medium; optional as low.
- Do not include any text outside the JSON object."""


def _parse_and_validate(raw_text: str, target_role: str, duration_weeks: int) -> dict | None:
    """
    Parse the LLM response and validate required fields.
    Returns a clean roadmap dict, or None if validation fails.
    """
    # Strip accidental markdown fences
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("LLM response was not valid JSON")
        return None

    if not isinstance(data, dict):
        return None

    title = data.get("title", "").strip()
    if not title:
        title = f"{target_role} — {duration_weeks}-week roadmap"

    raw_milestones = data.get("milestones")
    if not isinstance(raw_milestones, list) or not raw_milestones:
        return None

    milestones = []
    for pos, m in enumerate(raw_milestones):
        if not isinstance(m, dict):
            continue

        category = m.get("category", "")
        if category not in CATEGORIES:
            # Try to salvage with a best-guess mapping
            category = _guess_category(category)

        daily_tasks = m.get("daily_tasks", [])
        if not isinstance(daily_tasks, list):
            daily_tasks = []

        priority = m.get("priority", "medium")
        if priority not in ("high", "medium", "low"):
            priority = "medium"

        week_number = m.get("week_number")
        try:
            week_number = int(week_number)
        except (TypeError, ValueError):
            week_number = (pos // 2) + 1

        milestones.append({
            "week_number":  week_number,
            "title":        str(m.get("title", f"Week {week_number} milestone")).strip(),
            "priority":     priority,
            "category":     category,
            "description":  str(m.get("description", "")).strip(),
            "daily_tasks":  [str(t) for t in daily_tasks[:5]],
            "position":     pos,
        })

    if not milestones:
        return None

    return {"title": title, "source": "ai", "failure_reason": None, "milestones": milestones}


def _guess_category(raw: str) -> str:
    """Map an unrecognised category string to the closest known category."""
    raw_lower = raw.lower()
    mapping = {
        "dsa":        "DSA & Programming",
        "algorithm":  "DSA & Programming",
        "coding":     "DSA & Programming",
        "aptitude":   "Aptitude",
        "math":       "Aptitude",
        "quant":      "Aptitude",
        "cloud":      "Cloud & Networking",
        "aws":        "Cloud & Networking",
        "network":    "Cloud & Networking",
        "project":    "Projects",
        "portfolio":  "Projects",
        "interview":  "Interview Preparation",
        "hr":         "Interview Preparation",
        "behavioral": "Interview Preparation",
    }
    for key, cat in mapping.items():
        if key in raw_lower:
            return cat
    return CATEGORIES[0]  # default to DSA & Programming


# ── Rule-based fallback ────────────────────────────────────────────────────

# Category priority profiles keyed by common role keywords
_ROLE_PROFILES: dict[str, list[str]] = {
    "backend":   ["DSA & Programming", "Cloud & Networking", "Projects",
                  "Aptitude", "Interview Preparation"],
    "frontend":  ["DSA & Programming", "Projects", "Aptitude",
                  "Cloud & Networking", "Interview Preparation"],
    "fullstack": ["DSA & Programming", "Projects", "Cloud & Networking",
                  "Aptitude", "Interview Preparation"],
    "data":      ["DSA & Programming", "Aptitude", "Projects",
                  "Cloud & Networking", "Interview Preparation"],
    "cloud":     ["Cloud & Networking", "DSA & Programming", "Projects",
                  "Aptitude", "Interview Preparation"],
    "devops":    ["Cloud & Networking", "Projects", "DSA & Programming",
                  "Aptitude", "Interview Preparation"],
    "sde":       ["DSA & Programming", "Projects", "Aptitude",
                  "Cloud & Networking", "Interview Preparation"],
}

_DEFAULT_PROFILE = ["DSA & Programming", "Aptitude", "Cloud & Networking",
                    "Projects", "Interview Preparation"]

_MILESTONE_TEMPLATES: dict[str, list[dict]] = {
    "DSA & Programming": [
        {
            "title":       "Arrays, Strings & Hashing",
            "description": "Master foundational data structures. Solve 10–15 problems on arrays and strings daily using the two-pointer and sliding-window patterns.",
            "daily_tasks": ["Solve 3 LeetCode easy problems on arrays", "Study sliding window pattern with examples", "Review time/space complexity for solutions"],
            "priority":    "high",
        },
        {
            "title":       "Linked Lists, Stacks & Queues",
            "description": "Build fluency with pointer-based structures. Implement common operations from scratch and practise reversal, merging, and cycle-detection patterns.",
            "daily_tasks": ["Implement singly and doubly linked list from scratch", "Solve 2 stack/queue problems", "Study Floyd's cycle detection algorithm"],
            "priority":    "high",
        },
        {
            "title":       "Trees & Graphs",
            "description": "Understand tree traversals (BFS/DFS) and graph algorithms (Dijkstra, union-find). These appear frequently in technical interviews.",
            "daily_tasks": ["Practice BFS and DFS on binary trees", "Solve 2 graph problems (shortest path)", "Review topological sort"],
            "priority":    "high",
        },
        {
            "title":       "Dynamic Programming Foundations",
            "description": "Learn the top-down (memoisation) and bottom-up (tabulation) approaches. Start with classic problems: knapsack, LCS, coin change.",
            "daily_tasks": ["Study top-down DP with memoisation", "Solve knapsack and coin-change problems", "Trace through a DP table by hand"],
            "priority":    "medium",
        },
    ],
    "Aptitude": [
        {
            "title":       "Quantitative Math Foundations",
            "description": "Cover arithmetic, percentages, ratios, and time-speed-distance — the most tested quantitative areas in placement aptitude tests.",
            "daily_tasks": ["Solve 20 percentage/ratio problems", "Study short-cut formulas for time-speed-distance", "Take a 15-minute timed quiz"],
            "priority":    "high",
        },
        {
            "title":       "Logical Reasoning & Puzzles",
            "description": "Practise syllogisms, seating arrangements, blood relations, and logical sequences to sharpen analytical thinking.",
            "daily_tasks": ["Solve 10 seating arrangement problems", "Study syllogism rules and practice 5 questions", "Complete one timed logical reasoning section"],
            "priority":    "medium",
        },
    ],
    "Cloud & Networking": [
        {
            "title":       "Networking Fundamentals",
            "description": "Study the OSI model, TCP/IP, DNS, HTTP/HTTPS, and subnetting — core knowledge for cloud and system design interviews.",
            "daily_tasks": ["Read about OSI model layers with examples", "Understand TCP vs UDP differences", "Practice subnetting calculations"],
            "priority":    "medium",
        },
        {
            "title":       "AWS Core Services",
            "description": "Get hands-on with EC2, S3, IAM, VPC, Lambda, and RDS. Use the AWS free tier to create and destroy resources.",
            "daily_tasks": ["Launch an EC2 instance and connect via SSH", "Create an S3 bucket and upload a file", "Set up an IAM user with least-privilege policy"],
            "priority":    "medium",
        },
    ],
    "Projects": [
        {
            "title":       "Portfolio Project Planning",
            "description": "Define a full-stack or domain-specific project that showcases skills relevant to your target role. Write a clear scope and tech-stack decision.",
            "daily_tasks": ["Write a one-page project spec", "Set up repository with README and .gitignore", "Create project board with milestone issues"],
            "priority":    "medium",
        },
        {
            "title":       "Project Implementation Sprint",
            "description": "Build the core feature set of your portfolio project. Commit daily, write meaningful commit messages, and document APIs.",
            "daily_tasks": ["Implement one core feature per day", "Write unit tests for completed features", "Update README with setup instructions"],
            "priority":    "medium",
        },
    ],
    "Interview Preparation": [
        {
            "title":       "Resume & LinkedIn Review",
            "description": "Refine your resume with quantified impact statements and align your LinkedIn profile with your target role.",
            "daily_tasks": ["Update resume with measurable achievements", "Tailor LinkedIn headline and summary", "Get one peer review of your resume"],
            "priority":    "high",
        },
        {
            "title":       "Behavioural Interview Mastery",
            "description": "Prepare STAR-format answers for the 20 most common HR questions. Practise out loud and time yourself.",
            "daily_tasks": ["Write STAR answers for 3 behavioural questions", "Record and review a 2-minute self-introduction", "Research your top 3 target companies"],
            "priority":    "high",
        },
        {
            "title":       "Mock Technical Interviews",
            "description": "Simulate real interview conditions by solving problems on a whiteboard or plain editor while explaining your thought process.",
            "daily_tasks": ["Complete one 45-minute mock interview session", "Solve 2 medium problems without IDE autocomplete", "Review and note down gaps from the session"],
            "priority":    "medium",
        },
    ],
}


def _rule_based_fallback(
    target_role: str,
    skills: str,
    hours_per_day: float,
    duration_weeks: int,
) -> dict:
    """
    Generate a structured roadmap without any API call.
    Always labelled source='fallback'.
    failure_reason is set by the caller (generate_roadmap).
    """
    # Pick a category ordering based on role keywords
    role_lower = target_role.lower()
    ordered_cats = _DEFAULT_PROFILE
    for keyword, profile in _ROLE_PROFILES.items():
        if keyword in role_lower:
            ordered_cats = profile
            break

    # Distribute weeks across categories proportionally
    cat_week_shares = _distribute_weeks(ordered_cats, duration_weeks)

    milestones: list[dict] = []
    pos = 0
    current_week = 1

    for cat, week_count in cat_week_shares:
        templates = _MILESTONE_TEMPLATES.get(cat, [])
        num_milestones = min(max(1, week_count // 2), len(templates))
        weeks_per_milestone = max(1, week_count // num_milestones)

        for i in range(num_milestones):
            tmpl = templates[i % len(templates)]
            milestones.append({
                "week_number":  current_week,
                "title":        tmpl["title"],
                "priority":     tmpl["priority"],
                "category":     cat,
                "description":  tmpl["description"],
                "daily_tasks":  tmpl["daily_tasks"],
                "position":     pos,
            })
            current_week += weeks_per_milestone
            pos += 1

    title = f"{target_role} — {duration_weeks}-week Preparation Plan"
    return {
        "title":          title,
        "source":         "fallback",
        "failure_reason": None,   # caller sets this
        "milestones":     milestones,
    }


def _distribute_weeks(categories: list[str], total_weeks: int) -> list[tuple[str, int]]:
    """
    Return [(category, weeks)] with weeks summing to total_weeks.
    The first two categories get slightly more weight.
    """
    n = len(categories)
    if n == 0:
        return []

    # Weight vector: first two get 1.5x, rest get 1x
    weights = [1.5 if i < 2 else 1.0 for i in range(n)]
    total_weight = sum(weights)
    raw = [total_weeks * w / total_weight for w in weights]

    # Floor, then distribute remainder
    floored = [max(1, int(r)) for r in raw]
    shortfall = total_weeks - sum(floored)
    remainders = sorted(range(n), key=lambda i: -(raw[i] - floored[i]))
    for i in range(shortfall):
        floored[remainders[i % n]] += 1

    return list(zip(categories, floored))
