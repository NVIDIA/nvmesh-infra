#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from __future__ import division
from builtins import str
from xlro.core.util.general_utils import old_div
import os
import sys
import json
import traceback

from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple, Callable, Set, Optional

from xlro.core.entities import Volume, Client, Segment, SourceTypes, Manager
from xlro.core.util.block_objects import BlockSet
from xlro.core.util.cli_util import CLIArgumentParser, EntityArg, EntitiesArg
from xlro.core.util.scanner import ScanLocksMonitor, SCAN_LOCKS_PATH

from threading import Lock
print_lock = Lock()
# TODO: standardize on cli_notice() and better default-logging...
def tsprint(msg):
    ''' Threadsafe print '''
    with print_lock:
        print(msg)


def _get_unknown_lock_range(seg: Segment, data_disks_len: int, parity_disks_len: int) -> Optional[Tuple[int,int,int]]:
    ''' Scan locks and return min-bs, max-bs and count '''
    drive_to_ranges = {seg.drive: [(seg.lbs // 32, (seg.lbe + 1) // 32)]}

    locks_cmd = ScanLocksMonitor.build_scan_locks_cmd(drive_to_ranges,
                                          {"verbose": None, "conf_d": str(data_disks_len),
                                           "conf_p": str(parity_disks_len),
                                           "conf_role": str(seg.pRaidIndex)},
                                          SCAN_LOCKS_PATH)

    unknown_range_finder = ''' | awk -F' ' '
BEGIN                       { count=0; }
$7 == "txid=0x00000,"       {if (!min) { min=$2; }; max=$2; ++count; }
END                         { if (count) { print gensub(/:/, "", "g", min), gensub(/:/, "", "g", max), count; }}' -
        '''
    out, err, code = seg.drive.target.host.execute(locks_cmd + unknown_range_finder)
    if not out:
        return None

    min_dbs, max_dbs, count = (int(s, 0) for s in out.split())

    # Need to convert the Disk BS numbers to segment/drive relative bs
    sw2hw_ratio = old_div(Volume.BLOCK_SIZE, seg.drive.blockSize)
    seg_bs0 = old_div(seg.lbs, sw2hw_ratio / BlockSet.SLICE_COUNT)
    min_bs = min_dbs - seg_bs0
    max_bs = max_dbs - seg_bs0 + 1
    return (min_bs, max_bs, count)


def _find_attached_client(volume, manager):
    for c in manager.clients:
        if volume.name in c.attachments:
            return c
    raise Exception("Could not find a client that is attached to {}".format(volume.name))


def attach_if_needed(client: Client, volume: Volume) -> None:
    c_attachments = client.get_property('attachments', no_cache=True)
    c_visible_attachments = [attach_name for attach_name, attach_obj in c_attachments.items()
                             if not attach_obj.get_property('is_hidden', source=SourceTypes.MANAGEMENT)]
    if volume not in c_visible_attachments:
        client.attach([volume], wait_till_completed=True)


def process_volume(volume, client=None, manager=None, do_cli=True, quiet=False):
    txids_found = False
    cli_client = None
    try:
        if do_cli and not (client or manager):
            raise Exception('Client or Manager required to handle unknown-txids.')
        for chunk_idx, chunk in enumerate(volume.chunks):
            for praid in chunk.pRaids:
                with ThreadPoolExecutor(max_workers=32) as tpe:
                    results = tpe.map(lambda seg: _get_unknown_lock_range(seg, praid.dataDisks, praid.parityDisks),
                            praid.get_dataSegments())
                bs_min = bs_max = None
                bs_ranges = [res for res in results if res]
                if bs_ranges:
                    txids_found = True
                    cli_cmd = "#{volume}|recov_launch sgmnt=({chunk},{praid},0)" \
                              " type=1 is_mandatory=1 do_only_owners=0 blocksets=[{blkset_start}, {blkset_end})" \
                              " jgc_cookie=0".format(volume=volume.name, chunk=chunk_idx, praid=praid.stripeIndex,
                                                     blkset_start=min([r[0] for r in bs_ranges]),
                                                     blkset_end=max([r[1] for r in bs_ranges]))
                    if not quiet:
                        tsprint('CLI: {}'.format(cli_cmd))
                    if do_cli:
                        if not cli_client:
                            cli_client = client or _find_attached_client(volume, manager)
                            attach_if_needed(cli_client, volume)
                        cli_client.cli(cli_cmd)
    except Exception as e:
        traceback.print_exc()
        if not quiet:
            tsprint('Failure processing volume: {} - {}'.format(volume.name, repr(e)))
        raise

    # Let caller know if we are actually found anything
    return txids_found


def process_volumes(manager: Manager, client: Optional[Client], volumes: List[Volume], do_cli: bool = True, quiet: bool = False) -> None:
    with ThreadPoolExecutor(max_workers=len(volumes)) as tpe:
        tpe.map(lambda v: process_volume(v, client, manager, do_cli, quiet), volumes)


def main():
    parser = CLIArgumentParser()
    parser.add_argument('-n', '--dryrun', action='store_true', help="don't execute the cli")
    parser.add_argument('-c', '--client', type=EntityArg(Client),
                        help="client to run the txid fix from")
    parser.add_argument('-v', '--volume', type=EntitiesArg(Volume),
                        help="comma separated list of volumes")


    args = parser.parse_args()
    manager = args.manager

    ec_vols = [v for v in args.volume or manager.volumes if v.RAIDlevel == 'Erasure Coding']
    print("Scanning the following EC volumes: {}".format([v.name for v in ec_vols]))

    process_volumes(manager, args.client, ec_vols, do_cli=not args.dryrun)

if __name__ == '__main__':
    main()
