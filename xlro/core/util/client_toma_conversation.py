#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
import json

from xlro.core.entities import Client, Volume, ClientVolumeTopology
from datetime import datetime


def init_argparse():
    import argparse

    datetime_format = "%d/%m/%Y %H:%M:%S"

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--client', type=lambda c: Client(name=c), required=True)
    parser.add_argument('-v', '--volume', type=lambda v: Volume(name=v), required=True)
    parser.add_argument('-s', '--start_time', type=lambda t: datetime.strptime(t, datetime_format), default=None,
                        help='Specify the client\'s date in the following format {0}'.format(datetime_format))
    parser.add_argument('-e', '--end_time', type=lambda t: datetime.strptime(t, datetime_format), default=None,
                        help='Specify the client\'s date in the following format {0}'.format(datetime_format))
    return parser


if __name__ == '__main__':
    parser = init_argparse()
    args = parser.parse_args()

    conversations = Client.get_toma_client_conversations(args.client, args.volume, args.start_time, args.end_time)
    json.dump([c.__dict__ for c in conversations], sys.stdout, indent=2)
