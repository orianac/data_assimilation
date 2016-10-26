# =========================================================== #
# This script produces true and synthetic surface soil moisture measurements
#    - Run VIC with perturbed forcings and states --> "truth"
#    - Add random noise to "truth" top-layer soil moisture --> synthetic measurements
# =========================================================== #

import sys
import os
import numpy as np
import xarray as xr
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

from tonic.models.vic.vic import VIC
from tonic.io import read_configobj

from da_utils import (Forcings, setup_output_dirs, propagate,
                      perturb_soil_moisture_states, concat_vic_history_files,
                      calculate_max_soil_moist_domain, VarToPerturb)

# =========================================================== #
# Load command line arguments
# =========================================================== #
cfg = read_configobj(sys.argv[1])
mpi_proc = int(sys.argv[2])

# =========================================================== #
# Set random generation seed
# =========================================================== #
np.random.seed(cfg['CONTROL']['seed'])

# =========================================================== #
# Process some config parameters
# =========================================================== #
print('Processing config parameters...')
# Simulation time
start_time = pd.to_datetime(cfg['TIME_INDEX']['start_time'])
end_time = pd.to_datetime(cfg['TIME_INDEX']['end_time'])

# Construct forcing variable name dictionary
dict_varnames = {}
dict_varnames['PREC'] = cfg['VIC']['PREC']

# Set up output sub-directories
dirs = setup_output_dirs(os.path.join(cfg['CONTROL']['root_dir'],
                                      cfg['OUTPUT']['output_basedir']),
                         mkdirs=['truth', 'synthetic_meas', 'plots'])
truth_subdirs = setup_output_dirs(dirs['truth'],
                                  mkdirs=['global', 'history', 'states',
                                          'forcings', 'logs'])
meas_subdirs = setup_output_dirs(
                    dirs['synthetic_meas'],
                    mkdirs=['indep_prec_meas'])

# Construct time points for synthetic measurement (daily, last hour)
meas_times = pd.date_range(
        '{}{:02d}{:02d}-{:02d}'.format(start_time.year,
                                       start_time.month,
                                       start_time.day,
                                       cfg['TIME_INDEX']['last_hour']),
        '{}{:02d}{:02d}-{:02d}'.format(end_time.year,
                                       end_time.month,
                                       end_time.day,
                                       cfg['TIME_INDEX']['last_hour']),
        freq='D')

# VIC global template file
global_template = os.path.join(cfg['CONTROL']['root_dir'],
                               cfg['VIC']['vic_global_template'])

# =========================================================== #
# Simulate "truth" - run VIC with perturbed forcings and
# states
# =========================================================== #
print('Simulating \"truth\" - run VIC with perturbed forcings and states...')
# --- Create class VIC --- #
vic_exe = VIC(os.path.join(cfg['CONTROL']['root_dir'], cfg['VIC']['exe']))

# --- Prepare perturbed forcing data - "true" forcing --- #
# --- Also, simulate an independent set of synthetic precipitation
# measurement by adding noise to "true" precipitation --- #
print('\tPreparing perturbed forcing data...')
start_year = start_time.year
end_year = end_time.year
for year in range(start_year, end_year+1):
    # --- Prepare perturbed forcing - "true" forcing --- #
    # Construct Forcings class
    class_forcings_orig = Forcings(xr.open_dataset(
                os.path.join(cfg['CONTROL']['root_dir'],
                             '{}{}.nc'.format(cfg['VIC']['orig_forcing_nc_basepath'],
                                              year))))
    # Perturb precipitaiton
    ds_true = class_forcings_orig.perturb_prec_lognormal(
                                varname=dict_varnames['PREC'],
                                std=cfg['FORCINGS_STATES_PERTURB']['prec_std'])
    # Save to nc file
    ds_true.to_netcdf(os.path.join(truth_subdirs['forcings'],
                                   'forc_perturbed.{}.nc'.format(year)),
                      format='NETCDF4_CLASSIC')

    # --- Simulate independent precipitaiton measurement --- #
    # Construct Forcings class
    class_forcings_true = Forcings(ds_true)
    # Perturbe precipitation
    ds_perturbed = class_forcings_true.perturb_prec_lognormal(
                                varname=dict_varnames['PREC'],
                                std=cfg['FORCINGS_STATES_PERTURB']['prec_std'])
    da_prec_perturbed = ds_perturbed[dict_varnames['PREC']]
    # Save to nc file
    ds_simulated = xr.Dataset({'simulated_indep_prec_meas': da_prec_perturbed})
    ds_simulated.to_netcdf(os.path.join(
                                meas_subdirs['indep_prec_meas'],
                                'indep_prec.{}.nc'.format(year)),
                           format='NETCDF4_CLASSIC')

# --- Run VIC with perturbed forcings and soil moisture states --- #
# Initialize a list of file paths to be concatenated
list_history_paths = []

# (1) Run VIC until the first measurement time point (no initial state)
prop_period_stamp = '{}-{}'.format(start_time.strftime('%Y%m%d_%H%S'),
                                   meas_times[0].strftime('%Y%m%d_%H%S'))
print('\tRun VIC until the first measurement time {}...'.format(prop_period_stamp))
# Prepare log directories
log_dir = setup_output_dirs(
                    truth_subdirs['logs'],
                    mkdirs=['propagate.{}'.format(prop_period_stamp)])\
          ['propagate.{}'.format(prop_period_stamp)]
# Propagate until the first measurement point
propagate(start_time=start_time, end_time=meas_times[0],
          vic_exe=vic_exe, vic_global_template_file=global_template,
          vic_model_steps_per_day=cfg['VIC']['model_steps_per_day'],
          init_state_nc=None,
          out_state_basepath=os.path.join(truth_subdirs['states'],
                                          'propagated.state'),
          out_history_dir=truth_subdirs['history'],
          out_history_fileprefix='history',
          out_global_basepath=os.path.join(truth_subdirs['global'], 'global'),
          out_log_dir=log_dir,
          forcing_basepath=os.path.join(truth_subdirs['forcings'],
                                        'forc_perturbed.'),
          mpi_proc=mpi_proc,
          mpi_exe=cfg['VIC']['mpi_exe'])
# Concat output history file to the list to be concatenated
list_history_paths.append(os.path.join(truth_subdirs['history'],
                                       'history.{}-{:05d}.nc'.format(
                                            start_time.strftime('%Y-%m-%d'),
                                            start_time.hour*3600+start_time.second)))

# (2) Loop over until each measurement point and run VIC
for t in range(len(meas_times)):
    # --- Determine last, current and next time point (all these are time
    # points at the beginning of a time step)--- #
    last_time = meas_times[t]
    current_time = last_time +\
                   pd.DateOffset(hours=24/cfg['VIC']['model_steps_per_day'])
    if t == len(meas_times)-1:  # if this is the last measurement time
        next_time = end_time
    else:  # if not the last measurement time
        next_time = meas_times[t+1]
    # If current_time > next_time, do nothing (we already reach the end of the simulation)
    if current_time > next_time:
        break
    print('\tRun VIC ', current_time, 'to', next_time, '(perturbed forcings and states)')

    # --- Perturb states --- #
    state_time = last_time + pd.DateOffset(days=1/cfg['VIC']['model_steps_per_day'])
    orig_state_nc = os.path.join(
                            truth_subdirs['states'],
                            'propagated.state.{}_{:05d}.nc'.format(
                                    state_time.strftime('%Y%m%d'),
                                    state_time.hour*3600+state_time.second))
    perturbed_state_nc = os.path.join(
                            truth_subdirs['states'],
                            'perturbed.state.{}_{:05d}.nc'.format(
                                    state_time.strftime('%Y%m%d'),
                                    state_time.hour*3600+state_time.second))
    perturb_soil_moisture_states(
            states_to_perturb_nc=orig_state_nc,
            global_path=global_template,
            sigma_percent=cfg['FORCINGS_STATES_PERTURB']['state_perturb_sigma_percent'],
            out_states_nc=perturbed_state_nc)

    # --- Propagate to the next time point --- #
    # Prepare log directories
    prop_period_stamp = '{}-{}'.format(current_time.strftime('%Y%m%d_%H%S'),
                                       next_time.strftime('%Y%m%d_%H%S'))
    log_dir = setup_output_dirs(
                    truth_subdirs['logs'],
                    mkdirs=['propagate.{}'.format(prop_period_stamp)])\
              ['propagate.{}'.format(prop_period_stamp)]
    propagate(start_time=current_time, end_time=next_time,
              vic_exe=vic_exe, vic_global_template_file=global_template,
              vic_model_steps_per_day=cfg['VIC']['model_steps_per_day'],
              init_state_nc=perturbed_state_nc,
              out_state_basepath=os.path.join(truth_subdirs['states'],
                                              'propagated.state'),
              out_history_dir=truth_subdirs['history'],
              out_history_fileprefix='history',
              out_global_basepath=os.path.join(truth_subdirs['global'], 'global'),
              out_log_dir=log_dir,
              forcing_basepath=os.path.join(truth_subdirs['forcings'],
                                            'forc_perturbed.'),
              mpi_proc=mpi_proc,
              mpi_exe=cfg['VIC']['mpi_exe'])
    # Concat output history file to the list to be concatenated
    list_history_paths.append(os.path.join(truth_subdirs['history'],
                                           'history.{}-{:05d}.nc'.format(
                                                current_time.strftime('%Y-%m-%d'),
                                                current_time.hour*3600+current_time.second)))

# (3) Concatenate all history files
print('Concatenating all history files...')
ds_concat = concat_vic_history_files(list_history_paths)

# Save to history output directory
print('Saving history file to netCDF...')
first_time = pd.to_datetime(ds_concat['time'][0].values)
last_time = pd.to_datetime(ds_concat['time'][-1].values)
hist_concat_nc = os.path.join(truth_subdirs['history'],
                                 'history.concat.{}_{:05d}-{}_{:05d}.nc'.format(
                                        first_time.strftime('%Y%m%d'),
                                        first_time.hour*3600+first_time.second,
                                        last_time.strftime('%Y%m%d'),
                                        last_time.hour*3600+last_time.second))
ds_concat.to_netcdf(hist_concat_nc)

# =========================================================== #
# Simulate synthetic measurement - Extract top-layer soil
# moisture from "truth" at the end of each day, and add noise
# =========================================================== #

print('Simulating synthetic measurements...')

# --- Load history file --- #
ds_hist = xr.open_dataset(hist_concat_nc)

# --- Select out times of measurement --- #
# Extract full times
orig_times = pd.to_datetime(ds_hist['time'].values)
# Find indices of measurement time points in orig_times
list_time_index = []

for i, time in enumerate(meas_times):
    tmp = (abs(orig_times - time).days == 0) & (abs(orig_times - time).seconds <2)
    list_time_index.append(np.where(tmp==True)[0][0])
# Select times of measurement from the history file
ds_hist_meas_times = ds_hist.isel(time=list_time_index)
    
# --- Select top-layer soil moisture --- #
da_sm1_true = ds_hist_meas_times['OUT_SOIL_MOIST'].sel(nlayer=0)

# --- Add noise --- #
# Generate the standard deviation of noise to be added for each grid cell
da_sigma = da_sm1_true[0, :, :].copy()
da_sigma[:] = cfg['SYNTHETIC_MEAS']['sigma']
# Add noise
VarToPerturb_sm1 = VarToPerturb(da_sm1_true) # create class
da_sm1_perturbed = VarToPerturb_sm1.add_gaussian_white_noise(da_sigma)

# --- Save synthetic measurement to netCDF file --- #
ds_simulated = xr.Dataset({'simulated_surface_sm': da_sm1_perturbed})
ds_simulated.to_netcdf(os.path.join(dirs['synthetic_meas'],
                                    'synthetic_meas.{}_{}.nc'.format(start_time.strftime('%Y%m%d'),
                                                                     end_time.strftime('%Y%m%d'))),
                       format='NETCDF4_CLASSIC')

# =========================================================== #
# Plot - compare orig. and simulated sm1, at measurement time
# points
# =========================================================== #
# Extrac lat's and lon's
lat = da_sm1_true['lat'].values
lon = da_sm1_true['lon'].values

# Plot
for lt in lat:
    for lg in lon:
        if np.isnan(da_sm1_true.loc[da_sm1_true['time'][0],
                                    lt, lg].values) == True:  # if inactive cell, skip
            continue
        
        # Create figure
        fig = plt.figure(figsize=(12, 6))
        # plot truth
        da_sm1_true.loc[:, lt, lg].to_series().plot(
                color='k', style='-',
                label='Truth (VIC by perturbed forcings and states)',
                legend=True)
        # plot simulated measurement
        da_sm1_perturbed.loc[:, lt, lg].to_series().plot(
                color='r', style='--',
                label='Simulated meas. (perturbed truth)',
                legend=True)
        plt.xlabel('Time')
        plt.ylabel('Soil moisture (mm)')
        plt.title('Top-layer soil moisture, {}, {}'.format(lt, lg))
        fig.savefig(os.path.join(dirs['plots'],
                                 'check_plot.{}_{}.png'.format(lt, lg)),
                    format='png')