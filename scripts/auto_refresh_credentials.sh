#!/bin/bash
# Automatically refreshes temporary AWS STS credentials into a running MapServer container.
# Run this in the background when doing local development with SSO/STS credentials.

set -u

AWS_PROFILE_ARG=()
if [ -n "${AWS_PROFILE:-}" ]; then
  AWS_PROFILE_ARG=(--profile "$AWS_PROFILE")
fi

while true; do
  if docker ps --format '{{.Names}}' | grep -q "^mapserver$"; then
    tmp_creds="$(mktemp /tmp/mapserver-creds.XXXXXX.json)"
    if aws configure export-credentials "${AWS_PROFILE_ARG[@]}" > "$tmp_creds" && [ -s "$tmp_creds" ] && jq -e '.AccessKeyId and .SecretAccessKey' "$tmp_creds" >/dev/null; then
      docker cp "$tmp_creds" mapserver:/tmp/aws_credentials.json
      echo "Refreshed MapServer AWS credentials at $(date)"
    else
      echo "Skipped MapServer AWS credential refresh; run 'aws sso login --profile ${AWS_PROFILE:-default}' if this is an SSO profile. $(date)" >&2
    fi
    rm -f "$tmp_creds"
  fi
  # Refresh every 15 minutes
  sleep 900
done
