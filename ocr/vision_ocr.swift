// Japanese OCR via Apple's Vision framework (macOS only).
// Usage: vision-ocr <image>  ->  JSON on stdout:
//   {"width": W, "height": H, "lines": [{"text", "conf", "x", "y", "w", "h"}]}
// Box coordinates are normalised to 0..1 with a top-left origin.
import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: vision-ocr <image>\n".data(using: .utf8)!)
    exit(2)
}

let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: url),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read image \(url.path)\n".data(using: .utf8)!)
    exit(1)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["ja-JP"]
request.usesLanguageCorrection = true

do {
    try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
} catch {
    FileHandle.standardError.write("ocr failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}

var lines: [[String: Any]] = []
for obs in request.results ?? [] {
    guard let best = obs.topCandidates(1).first else { continue }
    let b = obs.boundingBox  // bottom-left origin
    lines.append([
        "text": best.string,
        "conf": best.confidence,
        "x": b.minX,
        "y": 1.0 - b.maxY,
        "w": b.width,
        "h": b.height,
    ])
}

let out: [String: Any] = ["width": cg.width, "height": cg.height, "lines": lines]
let data = try JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted])
FileHandle.standardOutput.write(data)
