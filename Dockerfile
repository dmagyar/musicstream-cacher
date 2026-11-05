# Rolling-buffer internet radio recorder. Stdlib only - no runtime dependencies.
#
# Deliberately has no RUN steps, so the image builds even on hosts where the Docker
# daemon cannot start a container to run them (a sandboxed dockerd that cannot read
# /sys/kernel/security/apparmor/profiles fails every RUN step).
FROM python:3.12-slim

# OCI provenance labels: Docker Hub shows these on the repository page, and the revision
# pins which commit the layers were built from. Metadata only - still a RUN-free build.
#   docker build --build-arg GIT_REVISION=$(git rev-parse HEAD) -t musicstream-cacher:1 .
ARG GIT_REVISION=unknown
ARG VERSION=1
LABEL org.opencontainers.image.title="musicstream-cacher" \
      org.opencontainers.image.description="Rolling-buffer internet radio recorder: exactly KEEP mp3 files of CHUNK_SECONDS each, so the media folder always holds the last 5 hours of a live ICY/Shoutcast MP3 stream." \
      org.opencontainers.image.source="https://github.com/dmagyar/musicstream-cacher" \
      org.opencontainers.image.url="https://hub.docker.com/r/dmagyar/musicstream-cacher" \
      org.opencontainers.image.revision="${GIT_REVISION}" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY recorder.py /app/recorder.py

# The rolling buffer. Mount a bind mount or volume there; it must be writable by the
# user below. /media is created by the mount, so no mkdir is needed.
VOLUME ["/media"]

# uid 1000 is a normal host uid, so a bind mount stays writable and host-browsable.
# Numeric (no useradd) keeps the build RUN-free; a passwd entry is not required.
# Escape hatches: `user: root` in compose, or PUID-matching user:, if the volume says
# otherwise. recorder.py refuses to start with a clear message rather than looping.
USER 1000:1000

# The recorder touches HEARTBEAT_FILE every 10 s even while a stream is down, so this
# only goes unhealthy when the process itself is wedged. /tmp is world-writable in the
# base image, so it works for any uid.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import os,sys,time; p=os.environ.get('HEARTBEAT_FILE','/tmp/musicstream-cacher.heartbeat'); sys.exit(0 if time.time()-os.path.getmtime(p)<240 else 1)"

CMD ["python", "/app/recorder.py"]
