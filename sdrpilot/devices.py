"""SDR-agnostic device handling for SDRangel.

SDRangel exposes every SDR through the same REST endpoints, but each device type has its own settings block
(hackRFInputSettings, rtlSdrSettings, airspySettings, ...) with its own field names and units.
device_catalog.json is generated from SDRangel's own API definitions (swagger) and documents every receive
device's settings and report fields; the helpers here turn generic requests (sample rate, gain, bandwidth)
into the right fields for whatever SDR is in use, and fall back to clear guidance when there is no mapping.
"""
from __future__ import annotations

import json
import math
import os
import re

_CAT: dict | None = None


def catalog() -> dict:
    global _CAT
    if _CAT is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_catalog.json")
        try:
            with open(p, encoding="utf-8") as f:
                _CAT = json.load(f)
        except (OSError, ValueError):
            _CAT = {}
    return _CAT


# plain-language names (dashboard) and descriptions (AI) for the settings that matter most; the catalog's own text
# (from SDRangel's API definitions) is used for everything else
FRIENDLY: dict[str, dict[str, tuple[str, str]]] = {
    "hackRFInputSettings": {
        "lnaExt": ("RF amp", "front-end RF amplifier right after the antenna (stage 1 of 3): 0 = off, 1 = on (+14 dB, "
                             "fixed). Helps weak signals most, but is the first stage to overload on strong ones. "
                             "Shown on a PortaPack as the amplifier before the first mixer."),
        "lnaGain": ("LNA", "MAX2837 LNA after the RF mixer, before the baseband filter (stage 2 of 3): 0-40 dB in 8 "
                           "dB steps. Front-end-like: raise this before VGA for weak signals."),
        "vgaGain": ("VGA", "baseband variable-gain amplifier after the baseband filter (stage 3 of 3): 0-62 dB in 2 dB "
                           "steps. Amplifies signal and noise alike - sets the level reaching the ADC, no SNR gain."),
        "bandwidth": ("Baseband filter", "analog baseband (anti-alias) filter in Hz - narrow it to cut strong neighbours"),
        "biasT": ("Bias-T", "DC power on the antenna port for a powered LNA/antenna: ONLY if the user says one is attached"),
        "LOppmTenths": ("LO correction", "local-oscillator correction in tenths of a ppm (calibrates the frequency)"),
        "log2Decim": ("Decimation", "software decimation: output rate = device rate / 2^log2Decim"),
        "fcPos": ("Fc position", "where the LO sits when decimating: 0 = infradyne, 1 = supradyne, 2 = centred"),
        "devSampleRate": ("Device sample rate", "ADC sample rate in S/s before decimation"),
        "dcBlock": ("DC block", "removes the centre (DC) spike"),
        "iqCorrection": ("IQ correction", "automatic IQ imbalance correction (removes mirror images)"),
        "transverterMode": ("Transverter", "frequency offset for an external up/down converter"),
        "transverterDeltaFrequency": ("Transverter offset", "Hz added for an external converter"),
        "iqOrder": ("IQ order", "1 = IQ, 0 = QI (swap)"),
        "centerFrequency": ("Centre frequency", "requested tuning frequency in Hz"),
    },
    "rtlSdrSettings": {
        "gain": ("Tuner gain", "tuner gain in tenths of a dB (e.g. 496 = 49.6 dB)"),
        "agc": ("RTL AGC", "the RTL2832's digital AGC"), "lowSampleRate": ("Direct sampling rate", "low sample rate mode"),
        "biasTee": ("Bias-T", "DC power on the antenna port: ONLY if the user says a powered LNA/antenna is attached"),
        "noModMode": ("Direct sampling", "HF direct sampling mode"), "loPpmCorrection": ("PPM correction", "frequency calibration in ppm"),
        "rfBandwidth": ("RF bandwidth", "tuner IF filter bandwidth in Hz"),
    },
    "airspyHFSettings": {
        "attenuatorSteps": ("Attenuator", "attenuation in 6 dB steps, before everything - for very strong signals"),
        "useLNA": ("Preamp", "front-end LNA on/off"), "useAGC": ("AGC", "the HF+'s own AGC"),
        "agcHigh": ("AGC threshold", "1 = high threshold, 0 = low"),
    },
    "sdrPlayV3Settings": {
        "lnaIndex": ("LNA state", "front-end gain REDUCTION state: 0 = most gain, higher = more attenuation"),
        "ifGain": ("IF gain reduction", "IF stage gain reduction (level only)"), "ifAGC": ("IF AGC", "the RSP's IF AGC"),
    },
    "limeSdrInputSettings": {
        "gainMode": ("Gain mode", "0 = auto (spread 'gain'), 1 = manual stages"), "gain": ("Gain", "total gain in dB (auto mode)"),
        "lnaGain": ("LNA", "RF LNA - the front end"), "tiaGain": ("TIA", "trans-impedance amp after the mixer"),
        "pgaGain": ("PGA", "programmable gain after the baseband filter (level only)"),
    },
    "xtrxInputSettings": {
        "gainMode": ("Gain mode", "auto (spread 'gain') or manual stages"), "gain": ("Gain", "total gain in dB (auto mode)"),
        "lnaGain": ("LNA", "RF LNA - the front end"), "tiaGain": ("TIA", "trans-impedance amp after the mixer"),
        "pgaGain": ("PGA", "programmable gain after the baseband filter (level only)"),
    },
    "bladeRF1InputSettings": {
        "lnaGain": ("LNA", "RF LNA - the front end"), "vga1": ("VGA1", "RX VGA1, after the mixer"),
        "vga2": ("VGA2", "RX VGA2, baseband after the filter (level only)"),
    },
    "fcdProPlusSettings": {
        "lnaGain": ("LNA", "front-end LNA on/off"), "mixGain": ("Mixer amp", "mixer amplifier on/off"),
        "ifGain": ("IF gain", "IF gain 0-59 dB (level only)"),
    },
    "perseusSettings": {
        "attenuator": ("Attenuator", "0/10/20/30 dB before the ADC"), "adcPreamp": ("ADC preamp", "the only gain stage"),
        "adcDither": ("ADC dither", "fewer ADC spurs, slightly more noise"),
    },
    "plutoSdrInputSettings": {
        "gain": ("Gain", "hardware gain in dB, spread over the AD9361's stages"),
        "gainMode": ("Gain mode", "manual / slow attack / fast attack / hybrid AGC"),
    },
    "airspySettings": {
        "lnaGain": ("LNA", "front-end LNA gain step 0-14"), "mixerGain": ("Mixer", "mixer gain step 0-15"),
        "vgaGain": ("VGA", "IF VGA gain step 0-15 (level only)"),
        "lnaAGC": ("LNA AGC", "automatic LNA gain"), "mixerAGC": ("Mixer AGC", "automatic mixer gain"),
        "biasT": ("Bias-T", "DC power on the antenna port: ONLY if the user says a powered LNA/antenna is attached"),
    },
}


# ---------------------------------------------------------------- how gain stages work (told to the AI)
GAIN_PRINCIPLES = (
    "Every amplifier multiplies everything at its input - the wanted signal AND the noise that arrived with it - by "
    "the same factor; no stage can raise the signal without raising that noise. Stages differ in (a) how much NEW "
    "noise they add and (b) where they sit relative to the filters. FRONT-END gain (RF amp / LNA, right after the "
    "antenna) is low-noise and comes first, so it lifts the signal above the noise that LATER stages (mixer, IF/"
    "baseband amps, ADC) will add - this is the gain that improves SNR and lets weak signals out of the receiver's "
    "own noise. It only helps until the antenna/band noise is what you see: when one more step of front-end gain "
    "raises the noise floor about as much as the signal, the front end has done its job, and more only costs "
    "headroom. Front-end stages also see EVERY signal the antenna picks up, before the narrow filters, so they are "
    "the first to overload (spurs, ghost signals, raised floor) when strong stations are nearby. IF/BASEBAND gain "
    "(VGA/PGA, after the baseband filter) amplifies signal and all the noise already there equally - it does not "
    "improve SNR (beyond keeping the ADC's own quantisation noise small); its job is to set the level into the ADC: "
    "high enough to use the ADC's bits, never clipping. Because it sits after the filter, strong out-of-band signals "
    "don't overload it. Recipe: front-end gain up until the noise floor starts rising with it (or until strong "
    "signals clip / create spurs), then IF/baseband gain to put the level around -25..-10 dBFS rms with no clipping "
    "(signal_level). Overload or spurs: lower the front end (or add attenuation) FIRST. Attenuators do the reverse "
    "of front-end gain: they cut strong signals but also worsen sensitivity. AGC is fine for listening; for "
    "measurements, bursts and decoding, fixed gain is more predictable. On HF (below ~10 MHz) band noise is usually "
    "far above the receiver's own noise, so little front-end gain is needed there.")

GAIN_GUIDE: dict[str, str] = {
    "hackRFInputSettings":
        "HackRF, in signal order: lnaExt = RF amp right after the antenna (0/1, +14 dB fixed; the front end - helps "
        "weak signals most, overloads first; off when strong signals are around, never needed on HF broadcast "
        "bands) -> RF mixer -> lnaGain = MAX2837 LNA 0-40 dB in 8 dB steps (still before the baseband filter: "
        "front-end-like, raise it before VGA) -> baseband filter 'bandwidth' -> vgaGain = baseband VGA 0-62 dB in 2 "
        "dB steps (sets the ADC level; no SNR gain) -> 8-bit ADC. Typical start: amp off, LNA 16-24, VGA 16-24. "
        "The 8-bit ADC leaves little headroom: keep rms around -25..-15 dBFS.",
    "rtlSdrSettings":
        "RTL-SDR (R820T/R828D tuner): one 'gain' in tenths of a dB (e.g. 496 = 49.6 dB) that the tuner spreads "
        "over its internal LNA, mixer and IF VGA from a fixed table - front-end gain grows with it. 'agc' is the "
        "RTL2832U's DIGITAL AGC: it only scales samples after the ADC (no SNR benefit, can pump) - usually leave it "
        "off. Tuner AGC is used when no manual gain is set. Typical: 30-40 dB; back off near FM broadcast or pager "
        "transmitters. Bias-T (biasTee) powers an external LNA - only if the user says one is attached; an external "
        "LNA then becomes the front end, so the dongle's own gain can come down.",
    "airspySettings":
        "Airspy R2/Mini, in order: lnaGain 0-14 (LNA, the front end) -> mixerGain 0-15 -> vgaGain 0-15 (IF VGA, "
        "level only). lnaAGC / mixerAGC automate the first two. Airspy's own 'sensitivity' presets favour LNA+mixer; "
        "'linearity' presets keep LNA/mixer lower and use more VGA - use the linearity approach near strong "
        "stations. Values are step indexes, not dB.",
    "airspyHFSettings":
        "Airspy HF+ (Discovery/Dual): attenuatorSteps (0-8 x 6 dB, before everything - for very strong signals) -> "
        "useLNA (front-end preamp on/off, mainly useful above ~10 MHz and on VHF where band noise is low) -> "
        "useAGC/agcHigh (the HF+'s own AGC; agcHigh = higher threshold). Its front end is very linear; on lower HF "
        "the band noise usually dominates, so leave the LNA off there.",
    "sdrPlayV3Settings":
        "SDRplay RSP (API v3), counted as gain REDUCTION: lnaIndex = LNA state (0 = least attenuation = most "
        "front-end gain; higher states = more attenuation; the number of states depends on the model and band), or "
        "lnaGain in dB as an alternative; ifGain = IF gain reduction (larger reduction = less IF gain - IF stage, "
        "level only); ifAGC = the RSP's IF AGC. Raise front-end gain by LOWERING lnaIndex; change in small steps and "
        "watch signal_level.",
    "sdrPlaySettings":
        "SDRplay (API v2): tunerGainMode 1 = one table gain 'tunerGain' spread over the stages; 0 = manual: lnaOn "
        "(front-end LNA), mixerAmpOn (mixer amp), basebandGain (baseband, level only).",
    "plutoSdrInputSettings":
        "PlutoSDR (AD9361): one hardware 'gain' in dB, which the chip spreads over its LNA, mixer, TIA, filter and "
        "VGA from its gain table (front end first). gainMode: manual, or AGC (slow attack for steady signals, fast "
        "attack for bursts, hybrid). For burst capture and measurements use manual. lpfFIRGain is the digital FIR "
        "filter gain (scaling only).",
    "limeSdrInputSettings":
        "LimeSDR (LMS7002M): gainMode 0 = auto, 'gain' (dB) spread optimally; gainMode 1 = manual stages in order: "
        "lnaGain (RF LNA, the front end) -> tiaGain (trans-impedance amp after the mixer, 1-3) -> pgaGain "
        "(programmable gain after the baseband filter - the VGA role: level, no SNR gain).",
    "xtrxInputSettings":
        "XTRX (LMS7002M, same as LimeSDR): gainMode auto uses 'gain'; manual: lnaGain (front end) -> tiaGain -> "
        "pgaGain (after the baseband filter, level only).",
    "bladeRF1InputSettings":
        "bladeRF 1: lnaGain (RF LNA, the front end: bypass/mid/max) -> vga1 (RX VGA1, after the mixer, before the "
        "filter) -> vga2 (RX VGA2, baseband after the filter, level only). Raise LNA, then VGA1; use VGA2 last.",
    "bladeRF2InputSettings":
        "bladeRF 2.0 (AD9361): one 'globalGain' spread by the chip's gain table; gainMode manual or AGC (slow/fast "
        "attack, hybrid) - like PlutoSDR.",
    "usrpInputSettings":
        "USRP: one 'gain' in dB that UHD spreads over the daughterboard's stages (front end first); gainMode 0 = "
        "auto, 1 = manual.",
    "fcdProPlusSettings":
        "FUNcube Pro+: lnaGain (front-end LNA on/off) -> mixGain (mixer amp on/off) -> ifGain 0-59 dB (IF, level "
        "only). Turn the LNA on for weak VHF/UHF signals, off near strong ones.",
    "fcdProSettings":
        "FUNcube Pro: lnaGainIndex (+ lnaEnhanceIndex) = front end; mixerGainIndex; gain1..gain6Index = baseband IF "
        "stages (level only). Raise LNA first.",
    "perseusSettings":
        "Perseus (direct-sampling HF): attenuator 0/10/20/30 dB before the ADC (for strong signals), adcPreamp "
        "(ADC preamp, the only gain - mostly useful on the quieter upper HF bands), adcDither (randomises ADC "
        "errors: fewer spurs, slightly more noise - usually on).",
    "metisMISOSettings":
        "HPSDR / Metis (Hermes, Red Pitaya): preamp per receiver (front end) - direct sampling, so little gain is "
        "needed on HF where band noise dominates.",
    "kiwiSDRSettings":
        "KiwiSDR (remote): the receiver's RF gain is set by its owner. useAGC and 'gain' (when AGC is off) only "
        "scale the stream you receive - level, not SNR.",
    "soapySDRInputSettings":
        "SoapySDR device: 'globalGain' is spread over the stages by the driver; 'individualGains' lists the named "
        "stages - names like LNA / RF / AMP are front-end gain, IF / VGA / PGA / BB / TIA are later stages (level "
        "mostly). autoGain = the device's AGC.",
    "remoteTCPInputSettings":
        "Remote TCP (rtl_tcp-style) input: 'gain' is the remote receiver's tuner gain (as for an RTL-SDR, tenths "
        "of a dB), 'agc' its digital AGC; 'channelGain' only scales the stream.",
    "androidSDRDriverInputSettings":
        "Android SDR driver: 'gain' is the dongle's tuner gain (RTL-SDR style), 'agc' its AGC.",
    "audioInputSettings":
        "Audio input (sound card): 'volume' only scales samples - the RF gain lives in whatever radio feeds the "
        "sound card.",
    "audioCATSISOSettings":
        "Transceiver via sound card + CAT: 'rxVolume' only scales the audio/IQ - preamp, attenuator and RF gain "
        "are the transceiver's own (set on the radio).",
}


def gain_guide(settings_key: str) -> dict:
    """What the AI needs to use this SDR's gain stages well."""
    g = GAIN_GUIDE.get(settings_key)
    return {"principles": GAIN_PRINCIPLES,
            "this_device": g or ("No specific notes for this SDR: front-end/RF/LNA-type gains improve weak-signal "
                                 "SNR (and overload first); IF/baseband/VGA-type gains set the level only.")}


# the gain stages in signal order, for the dashboard's Radio panel
GAIN_CHAIN: dict[str, list[str]] = {
    "hackRFInputSettings": ["lnaExt", "lnaGain", "bandwidth", "vgaGain"],
    "airspySettings": ["lnaGain", "mixerGain", "vgaGain"],
    "airspyHFSettings": ["attenuatorSteps", "useLNA", "useAGC"],
    "sdrPlayV3Settings": ["lnaIndex", "ifGain", "ifAGC"],
    "sdrPlaySettings": ["lnaOn", "mixerAmpOn", "basebandGain"],
    "limeSdrInputSettings": ["lnaGain", "tiaGain", "pgaGain"],
    "xtrxInputSettings": ["lnaGain", "tiaGain", "pgaGain"],
    "bladeRF1InputSettings": ["lnaGain", "vga1", "vga2"],
    "fcdProPlusSettings": ["lnaGain", "mixGain", "ifGain"],
    "perseusSettings": ["attenuator", "adcPreamp"],
    "rtlSdrSettings": ["gain", "agc"],
    "plutoSdrInputSettings": ["gain", "gainMode"],
    "bladeRF2InputSettings": ["globalGain", "gainMode"],
    "usrpInputSettings": ["gain", "gainMode"],
}


def friendly(settings_key: str, field: str) -> tuple[str, str]:
    """(label, description) for a device setting."""
    f = FRIENDLY.get(settings_key, {}).get(field)
    if f:
        return f
    d = catalog().get(settings_key, {}).get("fields", {}).get(field, {})
    label = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", field).replace("_", " ").strip()
    return (label[:1].upper() + label[1:], d.get("desc", ""))


def field_docs(settings_key: str, current: dict) -> dict:
    """{field: {value, type, desc}} for every field the device currently reports."""
    docs = catalog().get(settings_key, {}).get("fields", {})
    out = {}
    for k, v in current.items():
        if k.startswith("reverseAPI") or k in ("useReverseAPI",):
            continue
        d = docs.get(k, {})
        e = {"value": v}
        fr = FRIENDLY.get(settings_key, {}).get(k)
        if fr:
            e["desc"] = f"{fr[0]}: {fr[1]}"
        elif d.get("desc"):
            e["desc"] = d["desc"]
        if d.get("enum"):
            e["enum"] = d["enum"]
        out[k] = e
    return out


def report_body(report: dict) -> dict:
    for k, v in (report or {}).items():
        if k.endswith("Report") and isinstance(v, dict):
            return v
    return {}


def supported_rates(report: dict) -> list[int]:
    rb = report_body(report)
    rates = rb.get("sampleRates") or []
    return [int(r.get("rate", r) if isinstance(r, dict) else r) for r in rates]


def sample_rate_plan(settings_key: str, dev: dict, report: dict, requested: float) -> tuple[dict, str]:
    """Fields that give a baseband rate as close as possible to `requested` (device rate + software decimation)."""
    fields: dict = {}
    has_decim = "log2Decim" in dev
    note = ""
    if "devSampleRateIndex" in dev:
        rates = supported_rates(report)
        if not rates:
            raise ValueError(f"{settings_key}: sample rate is chosen by index but SDRangel didn't report the list; "
                             "set devSampleRateIndex yourself with radio_device_settings")
        best = None
        for i, r in enumerate(rates):
            for d in range(0, 7 if has_decim else 1):
                bb = r / 2 ** d
                # prefer >= requested (no aliasing of what was asked for), then closest, then lowest device rate
                score = (bb < requested * 0.97, abs(math.log(bb / requested)), r)
                if best is None or score < best[0]:
                    best = (score, i, d, bb, r)
        _, i, d, bb, r = best
        fields["devSampleRateIndex"] = i
        if has_decim:
            fields["log2Decim"] = d
        note = f"device rate {r:g} S/s (index {i}) / 2^{d} decimation = {bb:g} S/s"
    elif "devSampleRate" in dev:
        fields["devSampleRate"] = int(round(requested))
        if has_decim:
            fields["log2Decim"] = 0
        if settings_key == "rtlSdrSettings":
            # RTL2832: 225 kS/s-300 kS/s or 900 kS/s-3.2 MS/s. Use decimation for anything lower/between.
            if requested < 900e3 and not (225e3 < requested <= 300e3):
                d = max(0, int(math.ceil(math.log2(1.0e6 / requested))))
                fields["devSampleRate"] = int(round(requested * 2 ** d))
                fields["log2Decim"] = d
                note = f"RTL-SDR can't run at {requested:g} S/s directly: {fields['devSampleRate']:g} / 2^{d}"
            fields["devSampleRate"] = min(fields["devSampleRate"], 3_200_000)
    elif "sampleRate" in dev:
        fields["sampleRate"] = int(round(requested))
    else:
        raise ValueError(f"{settings_key} has no adjustable sample rate (fixed by the device/source). "
                         "Current settings: " + ", ".join(sorted(dev)))
    # analog/RF filter to match, for devices that expose one in Hz
    rate_for_bw = requested
    for bwf in ("bandwidth", "lpfBW", "rfBandwidth"):
        if bwf in dev and isinstance(dev.get(bwf), int):
            fields[bwf] = int(rate_for_bw * 0.8)
            break
    return fields, note


GAIN_HELP = "units differ per SDR; values are clamped/rounded by SDRangel to what the hardware supports"


def current_gain_db(settings_key: str, dev: dict) -> float | None:
    """The SDR's overall gain in dB (roughly the inverse of gain_plan), or None when it can't be worked out."""
    def n(k, d=0.0):
        try:
            return float(dev.get(k, d))
        except (TypeError, ValueError):
            return d
    k = settings_key
    if not dev:
        return None
    if k == "hackRFInputSettings":
        return n("lnaGain") + n("vgaGain") + 14 * n("lnaExt")
    if k == "rtlSdrSettings":
        return n("gain") / 10
    if k == "airspySettings":
        return (n("lnaGain") + n("mixerGain") + n("vgaGain")) * 45 / 44
    if k in ("plutoSdrInputSettings", "limeSdrInputSettings", "xtrxInputSettings", "usrpInputSettings",
             "kiwiSDRSettings"):
        return n("gain")
    if k in ("bladeRF2InputSettings", "soapySDRInputSettings"):
        return n("globalGain")
    if k == "bladeRF1InputSettings":
        return n("lnaGain") + n("vga1") + n("vga2")
    if k == "sdrPlayV3Settings":
        return n("lnaGain")
    return None


def gain_plan(settings_key: str, dev: dict, report: dict, gain_db: float | None, auto: bool | None) -> tuple[dict, str]:
    f: dict = {}
    g = gain_db
    k = settings_key
    if k == "hackRFInputSettings":
        if g is not None:
            amp = 1 if g > 62 else 0
            rest = g - 14 * amp
            lna = int(min(40, max(0, round(rest * 0.45 / 8) * 8)))
            vga = int(min(62, max(0, round((rest - lna) / 2) * 2)))
            f.update(lnaGain=lna, vgaGain=vga, lnaExt=amp)
        note = "HackRF: LNA 0-40 dB (8 dB steps) + VGA 0-62 dB (2 dB steps) + 14 dB RF amp (lnaExt); no AGC"
    elif k == "rtlSdrSettings":
        if auto is not None:
            f["agc"] = 1 if auto else 0
        if g is not None:
            gains = [int(x.get("gainCB", x) if isinstance(x, dict) else x) for x in report_body(report).get("gains", [])]
            want = int(round(g * 10))
            f["gain"] = min(gains, key=lambda x: abs(x - want)) if gains else want
        note = "RTL-SDR: tuner gain in tenths of dB (discrete steps from the tuner), agc = RTL2832 digital AGC"
    elif k == "airspySettings":
        if auto is not None:
            f.update(lnaAGC=int(auto), mixerAGC=int(auto))
        if g is not None:
            s = max(0, min(44, round(g / 45 * 44)))
            f.update(lnaGain=min(14, s // 3 + (1 if s % 3 > 0 else 0)), mixerGain=min(15, s // 3 + (1 if s % 3 > 1 else 0)),
                     vgaGain=min(15, s // 3))
        note = "Airspy: LNA 0-14, mixer 0-15, VGA 0-15 (step indexes, ~1 dB each); lnaAGC/mixerAGC"
    elif k == "airspyHFSettings":
        if auto is not None:
            f["useAGC"] = int(auto)
        if g is not None:
            f["useLNA"] = 1 if g > 10 else 0
            f["attenuatorSteps"] = 0 if g > 0 else min(8, int(round(-g / 6)))
        note = "Airspy HF+: LNA on/off, attenuator in 6 dB steps, AGC on/off"
    elif k in ("plutoSdrInputSettings",):
        if g is not None:
            f.update(gain=int(round(g)), gainMode=0)
        if auto:
            f["gainMode"] = 1
        note = "PlutoSDR: gain in dB (about -3..71); gainMode 0 = manual, 1-3 = AGC variants"
    elif k in ("limeSdrInputSettings", "xtrxInputSettings"):
        if g is not None:
            f.update(gain=int(round(g)), gainMode=0)
        note = "LimeSDR/XTRX: gain in dB distributed automatically (gainMode 0); gainMode 1 = set lnaGain/tiaGain/pgaGain yourself"
    elif k == "usrpInputSettings":
        if auto is not None:
            f["gainMode"] = 0 if auto else 1
        if g is not None:
            f.update(gain=int(round(g)), gainMode=1)
        note = "USRP: gain in dB, gainMode 0 = auto, 1 = manual"
    elif k == "bladeRF2InputSettings":
        if g is not None:
            f.update(globalGain=int(round(g)), gainMode=1)
        elif auto:
            f["gainMode"] = 0
        note = "bladeRF 2: globalGain in dB; gainMode 1 = manual, 0 = device default/AGC (see report gainModes)"
    elif k == "bladeRF1InputSettings":
        if g is not None:
            lna = 6 if g > 30 else 3 if g > 15 else 0
            vga1 = int(max(5, min(30, (g - lna) / 2)))
            vga2 = int(max(0, min(30, g - lna - vga1)))
            f.update(lnaGain=lna, vga1=vga1, vga2=vga2)
        note = "bladeRF 1: LNA 0/3/6 dB, VGA1 5-30 dB, VGA2 0-30 dB"
    elif k == "soapySDRInputSettings":
        if auto is not None:
            f["autoGain"] = int(auto)
        if g is not None:
            f["globalGain"] = int(round(g))
        note = "SoapySDR: globalGain in dB (range in device report gainRange); individualGains for per-stage control"
    elif k == "sdrPlayV3Settings":
        if auto is not None:
            f["ifAGC"] = int(auto)
        if g is not None:
            f["lnaGain"] = int(round(g))
        note = "SDRplay: lnaGain in dB (or lnaIndex), ifGain/ifAGC for the IF stage"
    elif k == "kiwiSDRSettings":
        if auto is not None:
            f["useAGC"] = int(auto)
        if g is not None:
            f["gain"] = int(round(g))
        note = "KiwiSDR: gain + useAGC"
    elif k in ("fcdProPlusSettings",):
        if g is not None:
            f.update(lnaGain=1 if g > 10 else 0, mixGain=1 if g > 20 else 0, ifGain=int(max(0, min(59, g))))
        note = "FunCube Pro+: LNA on/off, mixer on/off, IF gain 0-59 dB"
    elif k == "perseusSettings":
        if g is not None:
            f["attenuator"] = 0 if g >= 0 else min(3, int(round(-g / 10)))
        note = "Perseus: attenuator only (0/10/20/30 dB)"
    else:
        gainish = sorted(x for x in dev if re.search(r"gain|agc|att|lna|vga|pga|tia", x, re.I))
        raise ValueError(f"no automatic gain mapping for {settings_key}. Set these with radio_device_settings: "
                         f"{gainish or 'no gain fields reported'}")
    if not f:
        raise ValueError("pass gain_db and/or auto")
    return f, note


# settings visible in SDRangel's GUI that its web API doesn't expose (so nothing - SDR Pilot included - can set them)
GUI_ONLY = {
    "hackRFInputSettings": "SDRangel's 'auto BBF' checkbox is GUI-only (not in its web API). Set the baseband filter "
                           "with `bandwidth` instead - radio_tune already sets it to ~0.75x the sample rate. The ▶/■ "
                           "button is radio_run.",
}
