#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import print_function
from __future__ import absolute_import
from builtins import str
from argparse import Namespace
import click
import json
from xlro.core import infra_conf
from xlro.core.entities import BaseEntity, Manager
from .cli_util import str2entity, handle_common_args, jprint
from .cli import pretty

class EntityParamType(click.ParamType):
    name = "Entity"

    def __init__(self, name, *args, **kwargs):
        self.name = name
        self.cls = BaseEntity.ENTITY_REGISTRY[name]
        super(EntityParamType, self).__init__()

    def convert(self, value, param, ctx):
        try:
            return value if isinstance(value, self.cls) else str2entity(value, self.cls)
        except Exception as e:
            self.fail('Cannot convert {} to {} - ({}) {}'.format(str(value), self.name, type(e), e), param, ctx)

@click.group()
@click.option('--loglevel', '-L', help='Logging level')
@click.option("--configure", help='Override specific configuration', multiple=True)
@click.option("--configfile", help='Additional configuration file', multiple=True)
@click.option('--manager', '-M', type=EntityParamType('Manager'), default=None)
@click.option('--output', '-O', type=click.Choice(['json', 'json-full', 'pretty', 'print']), default='print')
@click.pass_context
def infra_cli(ctx, **kwargs):
    args = Namespace(**kwargs)
    args.confdebug = False
    args = handle_common_args(args)
    ctx.obj = args

@infra_cli.resultcallback()
def output_results(result, output=None, **kwargs):
    if not result:
        return
    try:
        if output == 'json':
            jprint(result)
        elif output == 'json-full':
            jprint(result, deep=True)
        elif output == 'pretty':
            pretty(result)
        else:
            print(result)
    except:
        # Default/fallback is plain print
        print(result)

@infra_cli.command()
@click.pass_context
def show_config(ctx):
    print('ARGS:', ctx.find_object(Namespace))
    print('CONF:', json.dumps(infra_conf.serialize_to_dict(), indent=2))

@infra_cli.command()
@click.argument('volumes', nargs=-1, type=EntityParamType('Volume'))
@click.pass_context
def show_volume(ctx, volumes):
    return volumes

@infra_cli.command()
@click.option('--full/--summary', default=False)
@click.argument('volume', nargs=1, type=EntityParamType('Volume'))
@click.argument('targets', nargs=-1, type=EntityParamType('Target'))
@click.pass_context
def vol_locks(ctx, full, volume, targets):
    from .scanner import get_vol_locks_summary, get_vol_locks_full, convert_volume_to_drive2ranges

    try:
        manager = Manager.get_manager()
    except Exception as e:
        ctx.fail('Manager option missing or invalid.')

    try:
        volume.get_property('chunks')
    except Exception as e:
        ctx.fail('Cannot load volume: ' + volume.name + ' - ' + str(e))

    include_targets = targets or None

    try:
        ret = get_vol_locks_full(volume, include_targets) if full else get_vol_locks_summary(volume, include_targets)
        print('RET:', type(ret).__name__, ret)
        return ret
    except Exception as e:
        ctx.fail('({}) {}'.format(type(e).__name__, e))


if __name__ == '__main__':
    infra_cli()
