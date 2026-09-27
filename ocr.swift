// Tiny OCR helper for the tracker on macOS: prints every line of text Vision
// finds in an image, one per line. Built on first use by tracker.py:
//   swiftc -O ocr.swift -o ocr
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1, let image = NSImage(contentsOfFile: args[1]),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("usage: ocr <image>\n".data(using: .utf8)!)
    exit(2)
}
let request = VNRecognizeTextRequest { req, _ in
    for obs in (req.results as? [VNRecognizedTextObservation]) ?? [] {
        if let top = obs.topCandidates(1).first { print(top.string) }
    }
}
request.recognitionLevel = .fast
request.usesLanguageCorrection = false
try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
