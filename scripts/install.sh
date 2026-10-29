#!/usr/bin/env bash
# Install smart-espresso from this checkout, or clone it, and start it on boot.
# The unit file is deploy/smart-espresso.service in that checkout.
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    DRY_RUN=1
  fi
done

SOURCE=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

if [[ -z "$SOURCE" || ! -f "$SOURCE/deploy/smart-espresso.service" ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "dry-run: would clone https://github.com/idoshr/smart-espresso.git to /opt/smart-espresso"
    echo "dry-run: would install deploy/smart-espresso.service from that checkout"
    exit 0
  fi
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "run as root (sudo)" >&2
    exit 1
  fi
  if [[ ! -d /opt/smart-espresso/.git ]]; then
    git clone https://github.com/idoshr/smart-espresso.git /opt/smart-espresso
  else
    git -C /opt/smart-espresso pull --ff-only
  fi
  SOURCE=/opt/smart-espresso
fi

exec python3 "$SOURCE/scripts/install_service.py" --source "$SOURCE" "$@"
