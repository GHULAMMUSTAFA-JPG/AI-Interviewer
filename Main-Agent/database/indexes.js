/**
 * MongoDB Index Setup for AI Interviewer
 * Run once after first deployment to ensure query performance at scale.
 * 
 * Usage:
 *   docker compose exec mongodb mongosh interviews < indexes.js
 */

print("📊 Setting up MongoDB indexes for interviews collection...");

// Index for status queries (cleanup service, UI polling)
print("  Creating index: status...");
db.interviews.createIndex({ status: 1 }, { background: true });

// Index for heartbeat-based cleanup (finds stale bots)
print("  Creating index: bot_heartbeat...");
db.interviews.createIndex({ bot_heartbeat: 1 }, { background: true });

// Index for age-based cleanup (finds old interviews)
print("  Creating index: created_at...");
db.interviews.createIndex({ created_at: 1 }, { background: true });

// Compound index for cleanup query optimization
print("  Creating compound index: status + bot_heartbeat...");
db.interviews.createIndex(
  { status: 1, bot_heartbeat: 1 },
  { background: true }
);

// Index for interview_id lookups (API queries)
print("  Creating index: interview_id...");
db.interviews.createIndex({ interview_id: 1 }, { unique: true, background: true });

// Index for transcripts (conversation view)
print("  Creating index: transcripts...");
db.transcripts.createIndex(
  { interview_id: 1, timestamp: 1 },
  { background: true }
);

print("✅ All indexes created successfully!");
print("");
print("📊 Index statistics:");
printjson(db.interviews.getIndexes());
print("");
print("📊 Transcript indexes:");
printjson(db.transcripts.getIndexes());
