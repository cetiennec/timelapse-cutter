// Reads text in images with Apple Vision (built into macOS).
// Image paths on stdin, one per line; one JSON line per image on stdout:
// {"path": ..., "lines": [{"text", "conf", "box": [x, y, w, h]}]}, box in fractions, origin top-left.
// Vision misses small text on a whole frame, so each image is read in overlapping tiles,
// enlarged: `ocr [cols] [rows] [scale]`, default 3 3 2.
import Foundation
import AppKit
import Vision

let args = CommandLine.arguments.dropFirst().compactMap { Double($0) }
let cols = Int(args.count > 0 ? args[0] : 3), rows = Int(args.count > 1 ? args[1] : 3)
let scale = args.count > 2 ? args[2] : 2

func readTile(_ cg: CGImage) -> [(String, Float, CGRect)] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false     // keep code, keys and addresses as written
    req.recognitionLanguages = ["en-US", "fr-FR"]
    try? VNImageRequestHandler(cgImage: cg).perform([req])
    return (req.results ?? []).compactMap { obs in
        guard let top = obs.topCandidates(1).first else { return nil }
        let b = obs.boundingBox
        return (top.string, top.confidence, CGRect(x: b.minX, y: 1 - b.maxY, width: b.width, height: b.height))
    }
}

func enlarge(_ cg: CGImage, _ k: Double) -> CGImage? {
    let w = Int(Double(cg.width) * k), h = Int(Double(cg.height) * k)
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
    ctx.interpolationQuality = .high
    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
    return ctx.makeImage()
}

func read(_ path: String) -> [[String: Any]] {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return [] }
    let W = Double(cg.width), H = Double(cg.height)
    let tw = W / (Double(cols) * 0.8 + 0.2), th = H / (Double(rows) * 0.8 + 0.2)   // 20% overlap
    var found: [(String, Float, CGRect)] = []
    for r in 0..<rows {
        for c in 0..<cols {
            let rect = CGRect(x: Double(c) * tw * 0.8, y: Double(r) * th * 0.8, width: tw, height: th).integral
            guard let tile = cg.cropping(to: rect), let big = enlarge(tile, scale) else { continue }
            for (t, conf, b) in readTile(big) {
                found.append((t, conf, CGRect(x: (rect.minX + b.minX * rect.width) / W,
                                              y: (rect.minY + b.minY * rect.height) / H,
                                              width: b.width * rect.width / W,
                                              height: b.height * rect.height / H)))
            }
        }
    }
    // the same text read in two overlapping tiles: keep the longer reading
    found.sort { $0.0.count > $1.0.count }
    var kept: [(String, Float, CGRect)] = []
    for f in found {
        let dup = kept.contains { k in
            let i = k.2.intersection(f.2)
            return !i.isNull && i.width * i.height > 0.5 * f.2.width * f.2.height
        }
        if !dup { kept.append(f) }
    }
    return kept.map { ["text": $0.0, "conf": $0.1,
                       "box": [$0.2.minX, $0.2.minY, $0.2.width, $0.2.height]] }
}

while let line = readLine() {
    let path = line.trimmingCharacters(in: .whitespaces)
    if path.isEmpty { continue }
    let out: [String: Any] = ["path": path, "lines": read(path)]
    if let data = try? JSONSerialization.data(withJSONObject: out),
       let s = String(data: data, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}
