# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import threading
from concurrent.futures import ThreadPoolExecutor
from logging import getLogger
from typing import Any, ClassVar, Dict, List, Tuple

from xlro.core.util.general_utils import WaitResult, wait_for_it
from xlro.core.util.ssh import Connection

logger = getLogger("DmCryptRegistry")

_LUKS_CLOSE_TIMEOUT = 30   # seconds to retry luksClose before giving up
_LUKS_CLOSE_INTERVAL = 2   # seconds between retries


class DmCryptRegistry:
    """Tracks open dm-crypt (LUKS) mapper devices across clients.

    CryptSetup registers/unregisters entries via register()/unregister().
    Operations call release_dm_devices()/reopen_dm_devices() to tear down
    or restore devices during disaster operations.
    """
    _registry: ClassVar[Dict[Tuple[str, str], dict]] = {}
    _registry_lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def register(cls, client_name: str, partition: str, client_conn: Any,
                 path: str, key: str) -> None:
        with cls._registry_lock:
            cls._registry[(client_name, partition)] = {
                'conn': client_conn,
                'path': path,
                'key': key,
            }

    @classmethod
    def unregister(cls, client_name: str, partition: str) -> None:
        with cls._registry_lock:
            cls._registry.pop((client_name, partition), None)

    @classmethod
    def release_dm_devices(cls, client_name: str) -> None:
        """Close all dm-crypt devices previously opened on *client_name* concurrently.
        Registry entries are preserved so reopen_dm_devices can restore them."""
        with cls._registry_lock:
            entries = [(k, v) for k, v in cls._registry.items()
                       if k[0] == client_name]
        if not entries:
            return
        with ThreadPoolExecutor(len(entries)) as executor:
            futures = [executor.submit(cls._release_single, cname, partition, info)
                       for (cname, partition), info in entries]
        # Threads have all finished here; re-raise the first failure (if any).
        for f in futures:
            f.result()

    @classmethod
    def release_dm_device(cls, client_name: str, volume_name: str) -> None:
        """Close the dm-crypt device for a single volume on *client_name*."""
        partition = f"{volume_name}_data"
        with cls._registry_lock:
            info = cls._registry.get((client_name, partition))
        if info is None:
            return
        cls._release_single(client_name, partition, info)

    @classmethod
    def _release_single(cls, client_name: str, partition: str, info: dict) -> None:
        _, _, code = info['conn'].execute(f"test -b /dev/mapper/{partition}")
        if code:
            return
        cmd = f"sudo cryptsetup luksClose /dev/mapper/{partition}"

        def attempt_close():
            try:
                Connection.err2exc(info['conn'].execute(cmd))
                return WaitResult(True)
            except Exception as e:
                logger.debug(
                    f"Failed to close {partition} on {client_name}: [{type(e).__name__}] {e} — retrying")
                return WaitResult(False, f"Failed to close {partition} on {client_name}: {e}")

        wait_for_it(attempt_close, poll=_LUKS_CLOSE_INTERVAL,
                    timeout=_LUKS_CLOSE_TIMEOUT).assert_result(
            f"giving up after {_LUKS_CLOSE_TIMEOUT}s")
        logger.info(f"Closed dm-crypt device {partition} on {client_name}")

    @classmethod
    def get_open_partitions(cls, client_name: str) -> List[str]:
        """Return names of registered partitions whose mapper device is still present on client."""
        with cls._registry_lock:
            entries = [(k, v) for k, v in cls._registry.items()
                       if k[0] == client_name]
        open_partitions = []
        for (_cname, partition), info in entries:
            _, _, code = info['conn'].execute(f"test -b /dev/mapper/{partition}")
            if code == 0:
                open_partitions.append(partition)
        return open_partitions

    @classmethod
    def reopen_dm_devices(cls, client_name: str) -> None:
        """Reopen all dm-crypt devices that were previously open on *client_name*.
        Skips devices already open.  No-op if nothing registered."""
        with cls._registry_lock:
            entries = [(k, v) for k, v in cls._registry.items()
                       if k[0] == client_name]
        for (cname, partition), info in entries:
            cls._reopen_single(cname, partition, info)

    @classmethod
    def reopen_dm_device(cls, client_name: str, volume_name: str) -> None:
        """Reopen the dm-crypt device for a single volume on *client_name*."""
        partition = f"{volume_name}_data"
        with cls._registry_lock:
            info = cls._registry.get((client_name, partition))
        if info is None:
            return
        cls._reopen_single(client_name, partition, info)

    @classmethod
    def _reopen_single(cls, client_name: str, partition: str, info: dict) -> None:
        _, _, code = info['conn'].execute(f"test -b /dev/mapper/{partition}")
        if code != 0:
            _, _, vol_code = info['conn'].execute(f"test -b {info['path']}")
            if vol_code == 0:
                try:
                    cmd = (f"echo -n {info['key']} | sudo cryptsetup "
                           f"luksOpen --key-file=- --batch-mode "
                           f"--verbose --force-password "
                           f"{info['path']} {partition}")
                    Connection.err2exc(info['conn'].execute(cmd))
                    logger.info(
                        f"Reopened dm-crypt device {partition} on {client_name}")
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to reopen {partition} on {client_name}: {e}") from e
