#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from future import standard_library
standard_library.install_aliases()
import os
import yaml
import json
import logging
import configparser

_logger = logging.getLogger('read_dict')

def read_dict(path, section='default', default_format='yaml'):
    ''' Used for loading dict from file.
    Path can be: *.js|json, *.yaml|yml, *.ini|conf
    '''
    suffix = ''
    try:
        if path.startswith('~/'):
            path = os.environ.get('HOME', '~') + path[1:]
        suffix = path.rpartition('.')[2]
        if suffix not in ('js', 'json', 'yaml', 'yml', 'ini', 'conf'):
            suffix = default_format
        with open(path, 'r') as dfile:
            dbuffer = dfile.read()
        if suffix in ('json', 'js'):
            return json.loads(dbuffer)
        elif suffix in ('yaml', 'yml'):
            # Prefer we only fail on missing module if someone is actually using YAML
            return yaml.safe_load(dbuffer)
        elif suffix in ('ini', 'conf'):
            conf = configparser.SafeConfigParser()
            conf.read(path)
            return dict(conf.items(section))
        else:
            raise Exception('Unsupported configfile type: {}'.format(path))
    except Exception as e:
        raise Exception('Cannot load dict from: {} [format={}] - {}'.format(path, suffix, e))

if __name__ == '__main__':
    import sys
    for path in sys.argv[1:]:
        # print('PATH:', path)
        try:
            d = read_dict(path)
            # print('DICT:')
            print(json.dumps(d, indent=2))
        except Exception as e:
            print('ERR:', type(e), e)
