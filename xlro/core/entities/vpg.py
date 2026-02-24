# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import str
from typing import Any,Dict,List,Optional
from collections import defaultdict
from uuid import UUID

from xlro.core.entities import Manager, Drive, Target

from xlro.core.entities.base import SourceTypes, PropertySpec, prop_loader
from xlro.core.entities.etypes import Size, Domain, RAIDLevel, ECSeparationType
from xlro.core.entities.sdk_base import SDKEntity, sdk_entity, SdkException
from xlro.core.entities.volume import EncryptionObj
from xlro.core.entities.rest_info import RestVersionManager
from xlro.core.sdk.Utils import Utils as SdkUtils
from xlro.core.util.volume_utils import check_for_resize


@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class TargetClass(SDKEntity):
    name : str = PropertySpec(str, key=True)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    uuid : UUID = PropertySpec(UUID)
    description : str = PropertySpec(str)
    dateModified : str = PropertySpec(str)
    modifiedBy : str = PropertySpec(str)
    targets : List['Target'] = PropertySpec(['Target'])
    domains : List[Domain] = PropertySpec([Domain])

    @classmethod
    def map_props(cls, propmap: Dict[str, Any], source_type: str = None) -> Dict[str, Any]:
        propmap = super(TargetClass, cls).map_props(propmap, source_type)

        if 'targets' in propmap:
            propmap['targets'] = [{'name': target_node} if isinstance(target_node, str) else target_node
                                  for target_node in propmap['targets']]

        return propmap

    @classmethod
    def _set_mgmt(cls, obj, mgmt):
        if isinstance(obj, dict) and 'scope' in obj and 'identifier' in obj:
            # Do NOT _set_mgmt in Domains
            return
        super(TargetClass, cls)._set_mgmt(obj, mgmt)

    def foreign_sdk_type_conversion(self, value: Any, attr: str = None) -> Any:
        if attr == 'targets':
            return [t.name for t in value]

        return super(TargetClass, self).foreign_sdk_type_conversion(value, attr)


@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class DriveClass(SDKEntity):
    name : str = PropertySpec(str, key=True)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    uuid : UUID = PropertySpec(UUID)
    description : str = PropertySpec(str)
    dateModified : str = PropertySpec(str)
    modifiedBy : str = PropertySpec(str)
    drives : List['Drive'] = PropertySpec(['Drive'])
    createdBy : str = PropertySpec(str)
    dateCreated : str = PropertySpec(str)
    domains : List[Domain] = PropertySpec([Domain])
    # A kludge for now - set in plugins/config.py
    _mgmt_drives_format: Optional[str] = None

    @prop_loader(SourceTypes.MANAGEMENT, ['drives'])
    def load_drives(self):
        sdk_dict = super(DriveClass, self).load_from_mgmt_sdk()
        # TODO: Shouldn't this be in map_props()? special loaders cancel SDKEntity.bulk_get_property()
        return {'drives': [Drive.instance(name=d['diskID']) for d in sdk_dict.get('disks', [])]}

    @property
    def mgmt_drives_format(self):
        # TODO - when possible replace with logic of determine disks format type (example - according to mgmt version'
        if not self._mgmt_drives_format:
            raise NotImplementedError('TODO - implement here mgmt_drives_format_discovery')
        return self._mgmt_drives_format

    @classmethod
    def set_mgmt_drives_format(cls, mgmt_drives_format):
        # TODO - replace mgmt_drives_format from str to Enum - will allow assert the type instead of the value str
        allowed_formats = [2.0, 1.3]
        assert mgmt_drives_format in allowed_formats, \
            'drives format {} is not in {}'.format(mgmt_drives_format, allowed_formats)
        cls._mgmt_drives_format = mgmt_drives_format

    @staticmethod
    def drives2disks_2_0(drives):
        """
        Generate drive class payload for version master.
        """
        return [{"diskID": drive.name, "node_id": drive.nodeID, "model": drive.adjusted_model} for drive in drives]

    @staticmethod
    def drives2disks_1_3(drives):
        """
        Generate drive class payload for version 1.3.
        """
        models_dict: Dict[str, List[Dict]] = defaultdict(list)
        for drive in drives:
            # Change model name to end with underscores instead of spaces.
            model = drive.adjusted_model
            models_dict[model].append({
                'diskID': drive.name,
                'node_id': drive.target.name
            })
        # Convert models dictionary to DriveClass payload format.
        return [{'model': model, 'disks': models_dict[model]} for model in models_dict]

    @classmethod
    def _set_mgmt(cls, obj, mgmt):
        if isinstance(obj, dict) and 'scope' in obj and 'identifier' in obj:
            # Do NOT _set_mgmt in Domains
            return
        super(DriveClass, cls)._set_mgmt(obj, mgmt)

    def to_foreign_sdk_entity(self, local_only: bool=False) -> Dict:
        d = super(DriveClass, self).to_foreign_sdk_entity(local_only=local_only)
        if not local_only or 'disks' in d:
            d['disks'] = [{'diskID': d.name, 'node_id': d.nodeID, 'model': d.adjusted_model} for d in self.drives]
        return d


@sdk_entity(sourcetypes=[SourceTypes.MANAGEMENT])
class VPG(SDKEntity):
    name : str = PropertySpec(str, key=True)
    uuid : UUID = PropertySpec(UUID)
    mgmt : 'Manager' = PropertySpec('Manager', key=True)
    description : str = PropertySpec(str)
    dateModified : str = PropertySpec(str)
    RAIDlevel : RAIDLevel = PropertySpec(RAIDLevel, required=False)
    capacity : int = PropertySpec(Size)
    allowOverflow : bool = PropertySpec(bool)
    driveClasses : List[DriveClass] = PropertySpec([DriveClass])
    targetClasses : List[TargetClass] = PropertySpec([TargetClass])
    stripeSize : int = PropertySpec(int)
    stripeWidth : int = PropertySpec(int)
    dataBlocks : int = PropertySpec(int)
    parityBlocks : int = PropertySpec(int)
    protectionLevel : ECSeparationType = PropertySpec(ECSeparationType) #, default='N/A')
    ignoreNodeSeparation : bool = PropertySpec(bool, default=False)
    domain : str = PropertySpec(str)
    numberOfMirrors : int = PropertySpec(int)
    serviceResources : str = PropertySpec(str)
    createdBy : str = PropertySpec(str)
    dateCreated : str = PropertySpec(str)
    modifiedBy : str = PropertySpec(str)
    VSGs : List['VolumeSecurityGroup'] = PropertySpec(['VolumeSecurityGroup'])
    crc_enabled : bool = PropertySpec(bool, default=False)
    isEncrypted : bool = PropertySpec(bool, default=False)
    autoEncrypt : bool = PropertySpec(bool, default=False)
    encryption: Optional[EncryptionObj] = PropertySpec(EncryptionObj)
    allowAllocationOnOfflineDrives : bool = PropertySpec(bool, default=False)

    # The following are the fields here that cannot be overridden by the Volume entity
    # The list is in "infra" field names.  See Volume.to_foreign_sdk_entity() for usage
    VPG_VOLUME_FIELDS = ['RAIDlevel', 'driveClasses', 'targetClasses', 'stripeSize', 'stripeWidth', 'dataBlocks', 'parityBlocks',
        'protectionLevel', 'ignoreNodeSeparation', 'numberOfMirrors', 'crc_enabled', 'isEncrypted', 'domain']

    @classmethod
    def map_props(cls, propmap, source_type=None):
        propmap = super(VPG, cls).map_props(propmap, source_type)

        if 'serverClasses' in propmap:
            propmap['targetClasses'] = propmap.pop('serverClasses')

        if 'diskClasses' in propmap:
            propmap['driveClasses'] = propmap.pop('diskClasses')

        if 'RAIDLevel' in propmap:
            propmap['RAIDlevel'] = propmap.pop('RAIDLevel')

        for prop in ('driveClasses', 'targetClasses'):
            if prop in propmap:
                propmap[prop] = [{'name': val} if isinstance(val, str) else val for val in propmap[prop]]

        if 'capacity' in propmap and isinstance(propmap['capacity'], str):
            try:
                propmap['capacity'] = int(str(propmap['capacity']))
            except ValueError as e:
                propmap['capacity'] = int(SdkUtils.convertUnitCapacityToBytes(propmap['capacity']))

        return propmap

    def foreign_sdk_type_conversion(self, value: Any, attr: str = None) -> Any:
        if attr in ('driveClasses', 'targetClasses'):
            return [getattr(cls_or_str, 'name', cls_or_str) for cls_or_str in getattr(self, attr)]

        return super(VPG, self).foreign_sdk_type_conversion(value, attr)

    def to_foreign_sdk_entity(self, local_only: bool=False) -> Dict:
        vpg_dict = super(VPG, self).to_foreign_sdk_entity(local_only=local_only)

        if not vpg_dict.get('isEncrypted'):
            vpg_dict.pop('encryption', None)

        if self.rest_feature('has-defaults'):
            if local_only:
                vpg_dict.pop('name', None)
        else:
            # Until REST defaults, enableCrcCheck was required and/or ignored
            if not vpg_dict.get('enableCrcCheck'):
                vpg_dict['enableCrcCheck'] = False

        # This should probably be in SDKEntity.to_foreign_sdk_entity()
        vpg_dict = {k: v for k, v in vpg_dict.items() if v is not None}
        return vpg_dict

    def update(self):
        """ Override update method because of change in resize REST """
        check_for_resize(self)
        return super().update()
