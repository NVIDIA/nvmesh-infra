# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Parse block is an Nvmesh Util used to parse debug_di info written on a block.
This file is wrap the C tool in some needed Python functionality
"""

import os

from typing import Tuple, Any

from xlro.core.entities import Host
from xlro.core.util.ssh import temp_dir, execute_cmd_locally


def parse_remotely(host: Host, block_path: str, parser_path: str, get_ctype: bool = False) -> Tuple[bytes, Any]:
    with temp_dir(host.name, "parse_block") as dir_rpath:
        block_file_name = os.path.basename(block_path)
        rpath = os.path.join(dir_rpath, block_file_name)

        host.connection.put_file(block_path, rpath)

        parse_output_f = os.path.join(dir_rpath, "parsed")
        out, err, code = host.execute("{} {} {} && cat {}".format(parser_path, rpath, parse_output_f,
                                                                  parse_output_f), decode=False)
        ctype = None
        if get_ctype and not code:
            blk_data = host.get_ctype('t_data_blk')
            ctype = blk_data.from_buffer(bytearray(out))

        return (out, ctype) or (err, None)
