# repowatch itself — a single Python process, no systemd inside the
# container (see docs_dev/ROADMAP.md item 10). nginx is a separate
# container/service in any real deployment (repowatch only ever talks to it
# over plain HTTP, via cache_base_url — the same relationship as on a bare
# host, see CLAUDE.md "Ключевые решения" #3); this image does not bundle or
# manage nginx at all.
FROM python:3.12-slim

# System binaries repowatch shells out to for specific, optional features —
# the same "external tool" pattern already used on a bare host (see
# gpgverify.py, apkverify.py, parsers/dnf.py, parsers/xbps.py):
#   gpgv    -> signature verification for apt/pacman/dnf/apt-rpm. A REAL bug
#              caught by hand-testing the actual built image (not assumed):
#              `gnupg` does NOT pull in `gpgv` on this base image — it's a
#              separate package — so installing `gnupg` here silently left
#              `gpgv` missing entirely. `gpgv` alone is also the smaller,
#              verification-only tool the README already documents as the
#              real requirement (not the full gnupg suite).
#   openssl -> apk signature verification (the default apk_signature_backend)
#   zstd    -> RPM-MD Zstandard-compressed metadata, and xbps repodata
#              (always Zstandard-compressed, not just optionally)
#   tini    -> real PID 1 semantics (signal forwarding, zombie reaping) —
#              `repowatch run` itself is a plain asyncio process, not an
#              init system; running it directly as PID 1 would leave
#              SIGTERM/SIGCHLD handling to Python's own (insufficient)
#              defaults, see the ENTRYPOINT below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini gpgv openssl zstd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Non-root, matching the same privilege-separation this project already
# insists on for a bare-host install (see CLAUDE.md) — nothing in the
# container needs root; nginx/system-level concerns don't apply here at all.
RUN useradd --system --create-home --home-dir /var/lib/repowatch repowatch \
    && mkdir -p /etc/repowatch \
    && chown repowatch:repowatch /var/lib/repowatch /etc/repowatch

USER repowatch

# state_db and any nginx-policy backups (see backup.py) belong here — mount
# a named volume or bind mount for real persistence across container
# recreation. config.yaml is expected under /etc/repowatch (bind-mount a
# host file/directory, or build your own image layer that COPYs one in);
# none is baked into this image.
VOLUME ["/var/lib/repowatch"]
EXPOSE 8085

ENTRYPOINT ["tini", "--", "repowatch"]
CMD ["-c", "/etc/repowatch/config.yaml", "run"]
