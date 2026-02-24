# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import stat

DEFAULT_FILE_PERMISSION = stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC | stat.S_IROTH
TEST_REMOTE = 'localhost.localdomain' # Bypass SSH localhost check, but still test on localhost for unit tests
