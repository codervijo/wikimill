# wikimill runtime image.
#
# Dependencies are baked into the image's (system) Python; wikimill source is
# bind-mounted at /usr/src/app at runtime, so code edits need no rebuild.
# Rebuild only matters when pyproject.toml changes.
#
# Also serves the central-builder dev path (make buildsh / make test), which
# runs `uv sync` into a .venv alongside these system deps — the two don't
# conflict.
FROM python:3.12-slim

# `make` is required: the central-builder dev path (`make buildsh` → `make deps`
# / `make test`) runs make *inside* this container, so omitting it breaks the
# documented workflow with a bare "make: command not found".
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash git curl ca-certificates make \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

# Install runtime deps (project source is bind-mounted at runtime, not copied).
WORKDIR /opt/wikimill-build
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml \
    && uv pip install --system pytest   # baked so `python -m pytest` works in-container

# Resolve `wikimill` from the bind-mounted src/. PYTHONSAFEPATH stops `-m`
# from prepending CWD to sys.path.
ENV PYTHONPATH=/usr/src/app/src
ENV PYTHONSAFEPATH=1
ENV WIKIMILL_IN_DOCKER=1
WORKDIR /usr/src/app

CMD ["bash"]
