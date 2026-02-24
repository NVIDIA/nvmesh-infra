#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from datetime import datetime
import sys
import os
import importlib
import atexit
import cProfile
import pstats
from xlro.core.util.common import get_path

if __name__ == '__main__':
    req_tool = os.path.basename(sys.argv[0])
    if req_tool.endswith('-p'):
        print(f'Starting profiler mode at {datetime.now().strftime("%d-%m-%Y %H:%M:%S.%f")}')
        req_tool = req_tool[:-2]
        profiler = cProfile.Profile()
        profiler.enable()

        def generate_profile():
            profiler.disable()
            stats_path = f'{os.getcwd()}/cli_profiling_{datetime.now().strftime("%d-%m-%Y-%H-%M-%S")}.prof'
            pstats.Stats(profiler).sort_stats('tottime').dump_stats(stats_path)
            print(f'.prof file is ready at {stats_path}. \n'
                      f'In order to visualize results in your browser, copy the file locally and run: \n'
                      f'    $ pip install snakeviz \n'
                      f'    $ snakeviz <prof_file_path> ')

        atexit.register(generate_profile)

    tools = {}
    config_file = os.path.join(os.path.dirname(get_path(os.path.realpath(__file__), __file__)), "routes.conf")
    with open(config_file, 'r') as stream:
        for line in stream.readlines():
            name, _, module = line.partition(' ')
            tools[name] = module.strip()

    try:
        # Enable renaming of nvmesh without yet changing our router plan
        req_module = tools.get(req_tool, tools.get('nvmesh'))
        tool_module = importlib.import_module(req_module)
    except Exception as e:
        print(f"Couldn't load requested infra tool {req_tool}: {repr(e)}", file=sys.stderr)
        sys.exit(1)
    if not hasattr(tool_module, 'main'):
        print(f"Tool {req_tool} doesn't have main function", file=sys.stderr)
        sys.exit(1)

    sys.exit(tool_module.main()) # type: ignore[attr-defined]
