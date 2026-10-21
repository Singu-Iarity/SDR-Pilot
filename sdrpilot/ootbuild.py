"""Building out-of-tree GNU Radio modules from source, as robustly as we can manage:

  fetch -> analyze -> pick a compatible branch/fork -> dependencies (conda, guarded) -> port if needed
        -> configure -> build -> install -> verify

* analyze: which GNU Radio version the module was written for (3.7: XML block files + SWIG; 3.8: YAML + SWIG;
  3.9+: pybind11 bindings), C++ or Python-only, what it needs (CMake find_package / pkg-config / #include / Python
  imports) mapped to conda-forge packages.
* compatibility: prefer a branch made for the installed GNU Radio (maint-3.10, main...), else a fork that ported
  it; failing that, an automatic port (3.7/3.8 -> 3.10): a fresh gr_modtool skeleton, the old sources moved in,
  the usual API changes applied (boost::shared_ptr, get_initial_sptr, boost::bind message handlers, header
  paths), pybind11 bindings generated with gr_modtool bind, XML block files converted. Ports can still fail on
  real API changes - the build log and oot_build let the AI fix the sources and rebuild.
* dependencies are installed with conda into radioconda, but only after a dry run shows the install would not
  change GNU Radio, Python or the libraries GNU Radio was built with.
* Python-only modules are installed by copying the Python package and block files (no compiler needed).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time

# CMake find_package / pkg-config names -> conda-forge packages (alternatives in order; None = nothing to install)
DEP_MAP: dict[str, list | None] = {
    "gnuradio": None, "gnuradioruntime": None, "gnuradio-runtime": None, "gnuradio-pmt": None, "pythonlibs": None,
    "pythoninterp": None, "python": None, "python3": None, "git": None, "doxygen": None, "cppunit": None,
    "gnuradio-blocks": None, "gnuradio-filter": None, "gnuradio-fft": None, "gnuradio-analog": None,
    "gnuradio-digital": None, "gnuradio-audio": None, "gnuradio-zeromq": None, "sphinx": None,
    "boost": [["libboost-devel", "boost-cpp"]], "volk": [["volk"]], "fftw3f": [["fftw"]], "fftw": [["fftw"]],
    "fftw3": [["fftw"]], "gsl": [["gsl"]], "armadillo": [["armadillo"]], "eigen3": [["eigen"]], "eigen": [["eigen"]],
    "pybind11": [["pybind11"]], "swig": [["swig"]], "uhd": [["uhd"]], "gnuradio-osmosdr": [["gnuradio-osmosdr"]],
    "osmosdr": [["gnuradio-osmosdr"]], "libusb": [["libusb"]], "libusb1": [["libusb"]], "usb-1.0": [["libusb"]],
    "zeromq": [["zeromq"]], "cppzmq": [["cppzmq"]], "zmq": [["zeromq"]], "log4cpp": [["log4cpp"]],
    "spdlog": [["spdlog"]], "fmt": [["fmt"]], "gmp": [["gmp"]], "mpir": [["gmp"]], "liquid": [["liquid-dsp"]],
    "liquiddsp": [["liquid-dsp"]], "sndfile": [["libsndfile"]], "libsndfile": [["libsndfile"]],
    "soapysdr": [["soapysdr"]], "qt5": [["qt-main"]], "qwt": [["qwt"]], "pkgconfig": [["pkg-config"]],
    "mbedtls": [["mbedtls"]], "openssl": [["openssl"]], "pcap": [["libpcap"]], "libpcap": [["libpcap"]],
    "itpp": [["itpp"]], "cairo": [["cairo"]], "portaudio": [["portaudio"]], "alsa": None, "jack": None,
    "threads": None, "pthread": None, "matio": [["libmatio"]], "hdf5": [["hdf5"]], "protobuf": [["libprotobuf"]],
    "json": [["nlohmann_json"]], "nlohmann_json": [["nlohmann_json"]], "libconfig": [["libconfig"]],
}
INCLUDE_MAP = {"fftw3.h": "fftw3", "volk/": "volk", "boost/": "boost", "gsl/": "gsl", "armadillo": "armadillo",
               "Eigen/": "eigen3", "itpp/": "itpp", "liquid/": "liquid", "uhd/": "uhd", "osmosdr/": "osmosdr",
               "zmq.h": "zeromq", "zmq.hpp": "cppzmq", "libusb": "libusb", "sndfile": "sndfile", "gmp": "gmp",
               "mbedtls/": "mbedtls", "openssl/": "openssl", "pcap": "pcap", "nlohmann/json": "json"}
PY_MAP = {"numpy": "numpy", "scipy": "scipy", "matplotlib": "matplotlib", "construct": "construct",
          "crcmod": "crcmod", "bitstring": "bitstring", "zmq": "pyzmq", "serial": "pyserial", "requests": "requests",
          "yaml": "pyyaml", "pmt": None, "gnuradio": None, "PyQt5": "pyqt", "sip": None, "osmosdr": "gnuradio-osmosdr",
          "cffi": "cffi", "click": "click", "websocket": "websocket-client", "ephem": "ephem",
          "skyfield": "skyfield", "sgp4": "sgp4", "feedparser": "feedparser", "pandas": "pandas",
          "Crypto": "pycryptodome", "Cryptodome": "pycryptodomex", "bitarray": "bitarray", "reedsolo": "reedsolo",
          "pyproj": "pyproj", "shapely": "shapely", "pylab": "matplotlib", "lxml": "lxml", "mako": "mako",
          "asciimatics": "asciimatics", "colorama": "colorama", "tqdm": "tqdm", "numba": "numba", "PIL": "pillow"}
# GNU Radio's own libraries: a dependency install must never swap these out from under it
PROTECTED = re.compile(r"^(gnuradio(-core|-.*)?|python|volk|libboost.*|boost.*|spdlog|fmt|pybind11-abi|numpy|"
                       r"libgnuradio.*|uhd|soapysdr|qt-main|qwt|pyqt|log4cpp)$")
BUILD_TOOLS = ["cmake", "ninja", "pkg-config", "pybind11"]


# ---------------------------------------------------------------------------------------------- helpers
def installed_gr_version() -> str:
    try:
        from gnuradio import gr
        return gr.version()
    except Exception:
        return ""


def vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def gr_major(v: str) -> str:
    m = re.match(r"(\d+)\.(\d+)", v or "")
    return f"{m.group(1)}.{m.group(2)}" if m else ""


def library_prefix() -> str:
    """Where GNU Radio itself is installed (radioconda: <env>\\Library on Windows, <env> elsewhere)."""
    try:
        from gnuradio import gr
        p = gr.prefix()
        if p and os.path.isdir(p):
            return p
    except Exception:
        pass
    return os.path.join(sys.prefix, "Library") if os.name == "nt" else sys.prefix


def python_dir() -> str:
    """The site-packages folder that holds the gnuradio package (a module must go next to it to import as
    gnuradio.<name>)."""
    try:
        import gnuradio
        return os.path.dirname(os.path.dirname(os.path.abspath(gnuradio.__file__)))
    except Exception:
        return sysconfig.get_paths()["purelib"]


def grc_blocks_dir() -> str:
    return os.path.join(library_prefix(), "share", "gnuradio", "grc", "blocks")


def _read(p: str, n: int = 400_000) -> str:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(n)
    except OSError:
        return ""


def _write(p: str, text: str) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _files(root: str, *pats: str) -> list[str]:
    out = []
    for pat in pats:
        out += glob.glob(os.path.join(root, "**", pat), recursive=True)
    skip = (os.sep + "build" + os.sep, os.sep + ".git" + os.sep)
    return sorted({p for p in out if not any(s in p for s in skip)})


_MODTOOL_BOOT = (
    "import sys\n"
    "try:\n"
    "    import pygccxml.parser.config as _c\n"
    "    _o = _c.xml_generator_configuration_t.__init__\n"
    "    def _i(self, *a, **k):\n"
    "        if sys.platform == 'win32':\n"
    "            # castxml emulates MSVC: drop GCC-only flags, and say the preprocessor is the conforming one (it is\n"
    "            # clang's) - otherwise Boost takes its old-MSVC preprocessor path and every header using Boost.MPL\n"
    "            # fails ('pasting formed BOOST_PP_TUPLE_ELEM_O_3(') -> no Python bindings for those blocks\n"
    "            fl = [f for f in str(k.get('cflags') or '').split() if f not in ('-fPIC', '-fsized-deallocation')]\n"
    "            try:\n"
    "                return _o(self, *a, **dict(k, cflags=' '.join(fl + ['-D_MSVC_TRADITIONAL=0'])))\n"
    "            except TypeError:  # cflags passed positionally by some pygccxml version: leave it as it was\n"
    "                pass\n"
    "        _o(self, *a, **k)\n"
    "    _c.xml_generator_configuration_t.__init__ = _i\n"
    "except Exception:\n"
    "    pass\n"
    "from gnuradio.modtool.cli.base import cli\n"
    "sys.argv[0] = 'gr_modtool'\n"
    "cli()\n")


def modtool_cmd(*args: str) -> list[str]:
    """gr_modtool, through this Python (works the same on Windows and Linux, whatever the PATH). On Windows its
    binding generator passes GCC-only flags (-fPIC) to castxml, which targets MSVC there and refuses them - they
    are dropped."""
    return [sys.executable, "-c", _MODTOOL_BOOT, *args]


MSVC_REPORT: list[str] = []  # what msvc_env() looked at - shown when no compiler is found


def _fresh_windows_path() -> str:
    """PATH as it is in the registry now (system + user). A PATH the user changed after Claude started isn't in
    this process's environment yet - read it straight from Windows instead of requiring a restart."""
    try:
        import winreg
    except ImportError:
        return os.environ.get("PATH", "")
    parts = []
    for root, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, r"Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                v, _t = winreg.QueryValueEx(k, "Path")
                parts.append(os.path.expandvars(str(v)))
        except OSError:
            continue
    merged = []
    for chunk in [os.environ.get("PATH", "")] + parts:
        for d in chunk.split(os.pathsep):
            d = d.strip().strip('"')
            if d and d.lower() not in {m.lower() for m in merged}:
                merged.append(d)
    return os.pathsep.join(merged)


def _vcvars_env(bat: str) -> dict | None:
    """Run vcvars64.bat in a throw-away batch file (no cmd quoting games) and capture the environment it sets."""
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), f"sdrpilot_vcvars_{os.getpid()}.bat")
    try:
        with open(tmp, "w", encoding="ascii", errors="replace") as f:
            f.write(f'@echo off\r\ncall "{bat}" > "{tmp}.log" 2>&1\r\necho ===SDRPILOT-ENV===\r\nset\r\n')
        r = subprocess.run(["cmd.exe", "/d", "/c", tmp], capture_output=True, text=True, timeout=180,
                           creationflags=0x08000000)
        out = r.stdout
    except Exception as e:
        MSVC_REPORT.append(f"running {bat} failed: {e}")
        return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    said = ""
    try:
        with open(tmp + ".log", encoding="utf-8", errors="replace") as f:
            said = " | ".join(l.strip() for l in f.read().splitlines() if l.strip() and "*" not in l)[-400:]
        os.remove(tmp + ".log")
    except OSError:
        pass
    env = {}
    for line in out.split("===SDRPILOT-ENV===", 1)[-1].splitlines():
        if "=" in line and not line.startswith("="):
            k, v = line.split("=", 1)
            env[k] = v
    if not env.get("INCLUDE") or not env.get("LIB"):
        MSVC_REPORT.append(f"{bat} ran but didn't set INCLUDE/LIB" + (f" - it said: {said}" if said else ""))
        return None
    return env


def _windows_sdk() -> tuple[str, str] | None:
    """(Windows Kits 10 root, newest SDK version that has both headers and x64 libraries)."""
    roots = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows Kits\Installed Roots") as k:
            roots.append(winreg.QueryValueEx(k, "KitsRoot10")[0])
    except Exception:
        pass
    roots.append(os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Windows Kits", "10"))
    for root in roots:
        vers = sorted((d for d in os.listdir(os.path.join(root, "Lib")) if d.startswith("10.")), key=vtuple,
                      reverse=True) if os.path.isdir(os.path.join(root, "Lib")) else []
        for v in vers:
            if os.path.exists(os.path.join(root, "Lib", v, "um", "x64", "kernel32.lib")) and \
                    os.path.isdir(os.path.join(root, "Include", v, "ucrt")):
                return root.rstrip("\\/"), v
    return None


def _manual_msvc_env(cl: str) -> dict | None:
    """When vcvars64.bat won't run (it happens with preview Visual Studio builds), set up what a CMake + Ninja build
    needs ourselves: the compiler's bin/include/lib folders plus the Windows SDK's."""
    low = cl.lower()
    i = low.find(os.sep + "bin" + os.sep)
    if i < 0:
        return None
    msvc = cl[:i]  # ...\VC\Tools\MSVC\<version>
    sdk = _windows_sdk()
    if not sdk:
        MSVC_REPORT.append("no Windows 10/11 SDK with x64 libraries found (Visual Studio Installer -> "
                           "Individual components -> Windows 11 SDK)")
        return None
    root, v = sdk
    inc = [os.path.join(msvc, "include")] + [os.path.join(root, "Include", v, d)
                                             for d in ("ucrt", "shared", "um", "winrt", "cppwinrt")]
    lib = [os.path.join(msvc, "lib", "x64"), os.path.join(root, "Lib", v, "ucrt", "x64"),
           os.path.join(root, "Lib", v, "um", "x64")]
    paths = [os.path.dirname(cl), os.path.join(root, "bin", v, "x64"), os.path.join(root, "bin", "x64")]
    # Visual Studio ships its own CMake and Ninja - no need to get them from conda
    j = low.find(os.sep + "vc" + os.sep)
    if j > 0:
        ide = os.path.join(cl[:j], "Common7", "IDE", "CommonExtensions", "Microsoft", "CMake")
        paths += [os.path.join(ide, "Ninja"), os.path.join(ide, "CMake", "bin")]
    env = dict(os.environ)
    env.update(INCLUDE=os.pathsep.join(p for p in inc if os.path.isdir(p)),
               LIB=os.pathsep.join(p for p in lib if os.path.isdir(p)), LIBPATH=os.path.join(msvc, "lib", "x64"),
               VCToolsInstallDir=msvc + os.sep, WindowsSdkDir=root + os.sep, WindowsSDKVersion=v + os.sep,
               UCRTVersion=v, UniversalCRTSdkDir=root + os.sep, VSCMD_ARG_TGT_ARCH="x64",
               PATH=os.pathsep.join([p for p in paths if os.path.isdir(p)] + [_fresh_windows_path()]))
    MSVC_REPORT.append(f"set up the compiler environment directly: {msvc} + Windows SDK {v}")
    return env


def _vcvars_candidates(path_env: str) -> list[str]:
    cands = []
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    vswhere = os.path.join(pf86, "Microsoft Visual Studio", "Installer", "vswhere.exe")
    if os.path.exists(vswhere):
        for args in (["-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"],
                     ["-all", "-prerelease", "-products", "*"]):
            try:
                out = subprocess.run([vswhere, *args, "-property", "installationPath"], capture_output=True,
                                     text=True, timeout=30, creationflags=0x08000000).stdout
                cands += [os.path.join(l.strip(), "VC", "Auxiliary", "Build", "vcvars64.bat")
                          for l in out.splitlines() if l.strip()]
            except Exception as e:
                MSVC_REPORT.append(f"vswhere failed: {e}")
    else:
        MSVC_REPORT.append("vswhere.exe not found (no Visual Studio installer)")
    # cl.exe on PATH: ...\VC\Tools\MSVC\<ver>\bin\Hostx64\x64\cl.exe -> ...\VC\Auxiliary\Build\vcvars64.bat
    for d in path_env.split(os.pathsep):
        cl = os.path.join(d, "cl.exe")
        if os.path.exists(cl):
            MSVC_REPORT.append(f"cl.exe on PATH: {cl}")
            low = cl.lower()
            i = low.find(os.sep + "vc" + os.sep + "tools" + os.sep)
            if i > 0:
                cands.append(os.path.join(cl[:i], "VC", "Auxiliary", "Build", "vcvars64.bat"))
    for base in (pf, pf86):
        cands += sorted(glob.glob(os.path.join(base, "Microsoft Visual Studio", "*", "*", "VC", "Auxiliary", "Build",
                                               "vcvars64.bat")), reverse=True)
    extra = os.environ.get("SDRPILOT_VCVARS", "")
    if extra:
        cands.insert(0, extra)
    seen, out = set(), []
    for c in cands:
        k = os.path.normcase(c)
        if k not in seen and os.path.exists(c):
            seen.add(k)
            out.append(c)
    return out


def msvc_env() -> dict | None:
    """Windows: the Visual Studio C++ build environment, or None (MSVC_REPORT says what was checked).
    Order: an environment that is already set up (cl.exe on PATH + INCLUDE/LIB, e.g. started from a Developer
    prompt), then vcvars64.bat from vswhere / next to the cl.exe on PATH / the usual install folders."""
    MSVC_REPORT.clear()
    if os.name != "nt":
        return dict(os.environ)
    path_env = _fresh_windows_path()
    env = dict(os.environ, PATH=path_env)
    if shutil.which("cl", path=path_env) and env.get("INCLUDE") and env.get("LIB"):
        MSVC_REPORT.append("using the compiler environment that is already set (cl.exe + INCLUDE/LIB)")
        return env
    tried = _vcvars_candidates(path_env)
    for bat in tried:
        e = _vcvars_env(bat)
        if e:
            # MSVC's folders first (so its link.exe wins over any Unix-tools 'link' in conda's usr\bin), then
            # radioconda's (cmake, ninja, pkg-config, python) if vcvars dropped them
            have = {x.lower() for x in e.get("PATH", "").split(os.pathsep)}
            conda = [x for x in os.environ.get("PATH", "").split(os.pathsep)
                     if "conda" in x.lower() and x.lower() not in have and not x.lower().endswith("usr\\bin")]
            e["PATH"] = os.pathsep.join([e.get("PATH", "")] + conda)
            MSVC_REPORT.append(f"using {bat}")
            return e
        MSVC_REPORT.append(f"tried {bat}")
    # no working vcvars: build the environment from the cl.exe we can see (PATH, or the newest one installed)
    cls = [os.path.join(d, "cl.exe") for d in path_env.split(os.pathsep) if os.path.exists(os.path.join(d, "cl.exe"))]
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        cls += sorted(glob.glob(os.path.join(base, "Microsoft Visual Studio", "*", "*", "VC", "Tools", "MSVC", "*",
                                             "bin", "Hostx64", "x64", "cl.exe")), reverse=True)
    for cl in cls:
        if "hostx64" + os.sep + "x64" not in cl.lower():
            continue
        e = _manual_msvc_env(cl)
        if e:
            return e
    return None


# ---------------------------------------------------------------------------------------------- analyze
def analyze(src: str) -> dict:
    """What a module source tree is: target GNU Radio version, language, block files, dependencies."""
    top = _read(os.path.join(src, "CMakeLists.txt"))
    all_cmake = "\n".join(_read(p) for p in _files(src, "CMakeLists.txt", "*.cmake")[:80])
    name = ""
    m = re.search(r"project\s*\(\s*gr-?([\w-]+)", top, re.I)
    if m:
        name = m.group(1).replace("-", "_")
    if not name:
        incs = [d for d in glob.glob(os.path.join(src, "include", "*")) if os.path.isdir(d)
                and os.path.basename(d) != "gnuradio"]
        incs += [d for d in glob.glob(os.path.join(src, "include", "gnuradio", "*")) if os.path.isdir(d)]
        name = os.path.basename(incs[0]) if incs else re.sub(r"^gr-?", "", os.path.basename(src.rstrip("/\\"))
                                                            ).split("-")[0].replace("-", "_")
    cc = [p for p in _files(src, "*.cc", "*.cpp", "*.c") if os.sep + "lib" + os.sep in p or os.sep + "src" + os.sep in p]
    cc = [p for p in cc if not os.path.basename(p).startswith(("qa_", "test_"))]
    xmls = [p for p in _files(src, "*.xml") if os.sep + "grc" + os.sep in p]
    ymls = [p for p in _files(src, "*.block.yml", "*.yml") if os.sep + "grc" + os.sep in p]
    has_swig = bool(_files(src, "*.i")) or os.path.isdir(os.path.join(src, "swig"))
    has_pybind = bool(glob.glob(os.path.join(src, "python", "**", "bindings"), recursive=True))
    wanted = re.findall(r'find_package\s*\(\s*Gnuradio\s+"?(\d+\.\d+)', all_cmake, re.I)
    ev = []
    if xmls and not ymls:
        era, ev = "3.7", ["GRC blocks are XML (3.7 format)"]
    elif re.search(r"GnuradioRuntime|GR_REQUIRED_COMPONENTS|include\s*\(\s*GrPlatform", all_cmake) and not wanted:
        era, ev = "3.7", ["CMake uses the 3.7 GnuradioRuntime / GR_REQUIRED_COMPONENTS setup"]
    elif has_swig and not has_pybind:
        era, ev = "3.8", ["SWIG Python bindings (3.8 and earlier; 3.9+ uses pybind11)"]
    elif has_pybind:
        era = wanted[0] if wanted and vtuple(wanted[0]) >= (3, 9) else "3.9+"
        ev = ["pybind11 bindings (3.9 or newer)"]
    elif not cc:
        era = wanted[0] if wanted else "any"
        ev = ["Python-only"]
    else:
        era = wanted[0] if wanted else "unknown"
    if wanted:
        ev.append(f'CMakeLists asks for GNU Radio {wanted[0]}')
    deps: set[str] = set()
    for m in re.finditer(r"find_package\s*\(\s*([\w.+-]+)", all_cmake, re.I):
        deps.add(m.group(1).lower())
    for m in re.finditer(r"pkg_check_modules\s*\(\s*\w+\s+(?:REQUIRED\s+|QUIET\s+)?([\w.+-]+)", all_cmake, re.I):
        deps.add(re.sub(r"[<>=].*", "", m.group(1)).lower())
    comps = set()
    for m in re.finditer(r"(?:COMPONENTS|GR_REQUIRED_COMPONENTS)\s+([\w\s-]+?)\)", all_cmake, re.I):
        comps |= {c.lower() for c in m.group(1).split() if c.upper() not in ("REQUIRED", "QUIET")}
    for p in cc + _files(src, "*.h", "*.hpp")[:400]:
        for inc in re.findall(r'#\s*include\s*[<"]([^>"]+)[>"]', _read(p, 60_000)):
            for k, v in INCLUDE_MAP.items():
                if inc.startswith(k) or inc == k:
                    deps.add(v)
    py_imports: set[str] = set()
    pyfiles = [p for p in _files(src, "*.py") if not os.path.basename(p).startswith(("qa_", "test_"))
               and os.sep + "docs" + os.sep not in p and os.sep + "examples" + os.sep not in p]
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    local = {os.path.splitext(os.path.basename(p))[0] for p in _files(src, "*.py")} | {name}
    py2 = False
    for p in pyfiles:
        t = _read(p, 80_000)
        if re.search(r"^\s*print\s+[\"'\w]", t, re.M) or "xrange(" in t or re.search(r"except\s+\w+\s*,\s*\w+:", t):
            py2 = True
        for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", t, re.M):
            mod = m.group(1)
            if mod not in stdlib and mod not in local and not mod.startswith("_") and not mod.endswith("_swig") \
                    and mod not in ("dl", "DLFCN"):
                py_imports.add(mod)
    for f in ("requirements.txt",):
        for line in _read(os.path.join(src, f)).splitlines():
            line = re.split(r"[<>=;# ]", line.strip())[0]
            if line:
                py_imports.add(line)
    return {"name": name, "written_for": era, "evidence": ev, "language": "C++" if cc else "Python",
            "cpp_files": len(cc), "grc_xml": len(xmls), "grc_yml": len(ymls), "swig": has_swig, "pybind11": has_pybind,
            "cmake_deps": sorted(deps), "gr_components": sorted(comps), "python_imports": sorted(py_imports),
            "python2_code": py2}


def needs_port(an: dict, gr_installed: str) -> bool:
    era, inst = an["written_for"], gr_major(gr_installed) or "3.10"
    if era in ("3.6", "3.7", "3.8"):
        return vtuple(inst) >= (3, 9)
    return False


def conda_packages(an: dict, porting: bool = False) -> tuple[list[list[str]], list[str]]:
    """-> (packages to install, each a list of alternatives; names we don't know how to get)."""
    pk, unknown = [], []
    seen = set()

    def add(alts):
        key = alts[0]
        if key not in seen:
            seen.add(key)
            pk.append(alts)

    if an["language"] == "C++":
        for t in BUILD_TOOLS:
            add([t])
        if os.name != "nt":
            add(["cxx-compiler"])
        if porting:
            add(["castxml"])
            add(["pygccxml"])
    for d in an["cmake_deps"]:
        key = re.sub(r"^lib", "", d) if d not in DEP_MAP else d
        if (d.startswith("gnuradio") and d not in DEP_MAP) or d in ("gruel",) or (porting and d == "swig"):
            continue  # parts of GNU Radio itself (gruel: 3.6), or SWIG, which a port replaces with pybind11
        alts = DEP_MAP.get(d, DEP_MAP.get(key, "?"))
        if alts == "?":
            unknown.append(d)
        elif alts:
            for a in alts:
                add(a)
    for imp in an["python_imports"]:
        alts = PY_MAP.get(imp, "?")
        if alts == "?":
            unknown.append("python:" + imp)
        elif alts:
            add([alts])
    return pk, sorted(set(unknown))


# ---------------------------------------------------------------------------------------------- XML -> YAML
def _expr(s: str) -> str:
    """A 3.7 field that's an expression of parameters ('$N_rb_dl*12', '$type.size') -> one 3.8+ expression
    '${ N_rb_dl*12 }' (fields such as vlen / dtype / nports are evaluated whole, not as a text template).
    A plain value stays as it is."""
    s = (s or "").strip()
    if "$" not in s:
        return s
    inner = re.sub(r"\$\{?\(?(\w+)\)?\}?", r"\1", s)
    return "${ " + inner + " }"


def _cheetah(s: str) -> str:
    """$var / ${var} / $(var) in 3.7 templates -> ${var} (the parameter's code). $(var()) / $var() / $var.eval
    (the parameter's evaluated value) -> ${var()} - GRC's template arguments return that when called."""
    s = re.sub(r"\$\((\w+)(?:\(\)|\.eval)\)", r"${\1()}", s or "")
    s = re.sub(r"\$(\w+)(?:\(\)|\.eval\b)", r"${\1()}", s)
    s = re.sub(r"\$\((\w+)\)", r"${\1}", s)
    return re.sub(r"\$(?!\{)(\w+)", r"${\1}", s)


def case_fallback_snippet(names: list[str]) -> str:
    """CMake: old modules use FFTW3F_LIBRARIES where GNU Radio 3.10's Find module sets FFTW3f_LIBRARIES - fill each
    wanted variable (and its _INCLUDE_DIRS sibling) from any variable with the same name in another case."""
    want = []
    for n in names:
        want.append(n)
        base = re.sub(r"_(LIBRARIES|LIBRARY|LIBS)$", "", n)
        want.append(base + "_INCLUDE_DIRS")
    if not want:
        return ""
    return ("# SDR Pilot port: variable names from the original module, filled case-insensitively\n"
            "get_cmake_property(_sp_vars VARIABLES)\n"
            f"foreach(_want {' '.join(want)})\n"
            "  if(NOT ${_want})\n"
            "    foreach(_v ${_sp_vars})\n"
            "      string(TOUPPER \"${_v}\" _vu)\n"
            "      string(TOUPPER \"${_want}\" _wu)\n"
            "      if(_vu STREQUAL _wu AND NOT _v STREQUAL _want AND ${_v})\n"
            "        set(${_want} ${${_v}})\n"
            "      endif()\n"
            "    endforeach()\n"
            "  endif()\n"
            "endforeach()\n\n")


def fix_block_imports(yml_text: str, module: str) -> str:
    """3.7/3.8 modules were top-level Python packages ('import lte'); from 3.9 they live in gnuradio ('from
    gnuradio import lte'). Fix the import lines of a block file accordingly."""
    m = re.escape(module)
    t = re.sub(rf"(^|\n)(\s*)import\s+{m}\s*(?=\n|$)", rf"\1\2from gnuradio import {module}", yml_text)
    t = re.sub(rf"(^|\n)(\s*)import\s+{m}\s+as\s+(\w+)", rf"\1\2from gnuradio import {module} as \3", t)
    t = re.sub(rf"(^|\n)(\s*)from\s+{m}\s+import\s+", rf"\1\2from gnuradio.{module} import ", t)
    t = re.sub(rf"(imports:\s*)import\s+{m}\s*$", rf"\1from gnuradio import {module}", t, flags=re.M)
    t = re.sub(rf"(imports:\s*['\"]?)import\s+{m}(['\"]?\s*)$", rf"\1from gnuradio import {module}\2", t, flags=re.M)
    return t


def xml_block_to_yml(xml_text: str) -> tuple[str, str, list[str]]:
    """A 3.7 GRC block XML -> (block id, 3.8+ YAML text, warnings)."""
    import xml.etree.ElementTree as ET
    import yaml
    root = ET.fromstring(xml_text)
    g = lambda el, tag, d="": (el.findtext(tag) or d).strip()  # noqa: E731
    warn = []
    bid = g(root, "key")
    out: dict = {"id": bid, "label": g(root, "name", bid)}
    if g(root, "category"):
        out["category"] = g(root, "category")
    params = []
    for p in root.findall("param"):
        d = {"id": g(p, "key"), "label": g(p, "name", g(p, "key")), "dtype": g(p, "type", "raw")}
        if d["dtype"].startswith("$") or "#" in d["dtype"]:
            warn.append(f"{bid}.{d['id']}: dynamic type '{d['dtype']}' -> raw (check it)")
            d["dtype"] = "raw"
        if p.find("value") is not None:
            d["default"] = g(p, "value")
        opts = p.findall("option")
        if opts:
            d["dtype"] = "enum"
            d["options"] = [g(o, "key") for o in opts]
            d["option_labels"] = [g(o, "name", g(o, "key")) for o in opts]
        if g(p, "hide") and "#" not in g(p, "hide") and "$" not in g(p, "hide"):
            d["hide"] = g(p, "hide")
        params.append(d)
    if params:
        out["parameters"] = params

    def ports(tag):
        res = []
        for s in root.findall(tag):
            t = g(s, "type", "complex")
            d = {"label": g(s, "name", "in" if tag == "sink" else "out"),
                 "domain": "message" if t == "message" else "stream"}
            if t != "message":
                if t.startswith("$") or "#" in t:
                    warn.append(f"{bid}: port type '{t}' depends on a parameter -> {_expr(t)}")
                d["dtype"] = _expr(t) if "$" in t else t
                if g(s, "vlen") and g(s, "vlen") != "1":
                    d["vlen"] = _expr(g(s, "vlen"))
                if g(s, "nports"):
                    d["multiplicity"] = _expr(g(s, "nports"))
            if g(s, "optional") in ("1", "True", "true"):
                d["optional"] = True
            res.append(d)
        return res
    if ports("sink"):
        out["inputs"] = ports("sink")
    if ports("source"):
        out["outputs"] = ports("source")
    tpl = {"imports": "\n".join(x.text.strip() for x in root.findall("import") if x.text)}
    make = g(root, "make")
    if "#" in make:
        warn.append(f"{bid}: the make template uses Cheetah logic - converted literally, check it")
    tpl["make"] = _cheetah(make)
    cbs = [_cheetah(c.text.strip()) for c in root.findall("callback") if c.text]
    if cbs:
        tpl["callbacks"] = cbs
    out["templates"] = tpl
    if g(root, "doc"):
        out["documentation"] = g(root, "doc")
    out["file_format"] = 1
    return bid, yaml.safe_dump(out, sort_keys=False, allow_unicode=True), warn


# ---------------------------------------------------------------------------------------------- porting
API_FIXES = [
    (r"#\s*include\s*<boost/shared_ptr\.hpp>\s*\n", "", "dropped <boost/shared_ptr.hpp>"),
    (r"boost::shared_ptr<", "std::shared_ptr<", "boost::shared_ptr -> std::shared_ptr"),
    (r"boost::dynamic_pointer_cast<", "std::dynamic_pointer_cast<", "boost::dynamic_pointer_cast -> std::"),
    (r"boost::make_shared<", "std::make_shared<", "boost::make_shared -> std::make_shared"),
    (r"boost::bind\s*\(\s*&([\w:]+)::(\w+)\s*,\s*this\s*,\s*_1\s*\)",
     r"[this](const pmt::pmt_t& msg) { this->\2(msg); }", "boost::bind(&X::f, this, _1) -> lambda"),
    (r"boost::bind\s*\(\s*&([\w:]+)::(\w+)\s*,\s*this\s*,\s*(?:boost::)?placeholders::_1\s*\)",
     r"[this](const pmt::pmt_t& msg) { this->\2(msg); }", "boost::bind placeholders -> lambda"),
    (r"#\s*include\s*<gnuradio/fft/fft\.h>", "#include <gnuradio/fft/fft.h>", ""),
    (r"\bboost::math::constants::pi<(\w+)>\(\)", r"static_cast<\1>(GR_M_PI)", "boost pi -> GR_M_PI"),
    (r"__GR_VLA\s*\(\s*([^,]+?)\s*,\s*(\w+)\s*,\s*([^;]+?)\)\s*;",
     r"std::vector<\1> \2##_vla_(\3); \1* \2 = \2##_vla_.data();", "__GR_VLA (removed) -> std::vector"),
]
API_WARNINGS = [
    (r"gr::fft::fft_complex\b(?!_)", "gr::fft::fft_complex was split into fft_complex_fwd / fft_complex_rev in 3.10"),
    (r"gr::fft::fft_real_fwd\s*\(\s*\w+\s*,\s*(true|false)", "fft constructors changed in 3.10 (no forward flag)"),
    (r"GR_LOG_DECLARE_LOGPTR|GR_LOG_ASSIGN_LOGPTR|GR_CONFIG_LOGGER|configure_default_loggers",
     "the old log4cpp logging macros are gone in 3.10 - use d_logger->info(...) / d_logger->warn(...)"),
    (r"\bprefs\(\)->", "gr::prefs access changed in 3.10 (gr::prefs::singleton())"),
    (r"boost::thread\b", "boost::thread: 3.10 uses gr::thread::thread (std::thread) - may need changing"),
    (r"message_port_register_(in|out)\s*\(\s*pmt::mp\(", ""),
    (r"\bgr_complex\s*\*\s*\w+\s*=\s*\(gr_complex\s*\*\)\s*fftw_malloc", "fftw_malloc: prefer volk_malloc"),
]


def _close_paren(text: str, i: int) -> int:
    """Index of the ')' matching the '(' at text[i]."""
    depth = 0
    for k in range(i, len(text)):
        c = text[k]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return k
    return -1


def _fix_initial_sptr(text: str) -> tuple[str, int]:
    """gnuradio::get_initial_sptr(new X(args)) -> gnuradio::make_block_sptr<X>(args)."""
    n = 0
    pat = re.compile(r"gnuradio::get_initial_sptr\s*\(\s*new\s+([\w:]+)\s*\(")
    while True:
        m = pat.search(text)
        if not m:
            return text, n
        outer = text.index("(", m.start())
        inner = m.end() - 1
        ci, co = _close_paren(text, inner), _close_paren(text, outer)
        if ci < 0 or co < 0:
            return text, n
        args = text[inner + 1:ci]
        text = text[:m.start()] + f"gnuradio::make_block_sptr<{m.group(1)}>({args})" + text[co + 1:]
        n += 1


_TEMPLATED_SUFFIX = re.compile(r"#\s*include\s*<gnuradio/(\w+)/(\w+?)_(b|s|i|f|c|bb|ss|ii|ff|cc|ci|cf|fc|sc|bf|"
                               r"ccc|ccf|fff|fcc|fsf|scc|sss|ffc|cff)\.h>")


def _installed_header(rel: str) -> bool:
    return os.path.exists(os.path.join(library_prefix(), "include", rel))


def apply_api_fixes(text: str) -> tuple[str, list[str]]:
    done = []
    for pat, rep, msg in API_FIXES:
        new, n = re.subn(pat, rep, text)
        if n and msg:
            done.append(f"{msg} ({n}x)")
        text = new
    text = text.replace("##_vla_", "_vla_")
    if "_vla_.data()" in text and "<vector>" not in text:
        text = re.sub(r"(#\s*include\s*[<\"][^>\"]+[>\"]\s*\n)", r"\1#include <vector>\n", text, count=1)
    text, n = _fix_initial_sptr(text)
    if n:
        done.append(f"get_initial_sptr(new X(...)) -> make_block_sptr<X>(...) ({n}x)")
    # GNU Radio 3.10's logger formats with fmt; with fmt 11 a boost::format argument no longer compiles
    # (error C2662 in fmt/base.h) - hand the logger the finished string instead
    text, n = re.subn(r"(GR_LOG_(?:TRACE|DEBUG|INFO|NOTICE|WARN|ERROR|CRIT|ALERT|FATAL|EMERG)\s*\(\s*[\w>.-]+\s*,\s*)"
                      r"(boost::format\s*\((?:[^;])*?)(\)\s*;)",
                      lambda m: m.group(1) + "(" + m.group(2) + ").str()" + m.group(3), text)
    if n:
        done.append(f"logger calls with boost::format -> .str() (fmt 11) ({n}x)")

    def templ(m):  # 3.7 per-type headers (sig_source_c.h) became one templated header (sig_source.h) in 3.8
        old_rel, new_rel = f"gnuradio/{m.group(1)}/{m.group(2)}_{m.group(3)}.h", f"gnuradio/{m.group(1)}/{m.group(2)}.h"
        if not _installed_header(old_rel) and _installed_header(new_rel):
            done.append(f"<{old_rel}> -> <{new_rel}> (templated since 3.8)")
            return f"#include <{new_rel}>"
        return m.group(0)
    text = _TEMPLATED_SUFFIX.sub(templ, text)
    if re.search(r"\bgr_complex\b", text) and "gr_complex.h" not in text and "gr_complex" not in \
            "".join(re.findall(r"typedef[^;]*gr_complex[^;]*;", text)):
        m = re.search(r"#\s*include\s*<[^>]+>\s*\n", text)
        if m:
            text = text[:m.end()] + "#include <gnuradio/gr_complex.h>\n" + text[m.end():]
            done.append("added <gnuradio/gr_complex.h> (no longer pulled in indirectly)")
    return text, done


def api_warnings(text: str) -> list[str]:
    return [msg for pat, msg in API_WARNINGS if msg and re.search(pat, text)]


def py2_fix(text: str) -> tuple[str, list[str]]:
    """The common Python 2 -> 3 changes (lib2to3 is gone from new Pythons): tabs, print, except/raise syntax,
    xrange, iteritems, has_key. Integer division (/ vs //) can't be fixed mechanically."""
    done = []
    lines = []
    tabs = False
    for line in text.splitlines():
        lead = re.match(r"[ \t]*", line).group(0)
        if "\t" in lead:
            tabs = True
            line = lead.expandtabs(8) + line[len(lead):]
        lines.append(line)
    text = "\n".join(lines) + "\n"
    if tabs:
        done.append("tabs -> spaces (Python 2 tab width 8)")
    rules = [
        (r"^(\s*)print\s*>>\s*([\w.]+)\s*,\s*(.+?)\s*$", r"\1print(\3, file=\2)", "print >>f, x -> print(x, file=f)"),
        (r"^(\s*)print\s+(?!\()(.+?)\s*,\s*$", r"\1print(\2, end=' ')", "print x, -> print(x, end=' ')"),
        (r"^(\s*)print\s+(?!\()(.+?)\s*$", r"\1print(\2)", "print x -> print(x)"),
        (r"^(\s*)print\s*$", r"\1print()", ""),
        (r"except\s+([\w.]+|\([^)]*\))\s*,\s*(\w+)\s*:", r"except \1 as \2:", "except X, e -> except X as e"),
        (r"raise\s+([\w.]+)\s*,\s*(.+)$", r"raise \1(\2)", "raise E, msg -> raise E(msg)"),
        (r"\bxrange\(", "range(", "xrange -> range"),
        (r"\.iteritems\(\)", ".items()", "iteritems -> items"),
        (r"\.itervalues\(\)", ".values()", ""), (r"\.iterkeys\(\)", ".keys()", ""),
        (r"([\w.\]\)]+)\.has_key\(([^()]+)\)", r"(\2 in \1)", "has_key -> in"),
        (r"\bunicode\(", "str(", ""), (r"\braw_input\(", "input(", ""),
    ]
    for pat, rep, msg in rules:
        text, n = re.subn(pat, rep, text, flags=re.M)
        if n and msg:
            done.append(f"{msg} ({n}x)")
    # GNU Radio 3.7 Python API -> 3.10: trellis metric types became enums (200/201/202 -> digital.TRELLIS_*)
    metric = {"200": "digital.TRELLIS_EUCLIDEAN", "201": "digital.TRELLIS_HARD_SYMBOL", "202": "digital.TRELLIS_HARD_BIT"}
    text, n = re.subn(r"(trellis\.(?:viterbi_combined_\w+|metrics_\w+|sccc_decoder_combined_\w+|"
                      r"pccc_decoder_combined_\w+)\((?:[^()]|\([^()]*\))*?,\s*)(20[012])(\s*[,)])",
                      lambda m: m.group(1) + metric[m.group(2)] + m.group(3), text)
    if n:
        done.append(f"trellis metric type 200/201/202 -> digital.TRELLIS_* ({n}x)")
        if not re.search(r"^\s*from gnuradio import .*\bdigital\b", text, re.M):
            m = re.search(r"^(\s*from gnuradio import [\w ,]+?)\s*$", text, re.M)
            if m:  # add it to the existing "from gnuradio import ..." line
                text = text[:m.start(1)] + m.group(1) + ", digital" + text[m.end(1):]
            else:
                m = re.search(r"^(?:from|import)\s.*$", text, re.M)
                text = (text[:m.end()] + "\nfrom gnuradio import digital" + text[m.end():]) if m else \
                    "from gnuradio import digital\n" + text
    return text, done


def fix_module_imports(text: str, name: str, local: set[str]) -> tuple[str, list[str]]:
    """3.7/3.8 modules imported themselves as top-level `lte` / `lte_swig` and their own files without a dot."""
    done = []
    rules = [
        (rf"^(\s*)import\s+{name}_swig\s+as\s+(\w+)\s*$", rf"\1from gnuradio import {name} as \2"),
        (rf"^(\s*)import\s+{name}_swig\s*$", rf"\1from gnuradio import {name} as {name}_swig"),
        (rf"^(\s*)from\s+{name}_swig\s+import\s+", rf"\1from gnuradio.{name} import "),
        (rf"^(\s*)import\s+{name}\s*$", rf"\1from gnuradio import {name}"),
        (rf"^(\s*)import\s+{name}\s+as\s+(\w+)\s*$", rf"\1from gnuradio import {name} as \2"),
        (rf"^(\s*)from\s+{name}\s+import\s+", rf"\1from gnuradio.{name} import "),
    ]
    for pat, rep in rules:
        text, n = re.subn(pat, rep, text, flags=re.M)
        if n:
            done.append(f"import of the module as '{name}' -> gnuradio.{name} ({n}x)")
    for mod in sorted(local):
        if mod in ("__init__", name):
            continue
        text, n1 = re.subn(rf"^(\s*)import\s+{mod}\s*$", rf"\1from . import {mod}", text, flags=re.M)
        text, n2 = re.subn(rf"^(\s*)from\s+{mod}\s+import\s+", rf"\1from .{mod} import ", text, flags=re.M)
        if n1 + n2:
            done.append(f"'{mod}' imported relatively ({n1 + n2}x)")
    return text, done


def port_module(src: str, an: dict, work: str, log) -> tuple[str, dict]:
    """Port a 3.7/3.8 module to the installed GNU Radio: new gr_modtool skeleton + old sources + API fixes +
    generated pybind11 bindings. -> (new source dir, report)."""
    name = an["name"]
    rep: dict = {"changes": [], "warnings": [], "blocks": []}
    dst_parent = os.path.join(work, "ported")
    shutil.rmtree(dst_parent, ignore_errors=True)
    os.makedirs(dst_parent)
    rc = log(modtool_cmd("newmod", name), cwd=dst_parent)
    dst = os.path.join(dst_parent, f"gr-{name}")
    if rc != 0 or not os.path.isdir(dst):
        raise RuntimeError("gr_modtool newmod failed - see the log")
    rep["changes"].append(f"new GNU Radio {gr_major(installed_gr_version())} module skeleton (gr_modtool newmod)")
    inc_new = os.path.join(dst, "include", "gnuradio", name)
    # headers
    old_inc = [d for d in (os.path.join(src, "include", name), os.path.join(src, "include", "gnuradio", name),
                           os.path.join(src, "include")) if os.path.isdir(d)]
    headers = []
    if old_inc:
        for h in sorted(glob.glob(os.path.join(old_inc[0], "*.h"))):
            if os.path.basename(h) == "api.h":
                continue
            t = re.sub(rf"#\s*include\s*<{name}/", f"#include <gnuradio/{name}/", _read(h))
            t, fx = apply_api_fixes(t)
            _write(os.path.join(inc_new, os.path.basename(h)), t)
            headers.append(os.path.basename(h))
            rep["changes"] += [f"{os.path.basename(h)}: {x}" for x in fx]
            rep["warnings"] += [f"{os.path.basename(h)}: {w}" for w in api_warnings(t)]
    _cmake_list_add(os.path.join(inc_new, "CMakeLists.txt"), "install(FILES", headers)
    # C++ sources
    lib_old = os.path.join(src, "lib")
    srcs = []
    for p in sorted(glob.glob(os.path.join(lib_old, "*.cc")) + glob.glob(os.path.join(lib_old, "*.cpp")) +
                    glob.glob(os.path.join(lib_old, "*.h")) + glob.glob(os.path.join(lib_old, "*.c"))):
        b = os.path.basename(p)
        if b.startswith(("qa_", "test_")) or b in ("api.h",):
            continue
        t = re.sub(rf"#\s*include\s*<{name}/", f"#include <gnuradio/{name}/", _read(p))
        t = re.sub(rf'#\s*include\s*"{name}/', f'#include "gnuradio/{name}/', t)
        t, fx = apply_api_fixes(t)
        _write(os.path.join(dst, "lib", b), t)
        if not b.endswith(".h"):
            srcs.append(b)
        rep["changes"] += [f"{b}: {x}" for x in fx]
        rep["warnings"] += [f"{b}: {w}" for w in api_warnings(t)]
    if srcs:
        _cmake_list_add(os.path.join(dst, "lib", "CMakeLists.txt"), f"list(APPEND {name}_sources", srcs)
    # extra libraries the old module linked / looked for
    old_lib_cmake = _read(os.path.join(lib_old, "CMakeLists.txt"))
    extra = []
    # every library variable the old lib/CMakeLists used (directly or through a set(..._libs ...) list), and
    # plain system libraries in its link lines - GNU Radio's own ones come with the new skeleton
    skip = r"GNURADIO|Boost|BOOST|LOG4CPP|PYTHON|VOLK|CPPUNIT|GRUEL|GR_|SWIG"
    for v in re.findall(r"\$\{(\w+_(?:LIBRARIES|LIBRARY|LIBS))\}", old_lib_cmake):
        if not re.search(skip, v) and f"${{{v}}}" not in extra:
            extra.append(f"${{{v}}}")
    for m in re.finditer(r"target_link_libraries\s*\(([^)]*)\)", old_lib_cmake):
        for tok in m.group(1).split():
            if re.match(r"^(fftw3f?|gsl|gslcblas|itpp|armadillo|liquid|m|usb-1\.0|pthread)$", tok) and tok not in extra:
                extra.append(tok)
    if extra:
        lc = os.path.join(dst, "lib", "CMakeLists.txt")
        t = _read(lc)
        t = case_fallback_snippet([v[2:-1] for v in extra if v.startswith("${")]) + t
        t2 = re.sub(rf"(target_link_libraries\s*\(\s*gnuradio-{re.escape(name)}\b[^)]*)",
                    lambda m: m.group(1) + " " + " ".join(extra), t, count=1)
        if t2 == t:
            t2 = t + f"\ntarget_link_libraries(gnuradio-{name} PUBLIC {' '.join(extra)})\n"
        _write(lc, t2)
        rep["changes"].append(f"links the extra libraries the old module used: {' '.join(extra)}")
    # the old module's own Find*.cmake modules (FindFFTW3f.cmake, ...) for its extra dependencies
    mods_new = os.path.join(dst, "cmake", "Modules")
    for f in glob.glob(os.path.join(src, "cmake", "Modules", "Find*.cmake")):
        b = os.path.basename(f)
        if re.search(r"Gnuradio|Gruel|CppUnit|Swig|Python|Volk|Log4Cpp|Doxygen", b, re.I):
            continue
        if not os.path.exists(os.path.join(mods_new, b)):
            os.makedirs(mods_new, exist_ok=True)
            _write(os.path.join(mods_new, b), _read(f))
            rep["changes"].append(f"kept the old CMake module {b}")
    old_top = _read(os.path.join(src, "CMakeLists.txt"))
    finds = [m.group(0) for m in re.finditer(r"^\s*(find_package|pkg_check_modules)\s*\([^)]*\)", old_top, re.M | re.I)
             if not re.search(r"Gnuradio|Boost|PythonLibs|PythonInterp|Doxygen|SWIG|CppUnit|Git|Log4Cpp|Volk|"
                              r"pybind11|UHD\b.*QUIET", m.group(0), re.I)]
    if finds:
        tc = os.path.join(dst, "CMakeLists.txt")
        t = _read(tc)
        t = re.sub(r"(\n\s*add_subdirectory\s*\(\s*include)", "\n# dependencies of the original module\n"
                   + "\n".join(f.strip() for f in finds) + r"\n\1", t, count=1)
        _write(tc, t)
        rep["changes"].append("kept the original's dependency lookups: " + "; ".join(f.strip() for f in finds))
    for f in finds:  # their include dirs
        m = re.search(r"\(\s*(\w+)", f)
        if m:
            lc = os.path.join(dst, "lib", "CMakeLists.txt")
            t = _read(lc)
            if "${" + m.group(1).upper() + "_INCLUDE_DIRS}" not in t:
                t += f"\nif({m.group(1).upper()}_INCLUDE_DIRS)\n  target_include_directories(gnuradio-{name} PUBLIC " \
                     f"${{{m.group(1).upper()}_INCLUDE_DIRS}})\nendif()\n"
                _write(lc, t)
    # Python
    py_new = os.path.join(dst, "python", name)
    py_old = os.path.join(src, "python", name) if os.path.isdir(os.path.join(src, "python", name)) \
        else os.path.join(src, "python")
    pys, init_lines = [], []
    local_py = {os.path.splitext(os.path.basename(x))[0] for x in glob.glob(os.path.join(py_old, "*.py"))}
    for p in sorted(glob.glob(os.path.join(py_old, "*.py"))):
        b = os.path.basename(p)
        if b.startswith(("qa_", "test_", "build_utils")):
            continue
        t, fx = py2_fix(_read(p))
        t, fx2 = fix_module_imports(t, name, local_py)
        fx += fx2
        if b == "__init__.py":
            init_lines = []  # the package's own Python blocks (py2 implicit relative imports made explicit)
            for l in t.splitlines():
                m = re.match(r"\s*from\s+\.?(\w+)\s+import\s+(.+)$", l)
                if m and m.group(1) in local_py and m.group(1) != "__init__":
                    init_lines.append(f"from .{m.group(1)} import {m.group(2).strip()}")
            continue
        _write(os.path.join(py_new, b), t)
        pys.append(b)
        rep["changes"] += [f"{b}: {x}" for x in fx]
    if pys:
        _cmake_list_add(os.path.join(py_new, "CMakeLists.txt"), "gr_python_install(", pys, after_files=True)
    if init_lines:
        ip = os.path.join(py_new, "__init__.py")
        _write(ip, _read(ip).rstrip() + "\n\n# Python blocks of the original module\n" + "\n".join(init_lines) + "\n")
        rep["changes"].append(f"{len(pys)} Python block file(s) moved into python/{name}/")
    # GRC block files
    grc_new = os.path.join(dst, "grc")
    ymls = []
    for p in sorted(glob.glob(os.path.join(src, "grc", "*.yml"))):
        _write(os.path.join(grc_new, os.path.basename(p)), fix_block_imports(_read(p), name))
        ymls.append(os.path.basename(p))
    for p in sorted(glob.glob(os.path.join(src, "grc", "*.xml"))):
        try:
            bid, y, w = xml_block_to_yml(_read(p))
        except Exception as e:
            rep["warnings"].append(f"{os.path.basename(p)}: couldn't convert ({e})")
            continue
        fn = f"{bid}.block.yml"
        _write(os.path.join(grc_new, fn), fix_block_imports(y, name))
        ymls.append(fn)
        rep["warnings"] += w
    if ymls:
        rep["changes"].append(f"{len(ymls)} GRC block file(s)" + (" converted from 3.7 XML" if an["grc_xml"] else ""))
        _cmake_list_add(os.path.join(grc_new, "CMakeLists.txt"), "install(FILES", ymls)
    # pybind11 bindings for every C++ block header
    blocks = [os.path.splitext(h)[0] for h in headers]
    rep["blocks"] = blocks
    bound, failed = [], []
    for b in blocks:
        rc = log(modtool_cmd("bind", b), cwd=dst)
        (bound if rc == 0 else failed).append(b)
    # `gr_modtool bind` only writes <block>_python.cc - registering it (what `gr_modtool add` would do) is up to
    # us: prototype + call in python_bindings.cc, the file in the bindings CMakeLists
    bdir = os.path.join(dst, "python", name, "bindings")
    have = [b for b in bound if os.path.exists(os.path.join(bdir, f"{b}_python.cc"))]
    if have:
        pb = os.path.join(bdir, "python_bindings.cc")
        t = _read(pb)
        protos = "".join(f"    void bind_{b}(py::module& m);\n" for b in have if f"bind_{b}(py::module" not in t)
        calls = "".join(f"    bind_{b}(m);\n" for b in have if f"bind_{b}(m);" not in t)
        t = re.sub(r"(// BINDING_FUNCTION_PROTOTYPES\()", lambda m: m.group(1) + "\n" + protos.rstrip("\n"), t, count=1)
        t = re.sub(r"(// BINDING_FUNCTION_CALLS\()", lambda m: m.group(1) + "\n" + calls.rstrip("\n"), t, count=1)
        _write(pb, t)
        bc = os.path.join(bdir, "CMakeLists.txt")
        t = _read(bc)
        t = re.sub(rf"list\(APPEND {re.escape(name)}_python_files\s*",
                   lambda m: m.group(0) + "\n    " + "\n    ".join(f"{b}_python.cc" for b in have) + "\n    ", t,
                   count=1)
        _write(bc, t)
    if bound:
        rep["changes"].append(f"pybind11 bindings generated and registered for {len(have)} block(s)")
    if failed:
        rep["warnings"].append(f"gr_modtool bind failed for {failed} (castxml/pygccxml missing, or a header it "
                               "can't parse) - those blocks won't be usable from Python/GRC until bound")
    if an.get("python2_code"):
        rep["warnings"].append("the Python code was Python 2: the usual changes were made automatically (print, "
                               "except/raise, xrange, tabs); integer division (/ vs //) may still need a look")
    return dst, rep


def _cmake_list_add(path: str, anchor: str, names: list[str], after_files: bool = False) -> None:
    """Add file names to a list in a generated CMakeLists.txt (after `anchor`, or after its FILES keyword)."""
    if not names or not os.path.exists(path):
        return
    t = _read(path)
    m = re.search(re.escape(anchor), t, re.I)
    i = m.start() if m else -1
    if i < 0:
        t += f"\n# added by SDR Pilot's port\n{anchor}\n    " + "\n    ".join(names) + "\n)\n"
    else:
        j = i + len(anchor)
        if "FILES" in t[i:i + 400] and (after_files or anchor.lower().startswith("install")):
            j = t.index("FILES", i) + len("FILES")
        new = [n for n in names if n not in t[i:t.find(")", j) if t.find(")", j) > 0 else len(t)]]
        t = t[:j] + "\n    " + "\n    ".join(new) + t[j:]
    _write(path, t)


# ---------------------------------------------------------------------------------------------- python-only install
def install_python_only(src: str, an: dict, log) -> dict:
    """Copy the module's Python package and block files into radioconda - no CMake, no compiler."""
    name = an["name"]
    pyd = os.path.join(python_dir(), "gnuradio", name)
    os.makedirs(pyd, exist_ok=True)
    py_old = os.path.join(src, "python", name) if os.path.isdir(os.path.join(src, "python", name)) \
        else os.path.join(src, "python")
    n = 0
    init = ""
    for p in sorted(glob.glob(os.path.join(py_old, "*.py"))):
        b = os.path.basename(p)
        if b.startswith(("qa_", "test_", "build_utils")):
            continue
        t = _read(p)
        if b == "__init__.py":
            local_py = {os.path.splitext(os.path.basename(x))[0] for x in glob.glob(os.path.join(py_old, "*.py"))}
            keep = []
            for l in t.splitlines():
                m = re.match(r"\s*from\s+\.?(\w+)\s+import\s+(.+)$", l)
                if m and m.group(1) in local_py and "_swig" not in l:
                    keep.append(f"from .{m.group(1)} import {m.group(2).strip()}")
            init = "\n".join(keep) + "\n"
            continue
        t, _fx = py2_fix(_read(p))
        t, _fx = fix_module_imports(t, name, {os.path.splitext(os.path.basename(x))[0]
                                              for x in glob.glob(os.path.join(py_old, "*.py"))})
        _write(os.path.join(pyd, b), t)
        n += 1
    _write(os.path.join(pyd, "__init__.py"), init or "")
    gd = grc_blocks_dir()
    os.makedirs(gd, exist_ok=True)
    g = 0
    for p in sorted(glob.glob(os.path.join(src, "grc", "*.yml"))):
        _write(os.path.join(gd, os.path.basename(p)), fix_block_imports(_read(p), name))
        g += 1
    for p in sorted(glob.glob(os.path.join(src, "grc", "*.xml"))):
        bid, y, _w = xml_block_to_yml(_read(p))
        _write(os.path.join(gd, f"{bid}.block.yml"), fix_block_imports(y, name))
        g += 1
    log(None, note=f"copied {n} Python file(s) to {pyd} and {g} block file(s) to {gd}")
    return {"python_dir": pyd, "python_files": n, "block_files": g}


# ---------------------------------------------------------------------------------------------- verify
def verify(name: str, env: dict | None = None) -> dict:
    """Does `import gnuradio.<name>` work in a fresh Python, and are its blocks in GRC's block folder?"""
    code = (f"import gnuradio.{name} as m, json; print('SPV' + json.dumps(sorted(x for x in dir(m) "
            "if not x.startswith('_'))[:80]))")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        out = r.stdout + r.stderr
    except Exception as e:
        out, r = str(e), None
    m = re.search(r"SPV(\[.*\])", out)
    blocks = sorted(os.path.basename(p) for p in glob.glob(os.path.join(grc_blocks_dir(), f"{name}_*.block.yml")))
    blocks += sorted(os.path.basename(p) for p in glob.glob(os.path.join(grc_blocks_dir(), f"{name}*.yml"))
                     if os.path.basename(p) not in blocks)
    res = {"imports": bool(m), "python_names": json.loads(m.group(1)) if m else [], "grc_block_files": blocks[:60]}
    if not m:
        res["import_error"] = "\n".join(out.strip().splitlines()[-6:])
    return res


# ---------------------------------------------------------------------------------------------- errors
_OPTIONAL = r"Doxygen|Git|CppUnit|Sphinx|LATEX|Dot|Thrift|Gcov|PythonLibs"


def error_summary(log_text: str, limit: int = 14) -> list[str]:
    """The lines of a build log that say what went wrong (compiler errors, CMake errors, missing packages) -
    one line per distinct problem."""
    pats = [r"error[: ]", r"CMake Error", r"Could NOT find", r"No such file or directory", r"undefined reference",
            r"unresolved external", r"fatal error", r"not found", r"PackagesNotFoundError", r"ModuleNotFoundError",
            r"ImportError"]
    out, kinds = [], set()
    for line in log_text.splitlines():
        t = line.strip()
        if not t or t.startswith(("#", "$")) or "warning" in t.lower() or re.search(rf"Could NOT find ({_OPTIONAL})", t) \
                or re.search(r"Could NOT find (GMP|PkgConfig)|not a problem if|Failed to run clang-format", t):
            continue
        if re.match(r"^\d+ errors? generated", t) or t.startswith("ninja: build stopped"):
            continue
        if any(re.search(p, t, re.I) for p in pats):
            kind = re.sub(r"^.*?(error\b|Error\b)", r"\1", t)
            kind = re.sub(r"'[^']*'|‘[^’]*’|\d+", "_", kind)[:120]
            if kind in kinds and len(out) > 4:
                continue
            kinds.add(kind)
            if t not in out:
                out.append(t[:300])
    return out[:limit]


ADVICE = [
    (rf"Could NOT find (?!(?:{_OPTIONAL}|GMP|PkgConfig)\b)(\w+)|By not providing \"Find(\w+)\.cmake\"",
     lambda m: f"missing dependency {m.group(1) or m.group(2)} - oot_build(action='deps', packages=[...]) installs "
               "conda packages, then action='rebuild'"),
    (r"boost/[\w/]+\.hpp.*No such file|boost/[\w/]+\.hpp.*cannot open",
     lambda m: "Boost headers missing: oot_build(action='deps', packages=['libboost-devel'])"),
    (r"fft_complex\b", lambda m: "3.10 split gr::fft::fft_complex into fft_complex_fwd / fft_complex_rev"),
    (r"get_initial_sptr", lambda m: "replace gnuradio::get_initial_sptr(new X(a)) with gnuradio::make_block_sptr<X>(a)"),
    (r"GR_LOG_DECLARE_LOGPTR|GR_LOG_ASSIGN_LOGPTR", lambda m: "remove the old logger macros; use d_logger->info(...)"),
    (r"No CMAKE_CXX_COMPILER could be found|cl\.exe|CMAKE_C_COMPILER not set",
     lambda m: "no C++ compiler: on Windows the user must install Visual Studio Build Tools (Desktop development "
               "with C++) - ask with user_action"),
    (r"swig\.swg|%module|SWIG_", lambda m: "SWIG is a 3.8-era binding system: this module needs the port "
                                           "(oot_install with port='auto')"),
    (r"unknown type name 'gr_complex'|‘gr_complex’ (was not declared|does not name)",
     lambda m: "add #include <gnuradio/gr_complex.h> to the header that uses gr_complex"),
    (r"(\w+)_(c|f|cc|ff|ccf|fff)\.h['’]? file not found|(\w+)_(c|f|cc|ff)\.h: No such file",
     lambda m: "3.8 replaced per-type headers (sig_source_c.h) with templated ones (sig_source.h, class "
               "sig_source<gr_complex> / typedef sig_source_c)"),
    (r"Error occurred while running CASTXML",
     lambda m: "binding generation (castxml) failed on a header - fix the header's compile errors first, then "
               "rebuild; re-run the binding with oot_build(action='rebuild')"),
]


def advice(log_text: str) -> list[str]:
    out = []
    compiled = len(re.findall(r"Building CXX object", log_text))
    if compiled and re.search(r"error C\d+|error:|FAILED:", log_text):
        out.append(f"The C++ compiler and build tools WORK ({compiled} source files were compiled) - the errors "
                   "are in the module's own code. Don't ask the user to (re)install Visual Studio; fix the code: "
                   "oot_build(action='read') the file named in the first error, oot_build(action='patch'), then "
                   "oot_build(action='rebuild').")
    if re.search(r"formatter<boost::format", log_text):
        out.append("fmt 11 can't format boost::format in logger calls: wrap the argument, e.g. GR_LOG_INFO(d_logger, "
                   "(boost::format(\"...\") % x).str()) - current SDR Pilot ports do this automatically")
    if re.search(r"Could NOT find GMP", log_text) and re.search(r"Found MPIR|MPIR found", log_text):
        out.append("'Could NOT find GMP' is harmless here: GNU Radio uses MPIR instead (found)")
    for pat, fn in ADVICE:
        m = re.search(pat, log_text, re.I)
        if m:
            t = fn(m)
            if t not in out:
                out.append(t)
    return out


_HASH_RE = re.compile(r"BINDTOOL_HEADER_FILE_HASH\(\s*([0-9a-fA-F]{32})\s*\)")
_HFILE_RE = re.compile(r"BINDTOOL_HEADER_FILE\(\s*([^)\s]+)\s*\)")


def sync_binding_hashes(src: str) -> list[str]:
    """GNU Radio's CMake compares each pybind11 binding file's BINDTOOL_HEADER_FILE_HASH with the MD5 of its header
    and stops with 'Python bindings for X.h are out of sync' when they differ. Two harmless causes are fixed here:
    line endings (git/zip tools on Windows turn LF into CRLF - the header is restored to LF), and headers SDR Pilot's
    own porter edited (API fixes - the stored hash is updated; any real mismatch then shows as a compile error, which
    oot_build can fix). Returns what was changed."""
    import hashlib
    out = []
    for cc in glob.glob(os.path.join(src, "python", "**", "bindings", "*_python.cc"), recursive=True):
        try:
            with open(cc, "r", encoding="utf-8", errors="surrogateescape", newline="") as f:
                text = f.read()
        except OSError:
            continue
        mh, mf = _HASH_RE.search(text), _HFILE_RE.search(text)
        if not mh or not mf or mf.group(1) in ("None", "") or mh.group(1) == "0":
            continue
        want = mh.group(1).lower()
        name = mf.group(1)
        hdr = None
        for cand in glob.glob(os.path.join(src, "include", "**", name), recursive=True):
            hdr = cand
            break
        if not hdr:
            continue
        with open(hdr, "rb") as f:
            raw = f.read()
        if hashlib.md5(raw).hexdigest() == want:
            continue
        lf = raw.replace(b"\r\n", b"\n")
        if hashlib.md5(lf).hexdigest() == want:
            with open(hdr, "wb") as f:
                f.write(lf)
            out.append(f"{name}: line endings restored")
            continue
        new = hashlib.md5(raw).hexdigest()
        text2 = text[:mh.start(1)] + new + text[mh.end(1):]
        with open(cc, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text2)
        out.append(f"{name}: header changed since the bindings were made - hash updated")
    return out
