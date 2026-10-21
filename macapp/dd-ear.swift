// dd-ear.swift — the microphone and what this Mac is playing, lined up in time, for dd's echo canceller.
// stdout: 16 kHz interleaved stereo 16-bit PCM — left = the microphone, right = this Mac's own sound output
// (a film, music, dd's voice: a Core Audio process tap of every app, macOS 14.2+). dd subtracts the right from the
// left (examples/dd_aec.py), so "Jarvis" is heard while a film plays on the speakers.
// The mic runs on its own (an IO proc on the input device) and the tap in its own aggregate device: a tap delivers
// nothing while nothing plays (Dhina's Mac, 21 Oct: mic + tap in one aggregate stayed silent — mic included — when
// started with nothing playing), so the mic never waits for it. The two are paired by their host time stamps.
// Permissions (asked once, for dd): Microphone, and System Audio Recording (Privacy & Security → Screen & System
// Audio Recording). Without the second one the tap gives silence and dd simply hears the plain mic.
// Built by the app: swiftc -O -o ~/.dd/bin/dd-ear macapp/dd-ear.swift. Diagnostics go to stderr ("dd-ear: ...").
// Exit codes: 3 no input device, 6 mic permission denied, 8 no mic callbacks, 10 mic start failed, 11 unexpected mic
// format. A tap that cannot be made only leaves the right channel silent (logged).
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
var aggID = unknownID   // the tap's own aggregate device
var tapProc: AudioDeviceIOProcID? = nil
var micProc: AudioDeviceIOProcID? = nil
var micDevice = AudioDeviceID(0)

func destroyTap() {
    if aggID != unknownID {
        if let p = tapProc {
            _ = AudioDeviceStop(aggID, p)
            _ = AudioDeviceDestroyIOProcID(aggID, p)
            tapProc = nil
        }
        _ = AudioHardwareDestroyAggregateDevice(aggID)
        aggID = unknownID
    }
    if tapID != unknownID {
        if #available(macOS 14.2, *) { _ = AudioHardwareDestroyProcessTap(tapID) }
        tapID = unknownID
    }
}

func cleanup() {
    if let p = micProc {
        _ = AudioDeviceStop(micDevice, p)
        _ = AudioDeviceDestroyIOProcID(micDevice, p)
        micProc = nil
    }
    destroyTap()
}

signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let termSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
termSource.setEventHandler { cleanup(); exit(0) }
termSource.resume()
let intSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
intSource.setEventHandler { cleanup(); exit(0) }
intSource.resume()

func hostSeconds(_ ts: UnsafePointer<AudioTimeStamp>) -> Double {
    let t = ts.pointee
    let host = (t.mFlags.contains(.hostTimeValid) && t.mHostTime != 0) ? t.mHostTime : AudioGetCurrentHostTime()
    return Double(AudioConvertHostTimeToNanos(host)) / 1e9
}

func isFloat32(_ f: AudioStreamBasicDescription) -> Bool {
    return f.mFormatID == kAudioFormatLinearPCM && f.mBitsPerChannel == 32 && (f.mFormatFlags & kAudioFormatFlagIsFloat) != 0
}

@available(macOS 14.2, *)
func runEar() {
    let micDev = defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
    guard micDev != 0 else {
        warn("no input device")
        exit(3)
    }
    micDevice = micDev
    let outDev = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)
    warn("devices: input '\(stringProp(micDev, kAudioObjectPropertyName) ?? "?")' \(Int(rateOf(micDev))) Hz, " +
         "output '\(stringProp(outDev, kAudioObjectPropertyName) ?? "?")' \(Int(rateOf(outDev))) Hz")
    guard let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true) else {
        warn("audio converter failed (16 kHz)")
        exit(11)
    }

    // shared by the two IO procs — touched on ioQueue only (the watchdog reads it through ioQueue.sync)
    let ioQueue = DispatchQueue(label: "dd-ear.io", qos: .userInteractive)
    var micQ = [Int16]()
    var refQ = [Int16]()
    var micEnd = 0.0     // host time (s) just after the newest sample in each queue
    var refEnd = 0.0
    var refSeen = 0.0    // host time of the newest tap audio (0 = none yet)
    var micPeak: Float = 0
    var refPeak: Float = 0
    var lastReport = Date()
    var micCallbacks = 0
    var tapCallbacks = 0
    var lastMic = Date()
    var micFmt: AVAudioFormat? = nil
    var micConv: AVAudioConverter? = nil
    var tapFmt: AVAudioFormat? = nil
    var tapConv: AVAudioConverter? = nil

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

    // the Mac's sound lined up with the mic by time: refQ[i] was playing when micQ[i] was heard (the echo itself
    // comes a little later — the canceller's filter covers that)
    func align() {
        if refSeen == 0 || micEnd - refSeen > 0.15 {  // nothing plays: silence for the Mac's sound
            if refQ.count < micQ.count {
                refQ.append(contentsOf: [Int16](repeating: 0, count: micQ.count - refQ.count))
            } else if refQ.count > micQ.count {
                refQ.removeLast(refQ.count - micQ.count)
            }
            refEnd = micEnd
            return
        }
        // refQ starts 20 ms before micQ: the echo always lands after its reference, even if a time stamp is late
        let want = micQ.count + Int(((refEnd - micEnd + 0.02) * 16000).rounded())
        if refQ.count > want + 32 {
            refQ.removeFirst(min(refQ.count, refQ.count - want))  // older than the oldest mic sample
        } else if refQ.count < want - 32 {
            refQ.insert(contentsOf: [Int16](repeating: 0, count: want - refQ.count), at: 0)
        }
    }

    // pairs out to stdout; false: the listener went away
    func emit() -> Bool {
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
        if micQ.count > 16000 { micQ.removeFirst(micQ.count - 16000) }  // (never more than 1 s behind)
        if Date().timeIntervalSince(lastReport) >= 30 {
            var note = ""
            if refPeak == 0 {
                note = tapCallbacks == 0 ? " (nothing played since dd-ear started — or System Audio Recording not allowed for dd)"
                                         : " (nothing playing)"
            }
            warn(String(format: "levels (30 s): mic peak %.3f, playback peak %.3f%@", micPeak, refPeak, note))
            micPeak = 0
            refPeak = 0
            lastReport = Date()
        }
        return true
    }

    // 2. The mic, on its own IO proc (it never waits for the tap)
    let mfmts = inputStreamFormats(micDev)
    guard let m0 = mfmts.first, isFloat32(m0) else {
        warn("unexpected mic stream format: \(mfmts.map { "id \($0.mFormatID) bits \($0.mBitsPerChannel)" })")
        exit(11)
    }
    let micRate = rateOf(micDev) > 0 ? rateOf(micDev) : m0.mSampleRate
    guard let mf = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: micRate, channels: 1, interleaved: false),
          let mc = AVAudioConverter(from: mf, to: outFmt) else {
        warn("audio converter failed for \(micRate) Hz")
        exit(11)
    }
    micFmt = mf
    micConv = mc
    var st = AudioDeviceCreateIOProcIDWithBlock(&micProc, micDev, ioQueue) { _, inInput, inTime, _, _ in
        guard let fmt = micFmt, let conv = micConv else { return }
        micCallbacks += 1
        lastMic = Date()
        let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInput))
        if micCallbacks == 1 || micCallbacks == 200 {  // (diagnostics: what the first callbacks carry)
            warn("callback \(micCallbacks): \(abl.count) buffers: " + abl.map { b in
                "\(b.mNumberChannels) ch \(b.mDataByteSize) B\(b.mData == nil ? " (no data)" : "")" }.joined(separator: ", "))
        }
        guard abl.count >= 1, let mp = abl[0].mData?.assumingMemoryBound(to: Float.self) else { return }
        let ch = max(1, Int(abl[0].mNumberChannels))
        let frames = Int(abl[0].mDataByteSize) / (4 * ch)
        convert(conv, fmt, UnsafePointer(mp), ch, frames, &micPeak, &micQ)
        micEnd = hostSeconds(inTime) + Double(frames) / fmt.sampleRate
        align()
        if !emit() {  // the listener went away
            cleanup()
            exit(0)
        }
    }
    guard st == noErr, micProc != nil else {
        warn("mic IO proc failed: OSStatus \(st)")
        exit(10)
    }
    st = AudioDeviceStart(micDev, micProc)
    guard st == noErr else {
        warn("mic start failed: OSStatus \(st)")
        cleanup()
        exit(10)
    }

    // 3. A tap of everything this Mac plays (mono mix of all apps, not muted: the film keeps playing as it was), in
    //    its own private aggregate device clocked by the output device; its callbacks come only while something plays.
    func buildTap() -> String? {
        guard let outUID = stringProp(outDev, kAudioDevicePropertyDeviceUID) else { return "no output device" }
        let desc = CATapDescription(monoGlobalTapButExcludeProcesses: [])
        desc.uuid = UUID()
        desc.name = "dd-ear"
        desc.muteBehavior = .unmuted
        if desc.responds(to: NSSelectorFromString("setPrivateTap:")) {  // (only visible to dd; KVC: the Swift name
            desc.setValue(true, forKey: "privateTap")                    //  of this flag differs between SDKs)
        }
        var s = AudioHardwareCreateProcessTap(desc, &tapID)
        guard s == noErr, tapID != unknownID else {
            tapID = unknownID
            return "system audio tap failed: OSStatus \(s)"
        }
        let agg: [String: Any] = [
            kAudioAggregateDeviceNameKey: "dd-ear",
            kAudioAggregateDeviceUIDKey: "dd-ear-" + UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: desc.uuid.uuidString]],
        ]
        s = AudioHardwareCreateAggregateDevice(agg as CFDictionary, &aggID)
        guard s == noErr, aggID != unknownID else {
            aggID = unknownID
            destroyTap()
            return "tap aggregate device failed: OSStatus \(s)"
        }
        var formats = inputStreamFormats(aggID)
        for _ in 0..<20 where formats.isEmpty {  // (the tap's stream can show up a moment after creation)
            Thread.sleep(forTimeInterval: 0.1)
            formats = inputStreamFormats(aggID)
        }
        guard let tf = formats.last, isFloat32(tf) else {
            destroyTap()
            return "unexpected tap streams: \(formats.map { "\($0.mChannelsPerFrame) ch id \($0.mFormatID) bits \($0.mBitsPerChannel)" })"
        }
        let nominal = rateOf(aggID)
        let rate = nominal > 0 ? nominal : tf.mSampleRate
        guard let f = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false),
              let c = AVAudioConverter(from: f, to: outFmt) else {
            destroyTap()
            return "tap audio converter failed for \(rate) Hz"
        }
        ioQueue.sync {
            tapFmt = f
            tapConv = c
        }
        s = AudioDeviceCreateIOProcIDWithBlock(&tapProc, aggID, ioQueue) { _, inInput, inTime, _, _ in
            guard let fmt = tapFmt, let conv = tapConv else { return }
            tapCallbacks += 1
            let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInput))
            if tapCallbacks == 1 || tapCallbacks == 200 {
                warn("tap callback \(tapCallbacks): \(abl.count) buffers: " + abl.map { b in
                    "\(b.mNumberChannels) ch \(b.mDataByteSize) B\(b.mData == nil ? " (no data)" : "")" }.joined(separator: ", "))
            }
            guard abl.count >= 1 else { return }
            let tb = abl[abl.count - 1]
            guard let tp = tb.mData?.assumingMemoryBound(to: Float.self), tb.mDataByteSize > 0 else { return }
            let ch = max(1, Int(tb.mNumberChannels))
            let frames = Int(tb.mDataByteSize) / (4 * ch)
            let t0 = hostSeconds(inTime)
            if refEnd > 0 {  // (a pause in the tap, or the first tap audio after silence: place it at its own time)
                let gap = Int(((t0 - refEnd) * 16000).rounded())
                if gap > 32 {
                    refQ.append(contentsOf: [Int16](repeating: 0, count: min(gap, 32000)))
                } else if gap < -32 {
                    refQ.removeLast(min(refQ.count, -gap))
                }
            }
            convert(conv, fmt, UnsafePointer(tp), ch, frames, &refPeak, &refQ)
            refEnd = t0 + Double(frames) / fmt.sampleRate
            refSeen = refEnd
            if refQ.count > 32000 { refQ.removeFirst(refQ.count - 32000) }
        }
        guard s == noErr, tapProc != nil else {
            tapProc = nil
            destroyTap()
            return "tap IO proc failed: OSStatus \(s)"
        }
        s = AudioDeviceStart(aggID, tapProc)
        guard s == noErr else {
            destroyTap()
            return "tap start failed: OSStatus \(s)"
        }
        return nil
    }
    if let why = buildTap() {
        warn("\(why) — the mic alone (no echo cancellation)")
    }
    warn("listening: mic + this Mac's sound (tap) -> 16000 Hz stereo [mic, playback]; mic \(Int(micRate)) Hz " +
         "\(Int(m0.mChannelsPerFrame)) ch; the Mac's sound joins whenever something plays")

    // 4. Watchdog: the mic must keep delivering (the tap may rest while nothing plays)
    let started = Date()
    while true {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.25))
        let (n, last) = ioQueue.sync { (micCallbacks, lastMic) }
        let limit: Double = n == 0 ? 10 : 4
        if Date().timeIntervalSince(n == 0 ? started : last) > limit {
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
