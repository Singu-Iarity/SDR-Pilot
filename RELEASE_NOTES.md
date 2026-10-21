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
