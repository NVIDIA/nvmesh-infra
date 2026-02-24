#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
import json
import requests

def multi_json_reader(filename):
        with open(filename) as f:
            text = ''
            for line in f:
                text += line
                try:
                   yield json.loads(text)
                   text = ''
                except:
                    pass
            if text:
                raise Exception(f'Failed to parse: {text}')

def replay(filename):
    print('REQUESTS VERSION:', requests.__version__)
    s = requests.session()
    for j in multi_json_reader(filename):
        try:
            url = j['SERVER'] + j['ROUTE']
            payload = j.get('PAYLOAD')
            if isinstance(payload, dict) and 'password' in payload:
                payload['password'] = 'admin'
            print(f'### {url}')
            if j['METHOD'].lower() == 'get':
                out = s.get(url, params=payload, verify=False)
            else:
                out = s.post(url, data=payload, verify=False)
            print(out)
            print(out.text)
        except Exception as e:
            print(f'*** {repr(e)}')

if __name__ == '__main__':
    files = sys.argv[1:]
    if not files:
        print(f'Usage: {sys.argv[0]} rest-file ...', file=sys.stderr)
        sys.exit(2)

    for filename in files:
        print(f'## FILENAME: {filename}')
        replay(filename)
