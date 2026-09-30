// OCR helper for the tracker on macOS. Prints one line per piece of text Vision
// finds: the text's height as a fraction of the image (0-1), a tab, the text.
// Built on first use by tracker.py:  swiftc -O ocr.swift -o ocr
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
        if let top = obs.topCandidates(1).first {
            print(String(format: "%.4f\t%@", obs.boundingBox.height, top.string))
        }
    }
}
request.recognitionLevel = .accurate  // the results banner is stylized lettering
request.usesLanguageCorrection = false
try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
