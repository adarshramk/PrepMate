import os
from dotenv import load_dotenv

load_dotenv()

# Sentinel used by create_app() to detect an unconfigured secret key.
_INSECURE_KEY = "dev-key-change-in-production"


class Config:
    SECRET_KEY    = os.environ.get("FLASK_SECRET_KEY", _INSECURE_KEY)
    DATABASE_PATH = os.environ.get("DATABASE_PATH", "prepmate.db")
    DEBUG         = os.environ.get("FLASK_ENV", "production") == "development"

    # ── Session / cookie security ───────────────────────────────────────
    # SECURE: cookie only sent over HTTPS.  Set False locally (no TLS).
    SESSION_COOKIE_SECURE   = not DEBUG
    SESSION_COOKIE_HTTPONLY = True       # JS cannot read the session cookie
    SESSION_COOKIE_SAMESITE = "Lax"      # CSRF mitigation for cross-site GETs

    # ── AWS Bedrock LLM config ──────────────────────────────────────────
    # Provider: "bedrock" (default) or any future provider key.
    LLM_PROVIDER     = os.environ.get("LLM_PROVIDER", "bedrock")
    BEDROCK_REGION   = os.environ.get("BEDROCK_REGION", "us-east-1")
    BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-micro-v1:0")

    # AWS credentials — boto3 also reads these from ~/.aws/credentials or the
    # EC2 instance IAM role.  Leave blank on AWS and attach an IAM role instead.
    AWS_ACCESS_KEY_ID     = os.environ.get("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

    # Timeout for Bedrock calls (seconds)
    BEDROCK_TIMEOUT = int(os.environ.get("BEDROCK_TIMEOUT", "30"))


class TestingConfig(Config):
    TESTING                 = True
    DATABASE_PATH           = ":memory:"
    SECRET_KEY              = "test-secret-key"
    WTF_CSRF_ENABLED        = False
    SESSION_COOKIE_SECURE   = False   # no TLS in tests
    # Blank credentials force fallback in all tests — no real API calls ever made
    AWS_ACCESS_KEY_ID       = ""
    AWS_SECRET_ACCESS_KEY   = ""
    BEDROCK_REGION          = "us-east-1"
    BEDROCK_MODEL_ID        = "amazon.nova-micro-v1:0"
