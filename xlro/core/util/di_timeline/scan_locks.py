# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from xlro.core.entities import Volume, Manager, Client
from xlro.core.util.di_timeline.addresses_calc import get_vlba_tree
from xlro.core.util.scanner import get_locks_full


def get_blockset_locks(vlba_tree):
    raid = vlba_tree.raid
    slice_content = raid.infra_obj.get_pslice(raid.more)
    drives_to_ranges = {}

    for page in slice_content:
        blkset_idx_in_raid = page.dlba_range.lbs.addr // 32
        drives_to_ranges[page.drive] = [(blkset_idx_in_raid, blkset_idx_in_raid + 1)]

    return get_locks_full(drives_to_ranges, {"verbose": None})


if __name__ == '__main__':
    import logging
    import sys

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)

    m = Manager.instance(host="n183")
    print(m.clients)
    v = Volume.instance(name="a")
    print(v.blocks)

    get_blockset_locks(get_vlba_tree(v, v.LBA(0)))
