# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

ARG BASE=ubuntu:20.04
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive

RUN if command -v apt-get >/dev/null 2>&1; then \
        apt-get update && apt-get install -y --no-install-recommends \
            rpm rpm2cpio cpio alien dpkg-dev fakeroot \
            build-essential rsync git sudo curl \
            python3-dev \
            file ca-certificates \
        && rm -rf /var/lib/apt/lists/* ; \
    elif command -v yum >/dev/null 2>&1; then \
        yum install -y rpm rpm-build rpmdevtools rsync git sudo \
            gcc gcc-c++ make file python3-devel \
            && yum clean all ; \
    elif command -v dnf >/dev/null 2>&1; then \
        dnf install -y rpm rpm-build rpmdevtools rsync git sudo \
            gcc gcc-c++ make file python3-devel \
            && dnf clean all ; \
    else \
        echo "Unsupported base image: $BASE"; exit 1 ; \
    fi

# Ubuntu only: install a known-good cpio for alien compatibility.
# - Ubuntu 20.04's patched cpio (2.13+dfsg-2ubuntu0.x) returns exit-code 2 on RPMs
#   with early symlink entries, which alien treats as fatal.
# - Ubuntu 22.04 ships cpio 2.14 which breaks alien's RPM decompression entirely.
# - cpio 2.15 requires glibc >= 2.38 (Ubuntu 24.04+), cannot be used on 20.04/22.04.
# The original Debian cpio (2.13+dfsg-2, no Ubuntu patches) handles both correctly.
RUN if command -v dpkg >/dev/null 2>&1; then \
        ARCH=$(dpkg --print-architecture) && \
        if [ "$ARCH" = "amd64" ]; then \
            CPIO_URL="http://archive.ubuntu.com/ubuntu/pool/main/c/cpio/cpio_2.13+dfsg-2_${ARCH}.deb"; \
        elif [ "$ARCH" = "arm64" ]; then \
            CPIO_URL="http://ports.ubuntu.com/pool/main/c/cpio/cpio_2.13+dfsg-2_${ARCH}.deb"; \
        else \
            echo "ERROR: unsupported architecture $ARCH for cpio package" >&2; exit 1; \
        fi && \
        curl -fsSL "$CPIO_URL" -o /tmp/cpio.deb && \
        dpkg -i --force-downgrade /tmp/cpio.deb && \
        rm -f /tmp/cpio.deb ; \
    fi

WORKDIR /src/nvmesh-utils
CMD ["/src/nvmesh-utils/RPM/buildrpm.sh"]
