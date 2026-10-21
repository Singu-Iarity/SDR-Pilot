# SDR Pilot

SDR Pilot is an MCP plugin that lets an AI (Claude, or any MCP-capable AI app or model) drive a software-defined radio through
**SDRangel** and design, build and run **GNU Radio** flowgraphs on the live IQ stream or on recordings. While it
works, a **status dashboard** shows what the AI is doing. The dashboard also works on a phone or tablet over
Tailscale, and has a chat box so you can talk to the AI from it.

```
 Claude ──MCP──▶ SDR Pilot ──REST :8091──▶ SDRangel ──▶ HackRF / PortaPack (HackRF mode) / any SDR
                    │  ◀──IQ (Remote TCP Sink :1234)──┘
                    ├──ZeroMQ :5600──▶ GNU Radio flowgraphs the AI builds (.grc, open them in GRC)
                    ├──http://127.0.0.1:8765──▶ dashboard: task, chat, live waterfall, outputs (phone via Tailscale)
                    └──MCP over HTTP :8770 (optional host) ──▶ AI apps on other devices
```

## What's new in 0.8

**Run it without the Claude app.** Double-click `SDR Pilot.bat` to start SDR Pilot and open the dashboard in its own
window. Chat with the dashboard agent there, using an API key or a local LM Studio / Ollama model. `install_app.bat`
does three things:
- adds "SDR Pilot" to the desktop and the Start menu;
- starts SDR Pilot in the background when you log in;
- restarts it if it stops.

SDRangel is started automatically when an AI needs the radio and it isn't running (`sdrangel_autostart`).

**Flowgraph builder fixes.** A local model got stuck in a loop on an RDS decoder. These are the fixes:
- **Connection notation:** `src.out0 -> fm.in0`, `fm[1]`, `src -> fm -> wav` chains and `{"from", "to"}` objects
  now work everywhere.
- **Not-yet-added blocks:** a connection to a block that isn't there yet waits for it.
- **Bad connections:** these are no longer saved, so they stopped coming back after every edit.
- **Reserved names:** a block named `fft` (or another reserved name) is renamed instead of failing.
- **Unconnected outputs:** these get a null sink. Previously an unused audio output blocked the flowgraph.
- **Parameter names:** near-misses are corrected (e.g. `path` becomes `file`).
- **Recipe arguments:** flat `params={"station": 93.3e6}` or `{"mpx_rate": ...}` are now understood.
- **RDS default:** the RDS recipe now defaults to the frequency the radio is tuned to (it was a fixed 100.1 MHz).
- **Off-band channels:** a channel outside the tuned band is refused at build time, with the numbers.
- **Repeated errors:** an AI that hits the same error three times is told to stop and change approach.

**DC spike.** The DC spike is no longer listed as a signal, so it can't be mistaken for a station.

**Decoded data, live.** Decoders send results to the new `sp_data_sink` block (or print `SPDATA {json}` lines):
- The dashboard's **Decoded data** panel shows them live:
  - RDS: a station card with PS name and RadioText.
  - ADS-B / APRS / BLE / WiFi: tables per aircraft or device.
  - Anything else: a message log.
- AIs get a compact `decoded` summary in job results instead of digging through logs.

**Flowgraph health and tidy-up.**
- Every saved flowgraph records whether it works, decoded nothing, crashed or never validated.
- Listings put working ones first and hide broken experiments, so one AI's failed attempt doesn't mislead the next.
- "tidy up" on the Flowgraph panel, and `gnuradio_prune_flowgraphs` for AIs, archive failed, unfinished, duplicate and
  superseded flowgraphs. Nothing is deleted, and each one can be restored.

**Fast, intermittent and wideband signals.**
- `record_on_signal`: triggered IQ recording when something transmits in a band, with pre-trigger samples. It stops on
  a timer or when the signal drops.
- `burst_scan`: microsecond burst timing on the live stream.
- `spectrogram`: microsecond time steps.
- `extract_channel`: cuts a narrow signal out of a wide capture.
- `analyze_capture(time_resolution_us=...)`: fine burst timing on a capture.
- Full 20 MS/s on the HackRF. SDR Pilot itself never had a rate limit, but the tune tool told AIs "2-10 MS/s is
  comfortable" and they treated that as a ceiling. That advice is gone; AIs are told to use the full rate for wide
  signals, and the Remote TCP Sink's own maximum (10 MS/s by default) is raised to match.
- A **WiFi receiver recipe** (`wifi_rx`, gr-ieee802-11) decodes beacons, SSIDs and devices. It was tested on your own
  2.4 GHz channel-6 recording.

**Digital data.** `bits_analyze` takes demodulated packets and finds:
- preamble and sync word;
- Manchester / NRZI / inverted views;
- CRC-8/16/32 at any alignment;
- which bits stay constant and which change across packets.

**IQ stream format.** `radio_stream_format` switches SDRangel's stream:
- from 8-bit RTL0 to the SDRangel protocol at 16/24/32 bits (or back);
- optional zlib compression for a remote SDRangel.

If the new format doesn't deliver samples, it switches back automatically. "SDRangel wss" isn't offered: it's the
same protocol wrapped in WebSocket + TLS for SDRangel's browser version, so it would only add overhead.

**Spurs.**
- Each SDR's known artifacts away from the centre: RTL-SDR 28.8 MHz crystal harmonics, HackRF clock lines and 8-bit
  overload spurs, LimeSDR/Pluto/XTRX internal lines.
- The DC spike's real position when fcPos/decimation moves the LO.
- `spur_check(freq)` retunes and tells a real signal from one the SDR makes itself.

**Out-of-tree modules.** `oot_search` searches conda-forge, CGRAN and GitHub; `oot_inspect` lists a module's blocks.
`oot_install` installs via conda (preferred), pip, or a CMake build. Your rule is enforced: a module's one-block
receiver (e.g. a whole `lora_rx`) is blocked in flowgraphs until the AI writes a justification, which is shown to
you. Component blocks are always allowed.

**Web search for local models.** `web_search` and `web_fetch` use DuckDuckGo or Bing, or SearXNG / Brave if you
configure them.

**The 📋 report button interrupts.**
- **Dashboard agent:** after its current tool call it must stop and report (tools disabled for that one reply), then
  it carries on.
- **Claude Desktop chats:** the request arrives as a priority item.

**Task card.** The AI is asked to update it after each reply to you and after 12 tool calls without an update. Both
are adjustable in Settings → AI context.

**Agent fixes.**
- **New chat** now really starts from an empty context: history, saved results, error counters and the old task card
  are cleared.
- **List models** shows every model LM Studio / Ollama has, loaded or not, with vision and context info, in a
  dropdown. The model box used to filter the list down to the current model.

## What's new in 0.7

- **Pictures that AIs actually use.**
  - `spectrum_snapshot` can zoom on one signal (`freq_hz`, `span_hz`) and make high-detail images
    (`detail="high"`).
  - With `annotate` (on by default), the image has a MHz grid, a labelled line through every detected signal
    ("1: 433.9213") and a DC-spike marker, so exact frequencies can be read straight off it.
  - Every image comes with a short note on what to read from it.
  - AIs are nudged to take a picture where it helps: identifying an unknown signal, an inconclusive analysis, or a
    decode that found nothing. Switch: Settings → AI context.
- **Vision detection.** The dashboard agent asks LM Studio or Ollama whether the model is a vision model and sends
  it images only then. Settings → Dashboard agent → Images: auto / always / never. Models that can't see images
  don't get the picture advice either.
- **DC spike awareness.**
  - SDR Pilot knows which receivers have a DC spike at the tuned centre: zero-IF HackRF, Pluto, Lime, bladeRF,
    USRP, XTRX and FUNcube do. Airspy, RTL-SDR with an R820T tuner, KiwiSDR and Perseus don't. For SDRplay it
    depends on the IF mode.
  - `radio_status` tells the AI about its receiver, `radio_tune` warns about the new centre, and snapshots mark it.
- **📋 report button** in the chat. It shows an instant summary of recent activity (task, tool calls, errors,
  flowgraph) and asks the AI for a short "what I'm doing" report.
- **Storage folders** (Settings → Storage): choose the workspace (chats, settings, logs) and a separate data folder
  for captures, flowgraphs, job outputs and images.
- **Cleanup.** Saved tool results of chats that ended, temp files and old analysis images (14 days by default) are
  removed automatically. Captures, flowgraphs and job outputs are never deleted. Switch: Settings → AI context.
  The agent host's saved results now survive its restarts.
- **Fixed:** spectrum traces lost their right-hand end (up to 16%) when the bin count didn't divide evenly by the
  plot width.

## Fixed in 0.6.1

- `inspect_output` now accepts a job folder or job id. It lists the files and shows the run log, and says plainly
  when a job wrote nothing but its log. Before, on Windows, a folder path failed with a misleading "Permission
  denied", and AIs retried it forever.
- Messy paths (quoted, doubled backslashes) are cleaned up.
- `gnuradio_run` and `gnuradio_job_status` say when a job produced no output files.
- Every tool now tells the AI when the exact same call has failed the same way again, so it changes approach
  instead of looping.

## What's new in 0.6

- **A task card that keeps up.**
  - The AI is reminded to update the card after a few tool calls without an update. You can switch this off and
    set the interval in Settings → AI context.
  - `task_step` can insert a step between others, delete or reorder steps, and move a step back (done → in progress
    → not started). Moving a step back reopens a finished task.
  - `task_clear` removes a cancelled task.
- **The AI tunes the receiver.**
  - It's told to adjust gains, the RF amp, filters and every other SDR setting for a cleaner signal.
  - `signal_level` (RMS and peak dBFS, clipping, a verdict and advice) comes back from `radio_status`,
    `spectrum_watch` and every gain or setting change.
- **Long replies aren't cut short.**
  - The agent's reply limit is a setting (16k tokens by default). When a long reply hits it, such as a big
    flowgraph in one call, the limit is doubled (as far as the context allows) and the reply regenerated.
  - Only replies that are stuck repeating themselves get the "smaller pieces" note.
- **Big and high-resolution screens** (Layout menu):
  - *Interface size*: auto scales up on large or high-DPI screens (130% at 2560×1440, up to 175% at 4K), or pick
    90–200%.
  - Panel titles come in small to extra-large and are larger by default.
  - Up to 4 columns, plus a *widescreen* preset.
- **Live progress for local models is easier to read.** It's larger and scales with the chat text size, and the
  prompt-reading bar shows a percentage.

## New in 0.5

- **Watch flowgraphs being built.**
  - AIs add blocks one at a time with `gnuradio_add_block`. Each new block fades in on the dashboard, unconnected
    ports pulse, and a build log lists every step with the AI's reason for it.
  - **◎ follow AI** keeps the view on whichever flowgraph the AI is building, editing or running. A line says what
    it's doing, and **⛶** gives the flowgraph the whole screen.
- **Rearrangeable dashboard.** Drag, resize and hide panels, choose presets and 1–3 columns, use compact mode, and
  change the chat text size. Idle chats fold into one chip, and any chat can be hidden.
- **Smaller prompts, nothing lost.**
  - Results are sent as compact JSON.
  - Big results are shortened with the full copy kept (`get_full_result`).
  - The dashboard agent keeps the start of its prompt identical between steps, so local servers reuse their cache.
  - The agent reads the model's context size, uses short tool descriptions on small models and compacts older
    results.
  - All of this can be switched off in Settings.
- **Better with local models (LM Studio, Ollama).**
  - Replies stream into the chat with live progress for prompt reading, thinking and writing. LM Studio's exact
    progress comes from its server log.
  - Tool calls written as text are recognised.
  - The model is nudged when it announces a step but doesn't take it.
  - It automatically recovers when the context fills up or a reply is cut off (`finish_reason: "length"`).
- **The AI tunes the receiver for a cleaner signal.**
  - It's told to adjust gains, the RF amp, the baseband filter and every other SDR setting, rather than accept a
    weak or overloaded signal.
  - `radio_status`, `spectrum_watch` and every gain or setting change report `signal_level`: RMS and peak in dBFS,
    the share of clipped samples, and a verdict with advice.
  - `spectrum_watch` adds `improve_signal` tips when the picture could be better.
- **Restarting is safe.** Restarting `start_host.bat` replaces the old agent host and the conversation carries over.
- **GNU Radio.**
  - New `adsb_receiver` recipe, built on gr-adsb.
  - Invented block names like `gr-adsb::adsb_demod` are mapped to the real ids.
  - Recipes can be asked for by description.
  - The RDS decoder locks reliably: 218–312 messages per test recording instead of 0–11.

## What the AI can do

| Area | Tools |
|---|---|
| Progress (shown on the dashboard) | `task_start`, `task_step`, `task_note`, `task_finish`, `open_dashboard` |
| Dashboard chat | `dashboard_reply`, `dashboard_listen` (messages typed on the dashboard also arrive inside every tool result) |
| Radio control (any SDR) | `radio_connect`, `radio_status`, `radio_tune`, `radio_run` (start/stop), `radio_device_info`, `radio_set_gain`, `radio_device_settings`, `radio_select_device` |
| Seeing signals | `spectrum_watch` (gap-free text waterfall + transmitter table, built from every sample), `spectrum_snapshot` (image), `band_sweep` (wider than one span), `iq_samples` (raw channelised IQ, optionally triggered on a burst) |
| Recording and analysis | `capture_iq`, `analyze_capture` (finds bursts), `analyze_signal` (modulation, deviation, baud rate, raw bits) |
| Intermittent signals | `monitor_start`, `monitor_report`, `monitor_stop` |
| GNU Radio: build | `gnuradio_info` (pass `task` to get matching decoders), `gnuradio_guide` (which blocks build which decoder), `gnuradio_search_blocks`, `gnuradio_block_info`, `gnuradio_add_block` (build block by block - you watch it grow), `gnuradio_build_flowgraph` (any blocks and connections at once, validated by GRC itself), `gnuradio_use_recipe` (tested templates), `gnuradio_import_grc`, `gnuradio_write_python` (last resort) |
| GNU Radio: reuse | `gnuradio_make_block` (your own reusable block, i.e. a GRC hierarchical block), `gnuradio_save_recipe` (save a working flowgraph as a template) |
| GNU Radio: modify | `gnuradio_edit_flowgraph`, `gnuradio_get_flowgraph`, `gnuradio_list_flowgraphs` |
| GNU Radio: run and control live | `gnuradio_run`, `gnuradio_job_status`, `gnuradio_stop`, `gnuradio_set_variable`, `gnuradio_get_variables`, `gnuradio_call_method`, `gnuradio_stream`, `gnuradio_open_in_companion` |
| Results | `inspect_output` (plots and reads WAV, float, bit, byte and IQ files), `get_full_result` (the complete version of a result that was shortened to save context) |

Flowgraphs can use two extra block types:

- **`sdr_source`** is the live radio stream.
- **`capture_source`** plays back a recording.

Either way, `samp_rate` and `center_freq` are filled in automatically. The AI can write custom Python blocks
(`epy_block`) for packet parsing. All of it is saved as normal `.grc` files you can open and edit.

SDR Pilot is **receive only**. It has no transmit tools, and it actively refuses transmit hardware:
- **Flowgraphs:** transmitter sink blocks (osmosdr/Soapy/UHD/IIO/LimeSDR/bladeRF sinks) are rejected when a
  flowgraph is built.
- **Hand-written scripts:** scripts that open a transmitter are refused.
- **SDRangel:** SDR Pilot won't control a transmit or MIMO device set.

## How the AI builds decoders

GNU Radio runs headless on the PC. You don't need GNU Radio Companion open. Everything the AI builds is a normal
`.grc` file you can open in it any time (`gnuradio_open_in_companion`).

The AI is steered to use GNU Radio rather than hand-writing DSP:
- **Hints:** these list the installed decoders, SDR Pilot blocks and recipes that match the request (gr-adsb, gr-satellites, gr-lora_sdr,
  ieee802_15_4, gr-rds, dect2, iridium). They appear in:
  - `task_start`
  - `gnuradio_info(task=...)`
  - messages from the dashboard chat
- **Cookbook:** `gnuradio_guide` (`GNURADIO_COOKBOOK.md`) tells the AI which GNU Radio blocks build each kind of
  decoder, with a signal-type → flowgraph table.
- **SDR Pilot block library:** tested receivers and demodulators, each a single block. They're installed at
  startup into `~/.grc_gnuradio`, so they also appear in GNU Radio Companion under *SDR Pilot*:

  | Block | Does |
  |---|---|
  | `sp_channel` | select and filter one channel |
  | `sp_wbfm_receiver` | broadcast FM: audio + the multiplex at 240 kS/s |
  | `sp_nbfm_receiver` | voice FM / data audio, with squelch |
  | `sp_am_receiver` | airband / AM with AGC |
  | `sp_fsk_demod`, `sp_ook_demod`, `sp_bpsk_demod` | bits from FSK / OOK / BPSK signals |
  | `sp_rds_decoder` | RDS station name, RadioText, PI, PTY (gr-rds) |
  | `sp_afsk1200_decoder` | APRS / AX.25 frames, CRC-checked |
- **Recipes:** `gnuradio_use_recipe` builds a tested template in one call:
  - `fsk_burst_decoder`: channel filter → FM discriminator → clock recovery → slicer → sync-word correlator → hex
    dump per burst
  - `ook_burst_decoder`: pulse/gap measurement with PWM/PPM/Manchester guesses, like rtl_433's analyzer
  - `fm_rds_receiver`: FM station audio to WAV plus RDS
  - `aprs_afsk1200`: APRS packets printed as `SRC>DEST,PATH:info`
  - `ble_adv_live`
  - `nbfm_to_wav`
- **Full control:** `gnuradio_build_flowgraph` and `gnuradio_edit_flowgraph` still give block-by-block,
  connection-by-connection control. Custom logic goes in an `epy_block` inside the flowgraph.
- **Hand-written scripts:** `gnuradio_write_python` is a last resort. The AI has to say why GRC can't do the job.
  The dashboard still draws these scripts: from the code before they run, and from the real connections once
  they have.

**Your own reusable blocks:** `gnuradio_make_block` turns a sub-flowgraph into a GNU Radio *hierarchical block*.
- It works like a function for flowgraphs: it has parameters and input/output ports.
- Afterwards it is a block type usable in any flowgraph.
- It appears in GNU Radio Companion's block list under *SDR Pilot*. The files live in `~/.grc_gnuradio`.

`gnuradio_save_recipe` stores a working flowgraph as a template in `workspace/library/`, so any chat or the
dashboard agent can start from it next time.

## Works with any SDR SDRangel supports

SDR Pilot talks to SDRangel, not to the radio, so any receiver SDRangel can open works the same way. That includes
HackRF/PortaPack, RTL-SDR, Airspy, Airspy HF+, SDRplay, PlutoSDR, LimeSDR, USRP, bladeRF, SoapySDR devices,
KiwiSDR, Perseus, FunCube, and SDRangel's remote and file inputs.

- `radio_device_info` lists every setting of the active SDR with its current value and SDRangel's own
  description. It also gives the device report (supported rates, gain steps, ranges), every SDR SDRangel can see,
  and its device sets.
- `radio_tune` sets the sample rate however the device expects it: a continuous rate, a rate index, or device
  rate plus software decimation. It warns if the device can only get close.
- `radio_set_gain(gain_db, auto)` maps a gain in dB onto that device's own gain stages.
- `radio_device_settings` changes any setting: bias-tee, antenna, filters, AGC, PPM, DC/IQ correction,
  transverter offset and so on. Misspelled settings are rejected with the valid list.
- `radio_select_device` switches to another SDR or another SDRangel device set.
- SDRangel can also run on another machine (a Raspberry Pi by the antenna, say). Set `sdrangel_host` in
  `sdrpilot_config.json`.

## Install (Windows + radioconda)

1. **SDRangel**: install a recent release that has the *Remote TCP Sink* channel. Its web API is on by default at
   `127.0.0.1:8091`.
2. **PortaPack H4M**:
   1. Start *HackRF mode* on the PortaPack and plug it in over USB.
   2. If Windows doesn't see it as a HackRF, install the WinUSB driver with Zadig.
   3. In SDRangel, add a HackRF receive device and press ▶.
3. Double-click **`setup_windows.bat`**. It does four things:
   1. Checks that radioconda's GNU Radio works.
   2. Installs the `mcp` Python package into radioconda.
   3. Runs the self-test (simulated signals, no hardware needed).
   4. Registers the server with the Claude desktop app.
4. Quit Claude completely (from the tray icon) and reopen it. "sdr-pilot" should now show up among its tools.

**Linux / macOS:** install radioconda (or your distro's `gnuradio`), then run `./setup_linux_mac.sh`. It does
the same four steps.

For Claude Code, use this instead of step 3.4:
`claude mcp add sdr-pilot -- "%USERPROFILE%\radioconda\python.exe" "<this folder>\run_server.py"`.

You don't have to configure SDRangel any further. On first use, SDR Pilot adds a Remote TCP Sink channel and sets
it to 127.0.0.1:1234, 8-bit, full bandwidth.

## Try it

- *"Use the SDR Pilot simulator: find the BLE advertising beacon on 2.4 GHz and decode it with a GNU Radio flowgraph."*
- *"Tune to 162.55 MHz, build an NBFM receiver and record 10 seconds of audio."*
- *"Watch 433.92 MHz for a few minutes and tell me what transmits intermittently."*
- *"Capture my key fob when I press it, then work out its modulation and bit timing."*
- *"Open the decoder you built in GNU Radio Companion."*

The dashboard opens in your browser on the first tool call (`open_dashboard` reopens it).

## The dashboard on your phone or tablet (Tailscale)

The dashboard works at phone and iPad sizes. On a phone it has a tab bar: Live, Chat, Task, Flowgraph and Runs.

1. Install [Tailscale](https://tailscale.com) on the PC and on your phone or iPad, signed in to the same account.
2. On the PC, double-click **`remote_access.bat`** and choose **1**. That runs `tailscale serve`, which publishes the
   dashboard on your tailnet over HTTPS: `https://<pc-name>.<tailnet>.ts.net/`. Only your own devices can reach
   it. If Tailscale asks you to enable HTTPS certificates, enable them, then run it again.
3. The script prints a phone link that includes your **access key**. You can also get it from Settings (⚙) →
   *Remote access* on the PC's dashboard, or by asking the AI for `open_dashboard`.
   - Open the link once on each device. The key is then remembered.
   - Treat the link like a password.
4. On iPhone, Share → **Add to Home Screen** makes it an app.

Other details:
- **Keeping it reachable:** the dashboard only exists while something runs SDR Pilot. To keep it up even when the
  Claude app is closed, use option 3 in `remote_access.bat` to start the **SDR Pilot host** at login, or run
  `start_host.bat` yourself.
- **Without HTTPS:** `python remote_setup.py direct` makes the dashboard listen on the Tailscale IP itself instead.
  Windows Firewall may then need the rule that the script prints.
- **Security:** requests from the PC itself are trusted. Every other device needs the key. The dashboard also
  rejects foreign host names (DNS rebinding). Anything that changes state needs the key in a header that other
  web pages can't send. `python remote_setup.py new-key` replaces the key.

## Talking to the AI from the dashboard

Every chat tab on the dashboard has a chat box. What it does depends on the tab.

- **An AI chat's tab** (e.g. a Claude conversation using SDR Pilot):
  - Your message is delivered inside the AI's next SDR Pilot tool result, so you can steer it mid-task from
    your phone.
  - It answers with `dashboard_reply`.
  - To talk to it only through the dashboard, tell it in the chat *"listen to the dashboard"*. It then waits on
    `dashboard_listen` and answers every message there.
  - A chat that has gone idle can't be woken from outside. That's an MCP limitation.
- **Dashboard agent** (**+ Agent** button):
  - A conversation that lives inside SDR Pilot itself, so it works with no AI chat open.
  - It uses the same tools. It runs in the SDR Pilot host on the PC.
  - **It's model-agnostic.** In Settings (⚙) → *Dashboard agent* you pick one of:
    - Anthropic (Claude)
    - OpenAI
    - Google Gemini
    - OpenRouter (any model)
    - a local model in **LM Studio** or **Ollama**
    - any other OpenAI-compatible API

    Paste a key (local models don't need one) and choose a model. *List models* asks the provider which models
    exist.
  - Usage is billed by that provider. Local models cost nothing but need tool-calling support. Qwen 3, Llama 3.1+
    and Mistral-family models work best.
  - LM Studio's server defaults to port **1234**, the same port SDRangel's Remote TCP Sink uses. Change one of
    them, e.g. LM Studio's to 1235.

## Any AI app, and other devices

SDR Pilot is a standard MCP server, so it isn't tied to Claude.

- **Same PC, any MCP app** (Claude desktop, LM Studio, Cursor, Codex, Gemini CLI, Continue, …): most apps take this
  JSON in their MCP settings (`mcp.json` / `mcpServers`):
  ```json
  {"mcpServers": {"sdr-pilot": {"command": "C:\\Users\\you\\radioconda\\python.exe",
                                "args": ["C:\\path\\to\\SDR-Pilot\\run_server.py"]}}}
  ```
- **Other devices:** the SDR Pilot host (`start_host.bat`) also serves MCP over HTTP on port 8770.
  - `remote_access.bat` option 2 publishes it on your tailnet at `https://<pc>.<tailnet>.ts.net:8443/k/<key>/mcp`.
  - Claude Code: `claude mcp add --transport http sdr-pilot <that url>`.
  - LM Studio and most other apps: `{"mcpServers": {"sdr-pilot": {"url": "<that url>"}}}`.
  - On the PC itself, `http://127.0.0.1:8770/mcp` works without a key.
- **A Claude chat started on your phone:** if the chat is linked to your PC through the Claude desktop app, the
  PC's local MCP servers, SDR Pilot included, are available to it. The PC must be on with the Claude app
  running.
- **Advanced:** `python remote_setup.py funnel-mcp` exposes the MCP endpoint on the public internet with Tailscale
  Funnel, e.g. for a claude.ai custom connector. Anyone with that URL can control the radio and run code on the
  PC, so only use it if you understand that.

## Keeping prompts small (local models)

Every step, a model re-reads its whole prompt: instructions, tool list and the conversation so far. SDR Pilot keeps
that small without throwing data away.

**For every AI** (Settings ⚙ → *AI context*):
- Tool results are sent as compact JSON, which is the same data in about 20% fewer tokens.
- Results longer than 12,000 characters are shortened, but keep their structure: the first items of long lists and
  the start of long text. The complete original is saved, and the AI can read any part of it with
  `get_full_result(id, path)`. You can turn this off or change the limit.

**For the dashboard agent** (Settings ⚙ → *Dashboard agent*):
- The start of the prompt stays identical between steps (the time goes into each message instead), so LM Studio
  and Ollama reuse their cache and only process what's new.
- *Context size* is read from LM Studio automatically (the length the model is loaded with), or you can set it
  yourself. If the tools alone wouldn't fit, the chat warns you. Load local models with a context length of 32k or
  more.
- *Tool descriptions*: with a small context, the model gets one-line descriptions plus `tool_help` for the full
  text. That saves about 4k tokens.
- *Compact older tool results*: when the context is about two-thirds full, older results become one-line stubs.
  Full copies are kept and the AI can reread them. You can turn this off.
- *Nudge*: small models often say "next I'll tune to…" and then stop. The agent notices this and tells the model to
  carry on. Tool calls that a model writes as plain text (Qwen `<tool_call>`, Llama `<|python_tag|>`, Mistral
  `[TOOL_CALLS]`, bare JSON) are recognised and run.

**Live progress and automatic recovery** (dashboard agent):
- Replies stream into the chat as they're written. A line above the input box shows what the model is doing:
  - *Reading prompt*: how many tokens are new and how many are cached, with a progress bar. The bar is exact when
    the server reports progress (llama.cpp, Ollama). LM Studio doesn't send it over its API, so SDR Pilot reads
    it from LM Studio's server log using its `lms` command-line tool (`lms log stream --source server`, LM Studio
    0.3.26+). The exact prompt and generation timings come from there too. If `lms` isn't found, the bar is
    estimated from the measured prompt speed.
  - *Thinking* or *Writing*: tokens so far and tokens per second.
- After each step, the chat header shows the prompt speed, reply speed and how full the context is.
- When a reply comes back cut off (`finish_reason: "length"`) or the server says the prompt doesn't fit, the agent
  shrinks the prompt and carries on:
  - It compacts results, moves the oldest steps out of the prompt and switches to short tool descriptions, keeping
    full copies of everything.
  - It reads the model's real context size from the error, if the error states it.
  - A reply that ran past the output limit is discarded, and the model is asked to keep it short.
  - Both behaviours can be switched off in Settings.
- Restarting `start_host.bat` replaces any older agent host, so updates take effect, and the conversation carries
  over.

## Arranging the dashboard

On a computer or iPad (wider than 900 px):
- Drag a panel by **⠿** in its title to move it between columns or to the full-width area at the bottom.
- Drag a panel's bottom edge to resize it; double-click the edge for automatic height.
- Drag the gap between columns to change their widths.
- **×** hides a panel.

The **Layout** button (next to ⚙) has presets (*chat focus*, *radio focus*, *flowgraph focus*), 1–3 columns, a
compact mode, chat text size and the list of hidden panels. The chat panel also has **A− / A+** buttons. The layout
is saved per browser. Phones keep the tab layout.

## Watching the AI build a flowgraph

AIs build new flowgraphs **one block at a time** (`gnuradio_add_block`), so you can watch the design grow on the
dashboard's *Flowgraph* tab:

- Each new block fades in with a green outline. The view re-fits itself unless you've panned or zoomed in the last
  15 s (*fit* snaps back).
- Ports that aren't connected yet are drawn hollow and pulse. The status reads *building · N blocks · K open* until
  everything is connected and valid.
- **Build steps** under the graph lists every block in order, with the AI's one-line reason for adding it. The same
  note is saved as the block's comment, so GNU Radio Companion shows it too.
- Each block is checked by GNU Radio as it's added, so mistakes (unknown block, wrong parameter name) show up at the
  step that caused them.
- Flowgraphs built in one go (recipes, `gnuradio_build_flowgraph`) are revealed block by block the first time you
  open them.
- On a phone the graph is laid out top to bottom, and the *Flowgraph* tab shows a ✎ badge while a build is in
  progress.

## Using the live stream in your own GNU Radio Companion flowgraphs

1. Ask Claude to run `gnuradio_stream` (or just open one of its flowgraphs with `gnuradio_open_in_companion`). That
   keeps the radio's IQ on ZeroMQ.
2. In GRC, add a **ZMQ SUB Source** with IO type `complex` and address `tcp://127.0.0.1:5600`. The tool's result
   gives the exact address and sample rate.
3. You can also hand Claude a `.grc` you made: `gnuradio_import_grc` brings it under SDR Pilot so it can edit,
   run and live-tune it.

## Several chats at once

Each chat runs its own copy of SDR Pilot against the same radio:
- Each copy gets its own dashboard port (8765, 8766, …). `radio_status` reports the real address.
- Captures, the monitor and live flowgraphs mark the radio as busy. `radio_tune` and `radio_run` from another
  chat then refuse to retune or stop the SDR unless you tell them to force it.
- Ended chats stay on the dashboard for a few hours. Remove one with its × or use *clear ended*.
- If the AI skips reporting task steps, they show as *?* ("not reported by the AI") instead of looking pending
  forever. The AI is also told which steps it missed.

## Simulator

`radio_connect(source="sim")` (or `"source": "sim"` in `sdrpilot_config.json`) starts a fake SDRangel with these
test signals:

| Frequency | Signal |
|---|---|
| 2402 / 2426 / 2480 MHz | BLE beacon "N1FUR-TEST" |
| 2437 MHz | Wi-Fi-like bursts |
| 433.92 MHz | OOK weather sensor, roughly every 9 s |
| 162.55 MHz | NBFM tone melody |
| 100.1 MHz | FM broadcast with stereo pilot and RDS (station "SDRPILOT", RadioText) |
| 144.39 MHz | APRS beacon (AFSK1200 AX.25), roughly every 7 s |

The AI goes through exactly the same code path as with real hardware.

## Files

- `workspace/captures/`: IQ recordings (`.cfile` complex64, plus a `.json` sidecar with frequency and rate)
- `workspace/flowgraphs/<name>/`: the `.grc`, the generated `.py` and embedded blocks
- `workspace/jobs/<run>/`: each run's working folder (output files, `run.log`)
- `recipes/`: tested flowgraph templates (FSK and OOK burst decoders, BLE advertising, NBFM to WAV) and their
  Python blocks
- `blocks/`: specs of the SDR Pilot block library (built into `~/.grc_gnuradio` at startup);
  `GNURADIO_COOKBOOK.md`: the decoder-building guide the AI reads with `gnuradio_guide`
- `workspace/library/`: recipes the AI saved. `~/.grc_gnuradio/`: reusable blocks it made.
- `sdrpilot_config.json`: ports, SDRangel address, protocol, simulator, `dashboard_bind`, agent defaults
- `workspace/state/access_key.txt`: the dashboard / MCP-over-HTTP access key. `agent.json` holds the dashboard
  agent's provider settings.

## Troubleshooting

- **"Cannot reach SDRangel API"**: SDRangel isn't running, or its API isn't on port 8091 (check
  `sdrangel_api_port`).
- **"device set is 'idle'"**: press ▶ on the device in SDRangel.
- **IQ stream too slow**: lower the sample rate (2–10 MS/s is comfortable), or check SDRangel's Remote TCP Sink
  "Max channel sample rate".
- **FLAC error**: you switched `sdrangel_protocol` to `sdra`. Tick *IQ only*, or pick *zlib*, in the Remote TCP
  Sink settings.
- **Phone can't open the dashboard**:
  1. Run `remote_access.bat` → 4 to check that Tailscale is connected and the dashboard is served.
  2. Make sure something is running SDR Pilot: the Claude app, or `start_host.bat`.
  3. A *401* or the key page means the link has no key, or an old one.
- **HackRF settings:** everything on SDRangel's HackRF panel is settable through `radio_device_settings`, the
  ▶/■ button through `radio_run`. The one exception is the *auto BBF* checkbox, which isn't in SDRangel's web API.
  `radio_tune` sets the BBF `bandwidth` itself.
- **Spike in the middle of the spectrum**: that's the HackRF's DC offset. The AI is told to tune signals off-centre.
- **Logs**, which you can send to Claude when something breaks:
  - `logs/setup_*.log`: everything setup did
  - `logs/selftest_*.log`: the self-test, including the server's output and full tracebacks
  - `workspace/logs/sdrpilot.log`: every tool call and error while Claude uses the plugin
  - `run.log` in each job folder: each flowgraph run's own output
  - `workspace/logs/host_console.log`: the SDR Pilot host's console output

Only decode signals you're entitled to receive: your own devices, broadcasts and amateur radio.

## License

MIT, see `LICENSE`.

## One-file Windows installer

`installer/SDR-Pilot-Setup-<version>.exe` installs everything for a new user, with no admin rights needed:
- SDR Pilot itself, into `%USERPROFILE%\SDR Pilot`;
- radioconda (GNU Radio for Windows), downloaded if it's missing;
- optionally SDRangel;
- the Python package SDR Pilot needs, then a self-test;
- desktop and Start-menu shortcuts plus a background service that starts at logon, so the Claude app isn't needed;
- optionally, registration with the Claude desktop app.

It uninstalls from Windows' "Installed apps" list, and asks before deleting your data.

**Download:** the newest installer is on the [Releases page](https://github.com/Singu-Iarity/SDR-Pilot/releases/latest).

## Updates

Installed copies check this repository's Releases every few hours. When a newer build is out, the dashboard shows a
banner: **Download & install** fetches the installer, checks its SHA-256, and updates SDR Pilot in place (your
settings, captures, flowgraphs and chats are kept). Nothing installs without your click. Settings (⚙) → Updates
shows the installed build and can check now; `update_repo` in `sdrpilot_config.json` changes the source (empty =
no checks).

Every build carries a stamp in `sdrpilot/BUILD` (e.g. `0.9.0.20261021.1335`). The installer, the running app and
the dashboard all compare that stamp, so an update by the installer, by copying files in, or by the dashboard's
restart button is recognised the same way, and an open dashboard reloads itself afterwards.

## Building and publishing (maintainers)

- `installer/build.sh` stamps a new build and builds the installer (`apt install nsis` on Linux).
- `installer/publish.sh owner/repo "notes"` publishes it as a GitHub Release (needs the `gh` CLI), uploading
  `SDR-Pilot-Setup-<build>.exe` with its `.sha256`. Installers without a checksum are refused by the updater.
- The desktop window (`SDR Pilot.exe`) is built from `launcher/` (Rust, tao + wry):
  `cargo build --release --target x86_64-pc-windows-gnu`.

## Contributing

Fork the repository, make your change on a branch, and open a pull request. Please keep SDR Pilot receive-only:
no transmit features. Run `python selftest.py` (simulator, no radio needed) before sending changes.
