import CaptureCore
import CoreAudio

struct CoreAudioError: Error, CustomStringConvertible {
    let what: String
    let status: OSStatus
    var description: String { "\(what) failed (OSStatus \(status))" }
}

func check(_ what: String, _ status: OSStatus) throws {
    guard status == noErr else { throw CoreAudioError(what: what, status: status) }
}

/// Read-only Core Audio process and device queries.
enum AudioProcesses {
    static func address(_ selector: AudioObjectPropertySelector,
                        _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }

    static func objectIDs(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                          _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> [AudioObjectID] {
        var addr = address(selector, scope)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &ids) == noErr else { return [] }
        return ids
    }

    static func uint32(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
        var addr = address(selector)
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr ? value : nil
    }

    static func string(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
        var addr = address(selector)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr, let value else { return nil }
        return value.takeRetainedValue() as String
    }

    /// Every audio process object with a bundle ID.
    static func snapshot() -> [AudioProcess] {
        objectIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList).compactMap { id in
            guard let bundle = string(id, kAudioProcessPropertyBundleID), !bundle.isEmpty else { return nil }
            return AudioProcess(objectID: id, bundleID: bundle,
                                isRunningInput: uint32(id, kAudioProcessPropertyIsRunningInput) == 1,
                                inputDevices: objectIDs(id, kAudioProcessPropertyDevices, kAudioObjectPropertyScopeInput))
        }
    }

    static func defaultOutputUID() throws -> String {
        guard let device = uint32(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultSystemOutputDevice),
              let uid = string(device, kAudioDevicePropertyDeviceUID) else {
            throw CoreAudioError(what: "default output device lookup", status: -1)
        }
        return uid
    }
}
