#!/usr/bin/env python3
"""
simulate_load.py -- on-demand load simulation to trip the HighPodCPUUsage /
HighRequestRate alerts for a demo.

Two independent simulations, run either or both:
  - CPU:     a throwaway `polinux/stress` pod in the `default` namespace
             (no NetworkPolicy concerns -- cAdvisor picks up any pod on the
             node regardless of namespace or annotations)
  - traffic: a sustained HTTPS request loop against the real ingress ALB,
             sent with the api-gateway Host header (the ALB routes on Host)

The demo alerts (HighPodCPUUsage / HighRequestRate) use 1m rate windows and
a 20s `for:` hold, and Prometheus scrapes every 10s, so with sustained load
they fire ~90-120s after load start and the email lands ~10s later. A very
short --duration still just prints "nothing firing yet" and exits -- give it
--duration 240 for a comfortable margin.

Always cleans up after itself (stress pod deleted, request loop stopped) on
normal exit, Ctrl-C, or an error partway through.

Pure Python 3 stdlib + the `kubectl`, `terraform` and `aws` CLIs on PATH --
no curl, no jq, no bash. Identical on Windows, macOS and Linux.

Usage:
  python3 scripts/simulate_load.py                    # both, 3 minutes
  python3 scripts/simulate_load.py --cpu-only
  python3 scripts/simulate_load.py --traffic-only
  python3 scripts/simulate_load.py --duration 300     # seconds
  python3 scripts/simulate_load.py --rps 30           # requests/sec target
"""
import argparse
import atexit
import base64
import json
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ENV = REPO_ROOT / "config.env"
TF_DIR = REPO_ROOT / "terraform"
STRESS_POD = "cpu-stress-demo"

_stop = threading.Event()
_load_threads: list[threading.Thread] = []
_stress_started = False


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        die(f"`{name}` not found on PATH -- install it and re-run.")
    return found


def capture(argv, **kw) -> str:
    return subprocess.run(
        argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw
    ).stdout.strip()


def try_capture(argv, **kw) -> str:
    try:
        return capture(argv, **kw)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def read_config_domain() -> str:
    if not CONFIG_ENV.is_file():
        return ""
    domain = ""
    for raw in CONFIG_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("DOMAIN="):
            domain = line.partition("=")[2].strip().strip('"').strip("'")
    return domain


def tf_output(terraform: str, name: str) -> str:
    return try_capture([terraform, f"-chdir={TF_DIR}", "output", "-raw", name])


def cleanup() -> None:
    print("\n── Cleaning up ──────────────────────────────────────────────────────")
    _stop.set()
    for t in _load_threads:
        t.join(timeout=5)
    if _load_threads:
        print("Stopped load generator.")
    if _stress_started:
        kubectl = shutil.which("kubectl")
        if kubectl:
            subprocess.run(
                [kubectl, "delete", "pod", STRESS_POD, "-n", "default", "--wait=false"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"Deleted {STRESS_POD}.")


def start_cpu_stress(kubectl: str, duration: int) -> None:
    global _stress_started
    print(f"── Starting CPU stress pod (2 cores, {duration}s) ──────────────────")
    subprocess.run(
        [kubectl, "delete", "pod", STRESS_POD, "-n", "default",
         "--ignore-not-found=true", "--wait=true"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [kubectl, "run", STRESS_POD, "--image=polinux/stress",
         "--restart=Never", "--namespace=default",
         "--", "stress", "--cpu", "2", "--timeout", f"{duration}s"],
        check=True, stdout=subprocess.DEVNULL,
    )
    _stress_started = True
    print(f"Started. Watch: kubectl top pod {STRESS_POD} -n default")


def start_traffic(kubectl: str, host: str, elb: str, rps: int, duration: int) -> None:
    print(f"── Starting traffic load (~{rps} req/s against https://{host}/books, "
          f"{duration}s) ──")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # equivalent of curl -k -- ALB SNI != Host
    url = f"https://{elb}/books"
    interval = 1.0 / rps

    def one_request() -> None:
        req = urllib.request.Request(url, headers={"Host": host})
        try:
            urllib.request.urlopen(req, timeout=3, context=ctx).read()
        except Exception:
            pass  # a dropped/slow request is fine -- we only care about rate

    def loop() -> None:
        end = time.monotonic() + duration
        while not _stop.is_set() and time.monotonic() < end:
            threading.Thread(target=one_request, daemon=True).start()
            _stop.wait(interval)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    _load_threads.append(t)
    print("Started.")


def prometheus_auth_header() -> dict:
    """Prometheus is behind basic auth (web.yml). Best-effort: pull the
    credentials from Secrets Manager the same way monitoring_credentials.py
    does. If that fails (no aws CLI, no perms), fall back to no auth -- the
    watch loop then just shows 'nothing firing yet'."""
    raw = try_capture([
        "aws", "secretsmanager", "get-secret-value",
        "--secret-id", "/bookstore/monitoring-basic-auth",
        "--query", "SecretString", "--output", "text",
    ])
    if not raw:
        return {}
    user = passwd = ""
    try:
        obj = json.loads(raw)
        user = obj.get("username", "") or obj.get("user", "")
        passwd = obj.get("password", "")
    except json.JSONDecodeError:
        if ":" in raw:
            user, _, passwd = raw.partition(":")
    if not user:
        user = "admin"
    if not passwd:
        return {}
    token = base64.b64encode(f"{user}:{passwd}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def watch_alerts(prom_ip: str, duration: int) -> None:
    print("\n── Watching alerts (Ctrl-C to stop early) ───────────────────────────")
    headers = prometheus_auth_header()
    url = f"http://{prom_ip}:9090/api/v1/alerts"
    end = time.monotonic() + duration
    while not _stop.is_set() and time.monotonic() < end:
        firing = []
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            for a in data.get("data", {}).get("alerts", []):
                if a.get("state") == "firing":
                    name = a.get("labels", {}).get("alertname", "?")
                    summary = a.get("annotations", {}).get("summary", "")
                    firing.append(f"{name}: {summary}")
        except Exception:
            pass
        stamp = time.strftime("%H:%M:%S")
        if firing:
            print(f"[{stamp}] FIRING:")
            for f in firing:
                print(f"    {f}")
        else:
            print(f"[{stamp}] nothing firing yet...")
        _stop.wait(15)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Trip the demo CPU / request-rate alerts on demand.")
    ap.add_argument("--cpu-only", action="store_true", help="CPU stress only")
    ap.add_argument("--traffic-only", action="store_true", help="traffic load only")
    ap.add_argument("--duration", type=int, default=180, help="seconds (default 180)")
    ap.add_argument("--rps", type=int, default=20, help="requests/sec target (default 20)")
    args = ap.parse_args()

    do_cpu = not args.traffic_only
    do_traffic = not args.cpu_only

    kubectl = tool("kubectl")
    terraform = tool("terraform")

    raw_domain = read_config_domain()
    if not raw_domain:
        die("Couldn't read DOMAIN from config.env -- run from repo root with "
            "config.env set up (docs/DEPLOYMENT.md Step 1).")
    host = f"api.bookstore.{raw_domain}"

    prom_url = tf_output(terraform, "prometheus_url")
    if not prom_url:
        die("Couldn't read prometheus_url from terraform output -- run from repo "
            "root with the stack applied.")
    prom_ip = prom_url.replace("http://", "").split(":")[0]

    atexit.register(cleanup)

    if do_cpu:
        start_cpu_stress(kubectl, args.duration)

    if do_traffic:
        elb = try_capture([
            kubectl, "get", "ingress", "bookstore-ingress", "-n", "bookstore",
            "-o", "jsonpath={.status.loadBalancer.ingress[0].hostname}",
        ])
        if not elb:
            die("Couldn't read the ALB hostname from the bookstore-ingress Ingress "
                "-- is the stack up and kubectl pointed at the right cluster?")
        start_traffic(kubectl, host, elb, args.rps, args.duration)

    try:
        watch_alerts(prom_ip, args.duration)
    except KeyboardInterrupt:
        pass

    grafana_url = tf_output(terraform, "grafana_url")
    print(f"\nDone. Prometheus: http://{prom_ip}:9090/alerts")
    if grafana_url:
        gip = grafana_url.replace("http://", "").split(":")[0]
        print(f"Grafana:    http://{gip}:3000")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
