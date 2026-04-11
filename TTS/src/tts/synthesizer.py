"""ElevenLabs TTS synthesizer — NOT used on the edge-tts branch.

This file is kept as a placeholder. On the edge-tts branch, EdgeTTSSynthesizer
(edge_synthesizer.py) is the only synthesizer used.

To switch to ElevenLabs on a future branch:
  1. Restore the full ElevenLabs implementation here.
  2. Add ElevenLabs config fields back to config.py.
  3. Add httpx[http2] and the ElevenLabs key env vars to .env.docker.
  4. Update TTSService in main.py to instantiate ElevenLabsSynthesizer.
"""
