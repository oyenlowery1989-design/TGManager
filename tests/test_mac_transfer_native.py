"""Optional native WebKit test: synthetic page/files, no Manager server or profiles.

Run TG_TEST_NATIVE_TRANSFER=1 python3 -m unittest discover -s tests
    -p test_mac_transfer_native.py -v from a logged-in macOS desktop.
The AppDelegate source is compiled WITHOUT the Manager entry point. Only the
file chooser is replaced, so WebKit executes the real navigation/download code.
"""
import http.server
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'TelegramManager.app/Contents/Resources/launcher.swift'


class NativeTransferSourceTests(unittest.TestCase):
    def test_file_selection_and_download_remain_trusted_private_and_non_overwriting(self):
        source = SOURCE.read_text()
        self.assertIn('action.sourceFrame.isMainFrame', source)
        self.assertIn('action.sourceFrame.request.url.map(isTrustedServerURL)', source)
        self.assertIn('url.scheme == "blob"', source)
        self.assertIn('origin.host == serverURL.host', source)
        self.assertIn('origin.port == serverURL.port', source)
        self.assertIn('frame.request.url.map(isTrustedServerURL)', source)
        self.assertIn('withIntermediateDirectories: false,\n                                                       attributes: [.posixPermissions: 0o700]', source)
        self.assertIn('setAttributes([.posixPermissions: 0o600]', source)
        self.assertIn('moveItem(at: saved.file, to: saved.destination)', source)
        self.assertNotIn('removeItem(at: saved.destination)', source)


@unittest.skipUnless(os.environ.get('TG_TEST_NATIVE_TRANSFER') == '1', 'Opt-in macOS WebKit fixture')
class NativeTransferIntegrationTests(unittest.TestCase):
    def test_real_webkit_blob_save_cancel_and_existing_file(self):
        page = b'''<!doctype html><html><body>Synthetic transfer fixture<script>
        const url=URL.createObjectURL(new Blob(['{"synthetic":true}'],{type:'application/json'}));
        const a=document.createElement('a');a.href=url;a.download='synthetic.json';
        document.body.appendChild(a);setTimeout(()=>a.click(),300);
        </script></body></html>'''
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(page)))
                self.end_headers(); self.wfile.write(page)
            def log_message(self, *args):
                pass
        httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        self.addCleanup(httpd.server_close); self.addCleanup(httpd.shutdown)
        with tempfile.TemporaryDirectory(prefix='manager-transfer-native-') as tmp:
            root = Path(tmp).resolve()
            prefix = SOURCE.read_text().split('// ── Entry point')[0]
            harness = r'''
class Fixture: AppDelegate {
    let output = URL(fileURLWithPath: CommandLine.arguments[2])
    let mode = CommandLine.arguments[3]
    override func applicationDidFinishLaunching(_ notification: Notification) {
        serverURL = URL(string: CommandLine.arguments[1])!
        let origin = "http://127.0.0.1:\(serverURL!.port!)"
        precondition(isTrustedDownloadURL(URL(string: "blob:\(origin)/fixture")!))
        for bad in ["blob:https://example.com/id", "file:///tmp/secret", "data:application/json,{}",
                    "blob:http://localhost:\(serverURL!.port!)/id", "blob:http://127.0.0.1:1/id",
                    "blob:http://user@127.0.0.1:\(serverURL!.port!)/id"] {
            precondition(!isTrustedDownloadURL(URL(string: bad)!))
        }
        window = NSWindow(contentRect: NSRect(x:0,y:0,width:500,height:200),
                          styleMask:[.titled],backing:.buffered,defer:false)
        window.title = "Synthetic export fixture"
        webView = WKWebView(frame: window.contentView!.bounds)
        window.contentView!.addSubview(webView)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.load(URLRequest(url: serverURL!))
        DispatchQueue.main.asyncAfter(deadline:.now()+20) { print("FAIL: WebKit fixture timed out"); exit(2) }
    }
    override func chooseDownloadDestination(_ filename:String, completion:@escaping(URL?)->Void) {
        precondition(filename == "synthetic.json")
        if mode == "cancel" { completion(nil); print("PASS: cancelled without a file"); exit(0) }
        completion(output)
    }
    override func downloadDidFinish(_ download:WKDownload) {
        super.downloadDidFinish(download)
        guard let bytes = try? Data(contentsOf:output),
              String(data:bytes,encoding:.utf8) == "{\"synthetic\":true}",
              let attrs = try? FileManager.default.attributesOfItem(atPath:output.path),
              (attrs[.posixPermissions] as? NSNumber)?.intValue == 0o600,
              downloads.isEmpty else { print("FAIL: wrong file, permissions or staging cleanup"); exit(3) }
        print("PASS: native JSON export saved privately")
        exit(0)
    }
    override func showDownloadError(_ message:String) {
        if mode == "existing", message.contains("already exists"),
           (try? String(contentsOf:output,encoding:.utf8)) == "keep me", downloads.isEmpty {
            print("PASS: existing file preserved"); exit(0)
        }
        print("FAIL: \(message)"); exit(4)
    }
}
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let fixture = Fixture()
application.delegate = fixture
application.run()
'''
            swift = root / 'fixture.swift'; swift.write_text(prefix + harness)
            binary = root / 'fixture'
            compile_result = subprocess.run(['swiftc', '-module-cache-path', '/private/tmp/tgmanager-transfer-module-cache',
                str(swift), '-o', str(binary), '-framework', 'Cocoa', '-framework', 'WebKit', '-framework', 'Foundation'],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            for mode in ('success', 'existing', 'cancel'):
                with self.subTest(mode=mode):
                    destination = root / (mode + '.json')
                    if mode == 'existing':
                        destination.write_text('keep me')
                    result = subprocess.run([str(binary), f'http://127.0.0.1:{httpd.server_port}/fixture/', str(destination), mode],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('PASS:', result.stdout)
                    if mode == 'cancel':
                        self.assertFalse(destination.exists())
