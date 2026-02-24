# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from builtins import str
from typing import Any, Dict, Optional, List
import os
import tempfile
import ctypes
import click
import click_shell
import click_shell.core as cs_core
from click.types import ParamType, IntParamType
from click.exceptions import UsageError
from click_shell._compat import readline
from shutil import rmtree
from collections import defaultdict, OrderedDict
from glob import glob
from concurrent.futures import ThreadPoolExecutor
from inspect import cleandoc
from enum import Enum
from xlro.tools.nvmesh_edit.contexts import *
from xlro.core.util.block_objects import BlockSet, PSlice
from xlro.tools.nvmesh_edit.io_storage import BlocksetStorage
from xlro.core.entities.journal import SliceRangeTransactions, JournalPage
from xlro.core.entities import *


BLOCK_MD = 'union__nvmeibc_block_dp_ec_data_block_md'
JBLOCK_MD = 'union__jblock_md'


class HelpGroups(Enum):
    Navigation = 0
    Object = 1
    Info = 2
    IO = 3
    Utilities = 4
    Versioning = 5
    Other = 100


def help_group(helpgroup):
    def decorator(cmd):
        cmd.help_group = helpgroup
        return cmd

    return decorator


io_group = click.Group('io')
edit_group = click.Group('edit')


PROMPT_FMTS = {
        Manager: (None, '@{nvmesh_edit._simple_name}'),
        Volume: ('nvmesh_edit', '{volume.name} '),
        BlockSet: ('volume', 'BS @{blockset.vlbs.addr}'),
        Transaction: ('blockset', 'TxId #{tx.tx_id}'),
        JournalPage: ('tx', 'Block #{page.pageref.rolename} @{page.pageref.vlba}'),
        JBLOCK_MD: ('page', 'Metadata'),
        RMBInfo: ('blockset', 'RMBInfo'),
        PSlice: ('blockset', 'Slice #{slice.index_in_bs} @{slice.vlbs.addr}'),
        PageRef: ('slice', 'Block #{page.rolename} @{page.vlba}'),
        BLOCK_MD: ('page', 'MetaData')
    }


SHOW_FMTS = {
        Volume: (None, 'Volume: {volume.name} ({volume.RAIDlevel}) ({volume.blocks} blocks)'),
        Chunk: ('volume', 'Chunk: @{praid.chunk.vlbs}-{praid.chunk.vlbe}'),
        PRaid: ('chunk', 'PRaid: #{praid.stripeIndex} - {praid.dataDisks}D+{praid.parityDisks}P x {praid.chunk.stripeWidth}'),
        BlockSet: ('praid', 'BS: #{blockset.block_set_idx} @{blockset.vlbs.addr}'),
        Transaction: ('blockset', 'Transaction: @txid {tx.tx_id}, @c_uuid {tx.client_uuid}'),
        JournalPage: ('tx', 'Block: #{page.pageref.rolename} @{page.j2d} {page.pageref.blocks.drive.name} : {page.pageref.blocks.dlba_range.lbs.addr}x{page.pageref.blocks.dlba_range.n_blocks}'),
        JBLOCK_MD: ('page', 'Metadata'),
        PSlice: ('blockset', 'Slice: #{slice.index_in_bs} @{slice.vlbs.addr}'),
        PageRef: ('slice', 'Block: #{page.rolename} @{page.vlba} {page.blocks.drive.name} : {page.blocks.dlba_range.lbs.addr}x{page.blocks.dlba_range.n_blocks}'),
        BLOCK_MD: ('page', 'MetaData')
    }


def _format(obj, ctx_map, fmt_map=SHOW_FMTS, sep='\n'):
    try:
        parent, fmt = fmt_map[type(obj)]
    except:
        try:
            parent, fmt = fmt_map[type(obj).__name__]
        except:
            return str(obj)

    if parent:
        if not parent in ctx_map:
            ctx_map[parent] = getattr(obj, parent)
        prefix = _format(ctx_map[parent], ctx_map, fmt_map, sep) + sep
    else:
        prefix = ''
    return prefix + fmt.format(**ctx_map)


def format_ctx(ctx, fmt_map, sep):
    obj = ctx.obj
    ctx_map = {}
    while ctx:
        ctx_map[ctx.command.name] = ctx.obj
        ctx = ctx.parent
    return _format(obj, ctx_map, fmt_map, sep)


def curr_prompt():
    try:
        curr_ctx = click.get_current_context()
        blockset = curr_ctx.find_object(BlockSet)
        is_dirty = blockset.storage.dirty_map.is_dirty if blockset else False
        return format_ctx(curr_ctx, PROMPT_FMTS, ' | ') + (' (*)> ' if is_dirty else ' > ')
    except Exception as e:
        return repr(e) + '>> '


def get_page_coords_by_ctx(ctx):
    while ctx:
        obj = ctx.obj
        if isinstance(obj, PageRef):
            return [(obj.role, obj.pslice.index_in_bs)]
        elif isinstance(obj, JournalPage):
            return [(obj.pageref.role, obj.pageref.pslice.index_in_bs)]  # type: ignore
        elif isinstance(obj, PSlice):
            return [(role, obj.index_in_bs) for role in range(obj.praid.width)]
        elif isinstance(obj, BlockSet):
            return [(role, slice) for role in range(obj.praid.width) for slice in range(BlockSet.SLICE_COUNT)]
        elif isinstance(obj, RMBInfo):
            return ['rmbinfo']

        ctx = ctx.parent

    raise Exception('Unable to get page coordination of current context for auditing')


# Clickshell hard-codes the Shell class, but we need some changes...
class NvckShell(cs_core.ClickShell):
    QUIET_PFX = '%'     # Prefix to commands that were queued internally, and should not be normally echoed
    COMMENT_PFX = '#'
    # ishell = None
    parent_history = None

    def precmd(self, line):
        line = line.strip()
        if line.startswith(self.QUIET_PFX):
            line = line.partition(self.QUIET_PFX)[2]
        elif not self.stdin.isatty():
            print(line)
        if line.startswith(self.COMMENT_PFX):
            # Note: Only handle comment at start of line.  '#' could be valid elsewhere
            line = ''
        if CtxShell.pop_count == 1:
            CtxShell.pop_count = 0
        return super(NvckShell, self).precmd(line)

    def preloop(self):
        # Save parent history, if any
        try:
            if readline.get_current_history_length():
                fd, self.parent_history = tempfile.mkstemp('.nvmesh_edit_hist')
                os.close(fd)
                readline.set_history_length(1000)
                readline.write_history_file(self.parent_history)
                readline.clear_history()
        except:
            pass
        super(NvckShell, self).preloop()

    def _clear_exit(self):
        ''' Clear %exit from cmdqueue. '''
        try:
            self.cmdqueue.remove('%exit')
        except:
            pass

    def postloop(self):
        super(NvckShell, self).postloop()
        # Restore parent history, if any
        try:
            if self.parent_history:
                readline.clear_history()
                readline.read_history_file(self.parent_history)
                os.remove(self.parent_history)
        except:
            pass
        self.parent_history = None
        # Clear cmdqueue.  %exit might have been queued, but we're leaving anyway, so de-queue it.
        self._clear_exit()

    def postcmd(self, stop, line):
        if CtxShell.pop_count:
            # If we're popping, we should clear the queue (potential '%exit' commands)
            self._clear_exit()
        return super(NvckShell, self).postcmd(stop, line) or CtxShell.pop_count > 1

    def do_help(self, arg):
        ''' Show this help '''
        if arg:
            return super(NvckShell, self).do_help(arg)

        click.echo(self._shell_command.name + ': ' + cleandoc(self._shell_command.__doc__))
        header = 'Sub-commands (help <sub-command> for details):'
        click.echo('\n' + header + '\n' + self.ruler * len(header))

        help_groups: Dict[HelpGroups, Dict[str, str]] = defaultdict(dict)
        shell_cmds = self._shell_command.commands
        for func in [f for f in dir(self) if f.startswith('do_')]:
            help_group = HelpGroups.Other
            cmd = func[3:]
            try:
                command = shell_cmds[cmd]
                if command.hidden:
                    continue
                desc = command.short_help
                help_group = getattr(command, 'help_group', HelpGroups.Other)
                assert desc, 'No description found for command {}'.format(str(command))
            except:
                if func in getattr(self, '_ancestor_cmds', []):
                    desc = 'Return to {} context'.format(cmd)
                    help_group = HelpGroups.Navigation
                else:
                    desc = getattr(self, func).__doc__ or ''
                    if cmd == 'break':
                        help_group = HelpGroups.Navigation
            desc = desc.strip().partition('\n')[0]
            help_groups[help_group][cmd] = desc

        for help_group, cmds in sorted(list(help_groups.items()), key=lambda kv: kv[0].value):
            sub_header = '{} commands'.format(help_group.name)
            click.echo(sub_header + '\n' + '-' * len(sub_header))
            for cmd, desc in sorted(cmds.items()):
                click.echo('  {:10} {:.65}'.format(cmd, desc))
            click.echo()

        # Click's help, but we're already in the shell, so usage seems unnecessary
        # click.echo(self._shell_command.get_help(click.get_current_context()))
        return

    def do_break(self, arg):
        ''' Break out of current context '''
        CtxShell.pop_ctx((int(arg) + 1) if arg else 2)
        return True

    def do_exit(self, arg):
        ''' Exit from current command '''
        return super(NvckShell, self).do_exit(arg)

    def do_quit(self, arg):
        ''' Quit program '''
        ctx: Optional[click.Context] = click.get_current_context()
        while ctx:
            try:
                ctx.command.shell.cmdqueue.append('%exit')  # type: ignore[attr-defined]
            except:
                pass
            ctx = ctx.parent
        return True

    try:
        # Only define py command if IPython found (won't be in pyinstaller version)
        from IPython.terminal.embed import InteractiveShellEmbed

        def do_py(self, arg):
            ''' Spawn ipython in the current context '''
            if not NvckShell.ishell:
                from IPython.terminal.embed import InteractiveShellEmbed
                import xlro.core as user_module
                NvckShell.ishell = InteractiveShellEmbed(user_module=user_module)
            py_locals = {}                          # The locals context for ipython
            ctx: Optional[click.Context] = click.get_current_context()
            while ctx:
                if ctx.obj:
                    py_locals[ctx.command.name] = ctx.obj
                ctx = ctx.parent
            NvckShell.ishell.mainloop(local_ns=py_locals)
            return False
    except:
        pass


cs_core.ClickShell = NvckShell


class CtxShell(click_shell.Shell):
    pop_count = 0

    def __init__(self, *args, **kwargs):
        super(CtxShell, self).__init__(*args, **kwargs)
        self.shell._shell_command = self
        self.shell._ancestor_cmds = []

    @classmethod
    def pop_ctx(cls, n):
        # TODO: A better implementation could pop to a named ctx level?
        cls.pop_count = n

    def invoke(self, ctx):
        # Auto-add commands to return to ancestor contexts.
        # I'm doing this dynamically (and resetting when done) in case the hierarchy becomes a graph somehow.
        ancestor_cmds: List[str] = []
        self.shell._ancestor_cmds = ancestor_cmds
        ancestor = ctx.parent
        distance = 1
        while ancestor:
            if isinstance(ancestor.command, CtxShell):
                fname = 'do_%s' % ancestor.command.name
                if not hasattr(self.shell, fname):
                    setattr(self.shell, fname, lambda x, d=distance: CtxShell.pop_ctx(d+1))
                    ancestor_cmds.append(fname)
            ancestor = ancestor.parent
            distance += 1

        # Shell calls super, but we need to replace Shell.invoke() completely, so we jump to Group
        ret = click.Group.invoke(self, ctx)

        if CtxShell.pop_count == 1 or self.shell.cmdqueue or (not ctx.protected_args and not ctx.invoked_subcommand):
            # Set this to None so that it doesn't get printed out in usage messages
            ctx.info_name = None

            # Set the context on the shell
            self.shell.ctx = ctx

            # Start up the shell
            ret = self.shell.cmdloop()

        if CtxShell.pop_count:
            CtxShell.pop_count -= 1
        for fname in ancestor_cmds:
            delattr(self.shell, fname)
        self.shell._ancestor_cmds = []

        return ret


def push_context_obj(ctx, obj):
    ctx.obj = obj


class UnderConstruction(UsageError):
    msg = 'This method is currently under construction.'

    def __init__(self, ctx=None):
        super(UnderConstruction, self).__init__(self.msg, ctx)


class LBAType(ParamType):
    name = "lba"

    def convert(self, value, param, ctx):
        try:
            return int(value, 0)
        except ValueError:
            self.fail("{} is not a valid LBA".format(value), param, ctx)

    def __repr__(self):
        return "LBA"


class IDXType(ParamType):
    name = "idx"
    step_into_char = '-'

    def convert(self, value, param, ctx):
        try:
            return int(value)
        except ValueError:
            try:
                assert value == self.step_into_char
                return value
            except AssertionError:
                self.fail("{} is not a valid IDX".format(value), param, ctx)

    def __repr__(self):
        return "IDX"


class TildePath(click.Path):
    # TODO: support multiple files list (i.e. /tmp/data_0 /tmp/metadata_0) in addition to regex
    def convert(self, value, param, ctx):
        return super(TildePath, self).convert(os.path.expanduser(value), param, ctx)


def cmd_error(msg):
    click.echo(msg, err=True)
    raise click.Abort()


def role2rolename(role, praid):
    if role < praid.dataDisks:
        return 'D' + str(role)
    else:
        return ('P' if praid.RAIDlevel == PRaid.RaidLevels.EC else 'M') + str(role - praid.dataDisks)


def get_tx_roles(blockset, transaction):
    return sorted([role2rolename(blockset.drive2role(jre.journal_range.drive), blockset.praid)
            for jre in transaction.journal_entries])


def get_jpage_list(blockset, journal_entries):
    return sorted([(jpage, role2rolename(blockset.drive2role(jpage.journal_entry.journal_range.drive), blockset.praid))
                   for jre in journal_entries for jpage in jre.journal_pages], key=lambda i: (i[1], i[0].page_index))


def get_tx_list(slice_range, reload=False):
    if not reload:
        try:
            return getattr(slice_range, 'transactions')
        except AttributeError:
            pass

    tx_list = SliceRangeTransactions(slice_range).slice_range_transactions
    setattr(slice_range, 'transactions', tx_list)
    return tx_list


def format_obj(d, indent=0, depth=0):
    ''' Recursive pretty printer for objects (including ctypes) '''
    result = ''
    i_spaces = ' ' * (indent * depth)
    eol = '\n' if indent else ' '
    if isinstance(d, dict):
        result += '{' + eol
        keys = list(d.keys()) if isinstance(d, OrderedDict) else sorted(d.keys())
        for key in keys:
            result += i_spaces + str(key) + ' : '
            result += format_obj(d[key], indent, depth+1)
        result += i_spaces[:-indent] + '}' + eol
    elif isinstance(d, list):
        result += '[' + eol
        for item in d:
            result += format_obj(item, indent, depth+1)
        result += i_spaces[:-indent] + ']' + eol
    elif hasattr(d, '_fields_'):        # For cstructs
        result += format_obj(OrderedDict({f[0]: getattr(d, f[0]) for f in d._fields_}), indent, depth or 1)
    elif getattr(d, '__dict__', None):
        result += format_obj(d.__dict__, indent, depth or 1)
    else:
        result += str(d) + eol
    return result


def print_manager_volumes(manager):
    volumes = manager.volumes
    click.echo('Existing volumes on {}:'.format(manager))
    for i, v in enumerate(volumes):
        click.echo("{}) name: {}        raid: {}        status: {}".format(i, v.name, v.RAIDlevel, v.status))


def path_completer(ctx, args, incomplete):
    return glob(os.path.expanduser(incomplete + '*'))


def volume_completer(ctx, args, incomplete):
    ctx = click.get_current_context()       # Weird, but get's passed a strange context
    try:
        return [v.name for v in ctx.find_object(Manager).volumes if v.name.startswith(incomplete)]
    except Exception as e:
        pass


_drive_names = None


def drive_completer(ctx, args, incomplete):
    ctx = click.get_current_context()       # Weird, but get's passed a strange context
    global _drive_names
    if _drive_names is None:
        targets = ctx.find_object(Manager).targets
        with ThreadPoolExecutor(len(targets)) as executor:
            drives_by_target = executor.map(lambda t: t.drives, targets)
        _drive_names = [d.name for dlist in drives_by_target for d in dlist if not d.excluded]
    try:
        return [d for d in _drive_names if d.startswith(incomplete)]
    except Exception as e:
        pass


def ctype_to_ptype(obj: Any) -> Any:
    if isinstance(obj, (ctypes.Array, list)):
        ret = [ctype_to_ptype(e) for e in obj]
        # pretty print byte arrays
        if hasattr(obj, "_type_") and obj._type_ == ctypes.c_byte:  # type: ignore
            ret = str(bytearray(ret))  # type: ignore
        return ret

    if isinstance(obj, ctypes._Pointer):  # type: ignore
        return ctype_to_ptype(obj.contents) if obj else None

    if isinstance(obj, ctypes._SimpleCData):
        return ctype_to_ptype(obj.value)

    if isinstance(obj, int):
        ret = int(obj)  # type: ignore
        if isinstance(ret, int):
            # as json don't support long just dump it as a string
            return str(ret)
        return ret

    if isinstance(obj, (bool, int, float, str)):
        return obj

    if obj is None:
        return obj

    if isinstance(obj, (ctypes.Structure, ctypes.Union)):
        result = {}
        anonymous = getattr(obj, '_anonymous_', [])

        for spec in getattr(obj, '_fields_', []):
            key = spec[0]
            value = getattr(obj, key)

            # private fields don't encode
            if key.startswith('_'):
                continue

            if key in anonymous:
                result.update(ctype_to_ptype(value))
            else:
                result[key] = ctype_to_ptype(value)

        return result


def bs_storage_cleanup():
    try:
        click.get_current_context().find_object(BlockSet).storage.delete()
    except Exception as e:
        click.echo('BS CLEANUP: ' + repr(e), err=True)


def all_storage_cleanup():
    try:
        rmtree(BlocksetStorage.storage_root)
    except Exception as e:
        click.echo('NVMESH_EDIT CLEANUP: ' + repr(e), err=True)


def io_geometry(ctx):
    ''' get (blockset, slices, roles) from context '''
    blockset = ctx.find_object(BlockSet)
    pslice = ctx.find_object(PSlice)
    pageref = ctx.find_object(PageRef)
    return blockset, pslice.index_in_bs if pslice else None, pageref.role if pageref else None


def create_raw(strct):
    buffer = ctypes.create_string_buffer(ctypes.sizeof(strct))
    ctypes.memmove(buffer, ctypes.addressof(strct), ctypes.sizeof(strct))
    return buffer.raw


class printStatuses:
    SUCCESS = 'green'
    FAILURE = 'red'
    WARNING = 'yellow'


def click_print(msg, status=None):
    from click import secho
    if status == printStatuses.WARNING:
        msg = 'WARNING: ' + msg
    secho(msg, err=status == printStatuses.FAILURE, fg=status)
