#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
import re
import sys

from time import sleep
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from xlro.core.util.ipmi import getIpmiByNode

from xlro.core.util.general_utils import wait_for_it
from xlro.core.util.ssh import Connection

REBOOT_TIMEOUT = 900
logger = logging.getLogger('reboot')


def reboot_host(host, timeout=None, power_cycle=False, safe_mode=False):
    if safe_mode:
        _disable_nvmesh_services(host)
    try:
        # Try primary reboot mechanism
        if power_cycle:
            _apply_power_cycle(host)
        else:
            _apply_reboot(host)
    except Exception as e:
        logger.info('{} reboot failed: {}. Will try fallback.'.format('IPMI' if power_cycle else 'SSH', e))
        try:
            # Try secondary reboot mechanism
            if power_cycle:
                _apply_reboot(host)
            else:
                _apply_power_cycle(host)
        except Exception as e:
            logger.error('{} reboot failed: {}'.format('SSH' if power_cycle else 'IPMI', e))
            return False

    # Need to wait for reboot/power-cycle to take effect
    logger.info('Sleep until reboot check for {}...'.format(host))
    sleep(20)
    reboot_timeout = timeout or REBOOT_TIMEOUT

    logger.info('Check ssh connection to {}. reboot-timeout={}.'.format(host, reboot_timeout))
    result = wait_for_it(lambda: Connection(host, timeout=20, fail_is_error=False), poll=10, timeout=reboot_timeout)

    logger.info('Reboot of {} - {}'.format(host, 'success' if result else 'failure'))

    return bool(result)


def reboot(hosts, **kwargs):
    with ThreadPoolExecutor(len(hosts)) as executor:
        results = executor.map(lambda h: reboot_host(h, **kwargs), hosts)
    return list(results)


def _disable_nvmesh_services(host):
    from xlro.core.entities.host import Host

    logger.debug('disabling nvmesh services on {}'.format(host))
    host = Host.instance(name=host)
    _, err, code = host.connection.execute('sudo systemctl disable nvmeshclient nvmeshtarget nvmeshmgr')
    if code:
        logger.info('Unable to disable nvmesh services on {}: {}'.format(host, err))


def _apply_reboot(host):
    # The "echo OK" is to ensure there's some output before we're disconnected.
    # If we see ouput, we assume we actually ran.
    out, err, code = Connection.execute_on_host(host, 'echo OK && sudo reboot --no-wall now', reconnect_timeout=0, timeout=20)
    logger.info('Reboot {} - err={}, out={}, code={}'.format(host, err, out, code))
    assert out, 'SSH reboot {} failed. Try IPMI.'.format(host)


def _apply_power_cycle(host):
    ipmi = getIpmiByNode(host)
    logger.info('Trying power-cycle of {} via {}'.format(host, ipmi.address))
    ret = ipmi.powerCycle()
    assert not ret.returnCode, "IPMI power cycle on {} failed code: {}, stderr: {}".format(host, ret.returnCode, ret.stderr)


def main():
    import argparse
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-l', '--loglevel', default=None)
    parser.add_argument('-t', '--timeout', type=int, default=REBOOT_TIMEOUT, help='timeout to wait for hosts to return')
    parser.add_argument('hosts', nargs=argparse.ONE_OR_MORE)
    parser.add_argument('-p', '--power_cycle', default=False, action="store_true", help='restart using ipmi power cycle')
    parser.add_argument('-s', '--safe_mode', default=False, action="store_true", help='disable nvmesh services befoore reboot')
    parser.add_argument('-i', '--ipmi', action="store_true", help='Just locate IPMI server.')
    args = parser.parse_args()

    if args.loglevel:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s: [%(levelname)s] %(name)s: %(message)s'))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(args.loglevel)

    # Accept args in the setup format (comma and/or colon separated)
    hostnames = {hostname for h in args.hosts for hostname in re.split(',|:', h)}
    if args.ipmi:
        for host in hostnames:
            print(f'Host: {host}, IMPI: {getIpmiByNode(host)}')

    else:
        sys.exit(
            reboot(hostnames, timeout=args.timeout, power_cycle=args.power_cycle, safe_mode=args.safe_mode).count(False))


if __name__ == '__main__':
    main()
