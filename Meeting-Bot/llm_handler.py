import google.generativeai as genai
import os

# Try to import credentials
try:
    import creds
    API_KEY = creds.GEMINI_API_KEY
except ImportError:
    API_KEY = os.getenv("GEMINI_API_KEY")

def generate_meeting_summary(transcript_text):
    """
    Sends the transcript to Gemini.
    Generates a high-level summary.
    Hides "Action Items" if none exist.
    """
    if not API_KEY:
        print("❌ Error: GEMINI_API_KEY not found in creds.py")
        return None

    print("🧠 Sending transcript to Gemini (Smart Mode)...")

    genai.configure(api_key=API_KEY)

    # 2.5-flash
    model = genai.GenerativeModel('gemini-2.5-flash')

    # --- THE CONDITIONAL PROMPT ---
    prompt = f"""
    You are an expert Chief of Staff.
    Summarize the following meeting transcript.

    TRANSCRIPT:
    "{transcript_text}"

    --------------------------------------------------

    **CRITICAL INSTRUCTIONS:**
    1. **Infer Roles:** Guess who is the "Lead", "Candidate", "Client", etc.
    2. **Fix Typos:** Correct phonetic errors (e.g. "coffee" -> "copies", "rice 7" -> "Ryzen 7").
    3. **Dynamic Action Items:** ONLY include the "Action Items" section if specific tasks were assigned. If there are no clear next steps, OMIT the section entirely.

    --------------------------------------------------

    **OUTPUT FORMAT:**

    # 📅 MEETING REPORT

    ## 🎯 Goal
    (1 sentence: Why did this meeting happen?)

    ## 👥 Participants (Inferred)
    * **[Name or "Unknown"]** - [Inferred Role]

    ## 📝 Executive Summary
    (A concise narrative of the outcome.)

    ## 💡 Key Insights & Decisions
    * **[Insight]**: [Explanation]

    ## 🗣️ Discussion Highlights
    (Single-line bullets. No nesting.)
    * [Topic]: [Fact]

    ## ⚡ Action Items (OPTIONAL - OMIT IF EMPTY)
    * **[Name/Role]**: [Task]
    """

    try:
        response = model.generate_content(prompt)
        print("✅ Summary generated successfully!")
        return response.text
    except Exception as e:
        print(f"❌ LLM Generation Error: {e}")
        return None
