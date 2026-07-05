import Cocoa
import WebKit
import Foundation

func readSessionToken() -> String {
    if let envToken = ProcessInfo.processInfo.environment["TG_SESSION_TOKEN"], !envToken.isEmpty {
        return envToken
    }
    return UUID().uuidString.replacingOccurrences(of: "-", with: "")
}

// ── Read port from manager_config.json (fallback 8477) ───────────────────────
func readPort() -> Int {
    // ROOT_DIR is two levels up from the .app bundle: ROOT_DIR/TelegramManager.app/
    let bundleURL   = Bundle.main.bundleURL
    let rootURL     = bundleURL.deletingLastPathComponent()
    let configURL   = rootURL.appendingPathComponent("manager_config.json")
    guard let data  = try? Data(contentsOf: configURL),
          let json  = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let port  = json["port"] as? Int else {
        return 8477
    }
    return port
}

// ── Best-effort cleanup of an older TelegramManager server only ──────────────
func killExistingServer(port: Int) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/bash")
    p.arguments = ["-c", "pgrep -f 'TelegramManager.app/Contents/Resources/server.py' 2>/dev/null | xargs kill 2>/dev/null || true"]
    p.standardOutput = FileHandle.nullDevice
    p.standardError  = FileHandle.nullDevice
    try? p.run()
    p.waitUntilExit()
    Thread.sleep(forTimeInterval: 0.25)
}

// ── App Delegate ─────────────────────────────────────────────────────────────
class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {

    var window: NSWindow!
    var webView: WKWebView!
    var serverProcess: Process?
    var statusItem: NSStatusItem?
    var port = 8477
    var sessionToken = readSessionToken()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        port = readPort()
        killExistingServer(port: port)
        startPythonServer()
        createWindow()
        setupMenuBar()
        waitForServer(attempt: 0)
    }

    // ── Python HTTP server ───────────────────────────────────────────────────
    func startPythonServer() {
        guard let res = Bundle.main.resourcePath else { return }
        let script = "\(res)/server.py"

        func tryLaunch(python: String) -> Bool {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = [script]
            p.environment = ProcessInfo.processInfo.environment.merging(["TG_SESSION_TOKEN": sessionToken]) { _, new in new }
            p.standardOutput = FileHandle.nullDevice
            p.standardError  = FileHandle.nullDevice
            do {
                try p.run()
                serverProcess = p
                return true
            } catch { return false }
        }

        // Try system Python, then Homebrew Intel, then Homebrew Apple Silicon
        if !tryLaunch(python: "/usr/bin/python3") &&
           !tryLaunch(python: "/usr/local/bin/python3") {
            _ = tryLaunch(python: "/opt/homebrew/bin/python3")
        }
    }

    // ── Window ───────────────────────────────────────────────────────────────
    func createWindow() {
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)
        let w: CGFloat = min(1140, screen.width  * 0.88)
        let h: CGFloat = min( 780, screen.height * 0.88)
        let x = screen.origin.x + (screen.width  - w) / 2
        let y = screen.origin.y + (screen.height - h) / 2

        window = NSWindow(
            contentRect: NSRect(x: x, y: y, width: w, height: h),
            styleMask:   [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title           = "Telegram Manager"
        window.minSize         = NSSize(width: 820, height: 600)
        window.backgroundColor = NSColor(red: 0.059, green: 0.059, blue: 0.059, alpha: 1)
        window.delegate        = self
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        let config = WKWebViewConfiguration()
        let prefs  = WKWebpagePreferences()
        prefs.allowsContentJavaScript = true
        config.defaultWebpagePreferences = prefs

        webView = WKWebView(frame: window.contentView!.bounds, configuration: config)
        webView.autoresizingMask   = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate         = self
        webView.setValue(false, forKey: "drawsBackground")

        window.contentView?.addSubview(webView)
    }

    // ── Menu bar icon ────────────────────────────────────────────────────────
    func setupMenuBar() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let btn = statusItem?.button {
            btn.title  = "✈"
            btn.font   = NSFont.systemFont(ofSize: 14)
        }

        let menu = NSMenu()
        let showItem = NSMenuItem(title: "Show Telegram Manager",
                                   action: #selector(showWindow), keyEquivalent: "")
        showItem.target = self
        menu.addItem(showItem)
        menu.addItem(.separator())
        let quitItem = NSMenuItem(title: "Quit",
                                   action: #selector(NSApplication.terminate(_:)),
                                   keyEquivalent: "q")
        menu.addItem(quitItem)
        statusItem?.menu = menu
    }

    @objc func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // NSWindowDelegate — hide instead of closing so menu bar keeps it alive
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        window.orderOut(nil)
        return false
    }

    // ── Poll until server responds ────────────────────────────────────────────
    func waitForServer(attempt: Int) {
        guard attempt < 60 else {
            // Poll exhausted — the server never came up. Show an inline error
            // page instead of leaving a permanently blank window.
            DispatchQueue.main.async { [weak self] in
                self?.showServerError()
            }
            return
        }
        let url = URL(string: "http://127.0.0.1:\(port)/\(sessionToken)/")!
        let task = URLSession.shared.dataTask(with: url) { [weak self] _, _, error in
            guard let self = self else { return }
            if error == nil {
                DispatchQueue.main.async { self.webView.load(URLRequest(url: url)) }
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                    self.waitForServer(attempt: attempt + 1)
                }
            }
        }
        task.resume()
    }

    // ── Inline error page when the server fails to start ──────────────────────
    func showServerError() {
        let html = """
        <html><head><meta charset="utf-8"><style>
          html,body{height:100%;margin:0}
          body{display:flex;align-items:center;justify-content:center;
               background:#0f0f14;color:#e6e6e6;
               font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
          .box{max-width:420px;text-align:center;padding:32px}
          h1{font-size:20px;margin:0 0 12px}
          p{font-size:13px;line-height:1.5;color:#9aa4b2;margin:8px 0}
          code{background:rgba(255,255,255,0.08);padding:2px 6px;border-radius:4px;
               font-size:12px}
        </style></head><body><div class="box">
          <div style="font-size:40px">⚠</div>
          <h1>TelegramManager server failed to start</h1>
          <p>The local server did not respond in time.</p>
          <p>Check <code>data/manager.log</code> for details, then relaunch TelegramManager.</p>
        </div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // ── WKNavigationDelegate ─────────────────────────────────────────────────
    func webView(_ webView: WKWebView,
                 decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        decisionHandler(.allow)
    }

    // ── WKUIDelegate — native confirm() dialog ────────────────────────────────
    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText     = message
        alert.alertStyle      = .warning
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    // ── Lifecycle ────────────────────────────────────────────────────────────
    func applicationWillTerminate(_ notification: Notification) {
        serverProcess?.terminate()
    }

    // Keep running in menu bar when window is closed
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }
}

// ── Entry point ───────────────────────────────────────────────────────────────
let app = NSApplication.shared
let del = AppDelegate()
app.delegate = del
app.run()
