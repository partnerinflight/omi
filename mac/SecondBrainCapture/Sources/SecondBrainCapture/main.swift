import AppKit
import AVFoundation
import ServiceManagement
import UserNotifications

if CommandLine.arguments.contains("--probe") {
    Probe.run(CommandLine.arguments)
    exit(0)
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: CaptureController?
    private var menu: StatusMenu?

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let controller = try CaptureController()
            self.controller = controller
            menu = StatusMenu(controller: controller)
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert]) { _, _ in }
            try? SMAppService.mainApp.register()  // start at login
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async {
                    controller.micAuthorized = granted
                    self.menu?.refresh()
                    controller.start()
                }
            }
        } catch {
            let alert = NSAlert()
            alert.messageText = "SecondBrainCapture could not start"
            alert.informativeText = "\(error)"
            alert.runModal()
            NSApp.terminate(nil)
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
