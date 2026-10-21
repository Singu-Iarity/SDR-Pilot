- **Speed warning for local models:** when a model's generation speed collapses (e.g. 25 → 4 tokens/s), the dashboard says so once, with what to do. That pattern means the model ran out of GPU memory and spilled into system RAM.
- Reasoning display reads its setting once per reply and tracks <think> tags incrementally (no per-token rescans or file reads).

- **"Python bindings ... are out of sync" fixed:** git on Windows turned the source's LF line endings into CRLF, which changes every header's fingerprint, so GNU Radio's CMake refused the module (for gr-lte, all 33 binding files). Sources are now downloaded without that conversion, and before configuring, SDR Pilot restores line endings (or updates the stored fingerprint for headers its own porter edited).

- **gr-lte (and other C++ modules) on Windows: Python bindings fixed.** castxml, which generates the bindings, emulates MSVC, so Boost took its old-MSVC preprocessor path and binding generation failed for nearly every block (only 2 of ~40 got bindings). SDR Pilot now tells castxml it has a conforming preprocessor (`_MSVC_TRADITIONAL=0`).
- **Ports first:** the installer looks for an existing GNU Radio 3.10 port across the whole fork network (the forks of the original repo too), and tries known-good ports first (gr-lte: bkerler/gr-lte@maint-3.10, which ships its bindings).
- **Broken installs are rebuilt:** `oot_install` notices when a module's blocks show up in GRC but can't be created from Python, and rebuilds it instead of answering "already installed". A flowgraph that fails this way tells the AI it's an install problem, not a flowgraph problem.

- **See the local model's reasoning:** models that think (Qwen3.x, gpt-oss, DeepSeek, Gemma 4 with thinking on) show a collapsible "thinking…" bubble in the dashboard chat, live while they think. Reasoning written as <think> tags is taken out of the reply. It's never sent back to the model. Switch: Settings → Dashboard agent.
- **Drag flowgraph blocks:** move overlapping blocks in the flowgraph view; positions are saved into the .grc (GNU Radio Companion sees them) and kept when the AI edits the flowgraph.
- **Fix for imported module examples (gr-lte and others):** block references that old GNU Radio stored as quoted text (`self."sync_ffofrac_sig"`, a SyntaxError) are fixed; modules imported earlier are re-imported automatically when the host starts.
- "Your instructions" box for the dashboard agent's system prompt.

Earlier:
Adds an "Your instructions" box to the dashboard agent's settings: text there is added to the AI's system prompt (your station, location, preferences).

Earlier in this release line:
First release with automatic updates. Install it once by hand; after that the dashboard offers new releases itself.

- Desktop app (SDR Pilot.exe): its own window, Start-menu search, pins to the taskbar as SDR Pilot
- Radio panel on the dashboard: every receiver setting the AI can change, the gain chain, who changed what
- Update banner: download & install new releases from the dashboard (SHA-256 verified)
- batch tool: several tool calls in one round-trip; task_step takes a list of steps
- Parameter aliases, clearer errors, `view` accepts plain words, gain-stage guidance for every SDRangel receiver
- Windows OOT module builds: MSVC, castxml and fmt fixes (gr-lte verified)
