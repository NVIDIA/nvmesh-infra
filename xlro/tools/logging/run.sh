# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

pkill -f /tracer.sh
sudo service fluent-bit stop
sudo rm -f /var/log/nvmesh/fluent-debug/*
sudo service fluent-bit start
sudo service rsyslog restart
nohup ./tracer.sh &>tracer.log &
# Until this is packaged, you can run from your laptop...
# nohup ./eventforwarder.py -m $(hostname) -f $(hostname):5170 &>eventforward.log &
