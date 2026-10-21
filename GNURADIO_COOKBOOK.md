# GNU Radio cookbook for SDR Pilot

This is how to build a decoder from GNU Radio blocks instead of hand-written DSP. The AI reads it through the
`gnuradio_guide` tool. Block ids are GRC ids, which you use as `type` in `gnuradio_build_flowgraph`. Look up
exact parameters with `gnuradio_block_info`.

## 0. Start here
1. Check whether a ready-made decoder fits. `gnuradio_info(task="…")` lists installed decoders (gr-adsb,
   gr-satellites, gr-lora_sdr, ieee802_15_4, gr-rds, dect2, iridium), SDR Pilot blocks (`sp_*`) and recipes
   that match the task.
2. Find out what the signal is: `spectrum_watch` → `capture_iq` → `analyze_capture` → `analyze_signal`. That
   gives the modulation, deviation, baud rate and burst timing.
3. Build: source → **channel** → **demodulate** → **recover timing** → **decide bits** → **find frames** →
   **parse**.
4. Run it on a capture first (`capture_source`). Then switch to `sdr_source` for live use with
   `gnuradio_edit_flowgraph replace_block_types`.

## 1. Sources and sinks
| Block | Use |
|---|---|
| `sdr_source` | live radio IQ (complex, `samp_rate`, centred on `center_freq`) |
| `capture_source` | a recorded `.cfile` (sets `samp_rate` / `center_freq` from its sidecar) |
| `blocks_file_sink` | raw samples or bits to a file (`type`: complex, float, byte…) |
| `blocks_wavfile_sink` | audio to WAV (`file`, `samp_rate`, `nchan`) |
| `sp_data_sink` | **decoded output**: any decoder's messages/PDUs → SPDATA records (job summary `decoded`, dashboard Decoded view). `kind` ('rds', 'adsb', ...), `key` (field naming a device, e.g. 'icao') |
| `sp_convert` | **type converter**: any stream type → any other (`in_type`/`out_type`: complex, float, int, short, byte). complex → real: `mode` mag / mag_squared / real / imag / arg; real → complex puts the value in I; `scale` multiplies (32767 for float audio → 16-bit). Inserted automatically when you connect mismatched ports |
| `blocks_message_debug` | print raw messages/PDUs to the run log (`print`, `print_pdu` ports) - prefer `sp_data_sink` |
| `blocks_tag_debug` | print stream tags (check sync-word hits) |

## 2. SDR Pilot blocks (whole receivers/demodulators as one block)
The receiver/demod blocks take `samp_rate` and `offset` (Hz from the tuning centre, e.g. `chan_freq - center_freq`).
`sp_rds_decoder` and `sp_afsk1200_decoder` take audio/MPX instead (see their In column).

| Block | In → Out | For |
|---|---|---|
| `sp_channel` | complex → complex | select and filter one channel (`bandwidth`, `decim`) |
| `sp_wbfm_receiver` | complex → audio float (`audio_rate`), MPX float at a fixed 240 kS/s | FM broadcast; MPX (out 1) feeds `sp_rds_decoder` |
| `sp_nbfm_receiver` | complex → audio float | ham/FRS/weather/marine voice (`audio_bw` 4e3), data modems (`audio_bw` 5e3) |
| `sp_am_receiver` | complex → audio float | airband, CB, AM broadcast |
| `sp_fsk_demod` | complex → bits byte, soft float | 2-FSK/GFSK/MSK packets (`deviation`, `baud`) |
| `sp_ook_demod` | complex → bits byte, envelope | fixed-rate OOK/ASK (NRZ/Manchester) |
| `sp_bpsk_demod` | complex → bits byte, symbols | BPSK/DBPSK telemetry (`baud`, `excess_bw`) |
| `sp_rds_decoder` | MPX float (`mpx_rate` 240e3) → messages (port `rds`) | RDS station name / RadioText / PI / PTY (needs gr-rds); connect `rds:rds -> out:in` with `out` an `sp_data_sink` (kind 'rds') |
| `sp_afsk1200_decoder` | audio float (`audio_rate`) → PDUs | APRS / AX.25 packet radio (CRC-checked HDLC frames) |

Recipes that use these: `fm_rds_receiver`, `aprs_afsk1200`, `fsk_burst_decoder`, `ook_burst_decoder`,
`ble_adv_live`, `nbfm_to_wav`, `wifi_rx`, `adsb_receiver`.

A minimal FM + RDS receiver (station at `station` Hz, radio tuned within ±1 MHz):
```
blocks: src sdr_source; fm sp_wbfm_receiver {samp_rate: samp_rate, offset: "station - center_freq"};
        rds sp_rds_decoder {mpx_rate: 240e3}; out sp_data_sink {kind: "'rds'"}
connections: "src -> fm", "fm:1 -> rds", "rds:rds -> out:in"     (fm:0 = audio: a WAV sink, or left open)
```
The `offset` must be inside the band the radio delivers (±samp_rate/2) - the builder refuses otherwise.

## 3. Channel selection and rate changes
| Block | Use |
|---|---|
| `freq_xlating_fir_filter_xxx` (ccc / fcc) | shift the signal to 0 Hz, filter and decimate in one step; taps `firdes.low_pass(1, samp_rate, cutoff, transition)` |
| `low_pass_filter`, `band_pass_filter` | plain filters (`fir_filter_fff` / `ccf` types) |
| `rational_resampler_xxx` | integer interp/decim |
| `mmse_resampler_xx` | any ratio (`resamp_ratio` = in_rate / out_rate) |
| `blocks_rotator_cc` | frequency shift without filtering |
| `dc_blocker_xx` | remove DC |

Rule of thumb: decimate early to about 4–10 samples per symbol (or 2–3× the channel width).

## 4. Demodulators
| Signal | Chain |
|---|---|
| FM / FSK / GFSK / MSK | `analog_quadrature_demod_cf` with gain = rate/(2π·deviation) |
| broadcast FM audio | quad demod → `analog_fm_deemph` → low-pass (or `analog_wfm_rcv`) |
| NBFM voice | `analog_nbfm_rx`, or quad demod → low-pass |
| AM / OOK / ASK | `blocks_complex_to_mag` (envelope) → `blocks_moving_average_xx` |
| BPSK / QPSK | `analog_agc_xx` → `digital_fll_band_edge_cc` → symbol sync (cc) → `digital_costas_loop_cc` (order 2 / 4) |
| general PSK/QAM | `digital_constellation_receiver_cb`, or `digital_constellation_decoder_cb` with `digital.constellation_bpsk()` / `qpsk()` |
| AFSK (tones in audio) | `freq_xlating_fir_filter_xxx` (type fcc) to the tone centre → quad demod (Bell 202: centre 1700 Hz, ±500 Hz) |
| SSB / CW | `freq_xlating_fir_filter_xxx` → `blocks_complex_to_real` (with BFO offset) |

## 5. Timing recovery and decisions
| Block | Use |
|---|---|
| `digital_symbol_sync_xx` (ff / cc) | clock recovery. `sps` = rate/baud. TED: `TED_MUELLER_AND_MULLER` (FSK/OOK), `TED_GARDNER` (PSK). `max_dev` around 0.02 for bursts |
| `digital_clock_recovery_mm_xx` | older M&M clock recovery |
| `digital_binary_slicer_fb` | float → 0/1 bits (one per byte) |
| `digital_constellation_decoder_cb` | complex symbols → symbol indices |
| `blocks_threshold_ff` | hysteresis threshold for OOK envelopes |
| matched filter | `fir_filter_xxx` with RRC taps (`firdes.root_raised_cosine`), or a pulse shape such as `[1]*n+[-1]*n` for Manchester/biphase |

## 6. Bits to frames
| Block | Use |
|---|---|
| `digital_correlate_access_code_tag_xx` | tags the stream where a sync word / preamble matches (`access_code` as a bit string, `threshold` = allowed bit errors) |
| `digital_correlate_access_code_xx_ts` | same, but outputs tagged streams with the header/payload length |
| `digital_diff_decoder_bb` | differential decoding (DBPSK, NRZI with `digital_map_bb [1,0]`, RDS) |
| `digital_map_bb` | invert or remap bits |
| `digital_descrambler_bb`, `digital_additive_scrambler_bb` | (de)whitening with an LFSR |
| `blocks_pack_k_bits_bb`, `blocks_unpack_k_bits_bb`, `blocks_repack_bits_bb` | bits ↔ bytes |
| `digital_hdlc_deframer_bp` | HDLC flags, bit stuffing and CRC → PDUs (AX.25, many links) |
| `digital_crc32_bb`, `digital_crc_check` (GR 3.10.6+) | CRC checks |
| `pdu_tagged_stream_to_pdu`, `pdu_pdu_to_tagged_stream` | switch between streams and messages |

## 7. Parsing (the only place for custom Python)
An `epy_block` inside the flowgraph receives bits, bytes or PDUs and decodes the protocol: length fields, IDs,
checksums, text. Templates you can copy:
- `recipes/burst_bits_block.py`: bits after a sync tag, as hex
- `recipes/ble_adv_parser_block.py`: dewhiten, CRC-24, fields
- `recipes/ax25_parser_block.py`: PDUs → callsigns + info
- `recipes/ook_pulse_block.py`: pulse widths → PWM/PPM bits

Pass their code with `_source_file`. Keep filtering, demodulation and timing out of the epy block; those belong
to the blocks above.

**Output format.** Print each decoded item as one line: `print("SPDATA " + json.dumps({"kind": "mysensor",
"key_field": "id", "id": dev_id, "temp_c": t, ...}), flush=True)` (or `from sdrpilot.decoded import emit;
emit("mysensor", id=..., temp_c=...)`). SDR Pilot collects these into `decoded.jsonl`, the job summary's `decoded`
section and the dashboard's Decoded view (a table per kind when records carry a key field, else a message log).

## 8. Recipes by signal type
| Signal | Build |
|---|---|
| Key fob / 433 MHz sensor (OOK PWM) | `ook_burst_decoder` recipe |
| Fixed-rate OOK (Manchester) | `sp_ook_demod` → `correlate_access_code_tag` → parser |
| FSK sensor / meter / pager | `sp_fsk_demod` → correlate (preamble/sync) → `burst_bits_block`, or the `fsk_burst_decoder` recipe |
| BLE advertising | `ble_adv_live` recipe |
| Zigbee / 802.15.4 | ieee802_15_4 OQPSK PHY blocks (`gnuradio_search_blocks ieee802_15_4`) |
| ADS-B 1090 MHz | gr-adsb: `adsb_demod` → `adsb_decoder` (`gnuradio_search_blocks adsb`) |
| LoRa / Meshtastic | gr-lora_sdr receiver blocks (`gnuradio_search_blocks lora`) |
| APRS / packet | `aprs_afsk1200` recipe |
| FM broadcast + RDS | `fm_rds_receiver` recipe |
| Weather satellites / cubesats | gr-satellites decoders (`gnuradio_search_blocks satellites`) |
| NOAA weather radio / ham FM | `sp_nbfm_receiver` → `blocks_wavfile_sink` |
| Airband | `sp_am_receiver` → WAV |
| WiFi 802.11a/g/p | `wifi_rx` recipe (gr-ieee802-11): tune exactly to the channel centre at 20 MS/s |
| Unknown digital signal | capture → `analyze_signal` → the matching `sp_*` demod → `burst_bits_block` to look at the frames → `bits_analyze` on several packets |

## 8b. Wideband and fast signals
- Use the SDR's full rate for wide signals (HackRF 20 MS/s). For a narrow signal inside a wide capture,
  `extract_channel` it to its own small file first, then decode that - much cheaper than filtering 20 MS/s in
  every flowgraph. Live: `freq_xlating_fir_filter_xxx` with decimation right after the source.
- `burst_scan` / `analyze_capture(time_resolution_us=...)`: burst start/duration to the sample; `spectrogram`:
  microsecond time steps over a short window; `record_on_signal`: triggered IQ recording with pre-trigger.

## 9. Reuse
- `gnuradio_make_block` turns any working sub-chain into your own block (parameters plus ports).
- `gnuradio_save_recipe` stores a whole working flowgraph as a template.
- `gnuradio_set_variable` tunes a running flowgraph live: offsets, thresholds, gains.

## Port types are matched for you

Connect what you mean; SDR Pilot fixes the types the way you would in GNU Radio Companion and says so in `notes`:

- Blocks that come in several types (`*_xx` / `*_vxx` blocks such as `blocks_multiply_const_vxx`, `blocks_add_xx`,
  `blocks_file_sink`, `blocks_null_sink`...) follow what they're connected to, along a whole chain - unless you set
  their `type` yourself.
- Otherwise a converter goes in between: `sp_convert` for complex/float/int/short/byte (complex → real uses the
  magnitude: if you meant the real part, the phase or a demodulator, put that block there yourself),
  `blocks_stream_to_vector` / `blocks_vector_to_stream` when only the vector length differs (FFT blocks).

## Describe the blocks you write

The user hovers over blocks on the dashboard to see what they do. GNU Radio's blocks are described already; for a
block YOU wrote (an `epy_block`, or your own `gnuradio_make_block` hier block) give one line - what it does, what goes
in and out: a class docstring, `doc` on `gnuradio_add_block` / a `blocks` entry, or
`gnuradio_edit_flowgraph(name, set_docs={'slicer': 'FSK audio -> bits; in: float, out: bytes 0/1'})`.


## Your own variables, in flowgraphs too

`analysis_variable(action='set', name='baud', formula='9600')` saves a named quantity for every chat. A saved
variable that works out to a single number can be used by name in any block parameter (`sps` → `samp_rate/baud`)
and is added to the flowgraph automatically (said in `notes`); every number argument of every tool also takes an
expression (`center_freq + 250e3`, `434.3M`, `baud*8`). Formulas and Python variables that work on columns
(`freq_dev / 1e3`, a scipy filter of `amplitude`) show up in `plot`.

## Flowgraphs that control the radio

SDR Pilot's `sp_radio_*` blocks let a running flowgraph change the SDR itself. SDR Pilot applies each request the
same way as its radio tools (and refuses it while another chat holds the radio).

* **sp_radio_control** (message in `cmd`) - a number sets `param` to `value`, an expression of `x`:
  `param=center_freq, value='2402e6 + 2e6*x'` turns a BLE channel number into a retune. A dict
  `{'center_freq': f, 'gain': 30}`, a pair `(gain . 30)` or the string `'gain=30'` sets those directly.
  Any parameter works: `center_freq`, `sample_rate`, `gain`, `agc`, or an SDRangel device setting (`lnaGain`,
  `vgaGain`, `biasT`, `bandwidth`...). Set `update_var=center_freq` so the flowgraph's own variable follows the
  applied value; its `freq_xlating` offsets then stay right. `min_interval` rate-limits.
* **sp_radio_agc** (float in: the level or SNR in dB) keeps a fading signal strong.
  Feed it `complex_to_mag_squared -> moving_average -> nlog10_ff(10)`. Outside `target_db +/- deadband_db` it
  steps the gain by `kp x error`, waits `settle` seconds, and stays within `min_gain..max_gain`.
* **sp_radio_param_from_stream** (float in) - every `interval` seconds the stream's mean/max/min/last becomes `x`,
  and `param` is set to `value(x)`.

Expressions may use `x` and every number tool arguments know: `center_freq`, `sample_rate`, `gain`, device
settings, and saved analysis variables.

## Any frequency, burst triggers

`gnuradio_use_recipe(recipe, frequency=433.92e6)` works for any band. If the signal is outside what the radio
receives now, the radio is retuned so the signal sits just off-centre, clear of the DC spike. `gnuradio_run` does the
same for live flowgraphs. `trigger_db=12` inserts a power squelch with the gate on after the channel filter. The
decoder then idles until a burst rises 12 dB above the channel's measured noise floor. The threshold is the
`trigger_level_db` variable, which you can change live.

For one-off bursts you don't have a recipe for, use `demodulate_burst` (OOK PWM/PPM/Manchester/NRZ, 2-FSK
NRZ/Manchester). It returns bits, hex, symbol rate and repeated messages.
