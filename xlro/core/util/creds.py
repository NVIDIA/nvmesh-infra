#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import logging
import base64
from typing import Tuple, List, Optional
from fnmatch import fnmatch
from xlro.core import infra_conf
from xlro.core.util.general_utils import host_name, host_aliases

logger = logging.getLogger('creds-util')

_local_settings_dir = None
def local_settings_dir() -> str:
    global _local_settings_dir
    if not _local_settings_dir:
        _local_settings_dir = os.path.expanduser(os.environ.get('NVMESH_DIR', '~/.nvmesh/'))
        os.makedirs(_local_settings_dir, exist_ok=True)
    return _local_settings_dir

CREDS_FILE = 'mgmt_creds'
def get_creds(mgrhost: str, defaults: Optional[Tuple[str,str]] = None, creds_path: str=None, user: str=None) -> Tuple[str, str]:
    creds_path = creds_path or os.path.join(local_settings_dir(), CREDS_FILE)
    hostname_aliases = host_aliases(mgrhost)
    try:
        with open(creds_path, 'r') as creds_fp:
            logger.debug(f'Getting creds from creds file {creds_path}')
            for line in creds_fp:
                creds: List[str] = line.strip().split()
                if not creds or creds[0][0] == '#':
                    continue
                if len(creds) != 3:
                    continue
                hostpatterns, login, passwd = creds
                logger.debug(f'Attempt decoding matching creds for user "{user}" to match with hostname aliases {hostname_aliases}')
                if user in (None, login) and any([fnmatch(alias, hp) for hp in hostpatterns.split(',') for alias in hostname_aliases]):
                    try:
                        return (login, base64.b64decode(passwd.encode()).decode())
                    except Exception as e:
                        logger.info(f'Ignoring invalid entry for {hostpatterns}. {repr(e)}')
                        break
            else:
                logger.debug(f'Creds file {creds_path} did not yield relevant creds - falling back to defaults')
    except FileNotFoundError:
        pass
    if defaults:
        return defaults
    return (user or '', '')

def store_creds(mgrhost: str, user: str, passwd: str, creds_path=None):
    creds_path = os.path.join(local_settings_dir(), CREDS_FILE)
    lock_path = creds_path + '.lck'
    os.makedirs(os.path.dirname(creds_path), exist_ok=True)
    fullhost = host_name(mgrhost)
    lock_fd = -1
    try:
        lock_fd = os.open(lock_path, flags=os.O_WRONLY|os.O_CREAT, mode=0o600)
        with os.fdopen(lock_fd, 'w') as lock_fp:
            # New first, to override any patterns, since we use first match
            lock_fp.write(fullhost + ' ' + user + ' ' + base64.b64encode(passwd.encode()).decode() + '\n')
            try:
                with open(creds_path, 'r') as creds_fp:
                    for line in creds_fp:
                        creds: List[str] = line.strip().split()
                        if len(creds) != 3 or creds[0] != fullhost or creds[1] != user:
                            lock_fp.write(line)
            except FileNotFoundError:
                pass
            os.rename(lock_path, creds_path)
    except Exception as e:
        logger.info(f'Update of {creds_path} failed. {repr(e)}')
        raise
    finally:
        try:
            if lock_fd >= 0:
                os.close(lock_fd)
            os.remove(lock_path)
        except:
            pass

if __name__ == '__main__':
    import sys
    from xlro.core.util.cli_util import CLIArgumentParser
    _, args = CLIArgumentParser().parse_known_args()
    try:
        if len(args) == 1:
            print(get_creds(args[0]))
        elif len(args) == 2:
            print(get_creds(args[0], user=args[1]))
        elif len(args) == 3:
            store_creds(*args)
        else:
            print(f'Usage: {__file__} host [user [passwd]]')
            sys.exit(2)
    except Exception as e:
        print(e)
        sys.exit(1)
    sys.exit(0)
