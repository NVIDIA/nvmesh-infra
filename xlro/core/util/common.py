# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from os import path
import sys


def get_path(orig_path: str, pyinstaller_location: str = "") -> str:
    if hasattr(sys, '_MEIPASS'):
        return path.join(sys._MEIPASS, pyinstaller_location)  # type: ignore[attr-defined]
    return orig_path
