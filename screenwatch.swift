// Long-running screen reader for the tracker (macOS 14+). Every `interval` seconds
// it captures the main display via ScreenCaptureKit, already shrunk to `width`
// pixels, runs Apple's text recognizer with the model kept warm, and prints:
//   FRAME <unix time>
//   <height fraction>\t<text>      (one per piece of text)
//   END
// Usage: screenwatch [interval seconds] [width]     Built by tracker.py:
//   swiftc -O screenwatch.swift -o screenwatch
import Foundation
import ScreenCaptureKit
import Vision

let args = CommandLine.arguments
let interval = args.count > 1 ? Double(args[1]) ?? 2.0 : 2.0
let width = args.count > 2 ? Int(args[2]) ?? 1400 : 1400
setvbuf(stdout, nil, _IOLBF, 0)

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false

func grab() async -> CGImage? {
    guard let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true),
          let display = content.displays.first else { return nil }
    let filter = SCContentFilter(display: display, excludingWindows: [])
    let config = SCStreamConfiguration()
    config.width = width
    config.height = Int(Double(display.height) * Double(width) / Double(display.width))
    config.showsCursor = false
    return try? await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
}

let sem = DispatchSemaphore(value: 0)
Task {
    while true {
        let start = Date()
        if let img = await grab() {
            let handler = VNImageRequestHandler(cgImage: img, options: [:])
            try? handler.perform([request])
            print("FRAME \(Int(start.timeIntervalSince1970))")
            for obs in request.results ?? [] {
                if let top = obs.topCandidates(1).first {
                    print(String(format: "%.4f\t%@", obs.boundingBox.height, top.string))
                }
            }
            print("END")
        } else {
            print("FRAME 0"); print("END")   // no permission or no display
        }
        let remaining = interval - Date().timeIntervalSince(start)
        if remaining > 0 { try? await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000)) }
    }
}
sem.wait()
