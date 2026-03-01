Here is your **organized, production-grade prompt / system design description**, rewritten clearly so it can be used for documentation, LLM context, or team reference.

---

# AI Interviewer System Architecture and Component Flow

## Overview

The system is a real-time AI interviewer that joins online meetings, conducts conversations with candidates, and responds using AI-generated speech. It consists of several independent services communicating through event-driven architecture using MongoDB change streams.

The core components are:

* UI Service
* Main Agent (Conversation Brain)
* TTS Service (Text-to-Speech)
* STT Service (Speech-to-Text)
* Meeting Bot Service (Headless browser container)
* MongoDB (Event bus and storage)

---

# Core Design Principles

* Event-driven architecture
* Containerized microservices
* Loose coupling between components
* Real-time bidirectional conversation
* Database-driven event communication (MongoDB Change Streams)
* Headless browser meeting automation

---

# Component Breakdown

## 1. UI Service

### Responsibilities

* Collects interview configuration from user
* Accepts:

  * Meeting link
  * Job description
  * Company information
  * Candidate CV

### Actions

* Stores interview metadata in MongoDB
* Triggers meeting container creation
* Notifies Main Agent to start interview

### Communicates with

* MongoDB
* Main Agent
* Meeting Bot containers

---

## 2. Meeting Bot Service (Headless Container)

This component is currently part of the STT service but should be separated logically.

### Responsibilities

* Joins meeting using headless browser
* Connects audio input/output
* Acts as physical presence of AI interviewer in meeting

### Technologies Used

* Headless browser (Chromium / Playwright / Puppeteer)
* Docker containers
* Audio input/output routing

### Responsibilities inside meeting

* Plays audio received from TTS service
* Captures candidate audio
* Sends captured audio to STT logic

---

## 3. STT Service (Speech-to-Text)

This service has two logical subcomponents:

### A. Meeting Bot Controller

* Launches headless containers
* Manages meeting lifecycle

### B. STT Processing Engine

### Responsibilities

* Receives audio stream from Meeting Bot
* Converts speech to text
* Saves transcript in MongoDB

### Example MongoDB document

```
transcripts collection

{
  _id: ObjectId,
  meeting_id: "meeting123",
  speaker: "candidate",
  text: "Hello, nice to meet you",
  is_final: true,
  created_at: timestamp
}
```

### This triggers MongoDB change stream event

---

## 4. Main Agent Service (Conversation Brain)

This is the core conversation intelligence component.

### Responsibilities

* Subscribes to MongoDB transcript events
* Generates intelligent alternate text responses using LLM
* Stores responses in MongoDB

### Also responsible for:

* Starting conversation by inserting greeting message

Example greeting document:

```
{
  speaker: "agent",
  text: "Hello, welcome to the interview. Can you introduce yourself?",
  type: "greeting"
}
```

### Internal workflow

```
MongoDB change stream event
        ↓
Receive candidate transcript
        ↓
Build prompt using:
    - job description
    - company info
    - candidate CV
    - conversation history
        ↓
Call LLM
        ↓
Generate alternate text response
        ↓
Save response to MongoDB
```

---

## 5. TTS Service (Text-to-Speech)

### Responsibilities

* Subscribes to MongoDB response events
* Converts agent text into speech
* Sends audio to Meeting Bot container

### Workflow

```
MongoDB change stream event
        ↓
Receive agent response text
        ↓
Convert text to speech (ElevenLabs or other TTS)
        ↓
Send audio to Meeting Bot container
        ↓
Meeting Bot plays audio in meeting
```

---

## 6. MongoDB (Storage and Event Bus)

MongoDB acts as both:

* Persistent storage
* Event streaming system via Change Streams

Collections used:

### transcripts collection

Stores candidate speech converted to text

### responses collection

Stores agent-generated replies

### meetings collection

Stores meeting metadata

---

# Full System Conversation Flow

## Step 1: Interview Setup

```
User enters meeting link, job description, company info, CV in UI
        ↓
UI stores data in MongoDB
        ↓
UI launches Meeting Bot container
        ↓
Meeting Bot joins meeting
```

---

## Step 2: Conversation Start

```
Main Agent inserts greeting message into MongoDB
        ↓
TTS detects new greeting event
        ↓
TTS converts greeting text to speech
        ↓
Meeting Bot plays greeting audio in meeting
```

---

## Step 3: Candidate Response

```
Candidate speaks
        ↓
Meeting Bot captures audio
        ↓
STT Service converts audio to text
        ↓
STT saves transcript in MongoDB
```

---

## Step 4: AI Response Generation

```
MongoDB emits transcript event
        ↓
Main Agent receives event
        ↓
Main Agent generates alternate text using LLM
        ↓
Main Agent saves response in MongoDB
```

---

## Step 5: AI Speech Output

```
MongoDB emits response event
        ↓
TTS Service receives event
        ↓
TTS converts text to speech
        ↓
Meeting Bot plays speech in meeting
```

---

## Step 6: Loop continues until interview ends

```
Candidate speaks → STT → Main Agent → TTS → Meeting Bot speaks
```

---

# Logical Separation of Responsibilities

Recommended separation:

STT Service should be split into:

* Meeting Bot Service
* STT Processing Service

Final architecture services:

* UI Service
* Meeting Bot Service
* STT Service
* Main Agent Service
* TTS Service
* MongoDB

---

# Event Flow Diagram

```
UI
 ↓
MongoDB ← Meeting Bot
 ↓            ↑
Main Agent    │
 ↓            │
MongoDB       │
 ↓            │
TTS Service   │
 ↓            │
Meeting Bot ──┘
 ↓
Candidate
 ↓
STT Service
 ↓
MongoDB
```

---

# Key Technologies Used

Main Agent

* Python
* AsyncIO
* LLM API

TTS Service

* ElevenLabs API
* Audio streaming

STT Service

* Web Speech API or streaming STT engine

Meeting Bot

* Playwright / Puppeteer
* Headless Chromium
* Docker

Database

* MongoDB
* Change Streams

---

# Summary

This system implements a fully automated AI interviewer using:

* Headless browser meeting automation
* Speech-to-text conversion
* LLM-based intelligent conversation engine
* Text-to-speech synthesis
* Event-driven microservices architecture

The conversation loop is fully automated and operates in real time.

---

If you want, I can also convert this into:

* developer-level architecture diagram
* or production folder structure for each service.
