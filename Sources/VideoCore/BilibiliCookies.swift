import Foundation
import Darwin

public enum BilibiliCookies {
    public static var fileURL: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("VideoDHelper", isDirectory: true)
            .appendingPathComponent("bilibili-cookies.txt")
    }

    /// Only cookies from this application's Bilibili web view are passed here.
    /// Other sites and malformed Netscape fields are excluded.
    public static func serialize(_ cookies: [HTTPCookie]) -> String {
        let lines = cookies.filter { cookie in
            let domain = cookie.domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
            return (domain == "bilibili.com" || domain.hasSuffix(".bilibili.com")) &&
                [cookie.domain, cookie.path, cookie.name, cookie.value].allSatisfy { !$0.contains("\t") && !$0.contains("\n") && !$0.contains("\r") }
        }.map { cookie in
            let domain = (cookie.isHTTPOnly ? "#HttpOnly_" : "") + cookie.domain
            let expiry = max(0, Int(cookie.expiresDate?.timeIntervalSince1970 ?? 0))
            return [domain, cookie.domain.hasPrefix(".") ? "TRUE" : "FALSE", cookie.path,
                    cookie.isSecure ? "TRUE" : "FALSE", String(expiry), cookie.name, cookie.value].joined(separator: "\t")
        }
        return "# Netscape HTTP Cookie File\n# Local VideoDHelper Bilibili session\n" + lines.joined(separator: "\n") + "\n"
    }

    public static func save(_ cookies: [HTTPCookie], to destination: URL = fileURL) throws {
        let manager = FileManager.default
        let folder = destination.deletingLastPathComponent()
        try manager.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let temporary = folder.appendingPathComponent(".session-\(UUID().uuidString)")
        defer { try? manager.removeItem(at: temporary) }
        guard manager.createFile(atPath: temporary.path, contents: Data(serialize(cookies).utf8), attributes: [.posixPermissions: 0o600]) else {
            throw CocoaError(.fileWriteUnknown)
        }
        guard rename(temporary.path, destination.path) == 0 else { throw CocoaError(.fileWriteUnknown) }
    }
}
