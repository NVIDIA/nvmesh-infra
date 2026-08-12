# -*- mode: python -*-

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os

block_cipher = None

hidden_imports = []
apps = []

onefile = os.getenv("INFRA_BUILD_MODE") == "onefile"
config_file = 'xlro/core/util/routes.conf'
with open(config_file, 'r') as stream:
    for line in stream.readlines():
        name, _, module = line.partition(' ')
        hidden_imports.append(module.strip())
        apps.append(name)

futurize_hiddenimports = [
    'UserDict',
    'itertools',
    'collections',
    'future.backports.misc',
    'base64',
    '__builtin__',
    'math',
    'reprlib',
    'functools',
    're',
    'subprocess',
    'gnureadline',
]

# Third-party modules loaded dynamically (logging config, etc.)
dynamic_hiddenimports = [
    'concurrent_log_handler',
]

a = Analysis(['xlro/core/util/router.py'],
             pathex=['.'],
             binaries=[
                # ('/lib64/libffi.so', '.'),
                ('/lib64/libcrypt.so.1', '.'),  # CentOS7-built libpython links against it; bundle for RHEL8+ runners
             ],
             datas=[
                ('xlro/core/config', 'xlro/core/config'),
                ('xlro/core/entities/rest.yaml', '.'),
                ('xlro/core/util/routes.conf', '.'),
                ('xlro/core/util/templates/*', './templates'),
                # ('xlro/core/util/infra_clockdiff/clockdiff', '.'),
                ('xlro/tools/logging/ename2affected.yaml', '.'),
                ('xlro/tools/cli/cli.yaml', '.'),
                ('xlro/tools/cli/default_templates.yaml', '.'),
                # TODO: This should be extracted dynamically from the tools
                ('xlro/tools/nvmesh_diag/modules/*', './modules'),
                ('xlro/tools/nvmesh_diag/expectations.yaml', '.'),
                ('xlro/tools/infra_shared.so', '.'),
             ],
             hiddenimports=hidden_imports + futurize_hiddenimports + dynamic_hiddenimports,
             hookspath=['.'],
             runtime_hooks=[],
             excludes=[],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)

a_pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe_common_params = {
    'name': os.path.join('dist', 'infra'),
    'debug': False,
    'bootloader_ignore_signals': False,
    'strip': False,
    'upx': True,
    'runtime_tmpdir': None,
    'console': True
}

if onefile:
    exe = EXE(a_pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [], **exe_common_params)
else:
    exe = EXE(a_pyz, a.scripts, [], exclude_binaries=True, **exe_common_params)

if not onefile:
    coll = COLLECT(exe, a.binaries, a.datas, upx=True, upx_exclude=[], name='infra')

for app in apps:
    symlink_target = 'infra' if onefile else 'infra/infra'
    link_path = os.path.join('dist', app)
    if os.path.lexists(link_path):
        os.unlink(link_path)
    os.symlink(symlink_target, link_path)
