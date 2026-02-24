#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
from os import path
import xlro.core.entities as entities
from xlro.core.util.common import get_path
from xlro.core.util.cli_util import CLIArgumentParser
from jinja2 import Environment, FileSystemLoader, Template
from xlro.core.sdk.Utils import Utils

def main():
    parser = CLIArgumentParser(require_manager=True, description='Show setup info via jinja2 template', epilog=f'Example: {sys.argv[0]} -M n179 simple-setup.j2')
    parser.add_argument('template', default='simple-setup.j2', nargs='?', help='template file name')
    args = parser.parse_args()

    try:
        import glob
        template_path = get_path(f'{path.dirname(__file__)}/templates', 'templates')
        print(Environment(loader=FileSystemLoader(['.', template_path])).get_template(args.template).render(
            {'mgr': args.manager, 'utils': Utils, 'entities': entities}))
        sys.exit(0)
    except Exception as e:
        print(f'Cannot load/render template: {args.template}. {repr(e)}')
        sys.exit(1)

if __name__ == '__main__':
    main()
