# Optional system dependency for Nix catalogs/signatures, requested explicitly.
# Only this dedicated non-root account uses this store; no build daemon runs.
RUN mkdir -p /etc/nix /nix \
    && printf 'build-users-group =\n' > /etc/nix/nix.conf \
    && chown repowatch:repowatch /nix
ENV NIX_REMOTE=local
