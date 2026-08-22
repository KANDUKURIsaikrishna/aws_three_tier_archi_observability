#!/usr/bin/env python3
"""
derive_ses_smtp_password.py — Derive an SES SMTP password from IAM SigV4
credentials and store it in Secrets Manager.

Invoked by terraform/main.tf's null_resource.ses_smtp_password via
`interpreter = ["python3"]` (not a shell) so this runs identically on
Windows/macOS/Linux — no bash heredoc, no cmd.exe quoting.

Required env vars: REGION, ACCESS_KEY, SECRET_KEY, FROM_EMAIL, TO_EMAIL, SECRET_ID
"""
import hmac
import hashlib
import base64
import json
import os
import subprocess


def sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def derive_smtp_password(secret_key: str, region: str) -> str:
    date = "11111111"
    k = sign(("AWS4" + secret_key).encode("utf-8"), date)
    k = sign(k, region)
    k = sign(k, "ses")
    k = sign(k, "aws4_request")
    k = sign(k, "SendRawEmail")
    return base64.b64encode(bytes([0x04]) + k).decode("utf-8")


def main():
    region = os.environ["REGION"]
    secret = {
        "SMTP_HOST": f"email-smtp.{region}.amazonaws.com",
        "SMTP_PORT": "587",
        "SMTP_USERNAME": os.environ["ACCESS_KEY"],
        "SMTP_PASSWORD": derive_smtp_password(os.environ["SECRET_KEY"], region),
        "SMTP_FROM": os.environ["FROM_EMAIL"],
        "SMTP_TO": os.environ["TO_EMAIL"],
    }
    subprocess.run(
        [
            "aws", "secretsmanager", "put-secret-value",
            "--secret-id", os.environ["SECRET_ID"],
            "--region", region,
            "--secret-string", json.dumps(secret),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
