# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
import threading

from typing import Dict

from xlro.core.util.general_utils import IDAdapter
from xlro.core.util.monitor import CmdMonitor, PatternHandler, RangePattern


class Stat(object):
    def __init__(self, name: str, delay: int):
        self.name = name
        self.delay = delay
        self.total = 0
        self.iterations = 0
        self.start_value = 0
        self.max_burst = 0
        self.first_event = False
        self.reset()
        self.logger = IDAdapter(logging.getLogger(name or self.__class__.__name__))

    def update(self, value: int):
        if value < self.total:
            self.logger.info('Got wraparound')
            self.dump()
            self.reset()

        if not self.first_event:
            self.first_event = True
            self.start_value = value
            self.total = value
            return

        self.iterations += 1
        diff = value - self.total
        if diff > self.max_burst:
            self.max_burst = diff

        self.total = value

    def reset(self):
        self.total = 0
        self.iterations = 0
        self.start_value = 0
        self.max_burst = 0
        self.first_event = False

    def dump(self):
        if self.iterations:
            self.logger.info('AVG per second is {}, max_burst per second = {}'.format((self.total - self.start_value) / (self.iterations * self.delay),
                                                                                      self.max_burst / self.delay))
        else:
            self.logger.info('No data was collected')


class TomaTracesStatsMonitor(CmdMonitor):

    def __init__(self, host: str, delay: int = 2, **kwargs):
        super(TomaTracesStatsMonitor, self).__init__(host=host,
                                                     log_option=0,
                                                     cmd=f'while :; do sudo /opt/nvmesh/common-repo/tools/toma_rpc trace-status; sleep {delay}; done',
                                                     **kwargs)
        self.add_msg_handler(PatternHandler([r'Num of buffer written to memory\(4k\): (?P<buffers>\d+)']))
        self.stat = Stat(f'toma_{host}', delay)
        self.lock = threading.Lock()
        self.reset()

    def on_event(self, event):
        super(TomaTracesStatsMonitor, self).on_event(event)
        with self.lock:
            current = int(event['buffers'])
            self.stat.update(current)

    def reset(self):
        with self.lock:
            self.stat.reset()

    def dump(self):
        self.stat.dump()


class KernelTracesStatsMonitor(CmdMonitor):

    def __init__(self, host, delay: int = 4, **kwargs):
        super(KernelTracesStatsMonitor, self).__init__(host=host,
                                                       cmd=f'while :; echo ~start~; do grep --line-buffered -E "capuch.*long|bufs_used" /proc/nvmeib/tracer/stats; echo ~end~; sleep {delay}; done',
                                                       log_option=0,
                                                       **kwargs)
        self.add_msg_handler(PatternHandler([RangePattern(startPattern=r'^> capuch \d+,(?P<cpu>\d+),(?P<chan_name>\S+),',
                                                          endPattern=r'^\tstats.bufs_used: (?P<bufs_used>\d+)'),
                                             ]))
        self.add_msg_handler(PatternHandler(r'~start~', {'START': True}))
        self.add_msg_handler(PatternHandler(r'~end~', {'END': True}))
        self.delay = delay
        self.lock = threading.Lock()
        self.stats: Dict[str, Stat] = {}
        self.tmp: Dict[str, int] = {}
        self.reset()
        self.dirty = False
        self.warn_on_msgs = 8000  # 24 cpus lead to handle 1000 messages in 15 seconds so to be safe

    def on_event(self, event):
        super(KernelTracesStatsMonitor, self).on_event(event)

        with self.lock:
            if 'START' in event:
                self.dirty = False

            if self.dirty:
                return

            if 'END' not in event and 'START' not in event:
                key = event['chan_name']
                current = int(event['bufs_used'])
                try:
                    self.tmp[key] += current
                except KeyError:
                    self.tmp[key] = current

                return

            # END event
            if 'END' in event:
                for key, val in self.tmp.items():
                    try:
                        self.stats[key].update(val)
                    except KeyError:
                        self.stats[key] = Stat(f'{key}_{self.host}', self.delay)
                        self.stats[key].update(val)
            self.tmp = {}

    def reset(self):
        with self.lock:
            self.stats = {}
            self.tmp = {}
            self.dirty = True

    def dump(self):
        for val in self.stats.values():
            val.dump()
