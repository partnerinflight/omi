import Foundation

if CommandLine.arguments.contains("--probe") {
    Probe.run(CommandLine.arguments)
    exit(0)
}
print("SecondBrainCapture: run with --probe [--probe-tap <bundle-id> <seconds>]")
