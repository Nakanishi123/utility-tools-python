import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple

from reazonspeech.k2.asr import load_model, transcribe, audio_from_path


def run(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            + " ".join(cmd)
            + "\n\n--- stdout ---\n"
            + p.stdout
            + "\n\n--- stderr ---\n"
            + p.stderr
        )


def srt_time(seconds: float) -> str:
    # 00:00:00,000
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h = ms // 3_600_000
    ms %= 3_600_000
    m = ms // 60_000
    ms %= 60_000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def natural_sort_key(p: Path) -> Tuple:
    # seg_000001.wav の数字でソート
    m = re.search(r"(\d+)", p.stem)
    return (int(m.group(1)) if m else -1, p.name)


def segment_audio_from_video(video_path: Path, out_dir: Path, segment_sec: int) -> List[Path]:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg が見つかりません。PATHに入れてください。")

    out_pattern = str(out_dir / "seg_%06d.wav")

    # -vn: 映像を捨てる
    # 16kHz/mono: ASRで扱いやすい定番
    # segment muxerで分割、reset_timestampsで各セグメントの先頭を0に
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "segment",
        "-segment_time",
        str(segment_sec),
        "-reset_timestamps",
        "1",
        out_pattern,
    ]
    run(cmd)

    segs = sorted(out_dir.glob("seg_*.wav"), key=natural_sort_key)
    if not segs:
        raise RuntimeError("音声セグメントが生成されませんでした。入力動画を確認してください。")
    return segs


def tokens_to_text(tokens: List[str]) -> str:
    # ざっくり連結（K2のret.textもあるけど、SRT生成側と揃えるため共通化）
    return "".join(tokens).strip()


def build_srt_from_subwords(
    subwords,  # ret.subwords: seconds/token を持つ
    segment_offset: float,
    max_line_sec: float = 4.0,
    max_chars: int = 26,
    pad_end_sec: float = 0.25,
) -> List[Tuple[float, float, str]]:
    """
    subwords を「数秒ごと or 文字数ごと」でまとめて字幕行を作る。
    返り値: (start_sec, end_sec, text) のリスト（secは動画の絶対時刻）
    """
    entries: List[Tuple[float, float, str]] = []
    if not subwords:
        return entries

    cur_tokens: List[str] = []
    cur_start: float = None
    cur_last: float = None

    for sw in subwords:
        t = float(sw.seconds) + segment_offset
        tok = str(sw.token)

        if cur_start is None:
            cur_start = t
            cur_last = t
            cur_tokens = [tok]
            continue

        cur_last = t
        cur_tokens.append(tok)

        text = tokens_to_text(cur_tokens)
        if (cur_last - cur_start) >= max_line_sec or len(text) >= max_chars:
            entries.append((cur_start, cur_last + pad_end_sec, text))
            cur_start = None
            cur_last = None
            cur_tokens = []

    if cur_tokens and cur_start is not None and cur_last is not None:
        entries.append((cur_start, cur_last + pad_end_sec, tokens_to_text(cur_tokens)))

    return entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=str, help="入力動画ファイルパス")
    ap.add_argument("--device", type=str, default="cuda", help="cpu / cuda")
    ap.add_argument("--segment-sec", type=int, default=25, help="K2の30秒制限回避のための分割秒数（推奨: 20〜28）")
    ap.add_argument("--out-txt", type=str, default=None, help="全文テキスト出力（省略時は video名.txt）")
    ap.add_argument("--out-srt", type=str, default=None, help="字幕SRT出力（省略時は video名.srt）")
    ap.add_argument("--keep-temp", action="store_true", help="一時ファイルを残す（デバッグ用）")
    args = ap.parse_args()

    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(str(video_path))

    out_txt = Path(args.out_txt) if args.out_txt else video_path.with_suffix(".txt")
    out_srt = Path(args.out_srt) if args.out_srt else video_path.with_suffix(".srt")

    # モデルは一回だけロード
    model = load_model(device=args.device)

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        seg_dir = work / "segs"
        seg_dir.mkdir(parents=True, exist_ok=True)

        segs = segment_audio_from_video(video_path, seg_dir, args.segment_sec)

        all_text_parts: List[str] = []
        srt_entries: List[Tuple[float, float, str]] = []

        for i, wav_path in enumerate(segs):
            # 各セグメントの開始時刻（segment_sec固定の前提）
            offset = i * float(args.segment_sec)

            audio = audio_from_path(str(wav_path))
            ret = transcribe(model, audio)

            # ざっくり全文（セグメント単位）
            seg_text = ret.text.strip()
            if seg_text:
                all_text_parts.append(seg_text)

            # SRTは subwords から作る（タイムスタンプ付き）
            # 公式Quickstartにある ret.subwords を利用 :contentReference[oaicite:5]{index=5}
            if hasattr(ret, "subwords") and ret.subwords:
                srt_entries.extend(build_srt_from_subwords(ret.subwords, segment_offset=offset))

        # TXT出力
        full_text = "\n".join(all_text_parts).strip()
        out_txt.write_text(full_text + "\n", encoding="utf-8")

        # SRT出力
        # 連番で書く
        lines: List[str] = []
        for idx, (st, ed, txt) in enumerate(srt_entries, start=1):
            if not txt:
                continue
            lines.append(str(idx))
            lines.append(f"{srt_time(st)} --> {srt_time(ed)}")
            lines.append(txt)
            lines.append("")  # blank line
        out_srt.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

        if args.keep_temp:
            # 一時ディレクトリを残したい場合はコピー
            dst = video_path.parent / f"{video_path.stem}_reazon_tmp"
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(work, dst)
            print(f"[keep-temp] temp copied to: {dst}")

    print(f"OK: {out_txt}")
    print(f"OK: {out_srt}")


if __name__ == "__main__":
    main()
