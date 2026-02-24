#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
import argparse, glob, json, os, re, subprocess, sys, time
from functools import reduce
from collections import OrderedDict

def get_nested(data_dict, map_list):
    """
    Get value from nested dictionary using functools.reduce. Stolen from https://stackoverflow.com/a/52260663
    Kids, say no to AI! Copy your code from StackOverflow like God intended!
    """
    # if we didn't get a proper dict, return 'N/A'
    if not isinstance(data_dict, (dict, OrderedDict)):
        return 'N/A'
    # if map_list is empty string, get only value of the first key of data_dict
    if map_list == ['']:
        map_list = [[k for k in data_dict.keys()][0]]
    try:
        return reduce(dict.get, map_list, data_dict)
    except TypeError:
        return 'N/A'

if __name__ == '__main__':
    parser = argparse.ArgumentParser('watch')
    parser.add_argument('--cmd', help='command to run')
    parser.add_argument('--file', nargs='*', help='file pattern')
    parser.add_argument('--pattern', help='output matching regex')
    parser.add_argument('--format', default='{0}', help='output format string')
    parser.add_argument('--json', action='store_true', help='parse output as json file')
    parser.add_argument('--jsonobjects', nargs='*', default='', help='objects to get from json. For nested arrays, use \'.\' as delimiter. Default is first object only')
    parser.add_argument('--delay', type=float, default=2.0, help='delay between repeats')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--debugfile', type=argparse.FileType('w'), help='enable debug, but send to this file.')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()

    if args.debugfile:
        args.debug=True
    elif args.debug:
        args.debugfile = sys.stderr

    if args.cmd and args.file:
        print('--cmd and --file are mutually exclusive.')
        sys.exit(2)

    if args.jsonobjects and not args.json:
        print('--jsonobjects must be used with --json.')
        sys.exit(2)

    if not args.cmd and not args.file:
        print('One of --cmd OR --file are required.')
        sys.exit(2)

    args.re = re.compile(args.pattern, re.MULTILINE) if args.pattern else None
    if args.json and not args.jsonobjects:
        args.jsonobjects = ['']

    prev_lines = [] # type: list
    first_diff = True

    def diff(out):
        has_diff = False
        global prev_lines, first_diff
        prev = prev_lines
        # ignore white-space diffs - is it OK?
        curr = [' '.join(line.split()) for line in sorted(out.splitlines())]
        prev_lines = curr[:]
        while prev and curr:
            if prev[0] == curr[0]:
                prev.pop(0)
                curr.pop(0)
                continue
            has_diff = True
            if prev[0] < curr[0]:
                print('< ' + prev.pop(0))
            else:
                print('> ' + curr.pop(0))
        has_diff = bool(has_diff or curr or prev)
        for line in prev:
            print('< ' + line)
        for line in curr:
            print('> ' + line)
        if first_diff or has_diff:
            print('# ' + str(os.getpid()))
            print()
        first_diff = False

    def pargs(*posargs, **kwargs):
        if args.debug: print('MATCH:', posargs, kwargs, file=args.debugfile)
        pass

    def match(out):
        if not args.re:
            if args.debug: print('NO-RE', file=args.debugfile)
            return out
        return '\n'.join([args.format.format(match.group(0), *(match.groups()), **(match.groupdict())) \
            for match in args.re.finditer(out)])

    while True:
        if args.debug: print('LOOP', 'cmd' if args.cmd else 'file', file=args.debugfile)
        if args.jsonobjects:
            for obj in args.jsonobjects:
                if ' ' in obj:
                    args.jsonobjects.remove(obj)
                    args.jsonobjects.extend(obj.split())
            if args.debug and args.jsonobjects[0] == '':  # outside of loop so it only prints once
                print('JSON OBJECTS: Not specified, getting first object only', file=args.debugfile)
            elif args.debug:
                print('JSON OBJECTS:', [o for o in args.jsonobjects], file=args.debugfile)

        if args.cmd:
            if args.debug: print('CMD:', args.cmd, file=args.debugfile)
            try:
                out = str(subprocess.check_output(args.cmd, shell=True).decode('utf-8'))
            except subprocess.CalledProcessError as e:
                out = '{}\nEXIT {}\n'.format(str(e.output.decode('utf-8')), e.returncode)
            if args.debug: print('CMD OUT:', out, file=args.debugfile)
            if args.json:
                outlist = []
                for jsonobj in args.jsonobjects:
                    jsonvalue = str(get_nested(json.loads(out), jsonobj.split('.')))
                    outlist.append(args.cmd + ';' + jsonobj + ':' + jsonvalue if jsonobj else
                                   [k for k in json.loads(out).keys()][0] + ':' + jsonvalue)
                diff('\n'.join(outlist))
            else:
                diff(match(out))
        elif args.file:
            outlist = []
            for fpattern in args.file:
                if args.debug: print('FPATTERN:', fpattern, file=args.debugfile)
                for fpath in glob.glob(fpattern):
                    if args.debug: print('FPATH:', fpath, file=args.debugfile)
                    with open(fpath, 'r') as fp:
                        content = fp.read()
                        if args.json:
                            if args.debug: print('JSON CONTENT:', content, file=args.debugfile)
                            for jsonobj in args.jsonobjects:
                                jsonvalue = str(get_nested(json.loads(content), jsonobj.split('.')))
                                outlist.append(fpath + ';' + jsonobj + ':' + jsonvalue if jsonobj else
                                               [k for k in json.loads(content).keys()][0] + ':' + jsonvalue)
                        else:
                            if args.debug: print('CONTENT:', content, '\nMATCH:', match(content), file=args.debugfile)
                            outlist.append(match(content))
            diff('\n'.join(outlist))
        else:
            # shouldn't get here, but just in case
            print('One of --cmd OR --file are required.')
            sys.exit(2)

        if args.once:
            break
        time.sleep(args.delay)
