# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
from xlro.core.util import monitor

logger = logging.getLogger('xlro.core.util.system_monitor')


class ControllerLogMonitor(monitor.CmdMonitor):
    LOG_CMD = 'sudo journalctl -f SYSLOG_IDENTIFIER=spdk'
    MONITOR_PATTERNS = [
            r'-->', #state change
            r'assertion',
            r'ERROR',
            r'status=100[4-5]' #rebuild failure
        ]

    def __init__(self, controller, *args, **kwargs):
        controller_host_name = controller.controller_host.name
        super(ControllerLogMonitor, self).__init__(self.LOG_CMD, controller_host_name, *args, **kwargs)
        self.controller = controller
        self.add_msg_handler(monitor.PatternHandler(self.MONITOR_PATTERNS))


class MRSPLogMonitor(monitor.MultiMonitor):

    def __init__(self, mrsp, *args, **kwargs):
        super(MRSPLogMonitor, self).__init__(*args, **kwargs)
        self.add_monitor(ControllerLogMonitor(mrsp.controller1, *args, **kwargs))
        self.add_monitor(ControllerLogMonitor(mrsp.controller2, *args, **kwargs))
