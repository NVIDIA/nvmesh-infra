# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

ENVIRONMENT SETUP
    The recommended way to setup your environment is as follows:

    IMPORTANT: Please make sure that python devel is installed on your client.
    For example, for linux mint, use: sudo apt-get install python-dev

    NOTE: Please note the lack of "sudo" in the rest of this section.
    If you need "sudo", you're doing something wrong!

    NOTE: It's assumed if you're reading this, you have already cloned the infrastructure repo

    1. Ensure you have python3 installed.  Infra currently supports python >=3.8 <3.12
        You can have others on the system, as long as at least one meets the requirements.
        Wherever "python3" is written below, you can replace with python3.x as needed.

    2. Install poetry (See: https://python-poetry.org/docs/)
        # NOTE: I had trouble with higher versions of poetry
        # If you want something other then the default python3, change python3 below to python3.x
        $ curl -sSL https://install.python-poetry.org | python3 - --version 1.8.3
        # It installs in ~/.local/bin - make sure it's in your path.
        # To confirm:
        $ poetry -V

    3. Create a virtual-env with our dependencies
        cd ~/projects/infrastructure
        # This will use the pyproject.toml/poetry.lock to install all dependencies
        $ poetry install --sync
        # Confirm your virtual-env was created:
        $ poetry env list
        # You should see "nvmesh-infra-XXXXXX-py3.10 (Activated)" (or whatever py3.x you wanted)

    4. Using the virtual-env
        # Poetry has "poetry run" to run a command from within the venv. 
        # If you want to "activate" the venv yourself:
        $ source $(poetry env info -p 2>/dev/null || poetry env info -p -C ~/projects/infrastructure)/bin/activate

    5. Set environment variables
        export PYTHONPATH=~/projects/infrastructure
        export SLASH_USER_SETTINGS=~/projects/infrastructure/xlro/infra/config/global-slashrc

        # NOTE: You need to repeat the above every shell session.
        # Put them in your .bashrc, and don't forget the "export"

    6. IDEs
	6.1 PyCharm + Teamcity plugin
	    6.1.1 Replace the existing <pycharm dir>/plugins/python-ce/helpers/pycharm/_jb_pytest_runner.py with xlro/ides/pycharm/_jb_pytest_runner.py
	    6.1.2 Start PyCharm, open project from ~/projects/infrastructure
	    6.1.3 Go to Settings > Project: Infrastructure > Python interpreter.
		  Add interpreter: Virtualenv Environment > Existing environment > set location to $(poetry env info -p)/bin/python3.x
	    6.1.4 Go to Settings > Tools > Python Integrated Tools.
		  Under Testing, change default test runner to pytest.
	    6.1.5 Open Edit Configurations..., go to Templates > Python tests > pytest
		  Configure the following:
		  Target: Select "Script path", put $(poetry env info -p)/projects/pyenvs/<xyz>/bin/slash
		  Environment variables: (Use absolute path, don't use ~)
		  PYTHONUNBUFFERED=1;PYTHONPATH=/home/<user>/projects/infrastructure;SLASH_USER_SETTINGS=/home/<user>/projects/infrastructure/xlro/infra/config/global-slashrc
		  Verify Python interpreter is python3.x from the virtualenv you configured before (it should be...)
		  And add whatever slash parameters you want in Additional Arguments field.
	6.2 VS Code
	    6.2.1 Use the example launch.json file in xlro/ides/vscode/launch.json

    7. Scale simulator
        in order to use scale simulator please add --simulator <number of instances>, --simulator_conf <path to node-config.json>
        #pre step - need to install docker on slash machine.
        See Ubuntu installation https://docs.docker.com/install/linux/docker-ce/ubuntu/

CONTENTS
    core:
        This is the core Entity layer.
        Domain objects are here, such as Volume, Client, Drive, PRaid, etc.
        Those object provide schema and methods for manipulating the system.
        From outside, import directly from xlro.core.entities and not from the specific modules.
        PREFERRED: from xlro.core.entities import Volume
        DISCOURAGED: from xlro.core.entities.volume import Volume
        See docs in core/__init__.py

    infra:
        This is the test infrastructure layer (above core).
        It has utility and convenience methods, slash related components, sample tests, etc.
        See docs in infra/__init__.py

    qa:
        These are the actual QA system tests

SLASH
    Tests in infra/ and qa/ are run via slash.  See https://slash.readthedocs.io/en/master/
    * ENVIRONMENT
        Make sure you have PYTHONPATH and SLASH_USER_SETTINGS set and exported as described above under Installation
        NOTE: you can ALSO have a ~/.slash/slashrc file. If it exists, it will automatically be invoked by the global-slashrc

    * CONFIGURATION
        - cluster
        - scenario

    * COMMAND-LINE
        $ slash run <option> ... <test> ...

        # See the slash docs, but the following are xlro extentions to the command line
        --force
            Our slash extensions use a convention of creating and removing a lock file on the Management
            server to avoid conflicting tests on same setup. --force means to clobber any existing lock file.
        --nocollect
            By default, if tests fail we run nvmesh_log_collector on each node. This is VERY expensive, so
            unless you really want it, use --nocollect.
        -c | --config <path>=<value>
            Overrides a single configuration path, for example -c cluster.management=n182. Use as many of -c
            and -C as you wish. They're applied in order.
        -C | --configfile <confpath>
            Path to a configuration file of overrides. Only needs override settings - don't copy the whole config
            and edit. Again, multiple allowed and they're applied in order.
        --reboot
            Reboot the setup after tests. Tries to "ssh <node> reboot now" or IPMI.
        -p install
            Prepare setup by installing new version as specified in the cluster.* configuration. There are several
            other -p options, but they're probably obsolete.


INFRA DEVELOPERS
    In order to run self-check and to enable use of the gen-stub tool, you must install Python3 and mypy 0.790
    OUTSIDE your virtualenv - i.e., sudo pip3 install mypy
