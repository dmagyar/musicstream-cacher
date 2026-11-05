#!/usr/bin/env python3
"""Prove a captured chunk is clean. `verify.py FILE [FILE...]` or `verify.py DIR`.

A capture with interleaved ICY metadata probes perfectly clean with ffprobe and still
sounds crackly, so duration is not proof. Two real checks:

  1. strict MPEG frame chain - every header must point at the next valid header and the
     chain must consume the whole file (no leftover bytes, no resync);
  2. a full decode counted for "Header missing" / invalid-data errors (needs ffmpeg).

Reports the bitrate profiles it finds: a broadcast that changes bitrate mid-file is
normal, the chain parser must not latch onto the first header.
"""
import os
import subprocess
import sys

BR = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
      2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]}
SR = {0b11: {0: 44100, 1: 48000, 2: 32000}, 0b10: {0: 24000, 1: 22050, 2: 16000},
      0b00: {0: 12000, 1: 11025, 2: 8000}}
VER = {0b00: "MPEG2.5", 0b10: "MPEG2", 0b11: "MPEG1"}


def frame_len(h):
    if len(h) < 4 or h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return None
    ver, layer = (h[1] >> 3) & 3, (h[1] >> 1) & 3
    bi, si, pad = (h[2] >> 4) & 0xF, (h[2] >> 2) & 3, (h[2] >> 1) & 1
    if ver == 0b01 or layer != 1 or not 0 < bi < 15 or si == 3 or si not in SR[ver]:
        return None
    br, sr = BR[1 if ver == 0b11 else 2][bi], SR[ver][si]
    return (144000 * br // sr) + pad if ver == 0b11 else (72000 * br // sr) + pad


def chain(d):
    i = n = 0
    prof = {}
    seconds = 0.0
    while i + 4 <= len(d):
        h = d[i:i + 4]
        fl = frame_len(h)
        if not fl:
            return n, i, len(d), prof, seconds, "no valid header at byte %d" % i
        ver = (d[1 + i] >> 3) & 3
        bi, si = (d[2 + i] >> 4) & 0xF, (d[2 + i] >> 2) & 3
        sr = SR[ver][si]
        seconds += 1152.0 / sr
        key = (VER[ver], BR[1 if ver == 0b11 else 2][bi], sr)
        prof[key] = prof.get(key, 0) + 1
        n += 1
        i += fl
    if i > len(d):
        return n, i, len(d), prof, seconds, "chain runs past EOF by %d B" % (i - len(d))
    if i != len(d):
        return n, i, len(d), prof, seconds, "%d B trailing partial frame" % (len(d) - i)
    return n, i, len(d), prof, seconds, None


def decode_errors(path):
    try:
        p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-f", "null", "-"],
                           capture_output=True, text=True)
    except FileNotFoundError:
        return None
    err = p.stderr or ""
    return sum(err.count(k) for k in ("Header missing", "invalid ", "Invalid data",
                                      "sync extension", "LAME / Libavcodec"))


def probe(path):
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration,bit_rate", "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True)
        return " ".join((p.stdout or "").split()) or "?"
    except FileNotFoundError:
        return "?"


def check(path):
    raw = open(path, "rb").read()
    tag = 0
    if raw[:3] == b"ID3":
        tag = ((raw[6] << 21) | (raw[7] << 14) | (raw[8] << 7) | raw[9]) + 10
    n, consumed, total, prof, secs, bad = chain(raw[tag:])
    dur = secs
    errs = decode_errors(path)
    ok = bad is None and (errs in (None, 0))
    print("%-14s %10s  %6d frames  %6.2f min  chain %d/%d  decode=%s  %s" %
          (os.path.basename(path), "%.1f MB" % (len(raw) / 1e6), n, dur / 60.0,
           consumed, total, "clean" if ok else "PROBLEM",
           "ffprobe duration/bitrate: " + probe(path)))
    if bad:
        print("               chain: %s" % bad)
    if errs:
        print("               decode: %d error line(s)" % errs)
    for k, v in sorted(prof.items(), key=lambda kv: -kv[1]):
        print("               %d x %s %dkbps %dHz" % (v, k[0], k[1], k[2]))
    return ok


if __name__ == "__main__":
    targets = []
    for a in sys.argv[1:] or ["."]:
        if os.path.isdir(a):
            targets += [os.path.join(a, f) for f in sorted(os.listdir(a))
                        if f.endswith(".mp3") and not f.endswith(".part")]
        else:
            targets.append(a)
    if not targets:
        sys.exit("no mp3s found")
    allok = all([check(t) for t in targets])
    print("ALL FILES CLEAN" if allok else "PROBLEMS FOUND")
    sys.exit(0 if allok else 1)
