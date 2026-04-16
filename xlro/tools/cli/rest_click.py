# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations
from typing import get_type_hints, _GenericAlias
import sys
import logging
import click
from time import perf_counter
import inspect
import collections
import traceback
import re
import os
from copy import copy

import yaml
import json
from jinja2 import Template
from threading import Lock
from functools import partial, wraps
from typing import Dict, List, Callable, Optional, Tuple, Iterable, Type, Union
from enum import EnumMeta
from humanfriendly.tables import format_smart_table, format_pretty_table, format_robust_table
import xlro.core.entities as entities
from xlro.core.entities import BaseEntity, SDKEntity, Manager, SourceTypes, HostPort, User, Client
from xlro.core.entities.base import PropertySpec, SourceTypes, Property, LoaderStatus
from xlro.core.entities.sdk_base import SdkObject, SdkException, MongoComparison, MongoObj, RE, sdk_entity, RestException
from xlro.core.sdk.Utils import Utils
from xlro.core.sdk.ConnectionManager import Connection as RESTConnection
from xlro.core.entities.rest_info import RestVersionManager, RestVersionInfo, RestEntityInfo
from xlro.tools.cli.common import prompt, confirm, success_msg, failure_msg, color_msg, warn_msg, FieldNotAvailable
from dataclasses import dataclass, field
from xlro.core.util.general_utils import wait_for_property_values, WaitResult
from xlro.core.util.dict_util import expand_dots
from xlro.core.util.creds import local_settings_dir
import xlro.core.entities.etypes as etypes
from xlro.core.util.common import get_path
from xlro.core.util.cli_util import str2entity, raw_to_snake, snake_to_human, hyphenated, jsonify
from xlro.core.util.general_utils import mask_hidden_fields
from xlro.tools.cli.common import GET_LIMIT, OUTPUT_FORMATS, click_env
from xlro.core import infra_conf

_logger = logging.getLogger('rest-click')
_exit_code = 0

def set_exit_code(code):
    global _exit_code
    _exit_code = code

def exit_code():
    return _exit_code

def flatten(lst):
    for item in lst:
        if isinstance(item, Iterable) and not isinstance(item, str):
            yield from flatten(item)
        else:
            yield item

_cli_templates: List[Dict] = []
def load_templates():
    # Lazy load user templates and default templates
    if not _cli_templates:
        default_paths = [ os.path.join(local_settings_dir(), 'templates'), os.path.join(get_path(os.path.dirname(__file__)), 'default_templates.yaml')]
        env_path = os.environ.get('NVMESH_TEMPLATES')
        paths = (env_path.split(':') if env_path else []) + default_paths
        for path in paths:
            try:
                with open(os.path.expanduser(path), 'r') as fp:
                    _cli_templates.append(yaml.safe_load(fp))
            except Exception as e:
                _logger.info(f'Failed to load template file "{path}": {repr(e)}')
    return _cli_templates

def load_template(entity_name, template_name):
    template_sets = load_templates()
    for templates in template_sets:
        try:
            return templates[entity_name][template_name]
        except:
            pass
    raise Exception(f'Failed to load template {template_name} for {entity_name}')

def template_complete(ctx: click.Context, args, incomplete: str, entity_name: str):
    template_sets = load_templates()
    matches = []
    for templates in template_sets:
        for tname in templates[entity_name]:
            if tname.startswith(incomplete):
                matches.append(tname)
    return matches

@dataclass
class RestContext:
    name: str
    rest_info: RestEntityInfo
    entity: SDKEntity
    name_index: Dict[str,str] = field(default_factory=dict)
    subobj_index: Dict[str, type] = field(default_factory=dict)
    human_fields: List[str] = field(default_factory=list)
    manager: entities.Manager = None
    orig_obj: Dict = None

def auto_complete_choices(ctx: click.Context, args, incomplete: str, choices: List[str]):
    ilower = incomplete.lower()
    return sorted([c for c in choices if c.lower().startswith(ilower)])

def auto_complete_multi_choices(ctx: click.Context, args, incomplete: str, choices: List[str], sep=','):
    pre, _, incomplete = incomplete.rpartition(sep)
    matches = auto_complete_choices(ctx, args, incomplete, choices)
    if not pre:
        return matches
    chosen = set([d.lower() for d in pre.split(sep)])
    return [f'{pre},{match}' for match in matches if match.lower() not in chosen]

def auto_complete_id(ctx: click.Context, args, incomplete: str, entity: SDKEntity=None):
    try:
        _, rest_ctx = get_rest_context(ctx)
        if entity:
            key = entity.cls_rest_info(rest_ctx.manager).dbkey
        else:
            entity = rest_ctx.entity
            key = rest_ctx.rest_info.dbkey
        headline_map = entity.get_headlines(count=20, query={key: {"regex": '^' + incomplete + '.*'}})
        return sorted([ent.rest_id for ent in headline_map.values()])
    except Exception as e:
        _logger.debug(f'Auto-complete() error: {repr(e)}')
    return []

def auto_complete_ids(ctx: click.Context, args, incomplete: str, entity: SDKEntity=None):
    # TODO: This is prep for supporting comma separated lists for multi options
    pre, sep, last = incomplete.rpartition(',')
    return [f'{pre}{sep}{choice}' for choice in auto_complete_id(ctx, args, last, entity)]

def e_auto_complete_ids(entity: type, is_multi: bool, *args, **kwargs):
    return auto_complete_ids(*args, **kwargs, entity=entity) if is_multi else auto_complete_id(*args, **kwargs, entity=entity)

def get_rest_cmd_path(ctx=None) -> str:
    path = ''
    c = ctx or click.get_current_context()
    while c:
        try:
            path = c.command.name + ' ' + path
        except:
            path = str(c.command) + ' ' + path
        if isinstance(c.command, RestGroup):
            break
        c = c.parent
    return path

def get_rest_context(ctx=None) -> (click.Context, RestContext):
    c = ctx or click.get_current_context()
    while c:
        if isinstance(c.command, RestGroup):
            return c, c.command.rest_context
        c = c.parent
    raise Exception(f'Invalid context for command: {ctx.command.name}')

def expand_braces(s: str) -> List[str]:
    ''' Implement shell like brace-expansion - e.g, 'v-{a,b}-{1..3}' -> ['v-a-1', 'v-a-2', 'v-a-3', 'v-b-1', 'v-b-2', 'v-b-3'] '''
    braces = re.match(r'([^{]*){([^}]+)}(.*)', s)
    if braces is None or not braces.groups()[1]:
        return [s]
    nrange = re.match(r'(\d+)\.\.(\d+)', braces.groups()[1])
    if nrange:
        vals = [str(n) for n in range(int(nrange.groups()[0]), int(nrange.groups()[1])+1)]
    else:
        vals = braces.groups()[1].split(',')
    pre = braces.groups()[0]
    return [pre + val + post for val in vals for post in expand_braces(braces.groups()[2])]

def expand_multi(multi: str) -> List[str]:
    s = set()
    for v in expand_braces(multi):
        s.update([part for part in v.split(',') if part])
    return sorted(list(s))

def rest_callback(f):
    ''' Decorator to safely wrap rest callbacks '''
    @wraps(f)
    def rest_cb(*args, **kwargs):
        global _exit_code
        try:
            # _, rest_obj = get_rest_context()
            ctx = click.get_current_context()
            rest_obj = ctx.find_object(RestContext)
            if not rest_obj.manager:
                raise Exception('No manager connected.  Use: ' + ('-m/--management' if rest_obj.orig_obj.get('cmd-line') else 'login host[:port]'))
            rest_info = rest_obj.rest_info

            # Track all entity params (kludgey :-( )
            e_params = set()
            # Key entities do not need to exist for *create* and will be checked individually for show or delete
            # TODO: This shouldn't be here, should be arg to rest_callback.
            missing_keys_ok = ctx.command.name in ('create', 'wait', 'show', 'snapshot-create-and-attach')
            k_params = set() # Key entities (not checked on *create* or show or delete)
            keyprop = hyphenated(rest_info.rest2infra.get(rest_info.dbkey, rest_info.dbkey))
            keyprop = 'id' if keyprop.endswith('-id') else keyprop

            def add_e_params(value):
                if isinstance(value, Iterable) and not isinstance(value, str):
                    list(map(add_e_params, value))
                elif isinstance(value, SDKEntity):
                    e_params.add(value)

            # Flatten multi values
            param_map = {p.name: p for p in ctx.command.params}
            for name, value in kwargs.items():
                param = param_map.get(name)
                if not param:
                    continue
                if param.multiple:
                    # We'll get () if no value on command-line, and ([],) if someone did --option []
                    if value == ():
                        kwargs[name] = value
                    else:
                        flattened = []
                        for v in value:
                            if isinstance(v, str):
                                flattened.extend(expand_multi(v))
                            elif isinstance(v, list):
                                flattened.extend(v)
                            else:
                                flattened.append(v)
                        kwargs[name] = flattened
                elif isinstance(param.type, EntityParamType) and isinstance(value, list):
                    kwargs[name] = value[0]

                # Gather all Entity param values for OOB and existence checks
                actual = kwargs[name]
                if name == 'dbkeys' or name == 'name' or name == keyprop:
                    instances = [ctx.obj.entity.instance(**{keyprop:k}) \
                            for k in ([actual] if not isinstance(actual, list) else actual) if k]
                    add_e_params(instances)
                    if missing_keys_ok:
                        k_params.update(instances)
                else:
                    add_e_params(actual)

            _logger.debug(f'rest_callback for {get_rest_cmd_path()}: e_params: {e_params} k_params: {k_params}')

            # Validate Entity args (can be any type) for existence and OOB changes
            cls2ent = collections.defaultdict(list)
            for e in e_params:
                cls2ent[e.__class__].append(e)
            oob_changes = []
            for etype, elist in cls2ent.items():
                oob_changes.extend(etype.any_uuid_changed(elist))
            # As the above forces Mgmt load, we do existence check after. FETCHED_TS shows we got it from Mgmt
            for e in (e_params-k_params):
                if not SDKEntity.FETCHED_TS in e and not e.get_self(optional=True):
                    raise Exception(f'{e} does not exist')
            if oob_changes:
                _logger.info(f'OOB Changes detected: {oob_changes}')
                oob_warning = 'WARNING: The following objects have been recreated outside the scope of this session:'
                sep = '\n\t' if click_env('human') else ' '
                for oob in oob_changes:
                    oob_warning += f'{oob.__class__.__name__}: {oob.rest_id}'
                warn_msg(oob_warning)
                if not confirm('Are you sure you want to continue?', default=False):
                    _logger.debug(f'Operation cancelled. cmd={get_rest_cmd_path()} args={args}, kwargs={kwargs}')
                    raise Exception('Operation cancelled. Not confirmed.')
                # Only clear the change flags if the user confirmed.  Otherwise, safer to force them to re-confirm later
                for oob in oob_changes:
                    oob.clear_uuid_change()

            _logger.debug(f'CLI-Execute cmd={get_rest_cmd_path()} args={args}, kwargs={mask_hidden_fields(kwargs)}')
            t_start = perf_counter()
            rest_count = RESTConnection.rest_count
            response = f(*args, **kwargs)
            t_elapsed = perf_counter() - t_start
            rest_count = RESTConnection.rest_count - rest_count
            _logger.debug(f'CLI-Execute cmd={get_rest_cmd_path()} response={response}, elapsed={t_elapsed}, ops={rest_count}')
            if click_env('timer'):
                color_msg(f'Elapsed: {t_elapsed:.3f}s, REST ops: {rest_count}', True, 'blue')
            if response != False:
                _exit_code = 0
                return response
        except click.Abort:
            pass
        except Exception as e:
            msg = RestGroup.format_failure(e)
            _logger.debug(f'callback={f}. msg={msg}. exception={repr(e)}')
            msg = re.sub(':Manager:[^]]*\]', '', msg)
            failure_msg(msg)

        if click_env('fail'):
            _logger.debug('Exit on failure.')
            sys.exit(1)
        _exit_code = 1
        return None

    return rest_cb

class MappingParamType(click.ParamType):
    name = 'KEY=VALUE'

    def convert(self, value, param, ctx):
        key, _, v_str = value.partition('=')
        try:
            val = int(v_str)
        except:
            try:
                val = float(v_str)
            except:
                if v_str in (None, ''):
                    val = ''
                elif v_str.lower() == 'true':
                    val = True
                elif v_str.lower() == 'false':
                    val = False
                else:
                    val = v_str
        return (key, val)

class EntityParamType(click.ParamType):
    name = 'ID/NAME'

    def __init__(self, ptype: BaseEntity):
        self.ptype = ptype

    def __str__(self):
        return f'EntityParamType({self.ptype})'

    def convert(self, value, param, ctx):
        # Always treat as multi.  It will be handled in rest_callback, where we know if it's meant to be multi
        try:
            if value == '':
                result = []
            elif isinstance(value, self.ptype):
                result = [value]
            elif isinstance(value, list):
                result = value
            elif ',' in value or '{' in value:
                result = []
                for v in expand_multi(value):
                    result.extend(self.convert(v, param, ctx))
            elif any(c in value for c in Utils.RE_CHARS):
                try:
                    anchored = Utils.anchor_regex(value)
                    filter_obj = MongoObj(RestVersionManager.version_info(Manager.get_manager().api_version).entities[self.ptype.__name__].dbkey, {"$regex": anchored})
                    result = self.ptype.sdk_get(filter_mongo_objs=[filter_obj])
                except Exception as e:
                    result = []
                assert result, 'Invalid or unmatched regex'
            else:
                try:
                    result = [str2entity(value, self.ptype)]
                except:
                    assert False, f'Failed to understand: "{value}"'
            _logger.debug(f'EntityParam:convert(): {value} -> [#{len(result)}]')
            return result
        except (ValueError, AssertionError) as e:
            _logger.info(f'Conversion error: "{value}" - {repr(e)}')
            message = f'{value} - {e}' if isinstance(e, AssertionError) else value
            self.fail(message, param, ctx)

class EntityNameParamType(EntityParamType):
    def convert(self, value, param, ctx):
        return [v.rest_id if isinstance(v, BaseEntity) else v  for v in super().convert(value, param, ctx)]


class EntityOption(click.Option):
    def __init__(self, names, name: str = None, entity: RE = None, multiple=False, **kwargs):
        if not names:
            rest_info = RestVersionManager.version_info(RestVersionManager.max_version()).entities[entity.__name__]
            dbkey = rest_info.dbkey
            keyprop = hyphenated(rest_info.rest2infra.get(dbkey, dbkey))
            keyprop = 'id' if keyprop.endswith('-id') else keyprop
            names = [name or 'name', '--' + keyprop, '-' + keyprop[0]]
        return super(EntityOption, self).__init__(names, type=EntityParamType(entity), multiple=multiple, autocompletion=partial(e_auto_complete_ids, entity, is_multi=multiple), **kwargs)

_load_lock = Lock()
_cli_defs: Dict = {}
def cli_defs():
    global _cli_defs, _load_lock
    if not _cli_defs:
        with _load_lock:
            if not _cli_defs:
                from xlro.core.util.read_dict import read_dict
                _cli_defs = read_dict(os.path.join(get_path(os.path.dirname(__file__)), 'cli.yaml'))
    return _cli_defs

class AliasedOption(click.Option):
    ''' A click.Option which looks up alternate aliases for option name or values '''
    orig_callback: Optional[Callable] = None
    value_map: Optional[Dict] = None

    @property
    def opt_aliases(self) -> Dict:
        aliases = cli_defs().get('aliases')
        if not aliases:
            return {}
        return aliases.get('options', {})

    @property
    def value_aliases(self):
        aliases = cli_defs().get('aliases')
        if not aliases:
            return {}
        return aliases.get('values', {})

    def map_value(self, ctx, param, value):
        _logger.debug(f'OptionCallback: param={param}, value={value}')
        newvalue = self.value_map.get(value, value) if self.value_map else value
        return newvalue if self.orig_callback is None else self.orig_callback(ctx, param, newvalue)

    def __init__(self, opts: List, callback=None, **kwargs):
        opt = opts[-1].strip('-')
        opt_aliases = self.opt_aliases.get(opt)
        if opt_aliases:
            opts = opts + [opt.strip() for opt in opt_aliases.split(',')]
        self.value_map = self.value_aliases.get(opt)
        if self.value_map:
            self.orig_callback = callback
            callback = self.map_value
        super(AliasedOption, self).__init__(opts, callback=callback, **kwargs)

    def __str__(self):
        return f'AliasedOption({"?" if self.is_flag else ("*" if self.multiple else "")} {self.type})'

@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class SimpleEntity(SDKEntity):
    ''' An "ad-hoc" entity which will be created on the fly if name in rest.yaml doesn't match existing entity '''
    key_prop: str = PropertySpec(str, key=True)


class RestGroup(click.Group):
    rest_context: Optional[RestContext] = None

    def __init__(self, name: str, rest_info: RestEntityInfo, mgr: Optional['Manager'] = None, **kwargs):
        self.mgr = mgr
        try:
            entity = getattr(entities, name, BaseEntity.ENTITY_REGISTRY[name])
        except:
            # If no existing entity, try and create a pseudo entity
            key = rest_info.dbkey or '_id'
            entity = type(name, (SimpleEntity,), {})
            # Swap the "key_prop" property with the dbkey name in rest.yaml, and register new entity
            entity._xlro_props = entity._xlro_props.copy()
            entity._xlro_props[key] = entity._xlro_props.pop('key_prop')
            entity._xlro_keyprops = [key, 'mgmt']
            BaseEntity.ENTITY_REGISTRY[name] = entity

        assert issubclass(entity, SDKEntity), f'Invalid SDKEntity: {name}'
        rest_ctx = RestContext(name, rest_info, entity)
        self._build_name_index(rest_ctx, rest_info)
        self.rest_context = rest_ctx
        cmd_name = hyphenated(name)
        super(RestGroup, self).__init__(cmd_name, callback=lambda: self.set_context(rest_ctx), **kwargs)
        for cmdname, cmd in inspect.getmembers(self.__class__, predicate=lambda v: isinstance(v, click.Command)):
            self.add_command(cmd)
        try:
            self.set_update_create(rest_ctx, entity)
        except Exception as e:
            pass

        dbkey = rest_info.dbkey or 'name'
        keyprop = hyphenated(rest_info.rest2infra.get(dbkey, dbkey))
        keyprop = 'id' if keyprop.endswith('-id') else keyprop
        if 'create' in self.commands:
            # Only delete what we can create.
            self.add_command(click.Command(name='delete', callback=self.do_delete, help='Delete a specified instance',
                params=[click.Option(['name', f'-{keyprop[0]}', f'--{keyprop}'],
                type=EntityNameParamType(entity), required=True, multiple=True,
                autocompletion=partial(e_auto_complete_ids, entity, is_multi=True))]))

        if 'show' not in self.commands:
            # Add generic Show command.  Couldn't use shared because of difference in key name (name vs. id)
            self.add_command(click.Command(name='show', callback=self.do_show, help=f'Show instances',
                params=[
                    click.Option(['name', f'-{keyprop[0]}', f'--{keyprop}'], multiple=True,
                        autocompletion=partial(e_auto_complete_ids, entity, is_multi=True),
                        help="Show specific instance(s) (default is show all)"),
                    click.Option(['-o', '--output-format'], type=click.Choice(OUTPUT_FORMATS)),
                    click.Option(['-l', '--limit'], type=int, help='Page limit (0 is unlimited)'),
                    click.Option(['-s', '--skip'], type=int, default=0),
                    click.Option(['-1', '--onepage'], type=bool, default=False, is_flag=True, help='Print one page and stop - no prompt'),
                    click.Option(['-f', '--fields'],
                        autocompletion=partial(auto_complete_multi_choices, choices=rest_ctx.human_fields),
                        help='Comma seperated fields to show (besides ID/Name) - case insensitive, use "-" for space'),
                ]))

        waitable = ('client', 'target', 'drive', 'volume', 'cdv', 'tpv')
        if 'wait' not in self.commands and name.lower() in waitable:
            # Add generic Wait command.
            self.add_command(click.Command(name='wait', callback=self.do_wait, help='Wait for a property to reach a desired value',
                params=[
                    click.Option(['name', f'-{keyprop[0]}', f'--{keyprop}'], multiple=True, required=True,
                        autocompletion=partial(e_auto_complete_ids, entity, is_multi=True)),
                    click.Option(['-p', '--property'], required=True, help='Property to wait for'),
                    click.Option(['-v', '--value'], required=True, multiple=True, help='Value to wait for (multiple allowed)'),
                    click.Option(['-b', '--boolean'], type=bool, default=False, is_flag=True, help='Value is true/false'),
                    click.Option(['-m', '--missing'], help='Default value if actual missing'),
                    click.Option(['--poll'], type=int, default=1, help='Seconds to wait between polling'),
                    click.Option(['--timeout'], type=int, default=60, help='Seconds to wait before timeout'),
                ]))

        # Add non-CRUD operations, if any
        for op, op_info in rest_info.kwargs.get('ops', {}).items():
            _logger.debug(f'REST op: {name}.{op}')
            if op in self.commands:
                # CRUD commands are already handled.  But we still still some options (wait, confirm).
                continue
            try:
                # Build parameters. Legacy CLI only uses options, so we'll do that.
                params = rest_info.kwargs.get('ops', {}).get(op, {}).get('params')
                op_params: List[click.Parameter] = self._params_from_entity(rest_ctx, entity, params) if params else []
                style = op_info.get('style', 'one')
                if style != 'none':
                    try:
                        key = rest_info.dbkey or 'name'
                        keytype = entity._property_spec(rest_info.rest2infra.get(key, key)).ptype
                    except:
                        keytype = str
                    keytype = EntityNameParamType(entity)
                    is_multi = True # we handle in wrapper to invoke multiple times if REST is style == 'one'
                    keyprop = hyphenated(rest_info.dbkey)
                    keyprop = 'id' if keyprop.endswith('-id') else keyprop
                    op_params = [p for p in op_params if p.name != keyprop]
                    op_params.append(AliasedOption(['dbkeys', '--' + keyprop],
                            type=keytype, required=True, multiple=is_multi,
                            autocompletion=partial(e_auto_complete_ids, entity, is_multi=is_multi)))
                for opt, opt_info in op_info.get('opts', {}).items():
                    # Get type from string
                    # TODO: handle more than built-in types, especially flags, choices or Entities
                    # TODO: Use _add_params here to handle more types and nesting
                    auto_complete = None
                    is_multi = opt_info.get('multi', False)
                    opt_type_name = opt_info.get('type', 'str')
                    ent_type = getattr(entities, opt_type_name, None)
                    if ent_type:
                        auto_complete = partial(e_auto_complete_ids, ent_type, is_multi=is_multi)
                        opt_type = EntityParamType(ent_type)
                    else:
                        opt_type = __builtins__.get(opt_type_name) or getattr(etypes, opt_type_name, str)
                        opt_data = None
                        if issubclass(opt_type, etypes.ChoiceStr):
                            opt_data = opt_type
                        elif opt_info.get('choices') or opt_info.get('consts_key'):
                            opt_data = opt_info
                        if opt_data:
                            auto_complete = partial(auto_complete_choices, choices=self.get_choices_by_type(opt_data, op))

                    help_str = snake_to_human(raw_to_snake(opt))
                    if auto_complete and auto_complete.keywords.get('choices'):
                        help_str += f' (options: {auto_complete.keywords["choices"]})'

                    is_flag = opt_type == bool
                    op_params.append(AliasedOption(['--' + hyphenated(opt)],
                        type=opt_type, required=opt_info.get('required', False), multiple=opt_info.get('multi', False),
                        autocompletion=auto_complete, help=help_str,
                        is_flag=is_flag, default=opt_info.get('default', False if is_flag else None)))

                self.add_command(click.Command(hyphenated(op), params=op_params, help=op_info.get('help'),
                            callback=partial(self.do_operation, op_name=op, op_info=op_info)))
            except Exception as e:
                _logger.warning(f'REST operation: "{name}.{op}" could not be processed. {repr(e)}')
                _logger.debug(traceback.format_exc())

        # Loop to handle special options: Wait and Confirm.  Applies even to CRUD ops
        for op, op_info in rest_info.kwargs.get('ops', {}).items():
            cmd = self.commands.get(op)
            if not cmd or not isinstance(cmd, click.Command) or not cmd.callback:
                continue
            wait_params = op_info.get('wait')
            if wait_params:
                def wait_wrapper(wait_params=wait_params, entity=entity, callback=cmd.callback,
                        op=op, wait=False, timeout=60, **kwargs):
                    wait_params = wait_params.copy() # Because we pop()
                    ctx = click.get_current_context()
                    affected = callback(**kwargs)
                    if wait and affected and isinstance(affected, Iterable):
                        affected_objs: List[SDKEntity] = []
                        key_template_str = wait_params.pop('obj-key', None)
                        _logger.debug(f'key-template: {key_template_str}')
                        if key_template_str:
                            _logger.debug(f'params: {ctx.params}')
                            _logger.debug(f'results: {affected}')
                        key_template = None if not key_template_str else Template(key_template_str)
                        for result in affected:
                            if isinstance(result, SDKEntity):
                                affected_objs.append(result)
                            elif isinstance(result, dict):
                                if not result.get('success', True): # Not sure what to do if dict has no 'success' value. Letting it through.
                                    _logger.debug(f'Not waiting for result: {result}')
                                    continue
                                key = key_template.render(result=result, params=ctx.params) if key_template else result['_id']
                                _logger.debug(f'key: {key}')
                                affected_objs.append(str2entity(key, cls=BaseEntity if key_template else entity))
                        if affected_objs:
                            _logger.debug(f'Waiting for affected: {affected_objs}')
                            click.echo('Waiting...')
                            if op in ('attach', 'detach'):
                                # TODO: refactor this after 3.2.0 - NVMESH-4846
                                c2v = {affected_objs[0].client: [a.volume for a in affected_objs]}
                                ref_id = None if not ctx.params else ctx.params.get('reference_id', None)
                                wait_result= Client.bulk_wait_for_attachment_values(c2v,
                                        strip_hidden=True, reference_id=ref_id, timeout=timeout,
                                        source=SourceTypes.MANAGEMENT, **wait_params) # type: ignore[arg-type]
                            else:
                                wait_result = wait_for_property_values(affected_objs, timeout=timeout,
                                        source=SourceTypes.MANAGEMENT, **wait_params) # type: ignore[arg-type]

                            if not wait_result:
                                global _exit_code
                                _exit_code = 1
                                failure_msg('Timed out')
                                return False
                            success_msg('OK')
                    return affected
                cmd.callback = wait_wrapper
                cmd.params.append(click.Option(['--wait'], type=bool, is_flag=True, default=False, help="Wait for operation to take effect"))
                cmd.params.append(click.Option(['--timeout'], type=int, default=60, help="Seconds to wait (default 60)"))

            if op_info.get('confirm') or op_info.get('pre-msg') or op_info.get('post-msg'):
                cmd.params.append(click.Option(['-y', '--yes'], type=bool, default=False, is_flag=True, help='Auto-confirm the operation'))

                def confirm_wrapper(op=cmd.name, op_info=op_info, callback=cmd.callback, yes=False, **kwargs):
                    # TODO: do we need messages to be templates?
                    pre, conf, post = ((s.strip() if s else s) for s in (op_info.get(k) for k in ['pre-msg', 'confirm', 'post-msg']))
                    if pre:
                        click.echo(pre)
                    if conf and not yes and not click_env('yes') and not confirm(conf, default=False):
                        failure_msg('Operation cancelled.')
                        click.get_current_context().abort()
                    ret = callback(**kwargs)
                    if post:
                        click.echo(post)
                    return ret
                cmd.callback = confirm_wrapper

            all_info = op_info.get('all')
            if all_info:
                param, propname = tuple(all_info.items())[0]
                param = next((p for p in cmd.params if p.name == param))
                param.required = False
                cmd.params.append(click.Option(['-a', '--all'], type=bool, default=False, is_flag=True, help=f'Preform {cmd} on all {propname}'))

    def get_choices_by_type(self, opt: Union[Dict, Type[etypes.ChoiceStr]], opt_name: Optional[str] = None):
        choices = opt.get('choices', []) if isinstance(opt, dict) else getattr(opt, 'choices', [])
        consts_key = opt.get('consts_key') if isinstance(opt, dict) else getattr(opt, 'consts_key', None)
        try:
            mgr = self.mgr or self.get_mgr()
            if consts_key and mgr.connection:
                options_by_key = mgr.api_consts[consts_key]
                if isinstance(options_by_key, dict):
                    choices = list(options_by_key.values())
                else:
                    choices = options_by_key
        except RuntimeError:
            # still no manager in context
            pass

        opt_mapping = cli_defs()['aliases']['values'].get(opt_name)
        if opt_mapping:
            choices = [k for k, v in opt_mapping.items() if v in choices]

        return choices

    def _build_name_index(self, rest_ctx: RestContext, rest_info: RestEntityInfo):
        human_fields: List[str] = []
        rest_args = rest_info.kwargs
        for prop in flatten(rest_args.get('params', []) + rest_args.get('display', []) + rest_args.get('extra_display', [])):
            snake = raw_to_snake(prop)
            rest_ctx.name_index[snake] = prop
            human_fields.append(snake_to_human(snake).replace(' ', '-'))
        rest_ctx.human_fields = list(set(human_fields))

    def _add_param(self, params, rest_ctx, prop_name, ptype, required=False, default=None, path=[]):
        dotname = '.'.join(path + [prop_name])
        if isinstance(ptype, collections.abc.Mapping) or isinstance(ptype, type) and issubclass(ptype, collections.abc.Mapping):
            is_multi = True
            ptype = MappingParamType()
        else:
            is_multi = isinstance(ptype, Iterable) and not isinstance(ptype, (str, EnumMeta))
            ptype = next(iter(ptype)) if is_multi else ptype
        if isinstance(ptype, type) and issubclass(ptype, SdkObject):
            # Handle nested objects...
            _logger.debug(f'Nested object params: {rest_ctx.entity.__name__}.{prop_name} ({ptype})')
            rest_ctx.subobj_index[dotname] = ptype
            for prop, subptype in ptype._get_specs().items():
                self._add_param(params, rest_ctx, prop_name=prop, ptype=subptype, required=False,
                        default=getattr(ptype, prop, None), path=path + [prop_name])
            return

        snake_name = raw_to_snake('_'.join(path + [prop_name]))
        opt_name = snake_name.replace('_', '-')
        # '-' sep is the convention for the options, but the param name will be snake.
        rest_ctx.name_index[snake_name] = dotname
        if isinstance(ptype, str):
            try:
                ptype = getattr(entities, ptype)
            except:
                pass
        auto_complete = None
        if isinstance(ptype, type):
            if issubclass(ptype, etypes.ChoiceStr):
                auto_complete = partial(auto_complete_choices, choices=self.get_choices_by_type(ptype, opt_name))
            elif issubclass(ptype, BaseEntity):
                if issubclass(ptype, SDKEntity):
                    auto_complete = partial(e_auto_complete_ids, ptype, is_multi=is_multi)
                ptype = EntityParamType(ptype)
        if isinstance(ptype, type) and issubclass(ptype, bool):
            opt_name = opt_name + '/--no-' + opt_name
            default = None # Let server decide default value, unless explicitly set
        help_str = snake_to_human(snake_name)
        if auto_complete and auto_complete.keywords.get('choices'):
            help_str += f' (options: {auto_complete.keywords["choices"]}'
        params.append(AliasedOption(['--' + opt_name],
                required=required,
                multiple=is_multi,
                default=default, # None especially needed to differentiate unset, especially for boolean's
                type=ptype if not isinstance(ptype, str) else str,
                autocompletion=auto_complete,
                help=help_str,
            ))


    def _params_from_entity(self, rest_ctx: RestContext, entity: SDKEntity, param_names=None):
        params: List[click.Parameter] = []
        infra_props = dict(entity._xlro_props.items())

        def flatten_params(x):
            return set(a for i in x for a in flatten_params(i)) if isinstance(x, Iterable) and not isinstance(x, str) else [x]

        props_spec = {p: infra_props[p] for p in flatten_params(param_names or rest_ctx.rest_info.kwargs['params']) if p in infra_props}
        for prop_name, spec in props_spec.items():
            # JW: Not really following why this still here if we're configuring the fields via rest_info
            if spec.transient or spec.readonly or prop_name == 'mgmt': # or isinstance(spec.ptype, Mapping):
                # Transient is read-only and we don't (yet?) handle Mappings
                continue
            self._add_param(params, rest_ctx, prop_name, spec.ptype, spec.required or spec.key, spec.default)
        return params

    def set_update_create(self, rest_ctx, entity):
        rest_info = rest_ctx.rest_info
        dbkey = rest_info.dbkey or 'name'
        keyprop = rest_info.rest2infra.get(dbkey, dbkey)

        # Generate create params from Entity
        params: List[click.Parameter] = []
        # Creation template, a'la volume_defs
        params.append(click.Option(['--cli-template'], type=str,
                autocompletion=partial(template_complete, entity_name=entity.__name__.lower()),
                help='Pull default values from a named template'))
        # Future proof if a new, as-yet unknown, property is added, it can still be passed
        params.append(click.Option(['--cli-property'],
                        multiple=True, type=str, help='Enable future properties as --extra-property foo=bar'))
        params += self._params_from_entity(rest_ctx, entity)
        # For create, enable multiple keys
        for p in params:
            if p.name == keyprop:
                p.multiple = True
                break

        ops = rest_info.kwargs.get('ops', {})
        additional_create_params = self._params_from_entity(rest_ctx, entity, ops['create'].get('params')) if ops.get('create') else []
        self.add_command(click.Command('create', params=params + additional_create_params, callback=self.do_create, help='Create a new instance'))

        update_params: List[click.Parameter] = []
        additional_update_params = self._params_from_entity(rest_ctx, entity, ops['update'].get('params')) if ops.get('update') else []

        # Update params differ: remove required and defaults, and add autocomplete to key
        for p in params + additional_update_params:
            p2 = copy(p)
            p2.default = None
            if p2.name == keyprop:
                p2.type = EntityNameParamType(entity)
                p2.autocompletion = partial(e_auto_complete_ids, entity, is_multi=True)
            else:
                p2.required = False
            update_params.append(p2)
            # If there's a key-value mapping, add a --delete-<param> option to delete keys by name
            try:
                if isinstance(p.type, MappingParamType):
                    opt_name = p.name.replace('_', '-')
                    del_opt = click.Option([f'--delete-{opt_name}'],
                            type=str, multiple=True, help=f'Delete key(s) from {opt_name}')
                    del_opt._delete_for = p.name  # tag linking back to the mapping param
                    update_params.append(del_opt)
                    _logger.debug(f'Added delete option for {opt_name}')
            except Exception as e:
                _logger.debug(f'Failed to add delete option for {p.name}: {e}')
        self.add_command(click.Command('update', params=update_params, callback=self.do_create, help='Update properties of an existing instance'))

    def get_mgr(self):
        return click.get_current_context().obj.get('manager-obj')

    def ents_by_name(self, obj, names, page=0, count=GET_LIMIT, routes=None, fields=None) -> List[SDKEntity]:
        dicts = self.dicts_by_name(obj, names, page, count, routes, fields)
        entities = [obj.entity._rdict_to_entity(d, obj.manager) for d in dicts]
        for e in entities:
            e._loaders_called[SourceTypes.MANAGEMENT][SDKEntity.load_from_mgmt_sdk] = LoaderStatus.PASSED
        return entities

    def dicts_by_name(self, obj, names, page=0, count=GET_LIMIT, routes=None, fields=None) -> List[dict]:
        sort_obj = [MongoObj('timestamp', '-1')] if obj.name == 'Log' else None
        # anchor patterns:
        names = [Utils.anchor_regex(n) if Utils.is_pattern(n) else n for n in names]
        if not fields:
            projection = None
        else:
            # TODO: solve issue of calculated properties mapping to projection to avoid refetch. E.g., target.nic_ids
            p2p = obj.rest_info.kwargs.get('prop2proj', {})
            dbkey = obj.rest_info.dbkey or 'name'
            keyprop = obj.rest_info.rest2infra.get(dbkey, dbkey)
            proj_fields = {p for f in fields + [keyprop] for p in p2p.get(f, obj.rest_info.infra2rest.get(f, f)).split(',')}
            if '*' in p2p:
                proj_fields.update(p2p['*'].split(','))
            _logger.debug(f'P2P mapping: {p2p}, Projection fields: {proj_fields}')
            projection = [MongoObj(pf, 1) for pf in proj_fields]
        if not names:
            filter_objs = obj.entity._get_filter(obj.manager)
            return list(obj.entity._sdk_get(page=page, count=count, mgmt=obj.manager, filter_mongo_objs=filter_objs, sort_mongo_objs=sort_obj, projection_mongo_objs=projection, routes=routes))
        # Multi-patterns must be joined (vs. names where we use "in (name1, name2)")
        if len(names) > 1 and any([Utils.is_pattern(n) for n in names]):
            # raise Exception('Only a single, stand-alone pattern is supported.')
            names = ['|'.join(names)]

        dbkey = obj.rest_info.dbkey

        # Check for type-casting (SQL objs have int keys)
        spec = obj.entity._property_spec(dbkey)
        if spec:
            try:
                names = [spec.ptype(n) for n in names]
            except Exception as e:
                _logger.info(f'Failed to cast "{dbkey}" = ({type(names)}){names} to {spec.ptype}')


        rest_dbkey = obj.rest_info.infra2rest.get(dbkey, dbkey)

        # Batch large name lists to avoid HTTP 431 (Request Header Fields Too Large), default to 50
        batch_size = infra_conf.root.general.max_rest_batch_size
        has_patterns = any(Utils.is_pattern(n) for n in names)

        if len(names) > batch_size and not has_patterns:
            if page > 0:
                _logger.debug(f'Batching mode: ignoring page={page}, returning empty (pagination not supported for batched requests)')
                return []
            total_batches = (len(names) + batch_size - 1) // batch_size
            _logger.debug(f'Batching {len(names)} names into batches of {batch_size}')
            results: List[dict] = []
            for i in range(0, len(names), batch_size):
                batch_num = (i // batch_size) + 1
                start_idx = i + 1
                end_idx = min(i + batch_size, len(names))
                _logger.debug(f'  Batch {batch_num}/{total_batches}: items {start_idx}-{end_idx}')
                batch_names = names[i:i + batch_size]
                filter_objs = obj.entity._get_filter(obj.manager, **{rest_dbkey: batch_names if len(batch_names) > 1 else batch_names[0]})
                batch_results = list(obj.entity._sdk_get(page=0, count=len(batch_names), mgmt=obj.manager,
                        filter_mongo_objs=filter_objs, sort_mongo_objs=sort_obj, projection_mongo_objs=projection, routes=routes))
                results.extend(batch_results)
        else:
            filter_objs = obj.entity._get_filter(obj.manager, **{rest_dbkey: list(names) if len(names) > 1 else names[0]})
            results: List[dict] = list(obj.entity._sdk_get(page=page, count=count, mgmt=obj.manager,
                    filter_mongo_objs=filter_objs, sort_mongo_objs=sort_obj, projection_mongo_objs=projection, routes=routes))

        found = {o[rest_dbkey] for o in results}
        # WARNING: There can be edge cases that won't detect "not found".  For example, if count < names, not found
        # may not be an error.  It could just be "out of page".  Also, if patterns used, we can't tell if any given
        # pattern returned results without sending N queries
        if page == 0 and count > len(names):
            if not found:
                notfound = names
            else:
                explicit = {n for n in names if not Utils.is_pattern(n)}
                notfound = list(explicit - found)
                # Not checking for pattern matches (except if None found)
            if notfound:
                raise Exception(f'{", ".join([str(nf) for nf in notfound])}: not found')
        return results

    def set_context(self, rest_ctx):
        ctx = click.get_current_context()
        rest_ctx.manager = self.get_mgr()
        rest_ctx.orig_obj = ctx.obj
        ctx.obj = rest_ctx

    @staticmethod
    def format_failure(response):
        ''' Try to make an intelligible error '''
        rest_ctx = None
        _logger.info(f'Format Failure: ({type(response)}) {response}')
        try:
            _, rest_ctx = get_rest_context()
        except:
            pass
        if isinstance(response, Exception):
            _logger.debug(traceback.format_exc())

        if isinstance(response, RestException):
            try:
                content = json.loads(response.error_obj.get('content', {}))
                response = content
            except:
                pass

        if isinstance(response, SdkException) and isinstance(response.args[0], dict):
            response = response.args[0]

        human = click_env('human') # Use more human-readable format, where relevant

        # TODO: We'll have to add various patterns, such as .xyz is required
        if isinstance(response, dict):
            try:
                def format_std_error(response: dict) -> str:
                    # This is the new standard with detailed error
                    error = response.get('error', response)
                    msg = f'{error.pop("message", "Unknown error")}'
                    msg = msg.replace('data/body/0/', '')
                    inner = error.pop('innerMessage', None)
                    if isinstance(inner, str):
                        inner = {'message': inner }
                    if human:
                        for k, v in sorted(error.items()):
                            msg += f'\n\t{str(k)}: {str(v)}'
                    elif error:
                        msg += ' Additional Info: ' + json.dumps(error)
                    if inner:
                        msg += f'\n{format_std_error(inner)}'
                    return msg
                msg = format_std_error(response)
            except Exception as e:
                error = response.get('error', response.get('message', 'Unknown error'))
                msg = str(error) if not isinstance(error, dict) else str(error.get('message', 'Unknown error'))
        elif isinstance(response, SdkException):
            raw_msg = str(response.args[0])
            match = re.search('duplicate key error .*: (?P<key>".*")', raw_msg)
            if rest_ctx is not None and match is not None:
                msg = f'{rest_ctx.entity.__name__} with id {match.group("key")} already exists.'
            elif rest_ctx is not None and 'Management received an unexpected generic error from mongo' in raw_msg:
                msg = f'{rest_ctx.entity.__name__} with that name already exists.'
            else:
                msg = f'{response.args[0]}'
        elif isinstance(response, AssertionError):
            msg = 'Internal error'
        elif isinstance(response, Exception):
            msg = str(response)
            arg0 = response.args[0]
            if isinstance(arg0, dict):
                msg = arg0.get('content') or arg0.get('message') or msg
                # Especially "content" may be an embedded dict as string...
                try:
                    msgdict = json.loads(msg)
                    msg = msgdict.get('error', msgdict.get('message', msg))
                except:
                    pass
            match = \
                    re.search("(must|should) have required property '\.*(?P<prop>[^']*)", str(msg)) \
                    or re.search("Failed to render.*UndefinedError..'(?P<prop>[^']*)", str(msg)) \
                    or re.search(".*/(?P<prop>[^ /]*) must NOT have fewer than 1 items", str(msg))
            if match is not None and rest_ctx is not None:
                rest_prop = match.group('prop')
                infra_prop = hyphenated(rest_ctx.rest_info.rest2infra.get(rest_prop, rest_prop))
                msg = f'Missing required value for {infra_prop}'
        else:
            msg = str(response)
        return f'failure: {msg}'


    @staticmethod
    def _handle_values(value):
        from uuid import UUID
        if isinstance(value, FieldNotAvailable):
            return 'N/A'
        elif isinstance(value, UUID):
            return str(value)
        elif not (value or isinstance(value, (int, float))):
            return ''
        elif isinstance(value, bool):
            return 'V' if value else ''
        elif isinstance(value, (int, float, str)):
            return value
        elif isinstance(value, SDKEntity):
            try:
                # _name is the display name, if it exists.  Probably need SDKEntity.displayName()
                return value._name
            except:
                return value.name
        elif isinstance(value, HostPort):
            return value.guid
        elif isinstance(value, collections.abc.Mapping):
            return ' '.join(f'{k}={v}' for k,v in value.items() if k != 'mgmt')
            return str({RestGroup._handle_values(k): RestGroup._handle_values(v) for k, v in value.items() if k != 'mgmt'})
        elif isinstance(value, Iterable):
            return ', '.join([RestGroup._handle_values(e) for e in value])

    @rest_callback
    def do_wait(self, name, property, value, missing, boolean, poll, timeout):
        _, obj = get_rest_context()
        entities: List[SDKEntity] = self.ents_by_name(obj, name)
        if boolean:
            falses = ('false', 'f', '0')
            value = [v.lower() not in falses for v in value]
            if missing:
                missing = missing.lower() not in falses
        else:
            value += [int(v) for v in value if v.isdigit()]
        result = wait_for_property_values(entities, prop_name=property, source=SourceTypes.MANAGEMENT, values=value, non_values=[missing], poll=poll, timeout=timeout)
        color_msg('OK' if result else 'Timed out', result)
        return bool(result)

    @rest_callback
    def do_show(self, name, output_format, skip, limit, fields, onepage, entries_left=-1):
        _, obj = get_rest_context()
        total = obj.entity.count(obj.manager)
        limit = int(obj.orig_obj.get('limit')) if limit is None else limit
        if limit == 0:
            limit = total or 1
        page = float(skip)/float(limit)  # apparently 'page' is a float, the reason for it is so that management server could calculate back the skip for mongodb
        if entries_left < 0:
            # Allow stupid log-alerts to send in a pre-calculated entries_left
            try:
                entries_left = len(name) if name else (total - skip)
                assert isinstance(entries_left, int)
            except:
                entries_left = 1
        first = True
        while entries_left > 0:
            self._show(obj, name, output_format or obj.orig_obj.get('output_format'), page, limit, fields, first=first)
            first = False
            entries_left -= limit
            page += 1
            if onepage:
                entries_left = 0
            if entries_left > 0:
                if prompt(f"{entries_left} remaining.\nPress n to present next {limit} entries\nPress q to quit", type=click.Choice(['n', 'q']), show_choices=False, default='n') == 'q':
                    entries_left = 0
            if entries_left <= 0:
                break
        return True

    def _show(self, obj, names, output_format, page, limit, custom_fields=None, first=True):
        if output_format == 'json':
            # Special case - dump full REST response - not fields of items
            click.echo(json.dumps(self.dicts_by_name(obj, names, page=page, count=limit), indent=2))
            return
        dbkey = obj.rest_info.dbkey or 'name'
        keyprop = obj.rest_info.rest2infra.get(dbkey, dbkey)
        def_fields = obj.rest_info.kwargs.get('display', [keyprop])
        key_field = [keyprop] if keyprop in def_fields else def_fields[:1]
        if custom_fields:
            fields = key_field
            for f in custom_fields.split(','):
                fs = raw_to_snake(f)
                prop = obj.name_index.get(fs)
                if keyprop in (f, fs, prop):
                    continue
                if not prop:
                    if hasattr(obj.entity, fs):
                        prop = fs
                    else:
                        prop = f
                fields.append(prop)
        elif output_format == 'list':
            fields = key_field
        else:
            fields = def_fields

        out_list, invalid = self.generate_out_list(obj, names, page, limit, fields, custom_fields)
        headers = [snake_to_human(raw_to_snake(f.replace('-', '_'))) for f in fields]
        if invalid:
            if first:
                warn_msg(f'Invalid field(s): {", ".join([f for n, f in invalid])}')
            for n in reversed([n for n,f in invalid]):
                headers.pop(n)
                for out_row in out_list:
                    out_row.pop(n)

        if not out_list:
            return
        else:
            if output_format == 'rows':
                click.echo(format_robust_table(out_list, headers))
            elif output_format == 'tabular':
                click.echo(format_pretty_table(out_list, headers))
            elif output_format == 'list':
                click.echo('\n'.join(['\t'.join([str(v) for v in row]) for row in out_list]))
            else:
                click.echo(format_smart_table(out_list, headers))

    def generate_out_list(self, obj, names, page, limit, fields, custom_fields):
        out_list = []
        invalid: List[Tuple[int, str]] = []
        row_1 = bool(custom_fields)
        results: List[SDKEntity] = self.ents_by_name(obj, names, page=page, count=limit, fields=fields)
        for r in results:
            unloaded = set(fields) - set(r.get_properties().keys())
            if unloaded:
                _logger.debug(f'SHOW {self.name} UNLOADED FIELDS: {list(unloaded)} (These will cause Mgmt refresh)')
            # TODO: We anyway need a map from arg-names to infra property-names.
            out_data = []
            for n, f in enumerate(fields):
                try:
                    value = r.get_property(f, SourceTypes.MANAGEMENT)
                except Exception as e:
                    _logger.debug(f'SHOW {self.name}.{f} Exception: {repr(e)}')
                    try:
                        value = getattr(r, f)
                    except Exception as e:
                        _logger.debug(f'SHOW {self.name}.{f} Invalid: {repr(e)}')
                        if row_1:
                            invalid.append((n, f))
                        value = FieldNotAvailable()
                out_data.append(RestGroup._handle_values(value))
            row_1 = False
            out_list.append(out_data)

        return out_list, invalid

    @click.command(help='Show how many instances of this type are in the system')
    @rest_callback
    def count():
        rest_ctx = click.get_current_context().find_object(RestContext)
        click.echo(rest_ctx.entity.count(rest_ctx.manager))
        return True

    @rest_callback
    def do_delete(self, name):
        ctx = click.get_current_context()
        obj = ctx.obj
        assert isinstance(obj, RestContext), f'Invalid context (type={type(obj).__name__}) for command: "delete"'
        _logger.debug(f'Execute {obj.name}.delete: {name}')
        # For delete, we have to first get all the keys, because paging during deletes won't work
        dicts: List[dict] = []
        page = 0
        while True:
            dpage = self.dicts_by_name(obj, name, page=page, fields=['uuid', '_id', obj.rest_info.dbkey], count=1000)
            if not dpage:
                break
            page += 1
            # While we only need dicts, we still must map_props() from MGMT
            dicts.extend([obj.entity.map_props(d, SourceTypes.MANAGEMENT) for d in dpage])
            # dicts.extend(dpage)
        if not dicts:
            return True

        for d in dicts:
            d['mgmt'] = obj.manager
        response = True
        # for result in obj.entity.bulk_delete(dicts):
        results = obj.entity._delete_many(dicts, obj.manager)
        for result in results:
            prefix = f'[{result["_id"]}] '
            if isinstance(result, dict) and result.get("success"):
                success_msg(prefix + 'success')
            else:
                response = False
                failure_msg(prefix + self.format_failure(result))
        return results if response else False

    @rest_callback
    def do_operation(self, op_name, op_info, key_prefix=None, **kwargs):
        ctx = click.get_current_context()
        obj = ctx.obj
        keys = kwargs.pop('dbkeys', [])
        style = op_info.get('style')
        if style == 'one' and len(keys) > 1:
            # Multi-call for single key operation...
            _logger.info(f'MULTI-OP: {op_name}, style: {style}, #keys: {len(keys)}, keys: {keys[0]} .. {keys[-1]}')
            results = []
            response = True
            for key in sorted(keys):
                result = self.do_operation(op_name, op_info, key_prefix=str(key), dbkeys=[key], **kwargs)
                _logger.info(f'MULTI-OP: {op_name}, key: {key}, result: {result}')
                if response is True:
                    if not result:
                        response = False
                    else:
                        results.extend(result)
            return results if response else False

        assert isinstance(obj, RestContext), f'Invalid context (type={type(obj).__name__}) for command: {ctx.command.name}'
        _logger.debug(f'Execute {obj.name}.{op_name} args: {mask_hidden_fields(kwargs)}')
        if isinstance(keys, str):
            keys = [keys]
        argsdict = self._process_kwargs(obj, kwargs)
        result = obj.entity.do_operation(obj.manager, op_name, keys, **argsdict)
        if not isinstance(result, Iterable): # Shouldn't happen. Should always be list[dict]
            result = [result]
        response = True
        pfx = f'{key_prefix} > ' if key_prefix else ''
        for i, r in enumerate(result):
            if isinstance(r, bool):
                success = r
                msg = f'[{keys[i]}] {"success" if r else "failure"}'
            else:
                success = r.get("success")
                msg = f'[{pfx}{r.get("_id", "")}] {"success" if success else self.format_failure(r)}'
                if style == 'one': # Results accumulated in multiple-calls, so need to differentiate
                    r['__op_key'] = key_prefix or (keys[0] if len(keys) == 1 else keys[i])
            color_msg(msg, success)
            if not success:
                response = False
        _logger.info(f'OP: {op_name}, result: {result}, response: {response}')
        return result if response else False

    def _process_kwargs(self, rest_ctx: RestContext, kwargs: Dict) -> Dict:
        ''' Convert kwargs sent by click to a possibly nested dict:
            - convert option names to infra names
            - enable templates and pass-through properties
            - enable nested objects
        '''
        # Load values from template, if any, and then parameters
        processed_args = {}
        template = kwargs.pop('cli_template', None)
        if template:
            for k,v in load_template(raw_to_snake(rest_ctx.name), template).items():
                # Try all possible mappings.  Template might be in REST or CLI (hyphenated) - Needs to be Infra
                k = rest_ctx.rest_info.rest2infra.get(k, k)
                k = raw_to_snake(k)
                k = rest_ctx.name_index.get(k.replace('-', '_'), k)
                processed_args[k] = v
        extra_props = kwargs.pop('cli_property', None)
        if extra_props:
            for extra in extra_props:
                _logger.debug(f'cli_property: {extra}')
                k, _, v = extra.partition('=')
                if k[-1] == '#':
                    k = k[:-1]
                    try:
                        v = int(v)
                    except ValueError:
                        v = float(v)
                elif k[-1] == '?':
                    k = k[:-1]
                    v = v.lower() not in ('f', 'false', '0', '')
                _logger.debug(f'cli_property: {k} = {v}')
                processed_args[rest_ctx.name_index.get(k, k)] = v
        processed_args.update({rest_ctx.name_index.get(p, p):v for p,v in kwargs.items() if v not in (None, ())})
        processed_args = expand_dots(processed_args)
        # NOTE: This doesn't support more than one level of nesting. (YAGNI?)
        for obj_prop, otype in rest_ctx.subobj_index.items():
            if obj_prop in processed_args:
                processed_args[obj_prop] = otype(**processed_args[obj_prop])

        return processed_args

    @rest_callback
    def do_create(self, **kwargs):
        ctx = click.get_current_context()
        _, rest_ctx = get_rest_context(ctx)
        obj = ctx.obj
        assert obj == rest_ctx, f'({type(obj)}) {obj} != ({type(rest_ctx)}) {rest_ctx}'
        method = ctx.command.name.lower()
        assert isinstance(obj, RestContext), f'Invalid context (type={type(obj).__name__}) for command: {ctx.command.name}'
        _logger.debug(f'Execute {obj.name}.{method}: {mask_hidden_fields(kwargs)}')

        # Merge --delete-<mapping> keys into their corresponding mapping params as (key, None) pairs,
        # then remove from kwargs so _process_kwargs doesn't see them.
        if method == 'update':
            param_map = {p.name: p for p in ctx.command.params}
            for param_name in list(kwargs.keys()):
                param = param_map.get(param_name)
                if param and hasattr(param, '_delete_for'):
                    keys_to_delete = kwargs.pop(param_name)
                    if keys_to_delete:
                        mapping_param = param._delete_for
                        existing = kwargs.get(mapping_param)
                        if existing is None:
                            existing = []
                        elif isinstance(existing, tuple):
                            existing = list(existing)
                        existing.extend([(k, None) for k in keys_to_delete])
                        kwargs[mapping_param] = existing

        prop_values = self._process_kwargs(obj, kwargs)

        # Invoke method
        # instance = obj.entity.from_dict(prop_values)
        dbkey = obj.rest_info.dbkey or 'name'
        keyprop = obj.rest_info.rest2infra.get(dbkey, dbkey)
        keys = prop_values.pop(keyprop, ['singleton'])
        bulk_method = getattr(obj.entity, 'bulk_' + method)
        _logger.debug(f'{method} keys: [#{len(keys)}] {keys[:3]}...')
        entities = [obj.entity.from_dict(d) for d in ({**prop_values, **{keyprop: k}} for k in keys)]
        response = entities
        for ent, result in zip(entities, bulk_method(entities)):
            prefix = f'[{ent.rest_id}] ' if len(entities) > 1 else ''

            # if isinstance(result, dict) and result.get("success"):
            if ent == result:
                success_msg(prefix + 'success')
            else:
                ent.clear_local_source(ent)
                response = False
                failure_msg(prefix + self.format_failure(result))
        return response

        try:
            results = obj.entity.getattr('bulk_'+method)(instances)
        except:
            # Clear local value, in case same entity id re-used later for create/update
            instance.clear_local_source(instance)
            raise

        success_msg('success')
        return [instance]


_current_rest_version: Optional[str] = None
def build_rest_cmds(group: click.Group, mgr: Optional['Manager'] = None, default_version: Optional[str] = None):
    """ Add REST commands to for the given REST version to the click Group.  mgr overrides version """
    import xlro.tools.cli.rest_custom as rest_custom

    if mgr:
        version = mgr.api_version
    else:
        version = default_version or RestVersionManager.max_version()

    global _current_rest_version
    _logger.debug(f'Building REST commands for version: {version}')

    # Clear any existing RestGroups, in case this is a reset
    if _current_rest_version is not None:
        list(map(group.commands.pop, [name for name, cmd in group.commands.items() if isinstance(cmd, RestGroup)]))

    _current_rest_version = version

    rv_info = RestVersionManager.version_info(version)
    rest_groups: Dict[str, click.Group] = {}
    for e_name, e_info in rv_info.entities.items():
        if e_info.route:
            _logger.debug(f'REST group: {e_name}')
            try:
                rest_group = getattr(rest_custom, e_name + 'Group')(e_name, e_info, mgr)
            except Exception as e:
                rest_group = RestGroup(e_name, e_info, mgr)
            rest_groups[e_name] = rest_group
            group.add_command(rest_group)

    for e_name, e_group in rest_groups.items():
        custom_cli = getattr(rest_custom, e_name + 'CLI', None)
        if custom_cli is None or not isinstance(custom_cli, type):
            continue
        for cmdname, cmd in inspect.getmembers(custom_cli, predicate=lambda v: isinstance(v, click.Command)):
            _logger.debug(f'REST custom: {e_name}.{cmdname}')
            cmd.callback = rest_callback(cmd.callback)
            # In case it's an override
            try:
                del e_group.commands[cmdname]
            except:
                pass
            e_group.add_command(cmd)
        for cmdname, cmd in inspect.getmembers(custom_cli, predicate=lambda v: v == rest_custom.Unsupported):
            _logger.debug(f'REST disable: {e_name}.{cmdname}')
            try:
                del e_group.commands[cmdname]
            except:
                pass

    global _cli_templates
    _cli_templates.clear()
