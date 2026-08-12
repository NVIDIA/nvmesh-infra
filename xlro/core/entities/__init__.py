# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
    Entities package

    Autoload all modules and export the entities as a convenience, so callers don't need
    to know which entities are defined in which modules. I.e., they can use
        from xlro import entities
        entities.Drive()
    even though Drive happens to be defined in volume.py at the moment.

    Sadly, although this could be programmatic, it seems to confuse python Editors and Linters.
    So, run ./imports_check.py to see if anything's missing
"""
from __future__ import absolute_import

from .base import BaseEntity, NamedEntity, UUIDEntity, SourceTypes
from .sdk_base import SDKEntity
from .host import Host, Process, Service, BaseService
from .client import Client, ClientNode, Attachment, ClientVolumeTopology
from .target import Target
from .drive import Drive, GPT, DriveStatus
from .volume import Chunk, Segment, Partition, Volume, PRaid, Block, VolumeSecurityGroup, KeyPair, SubVolume
from .mgmt_host import MgmtHost
from .manager import Manager
from .vpg import VPG, DriveClass, TargetClass
from .network import Node, NIC, BaseSwitch, MellanoxSwitch, SwitchPort, ROCEPort, TCPPort, IBPort, DellSwitch, CumulusSwitch, SupermicroSwitch,HostPort, CiscoSwitch, BasePort, UnmanagedMellanoxSwitch, VirtualSwitch, LocalSwitch, UnknownSwitch, BFRepresentorPort
from .mrsp import MRSP, Controller, MDrive, NVMESHBdev, TPV, Subsystem, MDrivePair
from .mrsp_client import MRSPLinuxClient, BlueField, MRSPWindowsClient, NVMEDevice, MRSPClient
from .nvnode import NvNode
from .topology import PraidTopology, SegmentTopology
from .journal import JournalPartition, JournalEntry, JournalRange, Transaction, JournalPage, SliceRangeTransactions
from .log import Log
from .cluster import Cluster
from .general_settings import GeneralSettings
from .user import User
from .external_client import ExternalClient
from .nfs_client import NfsClient
from .nvmft_client import NVMftClient
from .dpu_client import DpuClient
from .k8s_client import K8sClient
from .config_profile import ConfigProfile
from .upgrade import UpgradeAgent, Upgrade, UpgradeStep
