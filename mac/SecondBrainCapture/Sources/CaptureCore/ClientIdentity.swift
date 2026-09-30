import CryptoKit
import Foundation
import IOKit

public enum ClientIdentity {
    /// The Mac's hardware UUID (stable across reinstalls).
    public static func platformUUID() -> String? {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPlatformExpertDevice"))
        guard service != 0 else { return nil }
        defer { IOObjectRelease(service) }
        return IORegistryEntryCreateCFProperty(service, kIOPlatformUUIDKey as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue() as? String
    }

    /// The 6-byte v2 client id: the first bytes of SHA-256(hardware UUID).
    public static func clientID(uuid: String) -> Data {
        Data(SHA256.hash(data: Data(uuid.utf8)).prefix(6))
    }

    public struct SecretError: Error, CustomStringConvertible {
        public let description: String
    }

    /// The shared 32-byte secret stored as 64 hex characters (the receiver's upload-secret.hex).
    public static func loadSecret(path: String) throws -> Data {
        let text = try String(contentsOfFile: path, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        guard let secret = Data(hex: text), secret.count == 32 else {
            throw SecretError(description: "\(path): expected 64 hex characters")
        }
        return secret
    }
}
