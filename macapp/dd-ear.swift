// dd-ear.swift — the microphone and what this Mac is playing, on one clock, for dd's echo canceller.
// stdout: 16 kHz interleaved stereo 16-bit PCM — left = the microphone, right = this Mac's own sound output
// (a film, music, dd's voice: a Core Audio process tap of every app, macOS 14.2+). dd subtracts the right from the
// left (examples/dd_aec.py), so "Jarvis" is heard while a film plays on the speakers.
// Permissions (asked once, for dd): Microphone, and System Audio Recording (Privacy & Security → Screen & System
// Audio Recording). Without the second one the tap gives silence and dd simply hears the plain mic.
// Built by the app: swiftc -O -o ~/.dd/bin/dd-ear macapp/dd-ear.swift. Diagnostics go to stderr ("dd-ear: ...").
// Exit codes: 3 no input device, 5 system-audio tap unavailable, 6 mic permission denied, 8 no callbacks (the mic
// alone too), 9 aggregate device failed, 10 audio start failed, 11 unexpected stream layout/format.
// Started with nothing playing, the mic + tap device may deliver nothing: the mic alone goes out meanwhile (Mac
// sound = silence) and the mic + tap device is rebuilt once the Mac plays.
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
var soloProc: AudioDeviceIOProcID? = nil  // the mic alone, while the mic + tap device delivers nothing
var soloDev = AudioDeviceID(0)

func destroyAggregate() {
    if aggID != unknownID {
        if let p = procID {
            _ = AudioDeviceStop(aggID, p)
            _ = AudioDeviceDestroyIOProcID(aggID, p)
            procID = nil
        }
        _ = AudioHardwareDestroyAggregateDevice(aggID)
        aggID = unknownID
    }
    if tapID != unknownID {
        if #available(macOS 14.2, *) { _ = AudioHardwareDestroyProcessTap(tapID) }
        tapID = unknownID
    }
}

func stopSolo() {
    if let p = soloProc {
        _ = AudioDeviceStop(soloDev, p)
        _ = AudioDeviceDestroyIOProcID(soloDev, p)
        soloProc = nil
    }
}

func cleanup() {
    stopSolo()
    destroyAggregate()
}

func runningSomewhere(_ dev: AudioObjectID) -> Bool {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
                                          mScope: kAudioObjectPropertyScopeGlobal, mElement: mainElement)
    var v = UInt32(0)
    var size = UInt32(MemoryLayout<UInt32>.size)
    return AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, &v) == noErr && v != 0
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
    guard let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true) else {
        warn("audio converter failed (16 kHz)")
        exit(11)
    }

    // state shared by the two capture paths — touched on ioQueue only (the main loop reads it through ioQueue.sync)
    let ioQueue = DispatchQueue(label: "dd-ear.io", qos: .userInteractive)
    var micQ = [Int16]()
    var refQ = [Int16]()
    var micPeak: Float = 0
    var refPeak: Float = 0
    var lastReport = Date()
    var aggCallbacks = 0
    var soloCallbacks = 0
    var lastAgg = Date()
    var lastSolo = Date()
    var aggLive = false  // the mic + tap device delivers: the mic-alone capture stands down
    var aggMicCh = 1
    var aggTapCh = 1
    var aggFmt: AVAudioFormat? = nil
    var aggMicConv: AVAudioConverter? = nil
    var aggRefConv: AVAudioConverter? = nil
    var soloFmt: AVAudioFormat? = nil
    var soloConv: AVAudioConverter? = nil

    func convert(_ conv: AVAudioConverter, _ fmt: AVAudioFormat, _ src: UnsafePointer<Float>, _ stride: Int,
                 _ frames: Int, _ peak: inout Float, _ q: inout [Int16]) {
        guard frames > 0, let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(frames)) else { return }
        buf.frameLength = AVAudioFrameCount(frames)
        let dst = buf.floatChannelData![0]
        var p = peak
        for i in 0..<frames {
            let v = src[i * stride]
            dst[i] = v
            p = max(p, abs(v))
        }
        peak = p
        let cap = AVAudioFrameCount(Double(frames) * 16000.0 / fmt.sampleRate) + 64
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

    // pair mic and Mac sound up and write them out; false: the listener went away
    func emit() -> Bool {
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
            if !ok { return false }
        }
        if Date().timeIntervalSince(lastReport) >= 30 {
            warn(String(format: "levels (30 s): mic peak %.3f, playback peak %.3f%@", micPeak, refPeak,
                        refPeak == 0 ? (aggLive ? " (nothing playing — or System Audio Recording not allowed for dd)"
                                                : " (mic alone: the Mac's sound not joined yet)") : ""))
            micPeak = 0
            refPeak = 0
            lastReport = Date()
        }
        return true
    }

    // 2+3. A tap of everything this Mac plays (mono mix of all apps, not muted: the film keeps playing as it was) and
    //      one private aggregate device = the mic (clock master) + the tap (drift-compensated to the mic's clock):
    //      every callback carries both, sample-aligned — what an echo canceller needs. 0 = running, else exit code.
    func buildAggregate() -> Int32 {
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
            tapID = unknownID
            return 5
        }
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
            aggID = unknownID
            destroyAggregate()
            return 9
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
            destroyAggregate()
            return 11
        }
        for f in formats where !(f.mFormatID == kAudioFormatLinearPCM && f.mBitsPerChannel == 32
                                 && (f.mFormatFlags & kAudioFormatFlagIsFloat) != 0) {
            warn("unsupported stream format: id \(f.mFormatID) flags \(f.mFormatFlags) bits \(f.mBitsPerChannel)")
            destroyAggregate()
            return 11
        }
        let nominal = rateOf(aggID)
        let rate = nominal > 0 ? nominal : formats[0].mSampleRate
        guard let f = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false),
              let mc = AVAudioConverter(from: f, to: outFmt), let rc = AVAudioConverter(from: f, to: outFmt) else {
            warn("audio converter failed for \(rate) Hz")
            destroyAggregate()
            return 11
        }
        let micCh = max(1, Int(formats[0].mChannelsPerFrame))
        let tapCh = max(1, Int(formats[formats.count - 1].mChannelsPerFrame))
        ioQueue.sync {
            aggMicCh = micCh
            aggTapCh = tapCh
            aggFmt = f
            aggMicConv = mc
            aggRefConv = rc
            aggCallbacks = 0
        }
        st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, ioQueue) { _, inInput, _, _, _ in
            guard let fmt = aggFmt, let mconv = aggMicConv, let rconv = aggRefConv else { return }
            lastAgg = Date()
            aggCallbacks += 1
            let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInput))
            if aggCallbacks == 1 || aggCallbacks == 200 {  // (diagnostics: what the first callbacks carry)
                warn("callback \(aggCallbacks): \(abl.count) buffers: " + abl.map { b in
                    "\(b.mNumberChannels) ch \(b.mDataByteSize) B\(b.mData == nil ? " (no data)" : "")" }.joined(separator: ", "))
            }
            if !aggLive {  // (from now on this device carries the mic: what the mic alone left is dropped)
                aggLive = true
                micQ.removeAll()
                refQ.removeAll()
            }
            guard abl.count >= 1, let mp = abl[0].mData?.assumingMemoryBound(to: Float.self) else { return }
            let mb = abl[0]
            convert(mconv, fmt, UnsafePointer(mp), aggMicCh, Int(mb.mDataByteSize) / (4 * aggMicCh), &micPeak, &micQ)
            // the Mac's sound: absent until System Audio Recording is allowed (or while the tap is rebuilt) — then the
            // mic still goes out, paired with silence
            let tb = abl[abl.count - 1]
            if abl.count >= 2, let tp = tb.mData?.assumingMemoryBound(to: Float.self), tb.mDataByteSize > 0 {
                convert(rconv, fmt, UnsafePointer(tp), aggTapCh, Int(tb.mDataByteSize) / (4 * aggTapCh), &refPeak, &refQ)
            } else if refQ.count < micQ.count {
                refQ.append(contentsOf: [Int16](repeating: 0, count: micQ.count - refQ.count))
            }
            if !emit() {  // the listener went away
                cleanup()
                exit(0)
            }
        }
        guard st == noErr, procID != nil else {
            warn("IO proc failed: OSStatus \(st)")
            procID = nil
            destroyAggregate()
            return 10
        }
        st = AudioDeviceStart(aggID, procID)
        guard st == noErr else {
            warn("audio start failed: OSStatus \(st)")
            destroyAggregate()
            return 10
        }
        warn("listening: mic + this Mac's sound (tap) at \(Int(rate)) Hz -> 16000 Hz stereo [mic, playback]; " +
             "streams \(micCh) ch mic, \(tapCh) ch tap")
        return 0
    }

    // The mic alone (Mac sound = silence) while the mic + tap device delivers nothing: on Dhina's Mac it stayed silent
    // when started with nothing playing (21 Oct 22:53; it ran at once at 22:20 with a film on) — dd keeps hearing him.
    func startSolo() -> Bool {
        let fmts = inputStreamFormats(micDev)
        guard let f0 = fmts.first, f0.mFormatID == kAudioFormatLinearPCM, f0.mBitsPerChannel == 32,
              (f0.mFormatFlags & kAudioFormatFlagIsFloat) != 0 else {
            warn("mic alone: unsupported mic format")
            return false
        }
        let r = rateOf(micDev) > 0 ? rateOf(micDev) : f0.mSampleRate
        guard let f = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: r, channels: 1, interleaved: false),
              let c = AVAudioConverter(from: f, to: outFmt) else {
            warn("mic alone: audio converter failed for \(r) Hz")
            return false
        }
        ioQueue.sync {
            soloFmt = f
            soloConv = c
            lastSolo = Date()
        }
        soloDev = micDev
        var st = AudioDeviceCreateIOProcIDWithBlock(&soloProc, micDev, ioQueue) { _, inInput, _, _, _ in
            if aggLive { return }
            guard let fmt = soloFmt, let conv = soloConv else { return }
            lastSolo = Date()
            soloCallbacks += 1
            let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInput))
            if soloCallbacks == 1 {
                warn("mic alone: callback 1: \(abl.count) buffers: " + abl.map { b in
                    "\(b.mNumberChannels) ch \(b.mDataByteSize) B" }.joined(separator: ", "))
            }
            guard abl.count >= 1, let mp = abl[0].mData?.assumingMemoryBound(to: Float.self) else { return }
            let ch = max(1, Int(abl[0].mNumberChannels))
            convert(conv, fmt, UnsafePointer(mp), ch, Int(abl[0].mDataByteSize) / (4 * ch), &micPeak, &micQ)
            if refQ.count < micQ.count {
                refQ.append(contentsOf: [Int16](repeating: 0, count: micQ.count - refQ.count))
            }
            if !emit() {
                cleanup()
                exit(0)
            }
        }
        guard st == noErr, soloProc != nil else {
            warn("mic alone: IO proc failed: OSStatus \(st)")
            soloProc = nil
            return false
        }
        st = AudioDeviceStart(micDev, soloProc)
        guard st == noErr else {
            warn("mic alone: start failed: OSStatus \(st)")
            stopSolo()
            return false
        }
        return true
    }

    let code = buildAggregate()
    if code != 0 {
        cleanup()
        exit(code)
    }
    let started = Date()
    var soloOn = false
    var outSince: Date? = nil
    var lastBuild = Date()
    var backoff: Double = 15
    while true {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.25))
        let (live, la, ls) = ioQueue.sync { (aggLive, lastAgg, lastSolo) }
        let now = Date()
        if live {
            if soloOn {
                stopSolo()
                soloOn = false
                backoff = 15
                warn("the Mac's sound joined: mic + tap from now on")
            }
            if now.timeIntervalSince(la) > 4 {  // it ran and stopped: the mic alone, rebuilt when the Mac plays
                warn("the mic + tap device stopped — the mic alone until it can be rebuilt")
                ioQueue.sync { aggLive = false }
                destroyAggregate()
                guard startSolo() else {
                    warn("no audio callbacks for 4 s — giving up (dd uses the plain mic)")
                    cleanup()
                    exit(8)
                }
                soloOn = true
                lastBuild = now
            }
            continue
        }
        if !soloOn {
            // nothing from the mic + tap device yet: after 3 s, the mic alone meanwhile (macOS may also be showing
            // the System Audio Recording prompt)
            if now.timeIntervalSince(started) > 3 {
                if startSolo() {
                    soloOn = true
                    lastBuild = now
                    warn("waiting for the first audio of mic + tap (nothing playing, or macOS asks about System Audio " +
                         "Recording for dd: Allow) — the mic alone meanwhile; the Mac's sound joins when it can")
                } else if now.timeIntervalSince(started) > 90 {
                    warn("no audio callbacks for 90 s — giving up (dd uses the plain mic)")
                    cleanup()
                    exit(8)
                }
            }
            continue
        }
        if now.timeIntervalSince(ls) > 4 {
            warn("no audio callbacks for 4 s — giving up (dd uses the plain mic)")
            cleanup()
            exit(8)
        }
        // the Mac plays (its output device runs) and the mic + tap device still delivers nothing: build a fresh one
        let od = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)
        if od != 0 && runningSomewhere(od) {
            if outSince == nil { outSince = now }
            if now.timeIntervalSince(outSince ?? now) > 5 && now.timeIntervalSince(lastBuild) > backoff {
                lastBuild = now
                backoff = min(backoff * 2, 600)
                destroyAggregate()
                let c = buildAggregate()
                warn(c == 0 ? "the Mac is playing: mic + tap rebuilt (next try in \(Int(backoff)) s if it stays silent)"
                            : "mic + tap rebuild failed (code \(c)) — the mic alone")
            }
        } else {
            outSince = nil
        }
    }
}

if #available(macOS 14.2, *) {
    runEar()
} else {
    warn("system audio capture needs macOS 14.2 or newer")
    exit(5)
}
