#!/usr/bin/env python3
"""Sentence-preserving interjection split.

For every ASR sentence we keep the FULL [start, end] span, and pull out any
interjection that has a real pause around it as a CHILD interval.  The child
therefore always lies inside the parent's [start, end] - which is what actually
happened in the audio.

Output: sentences[] each with {start,end,text,spk,pieces[]}; pieces have
millisecond precision.
"""
from __future__ import annotations

import argparse, json, re, subprocess, tempfile, time
from pathlib import Path
from funasr import AutoModel

INTERJ = "嗯啊哦噢喔哎唉诶呀哈呃额"
PUNCT = "，,。！？!?；;、"

def run(cmd):
    return subprocess.run(cmd, check=True, text=True, capture_output=True)

def probe(path):
    return float(run(["ffprobe","-v","error","-show_entries","format=duration",
                      "-of","default=nw=1:nk=1",str(path)]).stdout.strip())

def build_model():
    return AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc",
                     device="mps", disable_update=True, disable_pbar=True, log_level="ERROR")

def transcribe(path, model, chunk_seconds=300):
    duration = probe(path)
    sentences = []
    with tempfile.TemporaryDirectory(prefix="nested-") as tmp:
        tmp = Path(tmp)
        for idx, offset in enumerate(range(0, int(duration), chunk_seconds)):
            length = min(chunk_seconds, duration - offset)
            wav = tmp / f"c{idx:03d}.wav"
            run(["ffmpeg","-hide_banner","-loglevel","error","-ss",str(offset),"-t",str(length),
                 "-i",str(path),"-ac","1","-ar","16000","-c:a","pcm_s16le","-y",str(wav)])
            result = model.generate(input=str(wav), batch_size_s=300,
                                    return_spk_res=True, sentence_timestamp=True)
            rec = result[0] if isinstance(result, list) else result
            for s in rec.get("sentence_info") or []:
                text = str(s.get("text") or "").strip()
                if not text:
                    continue
                stamps = [[offset + a/1000.0, offset + b/1000.0] for a, b in (s.get("timestamp") or [])]
                sentences.append({
                    "start": round(offset + float(s.get("start",0))/1000.0, 3),
                    "end": round(offset + float(s.get("end",0))/1000.0, 3),
                    "text": text,
                    "spk": s.get("spk"),
                    "timestamps": stamps,
                })
            print(f"[chunk {idx+1}] {offset}-{offset+length}s sentences={len(sentences)}", flush=True)
    return sentences, duration

def align_chars(text, timestamps):
    chars = [c for c in text if c.strip() and c not in PUNCT]
    if len(chars) != len(timestamps):
        return None
    return [(c, t[0], t[1]) for c, t in zip(chars, timestamps)]

def pieces_for(sentence):
    """Split into body/interjection pieces, preserving absolute ms timestamps."""
    text = sentence["text"]
    aligned = align_chars(text, sentence["timestamps"])
    if not aligned:
        return [{"text": text, "start": sentence["start"], "end": sentence["end"],
                 "isInterjection": False}]
    n = len(aligned)
    is_int = [False]*n
    i = 0
    while i < n:
        if aligned[i][0] in INTERJ:
            j = i
            while j+1 < n and aligned[j+1][0] in INTERJ:
                j += 1
            for k in range(i, j+1):
                is_int[k] = True
            i = j+1
        else:
            i += 1
    cuts = {0, n}
    idx = 0
    while idx < n:
        if is_int[idx]:
            j = idx
            while j+1 < n and is_int[j+1]:
                j += 1
            left_gap = aligned[idx][1]-aligned[idx-1][2] if idx > 0 else 9
            right_gap = aligned[j+1][1]-aligned[j][2] if j+1 < n else 9
            if left_gap >= 0.08 or idx == 0:
                cuts.add(idx)
            if right_gap >= 0.08 or j == n-1:
                cuts.add(j+1)
            idx = j+1
        else:
            idx += 1
    cuts = sorted(cuts)
    pieces = []
    for a, b in zip(cuts, cuts[1:]):
        chars = aligned[a:b]
        if not chars:
            continue
        body = "".join(c for c,_,_ in chars).strip()
        if not body:
            continue
        pieces.append({
            "text": body + "，",
            "start": round(chars[0][1], 3),
            "end": round(chars[-1][2], 3),
            "isInterjection": all(c in INTERJ for c,_,_ in chars),
        })
    if not pieces:
        pieces = [{"text": text, "start": sentence["start"], "end": sentence["end"],
                   "isInterjection": False}]
    return pieces

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--chunk-seconds", type=int, default=300)
    args = ap.parse_args()
    started = time.time()
    audio = Path(args.audio).resolve()
    model = build_model()
    sentences, duration = transcribe(audio, model, args.chunk_seconds)
    for s in sentences:
        s["pieces"] = pieces_for(s)
    payload = {
        "audio": str(audio), "duration": round(duration, 3),
        "engine": "FunASR paraformer-zh + char timestamps (nested interjection)",
        "sentence_count": len(sentences),
        "interjection_count": sum(1 for s in sentences for p in s["pieces"] if p["isInterjection"]),
        "elapsed": round(time.time()-started, 1),
        "sentences": sentences,
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("sentences", len(sentences), "interjections", payload["interjection_count"],
          "elapsed", payload["elapsed"])

if __name__ == "__main__":
    main()
