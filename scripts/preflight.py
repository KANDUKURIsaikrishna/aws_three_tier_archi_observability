#!/usr/bin/env python3
"""
preflight.py -- check the local toolchain BEFORE `terraform apply`.

Every `local-exec` provisioner in this project shells out to `python3`, and
the scripts they run call `aws` and `kubectl`. If any of those is missing or
too old, the failure surfaces deep inside an apply (typically
null_resource.wait_for_alb_hostname) with a confusing message. This script
catches it up front.

Checks, in order:
  1. terraform  -- present, on PATH, >= 1.10.0 (native S3 state locking)
  2. kubectl    -- present, on PATH
  3. aws        -- present, on PATH, v2 recommended
  4. python3    -- present, >= 3.8, and NOT the Windows Store stub
  5. AWS creds  -- `aws sts get-caller-identity` succeeds
  6. config.env -- exists and has the 5 required keys

Report only. It never downloads, installs, or edits PATH / rc files -- on a
FAIL it prints the install command for your OS and exits non-zero so it can
gate `make plan` / `make apply`.

Usage:
  python3 scripts/preflight.py
"""
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ENV = REPO_ROOT / "config.env"
REQUIRED_KEYS = ["AWS_ACCOUNT_ID", "AWS_REGION", "DOMAIN", "GITHUB_REPO", "ALERT_EMAIL"]
TERRAFORM_MIN = (1, 10, 0)
PYTHON_MIN = (3, 8)

OS = platform.system()  # 'Darwin' | 'Linux' | 'Windows'

INSTALL_HINTS = {
    "terraform": {
        "Darwin": "brew tap hashicorp/tap && brew install hashicorp/tap/terraform",
        "Linux": "see https://developer.hashicorp.com/terraform/install (apt: HashiCorp apt repo)",
        "Windows": "choco install terraform   (or: winget install HashiCorp.Terraform)",
    },
    "kubectl": {
        "Darwin": "brew install kubectl",
        "Linux": "sudo apt-get install -y kubectl   (or curl from dl.k8s.io/release)",
        "Windows": "choco install kubernetes-cli   (or: winget install Kubernetes.kubectl)",
    },
    "aws": {
        "Darwin": "brew install awscli",
        "Linux": 'curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o a.zip && unzip a.zip && sudo ./aws/install',
        "Windows": "msiexec /i https://awscli.amazonaws.com/AWSCLIV2.msi   (or: winget install Amazon.AWSCLI)",
    },
    "python3": {
        "Darwin": "brew install python@3.12",
        "Linux": "sudo apt-get install -y python3",
        "Windows": "install from https://www.python.org/downloads/ (NOT the Microsoft Store build -- it ships a stub that breaks local-exec)",
    },
}

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if OS == "Windows":
    GREEN = RED = YELLOW = DIM = RESET = ""

_rows = []
_failed = 0


def record(name: str, status: str, detail: str, hint: str = "") -> None:
    global _failed
    if status == "FAIL":
        _failed += 1
    _rows.append((name, status, detail, hint))


def which(cmd: str) -> str:
    return shutil.which(cmd) or ""


def run(argv, timeout=20):
    return subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=timeout,
    )


def parse_version(text: str):
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return tuple(int(x) if x else 0 for x in m.groups())


def hint_for(tool: str) -> str:
    return INSTALL_HINTS.get(tool, {}).get(OS, INSTALL_HINTS.get(tool, {}).get("Linux", ""))


def check_terraform() -> None:
    path = which("terraform")
    if not path:
        record("terraform", "FAIL", "not found on PATH", hint_for("terraform"))
        return
    try:
        out = run(["terraform", "version"]).stdout
    except Exception as e:
        record("terraform", "FAIL", f"found but won't run: {e}", hint_for("terraform"))
        return
    ver = parse_version(out)
    if ver and ver >= TERRAFORM_MIN:
        record("terraform", "PASS", f"{'.'.join(map(str, ver))}  ({path})")
    else:
        vtxt = ".".join(map(str, ver)) if ver else "unknown"
        record("terraform", "FAIL",
               f"{vtxt} < {'.'.join(map(str, TERRAFORM_MIN))} required (native S3 state locking)",
               hint_for("terraform"))


def check_kubectl() -> None:
    path = which("kubectl")
    if not path:
        record("kubectl", "FAIL", "not found on PATH", hint_for("kubectl"))
        return
    try:
        out = run(["kubectl", "version", "--client"]).stdout
    except Exception as e:
        record("kubectl", "FAIL", f"found but won't run: {e}", hint_for("kubectl"))
        return
    ver = parse_version(out)
    vtxt = ".".join(map(str, ver)) if ver else "client OK"
    record("kubectl", "PASS", f"{vtxt}  ({path})")


def check_aws() -> None:
    path = which("aws")
    if not path:
        record("aws", "FAIL", "not found on PATH", hint_for("aws"))
        return
    try:
        out = run(["aws", "--version"]).stdout.strip()
    except Exception as e:
        record("aws", "FAIL", f"found but won't run: {e}", hint_for("aws"))
        return
    if "aws-cli/1." in out:
        record("aws", "WARN", f"v1 ({out.split()[0]}) -- v2 recommended", hint_for("aws"))
    else:
        record("aws", "PASS", f"{out.split()[0].replace('aws-cli/', 'v')}  ({path})")


def check_python3() -> None:
    path = which("python3") or sys.executable
    # Windows Store stub: resolves into WindowsApps and either does nothing or
    # pops the Store. It breaks `interpreter = ["python3"]` in local-exec.
    if OS == "Windows" and "WindowsApps" in path:
        record("python3", "FAIL",
               "resolves to the Microsoft Store stub (WindowsApps) -- breaks local-exec",
               hint_for("python3"))
        return
    ver = sys.version_info[:2]
    if ver >= PYTHON_MIN:
        record("python3", "PASS",
               f"{ver[0]}.{ver[1]}  ({path})")
    else:
        record("python3", "FAIL",
               f"{ver[0]}.{ver[1]} < {PYTHON_MIN[0]}.{PYTHON_MIN[1]} required",
               hint_for("python3"))


def check_aws_creds() -> None:
    if not which("aws"):
        record("AWS credentials", "FAIL", "skipped -- aws CLI missing", "")
        return
    try:
        proc = run(["aws", "sts", "get-caller-identity", "--output", "text"], timeout=25)
    except Exception as e:
        record("AWS credentials", "FAIL", f"call failed: {e}",
               "aws configure   (or set AWS_PROFILE / AWS_ACCESS_KEY_ID)")
        return
    if proc.returncode != 0:
        first = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "no output"
        record("AWS credentials", "FAIL", first,
               "aws configure   (or set AWS_PROFILE / AWS_ACCESS_KEY_ID)")
        return
    acct = proc.stdout.split()[0] if proc.stdout.split() else "?"
    record("AWS credentials", "PASS", f"account {acct}")


def check_config_env() -> None:
    if not CONFIG_ENV.is_file():
        record("config.env", "FAIL", "not found",
               "cp config.env.example config.env  &&  edit it  (DEPLOYMENT.md Step 1)")
        return
    present = {}
    for raw in CONFIG_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        present[k.strip()] = v.strip().strip('"').strip("'")
    missing = [k for k in REQUIRED_KEYS if not present.get(k)]
    if missing:
        record("config.env", "FAIL", f"missing/empty: {', '.join(missing)}",
               "fill these in, then re-run  python3 scripts/configure.py")
    else:
        record("config.env", "PASS", f"all {len(REQUIRED_KEYS)} required keys set")


def main() -> int:
    print(f"\npreflight -- {platform.platform()}\n")

    check_terraform()
    check_kubectl()
    check_aws()
    check_python3()
    check_aws_creds()
    check_config_env()

    name_w = max(len(r[0]) for r in _rows)
    for name, status, detail, hint in _rows:
        colour = {"PASS": GREEN, "WARN": YELLOW, "FAIL": RED}[status]
        print(f"  {colour}{status:<4}{RESET}  {name:<{name_w}}  {DIM}{detail}{RESET}")
        if hint and status != "PASS":
            print(f"        {DIM}-> {hint}{RESET}")

    print()
    if _failed:
        print(f"{RED}{_failed} check(s) failed -- fix the above before `make apply`.{RESET}\n")
        return 1
    warns = sum(1 for r in _rows if r[1] == "WARN")
    tail = f" ({warns} warning(s))" if warns else ""
    print(f"{GREEN}all checks passed{tail}.{RESET}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
