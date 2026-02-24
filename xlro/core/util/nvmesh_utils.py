# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from xlro.core.util.thread_manager import ThreadPoolManager

import logging
logger = logging.getLogger('xlro.core.util.nvmesh_utils')


def get_cluster_nodes(manager, clients, targets):
    from xlro.core.entities import Target, ClientNode

    if clients or targets:
        targets = [Target.instance(name=n) for n in targets]
        clients = [ClientNode.instance(name=n) for n in clients]
    elif manager:
        targets = manager.targets
        clients = manager.client_nodes
    else:
        raise Exception("No nodes available to clean")

    return clients, targets


def nodes_cleanup(nodes, mnt_regex):
    from xlro.core.entities import Target, ClientNode

    with ThreadPoolManager() as executor:
        retcodes = list(zip([n for n in nodes], executor.map(lambda n: n.connection.execute(
            'sudo rm -rf {0} {1} ; sudo umount {2} && sudo rm -rf {2}'.format(
                Target.PERSISTENCIES, ClientNode.PERSISTENCIES, mnt_regex))[2], nodes)))

    bad_retcodes = [(h, r) for h, r in retcodes if r not in {0: 'SUCCESSFUL', 32: 'NO_MOUNT_POINT'}]
    assert not bad_retcodes, 'Got the following bad retcodes while unmounting {}'.format(bad_retcodes)


def prepare_mgmt_for_cleanup(manager):
    from xlro.core.entities import MgmtHost, Manager
    from xlro.core.util.operations_lib import BounceService

    if isinstance(manager, Manager):
        pass
    elif isinstance(manager, str):
        try:
            # Try starting mgr service up and return a Manager instance instead of string
            mgr_service = MgmtHost.instance(name=manager).services['mgr']
            BounceService(mgr_service).bounce()
            manager = Manager.instance(host=manager)
        except:
            logger.info('No management service available. Probably not installed.')
            manager = None
    else:
        raise Exception('Unsupported manager was provided')

    return manager


def cleanup(manager, clients=None, targets=None, full_db_drop=True, drives=None, mnt_regex='/mnt/*'):
    from xlro.core.entities import Drive
    from xlro.core.util.operations_lib import BounceService
    from xlro.core.util.operations import ParallelMultiOperation

    logger.info('Starting pre-session cleanup...')
    manager = prepare_mgmt_for_cleanup(manager)
    mgr_srv_stop = BounceService(manager.mgmt_host.services['mgr'])
    client_nodes, targets = get_cluster_nodes(manager, clients, targets)
    cluster_services = [t.services['target'] for t in targets] + [c.services['client'] for c in client_nodes]
    cluster_srv_ops = [BounceService(s) for s in cluster_services]
    cluster_srv_stop = ParallelMultiOperation(cluster_srv_ops)
    nodes = {s.host for s in cluster_services}

    logger.info('Pre-session Cleanup: Stopping services')
    cluster_srv_stop.do()

    logger.info('Pre-session Cleanup: removing mounts and persistencies')
    nodes_cleanup(nodes, mnt_regex)

    if manager:
        logger.info('Pre-session Cleanup: Dropping database')
        mgr_srv_stop.do()
        manager.mgmt_host.drop_database(full_db_drop)

        logger.info('Pre-session Cleanup: Starting management')
        mgr_srv_stop.undo()

    logger.info('Pre-session Cleanup: Starting services')
    cluster_srv_stop.undo()

    if manager:
        logger.info('Pre-session Cleanup: Formatting drives')
        drives = [d for t in manager.targets for d in t.drives] if drives is None else drives
        Drive.format_drives_and_verify(drives)
