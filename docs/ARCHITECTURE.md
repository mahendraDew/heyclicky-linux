# Architecture

## Overview

```text
┌────────────────────────── Qt GUI thread ──────────────────────────┐
│  Tray ── Panel ── Settings dialog                                 │
│    │        │                                                     │
│    └────────┴──── Pipeline (state machine, signals)               │
│                         │                                         │
│            ┌────────────┼─────────────┐                           │
│            ▼            ▼             ▼                           │
│       OverlayManager  Microphone   Single-instance IPC           │
│       (one per screen) Recorder    (QLocalServer)                │
└─────────────────────────┬──────────────────────────────────────────┘
                          │ worker thread (per utterance)
                          ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ STT ──► ScreenCaptureService ──► LLMProvider ──► TTS          │
   │ Groq   mss/portal/gnome/cli     Groq/Gemini/   edge/gemini/   │
   │ Gemini                              Ollama      piper/spd     │
   │ local                                                         │
   └───────────────────────────────────────────────────────────────┘
```

The Qt thread must never block on HTTP or audio. The pipeline owns a short-lived
worker per utterance. Results come back as Qt signals, which are queued to the
GUI thread automatically. High-frequency microphone levels use the same
mechanism but are emitted from the ALSA reader thread.

## Modules

| Module | Role |
| --- | --- |
| `app.py` | Argument parsing, Qt bootstrap, IPC server, doctor, capture test |
| `config.py` | Defaults, deep merge, XDG path, env overrides, 0600 save |
| `pipeline.py` | Voice state machine, recording, worker, history, pointing, TTS, transient hide |
| `state.py` | `VoiceState` and the ten-exchange `Conversation` |
| `audio.py` | `arecord`/`parec` capture, RMS metering, WAV wrapping |
| `capture.py` | Backend selection, multi-screen capture, JPEG resize, labels, cursor ordering |
| `geometry.py` | Logical↔screenshot coordinates, screen choice, Bezier math |
| `pointing.py` | Exact `[POINT:…]` grammar and tag stripping |
| `prompts.py` | Ported system prompt and screen labels |
| `hotkey.py` | evdev/pynput/GNOME/manual backends, combo parsing |
| `llm/` | Normalized streaming interface and three providers |
| `stt/` | Groq Whisper, Gemini audio, local faster-whisper, auto selection |
| `tts/` | Edge, Gemini, Piper, Speech Dispatcher, auto selection |
| `ui/theme.py` | Design tokens and application stylesheet |
| `ui/overlay.py` | Per-screen transparent windows and all animation |
| `ui/panel.py` | Two-column panel (controls + chat transcript), health rows, provider/model picker, settings |
| `ui/tray.py` | Tray icon/menu and generated app icon |

## State machine

```text
             start_listening
  IDLE ─────────────────────────► LISTENING
   ▲                                  │ stop_listening
   │                                  ▼
   │                             [transcribe]
   │                                  │ empty → IDLE
   │                                  ▼
   │                             PROCESSING ──► [capture → LLM]
   │                                  │
   │               no point / no TTS  │
   ├──────────────────────────────────┤
   │                                  │ coordinate
   │               IDLE ──► RESPONDING│ (emit point, then TTS)
   └──────────────────────────────────┘
```

Important details:

- A new press calls `cancel()` first: generation increments, the cancel event is
  set, TTS playback stops, the recorder is discarded, and any navigation resets.
- Every worker result is guarded by `generation == self._generation`. A stale
  response cannot overwrite a newer recording.
- `_set_state` notices the calling thread. Off-thread calls are emitted through
  `_state_requested`; only the GUI thread mutates overlay widgets.
- A point is emitted only after the state is set to idle, so the pointing ring
  is not competing with the response animation. This mirrors `CM:630`–`CM:634`.

## Threading rules

| Thread | May touch |
| --- | --- |
| GUI | Qt widgets, overlay, panel, tray, config, hotkey lifecycle |
| Hotkey listener | emits `_start_requested` / `_stop_requested` only |
| Mic reader | emits `audio_level` and `_max_reached` only |
| Response worker | STT, capture backend, HTTP, TTS; emits signals |
| Health worker | provider/STT/TTS/capture/hotkey probes; emits one result |

`ScreenCaptureService.capture_all(screens, cursor)` accepts a snapshot because
`QScreen`/`QCursor` must be read on the GUI thread. The worker hides the overlay
through a signal + `threading.Event`, waits 120ms for the compositor, captures,
then asks the GUI thread to show the overlay again.

## Provider interface

All providers implement:

```python
class LLMProvider:
    def health(self) -> tuple[bool, str]
    def list_models(self) -> list[str]
    def _stream(self, ChatRequest, emit, cancel) -> str
    def stream_chat(self, ChatRequest, on_delta=None, cancel=None) -> str
```

`ChatRequest` carries the system prompt, user text, labeled JPEGs, history and
sampling knobs. Groq and Gemini send data URLs/inline data; Ollama sends raw
base64. If a selected model cannot see images, all three providers drop the
images and add an explicit note to the text prompt instead of failing.

Streaming formats:

- Groq: OpenAI SSE (`data: {...}`, `[DONE]`);
- Gemini: `streamGenerateContent?alt=sse`;
- Ollama: newline-delimited JSON from `/api/chat`.

## Pointing pipeline

1. `parse_pointing()` splits the reply into `spoken_text` and a coordinate,
   label and optional 1-based screen number.
2. The bundle is already sorted cursor-screen first, matching the labels.
3. The target image is `bundle.images[screen_number - 1]` or the cursor image.
4. `map_screenshot_point()` clamps to the image and scales to the screen's
   logical geometry, adding the screen origin.
5. A `PointTarget` is emitted; `OverlayManager.point_at()` chooses the window
   whose geometry contains the global point.
6. A pulsing ring travels to the target on a quadratic arc, a short pointer
   phrase streams beside it, it holds for 3s, then the ring fades and calls
   `Pipeline.on_point_finished()`.

## Configuration

`~/.config/heyclicky/config.json` (or `$HEYCLICKY_CONFIG_DIR`), mode 0600:

```json
{
  "llm": {
    "provider": "openrouter",
    "reasoning": "off",
    "openrouter": {"api_key": "", "base_url": "https://openrouter.ai/api/v1", "model": "nvidia/nemotron-3.5-super-vl:free"},
    "groq":   {"api_key": "", "base_url": "...", "model": "openai/gpt-oss-20b"},
    "gemini": {"api_key": "", "base_url": "...", "model": "..."},
    "ollama": {"base_url": "http://127.0.0.1:11434", "model": "qwen2.5vl:7b", "keep_alive": "10m"}
  },
  "stt":     {"provider": "auto", "language": "en", "keyterms": []},
  "tts":     {"provider": "auto", "voice": "en-US-AriaNeural", "volume": 0.9},
  "capture": {"backend": "auto", "max_dimension": 1280, "jpeg_quality": 80},
  "hotkey":  {"backend": "auto", "combo": "ctrl+alt", "mode": "auto", "fallback_combo": "ctrl+shift"},
  "audio":   {"input_device": "default", "sample_rate": 16000},
  "ui":      {"edge_glow": true, "start_minimized": false}
}
```

Environment variables win over the file: `GROQ_API_KEY`, `OPENROUTER_GEMMA_API_KEY`,
`GEMINI_API_KEY` / `GOOGLE_API_KEY`, `OLLAMA_HOST`, `HEYCLICKY_LLM_PROVIDER`,
`HEYCLICKY_TTS_PROVIDER`, `HEYCLICKY_CONFIG_DIR`.

## IPC

A `QLocalServer` named `heyclicky-<uid>` accepts one command per line:

`toggle`, `start`, `stop`, `panel`, `settings`, `capture`, `welcome`, `quit`.

The same binary acts as client (`heyclicky --toggle-listen`). This is how the
GNOME custom keybinding toggles listening without a background process holding
`/dev/input` access.

## Failure behaviour

| Failure | User sees | Exit |
| --- | --- | --- |
| No provider key | health row warning; spoken fallback after a request | app keeps running |
| Ollama down | “not reachable” row; provider error if selected | app keeps running |
| No capture backend | error in panel; nothing sent to the model | app keeps running |
| No microphone tool | error on first press | app keeps running |
| Hotkey unavailable | panel hint + manual button | app keeps running |
| No tray | panel opens and stays available via `--show-panel` | app keeps running |
| Malformed config | renamed to `.json.broken`, defaults used | app keeps running |

## Extension points

- Add a provider: implement `LLMProvider`, add it to `llm/factory.py`.
- Add an STT engine: implement `STTEngine`, add to `stt/factory.py`.
- Add a TTS engine: implement `TTSEngine`, add to `tts/factory.py`.
- Add a capture backend: subclass `CaptureBackend`, register in `BACKENDS`.
- Add a hotkey backend: subclass `HotkeyBackend`, register in `BACKEND_CLASSES`.
