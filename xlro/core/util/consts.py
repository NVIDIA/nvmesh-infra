# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from builtins import map
from builtins import object
UNMET_REQUIREMENT_STRING = 'Unmet requirement'


class Deprecate(object):
    @staticmethod
    def ToBeReplaced(*methods):
        return "replaced with - %s" % ", ".join(map(str, methods or ('nothing', )))

