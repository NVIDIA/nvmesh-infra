#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from builtins import zip
import os
from os import path
import logging
import argparse

from xlro.core.entities import Manager, Volume

logger = logging.getLogger(__name__)


def init_argparse():
    parser = argparse.ArgumentParser()
    parser.add_argument('-m', '--mgmt',
                        type=lambda m_addr: Manager.from_dict(dict(list(zip(('host', 'port'), m_addr.split(':'))))),
                        required=True)
    parser.add_argument('-v', '--volume', type=lambda vname: Volume.instance(name=vname), required=True)
    parser.add_argument('-a', '--vlba', type=lambda v_addr: Volume.LBA(addr=int(v_addr)), required=True)
    parser.add_argument('-d', '--logdir', default=path.curdir)
    parser.add_argument('-l', '--loglevel', default=None)

    return parser


if __name__ == '__main__':
    parser = init_argparse()
    args = parser.parse_args()

    if args.loglevel:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s: [%(levelname)s] %(name)s: %(message)s'))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(args.loglevel)

    logger.info('calculating physical slice according to given VLBA')
    pslice = args.volume.get_pslice(args.vlba)
    logger.info('calculated slice - {}'.format(pslice))

    slice_dir = path.abspath(path.join(args.logdir, str(pslice)))
    os.mkdir(slice_dir)

    logger.info('dumping slice to - {}'.format(slice_dir))
    pslice.copy(slice_dir)
    logger.info('finished dumping slice successfully')
