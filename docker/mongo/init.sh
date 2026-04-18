#!/bin/bash
# Initialises the MongoDB replica set after the container starts with auth enabled.
# Idempotent — safe to re-run; AlreadyInitialized is treated as success.
set -e

MONGO_ROOT_PASSWORD="${MONGO_ROOT_PASSWORD:-changeme_mongo}"

echo "[mongo-init] Waiting for MongoDB to accept connections..."
until mongosh "mongodb://admin:${MONGO_ROOT_PASSWORD}@mongodb:27017/admin" \
    --quiet --eval "db.adminCommand('ping').ok" 2>/dev/null; do
    sleep 2
done
echo "[mongo-init] MongoDB ready."

mongosh "mongodb://admin:${MONGO_ROOT_PASSWORD}@mongodb:27017/admin" --quiet --eval "
try {
    rs.initiate({ _id: 'rs0', members: [{ _id: 0, host: 'mongodb:27017' }] });
    print('[mongo-init] Replica set rs0 initialized.');
} catch (e) {
    if (e.codeName === 'AlreadyInitialized') {
        print('[mongo-init] Replica set already initialized — OK.');
    } else {
        throw e;
    }
}
"
echo "[mongo-init] Done."
