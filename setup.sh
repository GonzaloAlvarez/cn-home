#!/bin/bash
# cn-home/setup.sh — interactive workstation-side prep.
#
#   1. Prompts for any missing .env values (one-time).
#   2. Fetches the step-ca root CA over plain HTTP (LAN-only bootstrap).
#   3. Renders templates with envsubst.
#
# Re-run any time; idempotent. .env is written once and left alone after.

set -euo pipefail

cd "$(dirname "$0")"

set_env_var() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${val}|" .env && rm -f .env.bak
  else
    echo "${key}=${val}" >> .env
  fi
}

if [ ! -f .env ]; then
  > .env
  while IFS= read -r line; do
    if [[ -z "$line" || "$line" =~ ^[[:space:]]*# || ! "$line" =~ = ]]; then
      echo "$line" >> .env
      continue
    fi
    varname="${line%%=*}"
    default="${line#*=}"
    if [[ -n "$default" ]]; then
      read -r -p "${varname} [${default}]: " value
      echo "${varname}=${value:-$default}" >> .env
    else
      read -r -p "${varname}: " value
      echo "${varname}=${value}" >> .env
    fi
  done < .env.example
  echo ""
fi

set -o allexport
# shellcheck disable=SC1091
source .env
set +o allexport

mkdir -p certs traefik-lan

echo "Fetching step-ca root CA from http://${PKI_IP}/cert/ca.crt ..."
if curl -sfL -o certs/root_ca.crt "http://${PKI_IP}/cert/ca.crt"; then
  echo "  → certs/root_ca.crt ($(wc -c < certs/root_ca.crt) bytes)"
else
  echo "  WARNING: could not fetch root CA. Traefik's Lego will fail to trust"
  echo "  step-ca until certs/root_ca.crt is present."
fi

if command -v envsubst >/dev/null; then
  envsubst '${LAN_DOMAIN}' < traefik-lan/dynamic.yml.tmpl > traefik-lan/dynamic.yml
  echo "  → traefik-lan/dynamic.yml rendered"
  envsubst '${LAN_DOMAIN} ${KAISER_IP}' < coredns/Corefile.tmpl > coredns/Corefile
  echo "  → coredns/Corefile rendered"
else
  echo "  WARNING: envsubst not found; cannot render templates. Install gettext."
fi

echo ""
echo "Setup complete. Next:"
echo "  ./deploy                       — program pfSense DNS + sync + bring up cn-home"
echo "  ./deploy --with-observability  — also deploy the sibling cn-observability/"
