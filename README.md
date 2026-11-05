# musicstream-cacher

A rolling buffer for an internet radio stream, in one Docker container.

It records a live ICY/Shoutcast MP3 mount into a media folder that holds **exactly
`KEEP` files named `01.mp3 … 10.mp3`**, each `CHUNK_SECONDS` long (default 30 min), so
the folder always contains the last **5 hours** of the show.

## What lands in the media folder

```
media/
├── 01.mp3                     oldest   (5 h ago)
├── …
├── 10.mp3                     newest   (just finished)
└── recording-<epoch>.part     the chunk being written now (does not match *.mp3)
```

`01.mp3` is the oldest chunk, `10.mp3` the newest. When a new chunk finishes the set
shifts down one slot: `01` is deleted, `02→01`, `03→02`, …, `10→09`, and the finished
chunk becomes `10`. Rotation is a sequence of `os.replace` calls, so it is atomic per
file and a crash mid-rotation cannot lose the buffer.

The in-progress chunk is named `recording-<start-epoch>.part` — visible, but it does not
match `*.mp3`, so the folder always counts exactly `KEEP` mp3s. Nothing is pre-created as
an empty placeholder: during the first `KEEP × CHUNK_SECONDS` the folder legitimately
holds fewer files.

Default stream: [One World Radio](https://oneworldradio.tomorrowland.com/) —
`https://25503.live.streamtheworld.com/OWR_INTERNATIONAL.mp3` (192 kbps MPEG-1 Layer III).

## Quick start

```bash
docker compose up -d                  # sandboxed dockerd: see "Hosts where containers
                                      # will not start" at the bottom
docker logs -f musicstream-cacher
ls -l media/
```

First file after `CHUNK_SECONDS`, a full window after `KEEP × CHUNK_SECONDS` (5 h).
Disk: 192 kbps ≈ 86 MB/h → 5 h ≈ **430 MB**, plus one in-progress chunk (~43 MB).

Another station, or a different folder:

```bash
STREAM_URL="https://stream.example.com/live.mp3" docker compose up -d
```

or edit `docker-compose.yml` (`MEDIA_DIR` inside the container is `/media`; the bind
mount source is where the files land on the host).

## Configuration — environment only

| Variable | Default | Meaning |
|---|---|---|
| `STREAM_URL` | OWR_INTERNATIONAL 192k | stream to record (ICY/Shoutcast mount) |
| `MEDIA_DIR` | `/media` | buffer folder; mount a volume here |
| `CHUNK_SECONDS` | `1800` | seconds per file |
| `KEEP` | `10` | files to keep → window = `KEEP × CHUNK_SECONDS` |
| `TZ` | `Europe/Amsterdam` | log timestamps and the ID3 time ranges |
| `USER_AGENT` | Mozilla-ish | some stations 403 a Python UA |
| `READ_TIMEOUT` | `20` | stall seconds before it reconnects |
| `TLS_VERIFY` | `1` | `0` for a stream host with a broken certificate chain |
| `TAG` | `1` | `0` to skip the ID3v2.4 tag on each file |
| `MIN_CHUNK_AUDIO` | 5 % of `CHUNK_SECONDS` | a chunk with less audio than this is discarded instead of shifting the buffer |
| `HEARTBEAT_FILE` | `/tmp/musicstream-cacher.heartbeat` | touched every 10 s for `HEALTHCHECK` |

## Why it is built this way

Each point below was measured against the default mount, and each one is a bug in the
obvious implementation.

1. **ICY metadata is stripped, not saved.** The client sends `Icy-MetaData: 1` (titles
   are worth logging), so the server interleaves a metadata block into the byte stream
   every `icy-metaint` audio bytes. Writing those bytes into the file corrupts about one
   frame in thirty — audible crackle that `ffprobe` still reports as a clean duration.
   `IcyStrip` is a three-state machine whose state survives a block split across reads.
2. **One connection for the whole run; rotation happens at the write layer.** A fresh
   connection to this CDN delivered **70.2 s of audio in 60 s of wall clock** — it
   flushes ~10 s of backlog at connect, then converges to realtime. Reopening the stream
   per output file repeats those ~10 s in every file. With one persistent connection the
   burst happens once and consecutive chunks stay contiguous (measured: 20.4 / 20.2 /
   20.0 s of audio for three 20.0 s windows).
3. **Chunks are cut on a wall-clock grid, not on captured bytes.** If the buffer is
   supposed to mean "the last 5 hours", a stream drop must leave a short file (a hole)
   rather than drift the whole buffer behind live time. After a long outage the grid
   fast-forwards and says so in the log.
4. **The tail is trimmed at a real frame boundary found by walking the frame chain.** A
   timed stop always lands inside a frame, and 192 kbps at 44.1 kHz is
   `144000×192/44100 = 626.5` B per frame, so frames *alternate* 626/627 — trimming by
   `size % frame_len` cuts the file in the wrong place (it did, in revision 1, leaving a
   file whose chain ran 608 B past EOF). The walk doubles as a per-chunk integrity check:
   a break is logged with its byte offset and attributed to the nearest reconnect when
   that is the cause (a CDN reconnect can resume mid-frame).
5. **A chunk with no usable audio is discarded, not rotated in** — otherwise an outage
   would push real recordings out of the buffer one empty file at a time.
6. **Restart-safe.** On `SIGTERM` the in-progress chunk stays on disk as
   `recording-<start-epoch>.part`; a restart inside its window resumes appending to it,
   after the window it is discarded, and any older abandoned `.part` is deleted.

Every file is prefixed with an ID3v2.4 tag (station, genre, local start–end range, stream
URL), because `07.mp3` says nothing on its own.

## Proof

```bash
python3 selftest.py      # 21 offline checks, no network: strip byte-identity with
                         # metadata blocks split across reads, rotation order and
                         # content, frame-chain trim, injected junk IS detected, ID3
                         # synchsafe round-trip
python3 verify.py media  # per file: frame chain over the WHOLE file + a full ffmpeg
                         # decode + ffprobe duration and tags
```

`chain N/N` (the chain consumes the file exactly) plus `decode=clean` is what proves a
capture is not silently corrupt; a duration alone proves nothing. After 7 h unattended on
the default mount:

```
01.mp3  43.2 MB  68886 frames  29.99 min  chain 43187304/43187304  decode=clean  ffprobe 1799.47 s  192001 bps
…
10.mp3  43.2 MB  68916 frames  30.00 min  chain 43206113/43206113  decode=clean  ffprobe 1800.25 s  192001 bps
ALL FILES CLEAN
```

All ten chunks: 29.99–30.01 min, ~68.9k frames, MPEG-1 Layer III 192 kbps 44.1 kHz, zero
decode errors.

## Repository layout

| File | Role |
|---|---|
| `recorder.py` | the container's whole program — Python standard library only, no ffmpeg needed |
| `Dockerfile` | `python:3.12-slim` + that file; deliberately RUN-free so it builds on a crippled dockerd |
| `docker-compose.yml` | service, config, `./media:/media`, `restart: unless-stopped` |
| `docker-compose.host-quirks.yml` | overlay for hosts whose dockerd cannot start confined containers |
| `selftest.py` | offline correctness tests |
| `verify.py` | audits recorded files: chain walk, decode, tags |

## Troubleshooting

- **"server answered text/html", or nothing is ever written** — wrong mount, or the
  station wants a browser `USER_AGENT`.
- **HTTP 403 only with this client** — set `USER_AGENT` to a browser string. Some
  stations also refuse datacenter IPs; that is the source, not the recorder.
- **`/media is not writable`** — the container runs as uid 1000. `chown 1000:1000` the
  host folder, or set `user: root` in compose.
- **Fewer than `KEEP` files** — still warming up, or a chunk was discarded after an
  outage (the log says so explicitly).
- **A chain break mid-file with no reconnect nearby** — the server changed framing, or
  metadata leaked; `verify.py` shows the offset.
- **Playing the buffer** is meant to be sequential `01 → 10`. Each file is raw CBR MP3
  with no Xing/Info header (a live stream cannot know its length in advance), so some
  players show no duration or cannot seek inside a chunk. `cat $(seq -f '%02g.mp3' 1 10)
  > all.mp3` gives one 5 h file that plays everywhere.

## Hosts where containers will not start

Some sandboxed hosts (containers-in-containers, certain VMs) deny reading
`/sys/kernel/security/apparmor/profiles`. Their dockerd then fails *every* confined
container start — and every `docker exec`, which is what a `HEALTHCHECK` runs — with:

```
Could not check if docker-default AppArmor profile was loaded: permission denied
```

Nothing about this app is involved: `docker run --rm python:3.12-slim id` fails the same
way. Two ways out:

```bash
# per project — unconfines this container and disables its health check
docker compose -f docker-compose.yml -f docker-compose.host-quirks.yml up -d
```

```jsonc
// or fix the daemon once — /etc/docker/daemon.json, then restart dockerd
{ "default-apparmor-profile": "unconfined" }
```

Both drop the (thin) default AppArmor layer, so prefer the daemon setting only on a host
that already cannot enforce anything. The image keeps its `HEALTHCHECK` for normal hosts.
