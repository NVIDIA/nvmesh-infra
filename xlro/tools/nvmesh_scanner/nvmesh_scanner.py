#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
import os
from collections import defaultdict
from os import path
from datetime import datetime
import argparse

from xlro.core import infra_conf
from xlro.core.entities import Manager, Volume, Drive, Target
from xlro.core.entities.volume import VOLUME_MDATA_SIZE
from xlro.core.sdk.ConnectionManager import Connection
from xlro.core.sdk.Utils import Utils
from xlro.core.util.block_objects import PSlice
from xlro.core.util.cli_util import CLIArgumentParser, EntitiesArg
from xlro.core.util.lba import SwLBA
from xlro.core.util.ssh import Connection

from concurrent.futures import ThreadPoolExecutor

HOSTNAME = ""
WORKDIR = ""
BAD_STATUSES = ['BAD_SECTOR', 'WRONG_CRC', 'LOGICAL_ZERO']
logger = logging.getLogger('nvmesh_scanner')
files_created = set()

def run_cmd(cmd, **kwargs):
    return Connection.execute_on_host(HOSTNAME, cmd, **kwargs)

def check_block(volume, segment, page_data, pslice, role, dlba, dbg_di, statuses):
    ''' Handle chacking of a single block, via DHS C code '''
    praid = pslice.praid
    is_parity = role >= praid.dataDisks
    rlba = (pslice.slice_idx * praid.dataDisks) + (0 if is_parity else (role * volume.snake))
    try:
        status = page_data.get_page_status(rlba, dbg_di, is_parity)
    except Exception as e:
        print(f'Failed to check block: {segment}@RLBA:{rlba} - {repr(e)}. Check logs for more information.')
        logger.debug(f'Failed to check block: {segment}@RLBA:{rlba} - {repr(e)}, {len(page_data.data)}, {"None" if not page_data.metadata else len(page_data.metadata)}')
        raise
    if status in statuses:
        vlba = pslice.vlbs
        if role < praid.dataDisks:
            vlba += (role * volume.snake)
        msg = f'Found {status} block on Volume:{volume.name}@VLBA:{vlba.addr} pRaid:{segment.pRaidIndex}@RLBA:{rlba}, Drive:{segment.drive.name}@DLBA:{dlba}'
        logger.debug(msg)
        return msg
        # Return from process must be picklable.  So either send back string, or, as with caller, convert to primitives
        # return (volume, segment, vlba, rlba, dlba, status)

def producer(volume, praid, segment, ranges, dbg_di, blocks_per_page, found, statuses):
    block_handling = 0
    logger.debug(f'Producer {volume}:{segment}: Start')
    start = datetime.now()
    seg_index = praid.diskSegments.index(segment)
    for d_addr, n_blocks in ranges:
        # Range is in DLBA terms - convert to page range
        slice_offset = (d_addr - segment.lbs) // blocks_per_page
        page_addr = SwLBA(d_addr/blocks_per_page)
        try:
            pages = segment.drive.read_pages(page_addr, count=n_blocks // blocks_per_page)
        except Exception as e:
            logger.debug(f'Failed to read {n_blocks} blocks from {d_addr} of segment {segment} - {repr(e)}')
            continue

        for idx, page_data in enumerate(pages):
            # This still seems wrong for Striping. Maybe relative to chunk, not praid?
            # Slice-index used in SliceRange.vlbs() calculates slice_in_stripe from slice_index
            # But rotational_steps() doesn't seem to.  Confusion remains...
            slice_index = slice_offset + idx
            # correct? slice_index += (slice_index - (slice_index % BlockSet.SLICE_COUNT)) * (chunk.StripeWidth - 1)
            pslice = PSlice(praid, slice_index)
            role = (seg_index + praid.width - pslice.rotational_steps) % praid.width
            start_block = datetime.now()
            try:
                result = check_block(volume, segment, page_data, pslice, role, d_addr+idx, dbg_di, statuses)
            except:
                continue
            block_handling += (datetime.now() - start_block).total_seconds()
            if result:
                found.append(result)

    elapsed = (datetime.now() - start).total_seconds() - block_handling
    logger.debug(f'Validating: {block_handling}s. Reading: {elapsed}s')
    logger.debug(f'Task {volume.name} {segment.drive.name} {ranges} Done')

def get_local_segments(volumes, drives):
    return [(v, c, p, s) for v in volumes for c in v.chunks for p in c.pRaids for s in p.get_dataSegments() if s.drive in drives]

# NOTE: Because of ProcessPoolExecutor - args and return must be primitives or picklable
def worker(args):
    volume_name, chunk_id, praid_id, segment_id, lbs, lbe, timeout, time_started, output_blocks, stop_on_problems = args
    volume = Volume.instance(name=volume_name)
    chunk = volume.chunks[chunk_id]
    praid = chunk.pRaids[praid_id]
    segment = praid.diskSegments[segment_id]

    global WORKDIR, HOSTNAME
    file_name = f'{WORKDIR}/nvmesh_scanner_{volume_name}_{str(time_started).replace(":", "_").replace(" ", "-")}.res'
    if timeout is None:
        # default timeout is calculated by 200Mbps
        timeout = (lbe - lbs) * segment.drive.blockSize // 200 * 1024 * 1024
    try:
        drive_work_dir = path.join(WORKDIR, segment.drive.name)
        do_output = f'mkdir -p {drive_work_dir} && cd {drive_work_dir} && {{ ln -s ../infra_shared.so . || : ; }} && ' if output_blocks else ''
        _, err, code = run_cmd(f'{do_output}'
            f'sudo bash -c "{path.abspath(infra_conf.root.tools.miniscrub_test)} {lbs} {lbe} {volume_name} {segment.drive.dev_name} '
            f'{praid.dataDisks} {praid.parityDisks} {segment.pRaidIndex} {int(output_blocks)} {stop_on_problems} &>> {file_name}"',
            timeout=timeout
        )
        if code:
            raise Exception(err)

        files_created.add(file_name)
        print(f'Finished scanning {volume_name}@{lbs}-{lbe}')
        # producer(volume, praid, segment, [range_tuple], dbg_di, blocks_per_page, found, statuses)
    except Exception as e:
        logger.debug(f'Failed to produce in {segment} - {repr(e)}')

def main():
    parser = CLIArgumentParser()
    parser.add_argument("-v", "--volumes", type=EntitiesArg(Volume), help='volumes to scan')
    parser.add_argument("-d", "--drives", type=EntitiesArg(Drive), help='drives to scan')
    parser.add_argument("-b", "--num-read-blocks", type=int, default=51200, help='Blocks per task')
    parser.add_argument("-a", "--dlba", type=int, default=0, help='dlba address for scan start')
    parser.add_argument("-n", "--num-blocks", type=int, help='num block to read from dlba start')
    parser.add_argument("--segment-start", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--num-segments", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--block-range-start", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--num-block-ranges", type=int, help=argparse.SUPPRESS)
    parser.add_argument("-r", "--remote", type=str, help=argparse.SUPPRESS)
    #parser.add_argument("-s", "--statuses", type=str, nargs='+', default=BAD_STATUSES, help=f'Default: {BAD_STATUSES}. All statuses: {list(PageData.CODE2STATUS.values())}')
    parser.add_argument("-p", "--processes", type=int, default=30, help='Max number of processes to use at once in total')
    parser.add_argument("--ppd", type=int, default=2, help='Max number of processes to use per drive')
    parser.add_argument("-t", "--timeout", type=int, help='Timeout per miniscrub run')
    parser.add_argument("-o", "--output-bad-blocks", action="store_true", help="output bad blocks to files when found")
    parser.add_argument("-c", "--continue-on-error", action="store_true", help="keep running even if problem found")
    parser.add_argument("--output-dir", type=str, help="output res files to path")

    args = parser.parse_args()

    global HOSTNAME, WORKDIR
    if args.remote:
        manager = args.manager
        HOSTNAME = args.remote
    else:
        manager = Manager.get_manager()
        HOSTNAME = Connection.localhostname()
        Connection.LOCALHOST_CHECK = True

    WORKDIR = run_cmd(f'mktemp -dt nvmesh-scanner.XXXXXX')[0].strip()

    volumes = args.volumes or manager.volumes
    drives = [d for d in Target.instance(name=HOSTNAME).drives if not args.drives or d in args.drives]

    local_segments = get_local_segments(volumes, drives)
    if args.segment_start or args.num_segments:
        local_segments = local_segments[args.segment_start:args.segment_start + args.num_segments] if args.num_segments else local_segments[args.segment_start:]

    print(f'Fetching data from {len(local_segments)} local segments. Management version: {manager.version}')
    pages = 0
    tasks = defaultdict(list)
    volumes_to_scan = set()
    time_started = datetime.now()
    for volume, chunk, praid, segment in local_segments:
        if segment.drive.metadata < VOLUME_MDATA_SIZE or segment.drive.blockSize != Volume.BLOCK_SIZE:
            logger.debug(f'Skipping segment: {segment}.  Drive metadata={segment.drive.metadata}, Block size={segment.drive.blockSize}')
            continue
        blocks_per_page = volume.blockSize // segment.drive.blockSize
        lbs = max(segment.lbs, args.dlba)
        lbe = min(segment.lbe, (args.dlba or lbs) + args.num_blocks * blocks_per_page) if args.num_blocks else segment.lbe
        if lbe < lbs:
            continue
        logger.debug(f'Seg.lbs: {segment.lbs}, bpr: {blocks_per_page}')
        volumes_to_scan.add(volume.name)
        tasks[segment.drive.name].append((volume.name,
                                          volume.chunks.index(chunk),
                                          chunk.pRaids.index(praid),
                                          praid.diskSegments.index(segment),
                                          lbs,
                                          lbe,
                                          args.timeout,
                                          time_started,
                                          args.output_bad_blocks,
                                          int(not args.continue_on_error)))
        pages += (lbe - lbs) // blocks_per_page

    print(f'Starting disk scan at {time_started} on Volumes:{list(volumes_to_scan)}, Drives:{list(tasks.keys())}\n')
    logger.debug(f'#Tasks: {len(tasks)}, #Processes: {args.processes}')
    with ThreadPoolExecutor(max_workers=args.processes) as task_pool:
        def tasks_per_drive(drive_tasks):
            with ThreadPoolExecutor(max_workers=args.ppd) as pool:
                pool.map(worker, drive_tasks, timeout=None)

        task_pool.map(lambda drive: tasks_per_drive(tasks[drive]), tasks, timeout=None)

    for drive in tasks if args.output_bad_blocks else []:
        run_cmd(f'sudo rm {path.join(WORKDIR, drive, "infra_shared.so")}')

    found_errors = False
    for file_name in files_created:
        if run_cmd(f'ls {file_name}')[2]:
            continue

        if not run_cmd(f'sudo test -s {file_name}')[2]:
            print(f'Errors found at: {HOSTNAME}:{file_name}')
            found_errors = True
        else:
            run_cmd(f'sudo rm {file_name}')

    if not found_errors:
        run_cmd(f'sudo rm -rf {WORKDIR}')

    logger.debug(f'Post Executer: {len(tasks)}')
    print(f'{pages} blocks, {Utils.convertBytesToUnit(pages * Volume.BLOCK_SIZE)} in {datetime.now() - time_started}')


if __name__ == "__main__":
    main()
