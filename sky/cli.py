"""The 'sky' command line tool.

Example usage:

  # See available commands.
  >> sky

  # Run a task, described in a yaml file.
  # Provisioning, setup, file syncing are handled.
  >> sky launch task.yaml
  >> sky launch [-c cl    os_disk_size = click.option('--os-disk-size',
                                default=None,
                                type=int,
                                required=False,
                                help=('OS disk size in GBs.'))
    disk_tier = click.option('--disk-tier',
                             default=None,
                             type=click.Choice(['low', 'medium', 'high'],
                                               case_sensitive=False),
                             required=False,
                             help=('OS disk tier. Could be one of "low", '
                                   '"medium", "high". Default: medium'))
    ports = click.option('--ports',
                         required=False,
                         type=str,
                         multiple=True,
                         help=('Ports to open on the cluster. '
                               'If specified, overrides the "ports" config in the YAML. '))
    no_confirm = click.option('--yes',
                         @click.option('--memory',
              default=None,
              type=str,
              required=False,
              help=('Amount of memory each instance must have in GB (e.g., '
                    '``--memory=16`` (exactly 16GB), ``--memory=16+`` (at least 16GB))'))
@click.option('--disk-size',
              default=None,
              type=int,
              required=False,
              help=('OS disk size in GBs.'))
@click.option('--disk-tier',
              default=None,
              type=click.Choice(['low', 'medium', 'high'], case_sensitive=False),
              required=False,
              help=('OS disk tier. Could be one of "low", "medium", "high". Default: medium'))
@click.option('--idle-minutes-to-autostop',
              '-i',
              default=None,
              type=int,
              required=False,
              help=('Automatically stop the cluster after thi    if not click.confirm(f'Cancelling {job_identity_str}. Proceed?',
                        default=True,
                        abort=True,
                        show_default=True):
        click.echo("Cancellation aborted.")
        sys.exit()

    try:
        core.cancel(cluster, all=all, job_ids=job_ids_to_cancel)
    except exceptions.NotSupportedError:
        # Friendly message for usage like 'sky cancel <spot controller> -a/<job id>'.
        error_str = ('Cancelling the spot controller\'s jobs is not allowed.'
                     f'\nTo cancel spot jobs, use: {bold}sky spot cancel <spot '
                     f'job IDs> [--all]{reset}')
        click.echo(error_str)
        sys.exit(1)
    except ValueError as e:
        raise click.UsageError(str(e))
    except exceptions.ClusterNotUpError as e:
        click.echo(f"Cluster is not up: {str(e)}")
        sys.exit(1)                 'of idleness, i.e., no running or pending jobs in the cluster\'s job '
                    'queue. Idleness gets reset whenever setting-up/running/pending jobs '
                    'are found in the job queue. ')) of running clusters.
  >> sky status

  # Tear down a specific cluster.
  >> sky down cluster_name

  # Tear down all existing clusters.
  >> sky down -a

NOTE: the order of command definitions in this file corresponds to how they are
listed in "sky --help".  Take care to put logically connected commands close to
each other.
"""
import copy
import datetime
import functools
import multiprocessing
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import time
import typing
from typing import Any, Dict, List, Optional, Tuple, Union
import webbrowser

import click
import colorama
import dotenv
from rich import progress as rich_progress
import yaml

import sky
from sky import backends
from sky import check as sky_check
from sky import clouds
from sky import core
from sky import exceptions
from sky import global_user_state
from sky import sky_logging
from sky import spot as spot_lib
from sky import status_lib
from sky.backends import backend_utils
from sky.backends import onprem_utils
from sky.benchmark import benchmark_state
from sky.benchmark import benchmark_utils
from sky.clouds import service_catalog
from sky.data import storage_utils
from sky.skylet import constants
from sky.skylet import job_lib
from sky.usage import usage_lib
from sky.utils import command_runner
from sky.utils import common_utils
from sky.utils import dag_utils
from sky.utils import env_options
from sky.utils import kubernetes_utils
from sky.utils import log_utils
from sky.utils import rich_utils
from sky.utils import schemas
from sky.utils import subprocess_utils
from sky.utils import timeline
from sky.utils import ux_utils
from sky.utils.cli_utils import status_utils

if typing.TYPE_CHECKING:
    from sky.backends import backend as backend_lib

logger = sky_logging.init_logger(__name__)

_CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

_CLUSTER_FLAG_HELP = """\
A cluster name. If provided, either reuse an existing cluster with that name or
provision a new cluster with that name. Otherwise provision a new cluster with
an autogenerated name."""
_INTERACTIVE_NODE_TYPES = ('cpunode', 'gpunode', 'tpunode')
_INTERACTIVE_NODE_DEFAULT_RESOURCES = {
    'cpunode': sky.Resources(cloud=None,
                             instance_type=None,
                             accelerators=None,
                             use_spot=False),
    'gpunode': sky.Resources(cloud=None,
                             instance_type=None,
                             accelerators={'K80': 1},
                             use_spot=False),
    'tpunode': sky.Resources(cloud=sky.GCP(),
                             instance_type=None,
                             accelerators={'tpu-v2-8': 1},
                             accelerator_args={'runtime_version': '2.12.0'},
                             use_spot=False),
}

# The maximum number of in-progress spot jobs to show in the status
# command.
_NUM_SPOT_JOBS_TO_SHOW_IN_STATUS = 5

_STATUS_IP_CLUSTER_NUM_ERROR_MESSAGE = (
    '{cluster_num} cluster{plural} {verb}. Please specify an existing '
    'cluster to show its IP address.\nUsage: `sky status --ip <cluster>`')


def _get_glob_clusters(clusters: List[str], silent: bool = False) -> List[str]:
    """Returns a list of clusters that match the glob pattern."""
    glob_clusters = []
    for cluster in clusters:
        glob_cluster = global_user_state.get_glob_cluster_names(cluster)
        if len(glob_cluster) == 0 and not silent:
            if onprem_utils.check_if_local_cloud(cluster):
                click.echo(
                    constants.UNINITIALIZED_ONPREM_CLUSTER_MESSAGE.format(
                        cluster=cluster))
            else:
                click.echo(f'Cluster {cluster} not found.')
        glob_clusters.extend(glob_cluster)
    return list(set(glob_clusters))


def _get_glob_storages(storages: List[str]) -> List[str]:
    """Returns a list of storages that match the glob pattern."""
    glob_storages = []
    for storage_object in storages:
        glob_storage = global_user_state.get_glob_storage_name(storage_object)
        if len(glob_storage) == 0:
            click.echo(f'Storage {storage_object} not found.')
        else:
            plural = 's' if len(glob_storage) > 1 else ''
            click.echo(f'Deleting {len(glob_storage)} storage object{plural}.')
        glob_storages.extend(glob_storage)
    return list(set(glob_storages))


def _warn_if_local_cluster(cluster: str, local_clusters: List[str],
                           message: str) -> bool:
    """Raises warning if the cluster name is a local cluster."""
    if cluster in local_clusters:
        click.echo(message)
        return False
    return True


def _interactive_node_cli_command(cli_func):
    """Click command decorator for interactive node commands."""
    assert cli_func.__name__ in _INTERACTIVE_NODE_TYPES, cli_func.__name__

    cluster_option = click.option('--cluster',
                                  '-c',
                                  default=None,
                                  type=str,
                                  required=False,
                                  help=_CLUSTER_FLAG_HELP)
    port_forward_option = click.option(
        '--port-forward',
        '-p',
        multiple=True,
        default=[],
        type=int,
        required=False,
        help=('Port to be forwarded. To forward multiple ports, '
              'use this option multiple times.'))
    screen_option = click.option('--screen',
                                 default=False,
                                 is_flag=True,
                                 help='If true, attach using screen.')
    tmux_option = click.option('--tmux',
                               default=False,
                               is_flag=True,
                               help='If true, attach using tmux.')
    cloud_option = click.option('--cloud',
                                default=None,
                                type=str,
                                help='Cloud provider to use.')
    instance_type_option = click.option('--instance-type',
                                        '-t',
                                        default=None,
                                        type=str,
                                        help='Instance type to use.')
    cpus = click.option(
        '--cpus',
        default=None,
        type=str,
        help=('Number of vCPUs each instance must have '
              '(e.g., ``--cpus=4`` (exactly 4) or ``--cpus=4+`` (at least 4)). '
              'This is used to automatically select the instance type.'))
    memory = click.option(
        '--memory',
        default=None,
        type=str,
        required=False,
        help=('Amount of memory each instance must have in GB (e.g., '
              '``--memory=16`` (exactly 16GB), ``--memory=16+`` (at least '
              '16GB))'))
    gpus = click.option('--gpus',
                        default=None,
                        type=str,
                        help=('Type and number of GPUs to use '
                              '(e.g., ``--gpus=V100:8`` or ``--gpus=V100``).'))
    tpus = click.option(
        '--tpus',
        default=None,
        type=str,
        help=('Type and number of TPUs to use (e.g., ``--tpus=tpu-v3-8:4`` or '
              '``--tpus=tpu-v3-8``).'))

    spot_option = click.option('--use-spot',
                               default=None,
                               is_flag=True,
                               help='If true, use spot instances.')

    tpuvm_option = click.option('--tpu-vm',
                                default=False,
                                is_flag=True,
                                help='If true, use TPU VMs.')

    disk_size = click.option('--disk-size',
                             default=None,
                             type=int,
                             required=False,
                             help=('OS disk size in GBs.'))
    disk_tier = click.option('--disk-tier',
                             default=None,
                             type=click.Choice(['low', 'medium', 'high'],
                                               case_sensitive=False),
                             required=False,
                             help=('OS disk tier. Could be one of "low", '
                                   '"medium", "high". Default: medium'))
    ports = click.option(
        '--ports',
        required=False,
        type=str,
        multiple=True,
        help=('Ports to open on the cluster. '
              'If specified, overrides the "ports" config in the YAML. '),
    )
    no_confirm = click.option('--yes',
                              '-y',
                              is_flag=True,
                              default=False,
                              required=False,
                              help='Skip confirmation prompt.')
    idle_autostop = click.option('--idle-minutes-to-autostop',
                                 '-i',
                                 default=None,
                                 type=int,
                                 required=False,
                                 help=('Automatically stop the cluster after '
                                       'this many minutes of idleness, i.e. '
                                       'no running or pending jobs in the '
                                       'cluster\'s job queue. Idleness gets '
                                       'reset whenever setting-up/running/'
                                       'pending jobs are found in the job '
                                       'queue. If not set, the cluster '
                                       'will not be auto-stopped.'))
    autodown = click.option('--down',
                            default=False,
                            is_flag=True,
                            required=False,
                            help=('Autodown the cluster: tear down the '
                                  'cluster after all jobs finish '
                                  '(successfully or abnormally). If '
                                  '--idle-minutes-to-autostop is also set, '
                                  'the cluster will be torn down after the '
                                  'specified idle time. Note that if errors '
                                  'occur during provisioning/data syncing/'
                                  'setting up, the cluster will not be torn '
                                  'down for debugging purposes.'))
    retry_until_up = click.option('--retry-until-up',
                                  '-r',
                                  is_flag=True,
                                  default=False,
                                  required=False,
                                  help=('Whether to retry provisioning '
                                        'infinitely until the cluster is up '
                                        'if we fail to launch the cluster on '
                                        'any possible region/cloud due to '
                                        'unavailability errors.'))
    region_option = click.option('--region',
                                 default=None,
                                 type=str,
                                 required=False,
                                 help='The region to use.')
    zone_option = click.option('--zone',
                               default=None,
                               type=str,
                               required=False,
                               help='The zone to use.')

    click_decorators = [
        cli.command(cls=_DocumentedCodeCommand),
        cluster_option,
        no_confirm,
        port_forward_option,
        idle_autostop,
        autodown,
        retry_until_up,

        # Resource options
        *([cloud_option] if cli_func.__name__ != 'tpunode' else []),
        region_option,
        zone_option,
        instance_type_option,
        cpus,
        memory,
        *([gpus] if cli_func.__name__ == 'gpunode' else []),
        *([tpus] if cli_func.__name__ == 'tpunode' else []),
        spot_option,
        *([tpuvm_option] if cli_func.__name__ == 'tpunode' else []),

        # Attach options
        screen_option,
        tmux_option,
        disk_size,
        disk_tier,
        ports,
    ]
    decorator = functools.reduce(lambda res, f: f(res),
                                 reversed(click_decorators), cli_func)

    return decorator


def _parse_env_var(env_var: str) -> Tuple[str, str]:
    """Parse env vars into a (KEY, VAL) pair."""
    if '=' not in env_var:
        value = os.environ.get(env_var)
        if value is None:
            raise click.UsageError(
                f'{env_var} is not set in local environment.')
        return (env_var, value)
    ret = tuple(env_var.split('=', 1))
    if len(ret) != 2:
        raise click.UsageError(
            f'Invalid env var: {env_var}. Must be in the form of KEY=VAL '
            'or KEY.')
    return ret[0], ret[1]


def _merge_env_vars(env_dict: Optional[Dict[str, str]],
                    env_list: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Merges all values from env_list into env_dict."""
    if not env_dict:
        return env_list
    for (key, value) in env_list:
        env_dict[key] = value
    return list(env_dict.items())


_TASK_OPTIONS = [
    click.option('--name',
                 '-n',
                 required=False,
                 type=str,
                 help=('Task name. Overrides the "name" '
                       'config in the YAML if both are supplied.')),
    click.option(
        '--workdir',
        required=False,
        type=click.Path(exists=True, file_okay=False),
        help=('If specified, sync this dir to the remote working directory, '
              'where the task will be invoked. '
              'Overrides the "workdir" config in the YAML if both are supplied.'
             )),
    click.option(
        '--cloud',
        required=False,
        type=str,
        help=('The cloud to use. If specified, overrides the "resources.cloud" '
              'config. Passing "none" resets the config.')),
    click.option(
        '--region',
        required=False,
        type=str,
        help=('The region to use. If specified, overrides the '
              '"resources.region" config. Passing "none" resets the config.')),
    click.option(
        '--zone',
        required=False,
        type=str,
        help=('The zone to use. If specified, overrides the '
              '"resources.zone" config. Passing "none" resets the config.')),
    click.option(
        '--num-nodes',
        required=False,
        type=int,
        help=('Number of nodes to execute the task on. '
              'Overrides the "num_nodes" config in the YAML if both are '
              'supplied.')),
    click.option(
        '--use-spot/--no-use-spot',
        required=False,
        default=None,
        help=('Whether to request spot instances. If specified, overrides the '
              '"resources.use_spot" config.')),
    click.option('--image-id',
                 required=False,
                 default=None,
                 help=('Custom image id for launching the instances. '
                       'Passing "none" resets the config.')),
    click.option('--env-file',
                 required=False,
                 type=dotenv.dotenv_values,
                 help="""\
        Path to a dotenv file with environment variables to set on the remote
        node.

        If any values from ``--env-file`` conflict with values set by
        ``--env``, the ``--env`` value will be preferred."""),
    click.option(
        '--env',
        required=False,
        type=_parse_env_var,
        multiple=True,
        help="""\
        Environment variable to set on the remote node.
        It can be specified multiple times.
        Examples:

        \b
        1. ``--env MY_ENV=1``: set ``$MY_ENV`` on the cluster to be 1.

        2. ``--env MY_ENV2=$HOME``: set ``$MY_ENV2`` on the cluster to be the
        same value of ``$HOME`` in the local environment where the CLI command
        is run.

        3. ``--env MY_ENV3``: set ``$MY_ENV3`` on the cluster to be the
        same value of ``$MY_ENV3`` in the local environment.""",
    )
]
_EXTRA_RESOURCES_OPTIONS = [
    click.option(
        '--gpus',
        required=False,
        type=str,
        help=
        ('Type and number of GPUs to use. Example values: '
         '"V100:8", "V100" (short for a count of 1), or "V100:0.5" '
         '(fractional counts are supported by the scheduling framework). '
         'If a new cluster is being launched by this command, this is the '
         'resources to provision. If an existing cluster is being reused, this'
         ' is seen as the task demand, which must fit the cluster\'s total '
         'resources and is used for scheduling the task. '
         'Overrides the "accelerators" '
         'config in the YAML if both are supplied. '
         'Passing "none" resets the config.')),
    click.option(
        '--instance-type',
        '-t',
        required=False,
        type=str,
        help=('The instance type to use. If specified, overrides the '
              '"resources.instance_type" config. Passing "none" resets the '
              'config.'),
    ),
    click.option(
        '--ports',
        required=False,
        type=str,
        multiple=True,
        help=('Ports to open on the cluster. '
              'If specified, overrides the "ports" config in the YAML. '),
    ),
]


def _complete_cluster_name(ctx: click.Context, param: click.Parameter,
                           incomplete: str) -> List[str]:
    """Handle shell completion for cluster names."""
    del ctx, param  # Unused.
    return global_user_state.get_cluster_names_start_with(incomplete)


def _complete_storage_name(ctx: click.Context, param: click.Parameter,
                           incomplete: str) -> List[str]:
    """Handle shell completion for storage names."""
    del ctx, param  # Unused.
    return global_user_state.get_storage_names_start_with(incomplete)


def _complete_file_name(ctx: click.Context, param: click.Parameter,
                        incomplete: str) -> List[str]:
    """Handle shell completion for file names.

    Returns a special completion marker that tells click to use
    the shell's default file completion.
    """
    del ctx, param  # Unused.
    return [click.shell_completion.CompletionItem(incomplete, type='file')]


def _get_click_major_version():
    return int(click.__version__.split('.')[0])


def _get_shell_complete_args(complete_fn):
    # The shell_complete argument is only valid on click >= 8.0.
    if _get_click_major_version() >= 8:
        return dict(shell_complete=complete_fn)
    return {}


_RELOAD_ZSH_CMD = 'source ~/.zshrc'
_RELOAD_FISH_CMD = 'source ~/.config/fish/config.fish'
_RELOAD_BASH_CMD = 'source ~/.bashrc'


def _install_shell_completion(ctx: click.Context, param: click.Parameter,
                              value: str):
    """A callback for installing shell completion for click."""
    del param  # Unused.
    if not value or ctx.resilient_parsing:
        return

    if value == 'auto':
        if 'SHELL' not in os.environ:
            click.secho(
                'Cannot auto-detect shell. Please specify shell explicitly.',
                fg='red')
            ctx.exit()
        else:
            value = os.path.basename(os.environ['SHELL'])

    zshrc_diff = '\n# For SkyPilot shell completion\n. ~/.sky/.sky-complete.zsh'
    bashrc_diff = ('\n# For SkyPilot shell completion'
                   '\n. ~/.sky/.sky-complete.bash')

    if value == 'bash':
        install_cmd = f'_SKY_COMPLETE=bash_source sky > \
                ~/.sky/.sky-complete.bash && \
                echo "{bashrc_diff}" >> ~/.bashrc'

        cmd = (f'(grep -q "SkyPilot" ~/.bashrc) || '
               f'[[ ${{BASH_VERSINFO[0]}} -ge 4 ]] && ({install_cmd})')
        reload_cmd = _RELOAD_BASH_CMD

    elif value == 'fish':
        cmd = '_SKY_COMPLETE=fish_source sky > \
                ~/.config/fish/completions/sky.fish'

        reload_cmd = _RELOAD_FISH_CMD

    elif value == 'zsh':
        install_cmd = f'_SKY_COMPLETE=zsh_source sky > \
                ~/.sky/.sky-complete.zsh && \
                echo "{zshrc_diff}" >> ~/.zshrc'

        cmd = f'(grep -q "SkyPilot" ~/.zshrc) || ({install_cmd})'
        reload_cmd = _RELOAD_ZSH_CMD

    else:
        click.secho(f'Unsupported shell: {value}', fg='red')
        ctx.exit()

    try:
        subprocess.run(cmd, shell=True, check=True, executable='/bin/bash')
        click.secho(f'Shell completion installed for {value}', fg='green')
        click.echo(
            'Completion will take effect once you restart the terminal: ' +
            click.style(f'{reload_cmd}', bold=True))
    except subprocess.CalledProcessError as e:
        click.secho(f'> Installation failed with code {e.returncode}', fg='red')
    ctx.exit()


def _uninstall_shell_completion(ctx: click.Context, param: click.Parameter,
                                value: str):
    """A callback for uninstalling shell completion for click."""
    del param  # Unused.
    if not value or ctx.resilient_parsing:
        return

    if value == 'auto':
        if 'SHELL' not in os.environ:
            click.secho(
                'Cannot auto-detect shell. Please specify shell explicitly.',
                fg='red')
            ctx.exit()
        else:
            value = os.path.basename(os.environ['SHELL'])

    if value == 'bash':
        cmd = 'sed -i"" -e "/# For SkyPilot shell completion/d" ~/.bashrc && \
               sed -i"" -e "/sky-complete.bash/d" ~/.bashrc && \
               rm -f ~/.sky/.sky-complete.bash'

        reload_cmd = _RELOAD_BASH_CMD

    elif value == 'fish':
        cmd = 'rm -f ~/.config/fish/completions/sky.fish'
        reload_cmd = _RELOAD_FISH_CMD

    elif value == 'zsh':
        cmd = 'sed -i"" -e "/# For SkyPilot shell completion/d" ~/.zshrc && \
               sed -i"" -e "/sky-complete.zsh/d" ~/.zshrc && \
               rm -f ~/.sky/.sky-complete.zsh'

        reload_cmd = _RELOAD_ZSH_CMD

    else:
        click.secho(f'Unsupported shell: {value}', fg='red')
        ctx.exit()

    try:
        subprocess.run(cmd, shell=True, check=True)
        click.secho(f'Shell completion uninstalled for {value}', fg='green')
        click.echo('Changes will take effect once you restart the terminal: ' +
                   click.style(f'{reload_cmd}', bold=True))
    except subprocess.CalledProcessError as e:
        click.secho(f'> Uninstallation failed with code {e.returncode}',
                    fg='red')
    ctx.exit()


def _add_click_options(options: List[click.Option]):
    """A decorator for adding a list of click option decorators."""

    def _add_options(func):
        for option in reversed(options):
            func = option(func)
        return func

    return _add_options


def _parse_override_params(
        cloud: Optional[str] = None,
        region: Optional[str] = None,
        zone: Optional[str] = None,
        gpus: Optional[str] = None,
        cpus: Optional[str] = None,
        memory: Optional[str] = None,
        instance_type: Optional[str] = None,
        use_spot: Optional[bool] = None,
        image_id: Optional[str] = None,
        disk_size: Optional[int] = None,
        disk_tier: Optional[str] = None,
        ports: Optional[Tuple[str]] = None) -> Dict[str, Any]:
    """Parses the override parameters into a dictionary."""
    override_params: Dict[str, Any] = {}
    if cloud is not None:
        if cloud.lower() == 'none':
            override_params['cloud'] = None
        else:
            override_params['cloud'] = clouds.CLOUD_REGISTRY.from_str(cloud)
    if region is not None:
        if region.lower() == 'none':
            override_params['region'] = None
        else:
            override_params['region'] = region
    if zone is not None:
        if zone.lower() == 'none':
            override_params['zone'] = None
        else:
            override_params['zone'] = zone
    if gpus is not None:
        if gpus.lower() == 'none':
            override_params['accelerators'] = None
        else:
            override_params['accelerators'] = gpus
    if cpus is not None:
        if cpus.lower() == 'none':
            override_params['cpus'] = None
        else:
            override_params['cpus'] = cpus
    if memory is not None:
        if memory.lower() == 'none':
            override_params['memory'] = None
        else:
            override_params['memory'] = memory
    if instance_type is not None:
        if instance_type.lower() == 'none':
            override_params['instance_type'] = None
        else:
            override_params['instance_type'] = instance_type
    if use_spot is not None:
        override_params['use_spot'] = use_spot
    if image_id is not None:
        if image_id.lower() == 'none':
            override_params['image_id'] = None
        else:
            override_params['image_id'] = image_id
    if disk_size is not None:
        override_params['disk_size'] = disk_size
    if disk_tier is not None:
        override_params['disk_tier'] = disk_tier
    if ports:
        override_params['ports'] = ports
    return override_params


def _default_interactive_node_name(node_type: str):
    """Returns a deterministic name to refer to the same node."""
    # FIXME: this technically can collide in Azure/GCP with another
    # same-username user.  E.g., sky-gpunode-ubuntu.  Not a problem on AWS
    # which is the current cloud for interactive nodes.
    assert node_type in _INTERACTIVE_NODE_TYPES, node_type
    return f'sky-{node_type}-{backend_utils.get_cleaned_username()}'


def _infer_interactive_node_type(resources: sky.Resources):
    """Determine interactive node type from resources."""
    accelerators = resources.accelerators
    cloud = resources.cloud
    if accelerators:
        # We only support homogenous accelerators for now.
        assert len(accelerators) == 1, resources
        acc, _ = list(accelerators.items())[0]
        is_gcp = cloud is not None and cloud.is_same_cloud(sky.GCP())
        if is_gcp and 'tpu' in acc:
            return 'tpunode'
        return 'gpunode'
    return 'cpunode'


def _check_resources_match(backend: backends.Backend,
                           cluster_name: str,
                           task: 'sky.Task',
                           node_type: Optional[str] = None) -> None:
    """Check matching resources when reusing an existing cluster.

    The only exception is when [cpu|tpu|gpu]node -c cluster_name is used with no
    additional arguments, then login succeeds.

    Args:
        cluster_name: The name of the cluster.
        task: The task requested to be run on the cluster.
        node_type: Only used for interactive node. Node type to attach to VM.
    """
    handle = global_user_state.get_handle_from_cluster_name(cluster_name)
    if handle is None:
        return

    if node_type is not None:
        assert isinstance(handle,
                          backends.CloudVmRayResourceHandle), (node_type,
                                                               handle)
        inferred_node_type = _infer_interactive_node_type(
            handle.launched_resources)
        if node_type != inferred_node_type:
            name_arg = ''
            if cluster_name != _default_interactive_node_name(
                    inferred_node_type):
                name_arg = f' -c {cluster_name}'
            raise click.UsageError(
                f'Failed to attach to interactive node {cluster_name}. '
                f'Please use: {colorama.Style.BRIGHT}'
                f'sky {inferred_node_type}{name_arg}{colorama.Style.RESET_ALL}')
        return
    backend.check_resources_fit_cluster(handle, task)


def _launch_with_confirm(
    task: sky.Task,
    backend: backends.Backend,
    cluster: Optional[str],
    *,
    dryrun: bool,
    detach_run: bool,
    detach_setup: bool = False,
    no_confirm: bool = False,
    idle_minutes_to_autostop: Optional[int] = None,
    down: bool = False,  # pylint: disable=redefined-outer-name
    retry_until_up: bool = False,
    no_setup: bool = False,
    node_type: Optional[str] = None,
    clone_disk_from: Optional[str] = None,
):
    """Launch a cluster with a Task."""
    if cluster is None:
        cluster = backend_utils.generate_cluster_name()

    clone_source_str = ''
    if clone_disk_from is not None:
        clone_source_str = f' from the disk of {clone_disk_from!r}'
        task, _ = backend_utils.check_can_clone_disk_and_override_task(
            clone_disk_from, cluster, task)

    with sky.Dag() as dag:
        dag.add(task)

    maybe_status, _ = backend_utils.refresh_cluster_status_handle(cluster)
    if maybe_status is None:
        # Show the optimize log before the prompt if the cluster does not exist.
        try:
            backend_utils.check_public_cloud_enabled()
        except exceptions.NoCloudAccessError as e:
            # Catch the exception where the public cloud is not enabled, and
            # only print the error message without the error type.
            click.secho(e, fg='yellow')
            sys.exit(1)
        dag = sky.optimize(dag)
    task = dag.tasks[0]

    _check_resources_match(backend, cluster, task, node_type=node_type)

    confirm_shown = False
    if not no_confirm:
        # Prompt if (1) --cluster is None, or (2) cluster doesn't exist, or (3)
        # it exists but is STOPPED.
        prompt = None
        if maybe_status is None:
            cluster_str = '' if cluster is None else f' {cluster!r}'
            if onprem_utils.check_if_local_cloud(cluster):
                prompt = f'Initializing local cluster{cluster_str}. Proceed?'
            else:
                prompt = (
                    f'Launching a new cluster{cluster_str}{clone_source_str}. '
                    'Proceed?')
        elif maybe_status == status_lib.ClusterStatus.STOPPED:
            prompt = f'Restarting the stopped cluster {cluster!r}. Proceed?'
        if prompt is not None:
            confirm_shown = True
            click.confirm(prompt, default=True, abort=True, show_default=True)

    if node_type is not None:
        if maybe_status != status_lib.ClusterStatus.UP:
            click.secho(f'Setting up interactive node {cluster}...',
                        fg='yellow')

        # We do not sky.launch if interactive node is already up, so we need
        # to update idle timeout and autodown here.
        elif idle_minutes_to_autostop is not None:
            core.autostop(cluster, idle_minutes_to_autostop, down)
        elif down:
            core.autostop(cluster, 1, down)

    elif not confirm_shown:
        click.secho(f'Running task on cluster {cluster}...', fg='yellow')

    if node_type is None or maybe_status != status_lib.ClusterStatus.UP:
        # No need to sky.launch again when interactive node is already up.
        sky.launch(
            dag,
            dryrun=dryrun,
            stream_logs=True,
            cluster_name=cluster,
            detach_setup=detach_setup,
            detach_run=detach_run,
            backend=backend,
            idle_minutes_to_autostop=idle_minutes_to_autostop,
            down=down,
            retry_until_up=retry_until_up,
            no_setup=no_setup,
            clone_disk_from=clone_disk_from,
        )


# TODO: skip installing ray to speed up provisioning.
def _create_and_ssh_into_node(
    node_type: str,
    resources: sky.Resources,
    cluster_name: str,
    backend: Optional['backend_lib.Backend'] = None,
    port_forward: Optional[List[int]] = None,
    session_manager: Optional[str] = None,
    user_requested_resources: Optional[bool] = False,
    no_confirm: bool = False,
    idle_minutes_to_autostop: Optional[int] = None,
    down: bool = False,  # pylint: disable=redefined-outer-name
    retry_until_up: bool = False,
):
    """Creates and attaches to an interactive node.

    Args:
        node_type: Type of the interactive node: { 'cpunode', 'gpunode' }.
        resources: Resources to attach to VM.
        cluster_name: a cluster name to identify the interactive node.
        backend: the Backend to use (currently only CloudVmRayBackend).
        port_forward: List of ports to forward.
        session_manager: Attach session manager: { 'screen', 'tmux' }.
        user_requested_resources: If true, user requested resources explicitly.
        no_confirm: If true, skips confirmation prompt presented to user.
        idle_minutes_to_autostop: Automatically stop the cluster after
                                  specified minutes of idleness. Idleness gets
                                  reset whenever setting-up/running/pending
                                  jobs are found in the job queue.
        down: If true, autodown the cluster after all jobs finish. If
              idle_minutes_to_autostop is also set, the cluster will be torn
              down after the specified idle time.
        retry_until_up: Whether to retry provisioning infinitely until the
                        cluster is up if we fail to launch due to
                        unavailability errors.
    """
    assert node_type in _INTERACTIVE_NODE_TYPES, node_type
    assert session_manager in (None, 'screen', 'tmux'), session_manager
    if onprem_utils.check_if_local_cloud(cluster_name):
        raise click.BadParameter(
            f'Name {cluster_name!r} taken by a local cluster and cannot '
            f'be used for a {node_type}.')

    backend = backend if backend is not None else backends.CloudVmRayBackend()
    if not isinstance(backend, backends.CloudVmRayBackend):
        raise click.UsageError('Interactive nodes are only supported for '
                               f'{backends.CloudVmRayBackend.__name__} '
                               f'backend. Got {type(backend).__name__}.')

    maybe_status, handle = backend_utils.refresh_cluster_status_handle(
        cluster_name)
    if maybe_status is not None:
        if user_requested_resources:
            if not resources.less_demanding_than(handle.launched_resources):
                name_arg = ''
                if cluster_name != _default_interactive_node_name(node_type):
                    name_arg = f' -c {cluster_name}'
                raise click.UsageError(
                    f'Relaunching interactive node {cluster_name!r} with '
                    'mismatched resources.\n    '
                    f'Requested resources: {resources}\n    '
                    f'Launched resources: {handle.launched_resources}\n'
                    'To login to existing cluster, use '
                    f'{colorama.Style.BRIGHT}sky {node_type}{name_arg}'
                    f'{colorama.Style.RESET_ALL}. To launch a new cluster, '
                    f'use {colorama.Style.BRIGHT}sky {node_type} -c NEW_NAME '
                    f'{colorama.Style.RESET_ALL}')
        else:
            # Use existing interactive node if it exists and no user
            # resources were specified.
            resources = handle.launched_resources

    # TODO: Add conda environment replication
    # should be setup =
    # 'conda env export | grep -v "^prefix: " > environment.yml'
    # && conda env create -f environment.yml
    task = sky.Task(
        node_type,
        workdir=None,
        setup=None,
    )
    task.set_resources(resources)

    _launch_with_confirm(
        task,
        backend,
        cluster_name,
        dryrun=False,
        detach_run=True,
        no_confirm=no_confirm,
        idle_minutes_to_autostop=idle_minutes_to_autostop,
        down=down,
        retry_until_up=retry_until_up,
        node_type=node_type,
    )
    handle = global_user_state.get_handle_from_cluster_name(cluster_name)
    assert isinstance(handle, backends.CloudVmRayResourceHandle), handle

    # Use ssh rather than 'ray attach' to suppress ray messages, speed up
    # connection, and for allowing adding 'cd workdir' in the future.
    # Disable check, since the returncode could be non-zero if the user Ctrl-D.
    commands = []
    if session_manager == 'screen':
        commands += ['screen', '-D', '-R']
    elif session_manager == 'tmux':
        commands += ['tmux', 'attach', '||', 'tmux', 'new']
    backend.run_on_head(handle,
                        commands,
                        port_forward=port_forward,
                        ssh_mode=command_runner.SshMode.LOGIN)
    cluster_name = handle.cluster_name

    click.echo('To attach to it again:  ', nl=False)
    if cluster_name == _default_interactive_node_name(node_type):
        option = ''
    else:
        option = f' -c {cluster_name}'
    click.secho(f'sky {node_type}{option}', bold=True)
    click.echo('To stop the node:\t', nl=False)
    click.secho(f'sky stop {cluster_name}', bold=True)
    click.echo('To tear down the node:\t', nl=False)
    click.secho(f'sky down {cluster_name}', bold=True)
    click.echo('To upload a folder:\t', nl=False)
    click.secho(f'rsync -rP /local/path {cluster_name}:/remote/path', bold=True)
    click.echo('To download a folder:\t', nl=False)
    click.secho(f'rsync -rP {cluster_name}:/remote/path /local/path', bold=True)


def _check_yaml(entrypoint: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Checks if entrypoint is a readable YAML file.

    Args:
        entrypoint: Path to a YAML file.
    """
    is_yaml = True
    config: Optional[List[Dict[str, Any]]] = None
    result = None
    shell_splits = shlex.split(entrypoint)
    yaml_file_provided = (len(shell_splits) == 1 and
                          (shell_splits[0].endswith('yaml') or
                           shell_splits[0].endswith('.yml')))
    try:
        with open(entrypoint, 'r') as f:
            try:
                config = list(yaml.safe_load_all(f))
                if config:
                    # FIXME(zongheng): in a chain DAG YAML it only returns the
                    # first section. OK for downstream but is weird.
                    result = config[0]
                else:
                    result = {}
                if isinstance(result, str):
                    # 'sky exec cluster ./my_script.sh'
                    is_yaml = False
            except yaml.YAMLError as e:
                if yaml_file_provided:
                    logger.debug(e)
                    invalid_reason = ('contains an invalid configuration. '
                                      ' Please check syntax.')
                is_yaml = False
    except OSError:
        if yaml_file_provided:
            entry_point_path = os.path.expanduser(entrypoint)
            if not os.path.exists(entry_point_path):
                invalid_reason = ('does not exist. Please check if the path'
                                  ' is correct.')
            elif not os.path.isfile(entry_point_path):
                invalid_reason = ('is not a file. Please check if the path'
                                  ' is correct.')
            else:
                invalid_reason = ('yaml.safe_load() failed. Please check if the'
                                  ' path is correct.')
        is_yaml = False
    if not is_yaml:
        if yaml_file_provided:
            click.confirm(
                f'{entrypoint!r} looks like a yaml path but {invalid_reason}\n'
                'It will be treated as a command to be run remotely. Continue?',
                abort=True)
    return is_yaml, result


def _make_task_or_dag_from_entrypoint_with_overrides(
    entrypoint: List[str],
    *,
    name: Optional[str] = None,
    cluster: Optional[str] = None,
    workdir: Optional[str] = None,
    cloud: Optional[str] = None,
    region: Optional[str] = None,
    zone: Optional[str] = None,
    gpus: Optional[str] = None,
    cpus: Optional[str] = None,
    memory: Optional[str] = None,
    instance_type: Optional[str] = None,
    num_nodes: Optional[int] = None,
    use_spot: Optional[bool] = None,
    image_id: Optional[str] = None,
    disk_size: Optional[int] = None,
    disk_tier: Optional[str] = None,
    ports: Optional[Tuple[str]] = None,
    env: Optional[List[Tuple[str, str]]] = None,
    # spot launch specific
    spot_recovery: Optional[str] = None,
) -> Union[sky.Task, sky.Dag]:
    """Creates a task or a dag from an entrypoint with overrides.

    Returns:
        A dag iff the entrypoint is YAML and contains more than 1 task.
        Otherwise, a task.
    """
    entrypoint = ' '.join(entrypoint)
    is_yaml, yaml_config = _check_yaml(entrypoint)
    entrypoint: Optional[str]
    if is_yaml:
        # Treat entrypoint as a yaml.
        click.secho('Task from YAML spec: ', fg='yellow', nl=False)
        click.secho(entrypoint, bold=True)
    else:
        if not entrypoint:
            entrypoint = None
        else:
            # Treat entrypoint as a bash command.
            click.secho('Task from command: ', fg='yellow', nl=False)
            click.secho(entrypoint, bold=True)

    if onprem_utils.check_local_cloud_args(cloud, cluster, yaml_config):
        cloud = 'local'

    override_params = _parse_override_params(cloud=cloud,
                                             region=region,
                                             zone=zone,
                                             gpus=gpus,
                                             cpus=cpus,
                                             memory=memory,
                                             instance_type=instance_type,
                                             use_spot=use_spot,
                                             image_id=image_id,
                                             disk_size=disk_size,
                                             disk_tier=disk_tier,
                                             ports=ports)

    if is_yaml:
        assert entrypoint is not None
        usage_lib.messages.usage.update_user_task_yaml(entrypoint)
        dag = dag_utils.load_chain_dag_from_yaml(entrypoint, env_overrides=env)
        if len(dag.tasks) > 1:
            # When the dag has more than 1 task. It is unclear how to
            # override the params for the dag. So we just ignore the
            # override params.
            if override_params:
                click.secho(
                    f'WARNING: override params {override_params} are ignored, '
                    'since the yaml file contains multiple tasks.',
                    fg='yellow')
            return dag
        assert len(dag.tasks) == 1, (
            f'If you see this, please file an issue; tasks: {dag.tasks}')
        task = dag.tasks[0]
    else:
        task = sky.Task(name='sky-cmd', run=entrypoint)
        task.set_resources({sky.Resources()})

    # Override.
    if workdir is not None:
        task.workdir = workdir

    # Spot launch specific.
    if spot_recovery is not None:
        override_params['spot_recovery'] = spot_recovery

    assert len(task.resources) == 1
    old_resources = list(task.resources)[0]
    new_resources = old_resources.copy(**override_params)

    task.set_resources({new_resources})

    if num_nodes is not None:
        task.num_nodes = num_nodes
    if name is not None:
        task.name = name
    task.update_envs(env)
    # TODO(wei-lin): move this validation into Python API.
    if new_resources.accelerators is not None:
        acc, _ = list(new_resources.accelerators.items())[0]
        if acc.startswith('tpu-') and task.num_nodes > 1:
            raise ValueError('Multi-node TPU cluster is not supported. '
                             f'Got num_nodes={task.num_nodes}.')
    return task


class _NaturalOrderGroup(click.Group):
    """Lists commands in the order defined in this script.

    Reference: https://github.com/pallets/click/issues/513
    """

    def list_commands(self, ctx):
        return self.commands.keys()

    @usage_lib.entrypoint('sky.cli', fallback=True)
    def invoke(self, ctx):
        return super().invoke(ctx)


class _DocumentedCodeCommand(click.Command):
    """Corrects help strings for documented commands such that --help displays
    properly and code blocks are rendered in the official web documentation.
    """

    def get_help(self, ctx):
        help_str = ctx.command.help
        ctx.command.help = help_str.replace('.. code-block:: bash\n', '\b')
        return super().get_help(ctx)


def _with_deprecation_warning(f, original_name, alias_name):

    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        click.secho(
            f'WARNING: `{alias_name}` is deprecated and will be removed in a '
            f'future release. Please use `{original_name}` instead.\n',
            err=True,
            fg='yellow')
        return f(self, *args, **kwargs)

    return wrapper


def _add_command_alias_to_group(group, command, name, hidden):
    """Add a alias of a command to a group."""
    new_command = copy.deepcopy(command)
    new_command.hidden = hidden
    new_command.name = name

    orig = f'sky {group.name} {command.name}'
    alias = f'sky {group.name} {name}'
    new_command.invoke = _with_deprecation_warning(new_command.invoke, orig,
                                                   alias)
    group.add_command(new_command, name=name)


@click.group(cls=_NaturalOrderGroup, context_settings=_CONTEXT_SETTINGS)
@click.option('--install-shell-completion',
              type=click.Choice(['bash', 'zsh', 'fish', 'auto']),
              callback=_install_shell_completion,
              expose_value=False,
              is_eager=True,
              help='Install shell completion for the specified shell.')
@click.option('--uninstall-shell-completion',
              type=click.Choice(['bash', 'zsh', 'fish', 'auto']),
              callback=_uninstall_shell_completion,
              expose_value=False,
              is_eager=True,
              help='Uninstall shell completion for the specified shell.')
@click.version_option(sky.__version__, '--version', '-v', prog_name='skypilot')
@click.version_option(sky.__commit__,
                      '--commit',
                      '-c',
                      prog_name='skypilot',
                      message='%(prog)s, commit %(version)s',
                      help='Show the commit hash and exit')
def cli():
    pass


@cli.command(cls=_DocumentedCodeCommand)
@click.argument('entrypoint',
                required=False,
                type=str,
                nargs=-1,
                **_get_shell_complete_args(_complete_file_name))
@click.option('--cluster',
              '-c',
              default=None,
              type=str,
              **_get_shell_complete_args(_complete_cluster_name),
              help=_CLUSTER_FLAG_HELP)
@click.option('--dryrun',
              default=False,
              is_flag=True,
              help='If True, do not actually run the job.')
@click.option(
    '--detach-setup',
    '-s',
    default=False,
    is_flag=True,
    help=
    ('If True, run setup in non-interactive mode as part of the job itself. '
     'You can safely ctrl-c to detach from logging, and it will not interrupt '
     'the setup process. To see the logs again after detaching, use `sky logs`.'
     ' To cancel setup, cancel the job via `sky cancel`. Useful for long-'
     'running setup commands.'))
@click.option(
    '--detach-run',
    '-d',
    default=False,
    is_flag=True,
    help=('If True, as soon as a job is submitted, return from this call '
          'and do not stream execution logs.'))
@click.option('--docker',
              'backend_name',
              flag_value=backends.LocalDockerBackend.NAME,
              default=False,
              help='If used, runs locally inside a docker container.')
@_add_click_options(_TASK_OPTIONS + _EXTRA_RESOURCES_OPTIONS)
@click.option('--cpus',
              default=None,
              type=str,
              required=False,
              help=('Number of vCPUs each instance must have (e.g., '
                    '``--cpus=4`` (exactly 4) or ``--cpus=4+`` (at least 4)). '
                    'This is used to automatically select the instance type.'))
@click.option(
    '--memory',
    default=None,
    type=str,
    required=False,
    help=('Amount of memory each instance must have in GB (e.g., '
          '``--memory=16`` (exactly 16GB), ``--memory=16+`` (at least 16GB))'))
@click.option('--disk-size',
              default=None,
              type=int,
              required=False,
              help=('OS disk size in GBs.'))
@click.option(
    '--disk-tier',
    default=None,
    type=click.Choice(['low', 'medium', 'high'], case_sensitive=False),
    required=False,
    help=(
        'OS disk tier. Could be one of "low", "medium", "high". Default: medium'
    ))
@click.option(
    '--idle-minutes-to-autostop',
    '-i',
    default=None,
    type=int,
    required=False,
    help=('Automatically stop the cluster after this many minutes '
          'of idleness, i.e., no running or pending jobs in the cluster\'s job '
          'queue. Idleness gets reset whenever setting-up/running/pending jobs '
          'are found in the job queue. '
          'Setting this flag is equivalent to '
          'running ``sky launch -d ...`` and then ``sky autostop -i <minutes>``'
          '. If not set, the cluster will not be autostopped.'))
@click.option(
    '--down',
    default=False,
    is_flag=True,
    required=False,
    help=
    ('Autodown the cluster: tear down the cluster after all jobs finish '
     '(successfully or abnormally). If --idle-minutes-to-autostop is also set, '
     'the cluster will be torn down after the specified idle time. '
     'Note that if errors occur during provisioning/data syncing/setting up, '
     'the cluster will not be torn down for debugging purposes.'),
)
@click.option(
    '--retry-until-up',
    '-r',
    default=False,
    is_flag=True,
    required=False,
    # Disabling quote check here, as there seems to be a bug in pylint,
    # which incorrectly recognizes the help string as a docstring.
    # pylint: disable=bad-docstring-quotes
    help=('Whether to retry provisioning infinitely until the cluster is up, '
          'if we fail to launch the cluster on any possible region/cloud due '
          'to unavailability errors.'),
)
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@click.option('--no-setup',
              is_flag=True,
              default=False,
              required=False,
              help='Skip setup phase when (re-)launching cluster.')
@click.option(
    '--clone-disk-from',
    '--clone',
    default=None,
    type=str,
    **_get_shell_complete_args(_complete_cluster_name),
    help=('[Experimental] Clone disk from an existing cluster to launch '
          'a new one. This is useful when the new cluster needs to have '
          'the same data on the boot disk as an existing cluster.'))
@usage_lib.entrypoint
def launch(
    entrypoint: List[str],
    cluster: Optional[str],
    dryrun: bool,
    detach_setup: bool,
    detach_run: bool,
    backend_name: Optional[str],
    name: Optional[str],
    workdir: Optional[str],
    cloud: Optional[str],
    region: Optional[str],
    zone: Optional[str],
    gpus: Optional[str],
    cpus: Optional[str],
    memory: Optional[str],
    instance_type: Optional[str],
    num_nodes: Optional[int],
    use_spot: Optional[bool],
    image_id: Optional[str],
    env_file: Optional[Dict[str, str]],
    env: List[Tuple[str, str]],
    disk_size: Optional[int],
    disk_tier: Optional[str],
    ports: Tuple[str],
    idle_minutes_to_autostop: Optional[int],
    down: bool,  # pylint: disable=redefined-outer-name
    retry_until_up: bool,
    yes: bool,
    no_setup: bool,
    clone_disk_from: Optional[str],
):
    """Launch a task from a YAML or a command (rerun setup if cluster exists).

    If ENTRYPOINT points to a valid YAML file, it is read in as the task
    specification. Otherwise, it is interpreted as a bash command.

    In both cases, the commands are run under the task's workdir (if specified)
    and they undergo job queue scheduling.
    """
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    env = _merge_env_vars(env_file, env)
    backend_utils.check_cluster_name_not_reserved(
        cluster, operation_str='Launching tasks on it')
    if backend_name is None:
        backend_name = backends.CloudVmRayBackend.NAME

    # A basic check. Programmatic calls will have a proper (but less
    # informative) error from optimizer.
    if (cloud is not None and cloud.lower() == 'azure' and
            use_spot is not None and use_spot):
        raise click.UsageError(
            'SkyPilot currently has not implemented '
            'support for spot instances on Azure. Please file '
            'an issue if you need this feature.')

    task_or_dag = _make_task_or_dag_from_entrypoint_with_overrides(
        entrypoint=entrypoint,
        name=name,
        cluster=cluster,
        workdir=workdir,
        cloud=cloud,
        region=region,
        zone=zone,
        gpus=gpus,
        cpus=cpus,
        memory=memory,
        instance_type=instance_type,
        num_nodes=num_nodes,
        use_spot=use_spot,
        image_id=image_id,
        env=env,
        disk_size=disk_size,
        disk_tier=disk_tier,
        ports=ports,
    )
    if isinstance(task_or_dag, sky.Dag):
        raise click.UsageError(
            'YAML specifies a DAG which is only supported by '
            '`sky spot launch`. `sky launch` supports a '
            'single task only.')
    task = task_or_dag

    backend: backends.Backend
    if backend_name == backends.LocalDockerBackend.NAME:
        backend = backends.LocalDockerBackend()
    elif backend_name == backends.CloudVmRayBackend.NAME:
        backend = backends.CloudVmRayBackend()
    else:
        with ux_utils.print_exception_no_traceback():
            raise ValueError(f'{backend_name} backend is not supported.')

    _launch_with_confirm(task,
                         backend,
                         cluster,
                         dryrun=dryrun,
                         detach_setup=detach_setup,
                         detach_run=detach_run,
                         no_confirm=yes,
                         idle_minutes_to_autostop=idle_minutes_to_autostop,
                         down=down,
                         retry_until_up=retry_until_up,
                         no_setup=no_setup,
                         clone_disk_from=clone_disk_from)


@cli.command(cls=_DocumentedCodeCommand)
@click.argument('cluster',
                required=True,
                type=str,
                **_get_shell_complete_args(_complete_cluster_name))
@click.argument('entrypoint',
                required=True,
                type=str,
                nargs=-1,
                **_get_shell_complete_args(_complete_file_name))
@click.option(
    '--detach-run',
    '-d',
    default=False,
    is_flag=True,
    help=('If True, as soon as a job is submitted, return from this call '
          'and do not stream execution logs.'))
@_add_click_options(_TASK_OPTIONS + _EXTRA_RESOURCES_OPTIONS)
@usage_lib.entrypoint
# pylint: disable=redefined-builtin
def exec(
    cluster: str,
    entrypoint: List[str],
    detach_run: bool,
    name: Optional[str],
    cloud: Optional[str],
    region: Optional[str],
    zone: Optional[str],
    workdir: Optional[str],
    gpus: Optional[str],
    ports: Tuple[str],
    instance_type: Optional[str],
    num_nodes: Optional[int],
    use_spot: Optional[bool],
    image_id: Optional[str],
    env_file: Optional[Dict[str, str]],
    env: List[Tuple[str, str]],
):
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Execute a task or a command on a cluster (skip setup).

    If ENTRYPOINT points to a valid YAML file, it is read in as the task
    specification. Otherwise, it is interpreted as a bash command.

    Actions performed by ``sky exec``:

    1. Workdir syncing, if:

       - ENTRYPOINT is a YAML with the ``workdir`` field specified; or

       - Flag ``--workdir=<local_dir>`` is set.

    2. Executing the specified task's ``run`` commands / the bash command.

    ``sky exec`` is thus typically faster than ``sky launch``, provided a
    cluster already exists.

    All setup steps (provisioning, setup commands, file mounts syncing) are
    skipped.  If any of those specifications changed, this command will not
    reflect those changes.  To ensure a cluster's setup is up to date, use ``sky
    launch`` instead.

    Execution and scheduling behavior:

    - The task/command will undergo job queue scheduling, respecting any
      specified resource requirement. It can be executed on any node of the
      cluster with enough resources.

    - The task/command is run under the workdir (if specified).

    - The task/command is run non-interactively (without a pseudo-terminal or
      pty), so interactive commands such as ``htop`` do not work. Use ``ssh
      my_cluster`` instead.

    Typical workflow:

    .. code-block:: bash

      # First command: set up the cluster once.
      sky launch -c mycluster app.yaml
      \b
      # For iterative development, simply execute the task on the launched
      # cluster.
      sky exec mycluster app.yaml
      \b
      # Do "sky launch" again if anything other than Task.run is modified:
      sky launch -c mycluster app.yaml
      \b
      # Pass in commands for execution.
      sky exec mycluster python train_cpu.py
      sky exec mycluster --gpus=V100:1 python train_gpu.py
      \b
      # Pass environment variables to the task.
      sky exec mycluster --env WANDB_API_KEY python train_gpu.py

    """
    if ports:
        raise ValueError('`ports` is not supported by `sky exec`.')

    env = _merge_env_vars(env_file, env)
    backend_utils.check_cluster_name_not_reserved(
        cluster, operation_str='Executing task on it')
    handle = global_user_state.get_handle_from_cluster_name(cluster)
    if handle is None:
        if onprem_utils.check_if_local_cloud(cluster):
            raise click.BadParameter(
                constants.UNINITIALIZED_ONPREM_CLUSTER_MESSAGE.format(
                    cluster=cluster))
        raise click.BadParameter(f'Cluster {cluster!r} not found. '
                                 'Use `sky launch` to provision first.')
    backend = backend_utils.get_backend_from_handle(handle)

    task_or_dag = _make_task_or_dag_from_entrypoint_with_overrides(
        entrypoint=entrypoint,
        name=name,
        cluster=cluster,
        workdir=workdir,
        cloud=cloud,
        region=region,
        zone=zone,
        gpus=gpus,
        cpus=None,
        memory=None,
        instance_type=instance_type,
        use_spot=use_spot,
        image_id=image_id,
        num_nodes=num_nodes,
        env=env,
    )

    if isinstance(task_or_dag, sky.Dag):
        raise click.UsageError('YAML specifies a DAG, while `sky exec` '
                               'supports a single task only.')
    task = task_or_dag

    click.secho(f'Executing task on cluster {cluster}...', fg='yellow')
    sky.exec(task, backend=backend, cluster_name=cluster, detach_run=detach_run)


def _get_spot_jobs(
        refresh: bool,
        skip_finished: bool,
        show_all: bool,
        limit_num_jobs_to_show: bool = False,
        is_called_by_user: bool = False) -> Tuple[Optional[int], str]:
    """Get the in-progress spot jobs.

    Args:
        refresh: Query the latest statuses, restarting the spot controller if
            stopped.
        skip_finished: Show only in-progress jobs.
        show_all: Show all information of each spot job (e.g., region, price).
        limit_num_jobs_to_show: If True, limit the number of jobs to show to
            _NUM_SPOT_JOBS_TO_SHOW_IN_STATUS, which is mainly used by
            `sky status`.
        is_called_by_user: If this function is called by user directly, or an
            internal call.

    Returns:
        A tuple of (num_in_progress_jobs, msg). If num_in_progress_jobs is None,
        it means there is an error when querying the spot jobs. In this case,
        msg contains the error message. Otherwise, msg contains the formatted
        spot job table.
    """
    num_in_progress_jobs = None
    try:
        if not is_called_by_user:
            usage_lib.messages.usage.set_internal()
        with sky_logging.silent():
            # Make the call silent
            spot_jobs = core.spot_queue(refresh=refresh,
                                        skip_finished=skip_finished)
        num_in_progress_jobs = len(spot_jobs)
    except exceptions.ClusterNotUpError as e:
        controller_status = e.cluster_status
        if controller_status == status_lib.ClusterStatus.INIT:
            msg = ('Controller\'s latest status is INIT; jobs '
                   'will not be shown until it becomes UP.')
        else:
            assert controller_status in [None, status_lib.ClusterStatus.STOPPED]
            msg = 'No in progress jobs.'
            if controller_status is None:
                msg += (f' (See: {colorama.Style.BRIGHT}sky spot -h'
                        f'{colorama.Style.RESET_ALL})')
    except RuntimeError as e:
        msg = ('Failed to query spot jobs due to connection '
               'issues. Try again later. '
               f'Details: {common_utils.format_exception(e, use_bracket=True)}')
    except Exception as e:  # pylint: disable=broad-except
        msg = ('Failed to query spot jobs: '
               f'{common_utils.format_exception(e, use_bracket=True)}')
    else:
        max_jobs_to_show = (_NUM_SPOT_JOBS_TO_SHOW_IN_STATUS
                            if limit_num_jobs_to_show else None)
        msg = spot_lib.format_job_table(spot_jobs,
                                        show_all=show_all,
                                        max_jobs=max_jobs_to_show)
    return num_in_progress_jobs, msg


@cli.command()
@click.option('--all',
              '-a',
              default=False,
              is_flag=True,
              required=False,
              help='Show all information in full.')
@click.option(
    '--refresh',
    '-r',
    default=False,
    is_flag=True,
    required=False,
    help='Query the latest cluster statuses from the cloud provider(s).')
@click.option('--ip',
              default=False,
              is_flag=True,
              required=False,
              help=('Get the IP address of the head node of a cluster. This '
                    'option will override all other options. For Kubernetes '
                    'clusters, the returned IP address is the internal IP '
                    'of the head pod, and may not be accessible from outside '
                    'the cluster.'))
@click.option('--show-spot-jobs/--no-show-spot-jobs',
              default=True,
              is_flag=True,
              required=False,
              help='Also show recent in-progress spot jobs, if any.')
@click.argument('clusters',
                required=False,
                type=str,
                nargs=-1,
                **_get_shell_complete_args(_complete_cluster_name))
@usage_lib.entrypoint
# pylint: disable=redefined-builtin
def status(all: bool, refresh: bool, ip: bool, show_spot_jobs: bool,
           clusters: List[str]):
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Show clusters.

    If CLUSTERS is given, show those clusters. Otherwise, show all clusters.

    If --ip is specified, show the IP address of the head node of the cluster.
    Only available when CLUSTERS contains exactly one cluster, e.g.
    ``sky status --ip mycluster``.

    The following fields for each cluster are recorded: cluster name, time
    since last launch, resources, region, zone, hourly price, status, autostop,
    command.

    Display all fields using ``sky status -a``.

    Each cluster can have one of the following statuses:

    - ``INIT``: The cluster may be live or down. It can happen in the following
      cases:

      - Ongoing provisioning or runtime setup. (A ``sky launch`` has started
        but has not completed.)

      - Or, the cluster is in an abnormal state, e.g., some cluster nodes are
        down, or the SkyPilot runtime is unhealthy. (To recover the cluster,
        try ``sky launch`` again on it.)

    - ``UP``: Provisioning and runtime setup have succeeded and the cluster is
      live.  (The most recent ``sky launch`` has completed successfully.)

    - ``STOPPED``: The cluster is stopped and the storage is persisted. Use
      ``sky start`` to restart the cluster.

    Autostop column:

    - Indicates after how many minutes of idleness (no in-progress jobs) the
      cluster will be autostopped. '-' means disabled.

    - If the time is followed by '(down)', e.g., '1m (down)', the cluster will
      be autodowned, rather than autostopped.

    Getting up-to-date cluster statuses:

    - In normal cases where clusters are entirely managed by SkyPilot (i.e., no
      manual operations in cloud consoles) and no autostopping is used, the
      table returned by this command will accurately reflect the cluster
      statuses.

    - In cases where clusters are changed outside of SkyPilot (e.g., manual
      operations in cloud consoles; unmanaged spot clusters getting preempted)
      or for autostop-enabled clusters, use ``--refresh`` to query the latest
      cluster statuses from the cloud providers.
    """
    # Using a pool with 1 worker to run the spot job query in parallel to speed
    # up. The pool provides a AsyncResult object that can be used as a future.
    with multiprocessing.Pool(1) as pool:
        # Do not show spot queue if user specifies clusters, and if user
        # specifies --ip.
        show_spot_jobs = show_spot_jobs and not clusters and not ip
        if show_spot_jobs:
            # Run the spot job query in parallel to speed up the status query.
            spot_jobs_future = pool.apply_async(
                _get_spot_jobs,
                kwds=dict(refresh=False,
                          skip_finished=True,
                          show_all=False,
                          limit_num_jobs_to_show=not all,
                          is_called_by_user=False))
        if ip:
            if refresh:
                raise click.UsageError(
                    'Using --ip with --refresh is not supported for now. '
                    'To fix, refresh first, then query the IP.')
            if len(clusters) != 1:
                with ux_utils.print_exception_no_traceback():
                    plural = 's' if len(clusters) > 1 else ''
                    cluster_num = (str(len(clusters))
                                   if len(clusters) > 0 else 'No')
                    raise ValueError(
                        _STATUS_IP_CLUSTER_NUM_ERROR_MESSAGE.format(
                            cluster_num=cluster_num,
                            plural=plural,
                            verb='specified'))
        else:
            click.echo(f'{colorama.Fore.CYAN}{colorama.Style.BRIGHT}Clusters'
                       f'{colorama.Style.RESET_ALL}')
        query_clusters: Optional[List[str]] = None
        if clusters:
            query_clusters = _get_glob_clusters(clusters, silent=ip)
        cluster_records = core.status(cluster_names=query_clusters,
                                      refresh=refresh)
        if ip:
            if len(cluster_records) != 1:
                with ux_utils.print_exception_no_traceback():
                    plural = 's' if len(cluster_records) > 1 else ''
                    cluster_num = (str(len(cluster_records))
                                   if len(clusters) > 0 else 'No')
                    raise ValueError(
                        _STATUS_IP_CLUSTER_NUM_ERROR_MESSAGE.format(
                            cluster_num=cluster_num,
                            plural=plural,
                            verb='found'))
            cluster_record = cluster_records[0]
            if cluster_record['status'] != status_lib.ClusterStatus.UP:
                with ux_utils.print_exception_no_traceback():
                    raise RuntimeError(f'Cluster {cluster_record["name"]!r} '
                                       'is not in UP status.')
            handle = cluster_record['handle']
            if not isinstance(handle, backends.CloudVmRayResourceHandle):
                with ux_utils.print_exception_no_traceback():
                    raise ValueError('Querying IP address is not supported '
                                     'for local clusters.')
            head_ip = handle.external_ips()[0]
            click.echo(head_ip)
            return
        nonreserved_cluster_records = []
        reserved_clusters = []
        for cluster_record in cluster_records:
            cluster_name = cluster_record['name']
            if cluster_name in backend_utils.SKY_RESERVED_CLUSTER_NAMES:
                reserved_clusters.append(cluster_record)
            else:
                nonreserved_cluster_records.append(cluster_record)
        local_clusters = onprem_utils.check_and_get_local_clusters(
            suppress_error=True)

        num_pending_autostop = 0
        num_pending_autostop += status_utils.show_status_table(
            nonreserved_cluster_records + reserved_clusters, all)
        status_utils.show_local_status_table(local_clusters)

        hints = []
        if show_spot_jobs:
            click.echo(f'\n{colorama.Fore.CYAN}{colorama.Style.BRIGHT}'
                       f'Managed spot jobs{colorama.Style.RESET_ALL}')
            with rich_utils.safe_status('[cyan]Checking spot jobs[/]'):
                try:
                    num_in_progress_jobs, msg = spot_jobs_future.get()
                except KeyboardInterrupt:
                    pool.terminate()
                    # Set to -1, so that the controller is not considered
                    # down, and the hint for showing sky spot queue
                    # will still be shown.
                    num_in_progress_jobs = -1
                    msg = 'KeyboardInterrupt'

                try:
                    pool.close()
                    pool.join()
                except SystemExit as e:
                    # This is to avoid a "Exception ignored" problem caused by
                    # ray worker setting the sigterm handler to sys.exit(15)
                    # (see ray/_private/worker.py).
                    # TODO (zhwu): Remove any importing of ray in SkyPilot.
                    if e.code != 15:
                        raise

            click.echo(msg)
            if num_in_progress_jobs is not None:
                # spot controller is UP.
                job_info = ''
                if num_in_progress_jobs > 0:
                    plural_and_verb = ' is'
                    if num_in_progress_jobs > 1:
                        plural_and_verb = 's are'
                    job_info = (
                        f'{num_in_progress_jobs} spot job{plural_and_verb} '
                        'in progress')
                    if num_in_progress_jobs > _NUM_SPOT_JOBS_TO_SHOW_IN_STATUS:
                        job_info += (
                            f' ({_NUM_SPOT_JOBS_TO_SHOW_IN_STATUS} latest ones '
                            'shown)')
                    job_info += '. '
                hints.append(
                    f'* {job_info}To see all spot jobs: {colorama.Style.BRIGHT}'
                    f'sky spot queue{colorama.Style.RESET_ALL}')

        if num_pending_autostop > 0 and not refresh:
            # Don't print this hint if there's no pending autostop or user has
            # already passed --refresh.
            plural_and_verb = ' has'
            if num_pending_autostop > 1:
                plural_and_verb = 's have'
            hints.append(f'* {num_pending_autostop} cluster{plural_and_verb} '
                         'auto{stop,down} scheduled. Refresh statuses with: '
                         f'{colorama.Style.BRIGHT}sky status --refresh'
                         f'{colorama.Style.RESET_ALL}')
        if hints:
            click.echo('\n' + '\n'.join(hints))


@cli.command()
@click.option('--all',
              '-a',
              default=False,
              is_flag=True,
              required=False,
              help='Show all information in full.')
@usage_lib.entrypoint
def cost_report(all: bool):  # pylint: disable=redefined-builtin
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Show estimated costs for launched clusters.

    For each cluster, this shows: cluster name, resources, launched time,
    duration that cluster was up, and total estimated cost.

    The estimated cost column indicates the price for the cluster based on the
    type of resources being used and the duration of use up until now. This
    means if the cluster is UP, successive calls to cost-report will show
    increasing price.

    This CLI is experimental. The estimated cost is calculated based on the
    local cache of the cluster status, and may not be accurate for:

    - Clusters with autostop/use_spot set; or

    - Clusters that were terminated/stopped on the cloud console.
    """
    cluster_records = core.cost_report()

    nonreserved_cluster_records = []
    reserved_clusters = dict()
    for cluster_record in cluster_records:
        cluster_name = cluster_record['name']
        if cluster_name in backend_utils.SKY_RESERVED_CLUSTER_NAMES:
            cluster_group_name = backend_utils.SKY_RESERVED_CLUSTER_NAMES[
                cluster_name]
            # to display most recent entry for each reserved cluster
            # TODO(sgurram): fix assumption of sorted order of clusters
            if cluster_group_name not in reserved_clusters:
                reserved_clusters[cluster_group_name] = cluster_record
        else:
            nonreserved_cluster_records.append(cluster_record)

    total_cost = status_utils.get_total_cost_of_displayed_records(
        nonreserved_cluster_records, all)

    status_utils.show_cost_report_table(nonreserved_cluster_records, all)
    for cluster_group_name, cluster_record in reserved_clusters.items():
        status_utils.show_cost_report_table(
            [cluster_record], all, reserved_group_name=cluster_group_name)
        total_cost += cluster_record['total_cost']

    click.echo(f'\n{colorama.Style.BRIGHT}'
               f'Total Cost: ${total_cost:.2f}{colorama.Style.RESET_ALL}')

    if not all:
        click.secho(
            f'Showing up to {status_utils.NUM_COST_REPORT_LINES} '
            'most recent clusters. '
            'To see all clusters in history, '
            'pass the --all flag.',
            fg='yellow')

    click.secho(
        'This feature is experimental. '
        'Costs for clusters with auto{stop,down} '
        'scheduled may not be accurate.',
        fg='yellow')


@cli.command()
@click.option('--all-users',
              '-a',
              default=False,
              is_flag=True,
              required=False,
              help='Show all users\' information in full.')
@click.option('--skip-finished',
              '-s',
              default=False,
              is_flag=True,
              required=False,
              help='Show only pending/running jobs\' information.')
@click.argument('clusters',
                required=False,
                type=str,
                nargs=-1,
                **_get_shell_complete_args(_complete_cluster_name))
@usage_lib.entrypoint
def queue(clusters: List[str], skip_finished: bool, all_users: bool):
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Show the job queue for cluster(s)."""
    click.secho('Fetching and parsing job queue...', fg='yellow')
    show_local_clusters = False
    if clusters:
        clusters = _get_glob_clusters(clusters)
    else:
        show_local_clusters = True
        cluster_infos = global_user_state.get_clusters()
        clusters = [c['name'] for c in cluster_infos]

    unsupported_clusters = []
    for cluster in clusters:
        try:
            job_table = core.queue(cluster, skip_finished, all_users)
        except (RuntimeError, ValueError, exceptions.NotSupportedError,
                exceptions.ClusterNotUpError, exceptions.CloudUserIdentityError,
                exceptions.ClusterOwnerIdentityMismatchError) as e:
            if isinstance(e, exceptions.NotSupportedError):
                unsupported_clusters.append(cluster)
            click.echo(f'{colorama.Fore.YELLOW}Failed to get the job queue for '
                       f'cluster {cluster!r}.{colorama.Style.RESET_ALL}\n'
                       f'  {common_utils.class_fullname(e.__class__)}: '
                       f'{common_utils.remove_color(str(e))}')
            continue
        job_table = job_lib.format_job_queue(job_table)
        click.echo(f'\nJob queue of cluster {cluster}\n{job_table}')

    local_clusters = onprem_utils.check_and_get_local_clusters()
    for local_cluster in local_clusters:
        if local_cluster not in clusters and show_local_clusters:
            click.secho(
                f'Local cluster {local_cluster} is uninitialized;'
                ' skipped.',
                fg='yellow')

    if unsupported_clusters:
        click.secho(
            f'Note: Job queues are not supported on clusters: '
            f'{", ".join(unsupported_clusters)}',
            fg='yellow')


@cli.command()
@click.option(
    '--sync-down',
    '-s',
    is_flag=True,
    default=False,
    help='Sync down the logs of a job to the local machine. For a distributed'
    ' job, a separate log file from each worker will be downloaded.')
@click.option(
    '--status',
    is_flag=True,
    default=False,
    help=('If specified, do not show logs but exit with a status code for the '
          'job\'s status: 0 for succeeded, or 1 for all other statuses.'))
@click.option(
    '--follow/--no-follow',
    is_flag=True,
    default=True,
    help=('Follow the logs of a job. '
          'If --no-follow is specified, print the log so far and exit. '
          '[default: --follow]'))
@click.argument('cluster',
                required=True,
                type=str,
                **_get_shell_complete_args(_complete_cluster_name))
@click.argument('job_ids', type=str, nargs=-1)
# TODO(zhwu): support logs by job name
@usage_lib.entrypoint
def logs(
    cluster: str,
    job_ids: Tuple[str],
    sync_down: bool,
    status: bool,  # pylint: disable=redefined-outer-name
    follow: bool,
):
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Tail the log of a job.

    If JOB_ID is not provided, the latest job on the cluster will be used.

    1. If no flags are provided, tail the logs of the job_id specified. At most
    one job_id can be provided.

    2. If ``--status`` is specified, print the status of the job and exit with
    returncode 0 if the job succeeded, or 1 otherwise. At most one job_id can
    be specified.

    3. If ``--sync-down`` is specified, the logs of the job will be downloaded
    from the cluster and saved to the local machine under
    ``~/sky_logs``. Mulitple job_ids can be specified.
    """
    if sync_down and status:
        raise click.UsageError(
            'Both --sync_down and --status are specified '
            '(ambiguous). To fix: specify at most one of them.')

    if len(job_ids) > 1 and not sync_down:
        raise click.UsageError(
            f'Cannot stream logs of multiple jobs (IDs: {", ".join(job_ids)}).'
            '\nPass -s/--sync-down to download the logs instead.')

    job_ids = None if not job_ids else job_ids

    if sync_down:
        core.download_logs(cluster, job_ids)
        return

    assert job_ids is None or len(job_ids) <= 1, job_ids
    job_id = None
    if job_ids:
        # Already check that len(job_ids) <= 1. This variable is used later
        # in core.tail_logs.
        job_id = job_ids[0]
        if not job_id.isdigit():
            raise click.UsageError(f'Invalid job ID {job_id}. '
                                   'Job ID must be integers.')
        job_ids_to_query = [int(job_id)]
    else:
        job_ids_to_query = job_ids
    if status:
        job_statuses = core.job_status(cluster, job_ids_to_query)
        job_id = list(job_statuses.keys())[0]
        # If job_ids is None and no job has been submitted to the cluster,
        # it will return {None: None}.
        if job_id is None:
            click.secho(f'No job found on cluster {cluster!r}.', fg='red')
            sys.exit(1)
        job_status = list(job_statuses.values())[0]
        job_status_str = job_status.value if job_status is not None else 'None'
        click.echo(f'Job {job_id}: {job_status_str}')
        if job_status == job_lib.JobStatus.SUCCEEDED:
            return
        else:
            if job_status is None:
                id_str = '' if job_id is None else f'{job_id} '
                click.secho(f'Job {id_str}not found', fg='red')
            sys.exit(1)

    core.tail_logs(cluster, job_id, follow)


@cli.command()
@click.argument('cluster',
                required=True,
                type=str,
                **_get_shell_complete_args(_complete_cluster_name))
@click.option('--all',
              '-a',
              default=False,
              is_flag=True,
              required=False,
              help='Cancel all jobs on the specified cluster.')
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@click.argument('jobs', required=False, type=int, nargs=-1)
@usage_lib.entrypoint
def cancel(cluster: str, all: bool, jobs: List[int], yes: bool):  # pylint: disable=redefined-builtin
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Cancel job(s).

    Example usage:

    .. code-block:: bash

      \b
      # Cancel specific jobs on a cluster.
      sky cancel cluster_name 1
      sky cancel cluster_name 1 2 3
      \b
      # Cancel all jobs on a cluster.
      sky cancel cluster_name -a
      \b
      # Cancel the latest running job on a cluster.
      sky cancel cluster_name

    Job IDs can be looked up by ``sky queue cluster_name``.
    """
    bold = colorama.Style.BRIGHT
    reset = colorama.Style.RESET_ALL
    job_identity_str = None
    job_ids_to_cancel = None
    if not jobs and not all:
        click.echo(f'{colorama.Fore.YELLOW}No job IDs or --all provided; '
                   'cancelling the latest running job.'
                   f'{colorama.Style.RESET_ALL}')
        job_identity_str = 'the latest running job'
    else:
        # Cancelling specific jobs or --all.
        job_ids = ' '.join(map(str, jobs))
        plural = 's' if len(job_ids) > 1 else ''
        job_identity_str = f'job{plural} {job_ids}'
        job_ids_to_cancel = jobs
        if all:
            job_identity_str = 'all jobs'
            job_ids_to_cancel = None
    job_identity_str += f' on cluster {cluster!r}'

    if not yes:
        click.confirm(f'Cancelling {job_identity_str}. Proceed?',
                      default=True,
                      abort=True,
                      show_default=True)

    try:
        core.cancel(cluster, all=all, job_ids=job_ids_to_cancel)
    except exceptions.NotSupportedError:
        # Friendly message for usage like 'sky cancel <spot controller> -a/<job
        # id>'.
        error_str = ('Cancelling the spot controller\'s jobs is not allowed.'
                     f'\nTo cancel spot jobs, use: {bold}sky spot cancel <spot '
                     f'job IDs> [--all]{reset}')
        click.echo(error_str)
        sys.exit(1)
    except ValueError as e:
        raise click.UsageError(str(e))
    except exceptions.ClusterNotUpError as e:
        click.echo(str(e))
        sys.exit(1)


@cli.command(cls=_DocumentedCodeCommand)
@click.argument('clusters',
                nargs=-1,
                required=False,
                **_get_shell_complete_args(_complete_cluster_name))
@click.option('--all',
              '-a',
              default=None,
              is_flag=True,
              help='Stop all existing clusters.')
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@usage_lib.entrypoint
def stop(
    clusters: List[str],
    all: Optional[bool],  # pylint: disable=redefined-builtin
    yes: bool,
):
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Stop cluster(s).

    CLUSTER is the name (or glob pattern) of the cluster to stop.  If both
    CLUSTER and ``--all`` are supplied, the latter takes precedence.

    Data on attached disks is not lost when a cluster is stopped.  Billing for
    the instances will stop, while the disks will still be charged.  Those
    disks will be reattached when restarting the cluster.

    Currently, spot instance clusters cannot be stopped.

    Examples:

    .. code-block:: bash

      # Stop a specific cluster.
      sky stop cluster_name
      \b
      # Stop multiple clusters.
      sky stop cluster1 cluster2
      \b
      # Stop all clusters matching glob pattern 'cluster*'.
      sky stop "cluster*"
      \b
      # Stop all existing clusters.
      sky stop -a

    """
    _down_or_stop_clusters(clusters,
                           apply_to_all=all,
                           down=False,
                           no_confirm=yes)


@cli.command(cls=_DocumentedCodeCommand)
@click.argument('clusters',
                nargs=-1,
                required=False,
                **_get_shell_complete_args(_complete_cluster_name))
@click.option('--all',
              '-a',
              default=None,
              is_flag=True,
              help='Apply this command to all existing clusters.')
@click.option('--idle-minutes',
              '-i',
              type=int,
              default=None,
              required=False,
              help=('Set the idle minutes before autostopping the cluster. '
                    'See the doc above for detailed semantics.'))
@click.option(
    '--cancel',
    default=False,
    is_flag=True,
    required=False,
    help='Cancel any currently active auto{stop,down} setting for the '
    'cluster. No-op if there is no active setting.')
@click.option(
    '--down',
    default=False,
    is_flag=True,
    required=False,
    help='Use autodown (tear down the cluster; non-restartable), instead '
    'of autostop (restartable).')
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@usage_lib.entrypoint
def autostop(
    clusters: List[str],
    all: Optional[bool],  # pylint: disable=redefined-builtin
    idle_minutes: Optional[int],
    cancel: bool,  # pylint: disable=redefined-outer-name
    down: bool,  # pylint: disable=redefined-outer-name
    yes: bool,
):
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Schedule an autostop or autodown for cluster(s).

    Autostop/autodown will automatically stop or teardown a cluster when it
    becomes idle for a specified duration.  Idleness means there are no
    in-progress (pending/running) jobs in a cluster's job queue.

    CLUSTERS are the names (or glob patterns) of the clusters to stop. If both
    CLUSTERS and ``--all`` are supplied, the latter takes precedence.

    Idleness time of a cluster is reset to zero, when any of these happens:

    - A job is submitted (``sky launch`` or ``sky exec``).

    - The cluster has restarted.

    - An autostop is set when there is no active setting. (Namely, either
      there's never any autostop setting set, or the previous autostop setting
      was canceled.) This is useful for restarting the autostop timer.

    Example: say a cluster without any autostop set has been idle for 1 hour,
    then an autostop of 30 minutes is set. The cluster will not be immediately
    autostopped. Instead, the idleness timer only starts counting after the
    autostop setting was set.

    When multiple autostop settings are specified for the same cluster, the
    last setting takes precedence.

    Typical usage:

    .. code-block:: bash

        # Autostop this cluster after 60 minutes of idleness.
        sky autostop cluster_name -i 60
        \b
        # Cancel autostop for a specific cluster.
        sky autostop cluster_name --cancel
        \b
        # Since autostop was canceled in the last command, idleness will
        # restart counting after this command.
        sky autostop cluster_name -i 60
    """
    if cancel and idle_minutes is not None:
        raise click.UsageError(
            'Only one of --idle-minutes and --cancel should be specified. '
            f'cancel: {cancel}, idle_minutes: {idle_minutes}')
    if cancel:
        idle_minutes = -1
    elif idle_minutes is None:
        idle_minutes = 5
    _down_or_stop_clusters(clusters,
                           apply_to_all=all,
                           down=down,
                           no_confirm=yes,
                           idle_minutes_to_autostop=idle_minutes)


@cli.command(cls=_DocumentedCodeCommand)
@click.argument('clusters',
                nargs=-1,
                required=False,
                **_get_shell_complete_args(_complete_cluster_name))
@click.option('--all',
              '-a',
              default=False,
              is_flag=True,
              required=False,
              help='Start all existing clusters.')
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@click.option(
    '--idle-minutes-to-autostop',
    '-i',
    default=None,
    type=int,
    required=False,
    help=('Automatically stop the cluster after this many minutes '
          'of idleness, i.e., no running or pending jobs in the cluster\'s job '
          'queue. Idleness gets reset whenever setting-up/running/pending jobs '
          'are found in the job queue. '
          'Setting this flag is equivalent to '
          'running ``sky launch -d ...`` and then ``sky autostop -i <minutes>``'
          '. If not set, the cluster will not be autostopped.'))
@click.option(
    '--down',
    default=False,
    is_flag=True,
    required=False,
    help=
    ('Autodown the cluster: tear down the cluster after specified minutes of '
     'idle time after all jobs finish (successfully or abnormally). Requires '
     '--idle-minutes-to-autostop to be set.'),
)
@click.option(
    '--retry-until-up',
    '-r',
    default=False,
    is_flag=True,
    required=False,
    # Disabling quote check here, as there seems to be a bug in pylint,
    # which incorrectly recognizes the help string as a docstring.
    # pylint: disable=bad-docstring-quotes
    help=('Retry provisioning infinitely until the cluster is up, '
          'if we fail to start the cluster due to unavailability errors.'),
)
@click.option(
    '--force',
    '-f',
    default=False,
    is_flag=True,
    required=False,
    help=('Force start the cluster even if it is already UP. Useful for '
          'upgrading the SkyPilot runtime on the cluster.'))
@usage_lib.entrypoint
# pylint: disable=redefined-builtin
def start(
        clusters: List[str],
        all: bool,
        yes: bool,
        idle_minutes_to_autostop: Optional[int],
        down: bool,  # pylint: disable=redefined-outer-name
        retry_until_up: bool,
        force: bool):
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Restart cluster(s).

    If a cluster is previously stopped (status is STOPPED) or failed in
    provisioning/runtime installation (status is INIT), this command will
    attempt to start the cluster.  In the latter case, provisioning and runtime
    installation will be retried.

    Auto-failover provisioning is not used when restarting a stopped
    cluster. It will be started on the same cloud, region, and zone that were
    chosen before.

    If a cluster is already in the UP status, this command has no effect.

    Examples:

    .. code-block:: bash

      # Restart a specific cluster.
      sky start cluster_name
      \b
      # Restart multiple clusters.
      sky start cluster1 cluster2
      \b
      # Restart all clusters.
      sky start -a

    """
    if down and idle_minutes_to_autostop is None:
        raise click.UsageError(
            '--idle-minutes-to-autostop must be set if --down is set.')
    to_start = []

    if not clusters and not all:
        # UX: frequently users may have only 1 cluster. In this case, be smart
        # and default to that unique choice.
        all_cluster_names = global_user_state.get_cluster_names_start_with('')
        if len(all_cluster_names) <= 1:
            clusters = all_cluster_names
        else:
            raise click.UsageError(
                '`sky start` requires either a cluster name or glob '
                '(see `sky status`), or the -a/--all flag.')

    if all:
        if len(clusters) > 0:
            click.echo('Both --all and cluster(s) specified for sky start. '
                       'Letting --all take effect.')

        # Get all clusters that are not reserved names.
        clusters = [
            cluster['name']
            for cluster in global_user_state.get_clusters()
            if cluster['name'] not in backend_utils.SKY_RESERVED_CLUSTER_NAMES
        ]

    if not clusters:
        click.echo('Cluster(s) not found (tip: see `sky status`). Do you '
                   'mean to use `sky launch` to provision a new cluster?')
        return
    else:
        # Get GLOB cluster names
        clusters = _get_glob_clusters(clusters)
        local_clusters = onprem_utils.check_and_get_local_clusters()
        clusters = [
            c for c in clusters
            if _warn_if_local_cluster(c, local_clusters, (
                f'Skipping local cluster {c}, as it does not support '
                '`sky start`.'))
        ]

        for name in clusters:
            cluster_status, _ = backend_utils.refresh_cluster_status_handle(
                name)
            # A cluster may have one of the following states:
            #
            #  STOPPED - ok to restart
            #    (currently, only AWS/GCP non-spot clusters can be in this
            #    state)
            #
            #  UP - skipped, see below
            #
            #  INIT - ok to restart:
            #    1. It can be a failed-to-provision cluster, so it isn't up
            #      (Ex: gpunode --gpus=A100:8).  Running `sky start` enables
            #      retrying the provisioning - without setup steps being
            #      completed. (Arguably the original command that failed should
            #      be used instead; but using start isn't harmful - after it
            #      gets provisioned successfully the user can use the original
            #      command).
            #
            #    2. It can be an up cluster that failed one of the setup steps.
            #      This way 'sky start' can change its status to UP, enabling
            #      'sky ssh' to debug things (otherwise `sky ssh` will fail an
            #      INIT state cluster due to head_ip not being cached).
            #
            #      This can be replicated by adding `exit 1` to Task.setup.
            if (not force and cluster_status == status_lib.ClusterStatus.UP):
                # An UP cluster; skipping 'sky start' because:
                #  1. For a really up cluster, this has no effects (ray up -y
                #    --no-restart) anyway.
                #  2. A cluster may show as UP but is manually stopped in the
                #    UI.  If Azure/GCP: ray autoscaler doesn't support reusing,
                #    so 'sky start existing' will actually launch a new
                #    cluster with this name, leaving the original cluster
                #    zombied (remains as stopped in the cloud's UI).
                #
                #    This is dangerous and unwanted behavior!
                click.echo(f'Cluster {name} already has status UP.')
                continue

            assert force or cluster_status in (
                status_lib.ClusterStatus.INIT,
                status_lib.ClusterStatus.STOPPED), cluster_status
            to_start.append(name)
    if not to_start:
        return

    # Checks for reserved clusters (spot controller).
    reserved, non_reserved = [], []
    for name in to_start:
        if name in backend_utils.SKY_RESERVED_CLUSTER_NAMES:
            reserved.append(name)
        else:
            non_reserved.append(name)
    if reserved and non_reserved:
        assert len(reserved) == 1, reserved
        # Keep this behavior the same as _down_or_stop_clusters().
        raise click.UsageError(
            'Starting the spot controller with other cluster(s) '
            'is currently not supported.\n'
            'Please start the former independently.')
    if reserved:
        assert len(reserved) == 1, reserved
        bold = backend_utils.BOLD
        reset_bold = backend_utils.RESET_BOLD
        if idle_minutes_to_autostop is not None:
            raise click.UsageError(
                'Autostop options are currently not allowed when starting the '
                'spot controller. Use the default autostop settings by directly'
                f' calling: {bold}sky start {reserved[0]}{reset_bold}')

    if not yes:
        cluster_str = 'clusters' if len(to_start) > 1 else 'cluster'
        cluster_list = ', '.join(to_start)
        click.confirm(
            f'Restarting {len(to_start)} {cluster_str}: '
            f'{cluster_list}. Proceed?',
            default=True,
            abort=True,
            show_default=True)

    for name in to_start:
        try:
            core.start(name,
                       idle_minutes_to_autostop,
                       retry_until_up,
                       down=down,
                       force=force)
        except (exceptions.NotSupportedError,
                exceptions.ClusterOwnerIdentityMismatchError) as e:
            click.echo(str(e))
        else:
            click.secho(f'Cluster {name} started.', fg='green')


@cli.command(cls=_DocumentedCodeCommand)
@click.argument('clusters',
                nargs=-1,
                required=False,
                **_get_shell_complete_args(_complete_cluster_name))
@click.option('--all',
              '-a',
              default=None,
              is_flag=True,
              help='Tear down all existing clusters.')
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@click.option('--purge',
              '-p',
              is_flag=True,
              default=False,
              required=False,
              help='Ignore cloud provider errors (if any). '
              'Useful for cleaning up manually deleted cluster(s).')
@usage_lib.entrypoint
def down(
    clusters: List[str],
    all: Optional[bool],  # pylint: disable=redefined-builtin
    yes: bool,
    purge: bool,
):
    # NOTE(dev): Keep the docstring consistent between the Python API and CLI.
    """Tear down cluster(s).

    CLUSTER is the name of the cluster (or glob pattern) to tear down.  If both
    CLUSTER and ``--all`` are supplied, the latter takes precedence.

    Tearing down a cluster will delete all associated resources (all billing
    stops), and any data on the attached disks will be lost.  Accelerators
    (e.g., TPUs) that are part of the cluster will be deleted too.

    For local on-prem clusters, this command does not terminate the local
    cluster, but instead removes the cluster from the status table and
    terminates the calling user's running jobs.

    Examples:

    .. code-block:: bash

      # Tear down a specific cluster.
      sky down cluster_name
      \b
      # Tear down multiple clusters.
      sky down cluster1 cluster2
      \b
      # Tear down all clusters matching glob pattern 'cluster*'.
      sky down "cluster*"
      \b
      # Tear down all existing clusters.
      sky down -a

    """
    _down_or_stop_clusters(clusters,
                           apply_to_all=all,
                           down=True,
                           no_confirm=yes,
                           purge=purge)


def _hint_or_raise_for_down_spot_controller(controller_name: str):
    # spot_jobs will be empty when the spot cluster is not running.
    cluster_status, _ = backend_utils.refresh_cluster_status_handle(
        controller_name)
    if cluster_status is None:
        click.echo('Managed spot controller has already been torn down.')
        return

    if cluster_status == status_lib.ClusterStatus.INIT:
        with ux_utils.print_exception_no_traceback():
            raise exceptions.NotSupportedError(
                f'{colorama.Fore.RED}Tearing down the spot controller while '
                'it is in INIT state is not supported (this means a spot '
                'launch is in progress or the previous launch failed), as we '
                'cannot '
                'guarantee that all the spot jobs are finished. Please wait '
                'until the spot controller is UP or fix it with '
                f'{colorama.Style.BRIGHT}sky start '
                f'{spot_lib.SPOT_CONTROLLER_NAME}{colorama.Style.RESET_ALL}.')
    msg = (f'{colorama.Fore.YELLOW}WARNING: Tearing down the managed '
           f'spot controller ({cluster_status.value}). Please be '
           f'aware of the following:{colorama.Style.RESET_ALL}'
           '\n * All logs and status information of the spot '
           'jobs (output of `sky spot queue`) will be lost.')
    click.echo(msg)
    if cluster_status == status_lib.ClusterStatus.UP:
        with rich_utils.safe_status(
                '[bold cyan]Checking for in-progress spot jobs[/]'):
            try:
                spot_jobs = core.spot_queue(refresh=False)
            except exceptions.ClusterNotUpError:
                # The spot controller cluster status changed during querying
                # the spot jobs, use the latest cluster status, so that the
                # message for INIT and STOPPED states will be correctly
                # added to the message.
                cluster_status = backend_utils.refresh_cluster_status_handle(
                    controller_name)
                spot_jobs = []

        # Find in-progress spot jobs, and hint users to cancel them.
        non_terminal_jobs = [
            job for job in spot_jobs if not job['status'].is_terminal()
        ]
        if (cluster_status == status_lib.ClusterStatus.UP and
                non_terminal_jobs):
            job_table = spot_lib.format_job_table(non_terminal_jobs,
                                                  show_all=False)
            msg = (f'{colorama.Fore.RED}In-progress spot jobs found. '
                   'To avoid resource leakage, cancel all jobs first: '
                   f'{colorama.Style.BRIGHT}sky spot cancel -a'
                   f'{colorama.Style.RESET_ALL}\n')
            # Add prefix to each line to align with the bullet point.
            msg += '\n'.join(
                ['   ' + line for line in job_table.split('\n') if line != ''])
            with ux_utils.print_exception_no_traceback():
                raise exceptions.NotSupportedError(msg)
        else:
            click.echo(' * No in-progress spot jobs found. It should be safe '
                       'to terminate (see caveats above).')


def _down_or_stop_clusters(
        names: List[str],
        apply_to_all: Optional[bool],
        down: bool,  # pylint: disable=redefined-outer-name
        no_confirm: bool,
        purge: bool = False,
        idle_minutes_to_autostop: Optional[int] = None) -> None:
    """Tears down or (auto-)stops a cluster (or all clusters).

    Reserved clusters (spot controller) can only be terminated if the cluster
    name is explicitly and uniquely specified (not via glob) and purge is set
    to True.
    """
    if down:
        command = 'down'
    elif idle_minutes_to_autostop is not None:
        command = 'autostop'
    else:
        command = 'stop'
    if not names and apply_to_all is None:
        # UX: frequently users may have only 1 cluster. In this case, 'sky
        # stop/down' without args should be smart and default to that unique
        # choice.
        all_cluster_names = global_user_state.get_cluster_names_start_with('')
        if len(all_cluster_names) <= 1:
            names = all_cluster_names
        else:
            raise click.UsageError(
                f'`sky {command}` requires either a cluster name or glob '
                '(see `sky status`), or the -a/--all flag.')

    operation = 'Terminating' if down else 'Stopping'
    if idle_minutes_to_autostop is not None:
        is_cancel = idle_minutes_to_autostop < 0
        verb = 'Cancelling' if is_cancel else 'Scheduling'
        option_str = 'down' if down else 'stop'
        if is_cancel:
            option_str = '{stop,down}'
        operation = f'{verb} auto{option_str} on'

    if len(names) > 0:
        reserved_clusters = [
            name for name in names
            if name in backend_utils.SKY_RESERVED_CLUSTER_NAMES
        ]
        reserved_clusters_str = ', '.join(map(repr, reserved_clusters))
        names = [
            name for name in _get_glob_clusters(names)
            if name not in backend_utils.SKY_RESERVED_CLUSTER_NAMES
        ]
        if not down:
            local_clusters = onprem_utils.check_and_get_local_clusters()
            # Local clusters are allowed to `sky down`, but not
            # `sky start/stop`. `sky down` unregisters the local cluster
            # from sky.
            names = [
                c for c in names
                if _warn_if_local_cluster(c, local_clusters, (
                    f'Skipping local cluster {c}, as it does not support '
                    '`sky stop/autostop`.'))
            ]
        # Make sure the reserved clusters are explicitly specified without other
        # normal clusters.
        if len(reserved_clusters) > 0:
            if len(names) != 0:
                names_str = ', '.join(map(repr, names))
                raise click.UsageError(
                    f'{operation} reserved cluster(s) '
                    f'{reserved_clusters_str} with other cluster(s) '
                    f'{names_str} is currently not supported.\n'
                    f'Please omit the reserved cluster(s) {reserved_clusters}.')
            if not down:
                raise click.UsageError(
                    f'{operation} reserved cluster(s) '
                    f'{reserved_clusters_str} is currently not supported. '
                    'It will be auto-stopped after all spot jobs finish.')
            else:
                # TODO(zhwu): We can only have one reserved cluster (spot
                # controller).
                assert len(reserved_clusters) == 1, reserved_clusters
                _hint_or_raise_for_down_spot_controller(reserved_clusters[0])
                confirm_str = 'delete'
                user_input = click.prompt(
                    f'To proceed, please check the warning above and type '
                    f'{colorama.Style.BRIGHT}{confirm_str!r}'
                    f'{colorama.Style.RESET_ALL}',
                    type=str)
                if user_input != confirm_str:
                    raise click.Abort()
                no_confirm = True
        names += reserved_clusters

    if apply_to_all:
        all_clusters = global_user_state.get_clusters()
        if len(names) > 0:
            click.echo(
                f'Both --all and cluster(s) specified for `sky {command}`. '
                'Letting --all take effect.')
        # We should not remove reserved clusters when --all is specified.
        # Otherwise, it would be very easy to accidentally delete a reserved
        # cluster.
        names = [
            record['name']
            for record in all_clusters
            if record['name'] not in backend_utils.SKY_RESERVED_CLUSTER_NAMES
        ]

    clusters = []
    for name in names:
        handle = global_user_state.get_handle_from_cluster_name(name)
        if handle is None:
            # This codepath is used for 'sky down -p <controller>' when the
            # controller is not in 'sky status'.  Cluster-not-found message
            # should've been printed by _get_glob_clusters() above.
            continue
        clusters.append(name)
    usage_lib.record_cluster_name_for_current_operation(clusters)

    if not clusters:
        click.echo('Cluster(s) not found (tip: see `sky status`).')
        return

    if not no_confirm and len(clusters) > 0:
        cluster_str = 'clusters' if len(clusters) > 1 else 'cluster'
        cluster_list = ', '.join(clusters)
        click.confirm(
            f'{operation} {len(clusters)} {cluster_str}: '
            f'{cluster_list}. Proceed?',
            default=True,
            abort=True,
            show_default=True)

    plural = 's' if len(clusters) > 1 else ''
    progress = rich_progress.Progress(transient=True,
                                      redirect_stdout=False,
                                      redirect_stderr=False)
    task = progress.add_task(
        f'[bold cyan]{operation} {len(clusters)} cluster{plural}[/]',
        total=len(clusters))

    def _down_or_stop(name: str):
        success_progress = False
        if idle_minutes_to_autostop is not None:
            try:
                core.autostop(name, idle_minutes_to_autostop, down)
            except (exceptions.NotSupportedError,
                    exceptions.ClusterNotUpError) as e:
                message = str(e)
            else:  # no exception raised
                success_progress = True
                message = (f'{colorama.Fore.GREEN}{operation} '
                           f'cluster {name!r}...done{colorama.Style.RESET_ALL}')
                if idle_minutes_to_autostop >= 0:
                    option_str = 'down' if down else 'stop'
                    passive_str = 'downed' if down else 'stopped'
                    plural = 's' if idle_minutes_to_autostop != 1 else ''
                    message += (
                        f'\n  The cluster will be auto{passive_str} after '
                        f'{idle_minutes_to_autostop} minute{plural} of '
                        'idleness.'
                        f'\n  To cancel the auto{option_str}, run: '
                        f'{colorama.Style.BRIGHT}'
                        f'sky autostop {name} --cancel'
                        f'{colorama.Style.RESET_ALL}')
        else:
            try:
                if down:
                    core.down(name, purge=purge)
                else:
                    core.stop(name, purge=purge)
            except RuntimeError as e:
                message = (
                    f'{colorama.Fore.RED}{operation} cluster {name}...failed. '
                    f'{colorama.Style.RESET_ALL}'
                    f'\nReason: {common_utils.format_exception(e)}.')
            except (exceptions.NotSupportedError,
                    exceptions.ClusterOwnerIdentityMismatchError) as e:
                message = str(e)
            else:  # no exception raised
                message = (
                    f'{colorama.Fore.GREEN}{operation} cluster {name}...done.'
                    f'{colorama.Style.RESET_ALL}')
                if not down:
                    message += ('\n  To restart the cluster, run: '
                                f'{colorama.Style.BRIGHT}sky start {name}'
                                f'{colorama.Style.RESET_ALL}')
                success_progress = True

        progress.stop()
        click.echo(message)
        if success_progress:
            progress.update(task, advance=1)
        progress.start()

    with progress:
        subprocess_utils.run_in_parallel(_down_or_stop, clusters)
        progress.live.transient = False
        # Make sure the progress bar not mess up the terminal.
        progress.refresh()


@_interactive_node_cli_command
@usage_lib.entrypoint
# pylint: disable=redefined-outer-name
def gpunode(cluster: str, yes: bool, port_forward: Optional[List[int]],
            cloud: Optional[str], region: Optional[str], zone: Optional[str],
            instance_type: Optional[str], cpus: Optional[str],
            memory: Optional[str], gpus: Optional[str],
            use_spot: Optional[bool], screen: Optional[bool],
            tmux: Optional[bool], disk_size: Optional[int],
            disk_tier: Optional[str], ports: Tuple[str],
            idle_minutes_to_autostop: Optional[int], down: bool,
            retry_until_up: bool):
    """Launch or attach to an interactive GPU node.

    Examples:

    .. code-block:: bash

        # Launch a default gpunode.
        sky gpunode
        \b
        # Do work, then log out. The node is kept running. Attach back to the
        # same node and do more work.
        sky gpunode
        \b
        # Create many interactive nodes by assigning names via --cluster (-c).
        sky gpunode -c node0
        sky gpunode -c node1
        \b
        # Port forward.
        sky gpunode --port-forward 8080 --port-forward 4650 -c cluster_name
        sky gpunode -p 8080 -p 4650 -c cluster_name
        \b
        # Sync current working directory to ~/workdir on the node.
        rsync -r . cluster_name:~/workdir

    """
    # TODO: Factor out the shared logic below for [gpu|cpu|tpu]node.
    if screen and tmux:
        raise click.UsageError('Cannot use both screen and tmux.')

    session_manager = None
    if screen or tmux:
        session_manager = 'tmux' if tmux else 'screen'
    name = cluster
    if name is None:
        name = _default_interactive_node_name('gpunode')

    user_requested_resources = not (cloud is None and region is None and
                                    zone is None and instance_type is None and
                                    cpus is None and memory is None and
                                    gpus is None and use_spot is None)
    default_resources = _INTERACTIVE_NODE_DEFAULT_RESOURCES['gpunode']
    cloud_provider = clouds.CLOUD_REGISTRY.from_str(cloud)
    if gpus is None and instance_type is None:
        # Use this request if both gpus and instance_type are not specified.
        gpus = default_resources.accelerators
        instance_type = default_resources.instance_type
    if use_spot is None:
        use_spot = default_resources.use_spot
    resources = sky.Resources(cloud=cloud_provider,
                              region=region,
                              zone=zone,
                              instance_type=instance_type,
                              cpus=cpus,
                              memory=memory,
                              accelerators=gpus,
                              use_spot=use_spot,
                              disk_size=disk_size,
                              disk_tier=disk_tier,
                              ports=ports)

    _create_and_ssh_into_node(
        'gpunode',
        resources,
        cluster_name=name,
        port_forward=port_forward,
        session_manager=session_manager,
        user_requested_resources=user_requested_resources,
        no_confirm=yes,
        idle_minutes_to_autostop=idle_minutes_to_autostop,
        down=down,
        retry_until_up=retry_until_up,
    )


@_interactive_node_cli_command
@usage_lib.entrypoint
# pylint: disable=redefined-outer-name
def cpunode(cluster: str, yes: bool, port_forward: Optional[List[int]],
            cloud: Optional[str], region: Optional[str], zone: Optional[str],
            instance_type: Optional[str], cpus: Optional[str],
            memory: Optional[str], use_spot: Optional[bool],
            screen: Optional[bool], tmux: Optional[bool],
            disk_size: Optional[int], disk_tier: Optional[str],
            ports: Tuple[str], idle_minutes_to_autostop: Optional[int],
            down: bool, retry_until_up: bool):
    """Launch or attach to an interactive CPU node.

    Examples:

    .. code-block:: bash

        # Launch a default cpunode.
        sky cpunode
        \b
        # Do work, then log out. The node is kept running. Attach back to the
        # same node and do more work.
        sky cpunode
        \b
        # Create many interactive nodes by assigning names via --cluster (-c).
        sky cpunode -c node0
        sky cpunode -c node1
        \b
        # Port forward.
        sky cpunode --port-forward 8080 --port-forward 4650 -c cluster_name
        sky cpunode -p 8080 -p 4650 -c cluster_name
        \b
        # Sync current working directory to ~/workdir on the node.
        rsync -r . cluster_name:~/workdir

    """
    if screen and tmux:
        raise click.UsageError('Cannot use both screen and tmux.')

    session_manager = None
    if screen or tmux:
        session_manager = 'tmux' if tmux else 'screen'
    name = cluster
    if name is None:
        name = _default_interactive_node_name('cpunode')

    user_requested_resources = not (cloud is None and region is None and
                                    zone is None and instance_type is None and
                                    cpus is None and memory is None and
                                    use_spot is None)
    default_resources = _INTERACTIVE_NODE_DEFAULT_RESOURCES['cpunode']
    cloud_provider = clouds.CLOUD_REGISTRY.from_str(cloud)
    if instance_type is None:
        instance_type = default_resources.instance_type
    if use_spot is None:
        use_spot = default_resources.use_spot
    resources = sky.Resources(cloud=cloud_provider,
                              region=region,
                              zone=zone,
                              instance_type=instance_type,
                              cpus=cpus,
                              memory=memory,
                              use_spot=use_spot,
                              disk_size=disk_size,
                              disk_tier=disk_tier,
                              ports=ports)

    _create_and_ssh_into_node(
        'cpunode',
        resources,
        cluster_name=name,
        port_forward=port_forward,
        session_manager=session_manager,
        user_requested_resources=user_requested_resources,
        no_confirm=yes,
        idle_minutes_to_autostop=idle_minutes_to_autostop,
        down=down,
        retry_until_up=retry_until_up,
    )


@_interactive_node_cli_command
@usage_lib.entrypoint
# pylint: disable=redefined-outer-name
def tpunode(cluster: str, yes: bool, port_forward: Optional[List[int]],
            region: Optional[str], zone: Optional[str],
            instance_type: Optional[str], cpus: Optional[str],
            memory: Optional[str], tpus: Optional[str],
            use_spot: Optional[bool], tpu_vm: Optional[bool],
            screen: Optional[bool], tmux: Optional[bool],
            disk_size: Optional[int], disk_tier: Optional[str],
            ports: Tuple[str], idle_minutes_to_autostop: Optional[int],
            down: bool, retry_until_up: bool):
    """Launch or attach to an interactive TPU node.

    Examples:

    .. code-block:: bash

        # Launch a default tpunode.
        sky tpunode
        \b
        # Do work, then log out. The node is kept running. Attach back to the
        # same node and do more work.
        sky tpunode
        \b
        # Create many interactive nodes by assigning names via --cluster (-c).
        sky tpunode -c node0
        sky tpunode -c node1
        \b
        # Port forward.
        sky tpunode --port-forward 8080 --port-forward 4650 -c cluster_name
        sky tpunode -p 8080 -p 4650 -c cluster_name
        \b
        # Sync current working directory to ~/workdir on the node.
        rsync -r . cluster_name:~/workdir

    """
    if screen and tmux:
        raise click.UsageError('Cannot use both screen and tmux.')

    session_manager = None
    if screen or tmux:
        session_manager = 'tmux' if tmux else 'screen'
    name = cluster
    if name is None:
        name = _default_interactive_node_name('tpunode')

    user_requested_resources = not (region is None and zone is None and
                                    instance_type is None and cpus is None and
                                    memory is None and tpus is None and
                                    use_spot is None)
    default_resources = _INTERACTIVE_NODE_DEFAULT_RESOURCES['tpunode']
    accelerator_args = default_resources.accelerator_args
    if tpu_vm:
        accelerator_args['tpu_vm'] = True
        accelerator_args['runtime_version'] = 'tpu-vm-base'
    if instance_type is None:
        instance_type = default_resources.instance_type
    if tpus is None:
        tpus = default_resources.accelerators
    if use_spot is None:
        use_spot = default_resources.use_spot
    resources = sky.Resources(cloud=sky.GCP(),
                              region=region,
                              zone=zone,
                              instance_type=instance_type,
                              cpus=cpus,
                              memory=memory,
                              accelerators=tpus,
                              accelerator_args=accelerator_args,
                              use_spot=use_spot,
                              disk_size=disk_size,
                              disk_tier=disk_tier,
                              ports=ports)

    _create_and_ssh_into_node(
        'tpunode',
        resources,
        cluster_name=name,
        port_forward=port_forward,
        session_manager=session_manager,
        user_requested_resources=user_requested_resources,
        no_confirm=yes,
        idle_minutes_to_autostop=idle_minutes_to_autostop,
        down=down,
        retry_until_up=retry_until_up,
    )


@cli.command()
@click.option('--verbose',
              '-v',
              is_flag=True,
              default=False,
              help='Show the activated account for each cloud.')
@usage_lib.entrypoint
def check(verbose: bool):
    """Check which clouds are available to use.

    This checks access credentials for all clouds supported by SkyPilot. If a
    cloud is detected to be inaccessible, the reason and correction steps will
    be shown.

    The enabled clouds are cached and form the "search space" to be considered
    for each task.
    """
    sky_check.check(verbose=verbose)


@cli.command()
@click.argument('accelerator_str', required=False)
@click.option('--all',
              '-a',
              is_flag=True,
              default=False,
              help='Show details of all GPU/TPU/accelerator offerings.')
@click.option('--cloud',
              default=None,
              type=str,
              help='Cloud provider to query.')
@click.option(
    '--region',
    required=False,
    type=str,
    help=
    ('The region to use. If not specified, shows accelerators from all regions.'
    ),
)
@service_catalog.fallback_to_default_catalog
@usage_lib.entrypoint
def show_gpus(
        accelerator_str: Optional[str],
        all: bool,  # pylint: disable=redefined-builtin
        cloud: Optional[str],
        region: Optional[str]):
    """Show supported GPU/TPU/accelerators and their prices.

    The names and counts shown can be set in the ``accelerators`` field in task
    YAMLs, or in the ``--gpus`` flag in CLI commands. For example, if this
    table shows 8x V100s are supported, then the string ``V100:8`` will be
    accepted by the above.

    To show the detailed information of a GPU/TPU type (its price, which clouds
    offer it, the quantity in each VM type, etc.), use ``sky show-gpus <gpu>``.

    To show all accelerators, including less common ones and their detailed
    information, use ``sky show-gpus --all``.

    Definitions of certain fields:

    * ``DEVICE_MEM``: Memory of a single device; does not depend on the device
      count of the instance (VM).

    * ``HOST_MEM``: Memory of the host instance (VM).

    If ``--region`` is not specified, the price displayed for each instance
    type is the lowest across all regions for both on-demand and spot
    instances. There may be multiple regions with the same lowest price.
    """
    # validation for the --region flag
    if region is not None and cloud is None:
        raise click.UsageError(
            'The --region flag is only valid when the --cloud flag is set.')
    # This will validate 'cloud' and raise if not found.
    clouds.CLOUD_REGISTRY.from_str(cloud)
    service_catalog.validate_region_zone(region, None, clouds=cloud)
    show_all = all
    if show_all and accelerator_str is not None:
        raise click.UsageError('--all is only allowed without a GPU name.')

    def _list_to_str(lst):
        return ', '.join([str(e) for e in lst])

    def _output():
        gpu_table = log_utils.create_table(
            ['COMMON_GPU', 'AVAILABLE_QUANTITIES'])
        tpu_table = log_utils.create_table(
            ['GOOGLE_TPU', 'AVAILABLE_QUANTITIES'])
        other_table = log_utils.create_table(
            ['OTHER_GPU', 'AVAILABLE_QUANTITIES'])

        name, quantity = None, None

        if accelerator_str is None:
            result = service_catalog.list_accelerator_counts(
                gpus_only=True,
                clouds=cloud,
                region_filter=region,
            )

            if len(result) == 0 and cloud == 'kubernetes':
                yield kubernetes_utils.NO_GPU_ERROR_MESSAGE
                return

            # "Common" GPUs
            for gpu in service_catalog.get_common_gpus():
                if gpu in result:
                    gpu_table.add_row([gpu, _list_to_str(result.pop(gpu))])
            yield from gpu_table.get_string()

            # Google TPUs
            for tpu in service_catalog.get_tpus():
                if tpu in result:
                    tpu_table.add_row([tpu, _list_to_str(result.pop(tpu))])
            if len(tpu_table.get_string()) > 0:
                yield '\n\n'
            yield from tpu_table.get_string()

            # Other GPUs
            if show_all:
                yield '\n\n'
                for gpu, qty in sorted(result.items()):
                    other_table.add_row([gpu, _list_to_str(qty)])
                yield from other_table.get_string()
                yield '\n\n'
            else:
                yield ('\n\nHint: use -a/--all to see all accelerators '
                       '(including non-common ones) and pricing.')
                return
        else:
            # Parse accelerator string
            accelerator_split = accelerator_str.split(':')
            if len(accelerator_split) > 2:
                raise click.UsageError(
                    f'Invalid accelerator string {accelerator_str}. '
                    'Expected format: <accelerator_name>[:<quantity>].')
            if len(accelerator_split) == 2:
                name = accelerator_split[0]
                # Check if quantity is valid
                try:
                    quantity = int(accelerator_split[1])
                    if quantity <= 0:
                        raise ValueError(
                            'Quantity cannot be non-positive integer.')
                except ValueError as invalid_quantity:
                    raise click.UsageError(
                        f'Invalid accelerator quantity {accelerator_split[1]}. '
                        'Expected a positive integer.') from invalid_quantity
            else:
                name, quantity = accelerator_str, None

        # Case-sensitive
        result = service_catalog.list_accelerators(gpus_only=True,
                                                   name_filter=name,
                                                   quantity_filter=quantity,
                                                   region_filter=region,
                                                   clouds=cloud,
                                                   case_sensitive=False)

        if len(result) == 0:
            if cloud == 'kubernetes':
                yield kubernetes_utils.NO_GPU_ERROR_MESSAGE
                return

            quantity_str = (f' with requested quantity {quantity}'
                            if quantity else '')
            yield f'Resources \'{name}\'{quantity_str} not found. '
            yield 'Try \'sky show-gpus --all\' '
            yield 'to show available accelerators.'
            return

        if cloud is None or cloud.lower() == 'gcp':
            yield '*NOTE*: for most GCP accelerators, '
            yield 'INSTANCE_TYPE == (attachable) means '
            yield 'the host VM\'s cost is not included.\n\n'

        import pandas as pd  # pylint: disable=import-outside-toplevel
        for i, (gpu, items) in enumerate(result.items()):
            accelerator_table_headers = [
                'GPU',
                'QTY',
                'CLOUD',
                'INSTANCE_TYPE',
                'DEVICE_MEM',
                'vCPUs',
                'HOST_MEM',
                'HOURLY_PRICE',
                'HOURLY_SPOT_PRICE',
            ]
            if not show_all:
                accelerator_table_headers.append('REGION')
            accelerator_table = log_utils.create_table(
                accelerator_table_headers)
            for item in items:
                instance_type_str = item.instance_type if not pd.isna(
                    item.instance_type) else '(attachable)'
                cpu_count = item.cpu_count
                if pd.isna(cpu_count):
                    cpu_str = '-'
                elif isinstance(cpu_count, (float, int)):
                    if int(cpu_count) == cpu_count:
                        cpu_str = str(int(cpu_count))
                    else:
                        cpu_str = f'{cpu_count:.1f}'
                device_memory_str = (f'{item.device_memory:.0f}GB' if
                                     not pd.isna(item.device_memory) else '-')
                host_memory_str = f'{item.memory:.0f}GB' if not pd.isna(
                    item.memory) else '-'
                price_str = f'$ {item.price:.3f}' if not pd.isna(
                    item.price) else '-'
                spot_price_str = f'$ {item.spot_price:.3f}' if not pd.isna(
                    item.spot_price) else '-'
                region_str = item.region if not pd.isna(item.region) else '-'
                accelerator_table_vals = [
                    item.accelerator_name,
                    item.accelerator_count,
                    item.cloud,
                    instance_type_str,
                    device_memory_str,
                    cpu_str,
                    host_memory_str,
                    price_str,
                    spot_price_str,
                ]
                if not show_all:
                    accelerator_table_vals.append(region_str)
                accelerator_table.add_row(accelerator_table_vals)

            if i != 0:
                yield '\n\n'
            yield from accelerator_table.get_string()

    if show_all:
        click.echo_via_pager(_output())
    else:
        for out in _output():
            click.echo(out, nl=False)
        click.echo()


@cli.group(cls=_NaturalOrderGroup)
def storage():
    """SkyPilot Storage CLI."""
    pass


@storage.command('ls', cls=_DocumentedCodeCommand)
@click.option('--all',
              '-a',
              default=False,
              is_flag=True,
              required=False,
              help='Show all information in full.')
@usage_lib.entrypoint
# pylint: disable=redefined-builtin
def storage_ls(all: bool):
    """List storage objects managed by SkyPilot."""
    storages = sky.storage_ls()
    storage_table = storage_utils.format_storage_table(storages, show_all=all)
    click.echo(storage_table)


@storage.command('delete', cls=_DocumentedCodeCommand)
@click.argument('names',
                required=False,
                type=str,
                nargs=-1,
                **_get_shell_complete_args(_complete_storage_name))
@click.option('--all',
              '-a',
              default=False,
              is_flag=True,
              required=False,
              help='Delete all storage objects.')
@usage_lib.entrypoint
def storage_delete(names: List[str], all: bool):  # pylint: disable=redefined-builtin
    """Delete storage objects.

    Examples:

    .. code-block:: bash

        # Delete two storage objects.
        sky storage delete imagenet cifar10
        \b
        # Delete all storage objects matching glob pattern 'imagenet*'.
        sky storage delete "imagenet*"
        \b
        # Delete all storage objects.
        sky storage delete -a
    """
    if sum([len(names) > 0, all]) != 1:
        raise click.UsageError('Either --all or a name must be specified.')
    if all:
        click.echo('Deleting all storage objects.')
        storages = sky.storage_ls()
        names = [s['name'] for s in storages]
    else:
        names = _get_glob_storages(names)

    subprocess_utils.run_in_parallel(sky.storage_delete, names)


@cli.group(cls=_NaturalOrderGroup)
def admin():
    """SkyPilot On-prem administrator CLI."""
    pass


@admin.command('deploy', cls=_DocumentedCodeCommand)
@click.argument('clusterspec_yaml', required=True, type=str, nargs=-1)
@usage_lib.entrypoint
def admin_deploy(clusterspec_yaml: str):
    """Launches Sky on a local cluster.

    Performs preflight checks (environment setup, cluster resources)
    and launches Ray to serve sky tasks on the cluster. Finally
    generates a distributable YAML that can be used by multiple
    users sharing the cluster.

    This command should be run once by the cluster admin, not cluster users.

    Example:

    .. code-block:: bash

        sky admin deploy examples/local/cluster-config.yaml
    """
    steps = 1
    clusterspec_yaml = ' '.join(clusterspec_yaml)
    assert clusterspec_yaml
    is_yaml, yaml_config = _check_yaml(clusterspec_yaml)
    common_utils.validate_schema(yaml_config, schemas.get_cluster_schema(),
                                 'Invalid cluster YAML: ')
    if not is_yaml:
        raise ValueError('Must specify cluster config')
    assert yaml_config is not None, (is_yaml, yaml_config)

    auth_config = yaml_config['auth']
    ips = yaml_config['cluster']['ips']
    if not isinstance(ips, list):
        ips = [ips]
    local_cluster_name = yaml_config['cluster']['name']
    usage_lib.record_cluster_name_for_current_operation(local_cluster_name)
    usage_lib.messages.usage.update_cluster_resources(
        len(ips), sky.Resources(sky.Local()))

    # Check for Ray
    click.secho(f'[{steps}/4] Installing on-premise dependencies\n',
                fg='green',
                nl=False)
    onprem_utils.check_and_install_local_env(ips, auth_config)
    steps += 1

    # Detect what GPUs the cluster has (which can be heterogeneous)
    click.secho(f'[{steps}/4] Auto-detecting cluster resources\n',
                fg='green',
                nl=False)
    custom_resources = onprem_utils.get_local_cluster_accelerators(
        ips, auth_config)
    steps += 1

    # Launching Ray Autoscaler service
    click.secho(f'[{steps}/4] Launching sky runtime\n', fg='green', nl=False)
    onprem_utils.launch_ray_on_local_cluster(yaml_config, custom_resources)
    steps += 1

    # Generate sanitized yaml file to be sent to non-admin users
    click.secho(f'[{steps}/4] Generating sanitized local yaml file\n',
                fg='green',
                nl=False)
    sanitized_yaml_path = onprem_utils.SKY_USER_LOCAL_CONFIG_PATH.format(
        local_cluster_name)
    onprem_utils.save_distributable_yaml(yaml_config)
    click.secho(f'Saved in {sanitized_yaml_path} \n', fg='yellow', nl=False)
    click.secho(f'Successfully deployed local cluster {local_cluster_name!r}\n',
                fg='green')


@cli.group(cls=_NaturalOrderGroup)
def spot():
    """Managed Spot commands (spot instances with auto-recovery)."""
    pass


@spot.command('launch', cls=_DocumentedCodeCommand)
@click.argument('entrypoint',
                required=True,
                type=str,
                nargs=-1,
                **_get_shell_complete_args(_complete_file_name))
# TODO(zhwu): Add --dryrun option to test the launch command.
@_add_click_options(_TASK_OPTIONS + _EXTRA_RESOURCES_OPTIONS)
@click.option('--cpus',
              default=None,
              type=str,
              required=False,
              help=('Number of vCPUs each instance must have (e.g., '
                    '``--cpus=4`` (exactly 4) or ``--cpus=4+`` (at least 4)). '
                    'This is used to automatically select the instance type.'))
@click.option(
    '--memory',
    default=None,
    type=str,
    required=False,
    help=('Amount of memory each instance must have in GB (e.g., '
          '``--memory=16`` (exactly 16GB), ``--memory=16+`` (at least 16GB))'))
@click.option('--spot-recovery',
              default=None,
              type=str,
              help='Spot recovery strategy to use for the managed spot task.')
@click.option('--disk-size',
              default=None,
              type=int,
              required=False,
              help=('OS disk size in GBs.'))
@click.option(
    '--disk-tier',
    default=None,
    type=click.Choice(['low', 'medium', 'high'], case_sensitive=False),
    required=False,
    help=(
        'OS disk tier. Could be one of "low", "medium", "high". Default: medium'
    ))
@click.option(
    '--detach-run',
    '-d',
    default=False,
    is_flag=True,
    help=('If True, as soon as a job is submitted, return from this call '
          'and do not stream execution logs.'))
@click.option(
    '--retry-until-up/--no-retry-until-up',
    '-r/-no-r',
    default=None,
    is_flag=True,
    required=False,
    help=(
        '(Default: True; this flag is deprecated and will be removed in a '
        'future release.) Whether to retry provisioning infinitely until the '
        'cluster is up, if unavailability errors are encountered. This '  # pylint: disable=bad-docstring-quotes
        'applies to launching the spot clusters (both the initial and any '
        'recovery attempts), not the spot controller.'))
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@timeline.event
@usage_lib.entrypoint
def spot_launch(
    entrypoint: List[str],
    name: Optional[str],
    workdir: Optional[str],
    cloud: Optional[str],
    region: Optional[str],
    zone: Optional[str],
    gpus: Optional[str],
    cpus: Optional[str],
    memory: Optional[str],
    instance_type: Optional[str],
    num_nodes: Optional[int],
    use_spot: Optional[bool],
    image_id: Optional[str],
    spot_recovery: Optional[str],
    env_file: Optional[Dict[str, str]],
    env: List[Tuple[str, str]],
    disk_size: Optional[int],
    disk_tier: Optional[str],
    ports: Tuple[str],
    detach_run: bool,
    retry_until_up: bool,
    yes: bool,
):
    """Launch a managed spot job from a YAML or a command.

    If ENTRYPOINT points to a valid YAML file, it is read in as the task
    specification. Otherwise, it is interpreted as a bash command.

    Examples:

    .. code-block:: bash

      # You can use normal task YAMLs.
      sky spot launch task.yaml

      sky spot launch 'echo hello!'
    """
    env = _merge_env_vars(env_file, env)
    task_or_dag = _make_task_or_dag_from_entrypoint_with_overrides(
        entrypoint,
        name=name,
        workdir=workdir,
        cloud=cloud,
        region=region,
        zone=zone,
        gpus=gpus,
        cpus=cpus,
        memory=memory,
        instance_type=instance_type,
        num_nodes=num_nodes,
        use_spot=use_spot,
        image_id=image_id,
        env=env,
        disk_size=disk_size,
        disk_tier=disk_tier,
        ports=ports,
        spot_recovery=spot_recovery,
    )
    # Deprecation.
    if retry_until_up is not None:
        flag_str = '--retry-until-up'
        if not retry_until_up:
            flag_str = '--no-retry-until-up'
        click.secho(
            f'Flag {flag_str} is deprecated and will be removed in a '
            'future release (managed spot jobs will always be retried). '
            'Please file an issue if this does not work for you.',
            fg='yellow')
    else:
        retry_until_up = True

    if not isinstance(task_or_dag, sky.Dag):
        assert isinstance(task_or_dag, sky.Task), task_or_dag
        with sky.Dag() as dag:
            dag.add(task_or_dag)
            dag.name = task_or_dag.name
    else:
        dag = task_or_dag

    if name is not None:
        dag.name = name

    dag_utils.maybe_infer_and_fill_dag_and_task_names(dag)
    dag_utils.fill_default_spot_config_in_dag_for_spot_launch(dag)

    click.secho(
        f'Managed spot job {dag.name!r} will be launched on (estimated):',
        fg='yellow')
    dag = sky.optimize(dag)

    if not yes:
        prompt = f'Launching the spot job {dag.name!r}. Proceed?'
        if prompt is not None:
            click.confirm(prompt, default=True, abort=True, show_default=True)

    for task in dag.tasks:
        # We try our best to validate the cluster name before we launch the
        # task. If the cloud is not specified, this will only validate the
        # cluster name against the regex, and the cloud-specific validation will
        # be done by the spot controller when actually launching the spot
        # cluster.
        resources = list(task.resources)[0]
        task_cloud = (resources.cloud
                      if resources.cloud is not None else clouds.Cloud)
        task_cloud.check_cluster_name_is_valid(name)

    sky.spot_launch(dag,
                    name,
                    detach_run=detach_run,
                    retry_until_up=retry_until_up)


@spot.command('queue', cls=_DocumentedCodeCommand)
@click.option('--all',
              '-a',
              default=False,
              is_flag=True,
              required=False,
              help='Show all information in full.')
@click.option(
    '--refresh',
    '-r',
    default=False,
    is_flag=True,
    required=False,
    help='Query the latest statuses, restarting the spot controller if stopped.'
)
@click.option('--skip-finished',
              '-s',
              default=False,
              is_flag=True,
              required=False,
              help='Show only pending/running jobs\' information.')
@usage_lib.entrypoint
# pylint: disable=redefined-builtin
def spot_queue(all: bool, refresh: bool, skip_finished: bool):
    """Show statuses of managed spot jobs.

    Each spot job can have one of the following statuses:

    - ``PENDING``: Job is waiting for a free slot on the spot controller to be
      accepted.

    - ``SUBMITTED``: Job is submitted to and accepted by the spot controller.

    - ``STARTING``: Job is starting (provisioning a spot cluster).

    - ``RUNNING``: Job is running.

    - ``RECOVERING``: The spot cluster is recovering from a preemption.

    - ``SUCCEEDED``: Job succeeded.

    - ``CANCELLING``: Job was requested to be cancelled by the user, and the
      cancellation is in progress.

    - ``CANCELLED``: Job was cancelled by the user.

    - ``FAILED``: Job failed due to an error from the job itself.

    - ``FAILED_SETUP``: Job failed due to an error from the job's ``setup``
      commands.

    - ``FAILED_PRECHECKS``: Job failed due to an error from our prechecks such
      as invalid cluster names or an infeasible resource is specified.

    - ``FAILED_NO_RESOURCE``: Job failed due to resources being unavailable
      after a maximum number of retries.

    - ``FAILED_CONTROLLER``: Job failed due to an unexpected error in the spot
      controller.

    If the job failed, either due to user code or spot unavailability, the
    error log can be found with ``sky spot logs --controller``, e.g.:

    .. code-block:: bash

      sky spot logs --controller job_id

    This also shows the logs for provisioning and any preemption and recovery
    attempts.

    (Tip) To fetch job statuses every 60 seconds, use ``watch``:

    .. code-block:: bash

      watch -n60 sky spot queue

    """
    click.secho('Fetching managed spot job statuses...', fg='yellow')
    with rich_utils.safe_status('[cyan]Checking spot jobs[/]'):
        _, msg = _get_spot_jobs(refresh=refresh,
                                skip_finished=skip_finished,
                                show_all=all,
                                is_called_by_user=True)
    if not skip_finished:
        in_progress_only_hint = ''
    else:
        in_progress_only_hint = ' (showing in-progress jobs only)'
    click.echo(f'{colorama.Fore.CYAN}{colorama.Style.BRIGHT}'
               f'Managed spot jobs{colorama.Style.RESET_ALL}'
               f'{in_progress_only_hint}\n{msg}')


_add_command_alias_to_group(spot, spot_queue, 'status', hidden=True)


@spot.command('cancel', cls=_DocumentedCodeCommand)
@click.option('--name',
              '-n',
              required=False,
              type=str,
              help='Managed spot job name to cancel.')
@click.argument('job_ids', default=None, type=int, required=False, nargs=-1)
@click.option('--all',
              '-a',
              is_flag=True,
              default=False,
              required=False,
              help='Cancel all managed spot jobs.')
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@usage_lib.entrypoint
# pylint: disable=redefined-builtin
def spot_cancel(name: Optional[str], job_ids: Tuple[int], all: bool, yes: bool):
    """Cancel managed spot jobs.

    You can provide either a job name or a list of job IDs to be cancelled.
    They are exclusive options.

    Examples:

    .. code-block:: bash

      # Cancel managed spot job with name 'my-job'
      $ sky spot cancel -n my-job
      \b
      # Cancel managed spot jobs with IDs 1, 2, 3
      $ sky spot cancel 1 2 3
    """
    _, handle = spot_lib.is_spot_controller_up(
        'All managed spot jobs should have finished.')
    if handle is None:
        # Hint messages already printed by the call above.
        sys.exit(1)

    job_id_str = ','.join(map(str, job_ids))
    if sum([len(job_ids) > 0, name is not None, all]) != 1:
        argument_str = f'--job-ids {job_id_str}' if len(job_ids) > 0 else ''
        argument_str += f' --name {name}' if name is not None else ''
        argument_str += ' --all' if all else ''
        raise click.UsageError(
            'Can only specify one of JOB_IDS or --name or --all. '
            f'Provided {argument_str!r}.')

    if not yes:
        job_identity_str = (f'managed spot jobs with IDs {job_id_str}'
                            if job_ids else repr(name))
        if all:
            job_identity_str = 'all managed spot jobs'
        click.confirm(f'Cancelling {job_identity_str}. Proceed?',
                      default=True,
                      abort=True,
                      show_default=True)

    core.spot_cancel(job_ids=job_ids, name=name, all=all)


@spot.command('logs', cls=_DocumentedCodeCommand)
@click.option('--name',
              '-n',
              required=False,
              type=str,
              help='Managed spot job name.')
@click.option(
    '--follow/--no-follow',
    is_flag=True,
    default=True,
    help=('Follow the logs of the job. [default: --follow] '
          'If --no-follow is specified, print the log so far and exit.'))
@click.option(
    '--controller',
    is_flag=True,
    default=False,
    help=('Show the controller logs of this job; useful for debugging '
          'launching/recoveries, etc.'))
@click.argument('job_id', required=False, type=int)
@usage_lib.entrypoint
def spot_logs(name: Optional[str], job_id: Optional[int], follow: bool,
              controller: bool):
    """Tail the log of a managed spot job."""
    try:
        if controller:
            core.tail_logs(spot_lib.SPOT_CONTROLLER_NAME,
                           job_id=job_id,
                           follow=follow)
        else:
            core.spot_tail_logs(name=name, job_id=job_id, follow=follow)
    except exceptions.ClusterNotUpError:
        # Hint messages already printed by the call above.
        sys.exit(1)


@spot.command('dashboard', cls=_DocumentedCodeCommand)
@click.option(
    '--port',
    '-p',
    default=None,
    type=int,
    required=False,
    help=('Local port to use for the dashboard. If None, a free port is '
          'automatically chosen.'))
@usage_lib.entrypoint
def spot_dashboard(port: Optional[int]):
    """Opens a dashboard for spot jobs (needs controller to be UP)."""
    # TODO(zongheng): ideally, the controller/dashboard server should expose the
    # API perhaps via REST. Then here we would (1) not have to use SSH to try to
    # see if the controller is UP first, which is slow; (2) not have to run SSH
    # port forwarding first (we'd just launch a local dashboard which would make
    # REST API calls to the controller dashboard server).
    click.secho('Checking if spot controller is up...', fg='yellow')
    hint = (
        'Dashboard is not available if spot controller is not up. Run a spot '
        'job first.')
    _, handle = spot_lib.is_spot_controller_up(stopped_message=hint,
                                               non_existent_message=hint)
    if handle is None:
        sys.exit(1)
    # SSH forward a free local port to remote's dashboard port.
    remote_port = constants.SPOT_DASHBOARD_REMOTE_PORT
    if port is None:
        free_port = common_utils.find_free_port(remote_port)
    else:
        free_port = port
    ssh_command = (f'ssh -qNL {free_port}:localhost:{remote_port} '
                   f'{spot_lib.SPOT_CONTROLLER_NAME}')
    click.echo('Forwarding port: ', nl=False)
    click.secho(f'{ssh_command}', dim=True)

    with subprocess.Popen(ssh_command, shell=True,
                          start_new_session=True) as ssh_process:
        time.sleep(3)  # Added delay for ssh_command to initialize.
        webbrowser.open(f'http://localhost:{free_port}')
        click.secho(
            f'Dashboard is now available at: http://127.0.0.1:{free_port}',
            fg='green')
        try:
            ssh_process.wait()
        except KeyboardInterrupt:
            # When user presses Ctrl-C in terminal, exits the previous ssh
            # command so that <free local port> is freed up.
            try:
                os.killpg(os.getpgid(ssh_process.pid), signal.SIGTERM)
            except ProcessLookupError:
                # This happens if spot controller is auto-stopped.
                pass
        finally:
            click.echo('Exiting.')


# ==============================
# Sky Benchmark CLIs
# ==============================


@ux_utils.print_exception_no_traceback()
def _get_candidate_configs(yaml_path: str) -> Optional[List[Dict[str, str]]]:
    """Gets benchmark candidate configs from a YAML file.

    Benchmark candidates are configured in the YAML file as a list of
    dictionaries. Each dictionary defines a candidate config
    by overriding resources. For example:

    resources:
        cloud: aws
        candidates:
        - {accelerators: K80}
        - {instance_type: g4dn.2xlarge}
        - {cloud: gcp, accelerators: V100} # overrides cloud
    """
    config = common_utils.read_yaml(os.path.expanduser(yaml_path))
    if not isinstance(config, dict):
        raise ValueError(f'Invalid YAML file: {yaml_path}. '
                         'The YAML file should be parsed into a dictionary.')
    if config.get('resources') is None:
        return None

    resources = config['resources']
    if not isinstance(resources, dict):
        raise ValueError(f'Invalid resources configuration in {yaml_path}. '
                         'Resources must be a dictionary.')
    if resources.get('candidates') is None:
        return None

    candidates = resources['candidates']
    if not isinstance(candidates, list):
        raise ValueError('Resource candidates must be a list of dictionaries.')
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError('Each resource candidate must be a dictionary.')
    return candidates


@cli.group(cls=_NaturalOrderGroup)
def bench():
    """SkyPilot Benchmark CLI."""
    pass


@bench.command('launch', cls=_DocumentedCodeCommand)
@click.argument('entrypoint',
                required=True,
                type=str,
                nargs=-1,
                **_get_shell_complete_args(_complete_file_name))
@click.option('--benchmark',
              '-b',
              required=True,
              type=str,
              help='Benchmark name.')
@_add_click_options(_TASK_OPTIONS)
@click.option('--gpus',
              required=False,
              type=str,
              help=('Comma-separated list of GPUs to run benchmark on. '
                    'Example values: "T4:4,V100:8" (without blank spaces).'))
@click.option(
    '--ports',
    required=False,
    type=str,
    multiple=True,
    help=('Ports to open on the cluster. '
          'If specified, overrides the "ports" config in the YAML. '),
)
@click.option('--disk-size',
              default=None,
              type=int,
              required=False,
              help=('OS disk size in GBs.'))
@click.option(
    '--disk-tier',
    default=None,
    type=click.Choice(['low', 'medium', 'high'], case_sensitive=False),
    required=False,
    help=(
        'OS disk tier. Could be one of "low", "medium", "high". Default: medium'
    ))
@click.option(
    '--idle-minutes-to-autostop',
    '-i',
    default=None,
    type=int,
    required=False,
    help=('Automatically stop the cluster after this many minutes '
          'of idleness after setup/file_mounts. This is equivalent to '
          'running `sky launch -d ...` and then `sky autostop -i <minutes>`. '
          'If not set, the cluster will not be autostopped.'))
# Disabling quote check here, as there seems to be a bug in pylint,
# which incorrectly recognizes the help string as a docstring.
# pylint: disable=bad-docstring-quotes
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@usage_lib.entrypoint
def benchmark_launch(
    entrypoint: str,
    benchmark: str,
    name: Optional[str],
    workdir: Optional[str],
    cloud: Optional[str],
    region: Optional[str],
    zone: Optional[str],
    gpus: Optional[str],
    num_nodes: Optional[int],
    use_spot: Optional[bool],
    image_id: Optional[str],
    env_file: Optional[Dict[str, str]],
    env: List[Tuple[str, str]],
    disk_size: Optional[int],
    disk_tier: Optional[str],
    ports: Tuple[str],
    idle_minutes_to_autostop: Optional[int],
    yes: bool,
) -> None:
    """Benchmark a task on different resources.

    Example usage: `sky bench launch mytask.yaml -b mytask --gpus V100,T4`
    will benchmark your task on a V100 cluster and a T4 cluster simultaneously.
    Alternatively, specify the benchmarking resources in your YAML (see doc),
    which allows benchmarking on many more resource fields.
    """
    env = _merge_env_vars(env_file, env)
    record = benchmark_state.get_benchmark_from_name(benchmark)
    if record is not None:
        raise click.BadParameter(f'Benchmark {benchmark} already exists. '
                                 'To delete the previous benchmark result, '
                                 f'run `sky bench delete {benchmark}`.')

    entrypoint = ' '.join(entrypoint)
    if not entrypoint:
        raise click.BadParameter('Please specify a task yaml to benchmark.')

    is_yaml, config = _check_yaml(entrypoint)
    if not is_yaml:
        raise click.BadParameter(
            'Sky Benchmark does not support command line tasks. '
            'Please provide a YAML file.')
    assert config is not None, (is_yaml, config)

    click.secho('Benchmarking a task from YAML spec: ', fg='yellow', nl=False)
    click.secho(entrypoint, bold=True)

    candidates = _get_candidate_configs(entrypoint)
    # Check if the candidate configs are specified in both CLI and YAML.
    if candidates is not None:
        message = ('is specified in both CLI and resources.candidates '
                   'in the YAML. Please specify only one of them.')
        if cloud is not None:
            if any('cloud' in candidate for candidate in candidates):
                raise click.BadParameter(f'cloud {message}')
        if region is not None:
            if any('region' in candidate for candidate in candidates):
                raise click.BadParameter(f'region {message}')
        if zone is not None:
            if any('zone' in candidate for candidate in candidates):
                raise click.BadParameter(f'zone {message}')
        if gpus is not None:
            if any('accelerators' in candidate for candidate in candidates):
                raise click.BadParameter(f'gpus (accelerators) {message}')
        if use_spot is not None:
            if any('use_spot' in candidate for candidate in candidates):
                raise click.BadParameter(f'use_spot {message}')
        if image_id is not None:
            if any('image_id' in candidate for candidate in candidates):
                raise click.BadParameter(f'image_id {message}')
        if disk_size is not None:
            if any('disk_size' in candidate for candidate in candidates):
                raise click.BadParameter(f'disk_size {message}')
        if disk_tier is not None:
            if any('disk_tier' in candidate for candidate in candidates):
                raise click.BadParameter(f'disk_tier {message}')
        if ports:
            if any('ports' in candidate for candidate in candidates):
                raise click.BadParameter(f'ports {message}')

    # The user can specify the benchmark candidates in either of the two ways:
    # 1. By specifying resources.candidates in the YAML.
    # 2. By specifying gpu types as a command line argument (--gpus).
    override_gpu = None
    if gpus is not None:
        gpu_list = gpus.split(',')
        gpu_list = [gpu.strip() for gpu in gpu_list]
        if '' in gpus:
            raise click.BadParameter('Remove blanks in --gpus.')

        if len(gpu_list) == 1:
            override_gpu = gpu_list[0]
        else:
            # If len(gpus) > 1, gpus is intrepreted
            # as a list of benchmark candidates.
            if candidates is None:
                candidates = [{'accelerators': gpu} for gpu in gpu_list]
                override_gpu = None
            else:
                raise ValueError('Provide benchmark candidates in either '
                                 '--gpus or resources.candidates in the YAML.')
    if candidates is None:
        candidates = [{}]

    if 'resources' not in config:
        config['resources'] = {}
    resources_config = config['resources']

    # Override the yaml config with the command line arguments.
    if name is not None:
        config['name'] = name
    if workdir is not None:
        config['workdir'] = workdir
    if num_nodes is not None:
        config['num_nodes'] = num_nodes
    override_params = _parse_override_params(cloud=cloud,
                                             region=region,
                                             zone=zone,
                                             gpus=override_gpu,
                                             use_spot=use_spot,
                                             image_id=image_id,
                                             disk_size=disk_size,
                                             disk_tier=disk_tier,
                                             ports=ports)
    resources_config.update(override_params)
    if 'cloud' in resources_config:
        cloud = resources_config.pop('cloud')
        if cloud is not None:
            resources_config['cloud'] = str(cloud)
    if 'region' in resources_config:
        if resources_config['region'] is None:
            resources_config.pop('region')
    if 'zone' in resources_config:
        if resources_config['zone'] is None:
            resources_config.pop('zone')
    if 'accelerators' in resources_config:
        if resources_config['accelerators'] is None:
            resources_config.pop('accelerators')
    if 'image_id' in resources_config:
        if resources_config['image_id'] is None:
            resources_config.pop('image_id')

    # Fully generate the benchmark candidate configs.
    clusters, candidate_configs = benchmark_utils.generate_benchmark_configs(
        benchmark, config, candidates)
    # Show the benchmarking VM instances selected by the optimizer.
    # This also detects the case where the user requested infeasible resources.
    benchmark_utils.print_benchmark_clusters(benchmark, clusters, config,
                                             candidate_configs)
    if not yes:
        plural = 's' if len(candidates) > 1 else ''
        prompt = f'Launching {len(candidates)} cluster{plural}. Proceed?'
        click.confirm(prompt, default=True, abort=True, show_default=True)

    # Configs that are only accepted by the CLI.
    commandline_args: Dict[str, Any] = {}
    # Set the default idle minutes to autostop as 5, mimicking
    # the serverless execution.
    if idle_minutes_to_autostop is None:
        idle_minutes_to_autostop = 5
    commandline_args['idle-minutes-to-autostop'] = idle_minutes_to_autostop
    if len(env) > 0:
        commandline_args['env'] = [f'{k}={v}' for k, v in env]

    # Launch the benchmarking clusters in detach mode in parallel.
    benchmark_created = benchmark_utils.launch_benchmark_clusters(
        benchmark, clusters, candidate_configs, commandline_args)

    # If at least one cluster is created, print the following messages.
    if benchmark_created:
        logger.info(
            f'\n{colorama.Fore.CYAN}Benchmark name: '
            f'{colorama.Style.BRIGHT}{benchmark}{colorama.Style.RESET_ALL}'
            '\nTo see the benchmark results: '
            f'{backend_utils.BOLD}sky bench show '
            f'{benchmark}{backend_utils.RESET_BOLD}'
            '\nTo teardown the clusters: '
            f'{backend_utils.BOLD}sky bench down '
            f'{benchmark}{backend_utils.RESET_BOLD}')
        subprocess_utils.run('sky bench ls')
    else:
        logger.error('No benchmarking clusters are created.')
        subprocess_utils.run('sky status')


@bench.command('ls', cls=_DocumentedCodeCommand)
@usage_lib.entrypoint
def benchmark_ls() -> None:
    """List the benchmark history."""
    benchmarks = benchmark_state.get_benchmarks()
    columns = [
        'BENCHMARK',
        'TASK',
        'LAUNCHED',
    ]

    max_num_candidates = 1
    for benchmark in benchmarks:
        benchmark_results = benchmark_state.get_benchmark_results(
            benchmark['name'])
        num_candidates = len(benchmark_results)
        if num_candidates > max_num_candidates:
            max_num_candidates = num_candidates

    if max_num_candidates == 1:
        columns += ['CANDIDATE']
    else:
        columns += [f'CANDIDATE {i}' for i in range(1, max_num_candidates + 1)]
    benchmark_table = log_utils.create_table(columns)

    for benchmark in benchmarks:
        if benchmark['task'] is not None:
            task = benchmark['task']
        else:
            task = '-'
        row = [
            # BENCHMARK
            benchmark['name'],
            # TASK
            task,
            # LAUNCHED
            datetime.datetime.fromtimestamp(benchmark['launched_at']),
        ]

        benchmark_results = benchmark_state.get_benchmark_results(
            benchmark['name'])
        # RESOURCES
        for b in benchmark_results:
            num_nodes = b['num_nodes']
            resources = b['resources']
            postfix_spot = '[Spot]' if resources.use_spot else ''
            instance_type = resources.instance_type + postfix_spot
            if resources.accelerators is None:
                accelerators = ''
            else:
                accelerator, count = list(resources.accelerators.items())[0]
                accelerators = f' ({accelerator}:{count})'
            # For brevity, skip the cloud names.
            resources_str = f'{num_nodes}x {instance_type}{accelerators}'
            row.append(resources_str)
        row += [''] * (max_num_candidates - len(benchmark_results))
        benchmark_table.add_row(row)
    if benchmarks:
        click.echo(benchmark_table)
    else:
        click.echo('No benchmark history found.')


@bench.command('show', cls=_DocumentedCodeCommand)
@click.argument('benchmark', required=True, type=str)
# TODO(woosuk): Add --all option to show all the collected information
# (e.g., setup time, warmup steps, total steps, etc.).
@usage_lib.entrypoint
def benchmark_show(benchmark: str) -> None:
    """Show a benchmark report."""
    record = benchmark_state.get_benchmark_from_name(benchmark)
    if record is None:
        raise click.BadParameter(f'Benchmark {benchmark} does not exist.')
    benchmark_utils.update_benchmark_state(benchmark)

    click.echo(
        textwrap.dedent("""\
        Legend:
        - #STEPS: Number of steps taken.
        - SEC/STEP, $/STEP: Average time (cost) per step.
        - EST(hr), EST($): Estimated total time (cost) to complete the benchmark.
    """))
    columns = [
        'CLUSTER',
        'RESOURCES',
        'STATUS',
        'DURATION',
        'SPENT($)',
        '#STEPS',
        'SEC/STEP',
        '$/STEP',
        'EST(hr)',
        'EST($)',
    ]

    cluster_table = log_utils.create_table(columns)
    rows = []
    benchmark_results = benchmark_state.get_benchmark_results(benchmark)
    for result in benchmark_results:
        num_nodes = result['num_nodes']
        resources = result['resources']
        row = [
            # CLUSTER
            result['cluster'],
            # RESOURCES
            f'{num_nodes}x {resources}',
            # STATUS
            result['status'].value,
        ]

        record = result['record']
        if (record is None or record.start_time is None or
                record.last_time is None):
            row += ['-'] * (len(columns) - len(row))
            rows.append(row)
            continue

        duration_str = log_utils.readable_time_duration(record.start_time,
                                                        record.last_time,
                                                        absolute=True)
        duration = record.last_time - record.start_time
        spent = num_nodes * resources.get_cost(duration)
        spent_str = f'{spent:.4f}'

        num_steps = record.num_steps_so_far
        if num_steps is None:
            num_steps = '-'

        seconds_per_step = record.seconds_per_step
        if seconds_per_step is None:
            seconds_per_step_str = '-'
            cost_per_step_str = '-'
        else:
            seconds_per_step_str = f'{seconds_per_step:.4f}'
            cost_per_step = num_nodes * resources.get_cost(seconds_per_step)
            cost_per_step_str = f'{cost_per_step:.6f}'

        total_time = record.estimated_total_seconds
        if total_time is None:
            total_time_str = '-'
            total_cost_str = '-'
        else:
            total_time_str = f'{total_time / 3600:.2f}'
            total_cost = num_nodes * resources.get_cost(total_time)
            total_cost_str = f'{total_cost:.2f}'

        row += [
            # DURATION
            duration_str,
            # SPENT($)
            spent_str,
            # STEPS
            num_steps,
            # SEC/STEP
            seconds_per_step_str,
            # $/STEP
            cost_per_step_str,
            # EST(hr)
            total_time_str,
            # EST($)
            total_cost_str,
        ]
        rows.append(row)

    cluster_table.add_rows(rows)
    click.echo(cluster_table)

    finished = [
        row for row in rows
        if row[2] == benchmark_state.BenchmarkStatus.FINISHED.value
    ]
    if any(row[5] == '-' for row in finished):
        # No #STEPS. SkyCallback was unused.
        click.secho(
            'SkyCallback logs are not found in this benchmark. '
            'Consider using SkyCallback to get more detailed information '
            'in real time.',
            fg='yellow')
    elif any(row[6] != '-' and row[-1] == '-' for row in rows):
        # No EST($). total_steps is not specified and cannot be inferred.
        click.secho(
            'Cannot estimate total time and cost because '
            'the total number of steps cannot be inferred by SkyCallback. '
            'To get the estimation, specify the total number of steps in '
            'either `sky_callback.init` or `Sky*Callback`.',
            fg='yellow')


@bench.command('down', cls=_DocumentedCodeCommand)
@click.argument('benchmark', required=True, type=str)
@click.option(
    '--exclude',
    '-e',
    'clusters_to_exclude',
    required=False,
    type=str,
    multiple=True,
    help=('Cluster name(s) to exclude from termination. '
          'Typically, you might want to see the benchmark results in '
          '`sky bench show` and exclude a "winner" cluster from termination '
          'to finish the running task.'))
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@usage_lib.entrypoint
def benchmark_down(
    benchmark: str,
    clusters_to_exclude: List[str],
    yes: bool,
) -> None:
    """Tear down all clusters belonging to a benchmark."""
    record = benchmark_state.get_benchmark_from_name(benchmark)
    if record is None:
        raise click.BadParameter(f'Benchmark {benchmark} does not exist.')

    clusters = benchmark_state.get_benchmark_clusters(benchmark)
    to_stop: List[str] = []
    for cluster in clusters:
        if cluster in clusters_to_exclude:
            continue
        if global_user_state.get_cluster_from_name(cluster) is None:
            continue
        to_stop.append(cluster)

    _down_or_stop_clusters(to_stop,
                           apply_to_all=False,
                           down=True,
                           no_confirm=yes)


@bench.command('delete', cls=_DocumentedCodeCommand)
@click.argument('benchmarks', required=False, type=str, nargs=-1)
@click.option('--all',
              '-a',
              default=None,
              is_flag=True,
              help='Delete all benchmark reports from the history.')
@click.option('--yes',
              '-y',
              is_flag=True,
              default=False,
              required=False,
              help='Skip confirmation prompt.')
@usage_lib.entrypoint
# pylint: disable=redefined-builtin
def benchmark_delete(benchmarks: Tuple[str], all: Optional[bool],
                     yes: bool) -> None:
    """Delete benchmark reports from the history."""
    if not benchmarks and all is None:
        raise click.BadParameter(
            'Either specify benchmarks or use --all to delete all benchmarks.')
    to_delete = []
    if len(benchmarks) > 0:
        for benchmark in benchmarks:
            record = benchmark_state.get_benchmark_from_name(benchmark)
            if record is None:
                print(f'Benchmark {benchmark} not found.')
            else:
                to_delete.append(record)
    if all:
        to_delete = benchmark_state.get_benchmarks()
        if len(benchmarks) > 0:
            print('Both --all and benchmark(s) specified '
                  'for sky bench delete. Letting --all take effect.')

    to_delete = [r['name'] for r in to_delete]
    if not to_delete:
        return

    benchmark_list = ', '.join(to_delete)
    plural = 's' if len(to_delete) > 1 else ''
    if not yes:
        click.confirm(
            f'Deleting the benchmark{plural}: {benchmark_list}. Proceed?',
            default=True,
            abort=True,
            show_default=True)

    progress = rich_progress.Progress(transient=True,
                                      redirect_stdout=False,
                                      redirect_stderr=False)
    task = progress.add_task(
        f'[bold cyan]Deleting {len(to_delete)} benchmark{plural}: ',
        total=len(to_delete))

    def _delete_benchmark(benchmark: str) -> None:
        clusters = benchmark_state.get_benchmark_clusters(benchmark)
        records = []
        for cluster in clusters:
            record = global_user_state.get_cluster_from_name(cluster)
            records.append(record)
        num_clusters = len([r for r in records if r is not None])

        if num_clusters > 0:
            plural = 's' if num_clusters > 1 else ''
            message = (f'{colorama.Fore.YELLOW}Benchmark {benchmark} '
                       f'has {num_clusters} un-terminated cluster{plural}. '
                       f'Terminate the cluster{plural} with '
                       f'{backend_utils.BOLD} sky bench down {benchmark} '
                       f'{backend_utils.RESET_BOLD} '
                       'before deleting the benchmark report.')
            success = False
        else:
            bucket_name = benchmark_state.get_benchmark_from_name(
                benchmark)['bucket']
            handle = global_user_state.get_handle_from_storage_name(bucket_name)
            assert handle is not None, bucket_name
            bucket_type = list(handle.sky_stores.keys())[0]
            benchmark_utils.remove_benchmark_logs(benchmark, bucket_name,
                                                  bucket_type)
            benchmark_state.delete_benchmark(benchmark)
            message = (f'{colorama.Fore.GREEN}Benchmark report for '
                       f'{benchmark} deleted.{colorama.Style.RESET_ALL}')
            success = True

        progress.stop()
        click.secho(message)
        if success:
            progress.update(task, advance=1)
        progress.start()

    with progress:
        subprocess_utils.run_in_parallel(_delete_benchmark, to_delete)
        progress.live.transient = False
        progress.refresh()


@cli.group(cls=_NaturalOrderGroup, hidden=True)
def local():
    """SkyPilot local tools CLI."""
    pass


@local.command('up', cls=_DocumentedCodeCommand)
@usage_lib.entrypoint
def local_up():
    """Creates a local cluster."""
    cluster_created = False
    # Check if ~/.kube/config exists:
    if os.path.exists(os.path.expanduser('~/.kube/config')):
        curr_context = kubernetes_utils.get_current_kube_config_context_name()
        skypilot_context = 'kind-skypilot'
        if curr_context is not None and curr_context != skypilot_context:
            click.echo(
                f'Current context in kube config: {curr_context}'
                '\nWill automatically switch to kind-skypilot after the local '
                'cluster is created.')
    with rich_utils.safe_status('Creating local cluster...'):
        path_to_package = os.path.dirname(os.path.dirname(__file__))
        up_script_path = os.path.join(path_to_package, 'sky/utils/kubernetes',
                                      'create_cluster.sh')
        # Get directory of script and run it from there
        cwd = os.path.dirname(os.path.abspath(up_script_path))
        # Run script and don't print output
        try:
            subprocess_utils.run(up_script_path, cwd=cwd, capture_output=True)
            cluster_created = True
        except subprocess.CalledProcessError as e:
            # Check if return code is 100
            if e.returncode == 100:
                click.echo('\nLocal cluster already exists. '
                           'Run `sky local down` to delete it.')
            else:
                stderr = e.stderr.decode('utf-8')
                click.echo(f'\nFailed to create local cluster. {stderr}')
                if env_options.Options.SHOW_DEBUG_INFO.get():
                    stdout = e.stdout.decode('utf-8')
                    click.echo(f'Logs:\n{stdout}')
                sys.exit(1)
    # Run sky check
    with rich_utils.safe_status('Running sky check...'):
        sky_check.check(quiet=True)
    if cluster_created:
        # Get number of CPUs
        p = subprocess_utils.run(
            'kubectl get nodes -o jsonpath=\'{.items[0].status.capacity.cpu}\'',
            capture_output=True)
        num_cpus = int(p.stdout.decode('utf-8'))
        if num_cpus < 2:
            click.echo('Warning: Local cluster has less than 2 CPUs. '
                       'This may cause issues with running tasks.')
        click.echo(
            'Local Kubernetes cluster created successfully with '
            f'{num_cpus} CPUs. `sky launch` can now run tasks locally.'
            '\nHint: To change the number of CPUs, change your docker '
            'runtime settings. See https://kind.sigs.k8s.io/docs/user/quick-start/#settings-for-docker-desktop for more info.'  # pylint: disable=line-too-long
        )


@local.command('down', cls=_DocumentedCodeCommand)
@usage_lib.entrypoint
def local_down():
    """Deletes a local cluster."""
    cluster_removed = False
    with rich_utils.safe_status('Removing local cluster...'):
        path_to_package = os.path.dirname(os.path.dirname(__file__))
        down_script_path = os.path.join(path_to_package, 'sky/utils/kubernetes',
                                        'delete_cluster.sh')
        try:
            subprocess_utils.run(down_script_path, capture_output=True)
            cluster_removed = True
        except subprocess.CalledProcessError as e:
            # Check if return code is 100
            if e.returncode == 100:
                click.echo('\nLocal cluster does not exist.')
            else:
                stderr = e.stderr.decode('utf-8')
                click.echo(f'\nFailed to delete local cluster. {stderr}')
                if env_options.Options.SHOW_DEBUG_INFO.get():
                    stdout = e.stdout.decode('utf-8')
                    click.echo(f'Logs:\n{stdout}')
    if cluster_removed:
        # Run sky check
        with rich_utils.safe_status('Running sky check...'):
            sky_check.check(quiet=True)
        click.echo('Local cluster removed.')


def main():
    return cli()


if __name__ == '__main__':
    main()
