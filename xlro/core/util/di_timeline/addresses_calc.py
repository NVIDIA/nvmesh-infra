# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import hex
import collections

from typing import Sequence
from xlro.core.entities import Volume, Client, Manager
from xlro.core.util.block_objects import BlockSet
from xlro.core.util.general_utils import dmsg_slice_dict

VLBATree = collections.namedtuple("VLBATree", ("chunk", "raid", "segment", "drive", "blkset"))
BlockGeometricData = collections.namedtuple("BlockGeometricData", ('infra_obj', 'more'))


def vlba_tree_to_str(tree):
    c, r, s, d, b = tree

    dec_hex = lambda dec: "{}({})".format(hex(dec), dec)
    blkset_addr = {k: dec_hex(v) for k, v in b.more.items()}

    ret = """
chunk: {}, clba: {}
raid: {}, rlba: {}
segment: {}, slba: {}, role: {}
drive: {}, dlba: {}, blocksize: {}
blockset: {}, address: {}
""".format(c.infra_obj, dec_hex(c.more.addr), r.infra_obj, dec_hex(r.more.addr), s.infra_obj, dec_hex(s.more[0].addr), s.more[1],
           d.infra_obj, dec_hex(d.more.lbs.addr), d.more.blockSize, b.infra_obj, blkset_addr
           )

    return ret


VLBATree.__str__ = vlba_tree_to_str  # type: ignore


def get_vlba_tree(volume: Volume, vlba: Volume.LBA) -> VLBATree:
    chunk, clba = volume.get_clba(vlba)
    praid, plba = chunk.get_plba(clba)
    segment, slba, role = praid.get_slba(plba)
    drive, dlba_range = segment.get_dlba_range(slba)

    blkset = BlockSet.vlba2block_set(volume, vlba)
    slba_blkset = blkset.block_set_idx
    rlba_blkset = plba.addr - (plba.addr % praid.width*blkset.SLICE_COUNT)

    return VLBATree(BlockGeometricData(chunk, clba), BlockGeometricData(praid, plba),
                    BlockGeometricData(segment, (slba, role)), BlockGeometricData(drive, dlba_range),
                    BlockGeometricData(blkset, {'slba_blkset': slba_blkset, 'rlba_blkset': rlba_blkset}))


def get_ioctl_addr_output(volume: Volume, vlba: Volume.LBA, client: Client, *more_clients: Client) -> VLBATree:

    for c in (client,) + more_clients:
        try:
            return dmsg_slice_dict(volume, vlba.addr, c)
        except:
            continue
    raise Exception("couldn't get ioctl response")
