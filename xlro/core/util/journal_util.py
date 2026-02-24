# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
import os
import logging
from typing import Dict, List
from collections import defaultdict
from xlro.core.entities import Client, JournalPage, SliceRangeTransactions, Volume
from xlro.core.util.block_objects import StaticSliceRange
from xlro.core.util.cli_util import CLIArgumentParser, EntityArg

journal_util_logger = logging.getLogger(__name__)


def init_argparse():
    parser = CLIArgumentParser()

    parser.add_argument('-v', '--volume', type=EntityArg(Volume), required=True)
    parser.add_argument('-a', '--vlba', type=lambda v: Volume.LBA(int(v)), required=True)
    parser.add_argument('-b', '--full-blockset', action='store_true')
    parser.add_argument('-n', '--num-slices', type=int)
    parser.add_argument('-c', '--client', type=EntityArg(Client))
    parser.add_argument('-l', '--logdir', default=os.path.curdir)
    parser.add_argument('-d', '--dump-journals', action='store_true')
    return parser


def print_all_transactions(slice_range, client=None):
    c2txs = defaultdict(list)
    for tx in SliceRangeTransactions(slice_range, client=client, with_data=False).slice_range_transactions:
        c2txs[tx.client].append(tx)

    for client, txs in c2txs.items():
        print("---------------------------------------------{}---------------------------------------------".format(
            client.name))
        for tx_idx, tx in enumerate(txs):
            print('{}) {}'.format(tx_idx, str(tx)))
            for jre in tx.journal_entries:
                print('- {}'.format(str(jre)))
                for jpage in jre.journal_pages:
                    print('  - {}'.format(repr(jpage)))


def dump_jpages(local_dir: str, slice_range: StaticSliceRange, client: Client) -> Dict[str, List[JournalPage]]:
    if not os.path.isdir(local_dir):
        os.makedirs(local_dir)

    dumped_jpages = defaultdict(list)
    slice_range_txmap = SliceRangeTransactions(slice_range, client=client, with_data=True).slice_range_transactions
    journal_util_logger.info('Found the following transactions for {}: {}'.format(slice_range, slice_range_txmap))
    for tx in slice_range_txmap:
        for jre in tx.journal_entries:
            jr = jre.journal_range
            jr_dir = os.path.join(local_dir, "_".join([jr.journal_partition.drive.target.name,
                                                       jr.journal_partition.drive.name, str(jr.index),
                                                       jr.client_host.split('_')[0], jr.status]))
            if not os.path.isdir(jr_dir):
                os.makedirs(jr_dir)

            for jpage in jre.journal_pages:
                with open(os.path.join(jr_dir, "_".join([str(jpage), 'data'])), 'wb') as dfp, \
                        open(os.path.join(jr_dir, "_".join([str(jpage), 'metadata'])), 'wb') as mfp:
                    dumped_jpages[jr_dir].append(jpage)
                    dfp.write(jpage.data)
                    mfp.write(jpage.meta_data)

    return dumped_jpages


if __name__ == '__main__':
    args = init_argparse().parse_args()
    journal_util_logger.info('calculating physical slice according to given VLBA')
    slice_range = args.volume.get_pslice(args.vlba)
    journal_util_logger.info('calculated slice - {}'.format(slice_range))
    if args.full_blockset:
        slice_range = args.volume.get_block_set(args.vlba)
    elif args.num_slices:
        slice_range = StaticSliceRange(slice_range.praid, slice_range.slice_idx, args.num_slices)

    if args.dump_journals:
        journals_dir = os.path.abspath(os.path.join(args.logdir, 'journals'))
        journal_util_logger.info('dumping journals to - {}'.format(journals_dir))
        dump_jpages(journals_dir, slice_range, args.client)
        journal_util_logger.info('finished dumping journals successfully')
    else:
        print_all_transactions(slice_range, args.client)
