#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function

import logging
from builtins import hex, str, range
from pipes import quote
from functools import update_wrapper
from xlro.core.util.general_utils import get_debug_di
from xlro.core.util.cli_util import CLIArgumentParser, jsonify, str2entity
from xlro.core.util.block_objects import PageData
from xlro.core.util.scanner import *
from xlro.core.util.ssh import temp_dir
from xlro.tools.nvmesh_edit.util import *

logger = logging.getLogger("nvmesh_edit")

def format_failure(err):
    ''' Can be used to massage various errors '''
    if isinstance(err, click.Abort):
        # Error has presumably been shown
        return
    msg = f'Error: {err}'
    # Don't show unhelpful exception types
    if not isinstance(err, (Exception, AssertionError)):
        msg += f' [{type(err).__name__}]'
    click.echo(msg, err=True)

def no_trace(f):
    ''' Wrapper/Decorator to hide stack traces and just echo the error. '''
    def exception_handler(*args, **kwargs):
        try:
            f(*args, **kwargs)
        except click.Abort:
            raise
        except Exception as e:
            ctx = click.get_current_context()
            logger.info(f'Exception while running command: {ctx.command.name if ctx else "Unknown"}', exc_info=True)
            format_failure(e)
            raise click.Abort()
    return update_wrapper(exception_handler, f)

# A bit fugly, but much easier than adding a decorator 30 times or finding all the places to send cls=MyCommand
click.Command.invoke = no_trace(click.Command.invoke)

def context_group(parent, ioable=True, editable=True, **kwargs):
    ''' A decorator to create a click-group from a function which returns a context. '''

    def decorator(f):
        def context_f(*args, **kwargs):
            ctx = click.get_current_context()
            ctx_obj = f(*args, **kwargs)
            if ctx_obj:
                logger.debug(f'Got into ctx: {ctx_obj}')
                push_context_obj(ctx, ctx_obj)

        f.__name__ = f.__name__.lstrip('_')
        cf = update_wrapper(context_f, f)
        cmdname = kwargs.get('name', f.__name__)
        hist_file = os.path.join(os.path.expanduser('~'), '.nvmesh-edit-{}-history'.format(cmdname))
        newkwargs = dict(kwargs, cls=CtxShell, invoke_without_command=True,
                prompt=curr_prompt, hist_file=hist_file)
        cmd = click.decorators.group(**newkwargs)(cf)
        if parent:
            parent.add_command(cmd)
        cmd.add_command(show_ctx_object)
        if ioable:
            for iocmd in io_group.list_commands(None):                          # type: ignore[arg-type]
                cmd.add_command(io_group.get_command(None, iocmd), iocmd)       # type: ignore[arg-type]
        if editable:
            for editcmd in edit_group.list_commands(None):                          # type: ignore[arg-type]
                cmd.add_command(edit_group.get_command(None, editcmd), editcmd)       # type: ignore[arg-type]
        setattr(cmd, 'help_group', HelpGroups.Navigation)
        return cmd

    return decorator


# Don't need unless we mix context_groups with regular shells
setattr(click.Group, 'context_group', context_group)


@help_group(HelpGroups.IO)
@io_group.command()
@click.pass_context
def read(ctx):
    ''' Read data from NVMesh to local buffer '''
    blockset, slice, role = io_geometry(ctx)
    if isinstance(ctx.obj, RMBInfo):
        blockset.storage.read_rmbinfo()
    elif isinstance(ctx.obj, JournalPage) or type(ctx.obj).__name__ == JBLOCK_MD:
        jpage = ctx.find_object(JournalPage)
        blockset.storage.read_jpage(jpage, role)
    else:
        blockset.storage.read(None if slice is None else [slice], None if role is None else [role], override=True)

    blockset.storage.do_audit(ctx)


@help_group(HelpGroups.Info)
@io_group.command()
@click.pass_context
def topo(ctx):
    ''' Show topology '''
    volume = ctx.find_object(Volume)
    blockset = ctx.find_object(BlockSet)

    # refresh volume to refresh segments status
    volume.get_property('status', source=SourceTypes.MANAGEMENT, no_cache=True)

    segments = blockset.praid.get_dataSegments()
    ordered_segments = segments[blockset.rotational_steps:] + segments[:blockset.rotational_steps]

    for idx, seg in enumerate(ordered_segments):
        click.echo("{} {} {} {}".format("D{}".format(idx) if idx < blockset.praid.dataDisks
                                        else "P{}".format(idx - blockset.praid.dataDisks),
                                        seg.drive.name, seg.drive.target.name, seg.status))


@help_group(HelpGroups.IO)
@io_group.command()
@click.pass_context
def write(ctx):
    ''' Write data from local buffer to NVMesh '''
    blockset, slice, role = io_geometry(ctx)
    if isinstance(ctx.obj, RMBInfo):
        blockset.storage.write_rmbinfo()
    elif isinstance(ctx.obj, JournalPage) or type(ctx.obj).__name__ == JBLOCK_MD:
        jpage = ctx.find_object(JournalPage)
        blockset.storage.write_jpage(jpage)
    else:
        blockset.storage.write(None if slice is None else [slice], None if role is None else [role])

    blockset.storage.commit_audit(ctx, ctx.find_object(Manager).mgmt_host)


@help_group(HelpGroups.Info)
@io_group.command()
@click.option('-r', '--reload', is_flag=True)
@click.option('-d', '--descending', is_flag=True)
@click.option('-s', '--sort-by', required=False, default='tx_id', type=click.Choice(Transaction.SORT_FIELDS))
@click.pass_context
def transactions(ctx, reload, descending, sort_by):
    ''' Show journal transactions of current context '''
    if 'Erasure Coding' not in ctx.find_object(Volume).RAIDlevel:
        cmd_error('No journals in non EC volumes')
    blockset, slice, role = io_geometry(ctx)
    tx_list = get_tx_list(blockset if slice is None else blockset.slice(slice), reload)
    tx_list.sort(key=lambda tx: getattr(tx, sort_by), reverse=descending)
    ctx_tx_list = [(idx, tx) for idx, tx in enumerate(tx_list)
                   if role is None or role2rolename(role, blockset.praid) in get_tx_roles(blockset, tx)]
    for idx, tx in ctx_tx_list:
        click.echo('{}) {} {}'.format(idx, tx, get_tx_roles(blockset, tx)))


@help_group(HelpGroups.Object)
@edit_group.command('get')
@click.argument('prop', required=False)
@click.argument('fmt', required=False)
@click.pass_context
def edit_get(ctx, prop, fmt):
    ctx.forward(prop_handler)


@help_group(HelpGroups.Object)
@edit_group.command('set')
@click.argument('prop', required=False)
@click.argument('value', required=False)
@click.pass_context
def edit_set(ctx, prop, value):
    ctx.forward(prop_handler)
    blockset, slice, role = io_geometry(ctx)
    if type(ctx.obj).__name__ == JBLOCK_MD:
        # In case of MD, save the data
        jpage = ctx.find_object(JournalPage)
        path_args = {'role': jpage.pageref.role, 'c_uuid': jpage.journal_entry.journal_range.client_uuid,
                     'tx_id': jpage.tx_id, 'j2d': jpage.j2d}
        data = blockset.storage.get(**path_args).data
        blockset.storage.set_jpage(PageData(data, create_raw(ctx.obj)), **path_args)
    elif type(ctx.obj).__name__ == BLOCK_MD:
        path_args = {'slice': slice, 'role': role}
        data = blockset.storage.get(**path_args).data
        blockset.storage.set_page(PageData(data, create_raw(ctx.obj)), **path_args)
    elif isinstance(ctx.obj, RMBInfo):
        # In case of RMBInfo, save lock under relevant role
        role_name = prop.split('.')[0]
        role_idx = sorted(list(ctx.obj.__dict__.keys()), key=lambda x: (x[0], int(x[1:]))).index(role_name)
        blockset.storage.set_rmbinfo(role_idx, ctx.obj[role_name])

    blockset.storage.do_audit(ctx, prop, value)


@help_group(HelpGroups.Versioning)
@io_group.command('versions')
@click.pass_context
def versions(ctx):
    ''' List available versions for restore '''
    click.echo([str(v) for v in ctx.find_object(BlockSet).storage.versions()])


@help_group(HelpGroups.IO)
@io_group.command('import')
@click.argument('from_paths', type=TildePath(), autocompletion=path_completer, nargs=-1)
@click.pass_context
def import_cmd(ctx, from_paths):
    ''' Import data from path into internal buffer '''
    blockset, slice, role = io_geometry(ctx)
    for path in from_paths:
        if isinstance(ctx.obj, RMBInfo):
            blockset.storage.import_rmbinfo(path)
        elif isinstance(ctx.obj, JournalPage) or type(ctx.obj).__name__ == JBLOCK_MD:
            jpage = ctx.find_object(JournalPage)
            blockset.storage.import_jpage(path, jpage.pageref.pslice.index_in_bs, jpage.pageref.role,
                                          jpage.journal_entry.journal_range.client_uuid, jpage.tx_id)
        else:
            blockset.storage.import_geometry(path, slice, role)

    blockset.storage.do_audit(ctx, ' '.join(from_paths))


@help_group(HelpGroups.IO)
@io_group.command('export')
@click.argument('to_path', type=TildePath(writable=True), autocompletion=path_completer)
@click.pass_context
def export_cmd(ctx, to_path):
    ''' Export data from internal buffer to path '''
    blockset, slice, role = io_geometry(ctx)
    if isinstance(ctx.obj, RMBInfo):
        blockset.storage.export_rmbinfo(to_path)
    elif isinstance(ctx.obj, JournalPage) or type(ctx.obj).__name__ == JBLOCK_MD:
        jpage = ctx.find_object(JournalPage)
        blockset.storage.export_jpage(to_path, jpage.pageref.role, jpage.journal_entry.journal_range.client_uuid,
                                      jpage.tx_id, jpage.j2d)
    else:
        blockset.storage.export_geometry(to_path, slice, role)


@help_group(HelpGroups.Info)
@io_group.command('dirty')
@click.pass_context
def print_dirty(ctx):
    ''' Show dirty flags '''
    blockset = ctx.find_object(BlockSet)
    dirty = blockset.storage.dirty()
    if not dirty:
        click.echo('No changes were made')
    else:
        click.echo('rmbinfo is {}'.format('dirty' if dirty.rmbinfo else 'clean'))
        click.echo('Blockset page map:')
        for slice in range(blockset.SLICE_COUNT):
            click.echo('-' * ((blockset.praid.width * 4) + 1))
            click.echo('| ' + ' | '.join(['D' if dirty.get(slice, role) else 'C' for role in range(blockset.praid.width)]) + ' |')

        click.echo('-' * ((blockset.praid.width * 4) + 1))

def show_pslice_context(pslice: PSlice, role: int):
    praid: PRaid = pslice.blockset().praid
    volume = praid.volume

    for i, blockrange in enumerate(pslice.ordered_content):
        d_r = blockrange.dlba_range
        mark = '*' if i == role else ' '
        dlba_str = f'{blockrange.drive.target.name}:{blockrange.drive.name} @{d_r.lbs.addr} {d_r.n_blocks}x{d_r.blockSize}'
        vlba_addr = pslice.vlbs.addr + (i * volume.snake if praid.RAIDlevel == PRaid.RaidLevels.EC else 0)
        vlba_str = f'{volume.name} @{vlba_addr}'

        if i < praid.dataDisks:
            vlba = Volume.LBA(vlba_addr)
        else:
            vlba = pslice.vlbs
        chunk, clba = volume.get_clba(vlba)
        praid, plba = chunk.get_plba(clba)
        plba_str = f' RAID: {volume.chunks.index(chunk)}, {chunk.pRaids.index(praid)} @{plba}'

        if i < praid.dataDisks:
            click.echo(f'{mark} [D{i}] DLBA: {dlba_str}, VLBA: {vlba_str} {plba_str}')
        elif praid.RAIDlevel == PRaid.RaidLevels.EC:
            click.echo(f'{mark} [P{i-praid.dataDisks}] DLBA: {dlba_str} {plba_str}')
        else: # mirror
            click.echo(f'{mark} [M{i-praid.dataDisks}] DLBA: {dlba_str}, VLBA: {vlba_str} {plba_str}')

    return

@help_group(HelpGroups.Info)
@click.command('show')
@click.option('-s', '--source')
@click.option('-d', '--deep', is_flag=True)
@click.option('-j', '--json', '_json', is_flag=True)
@click.option('-a', '--addresses', is_flag=True, help='Show addresses for slice context')
@click.argument('prop', required=False)
@click.pass_context
def show_ctx_object(ctx, source, deep, addresses, prop, _json):
    ''' A generic show command to be used in all contexts '''
    # Default to show full context
    if addresses:
        bs, slice_ndx, role = io_geometry(ctx)
        click.echo(f'bs: {bs}, slice: {slice_ndx}, role: {role}')
        if slice_ndx is None:
            cmd_error('Invalid context. You must be in a slice/block context to show addresses')
        show_pslice_context(bs.slice(slice_ndx), role)
        click.echo()
        return

    if not prop and not _json:
        click.echo(format_ctx(ctx, SHOW_FMTS, '\n'))
        return

    obj = ctx.obj
    if not obj:
        click.echo('No such object')
        return

    if prop:
        obj = obj.get_property(prop, source) if isinstance(obj, BaseEntity) else getattr(obj, prop)

    class ContextEncoder(json.JSONEncoder):
        def default(self, obj):
            try:
                super().default(obj)
            except:
                if isinstance(obj, BaseEntity):
                    return obj.key()
                try:
                    return obj.__dict__
                except:
                    return str(obj)

    # if _json and isinstance(obj, BaseEntity):
        # obj = obj.to_dict(shallow=not deep)
    click.echo(json.dumps(obj, cls=ContextEncoder, indent=2) if _json else str(obj))


@help_group(HelpGroups.Info)
@io_group.command('show_audit')
@click.pass_context
def show_audit(ctx):
    ''' Show audit records '''
    for audit_rec in ctx.find_object(BlockSet).storage.get_audit(ctx):
        click.echo(audit_rec)


@context_group(None, name='nvmesh_edit', ioable=False, editable=False)
@click.pass_context
def nvmesh_edit_app(ctx):
    """ Top level shell """
    ctx.call_on_close(all_storage_cleanup)
    try:
        # It may already be set via cli_util options...
        mgr = Manager.get_manager()
        mgr.connect()
        setattr(mgr, '_simple_name', mgr.live_host.partition('.')[0])
        return mgr
    except:
        pass

    manager = click.prompt('Manager')
    try:
        mgr = str2entity(manager, Manager) #Manager.instance(endpoints=manager.split(','))
        setattr(mgr, '_simple_name', manager.rpartition('@')[2])
        assert mgr.live_host, 'No live host was found for manager'
        return mgr
        # push_context_obj(ctx, Manager.instance(endpoints=manager.split(',')), 'Manager:{}'.format(manager))
    except Exception as e:
        cmd_error('Failed to connect to: {}. Please check credentials and connectivity'.format(manager))


@help_group(HelpGroups.Info)
@nvmesh_edit_app.command('list')
@click.pass_context
def list_manager(ctx):
    ''' List volumes '''
    manager = ctx.find_object(Manager)
    print_manager_volumes(manager)


@nvmesh_edit_app.context_group(ioable=False, editable=False)
@click.argument('name', autocompletion=volume_completer)
@click.pass_context
def volume(ctx, name):
    ''' Volume context shell '''
    mgr = ctx.find_object(Manager)
    vol = Volume.instance(name=name)
    if vol in mgr.volumes:
        return vol

    cmd_error('No volume "{}" found.'.format(name))


@help_group(HelpGroups.Info)
@volume.command('list')
@click.pass_context
def list_volume(ctx):
    ''' list available volumes '''
    _volume = ctx.find_object(Volume)
    click.echo('Existing pRaids on {}:'.format(_volume))
    for i, _chunk in enumerate(_volume.chunks):
        click.echo('Chunk {}:'.format(i))
        for j, _praid in enumerate(_chunk.pRaids):
            click.echo('    {}) {}'.format(j, _praid.uuid))


@help_group(HelpGroups.Navigation)  # type: ignore
@volume.command(context_settings=dict(ignore_unknown_options=True))
@click.argument('lba', type=LBAType())
@click.argument('subcmd', nargs=-1)
@click.pass_context
def vlba(ctx, lba, subcmd):
    ''' Shortcut to block context of a logical block address '''
    vol = ctx.find_object(Volume)
    if not vol:
        cmd_error('No Volume in context!')

    pslice = vol.get_pslice(Volume.LBA(lba))
    lba_cmd = '%blockset {} slice {} page {}'.format(
            lba, pslice.index_in_bs, (lba - pslice.vlbs.addr) // vol.snake)
    if subcmd:
        lba_cmd += ' ' + ' '.join([quote(s) for s in subcmd])

    ctx.parent.command.shell.cmdqueue.append(lba_cmd)
    if ctx.parent.invoked_subcommand == ctx.command.name:
        # If we're part of a chain, (we are parent.invoked_subcommand), we want parent to execute
        # the "shortcut" we enqueued, but then NOT continue the loop.
        ctx.parent.command.shell.cmdqueue.append('%exit')

@help_group(HelpGroups.Navigation)
@nvmesh_edit_app.command(context_settings=dict(ignore_unknown_options=True))
@click.argument('drive', autocompletion=drive_completer)
@click.argument('addr', type=LBAType())
@click.argument('subcmd', nargs=-1)
@click.pass_context
def dlba(ctx, drive, addr, subcmd):
    ''' Shortcut to block context based on physical block address '''
    pslice, role = PSlice.locate_dlba(Drive.instance(name=drive), addr)
    bs = pslice.blockset()
    dlba_cmd = '%volume {} blockset {} slice {} page {}'.format(
            pslice.praid.chunk.name, bs.vlbs.addr, pslice.index_in_bs, role)
    if subcmd:
        dlba_cmd += ' ' + ' '.join([quote(s) for s in subcmd])

    ctx.parent.command.shell.cmdqueue.append(dlba_cmd)
    if ctx.parent.invoked_subcommand == ctx.command.name:
        ctx.parent.command.shell.cmdqueue.append('%exit')

@help_group(HelpGroups.Utilities)
@io_group.command()
@click.pass_context
def cmp_blocks(ctx):
    ''' Compare blocks commands '''
    from xlro.core.util import compare_block_util
    blockset, slice, _ = io_geometry(ctx)
    client = Client.local_client()
    blockset.storage.read(None if slice is None else [slice])
    if slice is None:
        slice_dir = blockset.storage.concat_blockset_segments_files()
        chunk, clba = blockset.volume.get_clba(blockset.vlbs)
    else:
        slice_dir = os.path.join(blockset.storage.dir, str(slice))
        chunk, clba = blockset.volume.get_clba(blockset.slice(slice).vlbs)

    res = compare_block_util.rcmp_blocks(slice_dir, client.name, blockset.praid.dataDisks, blockset.praid.parityDisks,
                                         chunk.get_plba(clba)[1], get_debug_di(client, blockset.volume))
    try:
        # TODO: since the result should be in a json format, we can do better presenting the output of cmp_blocks
        click.echo(res.split('***')[2])
    except IndexError:
        raise Exception('cmp-blocks did not run well. returned: {}'.format(res))


@volume.context_group(editable=False)
@click.argument('lba', type=LBAType())
@click.pass_context
def _blockset(ctx, lba):
    ''' BlockSet context shell '''
    vol = ctx.find_object(Volume)
    vlba = Volume.LBA(lba)
    blockset = BlockSet.vlba2block_set(vol, vlba)
    setattr(blockset, 'volume', vol)
    setattr(blockset, 'vlba', vlba)
    setattr(blockset, 'storage', BlocksetStorage(blockset))
    ctx.call_on_close(bs_storage_cleanup)
    return blockset


@help_group(HelpGroups.Versioning)
@_blockset.command('backup')
@click.argument('name')
@click.pass_context
def backup(ctx, name):
    ''' Create a named backup of the current blockset workspace '''
    blockset = ctx.find_object(BlockSet)
    blockset.storage.read()
    blockset.storage.backup(name)


@help_group(HelpGroups.Versioning)
@_blockset.command('restore')
@click.argument('name')
@click.pass_context
def restore(ctx, name):
    ''' Restore current blockset workspace from a named backup '''
    blockset = ctx.find_object(BlockSet)
    blockset.storage.restore(name)
    blockset.storage.do_audit(ctx, name)


@_blockset.context_group(editable=False)
@click.argument('idx', type=IDXType())
@click.pass_context
def slice(ctx, idx):
    ''' Slice context shell '''
    bs = ctx.find_object(BlockSet)
    # JW: Very confusing.  Once in BS context, I'd think Slice # would be relative to BS...
    # As it is, slice could be out of BS!
    return bs.slice(idx) if idx != IDXType.step_into_char else bs.volume.get_pslice(bs.vlba)


@slice.context_group(editable=False)
@click.argument('pageref', required=False)
@click.pass_context
def page(ctx, pageref):
    ''' Logical block context shell '''
    pslice = ctx.find_object(PSlice)
    role = pslice.praid.page_role_to_index(pageref)
    return PageRef(pslice, role)


@page.context_group()
@click.pass_context
def md(ctx):
    ''' Metadata context shell '''
    blockset, slice, role = io_geometry(ctx)
    no_md_drives = [seg.drive.name for seg in blockset.content if not seg.drive.metadata]
    if no_md_drives:
        cmd_error(f"Unable to enter MD context - The following drives do not support MD: {no_md_drives}")
    md_struct = Client.local_client().host.get_ctype(BLOCK_MD.partition('__')[-1])  # This is cached, so harmless
    return md_struct.from_buffer(bytearray(blockset.storage.get(slice=slice, role=role).metadata))

@help_group(HelpGroups.Utilities)
@page.command()
@click.pass_context
def status(ctx):
    ''' Check block status '''
    blockset, slice, role = io_geometry(ctx)
    page = ctx.find_object(PageRef)
    page_data = blockset.storage.get(slice=slice, role=role)
    try:
        status = page_data.get_page_status(rlba=page.vlba,
                                           dbg_di=get_debug_di(Client.local_client(), blockset.volume),
                                           is_parity=page.rolename[0] == 'P')
        click_print(f'Block status: {status}', printStatuses.SUCCESS if status in ['OK', 'VIRGIN_DATA'] else printStatuses.FAILURE)
    except:
        click_print("Current nvmesh version does not support block status check. ", printStatuses.WARNING)

@help_group(HelpGroups.Utilities)
@slice.command()
@click.argument('pages', required=True, nargs=-1)
@click.pass_context
def fix(ctx, pages):
    ''' Fix slice command '''
    from xlro.core.util import compare_block_util
    if ctx.find_object(Volume).RAIDlevel not in ["Mirrored RAID-1", "Striped & Mirrored RAID-10", "Erasure Coding"]:
        cmd_error(f'Unable to fix slice in {ctx.find_object(Volume).RAIDlevel} volumes')
    blockset, slice, _ = io_geometry(ctx)
    if len(pages) > blockset.praid.parityDisks:
        cmd_error(f'Max roles to fix: {blockset.praid.parityDisks}')
    client = Client.local_client()
    slice_dir = os.path.join(blockset.storage.dir, str(slice))
    blockset.storage.read([slice])
    roles = [blockset.praid.page_role_to_index(page_idx) for page_idx in pages]
    rec_bmp = 2 ** blockset.praid.width - 1
    for role in roles:
        rec_bmp ^= 1 << role

    chunk, clba = blockset.volume.get_clba(blockset.slice(slice).vlbs)
    lpath = compare_block_util.reconstruct_blocks(hex(rec_bmp), slice_dir, client.name, blockset.praid.dataDisks,
                                                  blockset.praid.parityDisks, chunk.get_plba(clba)[1],
                                                  get_debug_di(client, blockset.volume))
    for role in roles:
        blockset.storage.import_geometry(lpath, slice, role)

    rmtree(lpath)
    blockset.storage.do_audit(ctx, ' '.join(pages))


@help_group(HelpGroups.Utilities)
@md.command()
@click.pass_context
def fix_edic(ctx):
    ''' Fix MD EDIC '''
    blockset, slice, role = io_geometry(ctx)
    page = ctx.find_object(PageRef)
    role_prefix = page.rolename[0]
    d_or_p = getattr(ctx.obj, role_prefix)
    old_edic = getattr(d_or_p, 'edic')
    page_data = blockset.storage.get(slice=slice, role=role)
    try:
        if page_data.is_never_written():
            cmd_error("block was never written, fix-edic is meaningless")
    except:
        click.echo("Current nvmesh version does not support is_never_written check. "
                   "The following edic might be shown as wrong because this block was never written!", printStatuses.WARNING)
    new_edic = page_data.calc_edic(page.vlba, get_debug_di(Client.local_client(), blockset.volume),
                                   role_prefix == 'P')
    setattr(d_or_p, 'edic', new_edic)
    blockset.storage.set_page(PageData(page_data.data, create_raw(ctx.obj)), slice=slice, role=role)
    click.echo(f'Existing edic {old_edic} is correct. Not changing edic.' if old_edic == new_edic else
               f'Old edic: {old_edic}\nSetting new edic: {new_edic}')
    blockset.storage.do_audit(ctx)


@_blockset.context_group()
@click.pass_context
def rmbinfo(ctx):
    ''' BlockSet RAM info context shell '''
    blockset = ctx.find_object(BlockSet)
    return ctx.find_object(RMBInfo) or RMBInfo(blockset, blockset.storage.read_rmbinfo())


@_blockset.context_group(ioable=False, editable=False)
@click.argument('idx', type=IntParamType())
@click.pass_context
def tx(ctx, idx):
    ''' Journal transaction context '''
    if 'Erasure Coding' not in ctx.find_object(Volume).RAIDlevel:
        cmd_error('No journals in non EC volumes')
    blockset = ctx.find_object(BlockSet)
    try:
        return get_tx_list(blockset)[idx]
    except IndexError:
        cmd_error('No such idx {} in transactions list'.format(idx))


@help_group(HelpGroups.Info)
@tx.command('list')
@click.pass_context
def list_jpages(ctx):
    ''' List journal pages '''
    blockset = ctx.find_object(BlockSet)
    jres = ctx.find_object(Transaction).journal_entries
    for idx, (jpage, role) in enumerate(get_jpage_list(blockset, jres)):
        click.echo('{}) {} Role: {}'.format(idx, repr(jpage), role))


@tx.context_group(name='page', editable=False)
@click.argument('idx')
@click.pass_context
def jpage(ctx, idx):
    ''' Journal Entry Page reference '''
    volume = ctx.find_object(Volume)
    blockset = ctx.find_object(BlockSet)
    tx = ctx.find_object(Transaction)
    try:
        role = volume.chunks[0].pRaids[0].page_role_to_index(idx)
        jpage, _ = get_jpage_list(blockset, tx.journal_entries)[role]
        pslice, role = PSlice.locate_dlba(jpage.drive, jpage.j2d)
        blockset.storage.read_jpage(jpage, role)
        setattr(jpage, 'pageref', PageRef(pslice, role))
        return jpage
    except IndexError:
        cmd_error('No such idx {} in journal pages list for current transaction'.format(idx))


@jpage.context_group(name='md')
@click.pass_context
def jmd(ctx):
    ''' Journal Metadata context shell '''
    blockset = ctx.find_object(BlockSet)
    jpage = ctx.find_object(JournalPage)
    jmd_struct = Client.local_client().host.get_ctype(JBLOCK_MD.partition('__')[-1])
    return jmd_struct.from_buffer(bytearray(blockset.storage.get(slice=jpage.pageref.pslice.index_in_bs, role=jpage.pageref.role).metadata))


@click.command()
@click.argument('prop', required=False)
@click.argument('value', required=False)
@click.argument('fmt', required=False)
@click.pass_context
def prop_handler(ctx, prop, value, fmt):
    obj = ctx.obj
    try:
        oldvalue = obj
        # Get sub-object, if needed
        # TODO: Need a more pythonic way, plus support [n] or {x}, etc.
        remaining = prop
        while remaining:
            prop, _, remaining = remaining.partition('.')
            obj = oldvalue
            oldvalue = getattr(oldvalue, prop)

        if not value:
            # It's a "get"
            if isinstance(oldvalue, BaseEntity):
                click.echo(jsonify(oldvalue, deep=(fmt == 'deep')))
            elif fmt:
                click.echo(fmt % oldvalue)
            else:
                click.echo(format_obj(oldvalue, indent=2))
            return
        else:
            # It's a "set"
            try:
                if isinstance(oldvalue, (int, int)):
                    if isinstance(obj, ctypes.Structure):
                        # set value to a md object
                        try:
                            max_val = pow(2, obj._fields_[[f[0] for f in obj._fields_].index(prop)][2])
                            if max_val < int(value, 0):
                                raise ValueError('Value {} too large. Expected value range 0-{}'.format(value, max_val - 1))
                        except ValueError as e:
                            click.echo(repr(e))
                            return
                    elif isinstance(obj, Lock) and prop not in list(Lock.PROP2SLOCKS_ARG.keys()):
                        # set value to a lock object inside RMBInfo
                        click.echo('Unsupported set property "{}"\nSupported set properties: {}'.format(
                            prop, list(Lock.PROP2SLOCKS_ARG.keys())))
                        return
                    setattr(obj, prop, int(value, 0))
                else:
                    setattr(obj, prop, type(oldvalue)(value))
            except Exception as se:
                print(repr(se), type(oldvalue))
    except Exception as e:
        cmd_error(repr(e))


@click.command(hidden=True)
@click.pass_context
def debug(ctx):
    ''' Internal debugging of context '''
    # Skip debug itself.
    ctx = ctx.parent
    while ctx:
        print('Context: {}, CMD: ({}) {}, OBJ: ({}) {}, CMDQ: {}'.format(id(ctx), type(ctx.command).__name__, type(ctx.obj).__name__, ctx.obj, ctx.command.name,
            'N/A' if not isinstance(ctx.command, click_shell.Shell) else '; '.join(ctx.command.shell.cmdqueue)))
        ctx = ctx.parent

for ent in [nvmesh_edit_app, volume, _blockset, slice, page]:
    ent.add_command(debug)


def do_shift(ctx, amount):
    ''' Go to next instance '''
    # For pages, Kobi wants to navigate by "meta-physical" block - i.e., including parity blocks, and including stripe-width, etc.
    # NOTE: this isn't 100% accurate, because we assume ALL praids in the volume are equal, but I think that's assumed everywhere for now
    obj = ctx.obj

    # Find volume context
    vol_ctx = ctx
    while vol_ctx:
        if vol_ctx.command.name == 'volume':
            break
        vol_ctx = vol_ctx.parent
    if not vol_ctx or not isinstance(vol_ctx.obj, Volume):
        cmd_error('Cannot find volume context!')
    vol = vol_ctx.obj
    vol_cmd = None
    if isinstance(obj, PageRef):
        pslice = obj.pslice
        praid = pslice.praid
        mp_addr = (pslice.vlbs.addr // praid.dataDisks * praid.width) + obj.role + amount
        if mp_addr < 0:
            print('You cannot go past start of volume.')
            return
        if mp_addr >= vol.blocks:
            print('You cannot go past end of volume.')
            return
        # VLBA of new pslice start
        ps_vlba = mp_addr // praid.width * praid.dataDisks
        new_role = mp_addr % praid.width
        new_pslice = vol.get_pslice(Volume.LBA(ps_vlba))
        vol_cmd = f'%blockset {ps_vlba} slice {new_pslice.index_in_bs} page {new_role}'
    elif isinstance(obj, PSlice):
        new_vlba = obj.vlbs.addr + (amount * obj.praid.dataDisks)
        pslice = vol.get_pslice(Volume.LBA(new_vlba))
        vol_cmd = f'%blockset {new_vlba} slice {pslice.index_in_bs}'
    elif isinstance(obj, BlockSet):
        new_vlba = obj.vlbs.addr + (amount * obj.praid.dataDisks * obj.SLICE_COUNT)
        vol_cmd = f'%blockset {new_vlba}'
    if vol_cmd:
        ctx.parent.command.shell.cmdqueue.insert(0, '%volume')
        vol_ctx.command.shell.cmdqueue.insert(0, vol_cmd)

@click.command(hidden=True)
@click.argument('count', required=False, default=1)
@click.pass_context
def next(ctx, count):
    do_shift(ctx, count)

@click.command(hidden=True)
@click.argument('count', required=False, default=1)
@click.pass_context
def prev(ctx, count):
    do_shift(ctx, -count)

for ent in [_blockset, slice, page]:
    ent.add_command(next)
    ent.add_command(prev)


def main():
    args, remaining = CLIArgumentParser().parse_known_args()
    Connection.LOCALHOST_CHECK = True
    nvmesh_edit_app(args=remaining)


if __name__ == '__main__':
    main()
