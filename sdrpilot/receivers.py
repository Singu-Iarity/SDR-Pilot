"""What SDR Pilot knows about each SDRangel receiver's quirks that matter to an AI operating it - mainly the DC
spike: zero-IF (direct-conversion) receivers show a spike exactly at the tuned centre frequency (LO leakage and DC
offset in the I/Q mixer), which hides or corrupts anything sitting there. Low-IF and direct-sampling receivers
don't. Keyed by SDRangel's device-settings name (radio_device_info shows it)."""
from __future__ import annotations

ZERO_IF = "zero-IF (direct-conversion)"

# settings key -> (has a centre DC spike: True / False / None = depends, architecture, what to do)
RECEIVERS: dict[str, tuple] = {
    "hackRFInputSettings": (True, ZERO_IF + " MAX2837 + MAX5864",
                            "Small spike at the exact centre (zero-IF). Harmless for wideband signals such as broadcast "
                            "FM; dcBlock=1 removes it. Only narrow signals (a few kHz) are worth keeping ~50-100 kHz "
                            "off-centre when dcBlock is off. Leave "
                            "iqCorrection off: at low gain it can run away and pin the stream at a constant value."),
    "plutoSdrInputSettings": (True, ZERO_IF + " AD9363",
                              "Spike at the centre, reduced by the chip's DC tracking (dcBlock / hwBBDCBlock, "
                              "hwRFDCBlock). Keep signals off-centre."),
    "limeSdrInputSettings": (True, ZERO_IF + " LMS7002M",
                             "Spike at the centre after calibration drift. Use dcBlock and keep signals off-centre "
                             "(or use the NCO / lpfFIR to shift)."),
    "bladeRF1InputSettings": (True, ZERO_IF + " LMS6002D", "Spike at the centre: dcBlock=1 and tune off-centre."),
    "bladeRF2InputSettings": (True, ZERO_IF + " AD9361", "Spike at the centre (smaller, DC tracking): tune off-centre."),
    "usrpInputSettings": (True, ZERO_IF + " (B2xx AD9361; most daughterboards)",
                          "Spike at the centre on B2xx-class radios: use dcBlock and tune off-centre."),
    "xtrxInputSettings": (True, ZERO_IF + " LMS7002M", "Spike at the centre: dcBlock and tune off-centre."),
    "fcdProSettings": (True, ZERO_IF + " (FUNcube Pro, E4000)", "Spike at the centre: tune off-centre; use dcBlock."),
    "fcdProPlusSettings": (True, ZERO_IF + " (FUNcube Pro+)", "Spike at the centre: tune off-centre; use dcBlock."),
    "sdrPlayV3Settings": (None, "MSi001 tuner - zero-IF or low-IF depending on the IF setting",
                          "In zero-IF mode (ifFrequencyIndex 0) there's a centre spike - tune off-centre or pick a "
                          "low-IF setting; low-IF modes have none."),
    "sdrPlaySettings": (None, "MSi001 tuner - zero-IF or low-IF depending on the IF setting",
                        "Zero-IF mode has a centre spike (tune off-centre); low-IF modes don't."),
    "rtlSdrSettings": (None, "RTL2832U + tuner: R820T/R828D/FC0012/FC0013 are low-IF; E4000 is zero-IF",
                       "R820T-family dongles (most RTL-SDRs) have no real centre spike; E4000 ones do (offset-tune "
                       "with offsetTuning=1). Direct sampling (HF) has none."),
    "airspySettings": (False, "low-IF (R820T2) with a real ADC", "No centre DC spike."),
    "airspyHFSettings": (False, "polyphase / digital down-conversion", "No centre DC spike."),
    "kiwiSDRSettings": (False, "direct sampling (web SDR)", "No centre DC spike."),
    "perseusSettings": (False, "direct sampling", "No centre DC spike."),
    "aaroniaRTSASettings": (False, "spectrum analyser / direct sampling front end", "No centre DC spike."),
    "remoteTCPInputSettings": (None, "remote SDR - depends on the remote device", "Depends on the remote receiver."),
    "soapySDRInputSettings": (None, "SoapySDR - depends on the device", "Zero-IF devices behind Soapy (HackRF, "
                              "Lime, Pluto, bladeRF, USRP) have a centre spike; check spectrum_snapshot."),
    "testSourceSettings": (False, "SDRangel test source", "No real front end."),
}


# Nominal maximum sample rate (S/s) and native ADC resolution (bits) - what the hardware can deliver; SDRangel and
# USB/CPU may set the practical limit lower. Used to tell the AI how wide it can look and whether a >8-bit IQ
# stream is worth it.
CAPS: dict[str, tuple[float, int]] = {
    "hackRFInputSettings": (20e6, 8), "rtlSdrSettings": (3.2e6, 8), "airspySettings": (10e6, 12),
    "airspyHFSettings": (0.912e6, 18), "sdrPlayV3Settings": (10e6, 14), "sdrPlaySettings": (10e6, 12),
    "plutoSdrInputSettings": (61.44e6, 12), "limeSdrInputSettings": (61.44e6, 12),
    "bladeRF1InputSettings": (40e6, 12), "bladeRF2InputSettings": (61.44e6, 12), "usrpInputSettings": (56e6, 12),
    "xtrxInputSettings": (120e6, 12), "kiwiSDRSettings": (20e3, 16), "perseusSettings": (2e6, 16),
    "fcdProSettings": (96e3, 16), "fcdProPlusSettings": (192e3, 16),
}

# Fixed-frequency spurs that are NOT at the tuning centre. Where the cause is a known reference clock the
# harmonics are listed exactly; otherwise there's a description of what to expect.
SPURS: dict[str, dict] = {
    "rtlSdrSettings": {"clock_hz": 28.8e6, "what": "harmonics of the dongle's 28.8 MHz crystal (28.8, 57.6, 86.4, "
                       "115.2, 144.0, 172.8 MHz, ...): narrow, fixed carriers that stay put when you retune"},
    "hackRFInputSettings": {"what": "narrow fixed lines from the HackRF's own clocks and processor (users report a "
                            "harmonic series every ~200 MHz between 0.2 and 2.8 GHz, plus a few odd lines); and, with "
                            "strong inputs, spurs around strong carriers from the 8-bit ADC (keep peaks under ~35 dB "
                            "above the noise floor). With fcPos Inf/Sup and decimation the LO - and so the DC spike - "
                            "moves away from the displayed centre (see dc_spike_hz)"},
    "plutoSdrInputSettings": {"what": "narrow fixed lines from its 40 MHz reference and digital clocks at some "
                              "frequencies; LO leakage at the centre"},
    "limeSdrInputSettings": {"what": "narrow internal spurs at fixed offsets from the LO (users report e.g. +2 MHz and "
                             "-1.5 MHz on LimeSDR-USB) and lines from its 30.72 MHz reference"},
    "xtrxInputSettings": {"what": "narrow internal spurs at fixed frequencies (LMS7002M + FPGA clocks)"},
    "airspySettings": {"what": "a few weak clock-harmonic birdies; generally clean"},
    "airspyHFSettings": {"what": "a few very weak spurs (below -140 dBm/Hz) that disappear under band noise with an "
                         "antenna"},
}
GENERAL_SPURS = ("Every SDR shows some signals of its own. Tell them apart before reporting a 'transmitter': (1) a "
                 "spur from a clock harmonic stays at the SAME absolute frequency when you retune and looks like a "
                 "perfectly steady, very narrow carrier; (2) DC offset, LO leakage and IQ-imbalance images MOVE with "
                 "the tuning (an image sits mirrored around the centre from a strong real signal: centre - (f - "
                 "centre)); (3) overload makes extra copies and intermod products around strong signals that shrink "
                 "faster than the real signal when you lower the gain. spur_check(freq_hz) does this test for you.")


def caps(settings_key: str | None) -> dict | None:
    c = CAPS.get(settings_key or "")
    return {"max_sample_rate": c[0], "adc_bits": c[1]} if c else None


def known_spurs(settings_key: str | None, f_lo: float, f_hi: float, limit: int = 40) -> list[float]:
    """Exact spur frequencies in [f_lo, f_hi] where the cause is a known clock (empty when unknown)."""
    d = SPURS.get(settings_key or "") or {}
    clk = d.get("clock_hz")
    if not clk or f_hi <= f_lo:
        return []
    import math
    n0 = max(1, math.ceil(f_lo / clk))
    out = []
    n = n0
    while n * clk <= f_hi and len(out) < limit:
        out.append(round(n * clk, 1))
        n += 1
    return out


def lo_offset(settings_key: str | None, dev: dict) -> float:
    """Where the hardware LO (and so the DC spike) sits relative to the displayed centre. SDRangel's fcPos
    Inf/Sup with decimation shifts it by Fc (HackRF readme: dec 2 -> SR/4, 4 -> 3SR/8, 8 -> 5SR/16, 16 -> 11SR/32,
    32 -> 21SR/64, 64 -> 21SR/128); fcPos 2 = centred (no shift)."""
    try:
        pos = int(dev.get("fcPos", 2))
        d = int(dev.get("log2Decim", 0))
        sr = float(dev.get("devSampleRate") or 0)
    except (TypeError, ValueError):
        return 0.0
    if pos == 2 or d <= 0 or not sr:
        return 0.0
    frac = {1: 1 / 4, 2: 3 / 8, 3: 5 / 16, 4: 11 / 32, 5: 21 / 64, 6: 21 / 128}.get(d, 0.0)
    return (sr * frac) if pos == 0 else -(sr * frac)  # Inf: LO above the band; Sup: below


def info(settings_key: str | None, hw_type: str = "") -> dict | None:
    """The receiver note for the device in use, or None when SDR Pilot has nothing to say about it."""
    key = settings_key or ""
    if not key and hw_type:
        low = hw_type.lower()
        key = next((k for k in RECEIVERS if k.lower().startswith(low)), "")
    r = RECEIVERS.get(key)
    if not r:
        return None
    spike, arch, advice = r
    out = {"device": hw_type or key, "architecture": arch,
           "dc_spike_at_center": spike if spike is not None else "depends on settings",
           "advice": advice}
    c = caps(key)
    if c:
        out.update(c)
    sp = SPURS.get(key)
    out["other_spurs"] = ((sp["what"] if sp else "no specific list known for this SDR")
                          + " - spur_check(freq_hz) tells a spur from a real signal")
    return out


def has_dc_spike(settings_key: str | None, hw_type: str = "") -> bool:
    i = info(settings_key, hw_type)
    return bool(i and i["dc_spike_at_center"] is True)
