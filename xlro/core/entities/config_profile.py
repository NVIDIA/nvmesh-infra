#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import List, Mapping, Union, Any
from xlro.core.entities import Manager, SDKEntity, SourceTypes
from xlro.core.entities.etypes import KeyValue
from xlro.core.entities.base import PropertySpec
from xlro.core.entities.sdk_base import sdk_entity

@sdk_entity(sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT, SourceTypes.PROC])
class ConfigProfile(SDKEntity):
    name : str = PropertySpec(str, key=True)
    uuid : str = PropertySpec(str)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    version : int = PropertySpec(int)
    config : KeyValue = PropertySpec(KeyValue, default=KeyValue())
    hosts : List['Client'] = PropertySpec(['Client'], default=[])
    labels : List[str] = PropertySpec([str], default=[])
    description : str = PropertySpec(str)

    def __init__(self, *args, **kwargs):
        super(ConfigProfile, self).__init__(*args, **kwargs)

    def apply(self, hosts: List['Client']):
        result = self._do_op()
        # Apply can "steal" hosts from other CPs, so all their hosts need refresh
        # Making hosts transient created way too many fetches. OTOH, this could be optimized, but not now
        ConfigProfile.sdk_get()

def show_values(obj, prop):
    print(f'{prop.upper()}: ' + ', '.join([f'{s}: ({type(v)}) {v}' for s,v in obj.get_property_values(prop)]))

def main():
    import sys
    from xlro.core.util.cli_util import CLIArgumentParser, jsonify
    args = CLIArgumentParser(require_manager=True).parse_args()

    clients = args.manager.clients

    jcp = ConfigProfile.instance(name='joe-test', config={'a': True, 'b': False})
    try:
        jcp.create()
    except:
        pass

    profiles = ConfigProfile.sdk_get()

    def show_hosts():
        for cp in profiles:
            print(f'  {cp.name} -> {cp.hosts}')

    if True:
        # Test apply
        print('INIT'); show_hosts()
        defcp = ConfigProfile.instance(name='Cluster Default')
        defcp.apply(hosts=clients)
        print('POST-RESET'); show_hosts()
        jcp.apply(hosts=defcp.hosts)
        print('POST-STEAL'); show_hosts()
        # reset all to default
        defcp.apply(hosts=clients)
        jcp.delete()
        sys.exit()

    # print(f'LABELS: {jcp.get_property_values("labels")}')
    # jcp.labels = ['baz', 'bar', 'xyzzy']
    # print("LOCAL CHANGE")
    # print(f'LABELS: {jcp.get_property_values("labels")}')
    # print("PRE-UPDATE")
    # print(jsonify(jcp.to_dict(skip='mgmt')))
    # jcp.update()
    # print("POST-UPDATE")
    # print(f'LABELS: {jcp.get_property_values("labels")}')
    # jcp.get_self()
    # print("POST-REGET")
    # print(f'LABELS: {jcp.get_property_values("labels")}')
    # print(jsonify(jcp.to_dict(skip='mgmt')))
    print('PRE')
    show_values(jcp, 'config')
    jcp.config = [('joe', 1), ('deb', 2)]
    print('CREATED')
    show_values(jcp, 'config')
    jcp.update()
    print('UPDATED')
    show_values(jcp, 'config')
    jcp.config = {'foo': 'bar'}
    jcp.update()
    print('RESET')
    show_values(jcp, 'config')
    jcp.config = ('baz', 1)
    jcp.update()
    print('ADDED')
    show_values(jcp, 'config')
    jcp.get_self()
    print('FETCHED')
    show_values(jcp, 'config')
    jcp.delete()


if __name__ == '__main__':
    main()
