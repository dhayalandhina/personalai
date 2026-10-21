// dd-ear.swift — the microphone and what this Mac is playing, on one clock, for dd's echo canceller.
// stdout: 16 kHz interleaved stereo 16-bit PCM — left = the microphone, right = this Mac's own sound output
// (a film, music, dd's voice: a Core Audio process tap of every app, macOS 14.2+). dd subtracts the right from the
// left (examples/dd_aec.py), so "Jarvis" is heard while a film plays on the speakers.
// Permissions (asked once, for dd): Microphone, and System Audio Recording (Privacy & Security → Screen & System
// Audio Recording). Without the second one the tap gives silence and dd simply hears the plain mic.
// Built by the app: swiftc -O -o ~/.dd/bin/dd-ear macapp/dd-ear.swift. Diagnostics go to stderr ("dd-ear: ...").
// Exit codes: 3 no input device, 5 system-audio tap unavailable, 6 mic permission denied, 8 no callbacks for 4 s,
// 9 aggregate device failed, 10 audio start failed, 11 unexpected stream layout/format.
import AVFoundation
import CoreAudio
import Foundation

let stderrH = FileHandle.standardError
func warn(_ s: String) { stderrH.write((s + "\n").data(using: .utf8)!) }

signal(SIGPIPE, SIG_IGN)

// 1. Microphone permission (the prompt and the Privacy entry belong to the app that launched us: dd)
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

let mainElement: AudioObjectPropertyElement = 0  // kAudioObjectPropertyElementMain

func defaultDevice(_ selector: AudioObjectPropertySelector) -> AudioDeviceID {
    var addr = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: mainElement)
    var dev = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &dev)
    return st == noErr ? dev : 0
}

func stringProp(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: mainElement)
    var value: Unmanaged<CFString>? = nil
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &value) == noErr,
          let cf = value?.takeRetainedValue() else { return nil }
    return cf as String
}

func rateOf(_ dev: AudioObjectID) -> Double {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyNominalSampleRate,
                                          mScope: kAudioObjectPropertyScopeGlobal, mElement: mainElement)
    var rate = Float64(0)
    var size = UInt32(MemoryLayout<Float64>.size)
    return AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &rate) == noErr ? rate : 0
}

func inputStreamFormats(_ dev: AudioObjectID) -> [AudioStreamBasicDescription] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams, mScope: kAudioObjectPropertyScopeInput,
                                          mElement: mainElement)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(dev, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioStreamID](repeating: 0, count: Int(size) / MemoryLayout<AudioStreamID>.size)
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids.map { sid -> AudioStreamBasicDescription in
        var asbd = AudioStreamBasicDescription()
        var a = AudioObjectPropertyAddress(mSelector: kAudioStreamPropertyVirtualFormat,
                                           mScope: kAudioObjectPropertyScopeGlobal, mElement: mainElement)
        var sz = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        _ = AudioObjectGetPropertyData(sid, &a, 0, nil, &sz, &asbd)
        return asbd
    }
}

let unknownID = AudioObjectID(kAudioObjectUnknown)
var tapID = unknownID
var aggID = unknownID
var procID: AudioDeviceIOProcID? = nil

func cleanup() {
    if aggID != unknownID {
        if let p = procID {
            _ = AudioDeviceStop(aggID, p)
            _ = AudioDeviceDestroyIOProcID(aggID, p)
        }
        _ = AudioHardwareDestroyAggregateDevice(aggID)
        aggID = unknownID
    }
    if tapID != unknownID {
        if #available(macOS 14.2, *) { _ = AudioHardwareDestroyProcessTap(tapID) }
        tapID = unknownID
    }
}

signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
termSource.setEventHandler { cleanup(); exit(0) }
termSource.resume()
let intSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
intSource.setEventHandler { cleanup(); exit(0) }
intSource.resume()

@available(macOS 14.2, *)
func runEar() {
    let micDev = defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
    guard micDev != 0, let micUID = stringProp(micDev, kAudioDevicePropertyDeviceUID) else {
        warn("no input device")
        exit(3)
    }
    let outDev = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)
    warn("devices: input '\(stringProp(micDev, kAudioObjectPropertyName) ?? "?")' \(Int(rateOf(micDev))) Hz, " +
         "output '\(stringProp(outDev, kAudioObjectPropertyName) ?? "?")' \(Int(rateOf(outDev))) Hz")

    // 2. A tap of everything this Mac plays (mono mix of all apps), not muted: the film keeps playing as it was.
    let desc = CATapDescription(monoGlobalTapButExcludeProcesses: [])
    desc.uuid = UUID()
    desc.name = "dd-ear"
    desc.muteBehavior = .unmuted
    if desc.responds(to: NSSelectorFromString("setPrivateTap:")) {  // (only visible to dd; KVC: the Swift name
        desc.setValue(true, forKey: "privateTap")                    //  of this flag differs between SDKs)
    }
    var st = AudioHardwareCreateProcessTap(desc, &tapID)
    guard st == noErr, tapID != unknownID else {
        warn("system audio tap failed: OSStatus \(st)")
        exit(5)
    }

    // 3. One private aggregate device = the mic (clock master) + the tap (drift-compensated to the mic's clock):
    //    every callback carries both, sample-aligned — what an echo canceller needs.
    let agg: [String: Any] = [
        kAudioAggregateDeviceNameKey: "dd-ear",
        kAudioAggregateDeviceUIDKey: "dd-ear-" + UUID().uuidString,
        kAudioAggregateDeviceMainSubDeviceKey: micUID,
        kAudioAggregateDeviceIsPrivateKey: true,
        kAudioAggregateDeviceIsStackedKey: false,
        kAudioAggregateDeviceTapAutoStartKey: true,
        kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: micUID]],
        kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                           kAudioSubTapUIDKey: desc.uuid.uuidString]],
    ]
    st = AudioHardwareCreateAggregateDevice(agg as CFDictionary, &aggID)
    guard st == noErr, aggID != unknownID else {
        warn("aggregate device failed: OSStatus \(st)")
        cleanup()
        exit(9)
    }
    var formats = inputStreamFormats(aggID)
    for _ in 0..<20 where formats.count < 2 {  // (the tap's stream can show up a moment after creation)
        Thread.sleep(forTimeInterval: 0.1)
        formats = inputStreamFormats(aggID)
    }
    warn("streams: " + formats.map { "\($0.mChannelsPerFrame) ch \(Int($0.mSampleRate)) Hz \($0.mBitsPerChannel) bit" }
        .joined(separator: " + "))
    guard formats.count >= 2 else {
        warn("expected mic + tap input streams, got \(formats.count)")
        cleanup()
        exit(11)
    }
    for f in formats where !(f.mFormatID == kAudioFormatLinearPCM && f.mBitsPerChannel == 32
                             && (f.mFormatFlags & kAudioFormatFlagIsFloat) != 0) {
        warn("unsupported stream format: id \(f.mFormatID) flags \(f.mFormatFlags) bits \(f.mBitsPerChannel)")
        cleanup()
        exit(11)
    }
    let nominal = rateOf(aggID)
    let rate = nominal > 0 ? nominal : formats[0].mSampleRate
    let micCh = max(1, Int(formats[0].mChannelsPerFrame))
    let tapCh = max(1, Int(formats[formats.count - 1].mChannelsPerFrame))
    guard let inFmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false),
          let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true),
          let micConv = AVAudioConverter(from: inFmt, to: outFmt),
          let refConv = AVAudioConverter(from: inFmt, to: outFmt) else {
        warn("audio converter failed for \(rate) Hz")
        cleanup()
        exit(11)
    }

    var micQ = [Int16]()
    var refQ = [Int16]()
    var micPeak: Float = 0
    var refPeak: Float = 0
    var lastReport = Date()
    var lastCallback = Date()

    func convert(_ conv: AVAudioConverter, _ src: UnsafePointer<Float>, _ stride: Int, _ frames: Int,
                 _ peak: inout Float, _ q: inout [Int16]) {
        guard frames > 0, let buf = AVAudioPCMBuffer(pcmFormat: inFmt, frameCapacity: AVAudioFrameCount(frames)) else { return }
        buf.frameLength = AVAudioFrameCount(frames)
        let dst = buf.floatChannelData![0]
        var p = peak
        for i in 0..<frames {
            let v = src[i * stride]
            dst[i] = v
            p = max(p, abs(v))
        }
        peak = p
        let cap = AVAudioFrameCount(Double(frames) * 16000.0 / rate) + 64
        guard let ob = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap) else { return }
        var fed = false
        var err: NSError?
        _ = conv.convert(to: ob, error: &err) { _, status in
            if fed {
                status.pointee = .noDataNow
                return nil
            }
            fed = true
            status.pointee = .haveData
            return buf
        }
        let n = Int(ob.frameLength)
        if n > 0, let ch = ob.int16ChannelData {
            q.append(contentsOf: UnsafeBufferPointer(start: ch[0], count: n))
        }
    }

    let ioQueue = DispatchQueue(label: "dd-ear.io", qos: .userInteractive)
    var callbacks = 0
    st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, ioQueue) { _, inInput, _, _, _ in
        lastCallback = Date()
        callbacks += 1
        let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInput))
        if callbacks == 1 || callbacks == 200 {  // (diagnostics: what the first callbacks carry)
            warn("callback \(callbacks): \(abl.count) buffers: " + abl.map { b in
                "\(b.mNumberChannels) ch \(b.mDataByteSize) B\(b.mData == nil ? " (no data)" : "")" }.joined(separator: ", "))
        }
        guard abl.count >= 1, let mp = abl[0].mData?.assumingMemoryBound(to: Float.self) else { return }
        let mb = abl[0]
        let mFrames = Int(mb.mDataByteSize) / (4 * micCh)
        convert(micConv, UnsafePointer(mp), micCh, mFrames, &micPeak, &micQ)
        // the Mac's sound: absent until System Audio Recording is allowed (or while the tap is rebuilt) — then the
        // mic still goes out, paired with silence
        let tb = abl[abl.count - 1]
        if abl.count >= 2, let tp = tb.mData?.assumingMemoryBound(to: Float.self), tb.mDataByteSize > 0 {
            convert(refConv, UnsafePointer(tp), tapCh, Int(tb.mDataByteSize) / (4 * tapCh), &refPeak, &refQ)
        } else if refQ.count < micQ.count {
            refQ.append(contentsOf: [Int16](repeating: 0, count: micQ.count - refQ.count))
        }
        // pair them up; one stream that stalls must never hold the other back
        if refQ.isEmpty && micQ.count > 3200 { refQ = [Int16](repeating: 0, count: micQ.count) }
        if micQ.isEmpty && refQ.count > 3200 { refQ.removeAll() }
        let n = min(micQ.count, refQ.count)
        if n > 0 {
            var out = [Int16](repeating: 0, count: 2 * n)
            for i in 0..<n {
                out[2 * i] = micQ[i]
                out[2 * i + 1] = refQ[i]
            }
            micQ.removeFirst(n)
            refQ.removeFirst(n)
            let ok = out.withUnsafeBytes { raw -> Bool in
                guard let base = raw.baseAddress else { return true }
                var off = 0
                while off < raw.count {
                    let w = write(1, base + off, raw.count - off)
                    if w <= 0 { return false }
                    off += w
                }
                return true
            }
            if !ok {  // the listener went away
                cleanup()
                exit(0)
            }
        }
        if Date().timeIntervalSince(lastReport) >= 30 {
            warn(String(format: "levels (30 s): mic peak %.3f, playback peak %.3f%@", micPeak, refPeak,
                        refPeak == 0 ? " (nothing playing — or System Audio Recording not allowed for dd)" : ""))
            micPeak = 0
            refPeak = 0
            lastReport = Date()
        }
    }
    guard st == noErr, procID != nil else {
        warn("IO proc failed: OSStatus \(st)")
        cleanup()
        exit(10)
    }
    st = AudioDeviceStart(aggID, procID)
    guard st == noErr else {
        warn("audio start failed: OSStatus \(st)")
        cleanup()
        exit(10)
    }
    warn("listening: mic + this Mac's sound (tap) at \(Int(rate)) Hz -> 16000 Hz stereo [mic, playback]; " +
         "streams \(micCh) ch mic, \(tapCh) ch tap")
    lastCallback = Date()
    let started = Date()
    var toldWaiting = false
    while true {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.25))
        // before the first callback macOS may be showing the System Audio Recording prompt: give it 90 s
        let limit: Double = callbacks == 0 ? 90 : 4
        if callbacks == 0 && !toldWaiting && Date().timeIntervalSince(started) > 5 {
            toldWaiting = true
            warn("waiting for the first audio — if macOS asks about System Audio Recording for dd: Allow")
        }
        if Date().timeIntervalSince(lastCallback) > limit {
            warn("no audio callbacks for \(Int(limit)) s — giving up (dd uses the plain mic)")
            cleanup()
            exit(8)
        }
    }
}

if #available(macOS 14.2, *) {
    runEar()
} else {
    warn("system audio capture needs macOS 14.2 or newer")
    exit(5)
}
