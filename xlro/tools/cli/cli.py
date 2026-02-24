#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import re

import click
import click_shell
from logging import getLogger
from typing import Optional, Set, Dict
from xlro.core import infra_conf
from xlro.core.util.ssh import Connection
Connection.LOCALHOST_CHECK = True
from xlro.tools.cli.rest_click import build_rest_cmds, set_exit_code,exit_code
from xlro.tools.cli.common import GET_LIMIT, OUTPUT_FORMATS, click_env, warn_msg, failure_msg, is_no_prompt, confirm
from xlro.core.sdk.ConnectionManager import ManagementLoginError, ConnectionManagerError, ChangePasswordRequiredError, DEFAULT_PORT
from xlro.core.entities import Manager, SourceTypes
from xlro.core.entities.rest_info import RestVersionManager, RestVersionInfo
from xlro.core.util.creds import store_creds, local_settings_dir
from xlro.core.util.cli_util import parse_early_args
from xlro.core.util.general_utils import mask_hidden_fields

# Initialize logging before ANYTHING in CLI...
parse_early_args()

logger = getLogger('nvmesh-cli')

os.environ.setdefault("LANG", os.environ.setdefault("LC_ALL", "en_US.utf-8"))

CLI_VERSION = '2.0' # Marking major version with latest API.
# Probably we should use <major>.<minor> where major indicates a breaking API change and minor is increased
# for non-breaking (e.g., added options, fixes, etc.) changes.  I can go either way on a new top-level command.

CTX_MGRNAME = 'manager-name'
CTX_MGROBJ = 'manager-obj'
CTX_MGRUSER = 'manager-user'
CTX_PROMPT = 'prompt'

_mgr_warned = False
def warn_unconnected():
    warn_msg('WARNING: You are in "unconnected" mode.')
    click.echo('You must either specify a management host on the command-line via -m/--manager or use the "login" command.')

def prompt_string():
    ctx_obj = click.get_current_context().obj
    if CTX_PROMPT in ctx_obj:
        return ctx_obj[CTX_PROMPT]
    mgrhost = click.get_current_context().obj.get(CTX_MGRNAME) or 'unconnected'
    global _mgr_warned
    if mgrhost == 'unconnected' and _mgr_warned == False:
        _mgr_warned = True
        warn_unconnected()
    return f'[{mgrhost}] '

def default_management():
    try:
        return ','.join(Manager.default_endpoints())
    except Exception as e:
        return None

_pre_parsed_args = {}
@click_shell.shell(context_settings={'help_option_names': ['-h', '--help']}, invoke_without_command=True,
                prompt=prompt_string if sys.stdin.isatty() else lambda: '',
                help='For interactive mode run \'nvmesh\' without any additional commands. '
                    'While in interactive mode you can use \'!\' prefix to execute shell commands, '
                    'and navigate history and use auto-complete as in the bash shell',
                epilog=
                    f'NOTE: Subcommands are dependent on the version of Management. Default is version: {RestVersionManager.max_version()}'
                    f'\nYou can use "{sys.argv[0]} -m <management-host> help" to see subcommand list for a specific management server.',
                hist_file=os.environ.get('NVMESH_HIST', os.path.join(local_settings_dir(), '.nvmesh_history')))
@click.pass_context
def nvmesh(ctx: click.Context):
    args = _pre_parsed_args
    mgr = args.pop('manager', None)
    ctx.ensure_object(dict)
    ctx.obj.update(args)
    # This is a secret override used by CLI test.  It's a bit much to add a cmd-line param, IMO.
    prompt = os.environ.get('NVMESH_CLI_PROMPT')
    if prompt:
        ctx.obj[CTX_PROMPT] = prompt
    if not args.get('color'):
        import humanfriendly.tables
        humanfriendly.tables.highlight_column_name = lambda s: s

    ctx.obj['cmd-line'] = ctx.invoked_subcommand
    if mgr:
        ctx.obj[CTX_MGROBJ] = mgr
        ctx.obj[CTX_MGRNAME] = mgr.current_host
        ctx.obj[CTX_MGRUSER] = mgr.connection.user

    if args.get('version'):
        ctx.invoke(version)
        sys.exit(0)

_nl = '\n'
_version_string = f'CLI-Version: {CLI_VERSION}{_nl}Infra-Version: {infra_conf.root.src_version}{_nl}Max-API-Version: {RestVersionManager.max_version()}'

@nvmesh.command(help='Show tool, infra and API versions')
@click.pass_context
def version(ctx: click.Context):
    click.echo(_version_string)
    mgmt = click_env(CTX_MGROBJ)
    if mgmt:
        click.echo(f'Connected to {click_env(CTX_MGRNAME)} as API: {mgmt.api_version}')

@nvmesh.command(help='Show active environment')
@click.option('-v', '--var')
def env(var=None):
    from xlro.core.util.cli_util import jsonify
    if not var:
        click.echo(jsonify(click.get_current_context().find_object(dict), indent=2))
    else:
        click.echo(click.get_current_context().find_object(dict).get(var))

@nvmesh.command(help='Connect to a management server')
@click.option('-m', '--management', help='Management address as host[:port]')
@click.option('-u', '--user')
@click.option('-p', '--password')
@click.option('--use-tls/--no-use-tls', default=None)
@click.option('--cert', help='Cert file for TLS connection with management')
@click.option('--key', help='Key file for TLS connection with management')
@click.option('--ca', help='CA file for TLS connection with management')
@click.option('--save/--no-save', default=None)
def login(management: str = None,
          user: Optional[str] = None,
          password: Optional[str] = None,
          use_tls: Optional[bool] = None,
          cert: Optional[str] = None,
          key: Optional[str] = None,
          ca: Optional[str] = None,
          save: Optional[bool] = None,
          no_prompt: bool = False):

    set_exit_code(1)
    ctx = click.get_current_context(silent=True)
    ctxobj = ctx.obj if ctx else {}
    cmd_line = ctxobj.get('cmd-line')
    conn = None
    management = management or ctxobj.get(CTX_MGRNAME) or default_management()
    user = user or ctxobj.get(CTX_MGRUSER)
    no_prompt = no_prompt or cmd_line or is_no_prompt()
    while conn is None:
        if not management:
            if no_prompt:
                failure_msg("No default management found. Use: " + ('-m/--management' if cmd_line != 'login' else 'login -m host[:port]'))
                return None
            management = click.prompt('Manager address')
        manager = Manager.instance(endpoints=[f'{ep.partition(":")[0]}:{DEFAULT_PORT}' for ep in management.split(',')])
        while True:
            try:
                conn = manager.connect(user, password=password, use_tls=use_tls, cert=cert, key=key, ca=ca)
                if password:
                    # New credentials
                    if save is None:
                        save = confirm('Do you want these credentials to be saved to disk?', default=False)
                    if save:
                        store_creds(manager.host, user, password)
                break
            except ChangePasswordRequiredError as e:
                failure_msg(str(e.args[0]))
                no_prompt = True
                break
            except ManagementLoginError as e:
                if user:
                    failure_msg(f'Login to {manager.endpoints} as {user} failed.')
            except Exception as e:
                if isinstance(e, ConnectionManagerError):
                    failure_msg(e.args[0])
                else:
                    failure_msg(f'Connection to {manager.endpoints} failed. ({e})')
                management = None
                break
            if no_prompt:
                return None
            user = click.prompt(f'Username at {manager.host}', default=user if user else None)
            password = click.prompt(f'Password for {user} at {manager.host}', hide_input=True)
        if no_prompt:
            break

    if conn:
        set_exit_code(0)
        if not no_prompt:
            click.echo(f'Connected to {manager.host} as {manager.connection.user}')
        ctx = click.get_current_context(silent=True)
        if ctx:
            ctx.obj[CTX_MGROBJ] = manager
            ctx.obj[CTX_MGRNAME] = manager.host
            ctx.obj[CTX_MGRUSER] = manager.connection.user
        build_rest_cmds(nvmesh, manager)
    return manager if conn else None

# Disable for now.  Probably OK as a command-line option, but need to design and test.
# @nvmesh.command()
# def repl():
    # click_repl.repl(click.get_current_context())

def main():
    from argparse import SUPPRESS, _ArgumentGroup, REMAINDER
    from xlro.core.util.cli_util import CLIArgumentParser
    # Use general cli-parser to handle hidden, debugging style arguments
    parser = CLIArgumentParser(require_manager=False, add_help=False, allow_abbrev=False)
    parser.add_argument('-h', '--help', action='store_true', help='Print this help')
    # A fugly hack to preserve -m/--management, which I shouldn't have done in the first place :-(
    for a in parser._actions:
        if a.dest == 'manager':
            # Should we just switch for cli?  Maintail all?
            a.option_strings = ['-m', '--management'] + a.option_strings
            parser._option_string_actions['-m'] = a
            parser._option_string_actions['--management'] = a
            a.default = default_management()
            break
    g = _ArgumentGroup(parser, 'nvmesh CLI options')
    parser._action_groups.insert(1, g)
    g.add_argument('-V', '--version', action='store_true')
    g.add_argument("-y", "--yes", action='store_true', help='Assume "yes" response, without confirming operations')
    g.add_argument("-q", "--quiet", action='store_true', help="Don't echo commands when non interactive")
    g.add_argument('-o', '--output-format', choices = OUTPUT_FORMATS, help='Default output format for "show"')
    g.add_argument("-l", "--limit", help='Default limit for pages of "show" (0 is unlimited)', default=GET_LIMIT)
    g.add_argument("--no-prompt", action='store_true', default=not sys.stdin.isatty() or not sys.stdout.isatty(),
                help='Assume default response without prompt (implied if stdin or stdout is not a TTY)')
    g.add_argument("--fail", action='store_true', help='Exit on error - for script mode')
    g.add_argument("--color", action='store_true', help='Enable color (disabled if stdin is not a TTY)')
    g.add_argument("--human", action='store_true', help='Print error details in more human-readable format')
    g.add_argument("--timer", action='store_true', help='Print elapsed time of commands')
    # -D should be hidden in Debugging
    for ag in parser._action_groups:
        if ag.title == 'Debugging':
            g = ag
            break
    else:
        g = parser
    g.add_argument('-D', '--dump', action='store_true', help='Dump command syntax')
    g.add_argument('--dump-version', help='Dump command syntax for a specific version')
    g.add_argument('-Z', '--detail-dump', action='store_true', help='Dump command syntax with details for API comparison')
    parser.add_argument('command', nargs='?', help='Command to run - if no command, enter interactive mode')
    parser.add_argument('cmd_options', nargs=REMAINDER, help='Use "<command> --help" to see options for a command')
    parser.set_defaults(limit_sourcetypes=[SourceTypes.LOCAL, SourceTypes.MANAGEMENT, SourceTypes.DEFAULT])
    args = parser.parse_args()

    # Log command in command-line mode (when a command is provided)
    if args.command:
        logger.info(f'COMMAND-MODE: {mask_hidden_fields(sys.argv)}')

    # Kobi insisted we get rid of "quit" :-)
    from click_shell._cmd import ClickCmd
    delattr(ClickCmd, 'do_quit')

    # Enable showing command in non-interactive logging
    orig_pre = nvmesh.shell.precmd
    def wrap_pre(line):
        s = line.strip()
        # Log command in interactive mode
        if s and s[0] not in ('#', '!'):
            logger.info(f'INTERACTIVE-MODE: {mask_hidden_fields(s)}')
        # For non-tty input, echo commands to output
        if not click_env('quiet') and not sys.stdin.isatty():
            click.echo('# ' + s)
        # Execute ! commands in shell
        if s and s[0] == '!':
            cmd = s[1:].strip()
            logger.debug(f'EXEC-CMD: {cmd}')
            os.system(cmd)
            line = ''
        # Skip comments
        if s and s[0] == '#':
            line = ''
        return orig_pre(line)
    nvmesh.shell.precmd = wrap_pre

    # Mask login password in history
    orig_post = nvmesh.shell.postcmd
    def mask_login(*args, **kwargs):
        from click_shell._compat import readline
        if readline:
            last_index = readline.get_current_history_length()
            last_item = readline.get_history_item(last_index)
            if last_item and last_item.partition(' ')[0] in ('login', 'user'):
                def mask_pass(match: re.Match):
                    return match.group(1) + ('*' * len(match.group(3)))
                readline.replace_history_item(last_index-1, re.sub('( (-p|--password) *)([^ ]*)', mask_pass, last_item))
        return orig_post(*args, **kwargs)
    nvmesh.shell.postcmd = mask_login

    if args.dump_version:
        mgr = None
    else:
        mgr = args.manager
        if mgr:
            # Try to connect to get api-version.  If -m not specified, then OK to fail
            try:
                # Call login "command" which will prompt for user/password
                login_args = {k:v for k, v in vars(args).items() if k in [p.name for p in login.params]}
                login_args['management'] = ",".join(mgr.endpoints)
                # For explicit command, or help/version - don't go into the prompt loop for login
                login_args['no_prompt'] = any([args.no_prompt, args.command, args.dump, args.detail_dump, args.version, args.help])
                logger.debug(f'LOGIN ARGS: {mask_hidden_fields(login_args)}')
                args.manager = mgr = login.callback(**login_args)
                if not mgr:
                    if login_args['no_prompt']:
                        mgr = args.manager = None
                    else:
                        return 1
            except click.Abort:
                logger.debug('User aborted during login.  Continuing w/o management')
                mgr = args.manager = None
            except Exception as e:
                logger.info(f'Failed to connect to management {mgr} - {repr(e)}')
                if isinstance(e, ConnectionManagerError):
                    click.echo(e.args[0])
                else:
                    click.echo(f'Failed to connect to {mgr}\n{e}')
                return 1

    build_rest_cmds(nvmesh, mgr, args.dump_version)

    if args.command and args.help:
        args.help = False
        args.cmd_options.append('--help')

    if args.dump:
        cmd_dump(nvmesh)
    elif args.detail_dump:
        detail_cmd_dump(nvmesh)
    elif args.help:
        from xlro.core.util.cli_util import snake_to_human
        parser.print_help()
        print()
        print(f'COMMANDS (API version: {mgr.api_version if mgr else RestVersionManager.max_version()})')
        print('  NOTE: The command list is dependent on the API version of the connected Manager.')
        print('        For a list of sub-commands for a Command Group, use <command> --help')
        print('        For help on a specific command or sub-command, use <command> [<subcommand>] ... --help')
        print()
        for cmdname, cmd in sorted(nvmesh.commands.items()):
            help = cmd.get_short_help_str() or f'{snake_to_human(cmdname.replace("-", "_")).title()} command group'
            print(f'  {cmdname:<30s}{help}')
    elif args.version:
        print(_version_string)
        if mgr:
            try:
                print(f'Connected to {args.manager.host} as API: {args.manager.api_version}')
            except:
                pass
    else:
        try:
            _pre_parsed_args.update(vars(args))
            nvmesh(args = [] if args.command is None else ([args.command] + args.cmd_options))
        except SystemExit as e:
            # We want to control exit code
            return e.code or exit_code()
    return 0

def opt_string(opt: click.Parameter):
    from xlro.tools.cli.rest_click import EntityParamType, MappingParamType
    out = ''
    for optname in opt.opts:
        if len(optname) > len(out):
            out = optname
    if getattr(opt, 'is_flag', False):
        pass
    elif isinstance(opt.type, (EntityParamType, MappingParamType)):
        out += ' ' + opt.type.name
    elif isinstance(opt.type, click.types.FuncParamType):
        out += ' ' + opt.type.name.upper()
    elif isinstance(opt.type, click.types.Path):
        out += ' ' + 'PATH'
    elif isinstance(opt.type, click.Choice):
        out += ' ' + '|'.join(opt.type.choices)
    else:
        out += f' {opt.type}'
    if not opt.required:
        out = '[' + out + ']'
    if opt.nargs > 1 or getattr(opt, 'multiple', False):
        out += ' ...'
    return out + ' '

def cmd_dump(cmd, prefix='', group=None):
    ''' dump commands and sub-commands in sorted order (so can be compared between versions) '''
    if isinstance(cmd, click.Group):
        for cmdname, subcmd in sorted(cmd.commands.items()):
            cmd_dump(subcmd, prefix + cmdname + ' ', group=cmd)
    elif isinstance(cmd, click.Command):
        out = prefix
        out += ''.join(sorted([opt_string(o) for o in cmd.params if o.required]))
        out += ''.join(sorted([opt_string(o) for o in cmd.params if not o.required]))
        out += f'# {cmd.help}'
        print(out)
        if cmd.name == 'show' and group:
            print(f'{prefix}default-fields: {", ".join(sorted(group.rest_context.rest_info.kwargs.get("display", ["ID"])))}')
    else:
        print('WTF:', type(cmd), cmd)

def detail_cmd_dump(cmd):
    ''' dump commands and sub-commands and options in detail per API version for API testing '''
    cmd_sets: Dict[str, RestVersionInfo] = {}
    for parsed_ver in sorted(RestVersionManager.versions):
        v_info = RestVersionManager.info_map[parsed_ver]
        v = v_info.version
        build_rest_cmds(nvmesh, None, v)
        cmd_sets[v] = detail_cmd_set(nvmesh)
        for c in sorted(cmd_sets[v] if not v_info.extends else cmd_sets[v] - cmd_sets[v_info.extends]):
            print(f'[API {v}] +{c}')
        if v_info.extends:
            deleted = None
            for c in sorted(cmd_sets[v_info.extends] - cmd_sets[v]):
                if deleted and c.startswith(deleted):
                    continue
                print(f'[API {v}] -{c}')
                deleted = c + ' '

def detail_cmd_set(cmd, prefix='') -> Set[str]:
    ''' get commands and sub-commands and options in detail for API testing '''
    cmds = set()
    if isinstance(cmd, click.Group):
        for cmdname, cmd in sorted(cmd.commands.items()):
            cmds.update(detail_cmd_set(cmd, prefix + cmdname + ' '))
    elif isinstance(cmd, click.Command):
        prefix = prefix.strip()
        cmds.add(prefix)
        for opt, param in sorted([(opt_string(p).replace('[', '').replace(']', ' ?').strip(' -'), p) for p in cmd.params]):
            cmds.add(f'{prefix} {opt} ({param.default})')
    return cmds

if __name__ == '__main__':
    sys.exit(main())
