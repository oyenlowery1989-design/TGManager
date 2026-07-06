#!/usr/bin/env python3
"""
Telegram Manager - Native macOS Window (FALLBACK ONLY)

NOT USED when the compiled Swift launcher exists (the normal case) —
launcher.sh builds and prefers launcher_swift; this PyObjC window is the
fallback if Swift compilation fails. It duplicates the config-path logic of
launcher.sh/launcher.swift; keep the three in sync if paths ever change.

Uses PyObjC + WebKit to create a real app window (not a browser tab).
"""

import sys
import os
import subprocess
import threading

# Add the Resources directory to path
DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)

import objc
from Foundation import NSObject, NSURL, NSURLRequest, NSApplication, NSApp, NSTimer
from AppKit import (
    NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
    NSBackingStoreBuffered, NSScreen, NSApplicationActivationPolicyRegular,
    NSImage, NSMenu, NSMenuItem, NSColor
)
import WebKit

def _read_port():
    """Read port from manager_config.json (same resolution as server.py ROOT_DIR logic)."""
    config_path = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "manager_config.json")
    )
    try:
        import json as _j
        with open(config_path) as f:
            return _j.load(f).get("port", 8477)
    except Exception:
        return 8477

PORT = _read_port()
URL = f"http://127.0.0.1:{PORT}"


class AppDelegate(NSObject):
    window = objc.ivar()
    webview = objc.ivar()
    server_started = objc.ivar()

    def applicationDidFinishLaunching_(self, notification):
        # Start the server in a background thread
        self.server_started = False
        server_thread = threading.Thread(target=self.start_server, daemon=True)
        server_thread.start()

        # Create the window
        screen = NSScreen.mainScreen().frame()
        width = 1100
        height = 750
        x = (screen.size.width - width) / 2
        y = (screen.size.height - height) / 2

        frame = ((x, y), (width, height))
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)

        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("Telegram Manager")
        self.window.setMinSize_((800, 500))
        self.window.setDelegate_(self)
        self.window.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(
            15/255, 15/255, 15/255, 1.0
        ))

        # Set app icon
        icon_path = os.path.join(DIR, "AppIcon.png")
        if os.path.exists(icon_path):
            icon = NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon:
                NSApp.setApplicationIconImage_(icon)

        # Create WebView
        config = WebKit.WKWebViewConfiguration.alloc().init()
        self.webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            ((0, 0), (width, height)), config
        )
        self.webview.setAutoresizingMask_(18)  # Flexible width + height

        self.window.setContentView_(self.webview)
        self.window.makeKeyAndOrderFront_(None)

        # Wait for server then load URL
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.3, self, "checkServer:", None, True
        )

    def checkServer_(self, timer):
        import urllib.request
        try:
            urllib.request.urlopen(URL, timeout=0.5)
            timer.invalidate()
            url = NSURL.URLWithString_(URL)
            request = NSURLRequest.requestWithURL_(url)
            self.webview.loadRequest_(request)
        except Exception:
            pass  # Server not ready yet, timer will retry

    def start_server(self):
        import server
        # server.py reads manager_config.json on import; don't override the port it found.
        # Use server's own ThreadedHTTPServer (not a plain HTTPServer) so a slow
        # scan_accounts() walk doesn't block every other concurrent request —
        # same reason server.py's own __main__ entrypoint uses it.
        httpd = server.ThreadedHTTPServer(("127.0.0.1", PORT), server.RequestHandler)
        os.chdir(DIR)
        self.server_started = True
        httpd.serve_forever()

    def windowShouldClose_(self, sender):
        # Hide instead of closing — keeps the server alive, matching Swift launcher behavior
        self.window.orderOut_(None)
        return False

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        # Don't quit when the window is hidden — user can reopen via Dock or re-launch
        return False

    def applicationWillTerminate_(self, notification):
        # Kill any server on our port
        result = subprocess.run(["lsof", f"-ti:{PORT}"], capture_output=True, text=True)
        for pid in result.stdout.split():
            subprocess.run(["kill", "-9", pid], capture_output=True)


def main():
    # Kill any existing instance on this port
    result = subprocess.run(["lsof", f"-ti:{PORT}"], capture_output=True, text=True)
    for pid in result.stdout.split():
        subprocess.run(["kill", "-9", pid], capture_output=True)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    # Create a basic menu bar
    menubar = NSMenu.alloc().init()
    app_menu_item = NSMenuItem.alloc().init()
    menubar.addItem_(app_menu_item)
    app.setMainMenu_(menubar)

    app_menu = NSMenu.alloc().init()
    quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Quit Telegram Manager", "terminate:", "q"
    )
    app_menu.addItem_(quit_item)
    app_menu_item.setSubmenu_(app_menu)

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.activateIgnoringOtherApps_(True)
    app.run()


if __name__ == "__main__":
    main()
