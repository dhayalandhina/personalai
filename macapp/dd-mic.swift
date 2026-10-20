// dd-mic.swift — the microphone with Apple's voice processing: echo cancellation + noise suppression + AGC.
// It subtracts what this Mac itself is playing (a video, music, dd's own voice) from the mic signal, so the
// wake word works while something plays on the speakers. Writes 16 kHz mono 16-bit PCM to stdout.
// Built on the Mac by install_app.sh / the app: swiftc -O -o ~/.dd/bin/dd-mic macapp/dd-mic.swift
// Flags: --no-aec (raw mic, for comparison). Diagnostics go to stderr (dd logs them as "dd-mic: ...").
// Exit codes: 2 converter, 3 no input format, 4 engine start, 6 microphone permission denied.
import AVFoundation
import CoreAudio
import Foundation

let stderrH = FileHandle.standardError
func warn(_ s: String) { stderrH.write((s + "\n").data(using: .utf8)!) }

signal(SIGPIPE, SIG_IGN)
signal(SIGTERM) { _ in exit(0) }
signal(SIGINT) { _ in exit(0) }

// 1. Microphone permission. The prompt (and the Privacy list entry) belongs to the app that launched us: dd.
switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .authorized:
    warn("mic permission: OK")
case .notDetermined:
    warn("mic permission: asking (Allow in the macOS prompt)...")
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    AVCaptureDevice.requestAccess(for: .audio) { ok in granted = ok; sem.signal() }
    _ = sem.wait(timeout: .now() + 300)
    warn("mic permission: \(granted ? "granted" : "NOT granted")")
    if !granted { exit(6) }
default:
    warn("mic permission: DENIED for dd — System Settings → Privacy & Security → Microphone → dd ON")
    exit(6)
}

// 2. Which input device the system hands us (diagnostic: a virtual/aggregate default device gives silence).
func defaultInputName() -> String {
    let main: AudioObjectPropertyElement = 0  // kAudioObjectPropertyElementMain / ...Master (same value)
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                          mScope: kAudioObjectPropertyScopeGlobal, mElement: main)
    var dev = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let sys = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyData(sys, &addr, 0, nil, &size, &dev) == noErr, dev != 0 else { return "?" }
    var naddr = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName,
                                           mScope: kAudioObjectPropertyScopeGlobal, mElement: main)
    var name: Unmanaged<CFString>? = nil
    var nsize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(dev, &naddr, 0, nil, &nsize, &name) == noErr,
          let cf = name?.takeRetainedValue() else { return "id \(dev)" }
    return cf as String
}

// 3. The engine. Voice processing is one IO unit for both directions, so the output side is kept alive
//    (a silent mixer) — that is the echo canceller's reference signal.
let engine = AVAudioEngine()
let input = engine.inputNode
let aec = !CommandLine.arguments.contains("--no-aec")
if aec {
    do { try input.setVoiceProcessingEnabled(true) } catch { warn("voice processing unavailable: \(error)") }
}
if aec && input.isVoiceProcessingEnabled {
    engine.mainMixerNode.outputVolume = 0  // touching mainMixerNode connects it to the output node
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
warn("input '\(defaultInputName())' \(Int(inFmt.sampleRate)) Hz x\(inFmt.channelCount) -> 16000 Hz mono, "
     + "voice processing \(input.isVoiceProcessingEnabled ? "ON" : "off")")

var zeroSamples = 0
var silenceReported = false
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
    var nonzero = false
    for i in 0..<n where ch[0][i] != 0 { nonzero = true; break }
    if nonzero {
        zeroSamples = 0
    } else {
        zeroSamples += n
        if !silenceReported && zeroSamples >= 16000 * 4 {
            silenceReported = true
            warn("4 s of digital silence — the default input device gives nothing (wrong device / permission); dd switches to ffmpeg")
        }
    }
    let bytes = n * 2
    let written = ch[0].withMemoryRebound(to: UInt8.self, capacity: bytes) { write(1, $0, bytes) }
    if written < 0 { exit(0) }  // the listener went away
}
do { try engine.start() } catch {
    warn("engine start failed: \(error)")
    exit(4)
}
RunLoop.main.run()
