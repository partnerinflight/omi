import Foundation

/// User configuration in `~/Library/Application Support/SecondBrainCapture/config.json`.
/// Missing keys take defaults, so the file can stay minimal (`{"host": "..."}`).
public struct Config: Codable, Equatable {
    public var host: String
    public var port: UInt16
    public var allowlist: [String]
    public var secretPath: String
    public var minCaptureSeconds: Double
    public var releaseGraceSeconds: Double
    public var spoolWarnBytes: Int64

    public static let defaultAllowlist = [
        "com.amazon.Amazon-Chime", "us.zoom.xos", "com.microsoft.teams2",
        "com.tinyspeck.slackmacgap", "Cisco-Systems.Spark",
    ]

    public init(host: String = "", port: UInt16 = 7331, allowlist: [String] = Config.defaultAllowlist,
                secretPath: String = "~/.omi-local/upload-secret.hex", minCaptureSeconds: Double = 60,
                releaseGraceSeconds: Double = 20, spoolWarnBytes: Int64 = 5_000_000_000) {
        self.host = host
        self.port = port
        self.allowlist = allowlist
        self.secretPath = secretPath
        self.minCaptureSeconds = minCaptureSeconds
        self.releaseGraceSeconds = releaseGraceSeconds
        self.spoolWarnBytes = spoolWarnBytes
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = Config()
        host = try c.decodeIfPresent(String.self, forKey: .host) ?? d.host
        port = try c.decodeIfPresent(UInt16.self, forKey: .port) ?? d.port
        allowlist = try c.decodeIfPresent([String].self, forKey: .allowlist) ?? d.allowlist
        secretPath = try c.decodeIfPresent(String.self, forKey: .secretPath) ?? d.secretPath
        minCaptureSeconds = try c.decodeIfPresent(Double.self, forKey: .minCaptureSeconds) ?? d.minCaptureSeconds
        releaseGraceSeconds = try c.decodeIfPresent(Double.self, forKey: .releaseGraceSeconds) ?? d.releaseGraceSeconds
        spoolWarnBytes = try c.decodeIfPresent(Int64.self, forKey: .spoolWarnBytes) ?? d.spoolWarnBytes
    }

    public static var directory: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SecondBrainCapture", isDirectory: true)
    }

    /// Load `url`, creating it with defaults if it does not exist.
    public static func load(from url: URL) throws -> Config {
        guard FileManager.default.fileExists(atPath: url.path) else {
            let config = Config()
            try config.save(to: url)
            return config
        }
        return try JSONDecoder().decode(Config.self, from: Data(contentsOf: url))
    }

    public func save(to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(self).write(to: url, options: .atomic)
    }

    public var isConfigured: Bool { !host.isEmpty }
    public var expandedSecretPath: String { (secretPath as NSString).expandingTildeInPath }
}
