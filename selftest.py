#!/usr/bin/env python3
"""Self-test for the recorder's correctness-critical pieces, no network and no waiting.

Run: python3 selftest.py
Each test prints PASS/FAIL; exit code is non-zero if any failed.

Covers the parts that are easy to get wrong and invisible when you do:
  1. the ICY metadata strip, fed in read-sized pieces that split metadata blocks;
  2. the rotation order (01 dropped, 02->01 ... new -> NN, exactly NN files);
  3. the frame-chain walk and tail trim on a stream whose frames alternate length
     (192 kbps at 44.1 kHz is 626.5 B, so padding alternates - a CBR assumption cuts
     the file in the wrong place);
  4. that the walk actually NOTICES interleaved junk (the classic un-stripped-metadata
     corruption), i.e. the auditor is not a no-op;
  5. the ID3v2.4 tag: synchsafe sizes, tag_length round-trip, ffprobe reads it back.
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recorder import (IcyStrip, id3_tag, rotate_and_commit, slot_names, tag_length,
                      walk_chain, frame_info)

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  -> " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


# --- synthetic mpeg -------------------------------------------------------------
def frame(pad=False, payload=b"\x00"):
    """A legal MPEG-1 Layer III 192 kbps 44.1 kHz frame header + its body.

    Bitrate index 11 (0xB) is 192 kbps; 0xC would be 224 kbps. 144000*192/44100 = 626.5,
    so a real encoder alternates 626 and 627 B frames - which is exactly why a trim that
    assumes one fixed frame length cuts the file in the wrong place.
    """
    hdr = bytes([0xFF, 0xFB, 0xB0 | (0x02 if pad else 0x00), 0x00])
    flen, sr = frame_info(hdr)
    return hdr + (payload * (flen // len(payload) + 1))[:flen - 4], flen


def synth_file(path, n_frames, tag=None, junk_at=()):
    """Write n_frames frames (padding alternates, like a real 44.1 kHz stream),
    optionally dropping 33 bytes of junk inside frame k to fake an un-stripped block."""
    body = bytearray()
    for k in range(n_frames):
        payload = bytes(random.getrandbits(8) for _ in range(700))
        f, _ = frame(pad=(k % 2), payload=payload)
        if k in junk_at:
            f = f[:40] + b"\x00" + b"StreamTitle='x';" * 2 + f[40:60]   # 33+ junk bytes
        body += f
    with open(path, "wb") as f:
        f.write(tag or b"")
        f.write(bytes(body))
    return len(body)


def test_icy_strip():
    random.seed(1)
    audio = bytes(random.getrandbits(8) for _ in range(120000))
    metaint = 16000
    wire = bytearray()
    titles = ["Artist %d - Track %d" % (i, i) for i in range(1, 8)]
    seen_titles = []
    ti = 0
    blocks = 0
    for off in range(0, len(audio), metaint):
        chunk = audio[off:off + metaint]
        wire += chunk
        if len(chunk) < metaint:
            break                       # a real server only blocks after a full metaint
        if ti < len(titles) and off % (metaint * 2) == 0:
            text = ("StreamTitle='%s';" % titles[ti]).encode() + b"\x00"
            block = text + b"\x00" * (16 - len(text) % 16)
            wire.append(len(block) // 16)          # k
            wire += block
            seen_titles.append(titles[ti])
            ti += 1
        else:
            wire.append(0)                          # k == 0: "nothing changed"
        blocks += 1
    strip = IcyStrip(metaint)
    got = bytearray()
    seen = []
    i = 0
    while i < len(wire):
        n = random.randint(1, 7000)                 # split blocks across "reads"
        got += strip.feed(bytes(wire[i:i + n]))
        if strip.new_title:
            seen.append(strip.new_title)
            strip.new_title = None
        i += n
    check("ICY strip: audio is byte-identical after stripping %d metadata blocks (%d of them titled)"
          % (blocks, ti), bytes(got) == audio,
          "%d B in, %d B out, expected %d" % (len(wire), len(got), len(audio)))
    check("ICY strip: all %d stream titles recovered, k==0 blocks ignored" % ti,
          seen == seen_titles, "%s vs %s" % (seen[:2], seen_titles[:2]))
    # a title block split across reads, the case that corrupts naive implementations
    block = b"StreamTitle='A - B';\x00"
    block += b"\x00" * (16 - len(block) % 16)
    wire2 = b"ABCD" + bytes([len(block) // 16]) + block + b"EFGH"
    s2 = IcyStrip(4)
    out = s2.feed(wire2[:4]) + s2.feed(wire2[4:17]) + s2.feed(wire2[17:])
    check("ICY strip: survives a metadata block split across three reads",
          out == b"ABCD" + b"EFGH" and s2.new_title == "A - B",
          repr(out) + " " + repr(s2.new_title))
    raw = IcyStrip(0)
    check("ICY strip: metaint=0 (server sends no metadata) passes bytes through",
          raw.feed(b"abcdefgh") == b"abcdefgh")


def test_rotation():
    d = tempfile.mkdtemp(prefix="rb-rot-")
    keep = 10
    try:
        for i in range(1, keep + 1):
            with open(os.path.join(d, "%02d.mp3" % i), "w") as f:
                f.write("old-%02d" % i)
        with open(os.path.join(d, "recording.part"), "w") as f:
            f.write("NEW")
        final = rotate_and_commit(d, keep, os.path.join(d, "recording.part"))
        names = sorted(n for n in os.listdir(d) if n.endswith(".mp3"))
        check("rotation: exactly %d mp3 files remain" % keep, len(names) == keep, str(names))
        check("rotation: new chunk lands in %s (highest = newest)" % os.path.basename(final),
              os.path.basename(final) == "%02d.mp3" % keep, os.path.basename(final))
        want = {("%02d.mp3" % i): "old-%02d" % (i + 1) for i in range(1, keep)}
        want["%02d.mp3" % keep] = "NEW"
        bad = {n: open(os.path.join(d, n)).read() for n in names
               if open(os.path.join(d, n)).read() != want[n]}
        check("rotation: 01 dropped, every other file shifted down one slot, nothing overwritten",
              not bad, str(bad))
        check("rotation: no .part left behind", not [n for n in names if n.endswith(".part")])
    finally:
        shutil.rmtree(d)


def test_chain_and_trim():
    d = tempfile.mkdtemp(prefix="rb-chain-")
    try:
        # (a) the real case: the stop cuts a real frame, so the last header is VALID but
        #     its frame runs past EOF. A walk that stepped past it would report a
        #     negative tail and the trim would silently skip.
        p = os.path.join(d, "valid-cut.mp3.part")
        synth_file(p, 400)
        cut = 104
        full, flen = frame(pad=False)
        hdr = full[:4]
        check("synthetic frames are the alternating 626/627 B the real stream uses",
              flen == 626, "flen=%d" % flen)
        with open(p, "ab") as f:
            f.write(hdr + b"\x11" * (cut - 4))       # a real frame header, body cut short
        w = walk_chain(p, 0)
        check("chain: a valid final frame that runs past EOF stops the walk at its header",
              w["frames"] == 400 and w["broken_at"] is None and w["resyncs"] == 0, str(w))
        check("chain: tail = the partial frame (%d B), so commit trims it" % cut,
              w["size"] - w["end"] == cut, "tail=%d expected %d" % (w["size"] - w["end"], cut))
        os.truncate(p, w["end"])
        w = walk_chain(p, 0)
        check("chain: after trimming, the file is exactly consumed by the chain",
              w["end"] == w["size"] and w["broken_at"] is None and w["frames"] == 400, str(w))

        # (b) junk bytes instead of a frame header: the walk must report the break
        p2 = os.path.join(d, "junk-tail.mp3.part")
        synth_file(p2, 400)
        with open(p2, "ab") as f:
            f.write(b"\xff\xfb\x00\x01" + b"\x11" * 196)   # bitrate index 0 = no frame
        w2 = walk_chain(p2, 0)
        check("chain: garbage at the tail is reported as a break at its offset",
              w2["broken_at"] == w2["size"] - 200 and w2["end"] == w2["size"] - 200, str(w2))
        check("chain: duration = 400 x 1152/44100 = 10.45 s (44.1 kHz, not 48)",
              abs(w2["seconds"] - 400 * 1152 / 44100.0) < 0.02, "%.3f" % w2["seconds"])
    finally:
        shutil.rmtree(d)


def test_chain_notices_junk():
    d = tempfile.mkdtemp(prefix="rb-junk-")
    try:
        p = os.path.join(d, "b.mp3")
        synth_file(p, 400, junk_at=(50, 120, 300))
        w = walk_chain(p, 0)
        check("auditor: un-stripped ICY junk inside the audio IS detected (breaks + resyncs)",
              w["broken_at"] is not None and w["resyncs"] >= 3,
              "broken_at=%s resyncs=%d frames=%d" % (w["broken_at"], w["resyncs"], w["frames"]))
        check("auditor: reports the first corrupt offset", w["broken_at"] is not None)
    finally:
        shutil.rmtree(d)


def test_id3():
    frames = [("TIT2", "", "OWR_INTERNATIONAL 2026-11-05 15:00-15:30 CET"),
              ("TPE1", "", "OWR_INTERNATIONAL"),
              ("TXXX", "stream-url", "https://example.com/x.mp3")]
    tag = id3_tag(frames)
    d = tempfile.mkdtemp(prefix="rb-id3-")
    try:
        check("id3: synchsafe size round-trips through tag_length", tag_length_of(tag) == len(tag))
        p = os.path.join(d, "c.mp3")
        synth_file(p, 60, tag=tag)
        check("id3: tag_length lets the chain start at byte 0 of audio",
              walk_chain(p, tag_length(p))["broken_at"] is None)
        check("id3: tag_length(p) == len(tag)", tag_length(p) == len(tag),
              "%d vs %d" % (tag_length(p), len(tag)))
        if shutil.which("ffprobe"):
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                  "format=duration:format_tags",
                                  "-of", "default=nw=1", p], capture_output=True, text=True).stdout
            flat = " ".join(out.split())
            check("id3: ffprobe reads the tag back (title, artist, TXXX comment)",
                  "title=OWR_INTERNATIONAL 2026-11-05 15:00-15:30 CET" in flat
                  and "artist=OWR_INTERNATIONAL" in flat and "stream-url" in flat, flat[:400])
            check("id3: ffprobe still walks the audio (duration ~= 1.57 s)",
                  "duration=1.5" in flat, flat[:400])
        else:
            print("SKIP ffprobe not installed (id3 read-back + duration)")
    finally:
        shutil.rmtree(d)


def tag_length_of(tag):
    b = tag[6:10]
    return ((b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]) + 10


if __name__ == "__main__":
    random.seed(7)
    print("slots for KEEP=10: %s" % " ".join(slot_names(10)))
    test_icy_strip()
    test_rotation()
    test_chain_and_trim()
    test_chain_notices_junk()
    test_id3()
    print("\n%d failed" % len(FAILS))
    sys.exit(1 if FAILS else 0)
