# AI Interviewer — Claude Code Context

## Project layout
```
AI Interviwer/
├── docker-compose.yml      ← Build and start everything from here
├── .env.example            ← All secrets in one file → copy to .env
│
├── Main-Agent/             ← LLM brain (Python + pip, Gemini/OpenAI/Anthropic)
├── TTS/                    ← Text-to-speech (Python + uv, ElevenLabs + sounddevice)
├── Meeting-Bot/            ← Chrome bot + caption scraper (Playwright, Ubuntu)
└── UI/                     ← Web form (FastAPI + Jinja2)
```

Each service has its own `CLAUDE.md` with file-level details. Read those when
working inside a specific service folder.

## Event-driven architecture
No polling. MongoDB change streams are the event bus.

```
Browser → http://localhost:8080 (UI)
  └── POST /start
        └── INSERT interviews.interviews {status:"in_progress", meeting_url, cv, jd, ...}
              │
              ├── interview-agent (greeting watcher)
              │     └── INSERT interviews.transcripts {speaker:"agent", text:"Hello!..."}
              │           └── tts watches {speaker:"agent", audio_url:null}
              │                 └── ElevenLabs → PulseAudio → Chrome (candidate hears greeting)
              │
              └── meeting-bot (interviews watcher)
                    └── Playwright Chrome joins meeting_url
                          └── DOM caption poll loop
                                └── INSERT interviews.transcripts {speaker:"candidate", text:"..."}
                                      └── interview-agent (candidate watcher)
                                            └── LLM generates response
                                                  └── INSERT {speaker:"agent", text:"..."}
                                                        └── tts plays → loop continues
                                                              (until interview ends → evaluation)
```

## MongoDB collections (all in `interviews` database)
| Collection | Written by | Read/Watched by |
|---|---|---|
| `interviews.interviews` | UI | Main-Agent (greeting), Meeting-Bot |
| `interviews.transcripts` | Main-Agent, Meeting-Bot | Main-Agent (candidate watch), TTS |
| `interviews.evaluations` | Main-Agent | — |
| `interviews.agent_state` | Main-Agent | Main-Agent (resume token) |

## Start everything
```bash
cp .env.example .env
# Fill in GEMINI_API_KEY, ELEVENLABS_API_KEY, BOT_EMAIL
docker compose up --build
# Open http://localhost:8080
```

## Change stream filter summary
| Service | Watches | Filter |
|---|---|---|
| interview-agent (greeting) | `interviews.interviews` | `operationType=insert` |
| interview-agent (candidate) | `interviews.transcripts` | `insert + speaker=candidate` |
| TTS | `interviews.transcripts` | `insert + speaker=agent + audio_url=null` |
| Meeting-Bot | `interviews.interviews` | `insert + status=in_progress` |

## Per-service CLAUDE.md locations
- `Main-Agent/CLAUDE.md` — LLM pipeline, phase management, evaluation
- `TTS/CLAUDE.md` — ElevenLabs, PulseAudio, audio routing
- `Meeting-Bot/CLAUDE.md` — Playwright, caption scraper, speaker filter
- `UI/CLAUDE.md` — FastAPI form, the DB write that starts everything
