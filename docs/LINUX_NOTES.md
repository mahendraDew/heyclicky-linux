# Linux notes

## Recommended: an Xorg session

Clicky needs two things that Wayland deliberately restricts: a global
push-to-talk shortcut with press/release events, and silent whole-screen
capture. Both work out of the box on **Ubuntu on Xorg**:

1. Log out.
2. At the login screen click the gear icon.
3. Choose **Ubuntu on Xorg**, then log in.
4. Run `heyclicky --doctor` and expect `X11 (mss)` and `evdev` (or `pynput`)
   to be green.

Wayland still works, with caveats described below.

## Hotkeys

The default shortcut is **Ctrl+Alt** (Ctrl+Alt+Space on GNOME) with
`hotkey.mode = "auto"` (hold on X11/evdev, toggle on GNOME) and
`hotkey.fallback_combo = "ctrl+shift"`.

### The Fn key is usually firmware-handled

`Fn` is not a normal key. On most laptops — including the Lenovo IdeaPad 82KT
used to develop this port — the embedded controller consumes it before the
kernel sees anything. The OS receives Super but never Fn, so no Linux shortcut
can distinguish `Fn+Super` from a plain Super press. Verify your machine:

```bash
heyclicky --doctor           # look for the fn-key line
sudo heyclicky --watch-keys  # press the chord and watch for KEY_FN
```

If `KEY_FN` appears, the evdev backend uses it like any other modifier. If it
does not, the resolver retries `hotkey.fallback_combo` (default `Ctrl+Shift`) and
records the reason in the panel and `--doctor`. Choose another chord under
**Settings → Shortcuts & capture** — `Ctrl+Alt`, `Ctrl+Shift`, `Alt+Shift` and
`Super+Shift` all work well. Avoid `Super+Space`, which GNOME uses to switch
input sources.

The Fn-lock toggle (`Fn+Esc`) only changes whether F1–F12 send media keys or
function keys; it does not make the Fn key visible to Linux.

### evdev (true push-to-talk, X11 or Wayland)

`evdev` reads `/dev/input/event*` directly, so it gets real key-down and key-up
events, including `KEY_FN` when the firmware exposes it. Access requires
membership in the `input` group:

```bash
sudo usermod -aG input "$USER"
# log out and back in, then:
heyclicky --doctor
```

Change the combo or mode in Settings or `config.json` (`hotkey.combo`,
`hotkey.mode` = `auto`/`hold`/`toggle`).

If you cannot join the `input` group, the app falls back automatically.

### pynput (X11, no extra group)

Works on Xorg without privileged device access. Press/release is emulated from
global X events. Prefer evdev when available because pynput can miss a release
if focus changes mid-recording.

### GNOME custom keybinding (Wayland, toggle)

GNOME does not expose press/release to applications. The app therefore installs
a custom keybinding made of the configured modifiers plus `Space` (with the
default `Ctrl+Alt+Space`) whose command is:

```bash
heyclicky --toggle-listen
```

One press starts recording, the next press stops it. GNOME cannot bind `Fn`, so
an Fn chord always resolves to the fallback here. The keybinding is written with
`gsettings` on first start and removed on clean shutdown; inspect it under
**Settings → Keyboard → Keyboard Shortcuts → Custom Shortcuts**.

Because the command needs to find the binary, run `./install.sh` (it symlinks
`~/.local/bin/heyclicky`) or add the repo's `.venv/bin` to `PATH`.

### Manual

Set `hotkey.backend` to `manual` to use only the panel/tray button. Useful in
nested sessions, remote desktops and automated tests.

## Screen capture

Backends are tried in this order:

1. **X11 (mss)** — fastest, silent, excludes nothing (the overlay is hidden for
   one frame instead).
2. **XDG ScreenCast (PipeWire)** — Wayland. GNOME/Plasma ask once which
   monitor(s) to share, then the app reads frames from the PipeWire stream with
   no further prompts. The permission is remembered through a restore token.
3. **xdg-desktop-portal screenshot** — Wayland. Works where the portal supports
   non-interactive screenshots (GNOME 43+); GNOME 42 only has the interactive
   screenshot UI.
4. **GNOME Shell DBus** — private API. On Wayland GNOME 42+ gates it behind the
   shell's unsafe mode and returns `AccessDenied`, so it is only attempted on
   Xorg.
5. **CLI** — `gnome-screenshot`, `grim`, `spectacle` or `scrot`.
6. **mock** — only used by tests.

Override with `capture.backend`: `auto`, `screencast`, `portal`, `x11`, `gnome`,
`cli`.

On Wayland, expect one of:

- a **“Share your screen”** dialog on the first capture — pick the monitor(s)
  Clicky should see and click **Share**; the choice is remembered;
- a compositor that returns a black image for protected windows;
- GNOME Shell denying its private DBus call (`AccessDenied`) — that is why the
  ScreenCast portal is the default on Wayland.

The ScreenCast backend needs the system PyGObject (the app borrows it from
`/usr/lib/python3/dist-packages`) and the PipeWire GStreamer plugin:

```bash
sudo apt install python3-gi gstreamer1.0-pipewire
```

Xorg avoids all of this and is still the most seamless option.

## Microphone

Capture uses `arecord` (ALSA) or `parec` (PulseAudio/PipeWire), so there is no
PortAudio dependency:

```bash
sudo apt install alsa-utils
arecord -l
```

Pick a device in Settings. The default `default` device follows the system
input; `plughw:1,0` style IDs target a specific card.

Linux does not have a per-app microphone permission prompt. If the desktop
shows a recording indicator, that is expected while you hold the shortcut.

## Text to speech

Order of preference:

1. **Edge TTS** (`edge-tts` Python package) — natural neural voices, requires
   network access to Microsoft's endpoint.
2. **Gemini TTS** — requires a Gemini API key.
3. **Piper** — fully offline, requires `piper` and a voice model path.
4. **Speech Dispatcher** (`spd-say`) — always available on most desktops,
   robotic but offline.

Playback uses `ffplay`, `paplay` or `aplay`. Install one:

```bash
sudo apt install ffmpeg
```

Disable speech entirely with `tts.enabled: false`; replies still appear in the
panel and the pointing animation still runs.

## Speech to text

- **Groq Whisper** (`whisper-large-v3-turbo`) — fast, needs a Groq key.
- **Gemini** — uses the Gemini key.
- **Local faster-whisper** — `pip install faster-whisper` and set
  `stt.provider` to `local`. The model downloads on first use.

If no key is configured and faster-whisper is absent, the app reports exactly
what to add instead of failing silently.

## OpenRouter (default)

OpenRouter proxies many models through a single OpenAI-compatible endpoint.
The app defaults to **NVIDIA Nemotron 3.5 Super VL (free)** on OpenRouter:

- provider: `openrouter`
- model: `nvidia/nemotron-3.5-super-vl:free`
- base URL: `https://openrouter.ai/api/v1`
- key: `OPENROUTER_GEMMA_API_KEY` (or `OPENROUTER_API_KEY`), set in `.env` or
  **Settings → Models → OpenRouter → API key`.

No local model download is required, but screenshots are sent to OpenRouter, so
use Ollama if you want a fully local setup.

Nemotron 3.5 Super VL accepts text, images and video, has a 1M-token context and
is free, so screen pointing works out of the box. If you only need text replies
and want the smallest/fastest option, `nvidia/nemotron-3-ultra-550b-a55b:free`
is text-only (screenshots are then dropped):

```bash
heyclicky --model nvidia/nemotron-3-ultra-550b-a55b:free
```

### Free-tier latency and empty replies

The `:free` pool is shared by every OpenRouter user. Expect five to ten second
replies, occasional empty streams and HTTP 429 when the daily quota (50 requests
without credits) runs out. The app shows the provider's reason in the chat
banner instead of failing silently.

Two settings keep replies snappy:

- `llm.reasoning = "off"` (the default) sends `reasoning: {"enabled": false}`.
  Reasoning models otherwise burn seconds - and sometimes the entire token
  budget - before producing any visible text.
- The reply streams into the chat and is spoken sentence by sentence, so audio
  starts as soon as the first sentence exists rather than after the last token.

For a fast, reliable free vision model, add a Gemini API key in
**Settings → Models → Gemini** (Google AI Studio issues free keys) and switch the
provider to Gemini. Adding $10 of OpenRouter credits also raises the free limit
to 1,000 requests/day.

## Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5vl:7b     # vision, recommended
ollama pull qwen2.5:7b       # text only, works but cannot see screenshots
```

Set the provider to **Local** in the panel. `qwen2.5vl:7b` needs roughly 6–8 GB
of VRAM/RAM; use `qwen2.5vl:3b` on smaller machines. The base URL defaults to
`http://127.0.0.1:11434` and can be overridden with `OLLAMA_HOST`.

If a text-only model is selected, screenshots are dropped and the prompt says
so, rather than failing the request.

## Tray icon

GNOME hides legacy tray icons by default. Either:

```bash
sudo apt install gnome-shell-extension-appindicator
# then enable "AppIndicator and KStatusNotifierItem Support" in Extensions
```

or use `heyclicky --show-panel` / the global shortcut. The panel now opens on
launch by default, so the tray icon is optional; tick **Start minimized** in
Settings if you want a tray-only start once the icon is visible. The icon has a
status dot (green ready, blue listening/answering, amber thinking), its menu
shows the same state, and clicking it reopens the panel. KDE, XFCE,
Cinnamon and MATE show the icon without extra packages.

## Multiple monitors and scaling

- Each display gets its own overlay window.
- The cursor screen is captured first and labeled “primary focus”.
- The model returns coordinates in screenshot pixels; the app scales them to
  the screen's logical size, so fractional scaling and different resolutions
  work as long as Qt reports the geometry correctly.
- Mixed-DPI setups are best tested on Xorg; Wayland compositors may report
  logical sizes that differ from the captured buffer.
- Wayland does not tell a client where the compositor placed its toplevel
  windows, so the panel cannot position itself reliably and a screen-wide
  overlay cannot be raised above it. When the model points at Clicky's own
  panel, the panel draws the pulsing ring inside its window instead.

## Remote / nested sessions

For SSH, VNC or a VM without a tray, run:

```bash
heyclicky --doctor
heyclicky --show-panel
```

If no display is available, capture and overlay cannot start. The doctor still
reports provider, STT and TTS health.

## Logs and diagnostics

```bash
heyclicky --doctor            # human readable
heyclicky --doctor --json     # machine readable
heyclicky --log-level debug   # verbose Qt/pipeline logs
heyclicky --capture-test      # save /tmp/heyclicky-capture-N.jpg
```

Config lives at `~/.config/heyclicky/config.json` (0600). Delete it to reset all
settings; keys are never written to logs.

## Uninstall

```bash
./uninstall.sh
rm -rf ~/.config/heyclicky
```

Also remove the GNOME custom shortcut if you added it, and `sudo gpasswd -d
"$USER" input` if you no longer want raw input access.
