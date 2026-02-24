#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
print('get-stubs now obsolete. Will be removed when self-test updated.')
sys.exit(0)

import typing # pylint: disable=unused-import
import os
import glob
import re
import shutil
import json
import tempfile
import os.path as path
import inspect
from collections import defaultdict, Mapping, Sequence
import xlro.core.entities


def typingname(ptype: typing.Any) -> str:
    if isinstance(ptype, str):
        return repr(ptype)
    if isinstance(ptype, type):
        return repr(ptype.__name__) if  issubclass(ptype, xlro.core.entities.BaseEntity) else ptype.__name__
    if isinstance(ptype, Mapping):
        return 'Dict[str, {}]'.format(typingname(list(ptype.values())[0]))
    if isinstance(ptype, Sequence):
        return 'List[{}]'.format(typingname(ptype[0]))
    raise Exception('unknkown ptype: {} {}'.format(type(ptype), ptype))

# Make a map of modules to entities
module_map: typing.Dict[str, typing.Dict] = defaultdict(dict)
xlro_path = path.abspath(path.dirname(xlro.core.entities.__file__))
for entity in list(xlro.core.entities.BaseEntity.ENTITY_REGISTRY.values()):
    entity_map: typing.Dict[str, typing.Dict] = {}
    # if not entity._xlro_props:
        # continue
    property_map: typing.Dict[str, str] = {}
    module_map[entity.__module__.rpartition('entities.')[2]][entity.__name__] = property_map
    for propname, proptype in entity._xlro_props.items():
        property_map[propname] = typingname(proptype.ptype)

# print 'MODULE_MAP:', json.dumps(module_map, indent=2)
# sys.exit(0)

tmpd = tempfile.mkdtemp(prefix='pyi-tmp-')
for module, entity_map in module_map.items():
    pyi = module + '.pyi'
    outpyi = path.join(xlro_path, pyi)
    os.system('rm -f {} && cd {} && stubgen -o {} --include-private --no-import --py2 {}.py >/dev/null'.format(outpyi, xlro_path, tmpd, module))
    inpyi = glob.glob(tmpd + '/**/' + pyi, recursive=True)[0]
    with open(inpyi, 'r') as in_fp, open(outpyi, 'w') as out_fp:
        out_fp.write('''
from xlro.core.entities import {}
from uuid import UUID
from future.types.newstr import newstr
from xlro.core.util.config_file import *
'''.format(', '.join([e for m, e_list in module_map.items() if m != module for e in e_list])))
        prev_indent = None
        for line in in_fp:
            stripped = line.strip()
            if '(UnknownEntity)' in stripped:
                # This is our way to create unknown entities - but can confuse mypy because of double define
                continue
            if stripped.startswith('from typing'):
                if '*' not in stripped:
                    stripped += ', Dict, List'
                line = stripped + '\n' #+ '\nfrom xlro.core.entities import *\nfrom uuid import UUID\nfrom xlro.core.util.config_file import *\n'
            elif stripped.startswith('__metaclass__'):
                continue
            elif line.startswith('class'):
                active_class = stripped.partition('(')[0].partition(' ')[2]
                property_map = entity_map.get(active_class, {})
                # print 'Class:', active_class, 'Property Map:', json.dumps(property_map, indent=2)
            elif stripped.endswith(': Any = ...'):
                prop = stripped.partition(':')[0]
                typestring = property_map.get(prop, 'Any')
                line = line.partition(':')[0] + ' : ' + typestring + ' = ...\n'    
            elif stripped.startswith('@abc.abstract'):
                # Some bug in mypy?
                line = line.replace('abc.', '')
            elif 'namedtuple' in stripped:
                if 'import' in stripped:
                    line += 'from typing import NamedTuple\n'
                else:
                    # special fix for namedtuple()
                    nt_pattern = r'(?P<attr>\w+) *= *namedtuple *\( *(?P<clsname>\'\w+\')\ *, *\[ *(?P<rawfields>.*[^ ]) *]'
                    try:
                        nt_match = re.match(nt_pattern, line)
                        nt_info = nt_match.groupdict() if nt_match else {}
                        nt_fields = []
                        for field in [field.strip(" '") for field in nt_info['rawfields'].split(',')]:
                            field_types = { 'addr': 'int', 'ptype': 'Any' }
                            f_type = field_types.get(field, 'bool')
                            if field == active_class.lower():
                                f_type = active_class
                            nt_fields.append("('{}', '{}')".format(field, f_type))
                        line = '{indent}{attr} = NamedTuple({clsname}, [{fields}])\n'.format(indent=prev_indent,
                                        fields=', '.join(nt_fields), **nt_info)
                    except:
                        pass
            if stripped:
                prev_indent = line.partition(stripped[0])[0]
            out_fp.write(line)
    # for entity, property_map in entity_map.iteritems():
# print 'LEAVING TEMP IN:', tmpd
shutil.rmtree(tmpd)

#print json.dumps(module_map, indent=2)
