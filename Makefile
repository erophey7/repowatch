.DEFAULT_GOAL := help
PYTHON ?= python3
DEV_VENV ?= .venv
WHEELHOUSE ?= build/wheels
BUILD_WHEELS ?= 1
MANIFEST ?= $(PREFIX)/share/repowatch/install.json
PREFIX ?= /usr/local
SYSCONFDIR ?= /etc
LOCALSTATEDIR ?= /var
DESTDIR ?=
CACHE_DIR ?= $(LOCALSTATEDIR)/cache/nginx/repowatch
SYSTEMD_UNIT_DIR ?= /etc/systemd/system
NGINX_CONF ?=
NGINX_ENABLED_DIR ?=
WITH_NGINX ?= 1
WITH_SYSTEMD ?= 1
export PREFIX SYSCONFDIR LOCALSTATEDIR DESTDIR CACHE_DIR SYSTEMD_UNIT_DIR
export NGINX_CONF NGINX_ENABLED_DIR WITH_NGINX WITH_SYSTEMD WHEELHOUSE BUILD_WHEELS

.PHONY: help dev test test-ui check wheel install activate
help:
	@echo 'make dev       Prepare local Python development environment'
	@echo 'make test      Run full Python test suite (after make dev)'
	@echo 'make test-ui   Run existing JS tests (requires node + jsdom)'
	@echo 'make check     Read-only installation diagnostics; OK/WARN/FAIL'
	@echo 'make wheel     Build application and dependency wheels'
	@echo 'make install   Check, build and install files; no service activation'
	@echo 'make upgrade-plan / upgrade  Plan / apply offline system upgrade with rollback'
	@echo 'make activate  Explicit local systemd/nginx activation (root, no DESTDIR)'
	@echo 'Paths: PREFIX SYSCONFDIR LOCALSTATEDIR DESTDIR CACHE_DIR'
	@echo 'Integration: WITH_NGINX WITH_SYSTEMD NGINX_CONF NGINX_ENABLED_DIR SYSTEMD_UNIT_DIR'
	@echo 'Infrastructure commands: make -f Makefile.dev help'

dev:
	$(PYTHON) -m venv "$(DEV_VENV)"
	"$(DEV_VENV)/bin/python" -m pip install -e . pytest

test:
	"$(DEV_VENV)/bin/python" -m pytest tests/

test-ui:
	node --test tests/dashboard.test.cjs

check:
	$(PYTHON) -B scripts/install.py check

wheel:
	$(PYTHON) -B scripts/install.py wheel

# Check before network/build work, then recheck immediately before writes.
install: check
	@if [ "$(BUILD_WHEELS)" = "1" ]; then $(MAKE) wheel; elif [ "$(BUILD_WHEELS)" != "0" ]; then echo "BUILD_WHEELS must be 0 or 1"; exit 1; fi
	$(PYTHON) -B scripts/install.py install

activate:
	$(PYTHON) -B scripts/install.py activate

# Wheelhouse must already contain target-compatible dependencies. No downloads.
.PHONY: upgrade upgrade-plan
upgrade-plan:
	$(PYTHON) -B scripts/upgrade-system.py --manifest "$(MANIFEST)" --wheelhouse "$(WHEELHOUSE)"
upgrade:
	$(PYTHON) -B scripts/upgrade-system.py --manifest "$(MANIFEST)" --wheelhouse "$(WHEELHOUSE)" --apply
