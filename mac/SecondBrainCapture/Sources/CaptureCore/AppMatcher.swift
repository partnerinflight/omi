/// Maps a process bundle ID to its allowlisted app family: the app itself or one of
/// its helpers (`<family>.<anything>`), whose audio may come from a helper process.
public struct AppMatcher {
    public let allowlist: [String]

    public init(allowlist: [String]) {
        self.allowlist = allowlist
    }

    public func family(of bundleID: String) -> String? {
        allowlist.first { bundleID == $0 || bundleID.hasPrefix($0 + ".") }
    }
}
