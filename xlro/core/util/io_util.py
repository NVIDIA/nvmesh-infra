#!/usr/bin/env python

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from typing import Dict, List
import os
import time
import datetime
import dateparser
import argparse
import warnings


from xlro.core.util.cli_util import CLIArgumentParser, EntitiesArg, cli_print
from xlro.core.util.io_monitors import BtestMonitor, FioMonitor, ElbenchoMonitor
from xlro.core.entities import Client, Volume, Attachment


def main():
    DEF_DURATION = 30
    DEF_VOL_OPTS = "capacity:2G,RAIDLevel:Mirrored RAID-1,numberOfMirrors:1,crc_enabled:true"

    parser = CLIArgumentParser()
    parser.formatter_class = argparse.RawDescriptionHelpFormatter

    parser.add_argument("-c", "--clients", type=EntitiesArg(Client),
            help='clients (comma separated) from which to run I/O')
    parser.add_argument("-v", "--volumes", type=EntitiesArg(Volume), default=[],
            help='volumes (comma separated) on which to run I/O')
    parser.add_argument("--paths", default=None, help='paths for elbencho')
    parser.add_argument("--volume-opts", default=[DEF_VOL_OPTS], action='append',
            help=f'override volume options as tag:value[,tag:value].... Can be used multiple times. (default={DEF_VOL_OPTS})')
    parser.add_argument("--no-cleanup", action='store_true',
            help='do not cleanup any create/attach actions taken, even on success')
    parser.add_argument("-d", "--duration", type=int, default=DEF_DURATION,
            help=f'duration of I/O. (default={DEF_DURATION})')
    parser.add_argument("-l", "--logdir",
            help='logging directory for command logs and data errors.', default='./io-util-logs')

    subparsers = parser.add_subparsers(title='IO Modes', parser_class=argparse.ArgumentParser)

    no_io_group = subparsers.add_parser('no-io', help='No I/O', parents=[parser], add_help=False)
    no_io_group.add_argument("-a", "--vlba", type=int, default=None,
            help='VLBA for post-io actions (no new I/O will be run)')
    no_io_group.add_argument("--start", type=dateparser.parse, default=datetime.datetime.now(),
            help='start time for no-I/O post-action time range')
    no_io_group.add_argument("--end", type=dateparser.parse, default=None,
            help='end time for no-I/O post-action time range')
    no_io_group.set_defaults(io_mode='no-io')

    btest_group = subparsers.add_parser('btest', help='BTest', parents=[parser], add_help=False)
    btest_group.add_argument("--btest-args", default=BtestMonitor.DEFAULT_ARGS,
            help=f'override arguments for btest. (default={BtestMonitor.DEFAULT_ARGS})')
    btest_group.add_argument("--btest-path", default=BtestMonitor.BTEST_CMD,
            help=f'path for btestEX. (default={BtestMonitor.BTEST_CMD})')
    btest_group.set_defaults(io_mode='btestEX')

    fio_group = subparsers.add_parser('fio', help='Fio', parents=[parser], add_help=False)
    fio_group.add_argument("--fio-args", default=FioMonitor.DEFAULT_ARGS,
            help=f'override arguments for fio. (default={FioMonitor.DEFAULT_ARGS})')
    fio_group.add_argument("--fio-path", default=FioMonitor.FIO_CMD,
            help=f'path for fio. (default={FioMonitor.FIO_CMD})')
    fio_group.add_argument("--mount-point", default=None,
            help='mount point path for fio')
    fio_group.set_defaults(io_mode='fio')

    elbencho_group = subparsers.add_parser('elbencho', help='Elbencho', parents=[parser], add_help=False)
    elbencho_group.add_argument("--elbencho-args", default=ElbenchoMonitor.DEFAULT_ARGS,
                           help=f'override arguments for elbencho. (default={ElbenchoMonitor.DEFAULT_ARGS})')
    elbencho_group.add_argument("--elbencho-path", default=ElbenchoMonitor.ELBENCHO_CMD,
                           help=f'path for elbencho. (default={ElbenchoMonitor.ELBENCHO_CMD})')
    elbencho_group.set_defaults(io_mode='elbencho')

    args = parser.parse_args()

    if not args.clients:
        if not args.manager:
            parser.error('At least one of --clients and --manager required.')
        args.clients = args.manager.clients
    if not args.volumes and not args.paths:
        if not args.manager:
            parser.error('At least one of --volumes and --manager required.')
        args.volumes = args.manager.volumes
        if not args.volumes:
            parser.error('No volumes found. Create volumes in setup or use --volumes.')
    if args.logdir and not os.path.isdir(args.logdir):
        os.makedirs(args.logdir)
    vol_opts = {}
    for opts in args.volume_opts:
        for opt in opts.split(','):
            key, sep, value = opt.strip().partition(':')
            vol_opts[key] = value

    created = []
    attached: Dict[Client, List[Volume]] = {}
    try:
        # Create volumes, if needed
        if args.manager and not args.paths:
            new_vols = set(args.volumes) - set(args.manager.volumes)
            if new_vols:
                cli_print('Creating volumes:' + ', '.join([v.name for v in new_vols]))
                cli_print('Volume-options:' + str(vol_opts))

                results = Volume.bulk_create([v.set_properties(vol_opts) for v in new_vols])
                # Moved before assert, so that any created could be cleaned-up in finally if partial success
                created = [v for v in results if not isinstance(v, Exception)]
                errors = [e for e in results if isinstance(e, Exception)]
                assert not errors, 'Errors creating volumes. {}'.format(errors)

        # Attach volumes, if needed
        for c in args.clients:
            to_attach = [v for v in args.volumes if v.name not in c.attachments]
            if to_attach:
                cli_print('Attaching on {} volumes: {}'.format(c.name, ', '.join([v.name for v in to_attach])))
                c.attach(to_attach)
                # TODO: Handle partial success?
                attached[c] = to_attach

        # Wait for our own attachments (others are user's responsibility)
        if attached:
            cli_print('Waiting for: ' + ' '.join([f'{c.name}/{v.name}' for c in attached for v in attached[c]]))
            assert Client.bulk_wait_for_attachments_io_enabled(attached, timeout=50, poll=5), \
                    'Timed out attaching {}.'.format({c.name: ','.join([v.name for v in volumes]) for c,volumes in attached.items()})

        if args.io_mode == 'elbencho' and args.volumes and not args.paths:
            args.paths = [Attachment.instance(client=c, volume=v).vol_dir for v in args.volumes for c in args.clients]

        if args.io_mode == 'no-io':
            # IF --lba, we just want to run the utilities, with no I/O. So simulate a panic 
            cli_print('Simulating panic on VLBA: {}. No I/O'.format(args.vlba))
            panics = [{
                'DI': '', 'host': args.clients[0].name,
                'volume': args.volumes[0].name, 'vlba': args.vlba,
                BtestMonitor.PANIC_TYPE: True
            }]
        else:
            # Run I/O
            cli_print(f'Starting {args.io_mode} Monitor...')
            mon = {'btestEX': BtestMonitor, 'fio': FioMonitor, 'elbencho': ElbenchoMonitor}[args.io_mode](**vars(args))
            mon.start()
            if args.duration > 15:
                # Don't wait for short durations, because we won't get message in time.
                start_wait = time.perf_counter()
                wait_result = mon.wait_for_io(timeout=15)
                assert wait_result, f'I/O did not start in {time.perf_counter()-start_wait:.1f} seconds. {wait_result}'

            while not mon.panics() and not mon.stopped_monitors():
                time.sleep(1)
            mon.stop()
            assert mon.is_io_started, 'I/O failed to start!'
            panics = [e for e in mon.events if BtestMonitor.PANIC_TYPE in e]
            cli_print(f'{len(panics)} panic(s) detected during {args.io_mode}')

        if not args.io_mode == 'no-io':
            args.end = datetime.datetime.now()

        # Build list of panics, if any.
        if panics:
            from xlro.core.util.failed_io import dump_di_event_info
            dump_di_event_info(panics, args.logdir, args.start)

    finally:
        if not args.no_cleanup:
            for c, vols in attached.items():
                c.detach(vols, wait_till_completed=True, force=True)
            for v in created:
                v.delete()


if __name__ == '__main__':
    with warnings.catch_warnings():
        # This doesn't work :-(
        warnings.simplefilter("ignore", DeprecationWarning)
        main()
