// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "SecondBrainCapture",
    platforms: [.macOS("14.4")],
    targets: [
        .target(name: "CaptureCore"),
        .executableTarget(name: "SecondBrainCapture", dependencies: ["CaptureCore"]),
        .testTarget(name: "CaptureCoreTests", dependencies: ["CaptureCore"]),
    ]
)
