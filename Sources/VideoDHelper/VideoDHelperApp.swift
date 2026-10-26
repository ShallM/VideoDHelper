import SwiftUI
import AppKit

@main
struct VideoDHelperApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var preferences = PreferencesStore()
    var body: some Scene {
        Window("VideoDHelper", id: "main") {
            WorkspaceView()
                .environmentObject(preferences)
                .preferredColorScheme(preferences.colorScheme)
                .frame(minWidth: 1060, minHeight: 720)
        }
        .defaultSize(width: 1220, height: 840)
        .windowStyle(.titleBar)
        Settings { SettingsView(store: preferences) }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
