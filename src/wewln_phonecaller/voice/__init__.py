"""The voice engine (Phase 5): a real-time spoken conversation on an answered call.

    phone <-> Asterisk <-> AudioSocket (TCP, 8 kHz 16-bit PCM) <-> wewln
                                                     VAD -> STT -> LLM -> TTS -> playback

- `audio`: PCM frames, levels, resampling, WAV files;
- `vad`: finds where the caller speaks (energy detector with an adaptive noise floor);
- `stt`, `llm`, `tts`: speech recognition, the dialogue model and speech synthesis; every
  backend runs on this computer (Vosk / faster-whisper / whisper.cpp, Ollama or an
  OpenAI-compatible local server, Piper) and is imported only when configured;
- `engine`: one conversation: greeting, turn-taking, barge-in, silence and error handling;
- `transport`: the audio connection of a call (AudioSocket server; an in-memory pair for tests);
- `simulate`: a simulated caller, so that the whole pipeline can be tried without any call;
- `runtime`: builds the configured backends and keeps them loaded between calls.

Nothing here places calls: the call service (telephony/service.py) starts a conversation
when a call it placed is answered, and ends the call when the conversation ends.
"""
