#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./infra/destroy.sh -g RESOURCE_GROUP [-y] [--no-wait]

Deletes the entire Azure resource group and every resource deployed by this
project. This operation is irreversible.

Required tools: az
The Azure CLI must be logged in and the selected subscription must be set.
EOF
}

resource_group=""
assume_yes=false
no_wait=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -g)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      resource_group="$2"
      shift 2
      ;;
    -y|--yes)
      assume_yes=true
      shift
      ;;
    --no-wait)
      no_wait=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

[[ -n "$resource_group" ]] || { usage >&2; exit 2; }
command -v az >/dev/null || { echo "Azure CLI (az) is required." >&2; exit 1; }

if ! az group exists --name "$resource_group" | grep -qx true; then
  echo "Resource group '$resource_group' does not exist."
  exit 0
fi

if [[ "$assume_yes" != true ]]; then
  printf "Delete resource group '%s' and ALL resources inside it? Type the resource group name to confirm: " "$resource_group"
  read -r confirmation
  [[ "$confirmation" == "$resource_group" ]] || {
    echo "Confirmation did not match; nothing was deleted."
    exit 1
  }
fi

delete_args=(group delete --name "$resource_group")
if [[ "$no_wait" == true ]]; then
  delete_args+=(--no-wait)
fi

az "${delete_args[@]}" --yes
if [[ "$no_wait" == true ]]; then
  echo "Deletion started for resource group '$resource_group'."
else
  echo "Resource group '$resource_group' and all contained resources were deleted."
fi
