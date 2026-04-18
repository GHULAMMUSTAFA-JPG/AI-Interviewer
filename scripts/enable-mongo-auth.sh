#!/bin/bash
# Run this ONCE on an existing MongoDB installation that has no auth yet.
#
# What it does:
#   1. Connects to the running MongoDB container WITHOUT credentials
#   2. Creates the admin root user
#   3. Prints next steps to restart with auth enabled
#
# Usage:
#   chmod +x scripts/enable-mongo-auth.sh
#   ./scripts/enable-mongo-auth.sh
#
# After running this script:
#   docker compose restart mongodb
#   (MongoDB will restart with --auth enabled per docker-compose.yml)

set -e

MONGO_ROOT_PASSWORD="${MONGO_ROOT_PASSWORD:-changeme_mongo}"

echo "[enable-mongo-auth] Checking if MongoDB is reachable without auth..."
if ! docker exec mongodb mongosh --quiet --eval "db.adminCommand('ping').ok" >/dev/null 2>&1; then
    echo "ERROR: MongoDB container is not running or not reachable."
    echo "Start it with: docker compose up -d mongodb"
    exit 1
fi

echo "[enable-mongo-auth] Creating admin user (password: ${MONGO_ROOT_PASSWORD})..."
docker exec mongodb mongosh admin --quiet --eval "
try {
    db.createUser({
        user: 'admin',
        pwd: '${MONGO_ROOT_PASSWORD}',
        roles: [{ role: 'root', db: 'admin' }]
    });
    print('Admin user created successfully.');
} catch (e) {
    if (e.codeName === 'DuplicateKey' || e.message.includes('already exists')) {
        print('Admin user already exists — OK.');
    } else {
        throw e;
    }
}
"

echo ""
echo "[enable-mongo-auth] Done. Next steps:"
echo "  1. Restart MongoDB to activate auth:"
echo "       docker compose restart mongodb"
echo "  2. Verify connection with credentials:"
echo "       docker exec -it mongodb mongosh 'mongodb://admin:${MONGO_ROOT_PASSWORD}@localhost:27017/admin'"
echo ""
echo "  The password '${MONGO_ROOT_PASSWORD}' is already set in .env.docker and docker-compose.yml."
echo "  Change it in both files if you want a different password."
