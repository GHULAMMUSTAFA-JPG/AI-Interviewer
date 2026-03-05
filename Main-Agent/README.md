# AI Interview Agent

> An autonomous AI-powered interview agent that conducts technical interviews using real-time event-driven architecture with MongoDB change streams and multi-model LLM support.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MongoDB](https://img.shields.io/badge/mongodb-7.0+-green.svg)](https://www.mongodb.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## 🎯 **Features**

### **Real-Time Event Processing**
- MongoDB change streams for instant message processing (no polling)
- Asynchronous 5-stage pipeline: Load → Build → Generate → Validate → Save
- Sub-second response time (100-500ms average)

### **Intelligent Conversation Management**
- **Memory System**: Auto-generates summaries every 5 turns
- **Structured Phases**: INTRO → EXPERIENCE → TECHNICAL → BEHAVIORAL → CLOSING
- **Context Management**: Sliding window (last 6 messages + running summary)

### **Multi-Model LLM Support**
- Google Gemini Flash 2.5 (default)
- OpenAI GPT-4
- Anthropic Claude
- Easy provider switching via configuration

### **Production-Grade Reliability**
- **Exception Handling**: Retry logic with exponential backoff (3 attempts)
- **Circuit Breaker**: Protects against LLM outages (5 failures → 60s cooldown)
- **Fallback Responses**: Phase-appropriate defaults when LLM fails
- **Output Validation**: Length checks, JSON injection detection, prompt leak prevention

### **Cost Controls & Safeguards**
- Duration monitoring (90min soft limit, 120min hard cutoff)
- Context window tracking (150K warning, 180K max tokens)
- Abandonment detection (3-minute silence timeout)
- Message truncation (1000 char limit)
- Turn count limits (200 warning, 500 max)

### **Final Hiring Evaluation**
- Structured recommendation: HIRE / MAYBE / NO_HIRE
- Confidence score (1-10)
- Top 3 strengths and concerns identified
- Detailed reasoning explanation

---

## 🏗️ **Architecture**

```
┌─────────────────┐
│  Candidate      │
│  Message        │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────┐
│  MongoDB (Transcripts Collection)       │
│  - Real-time change stream listener     │
└────────┬────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────┐
│  5-Stage Async Pipeline                 │
├─────────────────────────────────────────┤
│  1. LOAD CONTEXT                        │
│     - Interview state, history          │
│     - Conversation summary              │
│     - Job description + CV              │
│                                         │
│  2. BUILD PROMPT                        │
│     - Phase-specific instructions       │
│     - Recent messages (last 6)          │
│     - Security constraints              │
│                                         │
│  3. CALL LLM                            │
│     - Multi-model support               │
│     - Retry with backoff                │
│     - Circuit breaker protection        │
│                                         │
│  4. VALIDATE OUTPUT                     │
│     - Length check (30-500 chars)       │
│     - JSON injection detection          │
│     - Prompt leak prevention            │
│                                         │
│  5. SAVE & UPDATE                       │
│     - Save response to MongoDB          │
│     - Update conversation summary       │
│     - Phase transition logic            │
│     - Generate evaluation (if complete) │
└────────┬────────────────────────────────┘
         │
         ▼
┌─────────────────┐
│  Agent Response │
│  (TTS/Frontend) │
└─────────────────┘
```

---

## 🚀 **Quick Start**

### **Prerequisites**
- Python 3.11+
- MongoDB 7.0+ (configured as replica set)
- UV package manager
- API key for LLM (Gemini/OpenAI/Claude)

### **1. MongoDB Replica Set Setup**

Edit MongoDB config (`mongod.cfg` or `/etc/mongod.conf`):
```yaml
replication:
  replSetName: "rs0"
```

Restart MongoDB and initialize replica set:
```bash
# Windows
net stop MongoDB
net start MongoDB

# Linux/Mac
sudo systemctl restart mongod

# Initialize replica set
mongosh
> rs.initiate()
> exit
```

### **2. Installation**

```bash
# Clone repository
git clone <your-repo-url>
cd Main-Agent

# Install dependencies
uv sync

# Setup database
uv run python setup_db.py
```

### **3. Configuration**

Create `.env` file:
```bash
# LLM Configuration
LLM_PROVIDER=gemini                    # gemini | openai | anthropic
GEMINI_API_KEY=your-gemini-key-here
GEMINI_MODEL=models/gemini-2.5-flash

# MongoDB
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB_NAME=interview_agent

# Safeguard Limits
SOFT_DURATION_LIMIT_MINUTES=90
HARD_DURATION_LIMIT_MINUTES=120
CONTEXT_WARNING_TOKENS=150000
CONTEXT_MAX_TOKENS=180000
SILENCE_TIMEOUT_SECONDS=180
```

### **4. Run**

**Terminal 1** - Start the agent:
```bash
uv run python src/main.py
```

**Terminal 2** - Test with single message:
```bash
uv run python test_insert.py
```

**Or run full interview simulation** (13 messages, all phases):
```bash
uv run python test_full_interview.py
```

---

## 📊 **Database Schema**

### **Interviews Collection**
```javascript
{
  _id: ObjectId,
  candidate_id: ObjectId,
  job_id: ObjectId,
  status: "in_progress" | "completed" | "abandoned",
  phase: "INTRO" | "EXPERIENCE" | "TECHNICAL" | "BEHAVIORAL" | "CLOSING",
  turn_count: number,
  phase_turn_count: number,
  conversation_summary: string,         // Updated every 5 turns
  evaluation: {                         // Generated when complete
    recommendation: "HIRE" | "MAYBE" | "NO_HIRE",
    score: number,                      // 1-10
    strengths: [string, string, string],
    concerns: [string, string, string],
    reasoning: string
  },
  limits: {
    max_duration_hit: boolean,
    context_overflow: boolean,
    forced_end_reason: string | null
  },
  started_at: Date,
  ended_at: Date | null
}
```

### **Transcripts Collection**
```javascript
{
  _id: ObjectId,
  interview_id: ObjectId,
  speaker: "candidate" | "agent",
  text: string,
  timestamp: Date,
  metadata: {
    phase: string,
    turn_count: number,
    is_fallback: boolean,
    llm_provider: string,
    estimated_tokens: number,
    forced_end: boolean,
    reason: string
  }
}
```

---

## 🧪 **Testing**

### **Unit Tests**
```bash
# Run all unit tests
uv run pytest tests/unit/

# Specific test suites
uv run pytest tests/unit/test_validator.py      # 18 tests
uv run pytest tests/unit/test_prompt_builder.py  # 7 tests
```

### **Integration Tests**
```bash
# Full pipeline test
uv run pytest tests/integration/test_pipeline.py

# Context loader test (requires MongoDB)
uv run pytest tests/integration/test_context_loader.py
```

### **End-to-End Test**
```bash
# Complete interview simulation (13 messages)
uv run python test_full_interview.py
```

**Expected Output:**
- ✓ 13 messages processed
- ✓ Summaries at turn 5 and 10
- ✓ Phase transitions: INTRO → EXPERIENCE → TECHNICAL → BEHAVIORAL → CLOSING
- ✓ Final evaluation generated
- ✓ Interview status: "completed"

---

## 📁 **Project Structure**

```
Main-Agent/
├── src/
│   ├── agent/
│   │   ├── context_loader.py       # Load interview data from MongoDB
│   │   ├── prompt_builder.py       # Build secure prompts
│   │   ├── llm_provider.py         # Multi-model LLM abstraction
│   │   ├── validator.py            # Output validation & security
│   │   ├── pipeline.py             # 5-stage orchestrator
│   │   ├── summary_generator.py    # Memory management
│   │   ├── phases.py               # Interview phase logic
│   │   ├── evaluator.py            # Final hiring evaluation
│   │   ├── safeguards.py           # Cost controls & limits
│   │   ├── retry.py                # Exception handling
│   │   └── models.py               # Pydantic data models
│   ├── config.py                   # Configuration
│   ├── exceptions.py               # Custom exceptions
│   └── main.py                     # Entry point (change stream)
├── tests/
│   ├── unit/                       # Unit tests (25 total)
│   └── integration/                # Integration tests
├── setup_db.py                     # Database initialization
├── test_insert.py                  # Single message test
├── test_full_interview.py          # Complete interview simulation
├── IMPLEMENTATION_COMPLETE.md      # Complete system reference
├── TESTING.md                      # Testing guide
├── DEMO.md                         # Live demo script
└── README.md                       # This file
```

---

## 🔍 **Verification & Debugging**

### **Check Interview State**
```javascript
// MongoDB Shell
db.interviews.findOne({}, {
  status: 1,
  phase: 1,
  turn_count: 1,
  conversation_summary: 1,
  evaluation: 1
})
```

### **View Conversation History**
```javascript
db.transcripts.find({}).sort({timestamp: 1}).pretty()
```

### **Check for Safeguard Triggers**
```javascript
db.interviews.find({"limits.forced_end_reason": {$ne: null}})
db.transcripts.find({"metadata.forced_end": true})
```

### **Count Messages**
```javascript
db.transcripts.countDocuments({speaker: "candidate"})
db.transcripts.countDocuments({speaker: "agent"})
```

---

## 🎓 **Key Technical Decisions**

### **Why MongoDB Change Streams?**
- Real-time event processing (no polling loop)
- Native MongoDB feature (no additional infrastructure)
- Ordered, resumable streams
- Low latency (<100ms detection)

### **Why 5-Stage Pipeline?**
- Clear separation of concerns
- Easy to test each stage independently
- Simple to add middleware (logging, metrics)
- Predictable error boundaries

### **Why Conversation Summaries?**
- Prevents context window overflow
- Maintains long-term memory
- Reduces token costs (summarize old messages)
- LLM sees: recent context + summary of earlier conversation

### **Why Circuit Breaker Pattern?**
- Protects against cascading LLM failures
- Prevents cost runaway during outages
- Fast-fail when service is down
- Auto-recovery testing

---

## 📈 **Performance Characteristics**

| Metric | Value |
|--------|-------|
| **Avg Response Time** | 100-500ms |
| **Change Stream Latency** | <100ms |
| **Memory per Interview** | ~2-5MB |
| **Context Window** | 150K-180K tokens |
| **Max Concurrent Interviews** | Limited by MongoDB connections |
| **LLM Retry Budget** | 3 attempts per message |
| **Circuit Breaker Threshold** | 5 failures |

---

## 🛡️ **Security Features**

- ✅ Output validation (length, format, content)
- ✅ JSON injection detection
- ✅ Prompt leak prevention
- ✅ Message truncation (prevents DOS)
- ✅ Rate limiting via turn counts
- ✅ No sensitive data in logs
- ✅ Environment variable secrets

---

## 🚧 **Roadmap / Future Enhancements**

- [ ] TTS integration for voice responses
- [ ] Frontend web interface
- [ ] Multi-language support
- [ ] Interview analytics dashboard
- [ ] Candidate feedback collection
- [ ] Custom evaluation criteria per job
- [ ] Video interview support
- [ ] Real-time transcript export

---

## 📄 **License**

MIT License - see LICENSE file for details

---

## 🤝 **Contributing**

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Add tests for new features
4. Ensure all tests pass
5. Submit a pull request

---

## 📞 **Support**

For questions or issues:
- Open a GitHub issue
- Check TESTING.md for troubleshooting
- Review IMPLEMENTATION_COMPLETE.md for system details

---

## 🙏 **Acknowledgments**

Built with:
- [Motor](https://motor.readthedocs.io/) - Async MongoDB driver
- [Pydantic](https://docs.pydantic.dev/) - Data validation
- [Google Gemini](https://ai.google.dev/) - LLM provider
- [UV](https://docs.astral.sh/uv/) - Package manager

---

**Made for autonomous interviewing**
