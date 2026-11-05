#!/usr/bin/env python3
"""Rolling-buffer internet radio recorder.

Keeps exactly KEEP mp3 files named 01.mp3 .. NN.mp3 (NN = KEEP) in MEDIA_DIR, each one
CHUNK_SECONDS of a live ICY/Shoutcast stream. 01 is the oldest chunk, NN the newest.
When a chunk finishes the set shifts down one slot: 01 is dropped, 02 -> 01, ...
NN -> NN-1, and the finished chunk becomes NN. The folder therefore always holds the
last KEEP * CHUNK_SECONDS of the show, plus the in-progress recording-<epoch>.part.

Three things this does on purpose, because the easy version is wrong:

  * ONE connection for the whole run, rotation at the write layer. A CDN mount like
    streamtheworld serves ~10 s of buffered audio the instant you connect (measured:
    60 s of wall clock delivered 70 s of audio on a fresh connection). Reopening the
    stream per chunk would repeat those seconds in every file, so the connection stays
    up across rotations and only reconnects when it actually breaks.
  * Chunks are cut on a wall-clock grid, so the buffer stays aligned to real time even
    when the stream drops. A drop leaves a short chunk (a hole), not a shifted buffer.
  * ICY metadata is requested (titles are worth logging), which means the server
    interleaves metadata blocks INSIDE the audio every icy-metaint bytes. IcyStrip
    removes them; leaving them in corrupts roughly one frame in thirty - audible
    crackle that ffprobe still reports as a clean duration. A chunk's tail is trimmed
    at a real frame boundary (a timed stop always lands mid-frame) by walking the
    MPEG frame chain, which doubles as a per-chunk integrity check.

Configuration is environment-only, so one image works for compose and k8s:
  STREAM_URL         stream to record          (default: One World Radio, 192k MP3)
  MEDIA_DIR          buffer folder             (default: /media)
  CHUNK_SECONDS      length of one file        (default: 1800)
  KEEP               files to keep             (default: 10)
  USER_AGENT         HTTP User-Agent (some stations 403 a non-browser one)
  READ_TIMEOUT       stall timeout before reconnect (default: 20 s)
  TLS_VERIFY         0 to allow a broken stream cert (default: 1)
  TAG                0 to skip the ID3v2 tag   (default: 1)
  MIN_CHUNK_AUDIO    discard a chunk with fewer audio seconds than this
                     (default: 5% of CHUNK_SECONDS, i.e. 90 s for a 30 min chunk)
  HEARTBEAT_FILE     touched every 10 s for Docker HEALTHCHECK (default: /tmp/...)
"""
from __future__ import annotations

import glob
import logging
import mmap
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_URL = "https://25503.live.streamtheworld.com/OWR_INTERNATIONAL.mp3"
PART_GLOB = "recording-*.part"

log = logging.getLogger("radio")
STOP = threading.Event()

# --------------------------------------------------------------------------- config


def env(name, default):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def env_int(name, default):
    try:
        return int(float(env(name, default)))
    except ValueError:
        log.warning("ignoring non-numeric %s=%r, using %s", name, env(name, default), default)
        return default


class Config:
    def __init__(self):
        self.url = env("STREAM_URL", DEFAULT_URL)
        self.media_dir = env("MEDIA_DIR", "/media")
        self.chunk_seconds = env_int("CHUNK_SECONDS", 1800)
        self.keep = env_int("KEEP", 10)
        self.ua = env("USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) musicstream-cacher/1.0")
        self.read_timeout = env_int("READ_TIMEOUT", 20)
        self.tls_verify = env("TLS_VERIFY", "1") != "0"
        self.tag = env("TAG", "1") != "0"
        self.min_chunk_audio = env_int("MIN_CHUNK_AUDIO", max(1, int(self.chunk_seconds * 0.05)))
        self.heartbeat = env("HEARTBEAT_FILE", "/tmp/musicstream-cacher.heartbeat")
        if self.chunk_seconds < 10:
            raise SystemExit("CHUNK_SECONDS must be >= 10")
        if self.keep < 1:
            raise SystemExit("KEEP must be >= 1")

    @property
    def final_name(self):
        return "%02d.mp3" % self.keep


# ------------------------------------------------------------------------ icy strip


class IcyStrip:
    """Pull interleaved ICY metadata out of an audio byte stream.

    With `Icy-MetaData: 1` the server inserts a block after every `metaint` AUDIO
    bytes: one length byte k, then 16*k bytes of StreamTitle text. k == 0 means
    "nothing changed" and is the common case. All state lives on self, so a block
    split across two reads cannot desync the file. metaint == 0 => this server sends
    no metadata and the bytes pass straight through.
    """

    def __init__(self, metaint: int):
        self.metaint = metaint or 0
        self.state = "audio" if self.metaint else "raw"
        self.since = 0
        self.need = 0
        self.meta = bytearray()
        self.new_title = None

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        i, n = 0, len(data)
        while i < n:
            if self.state == "raw":
                out += data[i:]
                i = n
            elif self.state == "audio":
                take = min(n - i, self.metaint - self.since)
                out += data[i:i + take]
                i += take
                self.since += take
                if self.since >= self.metaint:
                    self.state = "len"
            elif self.state == "len":
                k = data[i]
                i += 1
                if k == 0:
                    self.since, self.state = 0, "audio"
                else:
                    self.need, self.meta, self.state = 16 * k, bytearray(), "meta"
            elif self.state == "meta":
                take = min(n - i, self.need)
                self.meta += data[i:i + take]
                i += take
                self.need -= take
                if self.need == 0:
                    text = bytes(self.meta).split(b"\x00")[0].decode("utf-8", "replace")
                    m = re.search(r"StreamTitle='([^']*)'", text)
                    title = (m.group(1) if m else text).strip()
                    if title:
                        self.new_title = title
                    self.since, self.state = 0, "audio"
        return bytes(out)


# ------------------------------------------------------------------------- mpeg bits

BR = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
      2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]}
SR = {0b11: {0: 44100, 1: 48000, 2: 32000},     # MPEG-1 ordering - do not invert
      0b10: {0: 24000, 1: 22050, 2: 16000},
      0b00: {0: 12000, 1: 11025, 2: 8000}}
VER = {0b00: "MPEG2.5", 0b10: "MPEG2", 0b11: "MPEG1"}
SAMPLES = 1152                                    # MPEG-1 / MPEG-2 Layer III


def frame_info(hdr: bytes):
    """(frame_length, sample_rate) from a 4-byte MPEG header, or None if not a header."""
    if len(hdr) < 4 or hdr[0] != 0xFF or (hdr[1] & 0xE0) != 0xE0:
        return None
    ver, layer = (hdr[1] >> 3) & 3, (hdr[1] >> 1) & 3
    bi, si, pad = (hdr[2] >> 4) & 0xF, (hdr[2] >> 2) & 3, (hdr[2] >> 1) & 1
    if ver == 0b01 or layer != 1 or not 0 < bi < 15 or si == 3 or si not in SR[ver]:
        return None
    br, sr = BR[1 if ver == 0b11 else 2][bi], SR[ver][si]
    if not br:
        return None
    flen = (144000 * br // sr) + pad if ver == 0b11 else (72000 * br // sr) + pad
    return flen, sr


def walk_chain(path: str, tag_len: int):
    """Walk the MPEG frame chain over the whole file, resyncing after a break.

    Returns frames, seconds, end (last byte of the last complete frame when the chain
    is clean), broken_at (first bad header), resyncs. A clean capture has resyncs == 0
    and end == size; a capture with ICY metadata left inside it shows many resyncs.
    Every header is parsed rather than latching the first frame's values, because a
    broadcast that changes bitrate or sample rate mid-file is normal.
    """
    size = os.path.getsize(path)
    out = {"frames": 0, "seconds": 0.0, "end": tag_len, "broken_at": None,
           "resyncs": 0, "size": size}
    if size <= tag_len + 4:
        return out
    sec_per_frame = {}
    with open(path, "rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as m:
            i, limit = tag_len, size
            while i + 4 <= limit:
                info = frame_info(m[i:i + 4])
                if info:
                    flen, sr = info
                    if flen <= 0 or i + flen > limit:
                        break                        # last frame is cut off by the stop
                    out["frames"] += 1
                    if sr not in sec_per_frame:
                        sec_per_frame[sr] = SAMPLES / float(sr)
                    out["seconds"] += sec_per_frame[sr]
                    i += flen
                    continue
                if out["broken_at"] is None:
                    out["broken_at"] = i
                out["resyncs"] += 1
                nxt = _resync(m, i, limit)
                if nxt is None:
                    break
                i = nxt
            out["end"] = i
    return out


def _resync(m, start: int, limit: int):
    """Next byte offset that looks like a real frame start (header + the one after it).

    Requiring the following frame to be valid too is what keeps a stray 0xFF 0xFB
    inside compressed data from being counted as a resync.
    """
    j = start
    while j + 4 <= limit:
        info = frame_info(m[j:j + 4])
        if info and j + info[0] + 4 <= limit and frame_info(m[j + info[0]:j + info[0] + 4]):
            return j
        j += 1
    return None



# ------------------------------------------------------------------------- id3v2.4


def _synchsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def id3_tag(frames) -> bytes:
    """ID3v2.4 with UTF-8 text frames (v2.4 frame sizes are synchsafe, v2.3's are not)."""
    body = b""
    for fid, desc, text in frames:
        if fid == "TXXX":
            data = b"\x03" + desc.encode("utf-8") + b"\x00" + text.encode("utf-8")
        else:
            data = b"\x03" + text.encode("utf-8")
        body += fid.encode("ascii") + _synchsafe(len(data)) + b"\x00\x00" + data
    return b"ID3" + bytes([4, 0, 0]) + _synchsafe(len(body)) + body


def tag_length(path: str) -> int:
    try:
        with open(path, "rb") as f:
            d = f.read(10)
    except OSError:
        return 0
    if d[:3] != b"ID3":
        return 0
    return ((_synchsafe_rev(d[6:10])) + 10)


def _synchsafe_rev(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def tz_label(epoch: float) -> str:
    return time.strftime("%Z", time.localtime(epoch)) or os.environ.get("TZ", "UTC")


# ------------------------------------------------------------------------- rotation


def slot_names(keep):
    return ["%02d.mp3" % i for i in range(1, keep + 1)]


def rotate_and_commit(media_dir: str, keep: int, part_path: str) -> str:
    """01 out, 02->01 ... keep->keep-1, then the finished chunk takes slot `keep`."""
    names = slot_names(keep)
    for src, dst in zip(names[1:], names[:-1]):
        s, d = os.path.join(media_dir, src), os.path.join(media_dir, dst)
        if os.path.exists(s):
            os.replace(s, d)                       # atomic; replaces the old dst
    final = os.path.join(media_dir, names[-1])
    os.replace(part_path, final)
    return final


def find_part(media_dir: str):
    """Return (path, start_epoch) of the in-progress chunk, newest wins.

    Any older .part left behind (a crash between commit attempts, several containers
    once pointed at the same volume) is deleted here: the newest one is the only chunk
    that can still be resumed, and a stray .part in the folder is just litter.
    """
    found = []
    for p in glob.glob(os.path.join(media_dir, PART_GLOB)):
        m = re.search(r"recording-(\d+)\.part$", p)
        if m:
            found.append((p, int(m.group(1))))
    if not found:
        return None
    found.sort(key=lambda t: t[1])
    best = found[-1]
    for p, start in found[:-1]:
        log.warning("removing abandoned chunk %s (started %s, superseded by a newer one)",
                    os.path.basename(p), stamp(start))
        try:
            os.remove(p)
        except OSError as e:
            log.warning("could not remove %s: %s", p, e)
    return best


def report_inventory(media_dir: str, keep: int):
    expected = set(slot_names(keep))
    present = [n for n in os.listdir(media_dir) if n.lower().endswith(".mp3")]
    filled = set(present) & expected
    log.info("buffer: %d/%d slots filled (%s)", len(filled), keep,
             " ".join(sorted(filled)) or "none")
    strays = sorted(set(present) - expected)
    if strays:
        log.warning("unexpected files in %s, left alone: %s", media_dir, " ".join(strays[:20]))


# --------------------------------------------------------------------------- stream


def open_stream(cfg: Config):
    """GET the mount. Retries once without Icy-MetaData if the server dislikes it."""
    headers = {"User-Agent": cfg.ua, "Accept": "*/*", "Connection": "close",
               "Icy-MetaData": "1"}
    ctx = ssl.create_default_context()
    if not cfg.tls_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    https = urlparse(cfg.url).scheme == "https"

    last_err = None
    for ask_meta in (True, False):
        h = dict(headers)
        if not ask_meta:
            h.pop("Icy-MetaData")
        try:
            resp = urlopen(Request(cfg.url, headers=h), timeout=cfg.read_timeout,
                           context=ctx if https else None)
        except HTTPError as e:
            last_err = e
            if ask_meta and e.code in (400, 401, 403):
                log.warning("HTTP %s with Icy-MetaData, retrying without it", e.code)
                continue
            raise
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype.startswith("text/") or "html" in ctype:
            resp.close()
            raise RuntimeError("server answered %s, not audio - wrong mount or a "
                               "consent/redirect page" % ctype)
        return resp
    raise last_err


# ---------------------------------------------------------------------- chunk writer


class ChunkWriter:
    """Owns the current chunk file and the wall-clock grid; rotates on its own deadline.

    The stream reader calls write() and never thinks about files, so one connection can
    span several chunks and one chunk can span several connections.
    """

    def __init__(self, cfg: Config, station: dict):
        self.cfg = cfg
        self.station = station
        self.fp = None
        self.path = None
        self.start = None
        self.deadline = None
        self.tag_len = 0
        self.bytes = 0
        self.boundaries = []            # byte offsets where a connection began

    # -- lifecycle ---------------------------------------------------------------
    def resume(self, path: str, start: int):
        """Adopt an interrupted chunk (a .part left by a previous run)."""
        self.path = path
        self.start = start
        self.deadline = start + self.cfg.chunk_seconds
        self.tag_len = tag_length(path)
        self.bytes = max(0, os.path.getsize(path) - self.tag_len)
        self.fp = open(path, "ab")
        w = walk_chain(path, self.tag_len)
        log.info("resuming chunk started %s (%.1f min of audio so far, %.1f min left)",
                 stamp(start), w["seconds"] / 60.0, (self.deadline - time.time()) / 60.0)

    def open_chunk(self, start: float):
        self.start = start
        self.deadline = start + self.cfg.chunk_seconds
        self.path = os.path.join(self.cfg.media_dir, "recording-%d.part" % int(start))
        self.bytes = 0
        self.boundaries = []
        tag = id3_tag(self._tag_frames(start)) if self.cfg.tag else b""
        self.tag_len = len(tag)
        # 256 KiB write buffer: ~11 s of 192 kbps audio per write() syscall, and a hard
        # kill (OOM, docker kill) loses at most that much of the current chunk. A larger
        # buffer leaves .part showing 0 bytes for the first minute, which looks broken.
        self.fp = open(self.path, "wb", buffering=1 << 18)
        if tag:
            self.fp.write(tag)
        log.info("chunk -> slot %s: %s .. %s %s", self.cfg.final_name, stamp(start),
                 stamp(self.deadline), tz_label(start))

    def close(self, keep_for_resume: bool = False):
        if self.fp:
            try:
                self.fp.flush()
                os.fsync(self.fp.fileno())
            except OSError as e:
                log.warning("flush failed: %s", e)
            self.fp.close()
            self.fp = None
        if keep_for_resume:
            log.info("keeping %s so a restart can resume it", os.path.basename(self.path))

    # -- the hot path ------------------------------------------------------------
    def write(self, audio: bytes):
        now = time.time()
        if self.fp is None:
            self.open_chunk(int(now))
            self.boundaries.append(0)
        while now >= self.deadline:
            nxt = self.deadline
            self.commit()
            if now > nxt + self.cfg.chunk_seconds:
                skipped = int((now - nxt) // self.cfg.chunk_seconds) - 1
                log.info("grid was %.1f min behind (no audio arriving); skipped %d empty "
                         "chunk(s), next window starts %s",
                         (now - nxt) / 60.0, skipped, stamp(nxt + skipped * self.cfg.chunk_seconds))
                nxt += skipped * self.cfg.chunk_seconds
            self.open_chunk(nxt)
        self.fp.write(audio)
        self.bytes += len(audio)

    def mark_connection(self):
        """Remember where a new connection starts in the byte stream (chain breaks are
        expected exactly there: the CDN can resume on a different frame boundary)."""
        if self.fp is not None:
            self.boundaries.append(self.bytes)

    # -- commit ------------------------------------------------------------------
    def commit(self):
        """Finish the current chunk: frame-walk, trim, rotate into the buffer."""
        cfg = self.cfg
        start, deadline, path = self.start, self.deadline, self.path
        if self.fp:
            self.fp.flush()
            os.fsync(self.fp.fileno())
            self.fp.close()
            self.fp = None
        audio_bytes = max(0, os.path.getsize(path) - self.tag_len)

        w = walk_chain(path, self.tag_len)
        if w["broken_at"] is not None:
            near = [b for b in self.boundaries if abs(b - w["broken_at"]) < 200000]
            log.warning("frame chain breaks at byte %d of %s (%d frames, %.1f min audio, "
                        "%d resync(s))%s", w["broken_at"], os.path.basename(path),
                        w["frames"], w["seconds"] / 60.0, w["resyncs"],
                        " - just after a reconnect, expected" if near else
                        " - ICY metadata may have leaked into the audio")
        if audio_bytes == 0 or w["seconds"] < cfg.min_chunk_audio:
            log.error("chunk %s .. %s captured only %.1f s of audio - discarding it "
                      "instead of shifting the buffer (MIN_CHUNK_AUDIO=%d)",
                      stamp(start), stamp(deadline), w["seconds"], cfg.min_chunk_audio)
            os.remove(path)
            self.path = self.start = self.deadline = None
            self.bytes = 0
            return None

        tail = w["size"] - w["end"]
        trimmed = tail if (0 < tail <= 2000 and w["broken_at"] is None) else 0
        if trimmed:
            os.truncate(path, w["end"])             # the partial frame a timed stop leaves
        elif tail > 2000:
            log.warning("keeping %d B of unparsed tail rather than trusting the chain", tail)
        final = rotate_and_commit(cfg.media_dir, cfg.keep, path)
        log.info("wrote %s: %.1f MB, %.2f min of audio in a %.1f min window, %d frames, "
                 "%d B partial frame trimmed%s", os.path.basename(final),
                 (w["size"] - trimmed) / 1e6, w["seconds"] / 60.0,
                 cfg.chunk_seconds / 60.0, w["frames"], trimmed,
                 "" if w["resyncs"] == 0 else ", %d resync(s)" % w["resyncs"])
        self.path = self.start = self.deadline = None
        self.bytes = 0
        return final

    def _tag_frames(self, start: float):
        cfg = self.cfg
        end = start + cfg.chunk_seconds
        name = self.station.get("name") or urlparse(cfg.url).hostname or "radio"
        tz = tz_label(start)
        f = [("TIT2", "", "%s %s %s-%s %s" % (name, time.strftime("%Y-%m-%d", time.localtime(start)),
                                              time.strftime("%H:%M", time.localtime(start)),
                                              time.strftime("%H:%M", time.localtime(end)), tz)),
             ("TPE1", "", name),
             ("TALB", "", "rolling buffer %s" % " ".join(slot_names(cfg.keep))),
             ("TDOR", "", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start)))]
        if self.station.get("genre"):
            f.append(("TCON", "", self.station["genre"]))
        f += [("TXXX", "stream-url", cfg.url),
              ("TXXX", "recorded-local", "%s .. %s %s" % (stamp(start), stamp(end), tz))]
        return f


# ---------------------------------------------------------------------------- main


def heartbeat_loop(path: str):
    while not STOP.wait(10):
        try:
            with open(path, "w") as f:
                f.write("%.3f\n" % time.time())
        except OSError:
            pass


def free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def stream_loop(cfg: Config, writer: ChunkWriter):
    """Keep one connection alive and hand stripped audio to the writer.

    Rotating chunks is the writer's business; this loop only cares about bytes.
    """
    station = writer.station
    backoff = 1.0
    stats = {"titles": 0, "reconnects": 0, "no_audio": 0.0}
    while not STOP.is_set():
        t_conn = time.time()
        resp = None
        try:
            resp = open_stream(cfg)
            metaint = int(resp.headers.get("icy-metaint") or 0)
            br = re.sub(r"[^0-9]", "", resp.headers.get("icy-br") or "")
            station["name"] = resp.headers.get("icy-name") or station.get("name")
            station["genre"] = resp.headers.get("icy-genre") or station.get("genre")
            strip = IcyStrip(metaint)
            writer.mark_connection()
            log.info("connected  icy-br=%s kbps  metaint=%s  type=%s  name=%s",
                     br or "?", metaint or "none", resp.headers.get("Content-Type"),
                     resp.headers.get("icy-name"))
            backoff = 1.0
            while not STOP.is_set():
                try:
                    data = resp.read1(1 << 16)
                except (socket.timeout, TimeoutError) as e:
                    raise RuntimeError("stalled: %s" % e)
                if not data:
                    raise RuntimeError("stream closed the connection")
                audio = strip.feed(data)
                if audio:
                    writer.write(audio)
                if strip.new_title:
                    stats["titles"] += 1
                    log.info("now playing: %s", strip.new_title)
                    strip.new_title = None
        except (HTTPError, URLError, OSError, RuntimeError, ValueError) as e:
            log.warning("stream problem: %s: %s", type(e).__name__, e)
        except Exception:
            log.exception("unexpected error in stream loop")
        finally:
            try:
                if resp:
                    resp.close()
            except Exception:
                pass
        if STOP.is_set():
            break
        stats["no_audio"] += time.time() - t_conn
        stats["reconnects"] += 1
        backoff = max(backoff, 2.0)
        log.warning("reconnecting in %.0f s (%.0f s without audio so far this run, "
                    "reconnect #%d)", backoff, stats["no_audio"], stats["reconnects"])
        STOP.wait(backoff)
        backoff = min(backoff * 2, 30.0)


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())

    cfg = Config()
    try:
        os.makedirs(cfg.media_dir, exist_ok=True)
        probe = os.path.join(cfg.media_dir, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        raise SystemExit("%s is not writable: %s - mount it with a uid this container "
                         "runs as (this container is uid %d), or set `user: root` in "
                         "compose" % (cfg.media_dir, e, os.getuid()))

    threading.Thread(target=heartbeat_loop, args=(cfg.heartbeat,), daemon=True).start()
    window = cfg.keep * cfg.chunk_seconds / 3600.0
    log.info("rolling buffer: %s", cfg.url)
    log.info("%d files x %d s = %.1f h window in %s (oldest=01, newest=%s); local time "
             "%s %s; %.0f MB free, %.0f MB needed at 192 kbps",
             cfg.keep, cfg.chunk_seconds, window, cfg.media_dir, cfg.final_name,
             stamp(time.time()), tz_label(time.time()), free_bytes(cfg.media_dir) / 1e6,
             cfg.keep * cfg.chunk_seconds * 24000 / 1e6)
    if free_bytes(cfg.media_dir) < cfg.keep * cfg.chunk_seconds * 24000:
        log.warning("volume may be too small for a full %.1f h buffer at high bitrate",
                    window)
    report_inventory(cfg.media_dir, cfg.keep)

    writer = ChunkWriter(cfg, {"name": None, "genre": None})
    found = find_part(cfg.media_dir)
    if found:
        part, start = found
        if start + cfg.chunk_seconds <= time.time():
            log.info("discarding stale %s (its window closed at %s)",
                     os.path.basename(part), stamp(start + cfg.chunk_seconds))
            os.remove(part)
        else:
            writer.resume(part, start)

    try:
        stream_loop(cfg, writer)
    finally:
        if writer.fp:
            writer.close(keep_for_resume=True)
        report_inventory(cfg.media_dir, cfg.keep)
        log.info("stopped")


if __name__ == "__main__":
    main()
