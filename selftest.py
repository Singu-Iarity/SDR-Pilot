"""SDR Pilot self-test: starts the MCP server with the built-in simulator (no hardware, no SDRangel needed)
and runs the BLE scenario end to end through the real MCP interface.

    radioconda\\python.exe selftest.py

Everything (including the server's own log output and tracebacks) is written to logs/selftest_<time>.log.
"""
import asyncio
import json
import os
import platform
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, f"selftest_{time.strftime('%Y%m%d_%H%M%S')}.log")
LOG = open(LOG_PATH, "w", encoding="utf-8", buffering=1)
WS = os.path.join(HERE, "workspace_selftest")


def out(msg: str = "") -> None:
    print(msg, flush=True)
    LOG.write(msg + "\n")


class SelfTestFailure(Exception):
    pass


def ok(msg):
    out(f"  PASS  {msg}")


def fail(msg):
    out(f"  FAIL  {msg}")
    raise SelfTestFailure(msg)


def environment_report() -> None:
    import importlib.metadata as md
    LOG.write(f"time: {time.ctime()}\npython: {sys.version} ({sys.executable})\nplatform: {platform.platform()}\n")
    for pkg in ("mcp", "numpy", "pyyaml", "pyzmq", "anyio", "pydantic"):
        try:
            LOG.write(f"{pkg}: {md.version(pkg)}\n")
        except Exception:
            LOG.write(f"{pkg}: not installed\n")
    try:
        from gnuradio import gr
        LOG.write(f"gnuradio: {gr.version()} prefix={gr.prefix()}\n")
    except Exception as e:
        LOG.write(f"gnuradio: import failed: {e}\n")
    LOG.write("\n")


async def main():
    import importlib.metadata
    from datetime import timedelta
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    major = int(importlib.metadata.version("mcp").split(".")[0])
    timeout = 300 if major >= 2 else timedelta(seconds=300)  # SDK 1.x wants a timedelta

    env = dict(os.environ)
    env.update({"SDRPILOT_SOURCE": "sim", "SDRPILOT_WORKSPACE": WS,
                "SDRPILOT_OPEN_DASHBOARD_ON_FIRST_USE": "false", "SDRPILOT_ZMQ_PORT": "5690",
                "SDRPILOT_DASHBOARD_PORT": "8790", "SDRPILOT_SIM_API_PORT": "18191", "SDRPILOT_SIM_IQ_PORT": "11334"})
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, "run_server.py")], env=env, cwd=HERE)
    out("SDR Pilot self-test (simulator)\n")
    LOG.write("---- server stderr is interleaved below ----\n")
    async with stdio_client(params, errlog=LOG) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            ok(f"MCP server started, {len(tools.tools)} tools")

            async def call(tool_name, **args):
                LOG.write(f"\n>>> {tool_name}({json.dumps(args, default=str)[:600]})\n")
                res = await s.call_tool(tool_name, args, read_timeout_seconds=timeout)
                err = getattr(res, "is_error", None)
                if err is None:
                    err = getattr(res, "isError", False)
                text = next((c.text for c in res.content if c.type == "text"), "")
                LOG.write(f"<<< {'ERROR ' if err else ''}{text[:4000]}\n")
                if err:
                    fail(f"{tool_name}: {text}")
                try:
                    return json.loads(text)
                except Exception:
                    return text

            st = await call("radio_status")
            ok(f"radio connected: {st.get('hw_type')} @ {st['center_freq_hz'] / 1e6:.3f} MHz")
            await call("radio_tune", center_freq_hz=2401e6, sample_rate=4e6)
            sp = await call("spectrum_snapshot", seconds=0.5)
            if not any(abs(x["freq_hz"] - 2402e6) < 100e3 for x in sp["signals"]):
                fail(f"BLE beacon not seen in spectrum: {sp['signals']}")
            ok("spectrum shows the BLE beacon at 2402 MHz")
            sw = await call("spectrum_watch", seconds=2, cols=60, rows=8)
            if not any(abs(t["freq_mhz"] - 2402) < 0.2 for t in sw["transmitters"]):
                fail(f"spectrum_watch did not list the beacon: {sw['transmitters'][:3]}")
            ok("spectrum_watch: text waterfall lists the beacon")
            iq = await call("iq_samples", offset_hz=1e6, bandwidth_hz=1.5e6, count=128, trigger_db=15, timeout_s=3)
            if not iq.get("triggered") or iq.get("count") != 128:
                fail(f"iq_samples trigger failed: {iq}")
            ok("iq_samples caught a triggered burst")
            cap = await call("capture_iq", seconds=1.0, label="selftest_ble")
            ok(f"captured {cap['samples']} samples")
            ac = await call("analyze_capture", path=cap["path"])
            if not ac["events"]:
                fail("no bursts found")
            e = ac["events"][0]
            sig = await call("analyze_signal", path=cap["path"], start_s=max(0, e["start_s"] - 5e-5),
                             duration_s=e["duration_s"] + 1e-4, offset_hz=e["offset_hz"], bandwidth_hz=1.5e6)
            if "FSK" not in sig.get("classification", "") or abs(sig.get("symbol_rate_est", 0) - 1e6) > 2e4:
                fail(f"unexpected fingerprint: {sig.get('classification')} {sig.get('symbol_rate_est')}")
            ok(f"fingerprint: {sig['classification']}, {sig['symbol_rate_est'] / 1e3:.0f} kbaud")
            with open(os.path.join(HERE, "recipes", "ble_adv_live.json"), encoding="utf-8") as f:
                spec = json.load(f)["spec"]
            with open(os.path.join(HERE, "recipes", "ble_adv_parser_block.py"), encoding="utf-8") as f:
                code = f.read()
            spec["name"] = "selftest_ble"
            for b in spec["blocks"]:
                if b["type"] == "epy_block":
                    b["params"]["_source_code"] = code
                if b["type"] == "sdr_source":
                    b["type"], b["params"] = "capture_source", {"path": cap["path"]}
            keys = ("name", "blocks", "connections", "variables", "imports", "title", "description")
            built = await call("gnuradio_build_flowgraph", **{k: spec[k] for k in keys})
            if not built["valid"]:
                fail(f"flowgraph invalid: {built['errors']}")
            ok("GNU Radio flowgraph built and validated by GRC")
            job = await call("gnuradio_run", name="selftest_ble", wait_s=60)
            log = job.get("log_tail", "")
            line = next((l for l in log.splitlines() if l.startswith("BLE summary")), "")
            if "valid CRC" not in line or line.startswith("BLE summary: 0 "):
                fail(f"decoder produced no packets. Flowgraph log:\n{log}")
            ok(line)
            spec["name"] = "selftest_ble_live"
            for b in spec["blocks"]:
                if b["type"] == "capture_source":
                    b["type"], b["params"] = "sdr_source", {}
            built = await call("gnuradio_build_flowgraph", **{k: spec[k] for k in keys})
            if not built["valid"]:
                fail(f"live flowgraph invalid: {built['errors']}")
            job = await call("gnuradio_run", name="selftest_ble_live", duration_s=3, wait_s=10)
            line = next((l for l in job.get("log_tail", "").splitlines() if l.startswith("BLE summary")), "")
            if not line or line.startswith("BLE summary: 0 "):
                fail(f"live decode failed. Flowgraph log:\n{job.get('log_tail')}")
            ok("live: " + line)
            info = await call("gnuradio_info")
            ok(f"gnuradio_info: GNU Radio {info['version']}, {info['blocks_total']} blocks")
            ed = await call("gnuradio_edit_flowgraph", name="selftest_ble_live", set_variables={"ble_freq": 2402.05e6},
                            save_as="selftest_ble_edit")
            if not ed["valid"]:
                fail(f"edit failed: {ed['errors']}")
            job = await call("gnuradio_run", name="selftest_ble_edit", duration_s=6, wait_s=1.5)
            v = await call("gnuradio_get_variables", job_id=job["job_id"])
            if "ble_freq" not in v["variables"]:
                fail(f"live variables missing: {v}")
            r = await call("gnuradio_set_variable", job_id=job["job_id"], variable="ble_freq", value=2402e6)
            if abs(r["value"] - 2402e6) > 1:
                fail(f"set_variable failed: {r}")
            ok("edited flowgraph and changed a variable of the running job live")
            await call("gnuradio_stop", job_id=job["job_id"])
            a1 = await call("gnuradio_add_block", flowgraph="selftest_steps", name="src", type="capture_source",
                            params={"path": cap["path"]}, note="the BLE recording", start_over=True)
            a2 = await call("gnuradio_add_block", flowgraph="selftest_steps", name="mag", type="blocks_complex_to_mag",
                            connect_from=["src"], note="envelope")
            if a1.get("complete") or not a1.get("open_ports") or a2.get("complete"):
                fail(f"block-by-block draft state wrong: {a1} / {a2}")
            a3 = await call("gnuradio_add_block", flowgraph="selftest_steps", name="sink", type="blocks_null_sink",
                            params={"type": "float"}, connect_from=["mag"])
            if not a3.get("complete"):
                fail(f"block-by-block flowgraph not complete: {a3}")
            ok("gnuradio_add_block: built a flowgraph one block at a time (draft -> complete)")
            rec = await call("gnuradio_use_recipe", recipe="fsk_burst_decoder", name="selftest_recipe",
                             capture_path=cap["path"], variables={"chan_freq": 2402e6, "deviation": 250e3, "baud": 1e6,
                                                                 "chan_bw": 1.3e6, "nbits": 64,
                                                                 "access_code": "'01101011011111011001000101110001'"})
            if not rec["valid"]:
                fail(f"recipe flowgraph invalid: {rec['errors']}")
            job = await call("gnuradio_run", name="selftest_recipe", wait_s=60)
            line = next((l for l in job.get("log_tail", "").splitlines() if l.startswith("Burst summary")), "")
            if not line or line.startswith("Burst summary: 0 "):
                fail(f"FSK recipe found no bursts. Log:\n{job.get('log_tail')}")
            ok("gnuradio_use_recipe: generic FSK decoder template - " + line)
            # SDR Pilot block library: FM broadcast + RDS, and APRS, through the sp_* blocks
            info = await call("gnuradio_info", task="read the RDS station name of an FM station")
            blocks_list = info.get("sdr_pilot_blocks", [])
            if info.get("_full_result"):  # shortened to save context: read the full list
                full = await call("get_full_result", id=info["_full_result"]["id"], path="sdr_pilot_blocks", limit=30000)
                blocks_list = json.loads(full["text"])
            lib = {b["type"]: b["installed"] for b in blocks_list if isinstance(b, dict)}
            if not lib.get("sp_wbfm_receiver") or not lib.get("sp_afsk1200_decoder"):
                fail(f"SDR Pilot block library not installed: {lib}")
            await call("radio_tune", center_freq_hz=100.3e6, sample_rate=2.4e6)
            await asyncio.sleep(2)  # let the stream settle at the new sample rate before recording
            fmcap = await call("capture_iq", seconds=7.0, label="selftest_fm")
            if lib.get("sp_rds_decoder"):
                rec = await call("gnuradio_use_recipe", recipe="fm_rds_receiver", name="selftest_rds",
                                 capture_path=fmcap["path"], variables={"station": 100.1e6})
                if not rec["valid"]:
                    fail(f"FM/RDS recipe invalid: {rec['errors']}")
                job = await call("gnuradio_run", name="selftest_rds", wait_s=90)
                rds = (job.get("decoded") or {}).get("rds") or {}
                if "54A8" not in json.dumps(rds).upper() and "PILOT" not in json.dumps(rds):
                    fail(f"RDS not decoded. Decoded: {job.get('decoded')} Log:\n{job.get('log_tail', '')}")
                ok(f"block library: sp_wbfm_receiver + sp_rds_decoder decoded the simulated station's RDS "
                   f"(PS '{rds.get('ps')}', PI {rds.get('pi')}) as SPDATA records")
            else:
                ok("block library installed (RDS skipped: gr-rds isn't installed here)")
            await call("radio_tune", center_freq_hz=144.6e6, sample_rate=2e6)
            apcap = await call("capture_iq", seconds=8.0, label="selftest_aprs")
            rec = await call("gnuradio_use_recipe", recipe="aprs_afsk1200", name="selftest_aprs", capture_path=apcap["path"])
            if not rec["valid"]:
                fail(f"APRS recipe invalid: {rec['errors']}")
            job = await call("gnuradio_run", name="selftest_aprs", wait_s=90)
            if "SIMTST" not in json.dumps(job.get("decoded") or {}):
                fail(f"APRS packets not decoded. Log:\n{job.get('log_tail')}")
            ok("block library: sp_nbfm_receiver + sp_afsk1200_decoder decoded simulated APRS packets")
            # receive-only: transmit sinks are refused
            tx = await call("gnuradio_build_flowgraph", name="selftest_tx", blocks=[
                {"name": "sig", "type": "analog_sig_source_x", "params": {"type": "complex"}},
                {"name": "out", "type": "osmosdr_sink", "params": {}}], connections=["sig -> out"])
            if tx["valid"] or not any("receive-only" in e for e in tx["errors"]):
                fail(f"a transmit sink was not refused: {tx}")
            ok("receive-only: transmitter sink blocks are refused")
            # start/stop and task-step bookkeeping
            r = await call("radio_run", running=True)
            if r.get("device_state") != "running":
                fail(f"radio_run did not start the SDR: {r}")
            await call("task_start", goal="selftest steps", steps=["a", "b", "c"])
            r = await call("task_step", index=2, status="done")
            if "warning" not in r:
                fail(f"skipped steps were not flagged: {r}")
            ok("radio_run works; steps the AI skips are flagged as unreported")
            # dashboard: remote viewers need the access key; chat messages typed there reach the AI
            import urllib.error
            import urllib.request
            dash = st["dashboard"]["on_this_pc"] if isinstance(st.get("dashboard"), dict) else st["dashboard"]
            with open(os.path.join(WS, "state", "access_key.txt"), encoding="utf-8") as f:
                key = f.read().strip()
            try:
                urllib.request.urlopen(urllib.request.Request(dash + "api/sessions", headers={"X-Forwarded-For": "100.64.1.2"}), timeout=5)
                fail("dashboard answered a remote viewer that had no access key")
            except urllib.error.HTTPError as e:
                if e.code != 401:
                    fail(f"remote viewer without key got HTTP {e.code}, expected 401")
            urllib.request.urlopen(urllib.request.Request(
                dash + "api/chat", data=json.dumps({"text": "selftest hello"}).encode(), method="POST",
                headers={"X-SDRPilot-Key": key, "Content-Type": "application/json"}), timeout=5).read()
            res = await call("radio_status")
            if "selftest hello" not in json.dumps(res.get("dashboard_messages", {})):
                fail(f"dashboard chat message did not reach the AI: {res.get('dashboard_messages')}")
            await call("dashboard_reply", message="selftest reply")
            ok("dashboard: access key enforced for remote viewers; its chat box reaches the AI")
    out("\nAll good - SDR Pilot works on this machine.")


def _find_failure(exc: BaseException):
    if isinstance(exc, SelfTestFailure):
        return exc
    for sub in getattr(exc, "exceptions", []) or []:
        f = _find_failure(sub)
        if f:
            return f
    return None


def _server_log_tail(n: int = 60) -> str:
    p = os.path.join(WS, "logs", "sdrpilot.log")
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-n:])


if __name__ == "__main__":
    environment_report()
    t0 = time.time()
    code = 0
    try:
        asyncio.run(main())
    except BaseException as e:  # noqa: BLE001 - we want to report anything
        code = 1
        failure = _find_failure(e)
        LOG.write("\n---- exception ----\n" + "".join(traceback.format_exception(e)))
        if failure is None:
            out(f"  FAIL  unexpected error: {type(e).__name__}: {e}")
        tail = _server_log_tail()
        if tail:
            LOG.write("\n---- end of server log (" + os.path.join(WS, "logs", "sdrpilot.log") + ") ----\n" + tail)
            # show the most relevant server lines on screen too
            err_lines = [l for l in tail.splitlines() if "Error" in l or "Traceback" in l or "File " in l][-12:]
            if err_lines:
                out("\n  Server-side details:")
                for l in err_lines:
                    out("    " + l.strip())
    out(f"\n({time.time() - t0:.0f} s)  Full log: {LOG_PATH}")
    LOG.close()
    sys.exit(code)
