#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from builtins import next
from builtins import str
from builtins import object
from typing import Any,Dict,Optional,Type
import sys
import os
import re
import json
import argparse
import logging
import collections
import logging.config
import confetti
from xlro.core import infra_conf
from xlro.core.util.common import get_path
from xlro.core.util.read_dict import read_dict
from xlro.core.util.dict_util import merge_dicts
from xlro.core.util.general_utils import mask_hidden_fields

class BriefEntityEncoder(json.JSONEncoder):
    def default(self, obj): # pylint: disable=method-hidden
        from xlro.core.entities import BaseEntity
        if isinstance(obj, BaseEntity):
            return obj.key()
        # Let the base class default method raise the TypeError
        try:
            return json.JSONEncoder.default(self, obj)
        except:
            return str(obj)

class DeepEntityEncoder(json.JSONEncoder):
    def __init__(self, use_refs=False, **kwargs):
        from xlro.core.entities import BaseEntity
        super(DeepEntityEncoder, self).__init__(**kwargs)
        self.tracker: Optional[Dict[BaseEntity, Dict[str, Any]]] = None if not use_refs else {}

    def default(self, obj): # pylint: disable=method-hidden
        from xlro.core.entities import BaseEntity
        if isinstance(obj, BaseEntity):
            return obj.to_dict(tracker=self.tracker)
        # Let the base class default method raise the TypeError
        try:
            return json.JSONEncoder.default(self, obj)
        except:
            return str(obj)

def jsonify(obj, **kwargs):
    deep = kwargs.pop('deep', False)
    kwargs.setdefault('cls', BriefEntityEncoder if not deep else DeepEntityEncoder)
    kwargs.setdefault('indent', 2)
    return json.dumps(obj, **kwargs)

def jprint(obj, **kwargs):
    print(jsonify(obj, **kwargs))

class EntityArg(object):
    ''' Usable as an argparse type which will interpret string arg a an entity spec '''
    def __init__(self, clsName, sep=None, value=None):
        self.clsName = clsName
        self.sep = sep
        self.value = value

    def __call__(self, value):
        return EntityArg(self.clsName, sep=self.sep, value=value)

    def evaluate(self):
        # Need delayed evaluation in case depending on Manager or something
        if self.sep:
            return str2entities(self.value, cls=self.clsName, sep=self.sep)
        else:
            return str2entity(self.value, cls=self.clsName)


class EntitiesArg(EntityArg):
    ''' Usable as an argparse type which will interpret string as a 'sep' separated list of entity specs '''
    def __init__(self, clsName, sep=','):
        super(EntitiesArg, self).__init__(clsName, sep=sep)

# We should promote this into the entities...
KEYFMTS = {
        'Manager': '((?P<user>[^:]+)(:(?P<passwd>[^:]+))?@)?(?P<host>\S+)',
    }

def str2entity(s, cls='BaseEntity'):
    """
        Convert string to Entity. Simplistic, and so-far, non-recursive.
        Considered rison, but forces tags for values and won't support typing very nicely.
    """
    from xlro.core.entities import BaseEntity, SDKEntity
    logger = logging.getLogger('str2entity')
    cls = cls if isinstance(cls, type) else BaseEntity.ENTITY_REGISTRY[cls]
    clsname, _, rest = s.partition(':')
    try:
        target_cls = BaseEntity.ENTITY_REGISTRY[clsname]
        assert issubclass(target_cls, cls), 'String class: {} not subclass of requested class {}'.format(target_cls.__name__, cls.__name__)
        s = rest
    except AssertionError:
        raise
    except KeyError:
        try:
            target_cls = cls
        except:
            raise Exception("Cannot find entity-class '{}' (clsname={}).".format(s, target_cls))

    # This was a good idea, but key props are not ordered :-( so it only works for 1 key!
    keylist = target_cls._xlro_keyprops[:]
    vdict: Dict[str, Any] = {}
    if issubclass(target_cls, SDKEntity) and 'mgmt' in keylist:
        # For SDK entities (maybe all), put mgmt last (if at all)
        keylist = [k for k in keylist if k != 'mgmt'] + ['mgmt']

    try:
        # See if there's a special key format for identifying this cls instance...
        match = re.match('^' + KEYFMTS[target_cls.__name__] + '$', s)
        # groupdict() sends None for missing values, and sends unicode
        assert match, 'Unable to identify class instance for {}'.format(s)
        vdict = {str(k): str(v) for k, v in match.groupdict().items() if v is not None}
    except Exception as e:
        # Otherwise, parse the string
        for part in s.split(':'):
            key, eq, val = part.partition('=')
            if not eq:
                if not keylist:
                    raise Exception('Cannot parse "{}" as {}. More values than key fields.'.format(s, target_cls.__name__))
                val = key
                key = keylist.pop(0)
            else:
                if key in keylist: keylist.remove(key)
            vdict[key] = val

    # Instance() should handle, but maybe defaults could save us, so don't pre-check
    # if keylist:
        # raise Exception('Cannot parse "{}" as {}. Missing keys: {}'.format(s, target_cls.__name__, ', '.join(keylist)))
    # Very simplistic attempt at PropertyType casting.  Only for primitive of list of primitives.
    for key, val in vdict.items():
        spec = target_cls._property_spec(key)
        if not spec or not spec.ptype:
            continue
        # If property is list, split the string
        if isinstance(spec.ptype, collections.abc.Iterable) and not isinstance(spec.ptype, str):
            # This will only work with an array of primitive types...
            membertype = next(iter(spec.ptype))
            nval = [membertype(v) for v in val.split(',')]
        else:
            try:
                assert isinstance(spec.ptype, type), '{} is not a type'.format(spec.ptype)
                if issubclass(spec.ptype, BaseEntity):
                    nval = str2entity(val, spec.ptype)
                else:
                    nval = spec.ptype(val)
            except:
                nval = val
        vdict[key] = nval
    return target_cls.instance(**vdict)


def str2entities(elist, cls=None, sep=','):
    return [str2entity(e, cls) for e in elist.split(sep)]

# Add notice level
# This level is used in slas-h, and as a default "print" level for CLI utilities (using cli_print())
NOTICE = logging.INFO+1
def _notice(logger: logging.Logger, msg: str, *args: Any, **kwargs: Any) -> None:
    ''' Special wrapper for notice level, for CLI utilities only. '''
    # TODO: I think all CLIs should use this (CLILogger.notice()) instead of print.
    # The advantages are thread-safety and auto-inclusion in other logs
    if logger.isEnabledFor(NOTICE):
        logger._log(NOTICE, msg, args, **kwargs)
logging.addLevelName(NOTICE, 'NOTICE')
setattr(logging.Logger, 'notice', _notice)
LOGGING_DEFAULT=NOTICE

class ConsoleHandler(logging.StreamHandler):
    ''' StreamHandler to split output to stdout/stderr '''
    def emit(self, record):
        self.stream = sys.stdout if record.levelno == NOTICE else sys.stderr
        super(ConsoleHandler, self).emit(record)

# Create global cli logger AFTER logging config.
_cli_logger: Optional[logging.Logger] = None
def cli_print(msg, *args, **kwargs):
    ''' Convenience for cli utils. '''
    # NOTE: notice() is non-standard, so mypy will correctly prevent Logger.notice() in core. This is CLI exception
    # Also, we can't just call cli_notice, because then the logger's filename/lineno will be here.
    global _cli_logger
    if not _cli_logger:
        _cli_logger = logging.getLogger(os.path.basename(sys.argv[0]))
    level = kwargs.pop('level', NOTICE)
    if _cli_logger.isEnabledFor(level):
        _cli_logger._log(level, msg, args, **kwargs)

# Shared parser for args that are parsed early (before main ArgumentParser).
# Used as a parent so these args appear in --help of the main parser.
_early_args_parser = argparse.ArgumentParser(add_help=False)
_early_args_group1 = _early_args_parser.add_argument_group('Debugging')
_early_args_group1.add_argument("-L", "--loglevel", help='Logging-level for stderr')
_early_args_group1.add_argument("--logfile", help='Log file (full DEBUG, in addition to stderr)')
_early_args_group2 = _early_args_parser.add_argument_group('Advanced Configuration')
_early_args_group2.add_argument("--configfile", help='Additional configuration file', action='append', default=[])
_early_args_group2.add_argument("--configure", help='Override specific configuration', action='append', default=[])
# Undocumented - For debugging of the config overrides
_early_args_group2.add_argument("--confdebug", help=argparse.SUPPRESS, action='store_true')


def add_common_args(parser, require_manager=False, include_manager=True):
    # Use upper-case for "common" options to avoid conflict.
    # Probably better to only allow long names...
    if include_manager:
        parser.add_argument("-M", "--manager", required=require_manager, type=EntityArg('Manager'))
    # Early-parsed args (--loglevel, --logfile, --configfile, --configure, --confdebug)
    # are defined in _early_args_parser. Add them as a parent for --help display.
    # Track created groups so we can add more args to them.
    groups_by_title = {}
    for group in _early_args_parser._action_groups:
        if group.title in ('positional arguments', 'optional arguments', 'options'):
            continue
        new_group = parser.add_argument_group(group.title, group.description)
        groups_by_title[group.title] = new_group
        for action in group._group_actions:
            # Copy the action to the new group for --help display
            new_group._group_actions.append(action)
            parser._option_string_actions.update({opt: action for opt in action.option_strings})
            parser._actions.append(action)  # Also add to _actions for usage line
    # Add additional debugging args to the same group
    g = groups_by_title.get('Debugging') or parser.add_argument_group('Debugging')
    g.add_argument("--rest-debug", help='Path to output REST requests and responses')
    g.add_argument("--limit-sourcetypes", help='Limit infra sourcetypes', default=[])

def init_logging(level=None, filename=None):
    """
    Initialize logging configuration

    Args:
        level: Console log level (e.g., 'DEBUG', 'INFO')
        filename: Optional explicit log file path (overrides persistent logging)
    """
    default_path = get_path('{}/../config/default_logging.yaml'.format(os.path.dirname(__file__)),
                            'xlro/core/config/default_logging.yaml')
    logging_conf = read_dict(default_path)
    custom_path = infra_conf.root.logging.conf_file
    if custom_path:
        merge_dicts(logging_conf, read_dict(custom_path))

    if filename:
        # If user wants to redirect stderr, they can.
        # logfile is separate handler (and defaults to DEBUG vs. NOTICE for console.)
        logging_conf['loggers']['']['handlers'].append('file')
        logging_conf['handlers']['file']['filename'] = filename
    elif 'file' not in logging_conf['loggers']['']['handlers']:
        # Logging was opening the file even if the file handler not in use!
        del logging_conf['handlers']['file']

    # Setup persistent rotating log file (separate from --logfile)
    if infra_conf.root.logging.persistent_path:
        try:
            # Get filename template from default_logging.yaml
            cli_log_file = infra_conf.root.logging.persistent_path

            # Replace {tool} placeholder with actual tool name
            log_file_path = cli_log_file.replace('{tool}', os.path.splitext(os.path.basename(sys.argv[0]))[0])

            # Expand user home directory
            log_file_path = os.path.expanduser(log_file_path)

            # Create directory if it doesn't exist
            log_dir = os.path.dirname(log_file_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            if not os.path.exists(os.path.dirname(log_file_path)):
                os.makedirs(os.path.dirname(log_file_path), exist_ok=True)

            # Update handler filename and enable it
            logging_conf['loggers']['']['handlers'].append('cli_file')
            logging_conf['handlers']['cli_file']['filename'] = log_file_path
        except Exception as e:
            # If CLI logging setup fails, don't break the tool
            # Remove handler to prevent dictConfig errors
            if 'cli_file' in logging_conf['handlers']:
                del logging_conf['handlers']['cli_file']
            logging.getLogger(__name__).warning(f'Failed to setup CLI logging: {e}')
    elif 'cli_file' in logging_conf['handlers']:
        # Remove handler from config if persistent_path is not set
        del logging_conf['handlers']['cli_file']

    if level:
        logging_conf['handlers']['console']['level'] = int(level) if level.isdigit() else level.upper()

    logging.config.dictConfig(logging_conf)

    class IdFilter(logging.Filter):
        def filter(self, record):
            if hasattr(record, 'id'):
                record.name += '.' + str(record.id)
            return True

    idfilter = IdFilter()
    for handler in logging.root.handlers:
        handler.addFilter(idfilter)


def config_override(config: confetti.Config, key: str, value: str) -> None:
    # Enhanced version of confetti's assign_path to support historic features of our slash/config plugin
    # - auto-split lists by ','
    # - support +/- for list values
    # - support tri-state (historic problem with boolean + None)
    # - support new leaf keys

    # Allow op's at end of tag name, e.g., x.y.somelist+=new1,new2 should EXTEND existing list
    # '+' and '-' operators act on lists, and add or remove the elements from the existing list
    # TODO: '/' to allow replacement in string?
    op = None
    if key[-1] in ['+', '-']:
        op = key[-1]
        key = key[:-1]

    try:
        leaf = config.get_config(key)
    except confetti.exceptions.InvalidPath:
        # Enable extending a new leaf config path
        parent, _, child = key.rpartition('.')
        try:
            leaf = config.get_config(parent)
        except confetti.exceptions.InvalidPath:
            raise Exception(f'Cannot add key: {key}.  Parent {parent} must exist.')
        if op:
            # '+' will force list value, '-' will be empty list
            leaf.extend({ child: [] if op != '+' else [v.strip() for v in value.split(',')] })
        else:
            leaf.extend({ child: value })
        return

    ovalue = leaf.get_value()
    if isinstance(ovalue, list):
        # We use ','-separated, typed lists.  Confetti uses python list syntax.  Preserving our behavior
        vlist = [v.strip() for v in value.split(',')]
        itemtype: Type = type(ovalue[0]) if len(ovalue) else str # type: ignore
        if issubclass(itemtype, str):
            itemtype = str
        # allow empty list
        value = value.strip()
        vlist = [itemtype(item.strip()) for item in value.split(',')] if value else []
        if op == '+':
            vlist = ovalue + vlist
        elif op == '-':
            vlist = [item for item in ovalue if item not in vlist]
        value = str(vlist)
    elif op in ['+','-']:
        raise Exception(f'Invalid config assignment "{op}=". Key: "{key}" is not a list.')

    # Handle historical boolean conversion and tri-state
    if isinstance(ovalue, bool):
        if value.lower() in ['null', 'none']:
            leaf.set_value(None)
            return
        value = 'true' if (value != '' and value.lower() != 'false' and value != '0') else 'false'

    # Let Confetti do it's thing
    config.assign_path(key, value, deduce_type=True, default_type=str)
    return


def handle_common_args(args):
    # Config overrides (--configfile, --configure, --confdebug) are now
    # processed early in parse_early_args() before this is called.

    if not os.path.exists(infra_conf.root.tools.infra_shared_so):
        infra_conf.root.tools.infra_shared_so = os.path.join(
            get_path(f'{os.path.dirname(__file__)}/../../tools'), 'infra_shared.so')

    if args.limit_sourcetypes:
        from xlro.core.entities import BaseEntity
        BaseEntity._xlro_limit_sourcetypes = args.limit_sourcetypes

    if args.rest_debug:
        os.environ['REST_DEBUG'] = args.rest_debug

    # We had to delay processing SDK entities until config fully processed...
    argsdict = vars(args)
    # Manager must be first. Yet another kludge...
    try:
        argsdict['manager'] = argsdict['manager'].evaluate()
    except:
        pass
    for opt, value in argsdict.items():
        if isinstance(value, EntityArg):
            argsdict[opt] = value.evaluate()
        elif isinstance(value, collections.abc.Sequence) and len(value) and isinstance(value[0], EntityArg):
            argsdict[opt] = [v.evaluate() for v in value]

    return args

# Make CLIArgumentParser parse certain arguments before all others

# This function must be called at the start of CLIArgumentParser.__init__.
_early_args_parsed = False
def parse_early_args(argv=None):
    """Parse config and logging args from argv before any ArgumentParser is created.

    Consumes the parsed arguments from sys.argv so subsequent parsers don't see them.
    Processes config overrides immediately (before logging init).
    NOTE: main() scripts which have logging side effects during module load or initialization must call this function before any logging is done.
    See cli.py for an example.
    """
    import sys
    import logging

    global _early_args_parsed
    if _early_args_parsed is True:
        return
    _early_args_parsed = True

    modify_sysargv = argv is None
    if argv is None:
        argv = sys.argv[1:]

    # Use shared _early_args_parser which defines the early-processed args
    known, remaining = _early_args_parser.parse_known_args(argv)

    if modify_sysargv:
        sys.argv[1:] = remaining

    # Process config overrides (same as handle_common_args)
    if known.confdebug:
        print('INITIAL CONFIG:', json.dumps(infra_conf.serialize_to_dict(), indent=2))
    for cfile in known.configfile:
        confdict = read_dict(cfile)
        infra_conf.update(confetti.Config(confdict))
        if known.confdebug:
            print('AFTER CFILE:', cfile, '\n', json.dumps(infra_conf.serialize_to_dict(), indent=2))
    for cstring in known.configure:
        key, _, value = cstring.partition('=')
        config_override(infra_conf, key, value)
        if known.confdebug:
            print('AFTER configure:', key, '=', value, '\n', json.dumps(infra_conf.serialize_to_dict(), indent=2))

    init_logging(level=known.loglevel, filename=known.logfile)
    logging.getLogger(__name__).info(f'Initialized early config and logging')

class CLIArgumentParser(argparse.ArgumentParser):
    """
    Extended ArgumentParser to pre-load common CLI arguments and handling.

    Args:
        require_manager: If True, manager connection is required
        user_style: If True, use user-friendly style with advanced options hidden
        include_login_opts: If True, include login/authentication options
    """
    def __init__(self, *args, require_manager=False, user_style=False, include_login_opts=True, **kwargs):
        parse_early_args()
        super(CLIArgumentParser, self).__init__(*args, **kwargs)
        self.user_style = user_style
        self.require_manager = require_manager
        self.include_login_opts = include_login_opts
        # In user-style, we hide all the advanced options in a trailing section
        self.add_argument("-M", "--manager", required=require_manager, type=EntityArg('Manager'))
        if self.include_login_opts:
            g = self.add_argument_group('Authentication')
            g.add_argument("-u", "--user", help='User ID for management login')
            g.add_argument('--use-tls', type=lambda t_f: t_f.lower() == 'true', default=None,
                    help='Use TLS vs login/password')
            g.add_argument('--cert', help='Cert file for tls connection with management')
            g.add_argument('--key', help='Key file for tls connection with management')
            g.add_argument('--ca', help='CA file for tls connection with management')
        if not user_style:
            add_common_args(self, require_manager=require_manager, include_manager=False)

    def parse_known_args(self, args=None, namespace=None):
        if self.user_style:
            adv_parser = self.add_argument_group('Advanced/Dev options')
            add_common_args(adv_parser, require_manager=self.require_manager, include_manager=False)
        args, remaining = super(CLIArgumentParser, self).parse_known_args(args, namespace)
        handle_common_args(args)
        return args, remaining

def raw_to_snake(s):
    snake = ''
    in_upper = False
    for c in s:
        if c == '-':
            snake += '_'
        elif c.isupper():
            if not in_upper:
                in_upper = True
                if snake and not snake[-1] == '_':
                    snake += '_'
            snake += c.lower()
        else:
            in_upper = False
            snake += c
    # Some unfortunate exceptions: but only 2 is pretty good.
    snake = snake.strip('_')
    if snake == 'raidlevel':
        return 'raid_level'
    if snake.startswith('p_raid'):
        return 'praid' + snake[6:]
    return snake

def hyphenated(s):
    return raw_to_snake(s).replace('_', '-')

def snake_to_camel(s):
    return ''.join([(word[0].upper() + word[1:]) for word in s.split('_')])

# All-Cap words
all_caps = ['ssl', 'ip', 'nic', 'id', 'nvme', 'praid', 'pci', 'snap', 'uuid', 'tpv', 'cdv', 'acm', 'mtu', 'gid', 'guid', 'os', 'ofed']
_human_mapping = {w: w.upper() for w in all_caps}
# Human words
_human_mapping.update({
        'num': 'Number',
        'jr': 'Journal',
        'sn': 'Serial #',
        'tx': 'Transaction',
        'mgmt': 'Manager',
        'nvmf': 'NVMf',
        'crc': 'CRC',
        'vsg': 'VSG'
    })
# Plurals
_human_mapping.update({w + 's': u + 's' for w, u in _human_mapping.items()})

def snake_to_human(s):
    return ' '.join([_human_mapping.get(word, word[0].upper() + word[1:]) for word in s.split('_') if word])


def main():
    parser = argparse.ArgumentParser()
    add_common_args(parser)
    parser.add_argument('-n', '--names', action='store_true', help='Only do name conversions')
    parser.add_argument('strings', nargs='+')
    args = parser.parse_args()
    handle_common_args(args)

    for arg in args.strings:
        try:
            if args.names:
                snake = raw_to_snake(arg)
                print(f'{arg} -> {snake} -> {snake_to_camel(snake)} -> {snake_to_human(snake)}')
            else:
                print('S2E:', arg)
                print(str2entity(arg).to_dict())
        except Exception as e:
            print(repr(e))

if __name__ == '__main__':
    main()
