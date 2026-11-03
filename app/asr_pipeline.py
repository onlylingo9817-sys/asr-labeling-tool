#!/usr/bin/env python3
"""Local ASR + speaker-diarization draft builder.

Reads project media, writes an intermediate transcript JSON.
Never writes to Label Studio; see push_predictions.py for that step.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from funasr import AutoModel


@dataclass
class Utterance:
    start: float
    end: float
    text: str
    speaker: str
    overlap: bool = False
    accent: bool = False
    unclear: bool = False
    cut: bool = False


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def probe_duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)]).stdout.strip()
    return float(out)


def decode_audio(source: Path, dest: Path) -> None:
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(dest)])


def load_model() -> AutoModel:
    return AutoModel(model="paraformer-zh", vad_model="fsmn-vad",
                     punc_model="ct-punc", spk_model="cam++", device="mps",
                     disable_update=True, disable_pbar=True, log_level="ERROR")


def transcribe(model: AutoModel, path: Path) -> list[dict[str, Any]]:
    result = model.generate(input=str(path), batch_size_s=300,
                            return_spk_res=True, sentence_timestamp=True)
    if isinstance(result, dict):
        return [result]
    return [x for x in result if isinstance(x, dict)]


def _speaker_name(raw: Any, mapping: dict[str, str]) -> str:
    key = str(raw)
    if key not in mapping:
        mapping[key] = f"SPEAKER_{len(mapping) + 1}"
    return mapping[key]


def flatten_sentences(result: list[dict[str, Any]], duration: float):
    rows, mapping = [], {}
    for record in result:
        for sentence in record.get("sentence_info") or []:
            text = str(sentence.get("text") or "").strip()
            if not text:
                continue
            start = max(0.0, float(sentence.get("start", 0)) / 1000.0)
            end = min(duration, float(sentence.get("end", 0)) / 1000.0)
            if end <= start:
                continue
            rows.append({"start": start, "end": end, "text": text,
                         "speaker": _speaker_name(sentence.get("spk", "unknown"), mapping)})
    if not rows:
        for record in result:
            text = str(record.get("text") or "").strip()
            if text:
                rows.append({"start": 0.0, "end": duration, "text": text,
                             "speaker": "SPEAKER_1"})
    return rows, mapping


def split_long_row(row: dict[str, Any], max_seconds: float = 18.0):
    start, end, text = row["start"], row["end"], row["text"]
    if end - start <= max_seconds or len(text) < 12:
        return [row]
    parts = [p for p in re.split(r"(?<=[，,；;。！？!?])", text) if p]
    if len(parts) <= 1:
        return [row]
    total = sum(len(p) for p in parts)
    out, cursor = [], start
    for part in parts:
        part_end = min(end, cursor + (end - start) * len(part) / total)
        out.append({**row, "start": cursor, "end": part_end, "text": part})
        cursor = part_end
    if out:
        out[-1]["end"] = end
    return out


def merge_tiny_fragments(rows):
    if not rows:
        return rows
    merged = [rows[0]]
    for row in rows[1:]:
        prev = merged[-1]
        gap = row["start"] - prev["end"]
        if (prev["speaker"] == row["speaker"] and gap < 0.35
                and (len(prev["text"]) <= 2 or len(row["text"]) <= 2)
                and not re.search(r"[。！？!?]$", prev["text"])):
            prev["text"] += row["text"]
            prev["end"] = row["end"]
        else:
            merged.append(row)
    return merged


def detect_overlap(rows):
    for i, row in enumerate(rows):
        for other in rows[i + 1:i + 8]:
            if other["start"] >= row["end"]:
                break
            if other["speaker"] != row["speaker"] and other["start"] < row["end"] - 0.05:
                row["overlap"] = True
                other["overlap"] = True


def map_roles(rows):
    """Map anonymous clusters to roles using cumulative talk time.

    Salesperson is the dominant speaker; customers are ordered by first speech.
    Short interjections often attach to the wrong cluster, so first-speaker is a
    weaker signal than total duration here.
    """
    stats, first_seen = {}, {}
    for row in rows:
        s = row["speaker"]
        stats[s] = stats.get(s, 0.0) + (row["end"] - row["start"])
        first_seen.setdefault(s, row["start"])
    ordered = sorted(stats, key=lambda s: (-stats[s], first_seen[s]))
    mapping, cust = {}, 1
    for idx, speaker in enumerate(ordered):
        if idx == 0:
            mapping[speaker] = "SALES_1"
        else:
            mapping[speaker] = f"CUSTOMER_{cust}"
            cust += 1
    return mapping




def group_same_speaker(rows, max_seconds=18.0, max_gap=0.55):
    """Merge adjacent clauses from one speaker into natural turns.

    Human reference annotations use speaker turns / natural pauses, not every
    comma-delimited clause.  This keeps one-word replies separate and preserves
    long monologues up to a bounded duration.
    """
    if not rows:
        return rows
    merged = [rows[0]]
    for row in rows[1:]:
        prev = merged[-1]
        gap = row["start"] - prev["end"]
        can_merge = (
            prev["speaker"] == row["speaker"]
            and gap <= max_gap
            and row["end"] - prev["start"] <= max_seconds
            and not re.search(r"[。！？!?]$", prev["text"])
            and len(prev["text"]) + len(row["text"]) >= 4
        )
        if can_merge:
            prev["text"] += row["text"]
            prev["end"] = row["end"]
        else:
            merged.append(row)
    return merged


DOMAIN_REPLACEMENTS = [
    (r"(?i)\bm\s*七\s*零\b", "N70"),
    (r"(?i)\bm\s*70\b", "N70"),
    (r"(?i)\bn\s*九\s*零\b", "N90"),
    (r"(?i)\bn\s*90\b", "N90"),
    (r"(?i)\bsu\s*7\b", "SU7"),
    (r"(?i)\byu\s*7\b", "YU7"),
]


def normalize_domain_terms(text: str) -> str:
    for pattern, replacement in DOMAIN_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    return text


def clean_text(text: str):
    text = text.strip().replace(" ", "")
    unclear = "[不清]" in text or "□" in text
    text = re.sub(r"[“”‘’]", "", text)
    return text, unclear


def calibrate_roles(rows, reference):
    """Map ASR clusters to human speaker labels by maximum time overlap.

    `reference` is a list of {start,end,speaker} from an existing human annotation.
    This is the standard's "map model IDs to roles by overlap duration" rule.
    """
    if not reference:
        return None
    clusters = {r["speaker"] for r in rows}
    scores = {c: {} for c in clusters}
    for c in clusters:
        for ref in reference:
            overlap = 0.0
            for r in rows:
                if r["speaker"] != c:
                    continue
                overlap += max(0.0, min(r["end"], ref["end"]) - max(r["start"], ref["start"]))
            scores[c][ref["speaker"]] = scores[c].get(ref["speaker"], 0.0) + overlap
    mapping = {}
    used = set()
    for c in sorted(clusters, key=lambda x: -sum(scores[x].values())):
        choices = sorted(scores[c].items(), key=lambda kv: -kv[1])
        for label, _ in choices:
            if label not in used:
                mapping[c] = label
                used.add(label)
                break
        else:
            mapping[c] = "UNKNOWN"
    return mapping


def build(path: Path, output: Path, reference: list[dict] | None = None,
          chunk_seconds: int = 300):
    duration = probe_duration(path)
    if duration > chunk_seconds * 2:
        return build_chunked(path, output, reference, chunk_seconds)
    with tempfile.TemporaryDirectory(prefix="asr-") as tmp:
        wav = Path(tmp) / "audio.wav"
        decode_audio(path, wav)
        started = time.time()
        result = transcribe(load_model(), wav)
        elapsed = time.time() - started
    rows, clusters = flatten_sentences(result, duration)
    rows = [part for row in rows for part in split_long_row(row)]
    rows = merge_tiny_fragments(rows)
    rows = group_same_speaker(rows)
    detect_overlap(rows)
    role_map = calibrate_roles(rows, reference) or map_roles(rows)
    utterances = []
    for row in rows:
        text, unclear = clean_text(row["text"])
        text = normalize_domain_terms(text)
        if not text:
            continue
        utterances.append(Utterance(
            start=round(row["start"], 3), end=round(row["end"], 3), text=text,
            speaker=role_map.get(row["speaker"], "UNKNOWN"),
            overlap=bool(row.get("overlap")), unclear=unclear))
    payload = {"audio": str(path), "duration": round(duration, 3),
               "engine": "FunASR paraformer-zh + fsmn-vad + ct-punc + cam++",
               "processing_seconds": round(elapsed, 2),
               "raw_speaker_clusters": clusters, "role_map": role_map,
               "utterances": [asdict(u) for u in utterances]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_chunked(path: Path, output: Path, reference: list[dict] | None,
                  chunk_seconds: int):
    """Process a long recording sequentially, releasing the model between chunks.

    The media is a single mixed track, so every chunk keeps the full recording's
    temporal coordinates; offsets are added back to absolute recording time.
    """
    import tempfile
    duration = probe_duration(path)
    started = time.time()
    all_rows = []
    all_clusters = {}
    model = load_model()
    with tempfile.TemporaryDirectory(prefix="asr-chunks-") as tmp:
        tmp = Path(tmp)
        for idx, offset in enumerate(range(0, int(duration), chunk_seconds)):
            length = min(chunk_seconds, duration - offset)
            wav = tmp / f"chunk_{idx:03d}.wav"
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(offset),
                 "-t", str(length), "-i", str(path), "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", "-y", str(wav)])
            result = transcribe(model, wav)
            rows, clusters = flatten_sentences(result, length)
            for row in rows:
                row["start"] += offset
                row["end"] += offset
            # Speaker clusters reset per chunk.  Resolve roles chunk-locally,
            # using the human reference where it overlaps and dominant-speaker
            # heuristics elsewhere, so labels stay consistent when merged.
            chunk_ref = None
            if reference:
                chunk_ref = [
                    r for r in reference
                    if r["end"] > offset and r["start"] < offset + length
                ]
            role = calibrate_roles(rows, chunk_ref) or map_roles(rows)
            for row in rows:
                row["speaker"] = role.get(row["speaker"], "UNKNOWN")
            all_rows.extend(rows)
            all_clusters.update({f"c{idx}:{k}": v for k, v in clusters.items()})
            print(f"[chunk {idx + 1}] {offset}s-{offset + length}s rows={len(rows)} roles={role}", flush=True)
            progress = output.with_suffix('.progress.json')
            progress.write_text(json.dumps({
                "audio": str(path), "duration": round(duration, 3),
                "chunks_done": idx + 1, "total_chunks": (int(duration) + chunk_seconds - 1) // chunk_seconds,
                "rows": len(all_rows), "offset": offset + length, "finished": False,
            }, ensure_ascii=False), encoding='utf-8')
    elapsed = time.time() - started
    rows = [part for row in all_rows for part in split_long_row(row)]
    rows = merge_tiny_fragments(rows)
    rows = group_same_speaker(rows)
    detect_overlap(rows)
    utterances = []
    for row in rows:
        text, unclear = clean_text(row["text"])
        text = normalize_domain_terms(text)
        if not text:
            continue
        utterances.append(Utterance(start=round(row["start"], 3), end=round(row["end"], 3),
                                    text=text, speaker=row["speaker"],
                                    overlap=bool(row.get("overlap")), unclear=unclear))
    progress = output.with_suffix('.progress.json')
    progress.write_text(json.dumps({
        "audio": str(path), "duration": round(duration, 3),
        "chunks_done": idx + 1, "total_chunks": idx + 1,
        "rows": len(all_rows), "offset": duration, "finished": True,
    }, ensure_ascii=False), encoding='utf-8')
    payload = {"audio": str(path), "duration": round(duration, 3),
               "engine": "FunASR paraformer-zh + fsmn-vad + ct-punc + cam++ (chunked)",
               "processing_seconds": round(elapsed, 2),
               "raw_speaker_clusters": all_clusters, "role_map": {},
               "utterances": [asdict(u) for u in utterances]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reference", type=Path, default=None)
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text()) if args.reference else None
    payload = build(args.audio, args.output, reference=reference)
    print(json.dumps({"utterances": len(payload["utterances"]),
                      "duration": payload["duration"],
                      "processing_seconds": payload["processing_seconds"],
                      "role_map": payload["role_map"],
                      "output": str(args.output)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
