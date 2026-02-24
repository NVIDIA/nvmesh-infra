# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from typing import List, Dict, Tuple, Any, Type
from uuid import UUID
from xlro.core.entities import SDKEntity, SourceTypes, Manager
from xlro.core.entities.base import PropertySpec, prop_loader
from xlro.core.entities.sdk_base import sdk_entity, RE
from xlro.core.entities.etypes import HostName, UpgradeExecutionModes, UpgradeRedundancyLevels
from xlro.core.entities.rest_info import RestVersionManager


@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class UpgradeAgent(SDKEntity):
    # These are only created by the services, so I think we can avoid normalizing the hostname
    # Otherwise, we're going to need the client/target _name trick to differentiate from "given" name
    hostname: str = PropertySpec(str, key=True)
    uuid: UUID = PropertySpec(UUID)
    operating_system: str = PropertySpec(str, readonly=True)
    kernel_version: str = PropertySpec(str, readonly=True)
    architecture: str = PropertySpec(str, readonly=True)
    ofed_version: str = PropertySpec(str, readonly=True)
    nvmesh_versions: dict = PropertySpec(dict, readonly=True)

    @classmethod
    def map_props(cls, propmap, source_type=None):
        propmap = super().map_props(propmap, source_type)

        data = propmap.get('upgradeAgentData')
        if data:
            propmap['operating_system'] = data['operatingSystem']['name']
            propmap['kernel_version'] = data['kernel']
            propmap['architecture'] = data['archType']
            propmap['ofed_version'] = data['ofed']
            propmap['nvmesh_versions'] = data['nvmeshVersions']

        return propmap


@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class Upgrade(SDKEntity):
    _read_only_props = SDKEntity._read_only_props + ('uuid',) # Why not readonly=true?
    uuid: str = PropertySpec(str, key=True)
    machinesToUpgrade: List[UpgradeAgent] = PropertySpec([UpgradeAgent])
    destinationVersion: str = PropertySpec(str)
    executionMode: UpgradeExecutionModes = PropertySpec(UpgradeExecutionModes)
    minRedundancyLevel: UpgradeRedundancyLevels = PropertySpec(UpgradeRedundancyLevels)
    skipMachinesOnFailure: bool = PropertySpec(bool)
    maxErrorsThreshold: int = PropertySpec(int)
    maxConcurrentClients: int = PropertySpec(int)
    sourceVersion: str = PropertySpec(str)
    status: str = PropertySpec(str)
    upgradeSteps: List['UpgradeStep'] = PropertySpec(['UpgradeStep'])

    MACHINES_AS_HOSTS = 'update-machines-hosts'      # Take machines as host list vs. objects

    def resume(self):
        return self._do_op()

    def start(self):
        return self._do_op()

    @classmethod
    def create_instance(cls: Type[RE], **properties: Any) -> RE:
        mgr = properties.setdefault('mgmt', Manager.get_manager())
        v_info = RestVersionManager.version_info(mgr.api_version)
        if not v_info.features.get(cls.MACHINES_AS_HOSTS):
            for upgrader in properties.get('machinesToUpgrade', []):
                # Force fetch, if not already fetched
                _ = upgrader.kernel_version
        return super().create_instance(**properties)

    @classmethod
    def _makePost(cls, mgr: 'Manager', routes, payload, timeout=None) -> Tuple[Dict, List[Dict]]:
        if 'save' in routes and isinstance(payload, List):
            assert len(payload) == 1, f'Only a single upgrade is allowed per create. {len(payload)} received.'
            payload = payload[0]

        return super()._makePost(mgr, routes, payload, timeout)

    def to_foreign_sdk_entity(self, local_only: bool = False) -> Dict:
        d = super().to_foreign_sdk_entity(local_only=local_only)
        if not self.rest_feature(self.MACHINES_AS_HOSTS):
            d['machinesToUpgrade'] = [UpgradeAgent.instance(hostname=m).to_foreign_sdk_entity() for m in d['machinesToUpgrade']]
        return d

    @property
    def machines(self):
        return [m2u.hostname for m2u in self.get_property('machinesToUpgrade', no_cache=True)]

    @prop_loader(SourceTypes.MANAGEMENT, ["upgradeSteps"])
    def load_steps(self):
        from xlro.core.entities import UpgradeStep
        return {'upgradeSteps': UpgradeStep.get_filtered(upgradeID=self.uuid)}

    #@classmethod
    #def get_possible_upgrades(cls, source_version: str, is_client_only: bool):
    #    return cls._makeGet(Manager.get_manager(), [f'getPossibleUpgrades?isClientOnly={{{is_client_only}}}&sourceVersion={{{source_version}}}'])


@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class UpgradeStep(SDKEntity):
    uuid: str = PropertySpec(str, key=True)

    def setBreakpoint(self, clear: bool = False):
        return self._do_op()

    @classmethod
    def map_props(cls, propmap, source_type=None):
        propmap = super().map_props(propmap, source_type)
        propmap.update(propmap.pop('command', {}))
        return propmap
