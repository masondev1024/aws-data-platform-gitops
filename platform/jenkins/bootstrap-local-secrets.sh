#!/usr/bin/env bash
set -euo pipefail

secret_dir="${JENKINS_SECRETS_DIR:-/tmp/develope-project-jenkins-secrets}"
export JENKINS_BOOTSTRAP_SECRET_DIR="${secret_dir}"

python3 - <<'PY'
from pathlib import Path
import os
import secrets
import stat
import sys


secret_dir = Path(os.environ["JENKINS_BOOTSTRAP_SECRET_DIR"]).expanduser()
current_uid = os.getuid()

if secret_dir.is_symlink():
    raise SystemExit(f"Refusing symlink secret directory: {secret_dir}")

secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
secret_dir = secret_dir.resolve(strict=True)
dir_stat = secret_dir.stat()
if dir_stat.st_uid != current_uid:
    raise SystemExit(f"Secret directory must be owned by uid {current_uid}: {secret_dir}")
if stat.S_IMODE(dir_stat.st_mode) != 0o700:
    secret_dir.chmod(0o700)


def ensure_secret_file(name: str, default: str | None) -> Path:
    path = secret_dir / name
    if path.is_symlink():
        raise SystemExit(f"Refusing symlink secret file: {path}")
    if path.exists():
        if not path.is_file():
            raise SystemExit(f"Secret path is not a regular file: {path}")
        if path.stat().st_uid != current_uid:
            raise SystemExit(f"Secret file must be owned by uid {current_uid}: {path}")
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o444)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if default is not None:
                handle.write(default)
    path.chmod(0o444)
    return path


admin_user = ensure_secret_file("jenkins_admin_user", "admin\n")
admin_password = ensure_secret_file(
    "jenkins_admin_password",
    f"{secrets.token_urlsafe(32)}\n",
)
agent_secret = ensure_secret_file("jenkins_agent_secret", None)

if not admin_user.read_text(encoding="utf-8").strip():
    raise SystemExit(f"Admin user secret is empty: {admin_user}")
if not admin_password.read_text(encoding="utf-8").strip():
    raise SystemExit(f"Admin password secret is empty: {admin_password}")

for path in (admin_user, admin_password, agent_secret):
    if stat.S_IMODE(path.stat().st_mode) != 0o444:
        raise SystemExit(f"Secret file must be Compose-readable mode 0444 inside private directory: {path}")
PY

cat <<EOF
Local Jenkins secret files are ready outside the repository:
  ${secret_dir}

Use this environment value when running docker compose:
  export JENKINS_SECRETS_DIR=${secret_dir}

Admin credential files:
  ${secret_dir}/jenkins_admin_user
  ${secret_dir}/jenkins_admin_password

After the controller is up, copy the generated secret for node 'platform-agent'
from the Jenkins UI with:
  JENKINS_SECRETS_DIR=${secret_dir} bash platform/jenkins/set-agent-secret.sh

The script does not print secret values. Read the admin password file locally
only when you need to log in to the loopback UI.
EOF
