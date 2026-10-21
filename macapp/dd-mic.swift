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

// 2. Which devices the system hands us (diagnostics: Bluetooth / HDMI outputs and virtual inputs change what
//    voice processing can do).
let mainElement: AudioObjectPropertyElement = 0  // kAudioObjectPropertyElementMain / ...Master (same value)

func defaultDevice(_ selector: AudioObjectPropertySelector) -> AudioDeviceID {
    var addr = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: mainElement)
    var dev = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &dev)
    return st == noErr ? dev : 0
}

func deviceName(_ dev: AudioDeviceID) -> String {
    guard dev != 0 else { return "?" }
    var addr = AudioObjectPropertyAddress(mSelector: kAudioObjectPropertyName, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: mainElement)
    var name: Unmanaged<CFString>? = nil
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &name) == noErr,
          let cf = name?.takeRetainedValue() else { return "id \(dev)" }
    return cf as String
}

func deviceRate(_ dev: AudioDeviceID) -> Double {
    guard dev != 0 else { return 0 }
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyNominalSampleRate,
                                          mScope: kAudioObjectPropertyScopeGlobal, mElement: mainElement)
    var rate = Float64(0)
    var size = UInt32(MemoryLayout<Float64>.size)
    return AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &rate) == noErr ? rate : 0
}

func defaultInputName() -> String { deviceName(defaultDevice(kAudioHardwarePropertyDefaultInputDevice)) }

let inDev = defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
let outDev = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)
warn("devices: input '\(deviceName(inDev))' \(Int(deviceRate(inDev))) Hz, output '\(deviceName(outDev))' \(Int(deviceRate(outDev))) Hz")

// 3. The engine. Voice processing is one IO unit for both directions: the output side must run (it is the echo
//    canceller's reference signal). Macs differ in what they accept, so the ways to set it up are tried in turn,
//    most capable first; a way that starts but only delivers digital zeros for 3 s is abandoned for the next:
//    0) mixer → output connected with the output hardware's own format
//    1) a silent player node keeping the output side running
//    2) the implicit mixer (AVAudioEngine's default wiring)
//    3) voice processing input only
//    4) plain microphone (no echo cancellation; dd's own software gain + silence check still apply)
let aec = !CommandLine.arguments.contains("--no-aec")
let modeNames = ["voice processing ON (mixer → output)", "voice processing ON (silent player)",
                 "voice processing ON (implicit mixer)", "voice processing ON (input only)", "voice processing off"]
var zeroSamples = 0
var silenceReported = false
var switchRequested = false
var keepPlayer: AVAudioPlayerNode? = nil

func start(mode: Int) -> AVAudioEngine? {
    let engine = AVAudioEngine()
    let input = engine.inputNode
    let vp = aec && mode < 4
    if vp {
        do { try input.setVoiceProcessingEnabled(true) } catch {
            warn("voice processing unavailable: \(error)")
            return nil
        }
        if #available(macOS 14.0, *) {  // keep the video's volume as it is (no "other audio" ducking)
            input.voiceProcessingOtherAudioDuckingConfiguration =
                AVAudioVoiceProcessingOtherAudioDuckingConfiguration(enableAdvancedDucking: false, duckingLevel: .min)
        }
        switch mode {
        case 0:
            let hw = engine.outputNode.outputFormat(forBus: 0)
            if hw.sampleRate > 0 && hw.channelCount > 0 {
                engine.connect(engine.mainMixerNode, to: engine.outputNode, format: hw)
            }
            engine.mainMixerNode.outputVolume = 0
        case 1:
            let player = AVAudioPlayerNode()
            engine.attach(player)
            engine.connect(player, to: engine.mainMixerNode, format: nil)
            engine.mainMixerNode.outputVolume = 0
            keepPlayer = player
        case 2:
            engine.mainMixerNode.outputVolume = 0
        default:
            break
        }
    }
    engine.prepare()
    var inFmt = input.outputFormat(forBus: 0)
    if inFmt.sampleRate == 0 || inFmt.channelCount == 0 { inFmt = input.inputFormat(forBus: 0) }
    guard inFmt.sampleRate > 0, inFmt.channelCount > 0 else {
        warn("no input format (mode \(mode)) — Microphone permission for dd? (System Settings → Privacy & Security → Microphone)")
        return nil
    }
    guard let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true),
          let monoFmt = AVAudioFormat(standardFormatWithSampleRate: inFmt.sampleRate, channels: 1),
          let conv = AVAudioConverter(from: monoFmt, to: outFmt),
          let convAll = AVAudioConverter(from: inFmt, to: outFmt) else {
        warn("audio converter failed for \(inFmt)")
        return nil
    }
    // Voice processing hands over several channels (the Mac's mic array); which one carries the processed voice
    // differs per model. The first ~1.7 s decide: the loudest channel is used from then on (mean until then).
    var chanPeak = [Float](repeating: 0, count: 32)
    var taps = 0
    var useChannel = -1
    input.installTap(onBus: 0, bufferSize: 2048, format: inFmt) { buf, _ in
        let frames = Int(buf.frameLength)
        let chs = Int(buf.format.channelCount)
        var source: AVAudioPCMBuffer = buf
        var converter = convAll
        if let fd = buf.floatChannelData, frames > 0, chs > 1,
           let mono = AVAudioPCMBuffer(pcmFormat: monoFmt, frameCapacity: AVAudioFrameCount(frames)) {
            if taps < 40 {
                for c in 0..<min(chs, 32) {
                    var p: Float = 0
                    for i in 0..<frames { p = max(p, abs(fd[c][i])) }
                    chanPeak[c] = max(chanPeak[c], p)
                }
                taps += 1
                if taps == 40 {
                    let peaks = Array(chanPeak.prefix(min(chs, 32)))
                    let best = peaks.max() ?? 0
                    useChannel = best > 0 ? (peaks.firstIndex(of: best) ?? 0) : -1
                    warn("channels \(chs): peaks \(peaks.map { String(format: "%.4f", $0) }) -> using \(useChannel >= 0 ? "channel \(useChannel)" : "mean (all silent)")")
                }
            }
            mono.frameLength = AVAudioFrameCount(frames)
            let dst = mono.floatChannelData![0]
            if useChannel >= 0 {
                for i in 0..<frames { dst[i] = fd[useChannel][i] }
            } else {
                let k = Float(chs)
                for i in 0..<frames {
                    var acc: Float = 0
                    for c in 0..<chs { acc += fd[c][i] }
                    dst[i] = acc / k
                }
            }
            source = mono
            converter = conv
        }
        let cap = AVAudioFrameCount(Double(frames) * outFmt.sampleRate / inFmt.sampleRate) + 32
        guard let ob = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap) else { return }
        var consumed = false
        var err: NSError?
        let st = converter.convert(to: ob, error: &err) { _, status in
            if consumed { status.pointee = .noDataNow; return nil }
            consumed = true
            status.pointee = .haveData
            return source
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
            if mode < 4 && zeroSamples >= 16000 * 3 && !switchRequested {
                switchRequested = true
            } else if mode == 4 && !silenceReported && zeroSamples >= 16000 * 4 {
                silenceReported = true
                warn("4 s of digital silence — the default input device gives nothing (wrong device / permission); dd switches to ffmpeg")
            }
        }
        let bytes = n * 2
        let written = ch[0].withMemoryRebound(to: UInt8.self, capacity: bytes) { write(1, $0, bytes) }
        if written < 0 { exit(0) }  // the listener went away
    }
    do { try engine.start() } catch {
        warn("engine start failed (mode \(mode)): \(error)")
        input.removeTap(onBus: 0)
        keepPlayer = nil
        return nil
    }
    if mode == 1, let p = keepPlayer { p.play() }
    warn("input '\(defaultInputName())' \(Int(inFmt.sampleRate)) Hz x\(inFmt.channelCount) -> 16000 Hz mono, \(modeNames[vp ? mode : 4]) [mode \(mode)]")
    return engine
}

var running: AVAudioEngine? = nil
var currentMode = 4

func tryStart(from: Int) {
    for m in from...4 {
        if !aec && m < 4 { continue }
        if let e = start(mode: m) { running = e; currentMode = m; return }
    }
    exit(4)
}

tryStart(from: 0)
while true {
    RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.25))
    if switchRequested {
        switchRequested = false
        if currentMode < 4 {
            warn("3 s of digital silence in mode \(currentMode) — trying mode \(currentMode + 1)")
            running?.inputNode.removeTap(onBus: 0)
            running?.stop()
            running = nil
            keepPlayer = nil
            zeroSamples = 0
            silenceReported = false
            RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.6))  // let the old voice-processing unit go
            tryStart(from: currentMode + 1)
        }
    }
}
