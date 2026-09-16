# Generated Docker builds (experimental packaging)

The generator builds the application image and a companion nginx image.
The development Compose file is included; the release Compose draft is ignored until release preparation; actual image builds, container integration
and multi-architecture validation remain pending.
Build commands do not start services; starting either Compose stack is explicit.

## Commands

```sh
make docker-features
make docker-generate
make docker-check
make docker-build
make docker-build DOCKER_EXPERIMENTAL=nix
make docker-clean
```

`docker-generate` renders the tracked root `Dockerfile` from
`docker/repowatch.Dockerfile.in` and creates a temporary root `.dockerignore`.
The root Dockerfile always represents the base image. An experimental selection
also generates `build/docker/<features>/Dockerfile`; it does not replace the
tracked base with an experimental variant. Selection is a comma-separated list;
unknown, duplicate, missing dependency and conflicting names fail before writing.

`docker-check` requires no container engine. It checks the tracked base against
the template, validates registered feature closures and context inputs, and does
not create files. It works with `.dockerignore` absent. It does not execute
Dockerfile instructions or prove an image can be built.

`docker-build` generates inputs and invokes the local engine. Default image tags
are `repowatch:local` and `repowatch:experimental-nix`; set `DOCKER_IMAGE` to change
them. `DOCKER_ENGINE` selects an executable (default `docker`), not a shell command
with flags. Builds do not push images or run containers. Generate/build/clean
commands serialize through a local file lock to avoid mixing experimental inputs.
Use `make docker-build` for builds: after cleanup, a bare `docker build .` has
no root ignore policy and would send the default repository context.

The wrapper removes `.dockerignore` after a build attempt, including an engine
failure or Python exception. Explicit generation leaves it for inspection;
run `make docker-clean` before committing. After a hard kill, clean it explicitly
as well. Cleanup only removes a file bearing the generator's marker. Keep
`/.dockerignore` in your local `.gitignore`; this repository's existing ignore
file is local rather than tracked. The old tracked `.dockerignore` is removed
with this change. No commit hook, staging or commit is performed automatically.

The Make targets live in the main Makefile, so Makefile.dev inherits them.
Neither the default target, normal tests, installation nor deployment invokes
Docker implicitly.

## Build inputs and runtime

The context specification is `docker/context.json`. It includes `pyproject.toml`,
`LICENSE`, the application's Python/HTML files, and the two explicit bootstrap
inputs `docker/compose/init.py` and `docker/compose/config.example.yaml`.
The generated ignore rules start by excluding everything, then allow exact source files and their ancestor
directories. Unrelated YAML, keys, databases, tests, local environments and docs
are excluded. Hidden source files/directories are excluded; source symlinks are
rejected. A Python file in the allowed application source tree is a build input,
so do not store credentials in source code. A Dockerfile-specific ignore file
would override the root policy and is rejected for the selected Dockerfile.

The builder creates wheels using `pyproject.toml`; the runtime installs those
wheels offline. Both stages use `python:3.12-slim-trixie`. This pins the Debian
release family, not an immutable image digest. Package indexes and image tags
can change, so deterministic generation does not imply reproducible image bytes.
The generator never copies Python dependencies into another manifest.

The runtime uses UID/GID 10001, home `/var/lib/repowatch`, and `tini` as PID 1.
It contains `gpgv` for signature checks, `gpg` for key-expiry inspection,
`openssl` and `zstd` for repository backends, plus CA certificates and timezone
data for HTTPS and IANA schedules. These are system packages, not new Python
dependencies. `apk-tools` remains absent; use the existing OpenSSL APK backend.
The default command is `repowatch -c /etc/repowatch/config.yaml run`.

Supply your own config and trusted keys, and persist `/var/lib/repowatch`.
A bind-mounted writable config directory and state directory must be accessible
to UID/GID 10001. A read-only configuration permits reading but prevents dashboard
configuration edits. To expose the status API through a published port, configure
its bind address accordingly; publishing a port does not change the application's
loopback bind. The application image does not contain nginx; the trial stack uses a companion
image. Neither image needs a host Docker socket mount.

## Experimental feature interface

Each registered feature lives under `docker/experimental/<name>/`:

- `feature.json`: description, build/runtime apt packages, explicit required and
  conflicting feature names, and additional persistent volume paths.
- `build.Dockerfile`: setup before building wheels (for example a future native
  backend's build configuration).
- `build-check.Dockerfile`: checks after wheel construction in the builder.
- `runtime.Dockerfile`: setup after creating the application account, before
  switching to that account.
- `check.Dockerfile`: image-build checks as the final unprivileged user.

Fragments are optional, trusted repository code, not directives from runtime
YAML. Dependencies determine fragment order; independent features are ordered
alphabetically. Build packages and build-only environment changes stay in the
builder; runtime libraries must be declared separately. Add future source suffixes
or explicit files to `docker/context.json` when needed.

`c-extension` is reserved but not implemented. Selecting it fails with a clear
message; there is no dummy extension or silently enabled compiler. Its actual
implementation remains the final release-refactoring task.

### Nix

`DOCKER_EXPERIMENTAL=nix` adds Debian's optional `nix-bin`. This marks the image
integration experimental; existing repowatch Nix support has not changed status.
The container uses `NIX_REMOTE=local` and a private `/nix` owned by repowatch,
with an empty `build-users-group`. There is no daemon and no shared host store.
This is a dedicated single-user store for source evaluation and signature checks;
repowatch's existing no-build/IFD restrictions remain in place.

Persist `/nix` separately when using this variant. Do not mount a host's existing
multi-user store or daemon socket into it. A fresh named volume inherits the image
contents; an existing/bind-mounted store must already have suitable ownership.
The image's build checks initialize the store, add a file and evaluate a basic
expression as repowatch. Nix source expressions may consume substantial disk/RAM;
select attributes explicitly as described in [Nix repositories](nix.md).

The local tests exercise native Nix with an isolated temporary local store.
They do not substitute for building and running this image: neither Docker nor
Podman is available in the implementation workspace, and no image build or
multi-architecture success is claimed for this revision.

References: [Docker build context and ignore precedence](https://docs.docker.com/build/concepts/context/),
[official Python images](https://hub.docker.com/_/python/),
[Debian trixie nix-bin](https://packages.debian.org/trixie/nix-bin), and
[Nix local store](https://nix.dev/manual/nix/2.34/store/types/local-store.html).


## Development Compose stack

`docker-compose.dev.yml` is the development stack: it uses locally built images
and bind-mounts bootstrap files from the checkout. For published images without
a checkout, use the separate release file described below.

From the repository root, with Docker Engine and Compose v2 installed:

```sh
# Use your non-root host group for shared config/state access.
export REPOWATCH_GID="$(id -g)"
make docker-compose-build
docker compose -f docker-compose.dev.yml config
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml ps
docker compose -f docker-compose.dev.yml logs -f
```

The dashboard is at <http://127.0.0.1:18085>; the cache is at
<http://127.0.0.1:18080>. Published ports bind only to host loopback.
The seed configuration contains `repos: []`: no upstream is contacted until you
add a source. Guest access is disabled and host-token repository restrictions
are enabled. The developer seed sets `allow_insecure_http: true` because Docker
can forward host-loopback connections with a bridge source address. Published
ports remain bound to host `127.0.0.1`; use HTTPS and disable that exception
before exposing the status port remotely. Set the administrator password interactively:

```sh
docker compose -f docker-compose.dev.yml exec repowatch repowatch -c /etc/repowatch/config.yaml set-password
```

After login, follow [repository management](repositories.md) to add the first
source, then [client configuration](clients.md). This developer profile keeps
its smaller cache/bandwidth limits; it is not a production capacity estimate.
Existing bind-mounted YAML is preserved, so changing the seed does not change
an already initialized stack.

The default stack uses host bind mounts, relative to `docker-compose.dev.yml`:

```text
config/
    config.yaml          # Persistent configuration, created from the trial seed
    keys/                # Public repository verification keys
data/                    # Persistent service data
    state/               # SQLite and application state
    cache/               # nginx cache files
    nix/                 # Dedicated optional Nix store
```

The directories map respectively to `/etc/repowatch`, `/var/lib/repowatch`,
`/var/cache/nginx/repowatch` and `/nix`. nginx mounts configuration read-only;
repowatch can edit it. Only nginx and init mount the cache directory. Generated
nginx policy/configuration stays private to the nginx container.

The one-shot `init` service creates missing directories and copies the seed only
when `config/config.yaml` is absent. Existing configuration, SQLite data, cache
and Nix contents are preserved. Changing the seed does not update an existing
configuration. An existing non-container `config/config.yaml` must be adapted to
the fixed trial paths/ports first; init does not overwrite it. Both `down` and
`down --volumes` leave these host directories intact. Previous named volumes are
not imported automatically: stop the old stack and migrate its data explicitly
before using this layout, retaining the old volumes until verified.

Application UID remains 10001. `REPOWATCH_GID` selects the primary group for the
app, initializer and nginx helper (default 10001, a positive numeric GID).
Export your host group as above for local read/write access to configuration and
state; keep the same value for all subsequent Compose commands, or save
`REPOWATCH_GID=<your numeric group>` in an untracked `.env` file. No matching group
name inside the container is needed. init sets config/state/keys directories to
2770 and the config/SQLite files to 660. Group inheritance keeps dashboard edits
accessible to the host group. nginx cache directory belongs to worker UID 33;
individual cache files and Nix store contents retain their backend-specific
permissions and are not promised to be writable by the host user.

Initialization changes ownership/modes of these managed directories and the
config/SQLite files, including the existing `config/` directory. It does not
recursively chown existing data or keys. Keep the selected group stable; changing
it on an existing deployment requires migrating file permissions while stopped.
For a host editor that replaces the YAML with a new inode/owner, restore config
ownership to `10001:<REPOWATCH_GID>` and mode 660 before further dashboard edits;
editing in place preserves these attributes. Do not give world write access.
Keep key files readable by the application group and reference container paths
such as `/etc/repowatch/keys/debian.gpg` in YAML.

Keep `/config/config.yaml`, `/config/keys/`, `/data/` and `/.env` excluded from Git
in your local ignore rules. The repository's existing `.gitignore` is local and
untracked. The generated build context already excludes these runtime files.

The nginx service owns the network namespace; repowatch joins it with
`network_mode: service:nginx`. Existing loopback-only purge, cache probes and
syslog therefore work within that namespace. nginx publishes both ports and
starts before repowatch, after its first successful configuration application.
If nginx is replaced, recreate both services together:

```sh
docker compose -f docker-compose.dev.yml up -d --force-recreate nginx repowatch
```

Compose dependency restart handling applies to Compose operations; automatic
engine restarts are not a general dependency recovery mechanism. The fixed
internal ports (8080 cache, 8085 status, 8081 private nginx health), state path,
cache path and loopback cache URL are part of this trial topology. Keep them when
editing the configuration. The nginx health endpoint is not published.

The companion image inherits the selected application image so the helper and
application use identical code. It adds Debian nginx, njs and cache-purge modules
as container-only system dependencies. Every five seconds its supervisor validates
a private copy of YAML and calls the existing nginx apply transaction, including
configuration testing, change detection and rollback. This polling interval is a
trial default, not a new host timer or a second renderer. nginx master/helper run
as root inside their container; repowatch runs as UID 10001. No privileged mode,
host networking or Docker socket is used. The helper reads dedup state without migrating or initializing the application's
schema. Shared state directories use the selected GID and setgid; existing
SQLite WAL/SHM access still requires appropriate directory/file permissions. Config is mounted read-only
in nginx; its policy and generated nginx files remain private to that container.

`make docker-compose-build` builds the app first and then the matching companion.
It generates the companion Dockerfile from `docker/nginx/Dockerfile.in` and uses
an exact temporary context allowlist for that directory. Both temporary
`.dockerignore` files are removed after build attempts, including failures;
`make docker-clean` also removes both. Compose uses local images with pull disabled
and deliberately has no `build:` entry: direct builds would bypass context setup.
Set `DOCKER_NGINX_IMAGE` to change the companion tag.

For the Nix variant, keep the app tag consistent between Make and Compose:

```sh
export DOCKER_IMAGE=repowatch:experimental-nix
make docker-compose-build DOCKER_EXPERIMENTAL=nix
docker compose -f docker-compose.dev.yml up -d
```

The same `data/nix/` bind mount is prepared even in the base trial, so enabling this
variant does not require a second Compose file. Nix repositories still need their
normal configuration. The companion inherits the selected feature backends too.

The Compose files have automated initialization, topology, configuration snapshot
and build-wrapper tests. Docker/Podman is unavailable in the implementation workspace;
image builds, `docker compose -f docker-compose.dev.yml config`, container startup and live cache operations
have not been run there. Run the commands above before treating it as validated
for deployment. Multi-architecture testing remains a separate backlog item.

Topology references: [Compose service configuration](https://docs.docker.com/reference/compose-file/services/)
and [Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/).


## Release Compose stack

`docker-compose.release.yml` is currently a local draft, excluded from Git until
release preparation. The following instructions describe that draft; it is not
yet distributed with the repository. It is a standalone file, not an override of the
development file. It pulls two images from Docker Hub with a matching version:

- `docker.io/<namespace>/repowatch:<version>` for the application and init.
- `docker.io/<namespace>/repowatch-nginx:<version>` for nginx and its helper.

`DOCKERHUB_NAMESPACE` and `REPOWATCH_VERSION` are required; there is no implicit
`latest` tag or assumed publishing account. Both images must be published first.
The release file uses `pull_policy: always`; running `up` checks the registry.
The bootstrap script and seed configuration are bundled in the application image,
so no source checkout, Makefile or `docker/` directory is needed on the target.
Existing YAML is still preserved. Bind mounts, group permissions, localhost ports
and the shared network namespace match the development stack above.

Copy the release YAML into a separate deployment directory, then run from there
(replace the example namespace and version with your published values):

```sh
export DOCKERHUB_NAMESPACE=your-dockerhub-namespace
export REPOWATCH_VERSION=0.1.0
export REPOWATCH_GID="$(id -g)"
docker compose -f docker-compose.release.yml config
docker compose -f docker-compose.release.yml up -d
docker compose -f docker-compose.release.yml ps
docker compose -f docker-compose.release.yml exec repowatch \
  repowatch -c /etc/repowatch/config.yaml set-password
```

Store those three values in that directory's `.env` for subsequent commands;
use a positive, non-root host GID. No registry credentials belong in that file.
The same `config/` and `data/` layout is created beside the release YAML. Run the
development and release stacks in separate directories; different project names
do not isolate bind-mounted files or avoid published-port conflicts. Do not run
both against the same data simultaneously. Both still bind the same localhost
ports by default.

To upgrade, keep the group and namespace stable, choose a new published version,
pull it, then stop the stack before initialization/migration of shared SQLite:

```sh
docker compose -f docker-compose.release.yml pull
docker compose -f docker-compose.release.yml down
# Back up config/ and data/state/ while stopped before upgrading.
docker compose -f docker-compose.release.yml up -d
```

This recreates nginx and repowatch together for their shared network namespace.
Stopping the stack retains bind-mounted data. Merely selecting an older image
tag is not a database rollback; retain the matching backup when upgrading.

## Publishing images to Docker Hub

Create the `repowatch` and `repowatch-nginx` repositories in your Docker Hub user
or organization namespace. Make them public if users should pull without access
to a private repository. The account used for login must have push access there.

From the source checkout, build both images with the intended fully qualified
tags. `0.1.0` below is an example, not a claim that this release is published:

```sh
export DOCKERHUB_NAMESPACE=your-dockerhub-namespace
export REPOWATCH_VERSION=0.1.0
export DOCKER_IMAGE="${DOCKERHUB_NAMESPACE}/repowatch:${REPOWATCH_VERSION}"
export DOCKER_NGINX_IMAGE="${DOCKERHUB_NAMESPACE}/repowatch-nginx:${REPOWATCH_VERSION}"
make docker-compose-build
```

The build wrapper tags images directly and builds the companion from that exact
local application image. Test the pair locally using the development stack before
publishing. Then authenticate interactively and upload both tags:

```sh
docker login
docker push "$DOCKER_IMAGE"
docker push "$DOCKER_NGINX_IMAGE"
```

Docker documents this [tag-and-push workflow](https://docs.docker.com/docker-hub/repos/manage/hub-images/push/)
and [interactive login](https://docs.docker.com/reference/cli/docker/login/).
Check both tags in Docker Hub after pushing. Publish a new version tag for each
release and do not overwrite old release tags; the Compose pair assumes that
identical version strings identify the matching application and companion.

For Nix, select `DOCKER_EXPERIMENTAL=nix` when building and use a distinct common
tag such as `0.1.0-nix` for both images. Recompute/export both image variables
with that tag before running Make. Consumers set `REPOWATCH_VERSION=0.1.0-nix`;
no separate release Compose file is needed.

These commands build for the local builder's architecture. They do not create a
multi-architecture manifest; that remains unimplemented/unverified. No automatic
push target, credentials, CI publication or actual Docker Hub upload is included
in this change. Image builds and release startup still need testing on Docker.
