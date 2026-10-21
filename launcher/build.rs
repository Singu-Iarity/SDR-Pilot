fn main() {
    // exe icon + version info (what Explorer, Start search and a pinned taskbar button show)
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut res = winresource::WindowsResource::new();
        res.set_icon("../sdrpilot/sdrpilot.ico")
            .set("ProductName", "SDR Pilot")
            .set("FileDescription", "SDR Pilot")
            .set("CompanyName", "SDR Pilot")
            .set("LegalCopyright", "MIT License");
        if let Err(e) = res.compile() {
            println!("cargo:warning=icon resource not embedded: {e}");
        }
    }
    println!("cargo:rerun-if-changed=../sdrpilot/sdrpilot.ico");
}
