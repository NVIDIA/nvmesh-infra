#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from typing import Dict, Optional
import os
import logging
import json
import argparse
import uuid
import datetime
import dateparser
import tempfile

from os import path, mkdir
from collections import defaultdict
from xlro.core.entities import ClientVolumeTopology, Client, Volume, Manager, Drive
from xlro.core.util.block_objects import BlockSet, BlockRange
from xlro.core.util.general_utils import dmsg_slice_dict
from xlro.core.util.journal_util import dump_jpages
from xlro.core.util.compare_block_util import compare_slice
from xlro.core.util.lba import HwLBA
from xlro.core.util.scanner import get_vol_locks_summary
from xlro.core.util.scanner import convert_drives2ranges_to_filter_str, convert_volume_to_drive2ranges, split_drive2ranges_by_target
from xlro.core.util.io_monitors import IOMonitor
from xlro.core.util.cli_util import add_common_args, handle_common_args


logger = logging.getLogger("FailedIO")
SCAN_LOCKS_PATH = "/opt/nvmesh/common-repo/tools/scan_locks_ec"


def init_argparse():
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--volume', type=lambda vname: Volume.instance(name=vname), required=True, help='volume name')
    parser.add_argument('-c', '--client', type=lambda cname: Client.instance(name=cname), required=True, help='client name')
    parser.add_argument('-l', '--local_dir', type=str, help='dump data to directory path', default='./failed-io-logs')
    parser.add_argument('-a', '--vlba', type=lambda vaddr: Volume.LBA(int(vaddr)), help="volume address")
    parser.add_argument('-s', '--topo_start', type=dateparser.parse, help="start time to collect topology and conversation info.")
    parser.add_argument('-e', '--topo_end', type=dateparser.parse, help="end time to collect topology and conversation info.")
    return parser

def dump_di_event_info(panics, logpath=None, topology_start_time=None, topology_end_time=None):

    logpath = logpath or os.getcwd()
    for pevent in panics:
        if IOMonitor.DI_TYPE in pevent:
            logger.debug("data corruption handling - {}".format(pevent))
            dump_lba_info(dc_volume=Volume.instance(name=pevent['nvmesh_volume_name']),
                          client=Client.instance(name=pevent['client']),
                          dc_vlba=Volume.LBA(int(pevent['vlba'])), dc_event=pevent, logpath=logpath)
            # handle only first di event
            break

    # fetch & dump test's topologies and toma-client conversations
    failed_volume_to_clients = defaultdict(lambda: set())
    for panic in [event for event in panics if IOMonitor.DI_TYPE in event]:
        failed_volume_to_clients[panic['volume']].add(panic['host'])

    collect_pager_info(failed_volume_to_clients, topology_start_time, topology_end_time, logpath)


def collect_pager_info(failed_volume_to_clients, start_time, end_time, topos_log_path):
    topos_log_path = path.join(topos_log_path, 'topologies')
    if not os.path.exists(topos_log_path):
        mkdir(topos_log_path)
    end_time = end_time or datetime.datetime.now()
    start_time = start_time or (end_time - datetime.timedelta(minutes=30))
    for vol, client_list in failed_volume_to_clients.items():
        for client in client_list:
            dump_toma_info(client, vol, start_time, end_time, topos_log_path)
    logger.info("Toma topology and Conversation info Can be found in {}".format(topos_log_path))


def dump_toma_info(client, vol, start_time, end_time, topos_log_path):
    try:
        dump_topology(client, vol,
                      start_time, end_time,
                      topos_log_path)
    except:
        logger.warning("dumping topology for {}:{} failed:".format(vol, client), exc_info=True)
    try:
        dump_toma_client_conversation(client,
                                      vol,
                                      start_time,
                                      end_time, topos_log_path)
    except:
        logger.warning("dumping conversation for {}:{} failed:".format(vol, client), exc_info=True)


def dump_lba_info(dc_volume: Volume, client: Client, dc_vlba: Volume.LBA, dc_event: Optional[dict] = None, logpath: Optional[str] = None) -> str:

    dc_dir_name = "DC-INFO_{vname}_{vlba}_{token}".format(vname=dc_volume.name, vlba=dc_vlba.addr, token=str(uuid.uuid4())[:8])
    logpath = logpath or os.getcwd()
    dc_logpath = path.join(logpath, dc_dir_name)
    mkdir(dc_logpath)
    dc_page = dc_volume.get_page(dc_vlba)
    dc_slice = dc_volume.get_pslice(dc_vlba)
    logger.info("dumping data corruption record to {}".format(dc_logpath))

    with open(path.join(dc_logpath, 'info.txt'), 'w+') as f:
        f.write("volume - {}, page - {}\n".format(dc_volume, dc_page))
        if dc_event:
            json.dump(dc_event, f, indent=2, default=str)

    if dc_event and IOMonitor.DUMP_BLOCK_TYPE in dc_event:
        btest_block_data = path.join(dc_logpath, 'btest_block_data')
        if not os.path.exists(btest_block_data):
            mkdir(btest_block_data)
        client.connection.get_file(dc_event[IOMonitor.DUMP_BLOCK_TYPE],
                                   path.join(btest_block_data, os.path.basename(dc_event[IOMonitor.DUMP_BLOCK_TYPE])))

    dump_items = [
        (f'compare blocks on volume {dc_volume.name} vlba {dc_vlba.addr}', compare_blocks, (dc_logpath, client, dc_volume, dc_vlba)),
        (f'scan locks on volume {dc_volume.name}', dump_scan_locks, (dc_logpath, dc_volume)),
        (f'dump blockset on volume {dc_volume.name} vlba {dc_vlba.addr}', dump_block_set, (dc_logpath, dc_volume, dc_vlba)),
        (f'translate vlba on volume {dc_volume.name} vlba {dc_vlba.addr}', dump_dlba_info, (dc_logpath, dc_volume, client, dc_vlba)),
        (f'dump MTV data on volume {dc_volume.name} vlba {dc_vlba.addr}', dump_mtv_info, (dc_logpath, client, dc_volume, dc_event, dc_vlba))
        if dc_volume.sub_volumes else
        (f'dump journal pages from blockset on volume {dc_volume.name} vlba {dc_vlba.addr}', dump_jpages, (path.join(dc_logpath, 'journal_pages'), dc_slice, client))
    ]

    for item in dump_items:
        ItemDump(*item).dump_item()

    logger.info("DC INFO Can be found in {}".format(dc_logpath))
    return dc_logpath


def dump_mtv_info(dc_logpath, client, dc_volume, dc_event, dc_vlba):
    mtv_info_path = os.path.join(dc_logpath, 'mtv_info')
    if not path.exists(mtv_info_path):
        mkdir(mtv_info_path)

    errors = []
    # Collect QLC blockset
    try:
        BlockSet.vlba2block_set(dc_volume.sub_volumes['QLC'], dc_vlba).copy(os.path.join(mtv_info_path, 'qlc'))
    except Exception as e:
        errors.append(repr(e))

    # Collect WCV block for all attached clients
    for client_name, client_info in dc_event['mtv_info'].items():
        try:
            BlockRange(Drive.instance(name=client_info['wcv_data_drive']), HwLBA(client_info['wcv_data_dlba'], 4096))\
                .copy(os.path.join(mtv_info_path, 'wcv', client_name, 'data'))
            BlockRange(Drive.instance(name=client_info['wcv_mirror_drive']), HwLBA(client_info['wcv_mirror_dlba'], 4096))\
                .copy(os.path.join(mtv_info_path, 'wcv', client_name, 'mirror'))
        except Exception as e:
            errors.append(repr(e))

    # Collect MDV block
    mdv_info = dc_event['mtv_info'][client.name]
    BlockRange(Drive.instance(name=mdv_info['mdv_data_drive']), HwLBA(mdv_info['mdv_data_dlba'], 4096)) \
        .copy(os.path.join(mtv_info_path, 'mdv', 'data'))
    BlockRange(Drive.instance(name=mdv_info['mdv_mirror_drive']), HwLBA(mdv_info['mdv_mirror_dlba'], 4096)) \
        .copy(os.path.join(mtv_info_path, 'mdv', 'mirror'))

    if errors:
        raise Exception(errors)


def dump_slice(dc_logpath, dc_slice):
    slice_content_path = path.join(dc_logpath, 'slice_content')
    if not path.exists(slice_content_path):
        mkdir(slice_content_path)
    return dc_slice.copy(local_dir=slice_content_path)


def compare_blocks(dc_logpath, client, volume, vlba):
    cb_path = path.join(dc_logpath, 'compare_blocks')
    if not path.exists(cb_path):
        mkdir(cb_path)
    return compare_slice(client, volume, vlba, dc_path=cb_path)


def dump_block_set(dc_logpath, volume, vlba):
    bs_content_path = os.path.join(dc_logpath, 'blockset_content')
    if not os.path.exists(bs_content_path):
        os.mkdir(bs_content_path)
    bs = volume.get_block_set(vlba)
    return bs.copy(bs_content_path)


def dump_scan_locks(dc_logpath, volume):

    scan_locks_dir = path.join(dc_logpath, 'scan_locks')
    if not path.exists(scan_locks_dir):
        mkdir(scan_locks_dir)

    # dump summary
    try:
        dump_scan_locks_summary(scan_locks_dir, volume)
    except Exception as e:
        logger.info('Dump scan locks summary failed - {}{}'.format(type(e), e))

    # dump full_data
    try:
        dump_scan_locks_full(scan_locks_dir, volume)
    except Exception as e:
        logger.info('Dump scan locks full dta failed - {}{}'.format(type(e), e))


def dump_scan_locks_summary(sc_log_path, volume):
    locks_summary = get_vol_locks_summary(volume)
    summary_locks_dict: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for drive, dlock in locks_summary.items():
        dblock_dict = dlock.__dict__
        dblock_dict.pop('drive', None)
        summary_locks_dict[drive.target.name][drive.name] = dblock_dict

    with open(os.path.join(sc_log_path, "scan_lock_summary.json"), "w") as f:
        json.dump(summary_locks_dict, f, indent=2, default=str)
    logger.info("Dumping scan locks summary Success")


def dump_scan_locks_full(sc_log_path, volume):
    drive2ranges = convert_volume_to_drive2ranges(volume)
    target2drive2ranges = split_drive2ranges_by_target(drive2ranges)
    from xlro.core.util.monitor import CmdMonitor, MultiMonitor
    monitors = []
    for target, drives_dict in target2drive2ranges.items():
        filter_str = convert_drives2ranges_to_filter_str(drives_dict)
        monitors.append(CmdMonitor(cmd="sudo {0} {1}".format(SCAN_LOCKS_PATH, filter_str),
                                   host=target.name,
                                   logpath=os.path.join(sc_log_path,
                                                        "{}_{}".format(target.name, "_".join(
                                                            [dr.name for dr in list(drives_dict.keys())])))))
    all_sl_monitors = MultiMonitor(monitors=monitors)
    all_sl_monitors.start()
    if any(all_sl_monitors.wait_all()):
        logger.info('not all monitors ran successfully')
    all_sl_monitors.stop()


def dump_dlba_info(dc_logpath, volume, client, vlba):
    slice_addresses = dmsg_slice_dict(volume, vlba.addr, client)
    with open(os.path.join(dc_logpath, "vlba_dlba_translation.json"), 'w') as f:
        json.dump(slice_addresses, f, indent=2, default=str)


def dump_toma_client_conversation(client, vol, local_start_time, local_end_time, topos_log_path):
    conversations = Client.get_toma_client_conversations(Client.instance(name=client),
                                                         Volume.instance(name=vol),
                                                         local_start_time,
                                                         local_end_time)
    with open(path.join(topos_log_path, '{0}-{1}-conversation'.format(client, vol)), "w+") as f:
        json.dump([c.__dict__ for c in conversations], f, indent=2, default=str)


def dump_topology(client, vol, local_start_time, local_end_time, topos_log_path):
    topos = ClientVolumeTopology.get_volume_topologies(Client.instance(name=client),
                                                       Volume.instance(name=vol),
                                                       local_start_time,
                                                       local_end_time)
    with open(path.join(topos_log_path, '{0}-{1}-topology'.format(client, vol)), "w+") as f:
        json.dump([topo.to_dict() for topo in topos], f, indent=2, default=str)


class ItemDump(object):
    def __init__(self, msg, func, args):
        self.msg = msg
        self.func = func
        self.args = args

    def dump_item(self):
        try:
            logger.info(f'Running {self.msg}')
            self.func(*self.args)
        except Exception as e:
            logger.warning(f"Failed to {self.msg} - {repr(e)}")


if __name__ == '__main__':
    parser = init_argparse()
    add_common_args(parser)
    args = parser.parse_args()
    handle_common_args(args)
    assert args.volume in args.manager.volumes, \
            'Volume {} not found!'.format(args.volume.name)
    assert args.volume.name in args.client.attachments, \
            'Volume {} not attached to Client {}.'.format(args.volume.name, args.client.name)

    if not os.path.isdir(args.local_dir):
        os.makedirs(args.local_dir)
    if args.vlba:
        dump_lba_info(dc_volume=args.volume, client=args.client, dc_vlba=args.vlba, logpath=args.local_dir)
    collect_pager_info(failed_volume_to_clients={args.volume.name: [args.client.name]},
                start_time=args.topo_start, end_time=args.topo_end, topos_log_path=args.local_dir)
    logger.info('All Failed_io results can be found in {}.'.format(args.local_dir))


