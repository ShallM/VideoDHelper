import SwiftUI
import WebKit
import VideoCore

@MainActor
final class BilibiliLoginController: NSObject, ObservableObject, WKNavigationDelegate, WKHTTPCookieStoreObserver {
    let webView: WKWebView
    @Published var message = "在 B 站官方页面扫码或登录，然后点击“使用当前会话”。"
    @Published var isSaving = false
    @Published var sessionDetected = false
    @Published var isLoading = true
    private var isClosed = true

    override init() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        // WKWebView's default UA lacks Safari's Version token. Bilibili's
        // browser check otherwise redirects a successful login to an upgrade page.
        webView.customUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
        open()
    }

    func open() {
        guard isClosed else { return }
        isClosed = false
        isLoading = true
        isSaving = false
        sessionDetected = false
        message = "在 B 站官方页面扫码或登录，然后点击“使用当前会话”。"
        webView.navigationDelegate = self
        webView.configuration.websiteDataStore.httpCookieStore.add(self)
        refreshSession()
        webView.load(URLRequest(url: URL(string: "https://passport.bilibili.com/login")!))
    }

    func cookiesDidChange(in cookieStore: WKHTTPCookieStore) { refreshSession() }

    func close() {
        guard !isClosed else { return }
        isClosed = true
        webView.stopLoading()
        webView.navigationDelegate = nil
        webView.configuration.websiteDataStore.httpCookieStore.remove(self)
    }

    private func refreshSession() {
        webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { [weak self] cookies in
            Task { @MainActor in
                guard let self, !self.isClosed else { return }
                if self.containsSession(cookies) {
                    self.sessionDetected = true
                    self.isLoading = false
                    self.message = "已取得登录会话。点击“使用当前会话”，继续读取字幕。"
                    self.webView.stopLoading()
                }
            }
        }
    }

    private func containsSession(_ cookies: [HTTPCookie]) -> Bool {
        cookies.contains {
            $0.name == "SESSDATA" && !$0.value.isEmpty &&
            ($0.domain == "bilibili.com" || $0.domain.hasSuffix(".bilibili.com")) &&
            ($0.expiresDate == nil || $0.expiresDate! > Date())
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { isLoading = false; refreshSession() }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        isLoading = false
        if !sessionDetected { message = "登录页面加载失败：\(error.localizedDescription)" }
    }

    func saveSession(completion: @escaping () -> Void) {
        isSaving = true
        webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { [weak self] cookies in
            Task { @MainActor in
                guard let self, !self.isClosed else { return }
                defer { self.isSaving = false }
                let loggedIn = self.containsSession(cookies)
                guard loggedIn else {
                    self.message = "还没有检测到登录会话，请先完成页面中的登录。"
                    return
                }
                do {
                    try BilibiliCookies.save(cookies)
                    completion()
                } catch { self.message = "保存会话失败：\(error.localizedDescription)" }
            }
        }
    }
}

private struct BilibiliWebView: NSViewRepresentable {
    let webView: WKWebView
    func makeNSView(context: Context) -> WKWebView { webView }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
}

struct BilibiliLoginView: View {
    @StateObject private var controller = BilibiliLoginController()
    @Environment(\.dismiss) private var dismiss
    let onSaved: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("登录 B 站").font(.title2.bold())
            Text(controller.message).font(.callout).foregroundStyle(.secondary)
            Group {
                if controller.sessionDetected {
                    VStack(spacing: 16) {
                        Image(systemName: "checkmark.circle.fill").font(.system(size: 58)).foregroundStyle(.green)
                        Text("已取得登录会话").font(.title2.bold())
                        Text("可以返回应用，继续读取视频字幕。").foregroundStyle(.secondary)
                    }
                } else {
                    ZStack(alignment: .top) {
                        BilibiliWebView(webView: controller.webView)
                        if controller.isLoading { ProgressView().padding(16) }
                    }
                }
            }.frame(minWidth: 860, minHeight: 540).clipShape(RoundedRectangle(cornerRadius: 12))
            HStack {
                Text("登录状态只用于本机获取视频和字幕。").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("使用当前会话") {
                    controller.saveSession { onSaved(); dismiss() }
                }.buttonStyle(.borderedProminent).disabled(controller.isSaving)
            }
        }.padding(20)
        .onAppear { controller.open() }
        .onDisappear { controller.close() }
    }
}
