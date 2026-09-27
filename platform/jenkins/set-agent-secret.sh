#!/usr/bin/env bash
set -euo pipefail

secret_dir="${JENKINS_SECRETS_DIR:-/tmp/develope-project-jenkins-secrets}"
export JENKINS_AGENT_SECRET_DIR="${secret_dir}"

secret_dir="$(
  python3 - <<'PY'
from pathlib import Path
import os
import stat
import sys

secret_dir = Path(os.environ["JENKINS_AGENT_SECRET_DIR"]).expanduser()
current_uid = os.getuid()

if secret_dir.is_symlink():
    raise SystemExit(f"Refusing symlink secret directory: {secret_dir}")
if not secret_dir.is_dir():
    raise SystemExit(f"Secret directory does not exist. Run platform/jenkins/bootstrap-local-secrets.sh first: {secret_dir}")

secret_dir = secret_dir.resolve(strict=True)
dir_stat = secret_dir.stat()
if dir_stat.st_uid != current_uid:
    raise SystemExit(f"Secret directory must be owned by uid {current_uid}: {secret_dir}")
if stat.S_IMODE(dir_stat.st_mode) != 0o700:
    raise SystemExit(f"Secret directory must be private mode 0700 before writing 0444 secrets: {secret_dir}")

agent_secret_path = secret_dir / "jenkins_agent_secret"
if agent_secret_path.is_symlink():
    raise SystemExit(f"Refusing symlink agent secret file: {agent_secret_path}")
if agent_secret_path.exists() and not agent_secret_path.is_file():
    raise SystemExit(f"Agent secret path is not a regular file: {agent_secret_path}")
if agent_secret_path.exists() and agent_secret_path.stat().st_uid != current_uid:
    raise SystemExit(f"Agent secret file must be owned by uid {current_uid}: {agent_secret_path}")

print(secret_dir)
PY
)"

agent_secret_path="${secret_dir}/jenkins_agent_secret"

if [ -t 0 ]; then
  printf 'Paste the platform-agent secret from Jenkins UI, then press Enter: ' >&2
  IFS= read -rs agent_secret
  printf '\n' >&2
else
  IFS= read -r agent_secret
fi
if [ -z "${agent_secret}" ]; then
  echo "Agent secret must not be empty." >&2
  exit 65
fi

tmp_path="$(mktemp "${secret_dir}/.jenkins_agent_secret.XXXXXX")"
cleanup() {
  rm -f "${tmp_path}"
  unset agent_secret
}
trap cleanup EXIT HUP INT TERM

umask 077
printf '%s\n' "${agent_secret}" > "${tmp_path}"
chmod 0444 "${tmp_path}"
mv "${tmp_path}" "${agent_secret_path}"
chmod 0444 "${agent_secret_path}"
unset agent_secret
trap - EXIT HUP INT TERM
printf 'Agent secret file updated: %s\n' "${agent_secret_path}"
