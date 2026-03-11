// View recent conversations from MongoDB
db = db.getSiblingDB('interviews');

print("\n=== RECENT INTERVIEWS ===\n");
var interviews = db.interviews.find().sort({ started_at: -1 }).limit(3).toArray();
interviews.forEach(function(interview) {
  print("Interview ID: " + interview.interview_id);
  print("Status: " + interview.status);
  print("Phase: " + interview.phase);
  print("Turn Count: " + interview.turn_count);
  print("Candidate: " + interview.candidate_name);
  print("Conversation Summary: " + interview.conversation_summary);
  print("---");
});

print("\n=== RECENT TRANSCRIPTS (Last 15) ===\n");
var transcripts = db.transcripts.aggregate([
  { $sort: { timestamp: -1 } },
  { $limit: 15 }
]).toArray();

transcripts.forEach(function(t) {
  print("[" + t.speaker + "] " + t.text.substring(0, 200) + "...");
  print("   Time: " + t.timestamp);
  print("---");
});
