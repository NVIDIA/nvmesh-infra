# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

SETUP:
    - install fluent-bit
    - update /etc/fluent-bit/fluent-bit.conf:
        - remove default INPUT and OUTPUTs (cpu and stdout)
        - add: @INCLUDE /opt/nvmesh/monitor/nvmesh-fluent-bit.conf
    - start/enable fluent-bit service

    - Inputs
        - Add /etc/rsyslog.d/60-fluent-bit.conf:
            *.info action(type="omfwd" Target="127.0.0.1" Port="5140" Protocol="tcp" Template="RSYSLOG_SyslogProtocol23Format")
        - Run eventmonitor.py
        - Run tracemonitor.py
