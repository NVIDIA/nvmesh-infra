#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.core.util.cli_util import CLIArgumentParser
from argparse import FileType
import json
import sys

def main():
    from xlro.core.util.cli_util import CLIArgumentParser
    # Use general cli-parser to handle hidden, debugging style arguments
    parser = CLIArgumentParser(require_manager=True)
    parser.add_argument('-m', '-X', '--method', default='GET')
    parser.add_argument('-p', '--payload', type=FileType('r'), help='Payload file (implies POST)')
    parser.add_argument('url')
    defargs = f'-M nvme26 --logfile rest.logs --rest-debug rest.rest'.split()
    args = parser.parse_args(defargs + sys.argv[1:])

    payload = None
    if args.payload:
        print(f'payload arg: {args.payload}')
        try:
            payload = json.load(args.payload)
        except Exception as e:
            print(f'Cannot load payload. {repr(e)}')
            sys.exit(1)
        args.method = 'PUT'

    assert args.manager.connection, f'Cannot connect to {args.manager}'

    args.manager.mgmt_host.syslog(f'START REST TEST {args.url}')
    err, out = args.manager.connection.request(args.method.lower(), args.url, payload, rest_log=sys.stdout)
    args.manager.mgmt_host.syslog(f'END REST TEST {args.url}')


if __name__ == '__main__':
    main()
