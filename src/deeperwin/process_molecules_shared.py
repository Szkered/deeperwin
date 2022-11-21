#!/usr/bin/env python3
"""
CLI to process multiple molecules with shared optimization.
"""
import copy
import logging
import os
import time
from dataclasses import dataclass
from typing import Tuple, Optional, List

import jax.numpy as jnp
import jax.random
import numpy as np
from jax.config import config as jax_config
from jax.lib import xla_bridge

from deeperwin.configuration import Configuration, SharedOptimizationConfig, OptimizationConfig, LoggingConfig
from deeperwin.run_tools.dispatch import idx_to_job_name
from deeperwin.evaluation import evaluate_wavefunction
from deeperwin.loggers import LoggerCollection, build_dpe_root_logger, WavefunctionLogger
from deeperwin.mcmc import MCMCState, MetropolisHastingsMonteCarlo, resize_nr_of_walkers
from deeperwin.model import build_log_psi_squared
from deeperwin.optimization import build_value_and_grad_func, build_optimizer, init_clipping_state
from deeperwin.utils import getCodeVersion, split_params, merge_params, setup_job_dir, get_number_of_params, replicate_across_devices, get_from_devices

logger = logging.getLogger("dpe")


@dataclass
class WaveFunctionData:
  physical = None
  fixed_params = None
  unique_trainable_params = None
  mcmc_state: MCMCState = None
  clipping_params: Optional[Tuple[float]] = None
  checkpoints = {}
  loggers = None
  current_metrics = {}
  n_opt_epochs: int = 0
  last_epoch_optimized: int = 0


def init_wfs(config: Configuration):
  wfs = []
  physical_configs = config.physical.set_from_changes()
  for i, p in enumerate(
    physical_configs
  ):  # self.shared_opt_config.config_changes):
    logger.info(f"Init wavefunction {i}...")
    # init WF object
    wf = WaveFunctionData()
    wf.physical = p

    # init parameters
    new_log_psi_squared, new_orbitals_func, new_trainable_params, wf.fixed_params = build_log_psi_squared(
      config.model, p
    )
    new_shared_params, wf.unique_trainable_params = split_params(
      new_trainable_params,
      config.optimization.shared_optimization.shared_modules
    )
    # in case of first WF, set shared_params and log_psi_squared for all WFs
    if i == 0:
      shared_params = new_shared_params
      log_psi_squared = new_log_psi_squared
      orbitals_func = new_orbitals_func

    # initialize and warm up MCMC state of WF
    logger.info(f"Starting warm-up for wf {i}...")
    # wf.mcmc_state.log_psi_sqr = log_psi_squared(new_trainable_params, *wf.mcmc_state.build_batch(wf.fixed_params))
    # wf.mcmc_state = mcmc.run_burn_in_opt(log_psi_squared, (new_trainable_params, wf.fixed_params), wf.mcmc_state)

    # make folder for single WF (stores adjusted config and logger data)
    job_name = idx_to_job_name(i)
    job_dir = setup_job_dir(".", job_name)

    # init loggers
    loggers = LoggerCollection(
      config.logging,
      config.experiment_name + "_" + job_name,
      save_path=job_name,
      prefix=job_name
    )
    loggers.on_run_begin()
    loggers.log_tags(config.logging.tags)
    if 'baseline_energies' in wf.fixed_params:
      loggers.log_metrics(wf.fixed_params['baseline_energies'])
    loggers.log_param("n_params", get_number_of_params(new_trainable_params))
    loggers.log_param("n_params_shared", get_number_of_params(shared_params))
    loggers.log_param(
      "n_params_unique", get_number_of_params(wf.unique_trainable_params)
    )

    wf.loggers = loggers
    # save full config for single wavefunction
    config_wf = copy.deepcopy(config)
    config_wf.physical = p
    config_wf.optimization.shared_optimization = None
    config_wf.save(os.path.join(job_dir, "full_config.yml"))

    # prepare checkpoints
    wfs.append(wf)

  # # build optimizer
  # if config.optimization.optimizer.name == 'kfac':
  #     grad_loss_func = build_grad_loss_kfac(log_psi_squared, config.optimization.clipping)
  # else:
  #     grad_loss_func = build_value_and_grad_func(log_psi_squared, config.optimization.clipping)
  #
  # trainable_params = merge_trainable_params(shared_params, wfs[0].unique_trainable_params)
  # opt_get_params, optimize_epoch, opt_state, opt_set_params = build_optimizer(log_psi_squared, grad_loss_func,
  #                                                                             mcmc, trainable_params,
  #                                                                             wfs[0].fixed_params,
  #                                                                             config.optimization,
  #                                                                             config.mcmc.n_walkers_opt,
  #                                                                             mcmc_state=wfs[0].mcmc_state)

  return log_psi_squared, orbitals_func, wfs, shared_params


# def update_opt_state(opt_state_old, get_params_func, opt_set_params, unique_trainable_params, shared_modules):
#     shared_params, _ = split_trainable_params(get_params_func(opt_state_old), shared_modules)
#     new_params = merge_trainable_params(shared_params, unique_trainable_params)
#     return opt_set_params(opt_state_old, new_params)


def get_index(n_epoch, wfs, config: SharedOptimizationConfig):
  method = config.scheduling_method
  if method == "round_robin":
    return n_epoch % len(wfs)
  elif method == 'stddev':
    wf_ages = n_epoch - jnp.array([wf.last_epoch_optimized for wf in wfs])
    if n_epoch < len(wfs) * 10:
      index = n_epoch % len(wfs)
    elif jnp.any(wf_ages > config.max_age):
      index = jnp.argmax(wf_ages)
    else:
      stddevs = [wf.current_metrics['E_std'] for wf in wfs]
      index = np.argmax(stddevs)
    return index
  else:
    raise ("Wavefunction scheduler currently not supported.")


# def _log_weights(wf: WaveFunctionData, shared_params, opt_state, opt_get_params, opt_set_params, shared_modules):
#     wf_params = merge_trainable_params(shared_params, wf.unique_trainable_params)
#     wf_opt_state = update_opt_state(opt_state, opt_get_params, opt_set_params, wf.unique_trainable_params,
#                                     shared_modules)
#     full_data = prepare_data_for_logging(wf_params, wf.fixed_params, wf.mcmc_state, wf_opt_state)
#     wf.loggers.log_weights(full_data)

# optimize_wavefunction(
#             log_psi_squared,
#             params,
#             fixed_params,
#             mcmc_state,
#             config.optimization,
#             config.physical,
#             loggers,
#             opt_state,
#             clipping_state,
#         )


def optimize_shared(
  log_psi_squared, shared_params, wfs: List[WaveFunctionData],
  opt_config: OptimizationConfig, initial_opt_state
):
  shared_modules = opt_config.shared_optimization.shared_modules
  n_wfs = len(wfs)
  n_devices = jax.device_count()
  log_psi_squared_pmapped = jax.pmap(log_psi_squared)

  # Run burn-in of MCMC states
  mcmc = MetropolisHastingsMonteCarlo(opt_config.mcmc)
  for i, wf in enumerate(wfs):
    logging.info(f"Running burn-in for wavefunction {i}")
    wf.mcmc_state = MCMCState.resize_or_init(
      wf.mcmc_state, opt_config.mcmc.n_walkers, wf.physical, n_devices
    )
    wf.clipping_params = wf.clipping_params or init_clipping_state()
    params = merge_params(shared_params, wf.unique_trainable_params)
    params, wf.fixed_params, wf.clipping_params = replicate_across_devices(
      (params, wf.fixed_params, wf.clipping_params)
    )
    wf.mcmc_state = mcmc.run_burn_in(
      log_psi_squared, wf.mcmc_state, params, wf.fixed_params
    )
  wf_loggers = [WavefunctionLogger(wf.loggers) for wf in wfs]

  # Initialize variational optimization
  rng = jax.random.split(jax.random.PRNGKey(0), n_devices)
  value_and_grad_func = build_value_and_grad_func(
    log_psi_squared, opt_config.clipping
  )
  optimizer = build_optimizer(
    value_and_grad_func, opt_config.optimizer, True, True
  )
  opt_state = optimizer.init(
    params, rng, wf.mcmc_state.build_batch(wf.fixed_params), wf.clipping_params
  )
  opt_state = initial_opt_state or opt_state

  for n_epoch in range(opt_config.n_epochs * n_wfs):
    n_epochs_geom = n_epoch // n_wfs

    # get next index for optimization
    index_next = get_index(n_epoch, wfs, opt_config.shared_optimization)
    wf = wfs[index_next]

    # optimize wf[index] for one eppoch
    params = merge_params(params, wf.unique_trainable_params)
    wf.mcmc_state.log_psi_sqr = log_psi_squared_pmapped(
      params, *wf.mcmc_state.build_batch(wf.fixed_params)
    )
    wf.mcmc_state = mcmc.run_inter_steps(
      log_psi_squared, wf.mcmc_state, params, wf.fixed_params
    )
    params, opt_state, wf.clipping_params, stats = optimizer.step(
      params,
      opt_state,
      rng=None,
      batch=wf.mcmc_state.build_batch(wf.fixed_params),
      func_state=wf.clipping_params
    )
    shared_params, wf.unique_trainable_params = split_params(
      params, shared_modules
    )
    wf_loggers[i].log_step(
      E_loc_unclipped=stats["aux"]["E_loc_unclipped"],
      E_loc_clipped=stats["aux"]["E_loc_clipped"],
      E_ref=wf.physical.E_ref,
      mcmc_state=wf.mcmc_state,
      opt_stats=get_from_devices(stats)
    )

    # update metrics + epoch counter
    wf.current_metrics = dict(
      E_mean=stats["aux"]["E_loc_clipped"],
      E_std=jnp.nanstd(stats["aux"]["E_loc_clipped"])
    )
    wf.n_opt_epochs += 1
    wf.last_epoch_optimized = n_epoch

    # # collect epoch time
    # t_end = time.time()
    #
    # # log metrics
    # if wf.loggers is not None:
    #     E_ref = wf.fixed_params['baseline_energies']['E_ref']
    #     # Will not work right now: must include energy history in logging
    #     metrics = calculate_metrics(n_epoch, E_epoch, E_epoch_unclipped, wf.mcmc_state, None, t_end - t_start, "opt", wf.n_opt_epochs, E_ref)
    #     wf.loggers.log_metrics(*metrics)
    #
    # # check for checkpoints. if any, all wfs have the same checkpoints.
    # if n_epochs_geom in wf.checkpoints and n_epoch % n_wfs == 0:
    #     trainable_params = opt_get_params(opt_state)
    #     shared_params, _ = split_trainable_params(trainable_params,
    #                                               config.optimization.shared_optimization.shared_modules)
    #     for wf in wfs:
    #         if wf.loggers is not None:
    #             # log weights
    #             _log_weights(wf, shared_params, opt_state, opt_get_params, opt_set_params, shared_modules)
    #
    #             # log checkpoint
    #             logger.info(f"Logging checkpoint to folder {wf.checkpoints[n_epochs_geom]}")
    #             wf.loggers.log_checkpoint(wf.checkpoints[n_epochs_geom])
    #
    # # reset clock and update index
    # t_start = time.time()
  return wfs, shared_params, opt_state


def process_molecule_shared(config_file):
  config = Configuration.load(config_file)
  logger = build_dpe_root_logger(config.logging.basic)

  config.save("full_config.yml")
  loggers_set = LoggerCollection(config.logging, config.experiment_name)
  loggers_set.on_run_begin()
  loggers_set.log_params(config.as_flattened_dict())
  loggers_set.log_tags(config.logging.tags)

  jax_config.update(
    "jax_enable_x64", config.computation.float_precision == "float64"
  )
  jax_config.update("jax_disable_jit", config.computation.disable_jit)
  logger.debug(f"Used hardware: {xla_bridge.get_backend().platform}")
  loggers_set.log_param("code_version", getCodeVersion())

  # Initialization of model, MCMC states and optimizer TODO restart and reload
  log_psi_squared, orbitals_func, wfs, shared_params = init_wfs(config)
  opt_state = None

  # Wavefunction optimization
  if config.optimization.n_epochs > 0:
    logger.info("Starting optimization...")
    wfs, opt_get_params, opt_set_params, opt_state = optimize_shared(
      log_psi_squared, shared_params, wfs, config.optimization, opt_state
    )
    trainable_params = opt_get_params(opt_state)
    shared_params, _ = split_trainable_params(
      trainable_params, config.optimization.shared_optimization.shared_modules
    )
  for wf in wfs:
    _log_weights(
      wf, shared_params, opt_state, opt_get_params, opt_set_params,
      config.optimization.shared_optimization.shared_modules
    )

  # Wavefunction evaluation
  if config.evaluation.n_epochs > 0:
    logger.info("Starting evaluation...")
    error_set = []
    sigma_set = []
    error_plus_2_stdev_set = []

    evaluation_step_func = build_evaluation_step(
      log_psi_squared, mcmc, config.evaluation
    )
    for i, wf in enumerate(wfs):
      logger.info(f"Starting evaluation for wavefunction {i}...")
      mcmc_state = resize_nr_of_walkers(
        wf.mcmc_state, config.mcmc.n_walkers_eval
      )
      trainable_params = merge_trainable_params(
        shared_params, wf.unique_trainable_params
      )
      E_eval, forces_eval, mcmc_state = evaluate_wavefunction(
        log_psi_squared, trainable_params, wf.fixed_params, mcmc, mcmc_state,
        config.evaluation, wf.loggers, evaluation_step_func
      )
      # Postprocessing
      E_mean = jnp.nanmean(E_eval)
      E_mean_sigma = jnp.nanstd(E_eval) / jnp.sqrt(len(E_eval))

      wf.loggers.log_metrics(
        dict(E_mean=E_mean, E_mean_sigma=E_mean_sigma), metric_type="eval"
      )
      if wf.physical.E_ref is not None:
        error_eval, sigma_eval = 1e3 * (
          E_mean - wf.physical.E_ref
        ), 1e3 * E_mean_sigma
        wf.loggers.log_metrics(
          dict(
            error_eval=error_eval,
            sigma_error_eval=sigma_eval,
            error_plus_2_stdev=error_eval + 2 * sigma_eval
          )
        )
        error_set.append(error_eval)
        sigma_set.append(sigma_eval)
        error_plus_2_stdev_set.append(error_eval + 2 * sigma_eval)

      if forces_eval is not None:
        forces_mean = jnp.nanmean(forces_eval, axis=0)
        wf.loggers.log_metric('forces_mean', forces_mean)

    loggers_set.log_metrics(
      dict(
        error_eval=jnp.nanmean(jnp.array(error_set)),
        sigma_error_eval=jnp.nanmean(jnp.array(sigma_set)),
        error_plus_2_stdev=jnp.nanmean(jnp.array(error_plus_2_stdev_set))
      ),
      metric_type="eval"
    )
  for wf in wfs:
    wf.loggers.on_run_end()
  loggers_set.on_run_end()


if __name__ == '__main__':
  import sys
  process_molecule_shared(sys.argv[1])
