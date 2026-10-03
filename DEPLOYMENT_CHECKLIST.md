# PrepMate — Deployment Checklist

Target platform: **AWS Elastic Beanstalk (Docker single-container)** with an
optional IAM role for Amazon Bedrock access.

---

## 1. Pre-flight: local verification

```bash
# Run the full test suite — must be 0 failures before deploying
python -m pytest -q

# Smoke-test the production startup locally (no .env — uses env vars directly)
FLASK_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(64))") \
  DATABASE_PATH=/tmp/prepmate-smoke.db \
  gunicorn --bind 0.0.0.0:8000 --workers 1 wsgi:application
# Visit http://localhost:8000/health → {"status":"ok","db":"ok","app":"PrepMate"}

# Build and test the Docker image
docker build -t prepmate:latest .
docker run --rm -p 8000:8000 \
  -e FLASK_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(64))") \
  -e DATABASE_PATH=/data/prepmate.db \
  prepmate:latest
# Visit http://localhost:8000/health → {"status":"ok","db":"ok","app":"PrepMate"}
```

---

## 2. AWS environment variables (required)

Set these in **Elastic Beanstalk → Configuration → Software → Environment properties**
(or in your Docker `-e` flags / `.env` file for non-EB deployments).

| Variable | Required | Description |
|---|---|---|
| `FLASK_SECRET_KEY` | **Yes** | Strong random string (≥ 64 hex chars). Generate with `python -c "import secrets; print(secrets.token_hex(64))"`. App refuses to start without this. |
| `FLASK_ENV` | No | Set to `production` (default). Use `development` only for local debugging. |
| `DATABASE_PATH` | **Yes (Docker)** | Absolute path on a mounted EBS volume, e.g. `/data/prepmate.db`. Without a persistent volume the DB is lost on every deploy. |
| `BEDROCK_REGION` | No | AWS region for Bedrock calls (default: `us-east-1`). Must match the region where you enabled model access. |
| `BEDROCK_MODEL_ID` | No | Bedrock model to use (default: `amazon.nova-micro-v1:0`). |
| `BEDROCK_TIMEOUT` | No | Seconds to wait for a Bedrock response (default: `30`). |
| `AWS_ACCESS_KEY_ID` | No | Only needed when running **outside AWS** without an IAM role. Leave blank on EC2/EB — use an IAM role instead. |
| `AWS_SECRET_ACCESS_KEY` | No | Only needed when running **outside AWS** without an IAM role. Never commit this value. |

> **Never set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` on an EC2 or Elastic
> Beanstalk instance.** Attach an IAM role instead (see section 3).

---

## 3. AWS IAM permissions

### Option A — IAM role (recommended for EC2 / Elastic Beanstalk)

1. Open **IAM → Roles → Create role**.
2. Trusted entity type: **AWS service → EC2** (EB uses the EC2 instance profile).
3. Attach the following inline policy (replace `<region>` and `<account-id>`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockInvokeModel",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:<region>::foundation-model/amazon.nova-micro-v1:0"
      ]
    }
  ]
}
```

4. Name the role `prepmate-ec2-role` (or similar).
5. In **Elastic Beanstalk → Configuration → Security**, set the **IAM instance profile** to the role you just created.

> If you use a different model (e.g. `amazon.titan-text-lite-v1`), update the
> `Resource` ARN accordingly.

### Option B — IAM user with access keys (local development only)

Only do this for local development when you cannot use an IAM role.

1. Open **IAM → Users → Create user**.
2. Attach the same inline policy above.
3. Generate **Access keys → Application running outside AWS**.
4. Add the keys to your local `.env` file (which is git-ignored).
5. **Never commit `.env` to version control.**

### Minimum permissions summary

| AWS Action | Required? | Purpose |
|---|---|---|
| `bedrock:InvokeModel` | Yes | Call the Bedrock Converse API |
| `bedrock:InvokeModelWithResponseStream` | Recommended | Future streaming support |

No other IAM permissions are needed. The app uses SQLite (local file), so no
RDS, S3, or DynamoDB permissions are required.

---

## 4. Amazon Bedrock model access

Bedrock model access must be explicitly enabled before the API will accept calls.

1. Open the **AWS Console → Amazon Bedrock → Model access** (in the same region as `BEDROCK_REGION`).
2. Click **Manage model access**.
3. Enable **Amazon Nova Micro** (model ID: `amazon.nova-micro-v1:0`).
4. Wait for status to become **Access granted** (usually instant for Nova models).

> If model access is not enabled, Bedrock returns `AccessDeniedException`.
> The app catches this and falls back to the rule-based planner silently.

---

## 5. Persistent database (Docker / Elastic Beanstalk)

SQLite stores all data in a single file. Without a persistent volume, the file
lives inside the container and is destroyed on every deployment.

**Elastic Beanstalk Docker with EBS:**

1. Create an EBS volume in the same Availability Zone as your EB environment.
2. In your `Dockerrun.aws.json` (v2) or EB extension, mount the volume at `/data`.
3. Set `DATABASE_PATH=/data/prepmate.db` in environment properties.

**Minimal `Dockerrun.aws.json` snippet:**

```json
{
  "AWSEBDockerrunVersion": "1",
  "Volumes": [
    {
      "HostDirectory": "/data",
      "ContainerDirectory": "/data"
    }
  ]
}
```

---

## 6. Pre-launch checklist

- [ ] `python -m pytest -q` passes with 0 failures locally.
- [ ] `FLASK_SECRET_KEY` is set to a strong random value (≥ 64 chars).
- [ ] `FLASK_SECRET_KEY` is **not** committed to version control.
- [ ] `DATABASE_PATH` points to a directory on a persistent volume.
- [ ] Bedrock model access is enabled in the target AWS region.
- [ ] IAM role attached to the EC2/EB instance (or access keys in `.env` for local dev).
- [ ] `GET /health` returns `{"status":"ok","db":"ok"}` after deploy.
- [ ] Docker HEALTHCHECK passes: `docker inspect <container> | grep -i health`.
- [ ] `SESSION_COOKIE_SECURE` is `True` (automatic when `FLASK_ENV=production`).
- [ ] No `.env` file in the deployed Docker image (verify with `docker run --rm prepmate cat .env` — should fail with "No such file").

---

## 7. Rollback procedure

```bash
# List recent EB deployments
eb history

# Roll back to a specific version
eb deploy --version-label <previous-label>
```

For a Docker-only deploy without EB:

```bash
docker stop prepmate-prod
docker run -d --name prepmate-prod \
  -p 8000:8000 \
  -e FLASK_SECRET_KEY=<key> \
  -e DATABASE_PATH=/data/prepmate.db \
  -v /data:/data \
  prepmate:<previous-tag>
```

---

## 8. What the app does NOT need

- No RDS, DynamoDB, or ElastiCache — SQLite handles all data.
- No S3 — no file uploads.
- No SQS, SNS, or Lambda — no async jobs.
- No VPC configuration — a default EB environment works.
- No SSL certificate setup — EB load balancer handles HTTPS termination.

The only AWS service the app actively calls is **Amazon Bedrock**. All other
infrastructure is self-contained.
