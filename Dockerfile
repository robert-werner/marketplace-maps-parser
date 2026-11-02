# marketplace-maps-parser runtime image.
#
# invisible-playwright stopped publishing macOS binaries after firefox-21,
# so live runs must happen on Linux.
#
# Base image otzovik-marketplace-parser:latest (noble, arm64, this machine):
#   * Python 3.14 (custom build)
#   * browser runtime deps already installed (xauth, xvfb, libgtk-3-0t64,
#     libdbus-glib-1-2, libasound2t64, libnss3)
#   * invisible-playwright BIDI engine pre-cached at
#     /root/.cache/invisible-playwright (~793 MB), proven working
#
# arm64 = native speed on Apple Silicon Docker; no engine re-download needed.
FROM otzovik-marketplace-parser:latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# uv (via pip into the base image's python 3.14, lands in /usr/local/bin)
RUN python3 -m pip install uv \
    && uv --version

COPY . .
RUN uv sync --frozen

# docker run marketplace-maps-parser [args]  =>  xvfb-run -a uv run marketplace-maps-parser [args]
# (BIDI-Firefox engine needs an X display; Xvfb ships in the base image)
CMD ["xvfb-run", "-a", "uv", "run", "marketplace-maps-parser", "--help"]
