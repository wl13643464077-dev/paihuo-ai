#!/bin/bash
set -euo pipefail

unset BASH_ENV CDPATH ENV GLOBIGNORE LD_LIBRARY_PATH LD_PRELOAD \
  PYTHONHOME PYTHONINSPECT PYTHONSTARTUP
export PATH="/usr/bin:/bin"

if [[ "$EUID" -ne 0 ]]; then
  echo "upgrade failed: the fixed launcher must run as root" >&2
  exit 1
fi

ENV_FILE="/etc/paihuo/paihuo.env"
CONTROL_DIR="/var/lib/paihuo-upgrade"
STATE_DIR="/var/lib/paihuo/data"
FIXED_OPS_LINK="/usr/local/lib/paihuo-ops"
FIXED_LAUNCHER="/usr/local/sbin/paihuo-upgrade"
BOOTSTRAP_TOOL="$CONTROL_DIR/bootstrap/bootstrap_release.py"
RELEASE_ID=""

UPGRADE_ARGS=()
while (($#)); do
  case "$1" in
    --release-id)
      if (($# < 2)) || [[ -n "$RELEASE_ID" ]] \
          || [[ ! "$2" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
        echo "upgrade failed: one safe release id is required" >&2
        exit 1
      fi
      RELEASE_ID="$2"
      shift 2
      ;;
    --release-id=*)
      value="${1#*=}"
      if [[ -n "$RELEASE_ID" ]] \
          || [[ ! "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
        echo "upgrade failed: one safe release id is required" >&2
        exit 1
      fi
      RELEASE_ID="$value"
      shift
      ;;
    --health-timeout)
      if (($# < 2)) || [[ ! "$2" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        echo "upgrade failed: health timeout must be numeric" >&2
        exit 1
      fi
      UPGRADE_ARGS+=("$1" "$2")
      shift 2
      ;;
    --health-timeout=*)
      value="${1#*=}"
      if [[ ! "$value" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        echo "upgrade failed: health timeout must be numeric" >&2
        exit 1
      fi
      UPGRADE_ARGS+=("--health-timeout" "$value")
      shift
      ;;
    *)
      echo "upgrade failed: unsupported production-wrapper argument" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$RELEASE_ID" ]]; then
  echo "upgrade failed: --release-id is required" >&2
  exit 1
fi

RELEASE_ROOT="/srv/paihuo/releases/$RELEASE_ID"
BOOTSTRAP_RELEASE_DIR="$CONTROL_DIR/bootstrap/releases/$RELEASE_ID"
BOOTSTRAP_LAUNCHER="$BOOTSTRAP_RELEASE_DIR/paihuo-upgrade"
BOOTSTRAP_OPS="$BOOTSTRAP_RELEASE_DIR/ops"
BOOTSTRAP_ATTESTATION="$BOOTSTRAP_RELEASE_DIR/attestation.json"
if ! SCRIPT_PATH="$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")"; then
  echo "upgrade failed: launcher path could not be resolved" >&2
  exit 1
fi
if [[ "$SCRIPT_PATH" == "$BOOTSTRAP_LAUNCHER" ]]; then
  if [[ "${PAIHUO_BOOTSTRAP_LAUNCHED:-}" != "1" ]]; then
    echo "upgrade failed: bootstrap launcher must be entered by trusted helper" >&2
    exit 1
  fi
  if [[ "$(/usr/bin/stat -Lc '%u:%g:%a:%h:%F' "$BOOTSTRAP_TOOL")" != \
        "0:0:600:1:regular file" ]]; then
    echo "upgrade failed: trusted bootstrap tool is unsafe" >&2
    exit 1
  fi
  /usr/bin/env -i "PATH=/usr/bin:/bin" "PYTHONDONTWRITEBYTECODE=1" \
    /usr/bin/python3 -I "$BOOTSTRAP_TOOL" \
    --check-stage \
    --release-id "$RELEASE_ID"
  TRUSTED_OPS="$BOOTSTRAP_OPS"
elif [[ "$SCRIPT_PATH" == "$FIXED_LAUNCHER" ]]; then
  if ! TRUSTED_OPS="$(/usr/bin/readlink -f -- "$FIXED_OPS_LINK")"; then
    echo "upgrade failed: fixed ops path could not be resolved" >&2
    exit 1
  fi
  if [[ "$(/usr/bin/stat -Lc '%u:%g:%a:%F' "$TRUSTED_OPS")" != \
        "0:0:755:directory" ]] \
      || [[ "$(/usr/bin/stat -Lc '%u:%g:%a:%h:%F' \
        "$TRUSTED_OPS/deploy/control_plane.py")" != \
        "0:0:644:1:regular file" ]]; then
    echo "upgrade failed: fixed ops trust metadata is unsafe" >&2
    exit 1
  fi
else
  echo "upgrade failed: run the root-owned fixed launcher" >&2
  exit 1
fi
TRUSTED_PYTHON=(
  /usr/bin/env -i
  "PATH=/usr/bin:/bin"
  "PYTHONDONTWRITEBYTECODE=1"
  "PYTHONPATH=$TRUSTED_OPS"
  /usr/bin/python3 -s
)

SECRET_BACKUP="$CONTROL_DIR/credentials/paihuo-env-$RELEASE_ID.backup"
CONTROL_ATTESTATION="$CONTROL_DIR/control-plane/$RELEASE_ID/attestation.json"
ARTIFACT_DIR="$CONTROL_DIR/incoming/$RELEASE_ID"
RELEASE_ARCHIVE="$ARTIFACT_DIR/$RELEASE_ID.tar.gz"
BUILD_RECEIPT="$ARTIFACT_DIR/$RELEASE_ID.receipt.json"
ARTIFACT_ATTESTATION="$ARTIFACT_DIR/release-artifact-attestation.json"
MATERIALIZED_ATTESTATION="$ARTIFACT_DIR/materialized-release-attestation.json"
CONTROL_PREPARED=0
UPGRADE_SUCCEEDED=0
WRAPPER_LOCK_HELD=0
WRAPPER_TRANSACTION_STARTED=0

restore_control_plane() {
  if [[ "$WRAPPER_LOCK_HELD" -ne 1 \
        || "$WRAPPER_TRANSACTION_STARTED" -ne 1 ]]; then
    return 0
  fi
  if [[ "$CONTROL_PREPARED" -eq 1 && "$UPGRADE_SUCCEEDED" -ne 1 \
        && -f "$CONTROL_ATTESTATION" ]]; then
    if "${TRUSTED_PYTHON[@]}" -m deploy.control_plane reconcile \
        --control-dir "$CONTROL_DIR" \
        --release-id "$RELEASE_ID"; then
      :
    else
      return 1
    fi
  fi
  if [[ -f "$CONTROL_DIR/wrapper-transaction.json" ]]; then
    if "${TRUSTED_PYTHON[@]}" -m deploy.control_plane finish \
        --control-dir "$CONTROL_DIR" \
        --release-id "$RELEASE_ID"; then
      :
    else
      return 1
    fi
  fi
  return 0
}

on_exit() {
  status=$?
  if ! restore_control_plane; then
    echo "upgrade failed: control-plane restoration was not proven" >&2
    exit 1
  fi
  exit "$status"
}

on_signal() {
  exit 130
}

cd /
trap on_exit EXIT
trap on_signal INT TERM HUP

if [[ -L "$CONTROL_DIR" ]]; then
  echo "upgrade failed: control directory must not be a symlink" >&2
  exit 1
fi
/usr/bin/install -d -m 0700 -o root -g root "$CONTROL_DIR"
if [[ "$(/usr/bin/stat -c '%u:%g:%a:%F' "$CONTROL_DIR")" != \
      "0:0:700:directory" ]]; then
  echo "upgrade failed: control directory must be root-owned 0700" >&2
  exit 1
fi
exec 9>"$CONTROL_DIR/wrapper.lock"
/usr/bin/chown root:root "$CONTROL_DIR/wrapper.lock"
/usr/bin/chmod 0600 "$CONTROL_DIR/wrapper.lock"
if ! /usr/bin/flock -n 9; then
  echo "upgrade failed: another deployment wrapper is active" >&2
  exit 1
fi
WRAPPER_LOCK_HELD=1

"${TRUSTED_PYTHON[@]}" -m deploy.control_plane bootstrap-check \
  --release-id "$RELEASE_ID" \
  --ops-root "$TRUSTED_OPS" \
  --launcher "$SCRIPT_PATH"
"${TRUSTED_PYTHON[@]}" -m deploy.control_plane release-evidence \
  --release "$RELEASE_ROOT" \
  --state "$STATE_DIR" \
  --control-dir "$CONTROL_DIR" \
  --archive "$RELEASE_ARCHIVE" \
  --receipt "$BUILD_RECEIPT" \
  --artifact-attestation "$ARTIFACT_ATTESTATION" \
  --materialized-attestation "$MATERIALIZED_ATTESTATION"
"${TRUSTED_PYTHON[@]}" -m deploy.control_plane gate \
  --release "$RELEASE_ROOT" \
  --control-dir "$CONTROL_DIR"
"${TRUSTED_PYTHON[@]}" -m deploy.control_plane begin \
  --release "$RELEASE_ROOT" \
  --control-dir "$CONTROL_DIR"
WRAPPER_TRANSACTION_STARTED=1
CONTROL_PREPARED=1
"${TRUSTED_PYTHON[@]}" -m deploy.control_plane prepare \
  --release "$RELEASE_ROOT" \
  --control-dir "$CONTROL_DIR"

"${TRUSTED_PYTHON[@]}" -m deploy.session_secret_env \
  --path "$ENV_FILE"
"${TRUSTED_PYTHON[@]}" -m deploy.session_secret_env \
  --path "$ENV_FILE" \
  --backup-to "$SECRET_BACKUP"
"${TRUSTED_PYTHON[@]}" -m deploy.session_secret_env \
  --path "$ENV_FILE" \
  --check-backup "$SECRET_BACKUP"
"${TRUSTED_PYTHON[@]}" -m deploy.upgrade_release \
  "${UPGRADE_ARGS[@]}" \
  --release "$RELEASE_ROOT" \
  --control-dir "$CONTROL_DIR" \
  --managed-env-file "$ENV_FILE" \
  --secret-backup "$SECRET_BACKUP" \
  --control-plane-attestation "$CONTROL_ATTESTATION" \
  --release-archive "$RELEASE_ARCHIVE" \
  --release-build-receipt "$BUILD_RECEIPT" \
  --release-artifact-attestation "$ARTIFACT_ATTESTATION" \
  --materialized-release-attestation "$MATERIALIZED_ATTESTATION" \
  --bootstrap-stage-attestation "$BOOTSTRAP_ATTESTATION"
"${TRUSTED_PYTHON[@]}" -m deploy.control_plane finish \
  --control-dir "$CONTROL_DIR" \
  --release-id "$RELEASE_ID"
UPGRADE_SUCCEEDED=1
trap - EXIT INT TERM HUP
