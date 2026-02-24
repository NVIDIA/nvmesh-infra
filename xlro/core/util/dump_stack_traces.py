# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
import threading
import logging
import traceback

logger = logging.getLogger(__name__)


def dump_all_stack_traces(_signo, _stack_frame):
    for th in threading.enumerate():
        logger.warning("Dumping stack of {}:{}".format(th.ident, th))
        logger.warning("".join(f for f in traceback.format_stack(sys._current_frames()[th.ident]))) # type: ignore
    return
