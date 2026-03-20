#!/bin/bash
# Fix Chrome SingletonLock issue in meeting-bot container
# Run this when bot fails to launch due to profile lock

echo "🔧 Fixing Chrome SingletonLock issue..."

# Check if meeting-bot container is running
if ! docker ps --format '{{.Names}}' | grep -q meeting-bot; then
    echo "❌ meeting-bot container is not running"
    exit 1
fi

# Remove stale lock file
echo "📁 Removing stale SingletonLock file..."
docker exec meeting-bot rm -f /app/chrome_profile/SingletonLock

# Verify removal
if docker exec meeting-bot test -f /app/chrome_profile/SingletonLock; then
    echo "❌ Failed to remove lock file"
    exit 1
else
    echo "✅ Lock file removed successfully"
fi

# Restart meeting-bot
echo "🔄 Restarting meeting-bot..."
docker restart meeting-bot

echo "✅ Fix complete! Bot should now launch Chrome successfully"
echo "📋 Check logs with: docker logs -f meeting-bot"
