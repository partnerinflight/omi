import CryptoKit
import Darwin
import Foundation

// Every method throws only UploadError.
public enum UploadError: Error, Equatable {
    case unreachable(String)
    case authFailed
    case rejected(UInt8)
    case protocolViolation(String)
    case localFile(String)

    /// Worth retrying later without counting against the capture.
    public var isRetryable: Bool {
        switch self {
        case .unreachable, .authFailed: return true
        case .rejected(let code): return code == Wire.Reject.busy.rawValue
        case .protocolViolation, .localFile: return false
        }
    }
}

/// What the upload queue needs from the network; faked in tests.
public protocol UploadTransport {
    func openCapture(_ record: CaptureRecord) throws
    func cancelCapture(id: String) throws
    func upload(_ record: CaptureRecord, file: URL) throws
}

/// Protocol-v2 client. Each call opens a short authenticated connection, because the
/// receiver drops idle connections after 60 s and any REJECT closes the connection.
public final class UploadClient: UploadTransport {
    let host: String
    let port: UInt16
    let secret: Data
    let clientID: Data
    let timeout: Int

    public init(host: String, port: UInt16, secret: Data, clientID: Data, timeoutSeconds: Int = 30) {
        self.host = host
        self.port = port
        self.secret = secret
        self.clientID = clientID
        timeout = timeoutSeconds
    }

    public func openCapture(_ record: CaptureRecord) throws {
        let id = try Self.captureID(record.captureID)
        try session { c in
            _ = try c.expect(c.call(.captureOpen, Wire.captureOpen(id: id, startMs: record.startMs, app: record.app)), .ok)
        }
    }

    public func cancelCapture(id: String) throws {
        let captureID = try Self.captureID(id)
        try session { c in _ = try c.expect(c.call(.captureCancel, captureID), .ok) }
    }

    public func upload(_ record: CaptureRecord, file: URL) throws {
        let id = try Self.captureID(record.captureID)
        let (size, digest) = try Self.sizeAndDigest(file)
        let begin = try Self.wire { try Wire.fileBegin(id: id, totalLength: size, sha256: digest, metadata: record.uploadMetadata) }
        let handle = try Self.local { try FileHandle(forReadingFrom: file) }
        defer { try? handle.close() }
        try session { c in
            let start = try Self.wire { try Wire.readU64(c.expect(c.call(.fileBegin, begin), .fileStart)) }
            guard let first = Int64(exactly: start), first >= 0, first <= size else {
                throw UploadError.protocolViolation("bad FILE_START offset \(start)")
            }
            var offset = first
            while offset < size {
                let want = Int(min(Int64(Wire.maxChunk), size - offset))
                let chunk: Data = try Self.local {
                    try handle.seek(toOffset: UInt64(offset))
                    return try handle.read(upToCount: want) ?? Data()
                }
                guard !chunk.isEmpty else { throw UploadError.protocolViolation("file shrank during upload") }
                let reply = try c.expect(c.call(.fileData, Wire.fileData(offset: offset, bytes: chunk)), .fileAck)
                let raw = try Self.wire { try Wire.readU64(reply) }
                guard let acked = Int64(exactly: raw), acked == offset + Int64(chunk.count) else {
                    throw UploadError.protocolViolation("unexpected ACK \(raw)")
                }
                offset = acked
            }
            guard try c.expect(c.call(.fileEnd), .fileBye) == Data([1]) else {
                throw UploadError.protocolViolation("receiver did not commit")
            }
        }
    }

    static func wire<T>(_ body: () throws -> T) throws -> T {
        do { return try body() } catch let e as Wire.Malformed { throw UploadError.protocolViolation(e.description) }
    }

    static func local<T>(_ body: () throws -> T) throws -> T {
        do { return try body() } catch { throw UploadError.localFile("\(error)") }
    }

    static func captureID(_ hex: String) throws -> Data {
        guard let id = Data(hex: hex), id.count == 16 else { throw UploadError.protocolViolation("bad capture id \(hex)") }
        return id
    }

    static func sizeAndDigest(_ url: URL) throws -> (Int64, Data) {
        try local {
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            var hash = SHA256()
            var size: Int64 = 0
            while let block = try handle.read(upToCount: 1 << 20), !block.isEmpty {
                hash.update(data: block)
                size += Int64(block.count)
            }
            return (size, Data(hash.finalize()))
        }
    }

    private func session(_ body: (Connection) throws -> Void) throws {
        let c = try Connection(host: host, port: port, timeout: timeout)
        defer { c.close() }
        let nonce = Data((0..<16).map { _ in UInt8.random(in: 0...255) })
        let (kind, challenge) = try c.call(.hello, Wire.hello(clientID: clientID, nonce: nonce))
        if kind == Wire.Msg.reject.rawValue { throw UploadError.rejected(challenge.first ?? 0) }
        guard kind == Wire.Msg.challenge.rawValue, challenge.count == 48 else {
            throw UploadError.protocolViolation("expected CHALLENGE")
        }
        let serverNonce = Data(challenge.prefix(16))
        let expected = Wire.authTag(secret: secret, label: Wire.labelServer, clientNonce: nonce, serverNonce: serverNonce)
        guard Data(challenge.suffix(32)) == expected else { throw UploadError.authFailed }
        let tag = Wire.authTag(secret: secret, label: Wire.labelClient, clientNonce: nonce, serverNonce: serverNonce)
        let (reply, payload) = try c.call(.auth, tag)
        if reply == Wire.Msg.reject.rawValue {
            throw payload.first == Wire.Reject.auth.rawValue ? UploadError.authFailed : UploadError.rejected(payload.first ?? 0)
        }
        guard reply == Wire.Msg.ok.rawValue else { throw UploadError.protocolViolation("expected OK") }
        try body(c)
    }
}

/// A blocking TCP connection with connect/read/write timeouts.
final class Connection {
    private let fd: Int32

    init(host: String, port: UInt16, timeout: Int) throws {
        fd = try Self.open(host: host, port: port, timeout: timeout)
    }

    func call(_ type: Wire.Msg, _ payload: Data = Data()) throws -> (UInt8, Data) {
        try send(Wire.frame(type, payload))
        let (kind, length) = try UploadClient.wire { try Wire.parseHeader(readExact(Wire.headerLength)) }
        guard length <= Wire.maxChunk + 64 else { throw UploadError.protocolViolation("reply too large") }
        return (kind, try readExact(length))
    }

    /// The payload of a `type` reply; a REJECT becomes `UploadError.rejected`.
    func expect(_ reply: (UInt8, Data), _ type: Wire.Msg) throws -> Data {
        if reply.0 == Wire.Msg.reject.rawValue { throw UploadError.rejected(reply.1.first ?? 0) }
        guard reply.0 == type.rawValue else { throw UploadError.protocolViolation("expected \(type), got \(reply.0)") }
        return reply.1
    }

    func close() {
        Darwin.close(fd)
    }

    private func send(_ data: Data) throws {
        var sent = 0
        while sent < data.count {
            let n = data.withUnsafeBytes { Darwin.send(fd, $0.baseAddress! + sent, data.count - sent, 0) }
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw UploadError.unreachable(String(cString: strerror(errno))) }
            sent += n
        }
    }

    private func readExact(_ count: Int) throws -> Data {
        guard count > 0 else { return Data() }
        var out = Data(count: count)
        var got = 0
        while got < count {
            let n = out.withUnsafeMutableBytes { recv(fd, $0.baseAddress! + got, count - got, 0) }
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw UploadError.unreachable(n == 0 ? "connection closed" : String(cString: strerror(errno))) }
            got += n
        }
        return out
    }

    private static func open(host: String, port: UInt16, timeout: Int) throws -> Int32 {
        var hints = addrinfo()
        hints.ai_family = AF_UNSPEC
        hints.ai_socktype = SOCK_STREAM
        hints.ai_protocol = IPPROTO_TCP
        var result: UnsafeMutablePointer<addrinfo>?
        let rc = getaddrinfo(host, String(port), &hints, &result)
        guard rc == 0, let first = result else { throw UploadError.unreachable(String(cString: gai_strerror(rc))) }
        defer { freeaddrinfo(result) }
        var failure = "no address for \(host)"
        var candidate: UnsafeMutablePointer<addrinfo>? = first
        while let info = candidate {
            let s = socket(info.pointee.ai_family, info.pointee.ai_socktype, info.pointee.ai_protocol)
            if s >= 0 {
                if connect(s, info.pointee.ai_addr, info.pointee.ai_addrlen, timeout) {
                    configure(s, timeout)
                    return s
                }
                failure = String(cString: strerror(errno))
                Darwin.close(s)
            }
            candidate = info.pointee.ai_next
        }
        throw UploadError.unreachable(failure)
    }

    private static func connect(_ s: Int32, _ address: UnsafeMutablePointer<sockaddr>, _ length: socklen_t, _ timeout: Int) -> Bool {
        let flags = fcntl(s, F_GETFL, 0)
        _ = fcntl(s, F_SETFL, flags | O_NONBLOCK)
        defer { _ = fcntl(s, F_SETFL, flags) }
        if Darwin.connect(s, address, length) == 0 { return true }
        guard errno == EINPROGRESS else { return false }
        var poller = pollfd(fd: s, events: Int16(POLLOUT), revents: 0)
        var ready = poll(&poller, 1, Int32(timeout * 1000))
        while ready < 0 && errno == EINTR { ready = poll(&poller, 1, Int32(timeout * 1000)) }
        guard ready == 1 else {
            errno = ETIMEDOUT
            return false
        }
        var error: Int32 = 0
        var size = socklen_t(MemoryLayout<Int32>.size)
        getsockopt(s, SOL_SOCKET, SO_ERROR, &error, &size)
        errno = error
        return error == 0
    }

    private static func configure(_ s: Int32, _ timeout: Int) {
        var on: Int32 = 1
        setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &on, socklen_t(MemoryLayout<Int32>.size))
        var limit = timeval(tv_sec: timeout, tv_usec: 0)
        setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &limit, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &limit, socklen_t(MemoryLayout<timeval>.size))
    }
}
