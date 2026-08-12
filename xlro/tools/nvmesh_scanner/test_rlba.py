# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import slash
from xlro.core.entities import Volume
from xlro.core.util.block_objects import PSlice
from xlro.core.util.di_timeline.addresses_calc import get_vlba_tree

volume = Volume.instance(name='kobi-r10')

def dlba2rlba(drive, dlba):
    # same logic as in disk scanner
    pslice, role = PSlice.locate_dlba(drive, dlba.lbs.addr)
    praid = pslice.praid
    if role >= praid.dataDisks:
        is_parity = praid.parityDisks > 0
        rlba = (pslice.slice_idx * pslice.praid.dataDisks)
    else:
        is_parity = False
        rlba = (pslice.slice_idx * pslice.praid.dataDisks) + (role * volume.snake)
    return rlba

@slash.tag('self-test')
def test_rlba(manager):
    for vlba in [0, 32, 64, 96, 78, 87, 128]: #, 4882432, 4882464, 4882496, 4882528, 4882560, 5000000, 12345, 515151]:
        vlba_tree = get_vlba_tree(volume, Volume.LBA(addr=vlba))
        drive = vlba_tree.drive.infra_obj
        dlba_range = vlba_tree.drive.more
        rlba = dlba2rlba(drive, dlba_range)
        assert rlba == vlba_tree.raid.more.addr, f'failed in vlba {vlba}'
