import Cocoa
import WebKit
import Foundation

func readSessionToken() -> String {
    if let envToken = ProcessInfo.processInfo.environment["TG_SESSION_TOKEN"], !envToken.isEmpty {
        return envToken
    }
    return UUID().uuidString.replacingOccurrences(of: "-", with: "")
}

// ── App Delegate ─────────────────────────────────────────────────────────────
class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {

    var window: NSWindow!
    var webView: WKWebView!
    var serverProcess: Process?
    var serverLog: FileHandle?
    var statusItem: NSStatusItem?
    var sessionToken = readSessionToken()
    var readyFileURL: URL?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        prepareReadyFile()
        startPythonServer()
        createWindow()
        setupMenuBar()
        waitForServer(attempt: 0)
    }

    // ── Python HTTP server ───────────────────────────────────────────────────
    func prepareReadyFile() {
        let name = "telegram-manager-\(UUID().uuidString).ready"
        let url = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent(name)
        try? FileManager.default.removeItem(at: url)
        readyFileURL = url
    }

    func serverURLFromReadyFile() -> URL? {
        guard let readyFileURL,
              let data = try? Data(contentsOf: readyFileURL),
              let record = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let token = record["session_token"] as? String, token == sessionToken,
              let port = record["port"] as? Int, (1024...65535).contains(port) else {
            return nil
        }
        return URL(string: "http://127.0.0.1:\(port)/\(sessionToken)/")
    }

    func startPythonServer() {
        guard let res = Bundle.main.resourcePath, let readyFileURL else { return }
        let script = "\(res)/server.py"
        let dataURL = Bundle.main.bundleURL.deletingLastPathComponent()
            .appendingPathComponent("data/manager.log")
        FileManager.default.createFile(atPath: dataURL.path, contents: nil)
        if let log = try? FileHandle(forWritingTo: dataURL) {
            log.seekToEndOfFile()
            serverLog = log
        }

        func tryLaunch(python: String) -> Bool {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = [script]
            p.environment = ProcessInfo.processInfo.environment.merging([
                "TG_SESSION_TOKEN": sessionToken,
                "TG_READY_FILE": readyFileURL.path,
            ]) { _, new in new }
            p.standardOutput = serverLog ?? FileHandle.nullDevice
            p.standardError  = serverLog ?? FileHandle.nullDevice
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
            DispatchQueue.main.async { [weak self] in
                self?.showServerError("The local server did not publish readiness in time.")
            }
            return
        }
        guard let process = serverProcess else {
            showServerError("Python could not be launched. Check data/manager.log.")
            return
        }
        guard process.isRunning else {
            showServerError("Python exited before the local server became ready. Check data/manager.log.")
            return
        }
        guard let url = serverURLFromReadyFile() else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                self.waitForServer(attempt: attempt + 1)
            }
            return
        }
        let task = URLSession.shared.dataTask(with: url) { [weak self] _, response, _ in
            guard let self = self else { return }
            if (response as? HTTPURLResponse)?.statusCode == 200 {
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
    func showServerError(_ reason: String) {
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
          <p>\(reason)</p>
          <p>Check <code>data/manager.log</code> for details, then relaunch TelegramManager.</p>
        </div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // ── WKNavigationDelegate ─────────────────────────────────────────────────
    func webView(_ webView: WKWebView,
                 decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url,
              let local = serverURLFromReadyFile(),
              url.scheme == local.scheme,
              url.host == local.host,
              url.port == local.port,
              url.path.hasPrefix("/\(sessionToken)/") else {
            if action.targetFrame?.isMainFrame == true, let url = action.request.url {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
            return
        }
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

    // ── WKUIDelegate — native alert() dialog ──────────────────────────────────
    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText     = message
        alert.alertStyle      = .warning
        alert.addButton(withTitle: "OK")
        alert.runModal()
        completionHandler()
    }

    // ── WKUIDelegate — native prompt() dialog ─────────────────────────────────
    func webView(_ webView: WKWebView,
                 runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText     = prompt
        alert.alertStyle      = .informational
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        if alert.runModal() == .alertFirstButtonReturn {
            completionHandler(field.stringValue)
        } else {
            completionHandler(nil)
        }
    }

    // ── Lifecycle ────────────────────────────────────────────────────────────
    func applicationWillTerminate(_ notification: Notification) {
        serverProcess?.terminate()
        if let readyFileURL { try? FileManager.default.removeItem(at: readyFileURL) }
        serverLog?.closeFile()
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
