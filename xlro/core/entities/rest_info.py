#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
from typing import Any, Union, Type, TypeVar, Tuple, Dict, List, Sequence, Optional, TYPE_CHECKING
from threading import Lock
import yaml
from copy import deepcopy
from os import path
import logging

from collections import defaultdict
from packaging.version import Version
from xlro.core.util.common import get_path
from xlro.core.util.dict_util import merge_dicts, del_path
from xlro.core.sdk.Utils import MongoObj
from xlro.core.util.general_utils import parentlookupdict


class MgmtVersion(str):
    """A version string that supports semantic version comparisons"""
    
    def __new__(cls, ver):
        obj = str.__new__(cls, str(ver))
        obj._ver = Version(str(ver))
        return obj

    def _to_version(self, other):
        if isinstance(other, MgmtVersion):
            return other._ver
        if isinstance(other, Version):
            return other
        return Version(str(other))

    def __lt__(self, other): return self._ver < self._to_version(other)
    def __le__(self, other): return self._ver <= self._to_version(other)
    def __gt__(self, other): return self._ver > self._to_version(other)
    def __ge__(self, other): return self._ver >= self._to_version(other)


class RestEntityInfo(object):
    def __init__(self, dbkey: Optional[str] = None, route: Optional[str] = None, projection: Optional[str] = None, rest2infra: Optional[Dict[str, str]] = None, **kwargs):
        # Maybe just take **kwargs and update from that vs. huge sig...
        self.kwargs = kwargs
        self.route = route
        self.dbkey = dbkey
        self.projection: Optional[List[MongoObj]] = None

        nested = kwargs.get('nested')
        if projection:
            if projection[0] == '-':
                value = 0
                projection = projection[1:]
            else:
                value = 1
            self.projection = [MongoObj(p.strip(), value) for p in projection.split(',')]
            if nested:
                for mo in self.projection:
                    mo.field = f'{nested}.{mo.field}'
        if nested:
            self.projection = [MongoObj(nested, 1)] + (self.projection or [])

        if route and dbkey is None:
            self.dbkey = '_id'
        self.rest2infra = rest2infra or {}
        # Invert for reverse mapping
        self.infra2rest = {v: k for k, v in self.rest2infra.items()}
        self.infrakey = self.rest2infra.get(self.dbkey, self.dbkey)

    def __str__(self):
        if self.route:
            return f'rest: /{self.route}?id={self.dbkey}, rest2infra: {self.rest2infra}'
        return f'Non-Rest, rest2infra: {self.rest2infra}'

class RestVersionInfo(object):
    def __init__(self, version: str, entities: Dict[str, RestEntityInfo] = {}, features: Optional[Dict[str, Any]] = None, extends: Optional[str] = None):
        self.version = version
        self.extends = extends
        self.entities: parentlookupdict = parentlookupdict(RestEntityInfo)  # type: ignore
        self.entities.update(entities)
        self.features: Dict[str, bool] = defaultdict(bool)
        if features:
            self.features.update(features)

class RestVersionManager(object):
    logger = logging.getLogger('RestVersionManager')
    lock = Lock()
    info_map: Optional[Dict[Union[Tuple, str], RestVersionInfo]] = None # Map of version string to version info
    full_confs: Dict[str, Dict] = {} # Map of version string to version info
    versions: List[Tuple[int, ...]] = [] # Versions as list of ints for easier comparison
    _default_version: str = ''
    _max_version: str = ''

    def __init__(self, *args, **kwargs):
        raise Exception('Use class as singleton.')

    @classmethod
    def default_version(cls):
        # Need to load map to get default version
        if not cls._default_version:
            cls._info_map()
        return cls._default_version

    @classmethod
    def max_version(cls):
        # Need to load map to get max version
        if not cls._max_version:
            cls._info_map()
        return cls._max_version

    @classmethod
    def _info_map(cls):
        # If first pass, load it
        if cls.info_map is None:
            with cls.lock:
                if cls.info_map is None:
                    conf_path = path.join(get_path(path.dirname(__file__)), 'rest.yaml')
                    cls.logger.debug(f'Loading REST info from {conf_path}')
                    try:
                        with open(conf_path, 'r') as confp:
                            conf_map = yaml.safe_load(confp)
                        versions: List[Tuple[int, ...]] = []
                        infomap = {}
                        for v_name, v_info in conf_map.items():
                            # Support partial overrides
                            extends = v_info.pop('extends', None)
                            if extends:
                                v_info = merge_dicts(deepcopy(cls.full_confs[extends]), v_info)
                            # Allow for info to be deleted
                            for dpath in v_info.pop('drops', []):
                                del_path(v_info, dpath)
                            cls.full_confs[v_name] = v_info
                            # Convert dict to objects
                            entities = { ename: RestEntityInfo(**(emap or {})) for ename, emap in v_info.get('entities', {}).items() }
                            infomap[v_name] = RestVersionInfo(v_name, entities=entities, features=v_info.get('features'), extends=extends)
                            # Parse version #s
                            v_ints = tuple(int(vpart) for vpart in v_name.partition('-')[0].split('.'))
                            versions.append(v_ints)
                            infomap[v_ints] = infomap[v_name]
                        # Rest versions are parsed version #s reverse sorted
                        cls.versions = sorted(versions, reverse=True)
                        cls._default_version = '.'.join([str(i) for i in cls.versions[-1]])
                        cls._max_version = '.'.join([str(i) for i in cls.versions[0]])
                        cls.logger.info(f'Loaded REST defs for versions: {cls.versions}. Default: {cls._default_version}')
                        # NOTE: Set at end, so no one will look while we're loading
                        cls.info_map = infomap
                    except Exception as e:
                        cls.logger.error(f'Failed to load {conf_path}: {repr(e)}')
                        # Change from none to prevent retrying load, although exception should stop things
                        cls.info_map = {}
                        raise
        return cls.info_map

    @classmethod
    def version_info(cls, version: str) -> RestVersionInfo:
        ''' Lookup info for REST to mgmt version in lazily loaded cache '''
        info_map = cls._info_map()

        try:
            return info_map[version]
        except:
            # Find highest match <= parsed version
            parsed_version = tuple(int(vpart) for vpart in version.partition('-')[0].split('.'))
            cls.logger.debug(f'Finding REST info for {version}. {parsed_version} from {cls.versions}')
            for known_ver in cls.versions:
                if known_ver <= parsed_version:
                    # Add to map, so we only do this once
                    cls.logger.info(f'Found REST-INFO version {".".join([str(i) for i in known_ver])} for mgmt-version: {version}.')
                    return info_map.setdefault(version, info_map[known_ver])
            raise Exception(f'Cannot find REST version defs for {version}{parsed_version}.  Known: {cls.versions}')


def main():
    import json
    from xlro.core.util.cli_util import CLIArgumentParser
    parser = CLIArgumentParser()
    parser.add_argument('-r', '--release', help='Release to match')
    parser.add_argument('-e', '--entity', help='Entity to dump')
    parser.add_argument('-j', '--json', action='store_true', help='Dump raw JSON')
    args = parser.parse_args()

    if not args.release and not args.manager:
        parser.error('One of -M|--manager or -r|--release is required.')
        parser.print_usage()
    if args.release and args.manager:
        parser.error('-M|--manager and -r|--release are mutually exclusive.')
        parser.print_usage()

    v = args.release if args.release else args.manager.api_version
    vinfo = RestVersionManager.version_info(v)
    if not args.json:
        print(f'Version: {v} (rest.yaml version: {vinfo.version})')
        print(f'Features: {json.dumps(vinfo.features, indent=2).strip("{}")}')

    if not args.entity:
        if args.json:
            json.dump(RestVersionManager.full_confs[vinfo.version], fp=sys.stdout, indent=2)
        else:
            print(f'Entities:')
            for ename, einfo in vinfo.entities.items():
                print(f'  {ename}: {f"/{einfo.route}?id={einfo.dbkey}" if einfo.route else "non-REST"}')
    else:
        # Caseless lookup
        ename = args.entity.lower()
        ename, einfo = next((k, v) for k, v in vinfo.entities.items() if k.lower() == ename)
        if args.json:
            json.dump(RestVersionManager.full_confs[vinfo.version]['entities'][ename], fp=sys.stdout, indent=2)
        else:
            print(f'Entity: {ename}')
            einfo = vinfo.entities[ename]
            edata = vars(einfo)
            edata.update(edata.pop('kwargs'))
            edata.pop('infra2rest')
            print(json.dumps(edata, indent=2, separators=('', ': '), default=str).strip("{}"))


if __name__ == '__main__':
    main()
