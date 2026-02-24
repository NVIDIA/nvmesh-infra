#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.core.util.general_utils import get_debug_di
from xlro.core.util.compare_blocks import *
import argparse
import logging
from os import path, mkdir
import uuid

from xlro.core.entities import Client, Volume, Attachment
from xlro.core.entities import manager

vol_compare_logger = logging.getLogger("COMPARE_BLOCKS")


def init_argparse():
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--mgmt', type=lambda c: manager.Manager.instance(host=c), required=True)
    parser.add_argument('--vname', type=lambda vname: Volume.instance(name=vname), required=True, help='volume name')
    parser.add_argument('--client', type=lambda cname: Client.instance(name=cname), required=True, help='client name')
    parser.add_argument('--verbose', type=bool, default=False, help='verbose gives details on errors')
    parser.add_argument('-l', '--local_dir', type=str, help='dump data to directory path')
    parser.add_argument('--cmp_path', type=str, help='path to compare_block script on client')
    parser.add_argument('--debug_di', default=None, action='store_true', help='parse data when debug_di flag is on')
    parser.add_argument('--blockset_count', type=int, default=1, help='number of blockset to check')
    parser.add_argument('--vlba', type=lambda vaddr: Volume.LBA(int(vaddr)), help="volume address")
    return parser


def compare_slice(client, volume, vlba, verbose=False, dc_path=None, debug_di=None, cmp_path=None, slice_folder=None):
    if debug_di is None:
        try:
            debug_di = volume.get_property('use_debug_di', no_cache=True)
            if debug_di is None:
                debug_di = False
        except:
            debug_di = False

    dc_slice = volume.get_pslice(vlba)
    plba = dc_slice.praid.LBA(dc_slice.slice_idx * dc_slice.praid.dataDisks)
    dc_slice_ld = os.getcwd() if not dc_path else dc_path
    return block_set_compare(dc_slice, client, plba=plba, base_local_dir=dc_slice_ld, debug_di=debug_di,
                             cmp_path=cmp_path, verbose=verbose, data_folder=slice_folder)


def compare_volume(vname, client, local_dir, verbose, blockset_count, debug_di=None, cmp_path=None):
    debug_di = debug_di if debug_di is not None else get_debug_di(client, vname)
    local_dir = local_dir or os.getcwd()
    vc = VolumeCompare(volume=vname, client=client, local_dir=local_dir, debug_di=debug_di, verbose=verbose, cmp_path=cmp_path)
    return vc.cmp(block_set_count=blockset_count)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    parser = init_argparse()
    args = parser.parse_args()
    vol_compare_logger.info(f"start compare block on volume {args.vname.name} through client {args.client.name}")
    if args.vlba:
        result = compare_slice(client=args.client, volume=args.vname, vlba=args.vlba, debug_di=args.debug_di,
                               verbose=args.verbose, dc_path=args.local_dir, cmp_path=args.cmp_path)
    else:
        result = compare_volume(vname=args.vname, client=args.client, local_dir=args.local_dir, debug_di=args.debug_di,
                                verbose=args.verbose, blockset_count=args.blockset_count, cmp_path=args.cmp_path)

    msg = f"Volume {args.vname.name} attach to client {args.client.name}"
    if result:
        vol_compare_logger.info(msg + " checked and finish successfully")
    else:
        vol_compare_logger.error(msg + " finish with errors please check results...")
