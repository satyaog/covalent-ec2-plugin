# Copyright 2021 Agnostiq Inc.
#
# This file is part of Covalent.
#
# Licensed under the GNU Affero General Public License 3.0 (the "License").
# A copy of the License may be obtained with this software package or at
#
#      https://www.gnu.org/licenses/agpl-3.0.en.html
#
# Use of this file is prohibited except in compliance with the License. Any
# modifications or derivative works of this file must retain this copyright
# notice, and modified files must contain a notice indicating that they have
# been altered from the originals.
#
# Covalent is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the License for more details.
#
# Relief from the License may be granted by purchasing a commercial license.

"""EC2 executor plugin for the Covalent dispatcher."""

import os
import subprocess
from typing import Any, Callable, Dict, List, Tuple

# Executor-specific imports:
import boto3
import cloudpickle as pickle
import paramiko

# Covalent imports
from covalent._results_manager.result import Result
from covalent._shared_files import logger
from covalent_ssh_plugin.ssh import SSHExecutor
from covalent_ssh_plugin.ssh import _EXECUTOR_PLUGIN_DEFAULTS as _SSH_EXECUTOR_PLUGIN_DEFAULTS
from scp import SCPClient

# Scripts that are executed in the remote environment:
from .scripts import EXEC_SCRIPT

# The plugin class name must be given by the EXECUTOR_PLUGIN_NAME attribute:
executor_plugin_name = "EC2Executor"

app_log = logger.app_log
log_stack_info = logger.log_stack_info

_EXECUTOR_PLUGIN_DEFAULTS = {
    "profile": "default",
    "credentials_file": "",
    "instance_type": "t2.micro",
    "volume_size": "8GiB",
    "vpc": "",
    "subnet": "",
}
_EXECUTOR_PLUGIN_DEFAULTS.update(_SSH_EXECUTOR_PLUGIN_DEFAULTS)


class EC2Executor(SSHExecutor):
    """
    Executor class that invokes the input function on an EC2 instance
    Args:
        username: username used to authenticate to the instance.
        profile: The name of the AWS profile
        credentials_file: Filename of the credentials file used for authentication to AWS.
        key_file: Filename of the private key used for authentication with the remote server if it exists.
        kwargs: Key-word arguments to be passed to the parent class (SSHExecutor)
    """

    _TF_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "infra"))

    def __init__(
        self,
        profile: str,
        credentials_file: str,
        instance_type: str = "",
        volume_size: str = "",
        vpc: str = "",
        subnet: str = "",
        **kwargs,
    ) -> None:
        # TODO: Read from config if value are not passed in the contructor
        self.profile = profile
        self.credentials_file = credentials_file
        self.instance_type = instance_type,
        self.volume_size = volume_size,
        self.vpc = vpc,
        self.subnet = subnet,
        super().__init__(username="", hostname="", **kwargs)

    def setup(self, task_metadata: Dict) -> None:
        """
        Invokes Terraform to provision supporting resources for the instance

        """
        proc = subprocess.run(["terraform", "init"], cwd=self._TF_DIR, capture_output=True)
        if proc.returncode != 0:
            raise Exception(proc.stderr.decode("utf-8").strip())

        state_file = os.path.join(self.cache_dir, f"{task_metadata['dispatch_id']}-{task_metadata['node_id']}.tfstate")

        base_cmd = [
            "terraform",
            "apply",
            "-auto-approve",
            f"-state={state_file}",
        ]

        infra_vars = [
            "-var='name=covalent-task-{dispatch_id}-{node_id}'".format(
                dispatch_id=task_metadata["dispatch_id"], 
                node_id=task_metadata["node_id"]
            ),
            f"-var='instance_type={self.instance_type}'",
            f"-var='disk_size={self.volume_size}'",
            f"-var='key_file={self.ssh_key_file}'",
        ]
        if os.environ["AWS_REGION"]:
            infra_vars += [
                "-var='aws_region={region}'".format(region=os.environ["AWS_REGION"])
            ]
        if self.profile:
            infra_vars += [
                "-var='aws_profile={self.profile}'"
            ]
        if self.vpc:
            infra_vars += [
                f"-var='vpc_id={self.vpc}'"
            ]
        if self.subnet:
            infra_vars += [
                f"-var='subnet_id={self.subnet}'"
            ]

        cmd = base_cmd + infra_vars

        proc = subprocess.run(
            cmd,
            cwd=self._TF_DIR, 
            capture_output=True
        )
        if proc.returncode != 0:
            raise Exception(proc.stderr.decode("utf-8").strip())

        proc = subprocess.run(
            [
                "terraform",
                "output",
                "-raw",
                f"-state={state_file}",
                "hostname"
            ],
            cwd=self._TF_DIR,
            capture_output=True
        )
        if proc.returncode != 0:
            raise Exception(proc.stderr.decode("utf-8").strip())
        self.hostname = proc.stdout.decode("utf-8").strip()

        proc = subprocess.run(
            [
                "terraform",
                "output",
                "-raw",
                f"-state={state_file}",
                "username"
            ],
            cwd=self._TF_DIR,
            capture_output=True
        )
        if proc.returncode != 0:
            raise Exception(proc.stderr.decode("utf-8").strip())
        self.username = proc.stdout.decode("utf-8").strip()

    def teardown(self, task_metadata: Dict) -> None:
        """
        Invokes Terraform to terminate the instance and teardown supporting resources
        """
        
        state_file = os.path.join(self.cache_dir, f"{task_metadata['dispatch_id']}-{task_metadata['node_id']}.tfstate")

        if not os.path.exists(state_file):
            raise FileNotFoundError(f"Could not find Terraform state file: {state_dir}. Infrastructure may need to be manually deprovisioned.")

        proc = subprocess.run(
            [
                "terraform",
                "destroy",
                "-auto-approve",
                f"-state={state_file}"
            ],
            cwd=self._TF_DIR,
            capture_output=True
        )
        if proc.returncode != 0:
            raise Exception(proc.stderr.decode("utf-8").strip())
