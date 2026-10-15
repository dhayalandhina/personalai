#!/usr/bin/env python3
"""video_pipeline.py — storyboard.json -> clips (ComfyUI / diffusers / mock) -> stitched MP4 (stdlib + ffmpeg).

  run STORYBOARD.json --out final.mp4 [--backend comfyui|diffusers|mock] [--workflow api.json] [--seed N]
                      [--narrate] [--subtitles] [--music bg.mp3] [--crossfade 0.3] [--compress] [--model ID]
  stitch CLIP1 CLIP2 ... --out final.mp4 [--crossfade 0.3]
  probe FILE.mp4
  --selftest
Env: COMFYUI_URL (default http://127.0.0.1:8188), DD_HOME. Clips land in $DD_HOME/video/<job>/.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
COMFY = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
COLORS = ["0x1f4e79", "0x7a3b1f", "0x2d5f2d", "0x5a2d5f", "0x80572b", "0x2b6d80", "0x6b2b2b", "0x3b3b6b"]


def home() -> Path:
    h = Path(os.environ.get("DD_HOME", Path.home() / ".dd")) / "video"
    h.mkdir(parents=True, exist_ok=True)
    return h


def sh(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ffmpeg_has(filter_name: str) -> bool:
    r = sh(["ffmpeg", "-hide_banner", "-filters"], 60)
    return re.search(rf"\b{filter_name}\b", r.stdout) is not None


def probe(path: str) -> dict:
    r = sh(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], 60)
    if r.returncode != 0:
        return {"error": r.stderr.strip()[:200], "path": path}
    d = json.loads(r.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    fps = round(int(num) / int(den), 2) if den and int(den) else 0
    return {"path": path, "seconds": round(float(d.get("format", {}).get("duration", 0)), 2),
            "width": v.get("width"), "height": v.get("height"), "fps": fps, "codec": v.get("codec_name"),
            "audio": bool(a), "mb": round(int(d.get("format", {}).get("size", 0)) / 1e6, 2)}


# ---------------- backends ----------------
def gen_mock(scene: dict, sb: dict, out: Path, idx: int) -> None:
    w, h, sec, fps = sb.get("width", 1280), sb.get("height", 704), scene.get("seconds", 5), sb.get("fps", 24)
    color = COLORS[idx % len(COLORS)]
    vf = []
    if ffmpeg_has("drawtext"):
        txt = re.sub(r"[^A-Za-z0-9 ]", "", scene["prompt"][:60])
        vf = ["-vf", f"drawtext=text='scene {scene['n']}\\: {txt}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={sec}:r={fps}",
           *vf, "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", str(out)]
    r = sh(cmd, 300)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg mock failed: {r.stderr[-300:]}")


def http_json(url: str, data: dict | None = None, timeout: int = 60) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fill_workflow(wf: dict, scene: dict, sb: dict, seed: int) -> dict:
    wf = json.loads(json.dumps(wf))
    wf.pop("_comment", None)
    frames = int(scene.get("seconds", 5) * sb.get("fps", 24)) + 1
    for nid, node in wf.items():
        ct, title = node.get("class_type", ""), (node.get("_meta", {}).get("title", "") or "").lower()
        inp = node.setdefault("inputs", {})
        if ct == "CLIPTextEncode":
            if "negative" in title:
                inp["text"] = scene.get("negative", "")
            elif "positive" in title or inp.get("text") == "POSITIVE_PROMPT":
                inp["text"] = scene["prompt"]
        elif ct in ("KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced", "RandomNoise"):
            for k in ("seed", "noise_seed"):
                if k in inp:
                    inp[k] = int(scene.get("seed", seed))
        elif ct in ("Wan22ImageToVideoLatent", "EmptyHunyuanLatentVideo", "EmptyLTXVLatentVideo", "EmptyCosmosLatentVideo",
                    "EmptyMochiLatentVideo", "WanImageToVideo"):
            for k, v in (("width", sb.get("width", 1280)), ("height", sb.get("height", 704)), ("length", frames)):
                if k in inp:
                    inp[k] = v
        elif ct == "LoadImage" and scene.get("image"):
            inp["image"] = Path(scene["image"]).name
        elif ct in ("SaveVideo", "VHS_VideoCombine", "SaveAnimatedWEBP"):
            inp["filename_prefix"] = f"dd/{scene.get('job', 'job')}_s{scene['n']:02d}"
    return wf


def comfy_upload(path: str) -> None:
    boundary = uuid.uuid4().hex
    data = Path(path).read_bytes()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{Path(path).name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{COMFY}/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    urllib.request.urlopen(req, timeout=120).read()


def gen_comfy(scene: dict, sb: dict, out: Path, workflow_path: str | None, seed: int, job: str) -> None:
    wf = json.loads(Path(workflow_path or SKILL / "assets" / "wan22_t2v_workflow.json").read_text(encoding="utf-8"))
    if scene.get("image"):
        comfy_upload(scene["image"])
    scene = {**scene, "job": job}
    prompt = fill_workflow(wf, scene, sb, seed)
    pid = http_json(f"{COMFY}/prompt", {"prompt": prompt, "client_id": f"dd-{job}"})["prompt_id"]
    deadline = time.time() + 3600
    while time.time() < deadline:
        hist = http_json(f"{COMFY}/history/{pid}")
        if pid in hist:
            entry = hist[pid]
            if entry.get("status", {}).get("status_str") == "error":
                raise RuntimeError(f"ComfyUI error: {json.dumps(entry.get('status'))[:300]}")
            outputs = entry.get("outputs", {})
            files = []
            for node_out in outputs.values():
                for key in ("videos", "gifs", "images"):
                    for f in node_out.get(key, []):
                        if f.get("filename", "").lower().endswith((".mp4", ".webm", ".mov", ".webp", ".gif")):
                            files.append(f)
            if files:
                f = files[0]
                q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": f.get("type", "output")})
                with urllib.request.urlopen(f"{COMFY}/view?{q}", timeout=600) as r, open(out, "wb") as fh:
                    shutil.copyfileobj(r, fh)
                if out.suffix != ".mp4" or not f["filename"].lower().endswith(".mp4"):
                    tmp = out.with_suffix(".src" + Path(f["filename"]).suffix)
                    shutil.move(out, tmp)
                    r2 = sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                             "-r", str(sb.get("fps", 24)), str(out)])
                    if r2.returncode != 0:
                        raise RuntimeError(r2.stderr[-300:])
                return
        time.sleep(3)
    raise TimeoutError("ComfyUI job did not finish within 60 min")


def gen_diffusers(scene: dict, sb: dict, out: Path, seed: int, model: str | None) -> None:
    frames = int(scene.get("seconds", 5) * sb.get("fps", 24)) + 1
    cmd = [sys.executable, str(HERE / "diffusers_t2v.py"), "--prompt", scene["prompt"], "--negative", scene.get("negative", ""),
           "--out", str(out), "--frames", str(frames), "--width", str(sb.get("width", 1280)), "--height", str(sb.get("height", 704)),
           "--fps", str(sb.get("fps", 24)), "--seed", str(scene.get("seed", seed))]
    if model:
        cmd += ["--model", model]
    if scene.get("image"):
        cmd += ["--image", scene["image"]]
    r = sh(cmd, 7200)
    if r.returncode != 0:
        raise RuntimeError(f"diffusers failed: {r.stderr[-400:]}")


# ---------------- assembly ----------------
def stitch(clips: list[str], out: str, crossfade: float = 0.0, fps: int = 24) -> dict:
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if crossfade > 0 and len(clips) > 1:
        durs = [probe(c)["seconds"] for c in clips]
        inputs = []
        for c in clips:
            inputs += ["-i", c]
        fc, prev, offset = [], "[0:v]", 0.0
        for i in range(1, len(clips)):
            offset += durs[i - 1] - crossfade
            nxt = f"[v{i}]" if i < len(clips) - 1 else "[vout]"
            fc.append(f"{prev}[{i}:v]xfade=transition=fade:duration={crossfade}:offset={offset:.3f}{nxt}")
            prev = nxt
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(fc), "-map", "[vout]",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), "-an", out]
    else:
        lst = Path(out).with_suffix(".concat.txt")
        lst.write_text("".join(f"file '{Path(c).resolve()}'\n" for c in clips))
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264",
               "-pix_fmt", "yuv420p", "-r", str(fps), "-an", out]
    r = sh(cmd, 1800)
    if r.returncode != 0:
        return {"error": r.stderr[-300:]}
    return {"ok": True, **probe(out)}


def make_srt(sb: dict, path: Path) -> None:
    t = 0.0
    lines = []
    for i, s in enumerate(sb["scenes"], 1):
        start, end = t, t + s.get("seconds", 5)
        t = end
        if s.get("narration"):
            fmt = lambda x: f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{int(x % 60):02d},{int((x % 1) * 1000):03d}"
            lines.append(f"{i}\n{fmt(start)} --> {fmt(end - 0.1)}\n{s['narration']}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def narration_track(sb: dict, job: Path) -> str | None:
    tts = SKILL.parent / "voice" / "scripts" / "tts.py"
    if not tts.exists():
        return None
    parts = []
    for s in sb["scenes"]:
        wav = job / f"nar_{s['n']:02d}.wav"
        sec = s.get("seconds", 5)
        if s.get("narration"):
            r = sh([sys.executable, str(tts), s["narration"], "--out", str(wav)], 300)
            if r.returncode != 0 or not wav.exists():
                return None
            padded = job / f"nar_{s['n']:02d}_pad.wav"
            sh(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-af", f"apad=whole_dur={sec},atrim=0:{sec}", str(padded)])
            parts.append(padded)
        else:
            silent = job / f"nar_{s['n']:02d}_sil.wav"
            sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"anullsrc=r=22050:cl=mono:d={sec}", str(silent)])
            parts.append(silent)
    lst = job / "nar.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    full = job / "narration.wav"
    r = sh(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), str(full)])
    return str(full) if r.returncode == 0 else None


def mux(video: str, out: str, narration: str | None, music: str | None, srt: Path | None, compress: bool) -> dict:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video]
    fc, maps = [], ["-map", "0:v"]
    ai = 1
    if narration:
        cmd += ["-i", narration]
        nar_idx, ai = ai, ai + 1
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        mus_idx, ai = ai, ai + 1
    if narration and music:
        fc.append(f"[{nar_idx}:a]volume=1.0[n];[{mus_idx}:a]volume=0.15[m];[n][m]amix=inputs=2:duration=first[aout]")
        maps += ["-map", "[aout]"]
    elif narration:
        maps += ["-map", f"{nar_idx}:a"]
    elif music:
        fc.append(f"[{mus_idx}:a]volume=0.4[aout]")
        maps += ["-map", "[aout]"]
    vf = []
    if srt and srt.exists() and ffmpeg_has("subtitles"):
        vf.append(f"subtitles={srt.as_posix()}:force_style='FontSize=20,Outline=1'")
    if compress:
        vf.append("scale='min(1280,iw)':-2")
    if fc:
        cmd += ["-filter_complex", ";".join(fc)]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += maps + ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "28" if compress else "20", "-preset", "medium",
                   "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", out]
    r = sh(cmd, 3600)
    if r.returncode != 0:
        return {"error": r.stderr[-300:]}
    return {"ok": True, **probe(out)}


def run(sb_path: str, out: str, backend: str = "comfyui", workflow: str | None = None, seed: int | None = None,
        narrate: bool = False, subtitles: bool = False, music: str | None = None, crossfade: float = 0.0,
        compress: bool = False, model: str | None = None) -> dict:
    sb = json.loads(Path(sb_path).read_text(encoding="utf-8"))
    seed = seed if seed is not None else sb.get("seed", 42)
    job_id = time.strftime("%Y%m%d-%H%M%S")
    job = home() / job_id
    job.mkdir(parents=True, exist_ok=True)
    clips, log = [], []
    for i, scene in enumerate(sb["scenes"]):
        clip = job / f"scene_{scene['n']:02d}.mp4"
        t0 = time.time()
        if backend == "mock":
            gen_mock(scene, sb, clip, i)
        elif backend == "comfyui":
            gen_comfy(scene, sb, clip, workflow, seed, job_id)
        elif backend == "diffusers":
            gen_diffusers(scene, sb, clip, seed, model)
        else:
            return {"error": f"unknown backend {backend}"}
        p = probe(str(clip))
        log.append({"scene": scene["n"], "secs": p.get("seconds"), "gen_time": round(time.time() - t0, 1)})
        clips.append(str(clip))
    raw = job / "stitched.mp4"
    st = stitch(clips, str(raw), crossfade, sb.get("fps", 24))
    if "error" in st:
        return {"error": f"stitch: {st['error']}", "clips": clips}
    nar = narration_track(sb, job) if narrate else None
    srt = job / "subs.srt"
    if subtitles:
        make_srt(sb, srt)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    if nar or music or (subtitles and srt.exists()) or compress:
        final = mux(str(raw), out, nar, music, srt if subtitles else None, compress)
    else:
        shutil.copy(raw, out)
        final = {"ok": True, **probe(out)}
    if "error" in final:
        return {"error": f"mux: {final['error']}", "stitched": str(raw)}
    expected = sum(s.get("seconds", 5) for s in sb["scenes"]) - (crossfade * (len(clips) - 1))
    final.update({"job_dir": str(job), "backend": backend, "scenes": log, "expected_seconds": round(expected, 2),
                  "duration_ok": abs(final["seconds"] - expected) <= 0.5, "narration": bool(nar),
                  "subtitles": str(srt) if subtitles else None})
    (job / "result.json").write_text(json.dumps(final, indent=1))
    return final


def selftest() -> int:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("video_pipeline selftest ok (ffmpeg not installed here — install ffmpeg to use this skill)")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="dd-vid-"))
    os.environ["DD_HOME"] = str(tmp)
    sb = {"fps": 24, "width": 320, "height": 176, "seed": 7, "scenes": [
        {"n": 1, "seconds": 1, "prompt": "a red bottle slow orbit", "negative": "blurry", "narration": "Scene one."},
        {"n": 2, "seconds": 1, "prompt": "close up of the cap", "negative": "blurry", "narration": ""}]}
    sbp = tmp / "sb.json"
    sbp.write_text(json.dumps(sb))
    res = run(str(sbp), str(tmp / "final.mp4"), backend="mock", subtitles=True)
    assert res.get("ok") and res["duration_ok"], res
    assert res["width"] == 320 and res["fps"] == 24, res
    wf = json.loads((SKILL / "assets" / "wan22_t2v_workflow.json").read_text())
    filled = fill_workflow(wf, {"n": 1, "seconds": 5, "prompt": "P", "negative": "N", "seed": 9}, {"fps": 24, "width": 832, "height": 480}, 1)
    assert filled["5"]["inputs"]["text"] == "P" and filled["6"]["inputs"]["text"] == "N"
    assert filled["8"]["inputs"]["seed"] == 9 and filled["7"]["inputs"]["length"] == 121 and filled["7"]["inputs"]["width"] == 832
    xf = stitch([str(Path(res["job_dir"]) / "scene_01.mp4"), str(Path(res["job_dir"]) / "scene_02.mp4")], str(tmp / "xf.mp4"), 0.3)
    assert xf.get("ok") and abs(xf["seconds"] - 1.7) <= 0.2, xf
    shutil.rmtree(tmp, ignore_errors=True)
    print("video_pipeline selftest ok (mock render, stitch, crossfade, workflow fill)")
    return 0


def arg(argv: list[str], flag: str, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--selftest":
        return selftest()
    cmd = argv[0]
    try:
        if cmd == "run":
            seed = arg(argv, "--seed")
            res = run(argv[1], arg(argv, "--out", "final.mp4"), arg(argv, "--backend", "comfyui"), arg(argv, "--workflow"),
                      int(seed) if seed else None, "--narrate" in argv, "--subtitles" in argv, arg(argv, "--music"),
                      float(arg(argv, "--crossfade", 0)), "--compress" in argv, arg(argv, "--model"))
        elif cmd == "stitch":
            clips = [a for a in argv[1:] if not a.startswith("--") and a != arg(argv, "--out") and a != arg(argv, "--crossfade")]
            res = stitch(clips, arg(argv, "--out", "final.mp4"), float(arg(argv, "--crossfade", 0)))
        elif cmd == "probe":
            res = probe(argv[1])
        else:
            print(__doc__)
            return 2
    except Exception as e:
        res = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if "error" in res else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
