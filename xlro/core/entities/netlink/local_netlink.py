#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import logging
from importlib.machinery import SourceFileLoader
from importlib.util import spec_from_loader, module_from_spec

pypath = './nvmesh_netlink.py'
loader = SourceFileLoader('netlink', pypath)
netlink = module_from_spec(spec_from_loader(loader.name, loader))
loader.exec_module(netlink)

handler = logging.StreamHandler(sys.stderr)
logger = netlink.logger
logger.addHandler(handler)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.setLevel(logging.DEBUG)


netlink.ReadRequest(disk_id='S3HCNX0JC02145.1', start_sector=1509632, data_len=4096000, md_len=8000, is_hw=0, pid=os.getpid()).perform()
