# Reverse engineering report: HeyClicky → heyclicky-linux

This document records how the original macOS app was analysed, what it does,
and which evidence each claim rests on. It is both an engineering record and
the map used by this port.

- **Upstream:** <https://github.com/farzaa/clicky> (MIT)
- **Commit analysed:** `e71105c`
- **Product page:** <https://www.heyclicky.com/>
- **Method:** static source analysis of the shipped repository (Swift app +
  TypeScript Cloudflare Worker), followed by black-box checks of the live site
  copy and Linux environment probes.
- **Target:** native Linux/Ubuntu recreation with Python 3.10 + PyQt6, using
  OpenRouter (NVIDIA Nemotron 3.5 Super VL free), Groq, Gemini or a local Ollama
  model instead of the hosted Claude proxy.

## 1. Why source analysis instead of the native REA engine

The REA skill routes by artifact type. This target ships a complete, readable
Swift source tree and a TypeScript worker under the MIT licence, so native
binary decompilation (Hopper/Ghidra/IDA/JADX) adds nothing: every control flow,
prompt and constant is already in source. The routing decision was therefore
"source-repository analysis", not "native reverse engineering".

The REA agent environment was checked anyway. `npx rea-agents@6.8.0 doctor
--client opencode --json` reported:

- Node `22.23.2` is below the required `22.23.4`;
- configuration drift in the OpenCode client config;
- no Hopper, Ghidra, IDA, JADX or Kuna installations.

None of those tools are needed for a source-level port. No REA MCP tools were
registered in the session, so the analysis used the cloned source, `grep`,
`sed` and the product page.

### Evidence legend

- `CM:line` = `leanring-buddy/CompanionManager.swift`
- `OW:line` = `leanring-buddy/OverlayWindow.swift`
- `CP:line` = `leanring-buddy/CompanionPanelView.swift`
- `CSC:line` = `leanring-buddy/CompanionScreenCaptureUtility.swift`
- `DS:line` = `leanring-buddy/DesignSystem.swift`
- `WK:line` = `worker/src/index.ts`
- `CA:line` = `leanring-buddy/ClaudeAPI.swift`
- `TTS:line` = `leanring-buddy/ElevenLabsTTSClient.swift`

`leanring-buddy` is the upstream folder name, typo included.

## 2. Repository map

| Path | Responsibility |
| --- | --- |
| `leanring-buddy/CompanionManager.swift` | Central state machine, voice pipeline, prompt, point parsing, history, onboarding |
| `leanring-buddy/OverlayWindow.swift` | One transparent overlay per display; triangle/waveform/spinner/bubble, flight animation |
| `leanring-buddy/CompanionPanelView.swift` | Menu-bar popover: status, permissions, model picker, feedback, quit |
| `leanring-buddy/CompanionScreenCaptureUtility.swift` | ScreenCaptureKit multi-monitor capture and image labels |
| `leanring-buddy/BuddyDictationManager.swift` | Push-to-talk recording, VAD, transcription |
| `leanring-buddy/GlobalPushToTalkShortcutMonitor.swift` | Global Control+Option press/release events |
| `leanring-buddy/ClaudeAPI.swift` | Streaming SSE client for the worker `/chat` route |
| `leanring-buddy/ElevenLabsTTSClient.swift` | Streaming TTS playback from `/tts` |
| `leanring-buddy/DesignSystem.swift` | Colours, radii, typography |
| `worker/src/index.ts` | Cloudflare Worker proxy: `/chat`, `/tts`, `/transcribe-token` |
| `AGENTS.md` | Upstream architecture summary |

## 3. Observed behaviour (evidence-backed)

### 3.1 App shape

- Menu-bar-only app with a 320pt popover (`CP:80`), dark near-black surface
  (`DS:28`, `#101211`), blue accent (`DS:96`, `#2563EB`) and a separate overlay
  cursor blue (`DS:144`, `#3380FF`).
- Status dot colour is derived from overlay visibility and voice state
  (`CP:728`); the panel hint is literally “Hold Control+Option to talk.”
  (`CP:130`).
- The panel contains permission rows, a model picker (Sonnet/Opus), a feedback
  card and a quit row (`CP:80`–`CP:761`).
- Onboarding is a video + scripted demo interaction (`CM:829`–`CM:1000`), not a
  separate window.

### 3.2 Voice state machine

Four states: `idle`, `listening`, `processing`, `responding` (`CM:17`–`CM:22`).
Transitions are driven by dictation flags (`CM:430`–`CM:463`):

| Event | State |
| --- | --- |
| Hotkey pressed | overlay shown, current response/TTS cancelled, `.processing` until recording starts |
| Recording active | `.listening` |
| Key released / transcript finalising | `.processing` |
| LLM response + TTS starts | `.responding` |
| Pointing target found | `.idle` before the flight (so the triangle is visible) |
| Pipeline done | `.idle`, then transient hide if enabled |

### 3.3 Response pipeline

`sendTranscriptToClaudeWithScreenshot` (`CM:586`–`CM:727`):

1. Cancel the previous response task and TTS (`CM:586`–`CM:588`).
2. Stay in `.processing` (spinner; no streaming text is displayed).
3. Capture every screen (`CM:596`).
4. Build labels containing actual screenshot pixel sizes (`CM:600`–`CM:607`).
5. Pass the last ten exchanges as history (`CM:609`–`CM:616`, `CM:692`–`CM:693`).
6. Stream the answer (`CM:613`–`CM:621`); `max_tokens: 1024` (`CA:143`–`CA:146`).
7. Parse the final `[POINT:…]` tag (`CM:626`).
8. Resolve the target screen, scale screenshot pixels to display points, convert
   to AppKit bottom-left global coordinates (`CM:636`–`CM:671`).
9. Append the tag-stripped reply to history (`CM:686`–`CM:693`).
10. Play TTS and switch to `.responding` once playback starts (`CM:700`–`CM:721`).
11. Return to `.idle` and schedule the transient hide (`CM:723`–`CM:727`).

### 3.4 Prompt contract

The system prompt (`CM:544`–`CM:577`) defines Clicky’s voice:

- lowercase, casual, speech-first, no markdown/lists/emojis;
- one or two sentences by default, unlimited depth when asked;
- reference visible screen content when relevant;
- never say “simply” or “just”;
- end with a “seed” rather than a yes/no question when natural;
- point whenever it helps, with `[POINT:x,y:label]` in screenshot pixel space,
  top-left origin;
- optional `:screenN` when the target is not on the cursor screen;
- `[POINT:none]` when pointing would not help.

The onboarding demo has a stricter prompt: one specific central element, a
3–6 word reaction, coordinates only between 20–80% of the image
(`CM:950`–`CM:970`).

### 3.5 Point parsing and coordinate conversion

The regex (`CM:784`–`CM:826`) is:

```text
\[POINT:(?:none|(\d+)\s*,\s*(\d+)(?::([^\]:\s][^\]:]*?))?(?::screen(\d+))?)\]\s*$
```

Conversion (`CM:640`–`CM:668`):

1. Clamp the model coordinate to `screenshotWidth/Height`.
2. Scale to display points: `x * displayWidth / screenshotWidth`.
3. Flip Y for AppKit: `appKitY = displayHeight - displayLocalY`.
4. Add the display frame origin.

Qt uses a top-left origin, so this port keeps step 1–2 and drops step 3
(`geometry.map_screenshot_point`).

### 3.6 Screen capture

- Every connected display is captured, not just the cursor screen
  (`CSC:30`–`CSC:120`).
- The cursor screen is sorted first so “screen 1” is the primary focus
  (`CSC:58`–`CSC:72`).
- Images are resized so the largest dimension is 1280px (`CSC:84`–`CSC:92`) and
  encoded as JPEG quality 0.8 (`CSC:100`).
- Labels include dimensions and role (`CSC:104`–`CSC:110`).
- The app’s own windows are excluded via `SCContentFilter`
  (`CSC:50`–`CSC:54`). Linux cannot exclude arbitrary windows in a full-screen
  grab, so this port hides its overlay for one frame instead.

### 3.7 Overlay animation

Evidence in `OW`:

- Triangle is equilateral (`OW:60`), default 16pt, trailing cursor by
  `+35,+25` (`OW:123`).
- Spring follow: response 0.2s, damping 0.6 (`OW:210`).
- Flight duration `min(max(distance / 800, 0.6), 1.4)` (`OW:510`).
- Quadratic Bezier with `arcHeight = min(distance * 0.2, 80)` (`OW:521`–`OW:522`).
- Smoothstep `3t² - 2t³` (`OW:540`).
- Apex scale ~1.3x (`OW:564`).
- Point bubble holds for 3 seconds (`OW:592`).
- Waveform replaces the triangle while listening (`OW:325`–`OW:336`);
  spinner while processing (`OW:766`).
- Overlay windows are click-through and never take focus.

### 3.8 Transient hide

When “Show Clicky” is off, the overlay appears only for an interaction. After
TTS stops, the pointing animation returns, and one extra second passes, the
overlay fades out (`CM:732`–`CM:757`). A new press cancels the pending hide
(`CM:480`–`CM:482`).

### 3.9 Worker API and provider substitution

The worker exposes (`WK:8`–`WK:36`):

- `POST /chat` → Anthropic Messages API, SSE passthrough;
- `POST /tts` → ElevenLabs streaming audio;
- `POST /transcribe-token` → short-lived AssemblyAI token.

Keys live server-side; the desktop binary never holds them. A local app cannot
ship a shared secret, so this port removes the proxy and talks directly to the
chosen provider. The normalized event shape (text deltas) is preserved.

## 4. Inferences (not directly observable in source)

- The exact Claude model alias is a moving target (`claude-sonnet-4-6` in the
  picker, `CA:17`). The port exposes provider/model fields instead of hardcoding
  aliases that may disappear.
- The worker’s rate limits and retry policy are not in the client; the port adds
  clear HTTP errors and a spoken fallback.
- The onboarding video URL (`CM:829`) is product-specific; the port replaces it
  with a text greeting and an optional spoken welcome.
- Analytics (PostHog) and email collection are product-growth features, not
  companion functionality. They are omitted; no telemetry is sent.

## 5. Unknowns / not verifiable from this checkout

- Exact VAD parameters inside `BuddyDictationManager` (the file is present, but
  the port uses a simpler “release to stop” model plus a 0.35s minimum).
- AssemblyAI streaming token configuration used in production.
- macOS TCC permission edge cases; the port replaces them with Linux checks.
- Whether the hosted product uses additional server-side prompt hardening.

## 6. macOS → Linux differences

| Concern | macOS original | Linux port |
| --- | --- | --- |
| UI shell | Menu-bar extra + NSPanel | `QSystemTrayIcon` + frameless `QWidget` |
| Global shortcut | CGEventTap, press/release | evdev (Fn-aware, hold or toggle), pynput on X11, GNOME custom keybinding toggle, panel fallback |
| Screen capture | ScreenCaptureKit, excludes own windows | mss/X11, ScreenCast portal (PipeWire) or xdg screenshot portal on Wayland, GNOME DBus, CLI, mock; overlay hidden for one frame |
| Coordinates | AppKit bottom-left | Qt top-left (no Y flip) |
| LLM | Claude via Cloudflare Worker | OpenRouter (default), Groq, Gemini, Ollama direct |
| STT | AssemblyAI streaming token | Groq Whisper, Gemini audio, optional faster-whisper |
| TTS | ElevenLabs | Edge TTS, Gemini TTS, Piper, Speech Dispatcher |
| Storage | UserDefaults | `~/.config/heyclicky/config.json` (0600) |
| Onboarding | Video + demo flight | Text card + welcome flight |
| Analytics | PostHog | none |

## 7. Feature parity checklist

Implemented:

- push-to-talk recording with live waveform level;
- multi-monitor capture, cursor-screen ordering, 1280px JPEG, labels;
- streaming replies from four providers;
- exact `[POINT:x,y:label:screenN]` grammar and coordinate mapping;
- flying triangle, random pointer bubbles, 3s hold, return flight;
- spinner while thinking, speaking state;
- ten-exchange memory with tags stripped;
- cancellation when a new utterance starts;
- transient overlay hide;
- tray panel, provider/model switch, health checks;
- settings for keys, models, STT/TTS, audio, hotkey, capture;
- single-instance IPC and CLI controls;
- doctor and capture-test diagnostics;
- tests for parsing, geometry, config, payloads, hotkey and the pipeline.

Deliberately different / not ported:

- onboarding video and music;
- email capture, PostHog analytics, DM/feedback link;
- macOS-specific permission prompts;
- paid hosted proxy with hidden keys.

## 8. Privacy notes

The original promise is “only sees your screen when you press the hotkey.”
This port keeps that: capture happens only after a non-empty transcript, the
capture is sent only to the selected provider, and no telemetry is sent. Ollama
keeps both audio and screenshots local (OpenRouter/Groq/Gemini are used for STT
by default unless `stt.provider` is `local`). API keys are stored in a 0600 JSON
file or read from environment variables (or a `.env` file next to the repo).

## 9. Licence and attribution

Upstream is MIT licensed. This port is unofficial and MIT licensed; the prompt,
design tokens and interaction model are derived from the upstream source. See
`LICENSE`.
