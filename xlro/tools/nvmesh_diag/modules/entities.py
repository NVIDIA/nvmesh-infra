# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import re
from xlro.core.entities import BaseEntity
from xlro.tools.nvmesh_diag.util import ClusterDiagnostic, MsgLvl

class EntitiesCheck(ClusterDiagnostic):
    def validate(self):
        mismatches_found = False
        for entity_name, expectation_groups in self.expectations.items():
            try:
                all_entities = BaseEntity.ENTITY_REGISTRY[entity_name].sdk_get()
            except Exception as e:
                self.add_message(f"Unable to get {entity_name} entities - {repr(e)}")
                continue

            if not all_entities:
                continue

            for expectation_group in expectation_groups:
                for expctation_name, expectations in expectation_group.items():
                    re_name = expectations.pop('name', None)
                    if re_name:
                        entities_to_expect_from = list(filter(lambda e: re.match(re_name, e.name), all_entities))
                        if not entities_to_expect_from:
                            self.add_message(f'No entities found for expectation group {expctation_name}', MsgLvl.ERROR)
                            continue
                    else:
                        entities_to_expect_from = all_entities

                    count = expectations.pop('count', None)
                    if count is not None and len(entities_to_expect_from) != count:
                        mismatches_found = True
                        self.add_message(f'Expected {count} {entity_name}s in expectation group "{expctation_name}". Got: {len(entities_to_expect_from)}', MsgLvl.ERROR)

                    for entity in entities_to_expect_from:
                        mismatch_str = ''
                        for attr, val in expectations.items():
                            if val is None:
                                continue

                            try:
                                actual_val = getattr(entity, attr)
                                # TODO: need to support other comparisons than != like <, >, regex, caseless, etc
                                # TODO: consider typing when making comparisons, not only str comparisons like in line below
                                if val != str(actual_val):
                                    mismatches_found = True
                                    mismatch_str += f'Attribute: {attr} - Expected: {val}, Got: {actual_val}. '
                            except AttributeError:
                                self.add_message(f'No such attribute {attr} for entity {entity_name}', MsgLvl.WARNING)

                        if mismatch_str:
                            self.add_message(f'{entity} had the following mismatches: {mismatch_str}', MsgLvl.ERROR)

        if not mismatches_found:
            self.add_message(f'No mismatches found in entities check', MsgLvl.SUCCESS)
