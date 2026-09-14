import Foundation
import AVFoundation
import AppKit
import CoreVideo

let dir = URL(fileURLWithPath: CommandLine.arguments[1])
let output = dir.appendingPathComponent("WHB-001-repaired-silent-video.mp4")
let writer = try AVAssetWriter(outputURL: output, fileType: .mp4)
let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 1080, AVVideoHeightKey: 1920, AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 2_500_000, AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel]])
input.expectsMediaDataInRealTime = false
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 1080, kCVPixelBufferHeightKey as String: 1920, kCVPixelBufferCGImageCompatibilityKey as String: true, kCVPixelBufferCGBitmapContextCompatibilityKey as String: true])
writer.add(input)
guard writer.startWriting() else { fatalError(String(describing: writer.error)) }
writer.startSession(atSourceTime: .zero)
let counts = [58, 50, 65, 65, 58, 64]
var frame = 0
for (index,count) in counts.enumerated() {
    let data = try Data(contentsOf: dir.appendingPathComponent("scenes/scene-\(index).png"))
    let bitmap = NSBitmapImageRep(data: data)!
    let cg = bitmap.cgImage!
    var buffer: CVPixelBuffer?
    let status = CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &buffer)
    guard status == kCVReturnSuccess, let pixel = buffer else { fatalError("pixel allocation") }
    CVPixelBufferLockBaseAddress(pixel, [])
    let context = CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 1080, height: 1920, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue)!
    context.draw(cg, in: CGRect(x: 0, y: 0, width: 1080, height: 1920))
    CVPixelBufferUnlockBaseAddress(pixel, [])
    for _ in 0..<count {
        let deadline = Date().addingTimeInterval(15)
        while !input.isReadyForMoreMediaData {
            if Date() > deadline || writer.status == .failed { fatalError("bounded encoder wait failed: \(String(describing: writer.error))") }
            Thread.sleep(forTimeInterval: 0.01)
        }
        guard adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(frame), timescale: 30)) else { fatalError("append: \(String(describing: writer.error))") }
        frame += 1
    }
}
input.markAsFinished()
writer.endSession(atSourceTime: CMTime(value: 360, timescale: 30))
let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
guard done.wait(timeout: .now()+30) == .success, writer.status == .completed else { fatalError("finish: \(String(describing: writer.error))") }
print("encoded \(frame) frames at 30 fps: \(output.path)")
// Preserve the original asset's silent AAC track without generating speech/music.
let composition = AVMutableComposition()
let videoAsset = AVURLAsset(url: output)
let video = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)!
try video.insertTimeRange(CMTimeRange(start: .zero, duration: CMTime(seconds: 12, preferredTimescale: 600)), of: videoAsset.tracks(withMediaType: .video)[0], at: .zero)
let original = AVURLAsset(url: URL(fileURLWithPath: CommandLine.arguments[2]))
let originalAudio = original.tracks(withMediaType: .audio)[0]
let audio = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)!
try audio.insertTimeRange(originalAudio.timeRange, of: originalAudio, at: .zero)
let final = dir.appendingPathComponent("WHB-001_million-vs-billion-seconds-repaired.mp4")
let export = AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough)!
export.outputURL = final
export.outputFileType = .mp4
export.shouldOptimizeForNetworkUse = true
let exported = DispatchSemaphore(value: 0)
export.exportAsynchronously { exported.signal() }
guard exported.wait(timeout: .now()+30) == .success, export.status == .completed else { fatalError("export: \(String(describing: export.error))") }
print("finished: \(final.path)")
