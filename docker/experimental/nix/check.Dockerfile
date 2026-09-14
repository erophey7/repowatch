# Test store writes as the final user, not merely executable presence as root.
RUN nix-store --init \
    && nix-store --add /etc/nix/nix.conf >/dev/null \
    && nix-instantiate --eval --strict --expr '1 + 1' | grep -qx 2
