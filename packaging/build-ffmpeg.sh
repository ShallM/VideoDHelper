#!/bin/zsh
set -euo pipefail
# Build only upstream LGPL components; no Homebrew libraries are linked.
SOURCE_ROOT="${1:A}"
OUTPUT_ROOT="${2:A}"
cd "$SOURCE_ROOT"
# A source archive may live inside the app's Git checkout. Pin the upstream
# release version so FFmpeg does not accidentally report the parent repo's hash.
cp RELEASE VERSION
./configure --prefix=/ --arch=arm64 --target-os=darwin --cc=/usr/bin/clang \
    --enable-static --disable-shared --disable-autodetect --disable-gpl \
    --disable-nonfree --disable-version3 --disable-doc --disable-debug \
    --disable-ffplay --disable-avdevice --disable-network \
    --disable-encoders --enable-encoder=pcm_s16le \
    --extra-cflags=-mmacosx-version-min=14.0 \
    --extra-ldflags=-mmacosx-version-min=14.0
make -j "${VIDEOHELPER_BUILD_JOBS:-8}"
make install DESTDIR="$OUTPUT_ROOT"
