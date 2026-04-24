# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import click
import os
import sys
import uuid
from typing import List, Dict
from functools import partial
from urllib.parse import urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from humanfriendly.tables import format_robust_table, format_pretty_table, format_smart_table
from xlro.core.entities import KeyPair, Log, SDKEntity, Drive, Target, Upgrade, Manager, SourceTypes
from xlro.core.entities.manager import get_rest_server_name
from xlro.core.util.general_utils import host_name

from xlro.core.util.ssh import Connection
from xlro.tools.cli.rest_click import RestContext, RestGroup, rest_callback, get_rest_context, EntityOption, e_auto_complete_ids, auto_complete_multi_choices
from xlro.tools.cli.common import GET_LIMIT, OUTPUT_FORMATS, confirm, is_no_prompt, success_msg, failure_msg, FieldNotAvailable, \
    warn_msg

from xlro.core.util.cli_util import raw_to_snake, snake_to_human

Unsupported = object()

class KeyPairCLI(object):
    logger = logging.getLogger('KeyPairCLI')

    @click.command(help='Download a key-pair to a file')
    @click.option('-n', '--name', required=True, autocompletion=partial(e_auto_complete_ids, KeyPair, is_multi=False))
    @click.option('-p', '--path', type=click.Path(dir_okay=False), required=True)
    @click.option('--overwrite', type=bool, is_flag=True, default=False)
    @rest_callback
    @click.pass_obj
    def download(rest_ctx: RestContext, name, path, overwrite):
        try:
            key = KeyPair.get_by_key(rest_ctx.manager, rest_ctx.rest_info.dbkey, name)
            assert key is not None
        except Exception as e:
            raise Exception(f'Failed to fetch key "{name}". {repr(e)}')
        try:
            if os.path.isfile(path) and not overwrite:
                if is_no_prompt():
                    raise Exception(f'{path} exists. Use --overwrite to force overwriting file.')
                if not confirm(f'Overwrite existing file: {path}', default=False):
                    click.get_current_context().abort()
            with open(path, 'w') as fp:
                fp.write(key.get_key() + '\n')
            success_msg('success')
            return True
        except click.Abort:
            raise
        except Exception as e:
            raise Exception(f'Failed to copy key "{name}" to {path}. {e}')

class ClusterCLI(object):
    count = Unsupported

# class VPGCLI(object):
    # update = Unsupported

class LogGroup(RestGroup):
    logger = logging.getLogger('LogGroup')
    def __init__(self, e_name, e_info, mgr):
        super(LogGroup, self).__init__(e_name, e_info, mgr)
        # Add --alerts to show
        show = self.commands['show']
        show.params.append(click.Option(['-a', '--alerts'], type=bool, is_flag=True, default=False, help='Only show alerts'))
        self.alerts_only = False

    @click.command(help='Count of logs or alerts')
    @click.option('--alerts', '-a', type=bool, is_flag=True, default=False, help='Only count alerts')
    @rest_callback
    @click.pass_obj
    def count(obj, alerts):
        _, rest_ctx = get_rest_context()
        click.echo(Log._err2exc(Log._makeGet(rest_ctx.manager, ['alerts', 'count'] if alerts else ['count']))[0])
        return True

    @rest_callback
    def do_show(self, name, output_format, skip, limit, fields, onepage, alerts):
        self.alerts_only=alerts
        mgr = click.get_current_context().obj.manager
        count = -1 if not alerts else int(Log._err2exc(Log._makeGet(mgr, ['alerts', 'count']))[0])
        return super().do_show(name, output_format, skip, limit, fields, onepage, entries_left=count)

    def ents_by_name(self, obj, names, page=0, count=GET_LIMIT, **kwargs) -> List[SDKEntity]:
        return super().ents_by_name(obj, names, page, count, routes=None if not self.alerts_only else ['alerts'], **kwargs)

class DriveCLI(object):
    NVMESH_TARGET = '/usr/bin/nvmesh_target'

    @staticmethod
    def _modify_nvme(drive: Drive, op: str):
        out, err, code = Connection.execute_on_host(drive.target.name, f'sudo {DriveCLI.NVMESH_TARGET} {op} nvme {drive.name.partition(".")[0]}')
        if code != 0 or err:
            raise Exception(f'{DriveCLI.NVMESH_TARGET} failed. exit={code}. {err or out}')
        click.echo(f'You must restart the target service on {drive.target.name} for changes to take effect')
        success_msg('success')
        return True

    @click.command(help=f'Include NVME drive via {NVMESH_TARGET} on target')
    @click.option(cls=EntityOption, name='drive', entity=Drive, required=True, multiple=False, help='Drive id')
    @rest_callback
    def include_nvme(drive: Drive):
        return DriveCLI._modify_nvme(drive, 'include')

    @click.command(help=f'Exclude NVME drive via {NVMESH_TARGET} on target')
    @click.option(cls=EntityOption, name='drive', entity=Drive, required=True, multiple=False, help='Drive id')
    @rest_callback
    def exclude_nvme(drive: Drive):
        return DriveCLI._modify_nvme(drive, 'exclude')

class GeneralSettingsCLI(object):
    create = Unsupported
    delete = Unsupported
    count = Unsupported

class GeneralSettingsGroup(RestGroup):
    logger = logging.getLogger('GeneralSettingsGroup')

    def _process_kwargs(self, rest_ctx: RestContext, kwargs: Dict) -> Dict:
        cmd = click.get_current_context().command.name.lower()
        processed_args =  super()._process_kwargs(rest_ctx, kwargs)
        if cmd == 'update':
            # Special handling for updating dicts - update, not replace
            from xlro.core.entities import GeneralSettings
            from xlro.core.util.dict_util import merge_dicts

            try:
                gs = next(GeneralSettings._sdk_get())
                for k, v in processed_args.items():
                    if isinstance(v, dict):
                        processed_args[k] = merge_dicts(gs.get(k, {}), v)
            except Exception as e:
                raise Exception(f'Failed to process GeneralSettings args. {repr(e)}')
        return processed_args


class UpgradeCLI(object):
    update = Unsupported

#    @click.command(help=f'Get possible upgrades for a given source version')
#    @click.option(name='is_client_only', type=bool, is_flag=True, required=True, multiple=False, help='Whether the upgrade is client only')
#    @click.option(cname='source_version', type=str, required=True, multiple=False, help='The source version to get possible upgrades for')
#    @rest_callback
#    def get_possible_upgrades(source_version: str, is_client_only: bool):
#        return Upgrade.get_possible_upgrades(source_version, is_client_only)

class UpgradeGroup(RestGroup):
    logger = logging.getLogger('UpgradeGroup')

    @rest_callback
    def do_create(self, **kwargs):
        # dummy uuid to server as temporary key until we drop from request payload
        kwargs['uuid'] = (str(uuid.uuid4()),)
        return super().do_create(**kwargs)


class ManagementGroup(RestGroup):
    logger = logging.getLogger('ManagementGroup')

    @click.command(help=f'Show HA status')
    @click.option('-o', '--output-format', type=click.Choice(OUTPUT_FORMATS))
    @click.option('-f', '--fields',
                autocompletion=partial(auto_complete_multi_choices, choices=['Hostname', 'Reachable', 'Connected-Inbound', 'Connected-Outbound']),
                        help='Comma seperated fields to show - case insensitive, use "-" for space')
    @rest_callback
    @click.pass_obj
    def status(obj: RestContext, output_format, fields):
        from xlro.core.sdk.Utils import Utils
        from xlro.core.entities.manager import DEFAULT_PORT

        def fix_url(url):
            p = urlparse(url)
            normal_loc = p.netloc.replace(p.hostname, host_name(p.hostname)) if host_name(p.hostname) != 'localhost' else p.netloc
            return urlunparse(p._replace(netloc=normal_loc))

        # mgmt_cluster loader has side-effect of expanding conn.managementServers
        mgr = obj.manager
        conn = mgr.connection
        user = conn.user
        auth = conn.auth
        _ = mgr.get_property('mgmt_cluster', source=SourceTypes.MANAGEMENT, no_cache=True)
        conf_rest = mgr.global_nvmeshconf().get("_REST_SERVERS")
        conf_servers = Utils.transformManagementClusterToUrls(conf_rest, mgr.protocol, DEFAULT_PORT) if conf_rest else []
        mgmt_urls = sorted(set([fix_url(url) for url in conn.managementServers + conf_servers]))

        def get_info(url):
            try:
                hostname = urlparse(url).hostname
                mgr = Manager.instance(endpoints=[hostname])
                epname = get_rest_server_name(hostname)
                ManagementGroup.logger.debug(f'status.get_info() hostname: {hostname} -> epname: {epname}')
                # Need to copy user/auth from existing connection
                conn = mgr.connect(user, **auth)
                # conn = mgr.connection
                servers_before = conn.managementServers[:]
                index_before = conn.currentMgmtIndex
                conn.setManagementServers([url])
                conn.currentMgmtIndex = 0
                try:
                    merr, cluster = conn.get('/managementCluster/all/0/0')
                    assert not merr, f'Failed to get management cluster from {url} - {merr}'
                    return { 'endpoint': epname, 'peers': cluster }
                except Exception as e:
                    return { 'endpoint': epname, 'error': str(e) }
                finally:
                    conn.setManagementServers(servers_before)
                    conn.currentMgmtIndex = index_before
            except Exception as e:
                return { 'endpoint': epname, 'error': str(e) }

        with ThreadPoolExecutor() as executor:
            future_to_endpoint = {executor.submit(get_info, url): url for url in mgmt_urls}

            results = []
            for future in as_completed(future_to_endpoint):
                result = future.result()
                error = result.get('error')
                if output_format == 'json':
                    results.append({result['endpoint']: [] if error else result['peers']})
                    continue

                info = [
                    result['endpoint'],
                    not bool(error),
                    '' if error else ', '.join(sorted([get_rest_server_name(r['hostname']) for r in result['peers'] if r.get('inbound_socket_status') == 'connected' and not r.get('isMe')])),
                    '' if error else ', '.join(sorted([get_rest_server_name(r['hostname']) for r in result['peers'] if r.get('outbound_socket_status') == 'connected' and not r.get('isMe')])),
                ]
                results.append(dict(zip(['hostname', 'reachable', 'connected_inbound', 'connected_outbound'], [RestGroup._handle_values(val) for val in info])))

            if output_format == 'json':
                click.echo(json.dumps(results, indent=2))
                return True

            results.sort(key=lambda info: info['hostname'])

            if fields:
                show_fields = ['hostname']
                for f in fields.split(','):
                    snake_f = raw_to_snake(f)
                    if snake_f in show_fields:
                        continue
                    show_fields.append(snake_f)
            elif output_format == 'list':
                show_fields = ['hostname']
            else:
                show_fields = ['hostname', 'reachable', 'connected_inbound', 'connected_outbound']

            rows = []
            for r in results:
                rows.append([r[f] for f in show_fields])

            headers = [snake_to_human(f) for f in show_fields]
            if output_format == 'rows':
                click.echo(format_robust_table(rows, headers))
            elif output_format == 'tabular':
                click.echo(format_pretty_table(rows, headers))
            elif output_format == 'list':
                click.echo('\n'.join(['\t'.join([str(v) for v in row]) for row in rows]))
            else:
                click.echo(format_smart_table(rows, headers))

        return True


# ── Thin Provisioning ─────────────────────────────────────────────────────────

class CDVGroup(RestGroup):
    """RestGroup override for CDV (Capacity Data Volume).

    Injects volumeClass='CDV' into every create payload so the management
    server stores the volume with the correct discriminator.  All filtering
    (show/delete/update fetches) is handled by CDV._get_filter in volume.py.
    """
    logger = logging.getLogger('CDVGroup')

    def _process_kwargs(self, rest_ctx: RestContext, kwargs: Dict) -> Dict:
        processed = super()._process_kwargs(rest_ctx, kwargs)
        if click.get_current_context().command.name == 'create':
            processed['volumeClass'] = 'CDV'
        return processed


class CDVMgmtCLI(object):
    """Disable all mutating operations for the allocator-satellite (CDV_MGMT)
    volume.  The satellite is created/attached/deleted automatically with its
    parent CDV and has no user-facing lifecycle — only 'show' is exposed.
    """
    create = Unsupported
    update = Unsupported
    delete = Unsupported
    count = Unsupported


class TPVGroup(RestGroup):
    """RestGroup override for TPV (Thin-Provisioned Volume).

    * Injects volumeClass='TPV' on create.
    * Redirects the auto-generated 'update' command to POST /volumes/tpv/update
      instead of /volumes/update (which the management server does not support
      for TPVs).  Both 'create' and 'update' share do_create as their callback
      (set by RestGroup.set_update_create), so the interception happens there.
    * TPV delete routing to /volumes/tpv/delete is handled entirely in rest.yaml
      via ops.delete.route; no Python override is needed.
    """
    logger = logging.getLogger('TPVGroup')

    def _process_kwargs(self, rest_ctx: RestContext, kwargs: Dict) -> Dict:
        processed = super()._process_kwargs(rest_ctx, kwargs)
        if click.get_current_context().command.name == 'create':
            processed['volumeClass'] = 'TPV'
        return processed

    @rest_callback
    def do_create(self, **kwargs):
        ctx = click.get_current_context()
        if ctx.command.name == 'update':
            # TPV update must POST to /volumes/tpv/update.
            _, rest_ctx = get_rest_context(ctx)
            obj = ctx.obj
            prop_values = self._process_kwargs(obj, kwargs)
            keyprop = rest_ctx.rest_info.rest2infra.get(
                rest_ctx.rest_info.dbkey, rest_ctx.rest_info.dbkey)
            names = prop_values.pop(keyprop, [])
            # Only description is mutable on a TPV via this route.
            allowed = {'description'}
            payload = [
                {'_id': name, **{k: v for k, v in prop_values.items() if k in allowed}}
                for name in names
            ]
            err, out = obj.entity._makePost(obj.manager, ['tpv', 'update'], payload)
            if err:
                raise Exception(f'TPV update failed: {err}')
            response = True
            for r in (out or []):
                if r.get('success'):
                    success_msg(f'[{r.get("_id", "")}] success')
                else:
                    response = False
                    failure_msg(self.format_failure(r))
            return out if response else False
        return super().do_create(**kwargs)


class TPVCLI(object):
    """Inject extra top-level `tpv` subcommands for offline compaction
    (TPV_Trimming.md Step 4).  The declarative 'compact' op in rest.yaml
    already covers POST; --abort is handled here by routing to DELETE.
    """

    @staticmethod
    @click.command(help='Show current compaction job state for a TPV')
    @click.argument('tpv_name')
    @click.pass_context
    def compact_show(ctx, tpv_name):
        obj = ctx.obj
        err, out = obj.entity._makeGet(
            obj.manager, ['thinProvisioning', 'tpv', tpv_name, 'compaction'])
        if err:
            failure_msg(f'compact show failed: {err}')
            return
        if not out:
            echo('no compaction job')
            return
        # Render order puts the most operator-relevant fields first.
        # 'percent' is computed by management from progress counters
        # so the CLI doesn't replicate the math.
        for k in ('state', 'percent', 'clientId', 'startedAt',
                  'progress', 'progressUpdatedAt', 'lastError'):
            if k in out:
                v = out[k]
                if k == 'percent':
                    echo(f'percent:             {v}%')
                else:
                    echo(f'{k}: {v}')

    @staticmethod
    @click.command(help='Abort an in-flight compaction job')
    @click.argument('tpv_name')
    @click.pass_context
    def compact_abort(ctx, tpv_name):
        obj = ctx.obj
        err, _out = obj.entity._makeDelete(
            obj.manager, ['thinProvisioning', 'tpv', tpv_name, 'compaction'])
        if err:
            failure_msg(f'compact abort failed: {err}')
            return
        success_msg(f'compaction abort requested for {tpv_name}')
