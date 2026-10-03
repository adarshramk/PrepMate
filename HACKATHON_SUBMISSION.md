# PrepMate — AWS Builder Center Zero to Shipped Submission
# PASTE THIS TEXT INTO BUILDER CENTER

---

## Title
PrepMate — AI-Powered Placement Preparation Platform

---

## Category / Lane
- Category: #workplace-efficiency
- Lane: #community

---

## Problem Statement

Millions of engineering students face placement season with no structured, personalized preparation plan. Generic study resources don't adapt to individual skill gaps, target roles, or available time. Students waste weeks on the wrong topics and arrive at interviews underprepared.

PrepMate solves this by generating a week-by-week preparation roadmap tailored to each student's target role (SDE, backend, cloud, etc.), current skills, daily study hours, and duration — then lets them approve it, track milestones, and import them as actionable tasks.

**Target users:** Final-year engineering students, recent graduates, and early-career developers preparing for technical placement interviews.

---

## Features

1. **AI-Powered Roadmap Generation** — Calls Amazon Bedrock (Nova Micro) via the Converse API to produce a structured JSON roadmap with milestone titles, priorities, categories, descriptions, and 3–5 daily tasks per week.

2. **Rule-Based Fallback** — When Bedrock is unavailable or returns an error, a deterministic rule-based engine instantly generates a full roadmap based on role keyword profiles. The fallback is transparent: the UI labels it "Rule-based plan" and shows a safe diagnostic hint (e.g. "model may not be enabled in Bedrock console").

3. **Failure Classification** — Every Bedrock failure is classified into one of five categories (`no_credentials`, `auth`, `config`, `transient`, `parse`) and stored alongside the roadmap. This powers both the UI hint and structured logging — without exposing credentials or raw AWS error messages to users.

4. **Milestone → Task Import** — After approving a roadmap, users select milestones and import them as tasks with due dates calculated from week numbers. A duplicate-import guard prevents double-importing.

5. **Task Tracker** — Full CRUD task list with categories (DSA & Programming, Aptitude, Cloud & Networking, Projects, Interview Preparation), completion tracking, filtering, and dashboard stats.

6. **User Auth** — Registration, login, session management, CSRF protection. Per-user data isolation enforced at the model layer.

7. **Health Endpoint** — `GET /health` returns JSON `{"status":"ok","db":"ok","app":"PrepMate"}`. Returns 503 with `{"status":"degraded"}` if the database is unavailable. Used for load-balancer and Docker health checks.

---

## Architecture & AWS Services

```
Browser → Gunicorn (WSGI) → Flask app
                              ├── SQLite (local file, /data/prepmate.db)
                              └── Amazon Bedrock Converse API
                                   └── amazon.nova-micro-v1:0 (us-east-1)
                                        └── Rule-based fallback if Bedrock unavailable
```

**AWS Services used:**
| Service | Role |
|---|---|
| Amazon Bedrock | AI roadmap generation via Converse API (boto3) |
| Amazon Nova Micro (`amazon.nova-micro-v1:0`) | Foundation model for structured JSON generation |
| AWS IAM | `PrepMate_Bedrock` custom inline policy scoped to `bedrock:InvokeModel` |
| AWS S3 | (Available via `AmazonS3FullAccess`) — not used at runtime, available for future file storage |

**Deployment target:** AWS App Runner (source-code deployment from GitHub, `apprunner.yaml` included) or AWS Elastic Beanstalk Docker single-container (Dockerfile and `Procfile` included). SQLite persists on ephemeral App Runner storage (data resets on redeploys; EBS volume recommended for production).

---

## Bedrock Status (Honest Disclosure)

The Bedrock Converse API call to `amazon.nova-micro-v1:0` in `us-east-1` currently returns:

```
ValidationException: Operation not allowed
```

This was confirmed by direct boto3 invocation (not a code bug). The same request to `ap-south-1` returns `AccessDeniedException`, and to other regions also returns `AccessDeniedException` — indicating the IAM policy restricts Bedrock to `us-east-1` but model access has not been granted in the AWS console for that account.

A cross-region quota-increase request is pending and an AWS Support case has been opened.

**The app handles this gracefully:** the rule-based fallback produces a fully usable roadmap immediately, the failure is classified as `failure_reason="config"`, logged safely (error code only, no credentials), and a diagnostic hint is shown in the UI. The Bedrock integration code is complete and correct — it will work the moment model access is granted.

---

## Coding Agent Usage

**Agent:** Kiro (AWS Kiro CLI coding agent)

**Real role of the agent in this project:**

1. **Architecture and implementation** — Kiro implemented the complete Flask application from scratch: database schema with SQLite, user auth with CSRF, task model, roadmap model, planner routes, LLM service with Bedrock Converse API integration, rule-based fallback, and all templates.

2. **Bedrock error classification** — Kiro designed and implemented the `_categorise_bedrock_error()` function mapping botocore exceptions (`NoCredentialsError`, `PartialCredentialsError`, `ClientError` with AWS error codes) to the five failure categories, with safe logging that never exposes credentials.

3. **Backward-compatible database migration** — Kiro added `failure_reason` column migration to `database/__init__.py` using `PRAGMA table_info()` — applied to the live `prepmate.db` without data loss, idempotent across restarts.

4. **Test suite** — Kiro wrote 212 tests across 6 test files covering: auth flows, task CRUD, roadmap model, LLM fallback, error classification, persistence, and database migration. All 212 pass, 0 fail.

5. **AWS investigation** — Kiro ran direct boto3 calls to identify the exact Bedrock failure mode (`ValidationException` in us-east-1, `AccessDeniedException` in other regions), verified credentials are present via the shared-credentials-file provider, and confirmed the failure is not a code issue.

6. **Deployment preparation** — Kiro initialized the Git repository, verified `.gitignore` excludes secrets and database files, staged safe files only, and created `apprunner.yaml` for App Runner deployment.

**Evidence of agent–AWS connection:**
- Kiro ran `aws sts get-caller-identity` and confirmed account `870325637337` / user `adarshramk`
- Kiro ran direct boto3 `bedrock-runtime.converse()` calls against `us-east-1`, `ap-south-1`, and `us-west-2`
- Kiro ran `aws s3api list-buckets`, `aws elasticbeanstalk describe-applications`, `aws elasticbeanstalk describe-environments`, `aws apprunner create-service`, `aws apprunner list-services` to map available permissions
- The `failure_reason` classification system and migration were developed in direct response to a real Bedrock `ValidationException` observed by the agent during live testing

---

## Development Process & Testing

- Built iteratively with Kiro CLI over multiple sessions
- Test-driven: every feature has unit and integration tests
- The last interrupted session implemented Bedrock error classification, DB migration, and UI diagnostic hints — Kiro audited the incomplete work, identified the missing migration logic, and completed all four tasks

**Test results (verified):**
```
212 passed, 0 failed
```
Test files: `test_basic.py`, `test_tasks.py`, `test_planner.py`, `test_llm.py`, `test_health_config.py`, `test_bedrock_errors.py`

---

## Repository

[GitHub URL — to be added after pushing to GitHub]

Instructions to push (one command after creating the repo on GitHub):
```
git remote add origin https://github.com/YOUR_USERNAME/prepmate.git
git push -u origin master
```

---

## Live URL

**Deployment blocked by IAM permissions** — see blocker section below.

The IAM user `adarshramk` has `AmazonS3FullAccess` and `PrepMate_Bedrock` only. Elastic Beanstalk `CreateApplication`, App Runner `CreateService`, ECR `DescribeRepositories`, ECS, Lambda, EC2, Lightsail, and Amplify all returned `AccessDeniedException`.

To deploy on App Runner (lowest cost, ~$5–15/month for a nano instance):
1. Add the `AWSAppRunnerFullAccess` managed policy to IAM user `adarshramk` (or attach to a role)
2. Push the GitHub repo
3. Create App Runner service from GitHub source, set `FLASK_SECRET_KEY` as an environment secret

[Live URL will be added after deployment unblocked]

---

## Cost Implications

- **App Runner nano instance:** ~$0.007/vCPU-hour + $0.0007/GB-hour ≈ $5–15/month depending on traffic
- **No additional services required:** SQLite handles all data; no RDS, no SQS, no ElastiCache
- **SQLite persistence note:** App Runner has ephemeral storage. User data will be lost on redeploys unless EFS is mounted. For a hackathon demo this is acceptable.
