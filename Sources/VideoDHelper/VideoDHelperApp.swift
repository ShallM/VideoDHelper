import SwiftUI
import AppKit
import VideoCore

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
        .commands {
            CommandGroup(replacing: .help) {
                Button("VideoDHelper 使用指南") {
                    NSWorkspace.shared.open(URL(string: "https://github.com/ShallM/VideoDHelper#readme")!)
                }
                Button("许可证与第三方组件") {
                    if let url = Bundle.main.url(forResource: "LICENSES", withExtension: "html") { NSWorkspace.shared.open(url) }
                    else { NSWorkspace.shared.open(URL(string: "https://github.com/ShallM/VideoDHelper/blob/main/THIRD_PARTY_NOTICES.md")!) }
                }
            }
        }
        Settings { SettingsView(store: preferences) }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var isTerminating = false
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        ASRModelStore.shared.prepareForQuit()
        guard BackgroundProcesses.isRunning else { return .terminateNow }
        guard !isTerminating else { return .terminateLater }
        isTerminating = true
        Task { @MainActor in
            await BackgroundProcesses.terminateForQuit()
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}
