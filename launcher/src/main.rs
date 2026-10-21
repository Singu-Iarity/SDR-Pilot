// SDR Pilot desktop window: a small native app (Tauri's window + WebView2 engine: tao + wry) that shows the
// dashboard served by the SDR Pilot host. Being its own .exe with its own icon is what makes Windows treat it as
// an app: Start-menu search, its own taskbar button, and "Pin to taskbar" pins SDR Pilot (not Edge/Chrome).
//
//   "SDR Pilot.exe"            find the running host (start it if needed) and show the dashboard
//   "SDR Pilot.exe <url>"      show that dashboard URL
// A second launch just brings the open window to the front.
#![windows_subsystem = "windows"]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use tao::dpi::LogicalSize;
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder};
use tao::window::{Icon, WindowBuilder};
use wry::{NewWindowResponse, WebContext, WebViewBuilder};

enum Msg {
    Load(String),
    Failed(String),
    Show,
}

const TITLE: &str = "SDR Pilot";

fn app_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn read_json(p: &Path) -> serde_json::Value {
    std::fs::read_to_string(p)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(serde_json::Value::Null)
}

fn http_get(port: u16, path: &str, timeout: Duration) -> Option<String> {
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let mut s = TcpStream::connect_timeout(&addr, timeout).ok()?;
    s.set_read_timeout(Some(timeout)).ok()?;
    s.set_write_timeout(Some(timeout)).ok()?;
    write!(s, "GET {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n").ok()?;
    let mut buf = String::new();
    s.read_to_string(&mut buf).ok()?;
    let (head, body) = buf.split_once("\r\n\r\n")?;
    if !head.starts_with("HTTP/1.") || !head.split_whitespace().nth(1).is_some_and(|c| c == "200") {
        return None;
    }
    Some(body.to_string())
}

/// The running host's dashboard URL, if one answers on the configured port range.
fn find_host(base_port: u16) -> Option<String> {
    for port in base_port..base_port.saturating_add(10) {
        let Some(body) = http_get(port, "/api/sessions", Duration::from_millis(800)) else { continue };
        let Ok(d) = serde_json::from_str::<serde_json::Value>(&body) else { continue };
        if d["kind"] == "host" {
            return Some(format!("http://127.0.0.1:{port}/"));
        }
        if let Some(list) = d["sessions"].as_array() {
            for s in list {
                if s["kind"] == "host" && s["alive"].as_bool().unwrap_or(true) {
                    if let Some(u) = s["dashboard_url"].as_str() {
                        return Some(u.to_string());
                    }
                }
            }
        }
    }
    None
}

fn python_exe(dir: &Path) -> Option<PathBuf> {
    let cfg = read_json(&dir.join("launcher.json"));
    let mut cands: Vec<PathBuf> = Vec::new();
    if let Some(p) = cfg["python"].as_str() {
        cands.push(PathBuf::from(p));
    }
    for var in ["USERPROFILE", "LOCALAPPDATA", "ProgramData"] {
        if let Ok(v) = std::env::var(var) {
            cands.push(Path::new(&v).join("radioconda").join("python.exe"));
        }
    }
    cands.into_iter().find(|p| p.exists())
}

/// Start the host through sdrpilot_app.py (same logic the Python launcher uses) and return its URL.
fn start_host(dir: &Path) -> Result<String, String> {
    let py = python_exe(dir).ok_or(
        "Couldn't find radioconda's python.exe. Run the SDR Pilot installer again (Repair), or put \
         {\"python\": \"C:\\\\path\\\\to\\\\python.exe\"} in launcher.json next to SDR Pilot.exe.",
    )?;
    let mut cmd = std::process::Command::new(&py);
    cmd.arg(dir.join("sdrpilot_app.py")).arg("url").current_dir(dir);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let out = cmd.output().map_err(|e| format!("Couldn't run {}: {e}", py.display()))?;
    let text = String::from_utf8_lossy(&out.stdout);
    if let Some(u) = text.lines().rev().map(str::trim).find(|l| l.starts_with("http")) {
        return Ok(u.to_string());
    }
    let err = String::from_utf8_lossy(&out.stderr);
    let tail: String = err.lines().rev().take(6).collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>().join("\n");
    Err(format!(
        "The SDR Pilot host didn't start. Details are in workspace\\logs\\host_console.log.\n{}",
        if tail.is_empty() { text.trim().to_string() } else { tail }
    ))
}

fn is_local(url: &str) -> bool {
    let lower = url.to_ascii_lowercase();
    if !(lower.starts_with("http://") || lower.starts_with("https://")) {
        return true; // about:, data:, blob: ...
    }
    let rest = lower.split_once("://").map(|x| x.1).unwrap_or("");
    let host = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host = host.rsplit_once('@').map(|x| x.1).unwrap_or(host);
    let name = if host.starts_with('[') { host.split(']').next().unwrap_or("").trim_start_matches('[') } else { host.split(':').next().unwrap_or("") };
    matches!(name, "127.0.0.1" | "localhost" | "::1")
}

#[cfg(windows)]
fn open_external(url: &str) {
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
    let w = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<u16>>();
    let (op, u) = (w("open"), w(url));
    unsafe {
        ShellExecuteW(std::ptr::null_mut(), op.as_ptr(), u.as_ptr(), std::ptr::null(), std::ptr::null(), SW_SHOWNORMAL);
    }
}
#[cfg(not(windows))]
fn open_external(url: &str) {
    let _ = std::process::Command::new("xdg-open").arg(url).spawn();
}

/// Single instance: returns false (after asking the open window to come forward) if SDR Pilot is already open.
#[cfg(windows)]
fn first_instance(proxy: tao::event_loop::EventLoopProxy<Msg>) -> bool {
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS};
    use windows_sys::Win32::System::Threading::{
        CreateEventW, CreateMutexW, OpenEventW, SetEvent, WaitForSingleObject, EVENT_MODIFY_STATE, INFINITE,
    };
    let w = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<u16>>();
    let (mname, ename) = (w("Local\\SDRPilotDesktopWindow"), w("Local\\SDRPilotDesktopWindowShow"));
    unsafe {
        let _m = CreateMutexW(std::ptr::null(), 0, mname.as_ptr());
        if GetLastError() == ERROR_ALREADY_EXISTS {
            let ev = OpenEventW(EVENT_MODIFY_STATE, 0, ename.as_ptr());
            if !ev.is_null() {
                SetEvent(ev);
            }
            return false;
        }
        let ev = CreateEventW(std::ptr::null(), 0, 0, ename.as_ptr()) as usize;
        if ev != 0 {
            std::thread::spawn(move || loop {
                WaitForSingleObject(ev as _, INFINITE);
                if proxy.send_event(Msg::Show).is_err() {
                    break;
                }
            });
        }
    }
    true
}
#[cfg(not(windows))]
fn first_instance(_: tao::event_loop::EventLoopProxy<Msg>) -> bool {
    true
}

fn icon(bytes: &[u8], n: u32) -> Option<Icon> {
    Icon::from_rgba(bytes.to_vec(), n, n).ok()
}

const LOADING: &str = r#"<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#0f1218;color:#d6dde8;font:15px system-ui,Segoe UI,sans-serif}
.c{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;padding:0 24px;text-align:center}
.s{width:34px;height:34px;border:3px solid #2a3342;border-top-color:#4ea1ff;border-radius:50%;animation:r 0.9s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
pre{white-space:pre-wrap;max-width:760px;text-align:left;background:#171c25;padding:12px 14px;border-radius:8px;color:#f0b4b4}
button{background:#2b6fd6;color:#fff;border:0;border-radius:6px;padding:8px 16px;font:inherit;cursor:pointer}
</style></head><body><div class="c"><div class="s" id="s"></div><div id="m">Starting SDR Pilot&hellip;</div></div>
<script>function fail(t){document.getElementById('s').style.display='none';
document.getElementById('m').innerHTML='<b>SDR Pilot couldn\u2019t start</b><pre></pre><button onclick="location.reload()">Try again</button>';
document.querySelector('pre').textContent=t;}</script></body></html>"#;

fn main() {
    let dir = app_dir();
    let arg_url = std::env::args().nth(1).filter(|a| a.starts_with("http://") || a.starts_with("https://"));

    let event_loop = EventLoopBuilder::<Msg>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    if !first_instance(proxy.clone()) {
        return;
    }

    let mut wb = WindowBuilder::new()
        .with_title(TITLE)
        .with_inner_size(LogicalSize::new(1500.0, 950.0))
        .with_min_inner_size(LogicalSize::new(640.0, 420.0))
        .with_window_icon(icon(include_bytes!("icon32.rgba"), 32));
    #[cfg(windows)]
    {
        use tao::platform::windows::WindowBuilderExtWindows;
        wb = wb.with_taskbar_icon(icon(include_bytes!("icon256.rgba"), 256)).with_window_classname("SDRPilotDesktop");
    }
    let window = wb.build(&event_loop).expect("window");

    let data_dir = std::env::var("LOCALAPPDATA")
        .map(|v| Path::new(&v).join("SDR Pilot").join("WebView2"))
        .unwrap_or_else(|_| dir.join("webview-data"));
    let _ = std::fs::create_dir_all(&data_dir);
    let mut ctx = WebContext::new(Some(data_dir));

    let webview = WebViewBuilder::new_with_web_context(&mut ctx)
        .with_html(LOADING)
        .with_hotkeys_zoom(true)
        .with_devtools(cfg!(debug_assertions))
        .with_navigation_handler(|url| {
            if is_local(&url) {
                true
            } else {
                open_external(&url);
                false
            }
        })
        .with_new_window_req_handler(|url, _| {
            if is_local(&url) {
                NewWindowResponse::Allow
            } else {
                open_external(&url);
                NewWindowResponse::Deny
            }
        })
        .build(&window);
    let webview = match webview {
        Ok(w) => w,
        Err(e) => {
            #[cfg(windows)]
            {
                use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR};
                let w = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<u16>>();
                let msg = w(&format!(
                    "SDR Pilot needs the Microsoft Edge WebView2 Runtime (built into Windows 10/11).\n\
                     Install it from https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n({e})"
                ));
                unsafe { MessageBoxW(std::ptr::null_mut(), msg.as_ptr(), w(TITLE).as_ptr(), MB_ICONERROR) };
            }
            eprintln!("{e}");
            return;
        }
    };

    // find / start the host off the UI thread
    {
        let proxy = proxy.clone();
        let dir = dir.clone();
        std::thread::spawn(move || {
            let msg = if let Some(u) = arg_url {
                Msg::Load(u)
            } else {
                let port = read_json(&dir.join("sdrpilot_config.json"))["dashboard_port"].as_u64().unwrap_or(8765) as u16;
                match find_host(port) {
                    Some(u) => Msg::Load(u),
                    None => match start_host(&dir) {
                        Ok(u) => Msg::Load(u),
                        Err(e) => {
                            // the logon service may be bringing it up right now - give it a moment
                            let t = Instant::now();
                            let mut found = None;
                            while t.elapsed() < Duration::from_secs(20) {
                                if let Some(u) = find_host(port) {
                                    found = Some(u);
                                    break;
                                }
                                std::thread::sleep(Duration::from_millis(700));
                            }
                            found.map(Msg::Load).unwrap_or(Msg::Failed(e))
                        }
                    },
                }
            };
            let _ = proxy.send_event(msg);
        });
    }

    event_loop.run(move |event, _, flow| {
        *flow = ControlFlow::Wait;
        match event {
            Event::UserEvent(Msg::Load(u)) => {
                let _ = webview.load_url(&u);
            }
            Event::UserEvent(Msg::Failed(e)) => {
                let _ = webview.evaluate_script(&format!("fail({})", serde_json::Value::String(e)));
            }
            Event::UserEvent(Msg::Show) => {
                window.set_minimized(false);
                window.set_visible(true);
                window.set_focus();
            }
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } => *flow = ControlFlow::Exit,
            _ => {}
        }
    });
}
