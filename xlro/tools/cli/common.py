# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

''' Common defs for rest_click, rest_custom, etc. '''
import sys
import click

OUTPUT_FORMATS = ['tabular', 'rows', 'json', 'list']
GET_LIMIT = 50

def click_env(key):
    try:
        return click.get_current_context().find_object(dict).get(key)
    except:
        return None

def is_no_prompt():
    # Cache was causing problems because we're called early on before context set.
    return click_env('no_prompt') or not sys.stdin.isatty()

def prompt(text, default=None, **kwargs):
    return default if is_no_prompt() else click.prompt(text, default=default, **kwargs)

def confirm(text, default=None, **kwargs): 
    return bool(default) if is_no_prompt() else click.confirm(text, default=default, **kwargs)

def color_msg(msg, is_success, color=None):
    if click_env('color'):
        msg = click.style(msg, fg=color or ('green' if is_success else 'red'))
    click.echo(msg, err=not is_success)

def failure_msg(msg):
    color_msg(msg, False)

def success_msg(msg):
    color_msg(msg, True)

def warn_msg(msg):
    if click_env('color'):
        msg = click.style(msg, bold=True)
    click.echo(msg, err=True)

class FieldNotAvailable(object):
    pass
