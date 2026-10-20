// dd-mic.swift — the microphone with Apple's voice processing: echo cancellation + noise suppression + AGC.
// It subtracts what this Mac itself is playing (a video, music, dd's own voice) from the mic signal, so the
// wake word works while something plays on the speakers. Writes 16 kHz mono 16-bit PCM to stdout.
// Built on the Mac by install_app.sh / the app launcher:  swiftc -O -o ~/.dd/bin/dd-mic macapp/dd-mic.swift
// Flags: --no-aec (raw mic, for comparison). Diagnostics go to stderr.
import AVFoundation
import Foundation

let stderrH = FileHandle.standardError
func warn(_ s: String) { stderrH.write((s + "\n").data(using: .utf8)!) }

signal(SIGPIPE, SIG_IGN)
signal(SIGTERM) { _ in exit(0) }
signal(SIGINT) { _ in exit(0) }

let engine = AVAudioEngine()
let input = engine.inputNode
if !CommandLine.arguments.contains("--no-aec") {
    do { try input.setVoiceProcessingEnabled(true) } catch { warn("voice processing unavailable: \(error)") }
}
engine.prepare()
var inFmt = input.outputFormat(forBus: 0)
if inFmt.sampleRate == 0 || inFmt.channelCount == 0 { inFmt = input.inputFormat(forBus: 0) }
guard inFmt.sampleRate > 0, inFmt.channelCount > 0 else {
    warn("no input format — Microphone permission for dd? (System Settings → Privacy & Security → Microphone)")
    exit(3)
}
guard let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true),
      let conv = AVAudioConverter(from: inFmt, to: outFmt) else {
    warn("audio converter failed for \(inFmt)")
    exit(2)
}
warn("dd-mic: \(Int(inFmt.sampleRate)) Hz x\(inFmt.channelCount) -> 16000 Hz mono, voice processing \(input.isVoiceProcessingEnabled ? "ON" : "off")")

input.installTap(onBus: 0, bufferSize: 2048, format: inFmt) { buf, _ in
    let cap = AVAudioFrameCount(Double(buf.frameLength) * outFmt.sampleRate / inFmt.sampleRate) + 32
    guard let ob = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap) else { return }
    var consumed = false
    var err: NSError?
    let st = conv.convert(to: ob, error: &err) { _, status in
        if consumed { status.pointee = .noDataNow; return nil }
        consumed = true
        status.pointee = .haveData
        return buf
    }
    if st == .error { warn("convert: \(err?.localizedDescription ?? "?")"); return }
    let n = Int(ob.frameLength)
    guard n > 0, let ch = ob.int16ChannelData else { return }
    let bytes = n * 2
    let written = ch[0].withMemoryRebound(to: UInt8.self, capacity: bytes) { write(1, $0, bytes) }
    if written < 0 { exit(0) }  // the listener went away
}
do { try engine.start() } catch {
    warn("engine start failed: \(error)")
    exit(4)
}
RunLoop.main.run()
