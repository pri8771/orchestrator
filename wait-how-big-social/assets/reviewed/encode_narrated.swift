import Foundation
import AVFoundation
import AppKit
import CoreVideo

guard CommandLine.arguments.count >= 4 else {
    fputs("Usage: swift encode_narrated.swift OUTPUT_DIR CONTENT_ID AUDIO.aiff\n", stderr)
    exit(1)
}

let dir = URL(fileURLWithPath: CommandLine.arguments[1])
let contentId = CommandLine.arguments[2]
let audioPath = URL(fileURLWithPath: CommandLine.arguments[3])
let manifestData = try Data(contentsOf: dir.appendingPathComponent("SCENE_MANIFEST.json"))
let manifest = try JSONSerialization.jsonObject(with: manifestData) as! [String: Any]
let counts = manifest["frame_counts"] as! [Int]
let fps = manifest["fps"] as! Int
let durationSeconds = manifest["duration_seconds"] as! Double

let silentVideo = dir.appendingPathComponent("\(contentId)-silent-video.mp4")
let finalOutput = dir.appendingPathComponent("\(contentId)-narrated-video.mp4")

if FileManager.default.fileExists(atPath: finalOutput.path) {
    fputs("refusing to overwrite \(finalOutput.path)\n", stderr)
    exit(2)
}

let writer = try AVAssetWriter(outputURL: silentVideo, fileType: .mp4)
let input = AVAssetWriterInput(
    mediaType: .video,
    outputSettings: [
        AVVideoCodecKey: AVVideoCodecType.h264,
        AVVideoWidthKey: 1080,
        AVVideoHeightKey: 1920,
        AVVideoCompressionPropertiesKey: [
            AVVideoAverageBitRateKey: 2_500_000,
            AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
        ],
    ]
)
input.expectsMediaDataInRealTime = false
let adaptor = AVAssetWriterInputPixelBufferAdaptor(
    assetWriterInput: input,
    sourcePixelBufferAttributes: [
        kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
        kCVPixelBufferWidthKey as String: 1080,
        kCVPixelBufferHeightKey as String: 1920,
        kCVPixelBufferCGImageCompatibilityKey as String: true,
        kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
    ]
)
writer.add(input)
guard writer.startWriting() else { fatalError(String(describing: writer.error)) }
writer.startSession(atSourceTime: .zero)

var frame = 0
for (index, count) in counts.enumerated() {
    let data = try Data(contentsOf: dir.appendingPathComponent("scenes/scene-\(index).png"))
    let bitmap = NSBitmapImageRep(data: data)!
    let cg = bitmap.cgImage!
    var buffer: CVPixelBuffer?
    let status = CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &buffer)
    guard status == kCVReturnSuccess, let pixel = buffer else { fatalError("pixel allocation") }
    CVPixelBufferLockBaseAddress(pixel, [])
    let context = CGContext(
        data: CVPixelBufferGetBaseAddress(pixel),
        width: 1080,
        height: 1920,
        bitsPerComponent: 8,
        bytesPerRow: CVPixelBufferGetBytesPerRow(pixel),
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue
    )!
    context.draw(cg, in: CGRect(x: 0, y: 0, width: 1080, height: 1920))
    CVPixelBufferUnlockBaseAddress(pixel, [])
    for _ in 0..<count {
        let deadline = Date().addingTimeInterval(30)
        while !input.isReadyForMoreMediaData {
            if Date() > deadline || writer.status == .failed {
                fatalError("bounded encoder wait failed: \(String(describing: writer.error))")
            }
            Thread.sleep(forTimeInterval: 0.01)
        }
        guard adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(frame), timescale: Int32(fps))) else {
            fatalError("append: \(String(describing: writer.error))")
        }
        frame += 1
    }
}
input.markAsFinished()
writer.endSession(atSourceTime: CMTime(value: Int64(frame), timescale: Int32(fps)))
let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
guard done.wait(timeout: .now() + 60) == .success, writer.status == .completed else {
    fatalError("finish: \(String(describing: writer.error))")
}
print("encoded \(frame) frames at \(fps) fps: \(silentVideo.path)")

let composition = AVMutableComposition()
let videoAsset = AVURLAsset(url: silentVideo)
let videoTrack = videoAsset.tracks(withMediaType: .video)[0]
let videoDuration = videoTrack.timeRange.duration
let video = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)!
try video.insertTimeRange(CMTimeRange(start: .zero, duration: videoDuration), of: videoTrack, at: .zero)

let audioAsset = AVURLAsset(url: audioPath)
let audioTrack = audioAsset.tracks(withMediaType: .audio)[0]
let audio = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)!
let audioDuration = min(audioTrack.timeRange.duration, videoDuration)
try audio.insertTimeRange(CMTimeRange(start: .zero, duration: audioDuration), of: audioTrack, at: .zero)

guard let export = AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetHighestQuality) else {
    fatalError("export session unavailable")
}
export.outputURL = finalOutput
export.outputFileType = .mp4
export.shouldOptimizeForNetworkUse = true
let exported = DispatchSemaphore(value: 0)
export.exportAsynchronously { exported.signal() }
guard exported.wait(timeout: .now() + 60) == .success, export.status == .completed else {
    fatalError("export: \(String(describing: export.error))")
}
print("finished narrated: \(finalOutput.path) duration_target=\(durationSeconds)s")
