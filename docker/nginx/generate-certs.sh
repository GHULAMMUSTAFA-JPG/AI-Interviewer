#!/bin/bash
# Generates a self-signed TLS certificate for local HTTPS testing.
#
# Usage (run from repo root):
#   chmod +x docker/nginx/generate-certs.sh
#   ./docker/nginx/generate-certs.sh
#
# Output:
#   docker/nginx/certs/fullchain.pem   — self-signed certificate
#   docker/nginx/certs/privkey.pem     — private key
#
# Browsers will show a "Not Secure" warning for self-signed certs.
# For production, replace these files with a real cert (e.g. Let's Encrypt).
#   certbot certonly --standalone -d yourdomain.com
#   cp /etc/letsencrypt/live/yourdomain.com/fullchain.pem docker/nginx/certs/
#   cp /etc/letsencrypt/live/yourdomain.com/privkey.pem   docker/nginx/certs/

set -e

CERT_DIR="$(dirname "$0")/certs"
mkdir -p "$CERT_DIR"

openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "$CERT_DIR/privkey.pem" \
    -out    "$CERT_DIR/fullchain.pem" \
    -subj "/C=US/ST=Local/L=Local/O=AI-Interviewer/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

echo ""
echo "Self-signed cert generated:"
echo "  $CERT_DIR/fullchain.pem"
echo "  $CERT_DIR/privkey.pem"
echo ""
echo "Valid for 365 days. Restart nginx to pick up the new cert:"
echo "  docker compose restart nginx"
