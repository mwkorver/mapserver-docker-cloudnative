#!/bin/bash
# Automatically refreshes temporary AWS STS credentials into a running MapServer container.
# Run this in the background when doing local development with SSO/STS credentials.

while true; do
  if docker ps --format '{{.Names}}' | grep -q "^mapserver$"; then
    # Export fresh credentials into a temporary file
    aws configure export-credentials > /tmp/creds.json
    
    # Push the credentials into the container
    docker cp /tmp/creds.json mapserver:/tmp/aws_credentials.json
    
    echo "Refreshed MapServer AWS credentials at $(date)"
  fi
  # Refresh every 15 minutes
  sleep 900
done
