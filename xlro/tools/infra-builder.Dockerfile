# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

FROM centos:centos7
# glibc == 2.17 on Centos7.7+
RUN sed -i s/mirror.centos.org/vault.centos.org/g /etc/yum.repos.d/*.repo
RUN sed -i s/^#.*baseurl=http/baseurl=http/g /etc/yum.repos.d/*.repo
RUN sed -i s/^mirrorlist=http/#mirrorlist=http/g /etc/yum.repos.d/*.repo

RUN yum install gcc openssl-devel bzip2-devel zlib-dev sqlite-devel sudo libffi-devel which wget curl -y -q
RUN yum group mark-install development
RUN yum group mark-convert development
RUN yum groupinstall development -y -q

# Build openssl for poetry
WORKDIR /usr/local/src
RUN curl -LO https://www.openssl.org/source/openssl-1.1.1w.tar.gz
RUN tar xzvf openssl-1.1.1w.tar.gz
RUN cd openssl-1.1.1w && ./config --prefix=/usr/local/openssl --openssldir=/usr/local/openssl shared zlib && make -j$(nproc) && make install
ENV LD_LIBRARY_PATH=/usr/local/openssl/lib:$LD_LIBRARY_PATH
ENV CPPFLAGS="-I/usr/local/openssl/include"
ENV LDFLAGS="-L/usr/local/openssl/lib"

# Get python by source, since centos+yum doesn't have latest
ARG PYVER=3.8.13
ENV PYVER=${PYVER}
RUN wget https://www.python.org/ftp/python/${PYVER}/Python-${PYVER}.tgz
RUN tar xvf Python-${PYVER}.tgz
RUN cd ./Python-${PYVER}/ && ./configure --enable-optimizations --enable-shared --with-openssl=/usr/local/openssl && sudo make install

# Can't get python to install to /usr/lib64 anyway (until 3.10 which takes --with-platlib)
# So just let it go to /usr/local and then redirect stuff
RUN ln -s /usr/local/bin/p[iy]*3* /usr/bin/
RUN ln -s /usr/local/lib/libpy*3*so* /usr/lib64/

# python3.8 is not supported for poetry 2
#RUN pip3 install "poetry<2.0"
RUN pip3 install poetry
RUN poetry config virtualenvs.create false

# Pre-install dependencies (will be synced at runtime if changed)
WORKDIR /src/infra
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-interaction --no-root
RUN python3 -c "import urllib3; print('urllib3:', urllib3.__version__)"  # TEMP: remove after verifying

# For historical reasons, infrastructure is under /management, adjacent to NVMeshSDK/CLI
VOLUME /management

CMD /src/infrastructure/xlro/infra/jenkins/nvmesh_cd/compilator/pybuilder/buildInfra.sh
