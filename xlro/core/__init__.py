# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import signal
import logging
import yaml
import os
import sys
import random
import threading
from confetti import Config

from xlro.core.util.dump_stack_traces import dump_all_stack_traces

logger = logging.getLogger('xlro.core')


# loads default infra configuration and override if needs
with open("{}/config/infra_config.yaml".format(os.path.dirname(__file__)), 'rb') as fp:
    content = yaml.safe_load(fp)
    infra_conf = Config(content)

conf_file = os.getenv("INFRA_CONFIG")
if conf_file:
    try:
        with open(conf_file, 'rb') as fp:
            content = yaml.safe_load(fp)
            # we don't enforce that all configurations paths exists before apply it as we let user to configure new paths
            infra_conf.extend(content)
    except Exception as e:
        logger.exception("")
        raise Exception("Couldn't load {} as configuration file. ({}) {}".format(conf_file, type(e), e))

# set debug signal
signal.signal(signal.SIGUSR1, dump_all_stack_traces)

# set LC_* for remote cmds recognition
os.environ['LC_NAME'] = 'infra_{}'.format(random.randint(0, 999999))
os.environ['LC_IDENTIFICATION'] = os.getenv('BUILD_URL', "")

# handle all infra side threads closing
shutdown = threading.Event()


# An API to close infra
def close_infra():
    logger.debug("Closing infra!")
    shutdown.set()


# An API to check if infra is running, could be used by side threads to abort when needed
def is_infra_running():
    return not shutdown.is_set()
