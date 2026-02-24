# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from typing import Dict,List,Union
from os import path
import re
import logging
from xlro.core.util import monitor, ssh
from xlro.core.entities import Host, Client
from collections import defaultdict, Sequence
from threading import Lock

from xlro.core.util.actions import RemoteCmdAction
from xlro.core.util.monitor import MsgHandler

logger = logging.getLogger('xlro.core.util.system_monitor')


class SysMonitor(monitor.CmdMonitor):
    # filter out mgmt stuff as we run with DEBUG and this abused journalctl
    SYS_LOG_CMD = 'sudo journalctl -f | grep -Eiv "nvmeshmgr.*\[.*\]|managementCM|managementAgent"'
    # Simple patterns here, multi-line are in __init__()
    SYS_PATTERNS: List[Union[str, Dict[str, List[str]], monitor.SpecialPattern]] = [
            "page allocation failure. order:1, mode:0x20",
            r'\bBUG:',
            r'Watchdog detected hard LOCKUP on cpu',
            ]

    def __init__(self, host, non_errors=None, **kwargs):
        super(SysMonitor, self).__init__(host=host, cmd=self.SYS_LOG_CMD, expected=0, resilient=True, **kwargs)

        # non_errors is an optional list of patterns, which add a NON_ERROR key to Kernel Warnings
        kernel_start = '(KERNEL WARNING TRIGGERED|kernel: WARNING: .* at (?P<location>\S+) (?P<func>\S+)(.* \[(?P<module>[^\]]+)])?)'
        kernel_middle = None
        if non_errors:
            # kernel_middle = '(?P<{}>.*({}))?'.format(self.NON_ERROR, '|'.join(non_errors))
            kernel_middle = '(.*(?P<{}>{}))?'.format(MsgHandler.NON_ERROR, '|'.join(non_errors))
            kernel_start += kernel_middle
        patterns = self.SYS_PATTERNS + [monitor.RangePattern(kernel_start, "end trace", 100, kernel_middle)]
        self.add_msg_handler(monitor.PatternHandler(patterns, {'host': host}))

        self.add_action(RemoteCmdAction(cmd="sudo top -b -n 1; sudo top -b -n 1; sudo top -b -n 1", host=host,
                                        logfile=self.logpath + '_top_analyzer' if self.logpath else None,
                                        condition=lambda e: 'lockup' in e['__message'],
                                        is_async=False))

    def on_event(self, event: Dict) -> None:
        super(SysMonitor, self).on_event(event)
        if MsgHandler.NON_ERROR in event:
            self.logger.warning('Ignoring event containing: {}'.format(event[MsgHandler.NON_ERROR]))
        else:
            self.logger.warning(event[MsgHandler.FULL_MSG].strip())


class MgmtSysMonitor(monitor.CmdMonitor):
    SYS_LOG_CMD = 'sudo journalctl -f | grep -E \"nvmeshmgr[^\[]*\[.*\]\" | grep -v DEBUG'
    SYS_PATTERNS = [
            {
                'nvmeshmgr-stats': [
                    'ERROR: Failed to save statistics',
                ]
            },
            {
                'nvmeshmgr': [
                    'Unhandled exception',
                ]
            }
        ]

    def __init__(self, host, **kwargs):
        super(MgmtSysMonitor, self).__init__(host=host, cmd=self.SYS_LOG_CMD, expected=0, resilient=True, **kwargs)
        self.add_msg_handler(monitor.PatternHandler(self.SYS_PATTERNS, {'host': host}))


if __name__ == '__main__':
    import sys, json

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[%(levelname)s] %(filename)s:%(lineno)d (%(name)s) %(message)s'))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.DEBUG)
    logger = logging.getLogger(__file__)

    if len(sys.argv) < 2:
        print('Usage: {} file ...'.format(sys.argv[0]))
        sys.exit(2)

    for logfile in sys.argv[1:]:
        print('***', logfile, '***')
        SysMonitor.SYS_LOG_CMD = 'cat ' + path.abspath(logfile)
        mon = SysMonitor('localhost', non_errors=['WARNING-ONLY', 'NON-FATAL'])
        with open(logfile, 'r') as fp:
            for line in fp:
                print('input:', line)
                mon.on_message(line)
            for event in mon.events:
                print('Event:', json.dumps(event, indent=2))
