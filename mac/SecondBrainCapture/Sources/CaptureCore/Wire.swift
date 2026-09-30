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

    public static func parseHeader(_ header: Data) -> (UInt8, Int) {
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
        id + u64(UInt64(totalLength)) + sha256 + (try JSONSerialization.data(withJSONObject: metadata, options: [.sortedKeys]))
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

    public static func readU64(_ data: Data) -> UInt64 {
        data.prefix(8).reduce(0) { $0 << 8 | UInt64($1) }
    }
}

extension Data {
    public init?(hex: String) {
        guard hex.count % 2 == 0 else { return nil }
        var bytes = [UInt8]()
        var index = hex.startIndex
        while index < hex.endIndex {
            let next = hex.index(index, offsetBy: 2)
            guard let byte = UInt8(hex[index..<next], radix: 16) else { return nil }
            bytes.append(byte)
            index = next
        }
        self.init(bytes)
    }

    public var hex: String {
        map { String(format: "%02x", $0) }.joined()
    }
}
