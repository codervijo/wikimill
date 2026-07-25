# wikimill — Python+uv CLI. Forwards to the central builder.
# The user-facing run path is bin/wikimill (see docs/architecture.md); this
# Makefile is the dev/test path (make buildsh / make deps / make test).
BUILDER_PATH ?= ../../builder
STACK ?= python

# Project-specific dev container — NOT the shared `mb1`. Docker pins a
# container's mounts at create-time, so a shared `mb1` created by another
# sibling keeps its /usr/src/app mount pointed there forever. A project-specific
# name forces a fresh container with the right mount.
CONTAINER_NAME ?= wikimill1
export CONTAINER_NAME

# CLI — no published port needed. Empty HOST_PORT/CONTAINER_PORT triggers
# --network=host in dev_container.sh, avoiding port collisions with siblings.
export HOST_PORT :=
export CONTAINER_PORT :=

include $(BUILDER_PATH)/Makefile
