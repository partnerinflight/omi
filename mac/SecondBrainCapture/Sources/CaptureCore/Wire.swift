import CryptoKit
import Foundation

/// Receiver protocol v2 (see omi_local/upload_protocol.py). Big-endian integers;
/// frames are [type:u8][length:u32][payload].
public enum Wire {
    public enum Msg: UInt8 {
        case hello = 0x01, challenge = 0x02, auth = 0x03, reject = 0x7F
        case captureOpen = 0x10, captureCancel = 0x11, fileBegin = 0x12, fileData = 0x13, fileEnd = 0x14
        case fileStart = 0x15, fileAck = 0x16, fileBye = 0x17, ok = 0x18
    }

    public enum Reject: UInt8 {
        case auth = 1, protocolError = 2, busy = 3
    }

    public struct Malformed: Error, Equatable, CustomStringConvertible {
        public let description: String
    }

    public static let version: UInt8 = 2
    public static let headerLength = 5
    public static let maxChunk = 64 * 1024
    public static let labelServer = Data("omi-local-srv".utf8)
    public static let labelClient = Data("omi-local-cli".utf8)

    public static func frame(_ type: Msg, _ payload: Data = Data()) -> Data {
        var out = Data([type.rawValue])
        out.append(u32(UInt32(payload.count)))
        out.append(payload)
        return out
    }

    public static func parseHeader(_ header: Data) throws -> (UInt8, Int) {
        guard header.count == headerLength else { throw Malformed(description: "short frame header") }
        let b = [UInt8](header)
        return (b[0], Int(b[1]) << 24 | Int(b[2]) << 16 | Int(b[3]) << 8 | Int(b[4]))
    }

    public static func hello(clientID: Data, nonce: Data) -> Data {
        Data("OMIL".utf8) + Data([version]) + clientID + nonce
    }

    public static func authTag(secret: Data, label: Data, clientNonce: Data, serverNonce: Data) -> Data {
        Data(HMAC<SHA256>.authenticationCode(for: label + clientNonce + serverNonce, using: SymmetricKey(data: secret)))
    }

    public static func captureOpen(id: Data, startMs: Int64, app: String) -> Data {
        id + u64(UInt64(startMs)) + Data(app.utf8)
    }

    public static func fileBegin(id: Data, totalLength: Int64, sha256: Data, metadata: [String: Any]) throws -> Data {
        guard JSONSerialization.isValidJSONObject(metadata) else { throw Malformed(description: "metadata is not JSON") }
        return id + u64(UInt64(totalLength)) + sha256 + (try JSONSerialization.data(withJSONObject: metadata, options: [.sortedKeys]))
    }

    public static func fileData(offset: Int64, bytes: Data) -> Data {
        u64(UInt64(offset)) + bytes
    }

    public static func u32(_ v: UInt32) -> Data {
        Data([UInt8(v >> 24 & 0xFF), UInt8(v >> 16 & 0xFF), UInt8(v >> 8 & 0xFF), UInt8(v & 0xFF)])
    }

    public static func u64(_ v: UInt64) -> Data {
        Data((0..<8).reversed().map { UInt8(v >> (UInt64($0) * 8) & 0xFF) })
    }

    public static func readU64(_ data: Data) throws -> UInt64 {
        guard data.count == 8 else { throw Malformed(description: "expected 8-byte integer") }
        return data.reduce(0) { $0 << 8 | UInt64($1) }
    }
}

extension Data {
    public init?(hex: String) {
        let chars = Array(hex.utf8)
        guard chars.count % 2 == 0 else { return nil }
        func nibble(_ c: UInt8) -> UInt8? {
            switch c {
            case 0x30...0x39: return c - 0x30
            case 0x61...0x66: return c - 0x61 + 10
            case 0x41...0x46: return c - 0x41 + 10
            default: return nil
            }
        }
        var bytes = [UInt8]()
        var i = 0
        while i < chars.count {
            guard let hi = nibble(chars[i]), let lo = nibble(chars[i + 1]) else { return nil }
            bytes.append(hi << 4 | lo)
            i += 2
        }
        self.init(bytes)
    }

    public var hex: String {
        map { String(format: "%02x", $0) }.joined()
    }
}
