"""
Dispatch management for different machines (clusters, local, ...)
"""

import copy
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from ruamel import yaml
from deeperwin.configuration import Configuration, to_prettified_yaml
from deeperwin.run_tools.available_gpus import assign_free_GPU_ids


def idx_to_job_name(idx):
  return f"{idx:04d}"


def dump_config_dict(directory, config_dict, config_name='config.yml'):
  with open(Path(directory).joinpath(config_name), 'w') as f:
    yaml.YAML().dump(to_prettified_yaml(config_dict), f)


def setup_experiment_dir(directory, force=False):
  if os.path.isdir(directory):
    if force:
      shutil.rmtree(directory)
    else:
      raise FileExistsError(
        f"Could not create experiment {directory}: Directory already exists."
      )
  os.makedirs(directory)
  return directory


def build_experiment_name(parameters, basename=""):
  s = [basename] if basename else []
  for name, value in parameters.items():
    if name == 'experiment_name' or name == 'reuse.path':
      continue
    else:
      s.append(value)
  return "_".join(s)


def get_fname_fullpath(fname):
  return Path(__file__).resolve().parent.joinpath(fname)


def dispatch_to_local(command, run_dir, config: Configuration, sleep_in_sec):
  env = os.environ.copy()
  env["CUDA_VISIBLE_DEVICES"] = assign_free_GPU_ids(sleep_seconds=sleep_in_sec)
  subprocess.run(command, cwd=run_dir, env=env)


def dispatch_to_local_background(
  command, run_dir, config: Configuration, sleep_in_sec
):
  n_gpus = config.computation.n_local_devices or 1
  with open(os.path.join(run_dir, 'GPU.out'), 'w') as f:
    command = f"export CUDA_VISIBLE_DEVICES=$(deeperwin select-gpus --n-gpus {n_gpus} --sleep {sleep_in_sec}) && " + " ".join(
      command
    )
    print(f"Dispatching to local_background: {command}")
    subprocess.Popen(
      command,
      stdout=f,
      stderr=f,
      start_new_session=True,
      cwd=run_dir,
      shell=True
    )


def dispatch_to_vsc3(command, run_dir, config: Configuration, sleep_in_sec):
  time_in_minutes = duration_string_to_minutes(config.dispatch.time)
  queue = 'gpu_a40dual' if config.dispatch.queue == "default" else config.dispatch.queue
  if config.computation.n_local_devices:
    n_gpus = config.computation.n_local_devices
  elif queue in ['gpu_a40dual', 'gpu_a100_dual']:
    n_gpus = 2
  else:
    n_gpus = 1
  n_nodes = config.computation.n_nodes
  if (n_nodes > 1) and ('a40' in queue) and (n_gpus < 2):
    print(
      "You requested multiple A40 nodes, using only 1 GPU each. Are you sure, you want this?"
    )
  jobfile_content = get_jobfile_content_vsc3(
    ' '.join(command), config.experiment_name, queue, time_in_minutes,
    config.dispatch.conda_env, sleep_in_sec, n_gpus, n_nodes
  )

  with open(os.path.join(run_dir, 'job.sh'), 'w') as f:
    f.write(jobfile_content)
  subprocess.run(['sbatch', 'job.sh'], cwd=run_dir)


def dispatch_to_vsc5(command, run_dir, config: Configuration, sleep_in_sec):
  time_in_minutes = duration_string_to_minutes(config.dispatch.time)
  queue = 'gpu_a100_dual' if config.dispatch.queue == "default" else config.dispatch.queue
  if config.computation.n_local_devices:
    n_gpus = config.computation.n_local_devices
  elif queue in ['gpu_a40dual', 'gpu_a100_dual']:
    n_gpus = 2
  else:
    n_gpus = 1
  n_nodes = config.computation.n_nodes
  if (n_nodes > 1) and ('a100' in queue) and (n_gpus < 2):
    print(
      "You requested multiple A100 nodes, using only 1 GPU each. Are you sure, you want this?"
    )
  jobfile_content = get_jobfile_content_vsc5(
    ' '.join(command), config.experiment_name, queue, time_in_minutes,
    config.dispatch.conda_env, sleep_in_sec, n_gpus, n_nodes
  )

  with open(os.path.join(run_dir, 'job.sh'), 'w') as f:
    f.write(jobfile_content)
  subprocess.run(['sbatch', 'job.sh'], cwd=run_dir)


def dispatch_to_vsc4(command, run_dir, config: Configuration, sleep_in_sec):
  time_in_minutes = duration_string_to_minutes(config.dispatch.time)
  queue = 'mem_0096' if config.dispatch.queue == "default" else config.dispatch.queue
  jobfile_content = get_jobfile_content_vsc4(
    ' '.join(command), config.experiment_name, queue, time_in_minutes
  )

  with open(os.path.join(run_dir, 'job.sh'), 'w') as f:
    f.write(jobfile_content)
  subprocess.run(['sbatch', 'job.sh'], cwd=run_dir)


def append_nfs_to_fullpaths(command):
  ret = copy.deepcopy(command)
  for idx, r in enumerate(ret):
    if r.startswith("/") and os.path.exists(r):
      ret[idx] = "/nfs" + r
  return ret


def _map_dgx_path(path):
  path = Path(path).resolve()
  if str(path).startswith("/home"):
    return "/nfs" + str(path)
  else:
    return path


def dispatch_to_dgx(command, run_dir, config: Configuration, sleep_in_sec):
  command = append_nfs_to_fullpaths(command)
  time_in_minutes = duration_string_to_minutes(config.dispatch.time)
  src_dir = _map_dgx_path(Path(__file__).resolve().parent.parent)
  jobfile_content = get_jobfile_content_dgx(
    ' '.join(command), config.experiment_name,
    _map_dgx_path(os.path.abspath(run_dir)), time_in_minutes,
    config.dispatch.conda_env, src_dir
  )

  with open(os.path.join(run_dir, 'job.sh'), 'w') as f:
    f.write(jobfile_content)
  subprocess.run(['sbatch', 'job.sh'], cwd=run_dir)


def duration_string_to_minutes(s):
  match = re.search("([0-9]*[.]?[0-9]+)( *)(.*)", s)
  if match is None:
    raise ValueError(f"Invalid time string: {s}")
  amount, unit = float(match.group(1)), match.group(3).lower()
  if unit in ['d', 'day', 'days']:
    return int(amount * 1440)
  elif unit in ['h', 'hour', 'hours']:
    return int(amount * 60)
  elif unit in ['m', 'min', 'mins', 'minute', 'minutes', '']:
    return int(amount)
  elif unit in ['s', 'sec', 'secs', 'second', 'seconds']:
    return int(amount / 60)
  else:
    raise ValueError(f"Invalid unit of time: {unit}")


def get_jobfile_content_vsc4(command, jobname, queue, time):
  return f"""#!/bin/bash
#SBATCH -J {jobname}
#SBATCH -N 1
#SBATCH --partition {queue}
#SBATCH --qos {queue}
#SBATCH --output CPU.out
#SBATCH --time {time}
module purge
{command}"""


def get_jobfile_content_vsc3(
  command, jobname, queue, time, conda_env, sleep_in_seconds, n_local_gpus,
  n_nodes
):
  if (n_nodes > 1) or (queue != 'gpu_a40dual'):
    nodes_string = f"#SBATCH -N {n_nodes}"
  else:
    nodes_string = ""
  return f"""#!/bin/bash
#SBATCH -J {jobname}
{nodes_string}
#SBATCH --partition {queue}
#SBATCH --qos {queue}
#SBATCH --output GPU.out
#SBATCH --time {time}
#SBATCH --gres=gpu:{n_local_gpus}

module purge
module load cuda/11.4.2
source /opt/sw/x86_64/glibc-2.17/ivybridge-ep/anaconda3/5.3.0/etc/profile.d/conda.sh
conda activate {conda_env}
export WANDB_DIR="${{HOME}}/tmp"
export CUDA_VISIBLE_DEVICES=$(deeperwin select-gpus --n-gpus {n_local_gpus} --sleep {sleep_in_seconds})
srun {command}"""


def get_jobfile_content_vsc5(
  command, jobname, queue, time, conda_env, sleep_in_seconds, n_local_gpus,
  n_nodes
):
  return f"""#!/bin/bash
#SBATCH -J {jobname}
#SBATCH -N {n_nodes}
#SBATCH --partition {queue}
#SBATCH --qos goodluck
#SBATCH --output GPU.out
#SBATCH --time {time}
#SBATCH --gres=gpu:{n_local_gpus}

module purge
module load cuda/11.5.0-gcc-11.2.0-ao7cp7w
source /gpfs/opt/sw/spack-0.17.1/opt/spack/linux-almalinux8-zen3/gcc-11.2.0/miniconda3-4.12.0-ap65vga66z2rvfcfmbqopba6y543nnws/etc/profile.d/conda.sh
conda activate {conda_env}
export WANDB_DIR="${{HOME}}/tmp"
export CUDA_VISIBLE_DEVICES=$(deeperwin select-gpus --n-gpus {n_local_gpus} --sleep {sleep_in_seconds})
srun {command}"""


def get_jobfile_content_dgx(command, jobname, jobdir, time, conda_env, src_dir):
  return f"""#!/bin/bash
#SBATCH -J {jobname}
#SBATCH -N 1
#SBATCH --output GPU.out
#SBATCH --time {time}
#SBATCH --gres=gpu:1
#SBATCH --chdir {jobdir}

export CONDA_ENVS_PATH="/nfs$HOME/.conda/envs:$CONDA_ENVS_PATH"
source /opt/anaconda3/etc/profile.d/conda.sh 
conda activate {conda_env}
export PYTHONPATH="{src_dir}"
export WANDB_API_KEY=$(grep -Po "(?<=password ).*" /nfs$HOME/.netrc)
export CUDA_VISIBLE_DEVICES="0"
export WANDB_DIR="/nfs${{HOME}}/tmp"
export XLA_FLAGS=--xla_gpu_force_compilation_parallelism=1
{command}"""


def dispatch_job(command, job_dir, config, sleep_in_sec):
  dispatch_to = config.dispatch.system
  if dispatch_to == "auto":
    dispatch_to = "local"
    if os.uname()[1] == "gpu1-mat":  # HGX
      dispatch_to = "local_background"
    elif os.path.exists("/etc/slurm/slurm.conf"):
      with open('/etc/slurm/slurm.conf') as f:
        slurm_conf = f.readlines()
        if 'slurm.vda.univie.ac.at' in ''.join(slurm_conf):
          dispatch_to = "dgx"
    if os.environ.get('HOSTNAME', '').startswith("l5"):
      dispatch_to = "vsc5"
    elif 'HPC_SYSTEM' in os.environ:
      dispatch_to = os.environ["HPC_SYSTEM"].lower()  # vsc3 or vsc4
  logging.info(f"Dispatching command {' '.join(command)} to: {dispatch_to}")
  dispatch_func = dict(
    local=dispatch_to_local,
    local_background=dispatch_to_local_background,
    vsc3=dispatch_to_vsc3,
    vsc4=dispatch_to_vsc4,
    vsc5=dispatch_to_vsc5,
    dgx=dispatch_to_dgx
  )[dispatch_to]
  dispatch_func(command, job_dir, config, sleep_in_sec)
