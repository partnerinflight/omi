import AppKit
import CaptureCore

/// Menu-bar icon: idle, recording (red), uploads pending, or error.
final class StatusMenu: NSObject {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let controller: CaptureController

    init(controller: CaptureController) {
        self.controller = controller
        super.init()
        controller.onChange = { [weak self] in self?.refresh() }
        refresh()
    }

    func refresh() {
        let error = controller.captureError ?? controller.uploadError
        let symbol: String
        let title: String
        if let app = controller.recordingApp {
            symbol = "record.circle.fill"
            title = "Recording \(app)"
        } else if controller.captureError == nil, controller.micAuthorized, controller.uploadError != nil {
            symbol = "exclamationmark.triangle"
            title = "Idle — upload problem"
        } else if error != nil || !controller.micAuthorized {
            symbol = "exclamationmark.triangle"
            title = "Not recording"
        } else if controller.pendingUploads > 0 {
            symbol = "arrow.up.circle"
            title = "Idle"
        } else {
            symbol = "waveform"
            title = controller.isPaused ? "Paused" : "Idle"
        }
        item.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)
        item.button?.contentTintColor = controller.recordingApp == nil ? nil : .systemRed

        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: title, action: nil, keyEquivalent: ""))
        if !controller.micAuthorized {
            menu.addItem(NSMenuItem(title: "Microphone permission missing", action: nil, keyEquivalent: ""))
        }
        if controller.pendingUploads > 0 {
            menu.addItem(NSMenuItem(title: "\(controller.pendingUploads) uploads pending", action: nil, keyEquivalent: ""))
        }
        if controller.failedCaptures > 0 {
            menu.addItem(NSMenuItem(title: "\(controller.failedCaptures) captures failed to upload", action: nil, keyEquivalent: ""))
        }
        if controller.spoolOverLimit {
            menu.addItem(NSMenuItem(title: "Spool is over the size limit", action: nil, keyEquivalent: ""))
        }
        if let error {
            menu.addItem(NSMenuItem(title: String(error.prefix(120)), action: nil, keyEquivalent: ""))
        }
        menu.addItem(.separator())
        if controller.recordingApp != nil {
            add(menu, "Skip this meeting", #selector(skip))
        }
        if controller.isPaused {
            add(menu, "Resume capture", #selector(resume))
        } else {
            add(menu, "Pause for 1 hour", #selector(pause))
        }
        add(menu, "Open spool folder", #selector(openSpool))
        menu.addItem(.separator())
        add(menu, "Quit", #selector(quit))
        item.menu = menu
    }

    private func add(_ menu: NSMenu, _ title: String, _ action: Selector) {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: "")
        entry.target = self
        menu.addItem(entry)
    }

    @objc private func skip() { controller.skipMeeting() }
    @objc private func pause() { controller.pauseForAnHour() }
    @objc private func resume() { controller.resume() }
    @objc private func openSpool() { NSWorkspace.shared.open(controller.spool.root) }
    @objc private func quit() { NSApp.terminate(nil) }
}
