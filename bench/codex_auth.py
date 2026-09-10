"""Validate/stage a subscription login or upload it to AWS SSM without logging tokens."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Optional


def ssm_parameter(worker: Optional[int] = None) -> str:
    """Return the isolated SSM parameter for an optional Codex worker slot."""
    if worker is None:
        return "/mineclaude-bench/codex-auth"
    if worker < 1:
        raise ValueError("worker must be a positive integer")
    return f"/mineclaude-bench/codex-auth-worker-{worker}"


def subscription_auth(path: Path) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("expected an auth object")
    tokens = data.get("tokens") or {}
    if not isinstance(tokens, dict):
        raise ValueError("expected subscription tokens")
    if (data.get("auth_mode") != "chatgpt" or data.get("OPENAI_API_KEY")
            or not all(isinstance(tokens.get(k), str) and tokens[k]
                       for k in ("access_token", "refresh_token", "id_token"))):
        raise ValueError("expected a ChatGPT subscription login, not API-key auth")
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("--stage", type=Path)
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--worker", type=int,
                    help="isolated Codex worker slot (defaults to the legacy shared credential)")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = ap.parse_args()
    try:
        data = subscription_auth(args.source)
        parameter = ssm_parameter(args.worker)
    except (OSError, ValueError, TypeError):
        ap.exit(2, "Codex auth is missing or invalid; use a file-backed ChatGPT login.\n")
    payload = json.dumps(data, separators=(",", ":"))
    if args.stage:
        args.stage.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = args.stage / "auth.json"
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        dest.chmod(0o600)
    if args.upload:
        with tempfile.TemporaryDirectory(prefix="bench-codex-auth-") as folder:
            path = Path(folder) / "auth.json"
            path.write_text(payload)
            path.chmod(0o600)
            subprocess.run([
                "aws", "ssm", "put-parameter", "--region", args.region,
                "--name", parameter, "--type", "SecureString",
                "--tier", "Intelligent-Tiering", "--value", f"file://{path}",
                "--overwrite",
            ], check=True, stdout=subprocess.DEVNULL)
        worker = f" for worker {args.worker}" if args.worker is not None else ""
        print(f"Stored Codex subscription login in SSM{worker}")


if __name__ == "__main__":
    main()
