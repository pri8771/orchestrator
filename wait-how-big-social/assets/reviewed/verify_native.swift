import Foundation
import AVFoundation
import AppKit
import CoreMedia

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputDirectory = URL(fileURLWithPath: CommandLine.arguments[2])
let label = CommandLine.arguments[3]
let asset = AVURLAsset(url: inputURL)
var result: [String: Any] = ["path": inputURL.path, "duration_seconds": asset.duration.seconds, "method": "AVAssetReader full video and audio decode; AVAssetImageGenerator scene samples"]
let videoTracks = asset.tracks(withMediaType: .video)
let audioTracks = asset.tracks(withMediaType: .audio)
func fourcc(_ code: FourCharCode) -> String {
    String(bytes: [UInt8((code >> 24) & 255), UInt8((code >> 16) & 255), UInt8((code >> 8) & 255), UInt8(code & 255)], encoding: .ascii) ?? String(code)
}
result["video_tracks"] = videoTracks.map { ["width": $0.naturalSize.width, "height": $0.naturalSize.height, "fps": $0.nominalFrameRate, "codec": $0.formatDescriptions.map { fourcc(CMFormatDescriptionGetMediaSubType($0 as! CMFormatDescription)) }] as [String: Any] }
let reader = try AVAssetReader(asset: asset)
let videoOutput = AVAssetReaderTrackOutput(track: videoTracks[0], outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
reader.add(videoOutput)
guard reader.startReading() else { fatalError("video start failed") }
var frameCount = 0
var firstPTS: Double? = nil
var lastPTS: Double? = nil
while let sample = videoOutput.copyNextSampleBuffer() {
    guard CMSampleBufferGetImageBuffer(sample) != nil else { fatalError("missing decoded pixel buffer") }
    let seconds = CMSampleBufferGetPresentationTimeStamp(sample).seconds
    if firstPTS == nil { firstPTS = seconds }
    lastPTS = seconds
    frameCount += 1
}
result["full_video_decode"] = ["frames": frameCount, "first_pts_seconds": firstPTS ?? -1, "last_pts_seconds": lastPTS ?? -1, "completed": reader.status == .completed, "error": String(describing: reader.error)]
var audioResults: [[String: Any]] = []
for track in audioTracks {
    let audioReader = try AVAssetReader(asset: asset)
    let audioOutput = AVAssetReaderTrackOutput(track: track, outputSettings: [AVFormatIDKey: kAudioFormatLinearPCM, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsFloatKey: true, AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false])
    audioReader.add(audioOutput)
    guard audioReader.startReading() else { fatalError("audio start failed") }
    var samples = 0
    var peak = 0.0
    var sumSquares = 0.0
    var nonzero = 0
    var bufferCount = 0
    while let sample = audioOutput.copyNextSampleBuffer() {
        guard let block = CMSampleBufferGetDataBuffer(sample) else { fatalError("audio block missing") }
        let length = CMBlockBufferGetDataLength(block)
        guard length % 4 == 0 else { fatalError("audio alignment") }
        var data = Data(count: length)
        let status = data.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!) }
        guard status == kCMBlockBufferNoErr else { fatalError("audio read error") }
        data.withUnsafeBytes { bytes in
            for value in bytes.bindMemory(to: Float.self) {
                let f = Double(value)
                guard f.isFinite else { fatalError("nonfinite PCM") }
                samples += 1
                if f != 0 { nonzero += 1 }
                peak = max(peak, abs(f))
                sumSquares += f*f
            }
        }
        bufferCount += 1
    }
    audioResults.append(["codec": track.formatDescriptions.map { fourcc(CMFormatDescriptionGetMediaSubType($0 as! CMFormatDescription)) }, "duration_seconds": track.timeRange.duration.seconds, "decoded_pcm_samples_including_channels": samples, "buffers": bufferCount, "nonzero_samples": nonzero, "peak_absolute": peak, "rms": samples > 0 ? sqrt(sumSquares / Double(samples)) : 0, "completed": audioReader.status == .completed, "error": String(describing: audioReader.error)])
}
result["full_audio_decode"] = audioResults
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero
generator.maximumSize = CGSize(width: 720, height: 1280)
let fractions: [Double] = [0.08, 0.23, 0.39, 0.57, 0.74, 0.91]
var frames: [[String: Any]] = []
for (index,fraction) in fractions.enumerated() {
    let requested = asset.duration.seconds * fraction
    var actual = CMTime.zero
    let cg = try generator.copyCGImage(at: CMTime(seconds: requested, preferredTimescale: 600), actualTime: &actual)
    let png = NSBitmapImageRep(cgImage: cg).representation(using: .png, properties: [:])!
    let path = outputDirectory.appendingPathComponent("\(label)-scene-\(index).png")
    try png.write(to: path)
    frames.append(["path": path.path, "requested_seconds": requested, "actual_seconds": actual.seconds, "width": cg.width, "height": cg.height])
}
result["scene_frames"] = frames
let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
try data.write(to: outputDirectory.appendingPathComponent("\(label)-FULL_DECODE.json"))
print(String(data: data, encoding: .utf8)!)
