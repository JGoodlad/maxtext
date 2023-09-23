#!/usr/bin/env python3

from multihost_job import main as multihost_job_main
import yaml
import copy
import os
import re

args = {
    'dryrun': True,
    'tpu': 'v4', # 'v4' 'v5'
    'stable': False,
}


def update_yaml_fields(yaml_data, update_dict, allow_new_keys=False):
    yaml_copy=copy.deepcopy(yaml_data)
    for key, value in update_dict.items():
        if not allow_new_keys:
            assert key in yaml_copy, key
        yaml_copy[key] = value
    return yaml_copy


BASE_MHJ_CMD="""export LIBTPU_INIT_ARGS="--xla_tpu_spmd_rng_bit_generator_unsafe=true --xla_tpu_enable_data_parallel_all_reduce_opt=true --xla_tpu_data_parallel_opt_different_sized_ops=true --xla_tpu_enable_async_collective_fusion=true --xla_tpu_enable_async_collective_fusion_fuse_all_gather=true --xla_tpu_enable_async_collective_fusion_multiple_steps=true --xla_tpu_overlap_compute_collective_tc=true --xla_enable_async_all_gather=true" && \
bash setup_with_retries.sh && \
bash rto_setup.sh && \
python3 MaxText/train.py """

BASE_MHJ_CMD_14_CP="""export LIBTPU_INIT_ARGS="--xla_tpu_spmd_rng_bit_generator_unsafe=true --xla_tpu_enable_data_parallel_all_reduce_opt=true --xla_tpu_data_parallel_opt_different_sized_ops=true --xla_tpu_enable_async_collective_fusion=true --xla_tpu_enable_async_collective_fusion_fuse_all_gather=true --xla_tpu_enable_async_collective_fusion_multiple_steps=true --xla_tpu_overlap_compute_collective_tc=true --xla_enable_async_all_gather=true" && \
export TPU_LIBRARY_PATH=$HOME/custom_libtpu/libtpu.so && \
bash setup_with_retries.sh JAX_VERSION=0.4.14 LIBTPU_GCS_PATH=gs://libtpu_internal/mattdavidow/viperlite/2023-08-24-23:56:27-libtpu.so && \
bash rto_setup.sh && \
python3 MaxText/train.py """

def bname(b: bool):
    assert b == True or b == False, f'not bool: "{b}"'
    return str(b)[0]

def run_job(run_name, base_config, **config_updates):
    maxtext_config = update_yaml_fields(base_config, config_updates)
    model_size = maxtext_config['global_parameter_scale']
    with open('MaxText/configs/base.yml', 'r') as file:
        base_yml = yaml.safe_load(file)

    yml = update_yaml_fields(base_yml, maxtext_config)

    num_slice = yml['num_slice']
    tokens_per_seq = yml['max_target_length']
    seqs_per_chip = yml['per_device_batch_size']
    fill_ratio = yml['fill_ratio']

    def calc_chinchilla_step_count(num_params_billions, num_slice, seqs_per_chip, tokens_per_seq, fill_ratio):
        billion = 2 ** 30
        chips_per_slice = 256
        needed_tokens = num_params_billions * billion * 20
        tokens_per_step = tokens_per_seq * seqs_per_chip * chips_per_slice * num_slice
        needed_steps = int(needed_tokens / tokens_per_step / fill_ratio)
        return needed_steps
    lr_steps = calc_chinchilla_step_count(num_params_billions=model_size, num_slice=num_slice, seqs_per_chip=seqs_per_chip, tokens_per_seq=tokens_per_seq, fill_ratio=fill_ratio)

    yml = update_yaml_fields(yml, {
        'learning_rate_schedule_steps': lr_steps,
    })

    attempt = args['attempt']
    sweep_name = args['sweep']
    use_cl = args['jax_14_cl']
    assert use_cl, 'forbidden to not use it'
    run_name = f'int8-{sweep_name}-a{attempt}-{run_name}'

    jobre = args['jobre']
    url = f"https://pantheon.corp.google.com/logs/query;query=timestamp%20%3E%20%222023-08-18%22%20AND%20labels.%22agent.googleapis.com%2Flog_file_path%22%3D~%22{run_name}.*%2Fmain_command_log_slice_0_worker_0%22"
    if not re.findall(jobre, run_name):
        print(f"SKIP: {run_name:30}", url)
        return

    print(f"RUN:  {run_name:30}", url)

    yml = update_yaml_fields(yml, {'run_name': run_name})
    experiment_yml_file = f"MaxText/configs/{run_name}.yml"
    with open(experiment_yml_file, 'w') as file:
        yaml.dump(yml, file)

    if args['jax_14_cl']:
        mhj_cmd = BASE_MHJ_CMD_14_CP
    else:
        mhj_cmd = BASE_MHJ_CMD

    experiment_mhj = {
        '--RUN_NAME': run_name,
        '--BUCKET_NAME': 'mattdavidow-maxtext-br',
        '--NUM_SLICE': num_slice,
        '--TPU_TYPE': 'v5litepod-256',  # v5litepod-16
        '--VERSION': 'v2-alpha-tpuv5-lite',
        '--PROJECT': 'tpu-prod-env-multipod',
        '--ZONE': 'us-east5-b',
        '--COMMAND': mhj_cmd + experiment_yml_file,
        '--CQR_EXTRA_ARGS': ' --network=mtu9k'
        # '--COMMAND_TYPE': 'curl'  # Uncomment for Stable fleet
    }
    if args['stable']:
        experiment_mhj['--COMMAND_TYPE'] = 'curl'
        experiment_mhj['--PROJECT'] = 'tpu-prod-env-vlp-2nic'


    # V4_MHJ_DICT={
    #     '--BUCKET_NAME': 'mattdavidow-br',  # for cloud-tpu-multipod-dev
    #     '--NUM_SLICE': 1,
    #     '--TPU_TYPE': 'v4-128',  # v4-8
    #     '--VERSION': 'tpu-ubuntu2204-base',
    #     '--PROJECT': 'cloud-tpu-multipod-dev',
    #     '--ZONE': 'us-central2-b',
    #     '--CQR_EXTRA_ARGS': ' --best-effort',
    # }
    # And this.
    # 'base_output_directory':'gs://max-experiments',
    # 'dataset_path':'gs://maxtext-dataset',

    mhj_args = []
    for key in experiment_mhj.keys():
        mhj_args.append(key)
        mhj_args.append(str(experiment_mhj[key]))

    if args['dryrun']:
        import pprint
        # pprint.pprint(yml)
        # pprint.pprint(experiment_mhj)
        # print()
    else:
        multihost_job_main(mhj_args)

    if args['delyml']:
        os.remove(experiment_yml_file)



def run_s16():
    config = {
        'fwd_int8': True,
        'dlhs_int8': True,
        'drhs_int8': True,
        'learning_rate': 1.e-3,
        'num_slice': 4,
        'save_period': 1000,
        'global_parameter_scale': 8,
    }
    run_job('TTT-checkpoint_baseline-4s', config)

def run_s16_load():
    def run(
            *,
            fwd = True,
            dlhs = True,
            drhs = True,
            lr_mul = 1.0,
            clip_global = 0.0,
    ):
        config = {
            'fwd_int8': fwd,
            'dlhs_int8': dlhs,
            'drhs_int8': drhs,
            'learning_rate': 1.e-3 * lr_mul,
            'num_slice': 4,
            'save_period': 1000,
            'load_from_other_directory': f'gs://maxtext-experiments-multipod/int8-s16-a1-TTT-checkpoint_baseline-4s/checkpoints',
            'load_from_other_directory_step': 4000, # end of warmup
            'clip_by_global_norm': clip_global,
            'global_parameter_scale': 8,
        }
        run_name = f'4s-L-{bname(fwd)}{bname(dlhs)}{bname(drhs)}_global{int(clip_global*10)}-LR{int(lr_mul)}'
        run_job(run_name, config)
    run()
    run(dlhs=False, drhs=False)
    run(fwd=False, dlhs=False, drhs=False)
    run(lr_mul=10.0)
    run(lr_mul=10.0, clip_global=0.5)

# This is a warmup checkpoint generation for S19
def run_s18_8B_16seq_warmup():
    config = {
        'fwd_int8': True,
        'dlhs_int8': True,
        'drhs_int8': True,
        'learning_rate': 1.e-3,
        'num_slice': 4,
        'per_device_batch_size': 16,
        'save_period': 1000,
        'global_parameter_scale': 8,
    }
    run_job('yep', config)

# This is a sweep on: FFF,TFF,TTF,TTT
# For pseudo-final eval on 8B model. 4pods, 16seq
def run_s19():
    def run(
            *,
            fwd = True,
            dlhs = True,
            drhs = True,
    ):
        config = {
            'load_from_other_directory': f'gs://maxtext-experiments-multipod/int8-s18_8B_16seq_warmup-a1-yep/checkpoints',
            'load_from_other_directory_step': 1000,
            'num_slice': 4,
            'per_device_batch_size': 16,
            'fwd_int8': fwd,
            'dlhs_int8': dlhs,
            'drhs_int8': drhs,
            'global_parameter_scale': 8,
            # 'learning_rate': 1.e-3 * lr_mul,
        }
        run_name = f'{bname(fwd)}{bname(dlhs)}{bname(drhs)}'
        run_job(run_name, config)

    run(fwd=False, dlhs=False, drhs=False)
    run(fwd=True, dlhs=False, drhs=False)
    run(fwd=True, dlhs=True, drhs=False)
    run(fwd=True, dlhs=True, drhs=True)


# Same as S19 but back to 4seq and added gradient clipping.
def base_run_s20(
        *,
        fwd = True,
        dlhs = True,
        drhs = True,
        clip_global = 0.3,
        clip_by_ucb = 0, # 0 or 1
        # lrs = 0,  # This is a small delta to LR, meant as a 'seed' replacement
        lr_mul = 1.0,  # This is a small delta to LR, meant as a 'seed' replacement
):
    config = {
        # For seq16
        # 'load_from_other_directory': f'gs://maxtext-experiments-multipod/int8-s18_8B_16seq_warmup-a1-yep/checkpoints',
        # 'load_from_other_directory_step': 1000,
        'save_period': 1000,
        'load_from_other_directory': 'gs://maxtext-experiments-multipod/int8-s16-a1-TTT-checkpoint_baseline-4s/checkpoints',
        'load_from_other_directory_step': 4000, # end of warmup
        'num_slice': 4,
        'per_device_batch_size': 4,
        'fwd_int8': fwd,
        'dlhs_int8': dlhs,
        'drhs_int8': drhs,
        'clip_by_global_norm': clip_global,
        'clip_by_ucb': clip_by_ucb,
        # 'learning_rate': 1.e-3 * (1.0 + lrs / 10000.0),
        'learning_rate': 1.e-3 * lr_mul,
        'global_parameter_scale': 8,
    }
    run_name = f'{bname(fwd)}{bname(dlhs)}{bname(drhs)}-cg{int(clip_global*10):02}-cucb{clip_by_ucb}-lr{int(lr_mul*10):03}'
    run_job(run_name, config)


# S19 was spikey, back to 4seq. Add clip_global.
def run_s20():
    base_run_s20(fwd=False, dlhs=False, drhs=False)
    base_run_s20(dlhs=False, drhs=False)
    base_run_s20(drhs=False)
    base_run_s20(clip_global=0.2)
    base_run_s20(clip_global=0.3)
    base_run_s20(clip_global=0.5)


# S20 we were unlucky with spikes.
# Add more similar runs.
# Add UCB clipping.
def run_s21():
    base_run_s20(drhs=False, clip_global=0.2)
    base_run_s20(drhs=False, clip_global=0.3)
    base_run_s20(drhs=False, clip_global=0.5)
    base_run_s20(drhs=False, clip_global=0.0, clip_by_ucb=1)
    base_run_s20(drhs=False, clip_global=0.0, clip_by_ucb=1, lr_mul=2.0)
    base_run_s20(drhs=False, clip_global=0.0, clip_by_ucb=1, lr_mul=5.0)


# Benchmark 32B on 1 pod
def run_s22():
    def run(int8: bool, pods: int, bs:int, seq:int):
        config = {
            'save_period': 100000,
            'log_period:': 50,
            'num_slice': pods,
            'per_device_batch_size': bs,
            'int8_training' : int8,
            'fwd_int8': True,
            'dlhs_int8': True,
            'drhs_int8': True,
            'global_parameter_scale': 32,
            'steps': 151,
            'max_target_length': seq,
        }
        run_name = f'aqt{bname(int8)}-bs{bs}-seq{seq}-pods{pods}'
        run_job(run_name, config)
    for pods in [1, 2]:
        for bs in [4, 8]:
            for seq in [1024, 2048]:
                for int8 in [True, False]:
                    run(int8=int8, bs=bs, seq=seq, pods=pods)

def run_simple_test():
    config = {
        'log_period:': 20,
        'steps': 22,
        'save_period': 1000,
        'num_slice': 1,
        'per_device_batch_size': 4,
        'global_parameter_scale': 8,
        'fwd_int8':  True,
        'dlhs_int8': True,
        'drhs_int8': True,
    }
    run_name = f''
    run_job('', config)

# Tuned variant of S22
def run_s23():
    def run(int8: bool, pods: int, bs:int, seq:int):
        config = {
            'save_period': 100000,
            'log_period:': 50,
            'num_slice': pods,
            'per_device_batch_size': bs,
            'int8_training' : int8,
            'fwd_int8': True,
            'dlhs_int8': True,
            'drhs_int8': True,
            'global_parameter_scale': 32,
            'steps': 151,
            'max_target_length': seq,
        }
        run_name = f'aqt{bname(int8)}-bs{bs}-seq{seq}-pods{pods}'
        run_job(run_name, config)
    for bs in [8, 12, 16, 20]:
        for int8 in [True, False]:
            run(int8=int8, bs=bs, seq=1024, pods=1)


def base_run_s24(
        *,
        fwd = True,
        dlhs = True,
        drhs = False,
        clip_global = 0.3,
        clip_by_ucb = 0, # 0 or 1
        lr_mul = 1.0,  # This is a small delta to LR, meant as a 'seed' replacement
        load = "",
        load_step = -1,
        num_slice = 4,
        steps = -1,
        quant_pv = False,
        aqt_use_dummy_static_bound = False,
        aqt_rng_type: str = 'jax.uniform',
):
    config = {
        # For seq16
        # 'load_from_other_directory': f'gs://maxtext-experiments-multipod/int8-s18_8B_16seq_warmup-a1-yep/checkpoints',
        # 'load_from_other_directory_step': 1000,
        'save_period': 1000,
        # 'load_from_other_directory': 'gs://maxtext-experiments-multipod/int8-s16-a1-TTT-checkpoint_baseline-4s/checkpoints',
        # 'load_from_other_directory_step': 4000, # end of warmup
        'num_slice': num_slice,
        'per_device_batch_size': 8,
        'fwd_int8': fwd,
        'dlhs_int8': dlhs,
        'drhs_int8': drhs,
        'clip_by_global_norm': clip_global,
        'clip_by_ucb': clip_by_ucb,
        'learning_rate': 1.e-3 * lr_mul,
        'global_parameter_scale': 16,
        'steps': steps,
        'fwd_int8_pv' : fwd and quant_pv,
        'dlhs_int8_pv' : dlhs and quant_pv,
        'drhs_int8_pv' : drhs and quant_pv,
        'aqt_use_dummy_static_bound': aqt_use_dummy_static_bound,
        'aqt_rng_type': aqt_rng_type,
    }
    if load != "":
        # config['load_from_other_directory'] = f'gs://maxtext-experiments-multipod/int8-s24_prefix-a1-FFF-clip03-ucb0-lr010-clT/checkpoints'
        # config['load_from_other_directory_step'] = 1000
        config['load_from_other_directory'] = f'gs://maxtext-experiments-multipod/{load}/checkpoints'
        config['load_from_other_directory_step'] = load_step
    run_name = f'{bname(fwd)}{bname(dlhs)}{bname(drhs)}-clip{int(clip_global*10):02}-ucb{clip_by_ucb}-lr{int(lr_mul*10):03}-load{bname(load!="")}-ns{num_slice}'
    run_name += f'-rng_{aqt_rng_type[0]}-dummy{bname(aqt_use_dummy_static_bound)}-pv{bname(quant_pv)}'
    run_job(run_name, config)


# Generating from-scratch runs testing FFF vs TTF and ucb vs gc
def run_s24_prefix():
    base_run_s24(fwd=True, dlhs=True, drhs=False, clip_global=0.3, clip_by_ucb=0)
    base_run_s24(fwd=False, dlhs=False, drhs=False, clip_global=0.3, clip_by_ucb=0)
    base_run_s24(fwd=True, dlhs=True, drhs=False, clip_global=0.0, clip_by_ucb=1)
    base_run_s24(fwd=False, dlhs=False, drhs=False, clip_global=0.0, clip_by_ucb=1)


def run_s24_prefix_reload():
    base_run_s24(fwd=True, dlhs=True, drhs=False, clip_global=0.3, clip_by_ucb=0, load="int8-s24_prefix-a1-TTF-clip03-ucb0-lr010-clT", load_step=25000)

# No spikes. Just gc. Try 8 slices
# 8 did not work. It was stopeed. And it spiked anyway. So we need a fresh start.
# def run_s25():
#     base_run_s24(fwd=True, dlhs=True, drhs=False, clip_global=0.3, load=True, num_slice=8, steps=2000)
#     base_run_s24(fwd=False, dlhs=False, drhs=False, clip_global=0.3, load=True, num_slice=8, steps=2000)

def base_run_s26(
        *,
        fwd = False,
        dlhs = False,
        drhs = False,
        fwd_int8_qk = False,
        dlhs_int8_qk = False,
        drhs_int8_qk = False,
        fwd_int8_pv = False,
        dlhs_int8_pv = False,
        drhs_int8_pv = False,
        clip_global = 0.3,
        clip_by_ucb = 0, # 0 or 1
        lr_mul = 1.0,  # This is a small delta to LR, meant as a 'seed' replacement
        load = "",
        load_step = -1,
        num_slice = 8,
        steps = -1,
        global_parameter_scale = 16,
        mlp_bonus = 0,
):
    config = {
        # For seq16
        # 'load_from_other_directory': f'gs://maxtext-experiments-multipod/int8-s18_8B_16seq_warmup-a1-yep/checkpoints',
        # 'load_from_other_directory_step': 1000,
        'save_period': 1000,
        # 'load_from_other_directory': 'gs://maxtext-experiments-multipod/int8-s16-a1-TTT-checkpoint_baseline-4s/checkpoints',
        # 'load_from_other_directory_step': 4000, # end of warmup
        'num_slice': num_slice,
        'per_device_batch_size': 6,
        'fwd_int8': fwd,
        'dlhs_int8': dlhs,
        'drhs_int8': drhs,
        'fwd_int8_qk' : fwd_int8_qk,
        'dlhs_int8_qk' : dlhs_int8_qk,
        'drhs_int8_qk' : drhs_int8_qk,
        'fwd_int8_pv' : fwd_int8_pv,
        'dlhs_int8_pv' : dlhs_int8_pv,
        'drhs_int8_pv' : drhs_int8_pv,
        'clip_by_global_norm': clip_global,
        'clip_by_ucb': clip_by_ucb,
        'learning_rate': 1.e-3 * lr_mul,
        'global_parameter_scale': global_parameter_scale,
        'max_target_length': 2048,
        'steps': steps,
        'fill_ratio': 0.8,
        'global_parameter_scale_mlp_bonus': mlp_bonus,
    }
    assert mlp_bonus % 256 == 0
    if load != "":
        # config['load_from_other_directory'] = f'gs://maxtext-experiments-multipod/int8-s24_prefix-a1-FFF-clip03-ucb0-lr010-clT/checkpoints'
        # config['load_from_other_directory_step'] = 1000
        config['load_from_other_directory'] = f'gs://maxtext-experiments-multipod/{load}/checkpoints'
        config['load_from_other_directory_step'] = load_step
    q = f'{bname(fwd)}{bname(dlhs)}{bname(drhs)}'
    q_qk = f'{bname(fwd_int8_qk)}{bname(dlhs_int8_qk)}{bname(drhs_int8_qk)}'
    q_pv = f'{bname(fwd_int8_pv)}{bname(dlhs_int8_pv)}{bname(drhs_int8_pv)}'
    run_name = f'{global_parameter_scale}B-{q}-qk{q_qk}-pv{q_pv}-bonus{int(mlp_bonus/256)}-clip{int(clip_global*10):02}-ucb{clip_by_ucb}-lr{int(lr_mul*10):03}-load{bname(load!="")}-ns{num_slice}'
    run_job(run_name, config)

# This is supposed to be part of a final (paper) run. Still 16B.
#  - Increase seq len to 2k.
#  - Make the training longer to take fill_ratio into account.
#  - Use 8 slices.
# Results:
#  - these trainings got maybe halfway. I think the processes crashed or something.
def run_s26_prefix():
    base_run_s26(fwd=True, dlhs=True, drhs=False, clip_global=0.3, clip_by_ucb=0)
    base_run_s26(fwd=False, dlhs=False, drhs=False, clip_global=0.3, clip_by_ucb=0)

# This run is testing {fwd, dlhs, drhs} * {PV, QK} quantization on 1B model.
def run_s27():
    kwargs_1 = {
        'num_slice': 1,
        'global_parameter_scale': 1,
    }

    kwargs_2 = {
        'fwd': True,
        'dlhs': True,
        'drhs': False,
    }

    base_run_s26(**kwargs_1, **kwargs_2, fwd_int8_qk = True)
    base_run_s26(**kwargs_1, **kwargs_2, dlhs_int8_qk = True)
    base_run_s26(**kwargs_1, **kwargs_2, drhs_int8_qk = True)
    base_run_s26(**kwargs_1, **kwargs_2, fwd_int8_pv = True)
    base_run_s26(**kwargs_1, **kwargs_2, dlhs_int8_pv = True)
    base_run_s26(**kwargs_1, **kwargs_2, drhs_int8_pv = True)
    base_run_s26(**kwargs_1, **kwargs_2)
    base_run_s26(**kwargs_1, fwd=False, dlhs=False, drhs=False)

# This run is looking for iso-quality on 1B model.
def run_s28():
    kwargs_1 = {
        'num_slice': 1,
        'global_parameter_scale': 1,
    }

    kwargs_2 = {
        'fwd': True,
        'dlhs': True,
        'drhs': False,
    }
    base_run_s26(**kwargs_1, **kwargs_2, mlp_bonus=256*(-1))
    base_run_s26(**kwargs_1, **kwargs_2)
    base_run_s26(**kwargs_1, **kwargs_2, mlp_bonus=256*1)
    base_run_s26(**kwargs_1, **kwargs_2, mlp_bonus=256*2)
    base_run_s26(**kwargs_1, **kwargs_2, mlp_bonus=256*3)


# This is an extension to s24 to see the effect of few more changes on a big model.
# Questions inline.
def run_s24_2():
    # Perf: https://screenshot.googleplex.com/9jRDVdRDCNwTmEH
    # Loss: https://screenshot.googleplex.com/9nRapGgKDP6qgVF

    # Q: check that is identical with s24_prefix
    base_run_s24(steps=200)
    # It wa identical.

    # Q: custom-1 RNG
    base_run_s24(aqt_rng_type='custom-1')
    # custom-1 RNG increases the finall loss loss from 0.005 to 0.008 and speeds up only 1.8%

    # Q: Quality and pref of quant_pv?
    base_run_s24(quant_pv=True)
    # Good quality, no-op on perf, but adds optimization opportunity.

    # Q: Perf cost of calibration?
    base_run_s24(aqt_use_dummy_static_bound=True, steps=200)
    # Not calibrating at all speeds up ONLY 1.7% (shock for me).


# Run to debug performance.
def run_s29():
    def kwargs(quant, num_slice, aqt_use_dummy_static_bound):
        return {
            "fwd": quant,
            "dlhs": quant,
            "drhs": False,
            "num_slice": num_slice,
            "steps": 10,
            "quant_pv": True,
            "aqt_use_dummy_static_bound": aqt_use_dummy_static_bound,
        }
    base_run_s24(**kwargs(False, 2, False))
    base_run_s24(**kwargs(True, 2, False))
    base_run_s24(**kwargs(True, 2, True))
    base_run_s24(**kwargs(False, 1, False))
    base_run_s24(**kwargs(True, 1, False))
    base_run_s24(**kwargs(True, 1, True))



# This is 16B run searching for good stability.
def run_s30():
    # (GBS = 8B)
    baseline = dict(
        num_slice = 4,
        per_device_batch_size = 4,
        fwd_int8 = True,
        dlhs_int8 = True,
        drhs_int8 = False,
        clip_by_global_norm = 1.0,
        learning_rate = 3.0e-4,
        global_parameter_scale = 16,
        fwd_int8_pv = True,
        dlhs_int8_pv = True,
        drhs_int8_pv = False,
        max_target_length = 2048,
        adam_weight_decay = 0.1,
        aqt_use_fwd_quant = False,
    )
    run_job("baseline", baseline)
    run_job("lr_2p0", baseline, learning_rate=6.0e-4)
    run_job("lr_0p5", baseline, learning_rate=1.5e-4)
    run_job("gbs_0p5", baseline, per_device_batch_size=2)
    run_job("gbs_1p5", baseline, per_device_batch_size=6)
    run_job("ns_0p5", baseline, num_slice=2)
    run_job("ns_1p5", baseline, num_slice=6)
    run_job("seq_0p5", baseline, max_target_length=1024)
    run_job("wd_0p3", baseline, adam_weight_decay=0.03)
    run_job("wd_3p0", baseline, adam_weight_decay=0.3)
    run_job("gc_0p1", baseline, clip_by_global_norm=0.1)
    run_job("pv_F", baseline, fwd_int8_pv=False, dlhs_int8_pv=False)
    run_job("fwdq_T", baseline, aqt_use_fwd_quant=True)


def baseline_s31(quant):
    return dict(
        global_parameter_scale = 16,

        num_slice = 8,
        max_target_length = 2048,
        per_device_batch_size = 4,

        learning_rate = 10.0e-4,
        adam_weight_decay = 0.1,
        clip_by_global_norm = 0.1,
        fill_ratio = 0.8 / 1.5,  # divide by 1.5 to make the training longer. Maybe see another drop in loss curev

        fwd_int8 = quant,
        dlhs_int8 = quant,
        drhs_int8 = False,
        fwd_int8_pv = quant,
        dlhs_int8_pv = quant,
        drhs_int8_pv = False,

        aqt_use_fwd_quant = False,
    )

def run_s31():
    # WARNING: warmup is 3%, which is 600 and 1200 steps on ns16 and ns8 respecively.
    # Summary:
    # https://screenshot.googleplex.com/7v68TWqBMFYQMk4
    # Speedup is around 19.5%  FFF vs TTF
    run_job("q_TTF", baseline_s31(True))
    run_job("q_FFF", baseline_s31(False))
    run_job("q_TTF-ns_16-lr_20", baseline_s31(True), num_slice=16, learning_rate=20.0e-4)
    run_job("q_TTF-gc0p01", baseline_s31(True), clip_by_global_norm=0.01)


def run_s31_2():
    # Retry 16 pods on different LR, on tuned clipping and slower warmup
    common = dict(
        fill_ratio = 0.8,
        num_slice=16,
    )
    # https://screenshot.googleplex.com/7U6T3cjrQokZEx6
    # https://screenshot.googleplex.com/AjtnDbindvMSiqP
    # We will continue with gc_0p10 and lr_05
    run_job("q_TTF-ns_16-lr_20", baseline_s31(True), **common, learning_rate=20.0e-4) # not a repro of s31. 3x longer warmup!
    run_job("q_TTF-ns_16-lr_10-gc_0p05", baseline_s31(True), **common, learning_rate=10.0e-4, clip_by_global_norm=0.05)
    run_job("q_TTF-ns_16-lr_05-gc_0p05", baseline_s31(True), **common, learning_rate=05.0e-4, clip_by_global_norm=0.05)


def baseline_s32():
    return dict(
        global_parameter_scale = 16,

        num_slice = 16,
        max_target_length = 2048,
        per_device_batch_size = 4,

        learning_rate = 5.0e-4,
        adam_b1 = 0.9,
        adam_b2 = 0.95,
        adam_weight_decay = 0.1,
        clip_by_global_norm = 0.1,
        fill_ratio = 0.8,

        int8_training = True,

        fwd_int8 = True,
        dlhs_int8 = True,
        drhs_int8 = False,

        fwd_int8_pv = True,
        dlhs_int8_pv = True,
        drhs_int8_pv = False,

        fwd_int8_qk = False,
        dlhs_int8_qk = False,
        drhs_int8_qk = False,

        fwd_int8_logits = False,
        dlhs_int8_logits = False,
        drhs_int8_logits = False,


        aqt_use_fwd_quant = False,
        data_shuffle_seed = 0,
        init_weights_seed = 0,
        load_from_other_directory = "",
        load_from_other_directory_step = -1,
    )

############################### FINAL runs start here

# Paper 16B
def run_s32():
    run_job("q_FFF", baseline_s32(), int8_training=False)
    run_job("q_TTF", baseline_s32())
    run_job("q_TTT", baseline_s32(), drhs_int8=True)


# Paper 8B
def run_s33():
    common = dict(
        num_slice=8,
        global_parameter_scale = 8,
    )

    run_job("q_FFF_s8_ns8", baseline_s32(), int8_training=False, **common)
    run_job("q_TTF_s8_ns8", baseline_s32(), **common)
    run_job("q_TTT_s8_ns8", baseline_s32(), **common, drhs_int8=True)

# This is S33 put with 16 slices, we adjust adam betas and LR to be close
# R: This did not finish and I will not try again.
# def run_s34():
#     import numpy as np
#     s = 2
#     num_slice = 8*s
#     ps = 8
#     common = dict(
#         num_slice=num_slice,
#         global_parameter_scale = ps,
#         adam_b1=0.9 ** s,
#         adam_b2=0.95 ** s,
#         learning_rate = 5.0e-4 * s,
#     )
#     run_job(f"q_TTF_ps{ps}_ns{num_slice}", baseline_s32(), **common)
#     run_job(f"q_FFF_ps{ps}_ns{num_slice}", baseline_s32(), int8_training=False, **common)

# Paper 4B, 2B, 1B
def run_s35():
    def common(gps):
        return  dict(
            num_slice=8,
            global_parameter_scale=gps,
        )
    # 18, 50, 3.5h
    for s in [1, 2, 4]:
        run_job(f"q_FFF_s{s}_ns8", baseline_s32(), int8_training=False, **common(s))
        run_job(f"q_TTF_s{s}_ns8", baseline_s32(), **common(s))
        run_job(f"q_TTT_s{s}_ns8", baseline_s32(), **common(s), drhs_int8=True)


################ ABLATION RUNS ########################

def ablation(gps):
    cfg = update_yaml_fields(baseline_s32(), dict(
        global_parameter_scale = gps,
        num_slice = 2,
    ))
    return cfg

# Init seed and data seed
def run_s36(): # 20
    for s in range(1,6):
        run_job(f"gps_1-dseed_{s}", ablation(gps=1), data_shuffle_seed = s)
        run_job(f"gps_1-wseed_{s}", ablation(gps=1), init_weights_seed = s)


def run_s37(): # 20
    run_job(f"gps_{4}-q_FFF", ablation(gps=4), int8_training=False)
    for gps in [1, 2]:
        run_job(f"gps_{gps}-q_FFF", ablation(gps=gps), int8_training=False)
        run_job(f"gps_{gps}-q_TTF", ablation(gps=gps), int8_training=True)
        run_job(f"gps_{gps}-q_TTT", ablation(gps=gps), drhs_int8=True)
        run_job(
            f"gps_{gps}-qk_TTF",
            ablation(gps=gps),
            fwd_int8_qk = True,
            dlhs_int8_qk = True,
            drhs_int8_qk = False,
        )
        logits = dict(
            fwd_int8_logits = True,
            dlhs_int8_logits = True,
            drhs_int8_logits = False,
        )
        run_job(f"gps_{gps}-fwdq_T", ablation(gps=gps), aqt_use_fwd_quant=True)
        run_job(f"gps_{gps}-logits_T", ablation(gps=gps), **logits)

# Long training
def run_s38(): # 32
    # want 16000 steps on 16 slices
    run_job(f"long-FFF", ablation(gps=1), num_slice=16, fill_ratio=0.8 / 16 /1.20  , int8_training=False)
    run_job(f"long-TTF", ablation(gps=1), num_slice=16, fill_ratio=0.8 / 16 /1.20  , int8_training=True)


def baseline_s39(*, gps, ns, load_dir, load_step):
    if load_dir == "":
        assert load_step == -1
    else:
        load_dir = f'gs://maxtext-experiments-multipod/{load_dir}/checkpoints'
        assert load_step > 0
    d = dict(
        num_slice=ns,
        global_parameter_scale=gps,

        max_target_length = 2048,
        per_device_batch_size = 4,

        learning_rate = 5.0e-4,
        adam_b1 = 0.9,
        adam_b2 = 0.95,
        adam_weight_decay = 0.1,
        clip_by_global_norm = 0.1,
        fill_ratio = 0.8,

        int8_training = True,

        fwd_int8 = True,
        dlhs_int8 = True,
        drhs_int8 = False,

        fwd_int8_pv = True,
        dlhs_int8_pv = True,
        drhs_int8_pv = False,

        fwd_int8_qk = False,
        dlhs_int8_qk = False,
        drhs_int8_qk = False,

        fwd_int8_logits = True,
        dlhs_int8_logits = True,
        drhs_int8_logits = False,


        aqt_use_fwd_quant = True,
        data_shuffle_seed = 0,
        init_weights_seed = 0,
        load_from_other_directory = load_dir,
        load_from_other_directory_step = load_step,
    )
    return d


# repeat with logits and use_fwd_quant
def run_s39():
    # We do want the checkpoint. We have for ns8, ns2
    for gps, ns, load_dir, load_step, in [
        (1, 2, 'int8-s37-a1-gps_1-q_FFF', 500),    #  1h, 6400
        (2, 2, 'int8-s37-a1-gps_2-q_FFF', 1000),   #  4h, 12800
        # (4, 2, 'int8-s37-a1-gps_2-q_FFF', 2000),   # 16h, 25600, no data
        (1, 8, 'int8-s35-a1-q_FFF_s1_ns8', 500),   # .2h, 1600
        (2, 8, 'int8-s35-a1-q_FFF_s2_ns8', 500),   #  1h, 3200
        (4, 8, 'int8-s35-a1-q_FFF_s4_ns8', 1000),  #  4h, 6400
        (8, 8, 'int8-s33-a5-q_FFF_s8_ns8', 1500),  # 16h, 12800
        (16, 16, 'int8-s32-a1-q_FFF', 1500),       # 32h, 12800

        (1, 8, '', -1),   # .2h, 1600
        (2, 8, '', -1),   #  1h, 3200
        (4, 8, '', -1),  #  4h, 6400
        (8, 8, '', -1),  # 16h, 12800
        (16, 16, '', -1),       # 32h, 12800
    ]:
        # No need to rereun this, because it does not changes
        base = baseline_s39(gps=gps, ns=ns, load_dir=load_dir, load_step=load_step)
        real_ttt = dict(
            drhs_int8 = True,
            drhs_int8_pv = True,
            drhs_int8_logits = True,
        )
        qk = dict(
            fwd_int8_qk = True,
            dlhs_int8_qk = True,
        )
        logits_F = dict(
            fwd_int8_logits = False,
            dlhs_int8_logits = False,
            drhs_int8_logits = False,
        )
        # a hack to get nicer job names.
        if load_step == -1:
            load_step = "_F"
        # This is our main guy
        run_job(f"gps{gps}-ns{ns}-load{load_step}-TTF-fwdq_F", base, aqt_use_fwd_quant=False)

        # We don't want ablations from scratch, too much noise.
        if load_dir != "":
            # This was our main guy, but is subpar now.
            run_job(f"gps{gps}-ns{ns}-load{load_step}-TTF", base)

            if ns == 2:
                # We don't need this one because supposedly we have it. But I want at least one sanity check:
                run_job(f"gps{gps}-ns{ns}-load{load_step}-FFF", base, int8_training=False)
                # Show ablations only on 2-pod configs.
                run_job(f"gps{gps}-ns{ns}-load{load_step}-TTT", base, **real_ttt)
                run_job(f"gps{gps}-ns{ns}-load{load_step}-TTF-qk_T", base, **qk)
                run_job(f"gps{gps}-ns{ns}-load{load_step}-TTF-logits_F", base, **logits_F)


# TODO:
#  - per-tensor-scale (*6),
#  - accum,
#  - SR scaled

def main():
    import argparse
    parser = argparse.ArgumentParser(description='TPU configuration options')
    parser.add_argument('--dryrun', type=bool, default=True, action=argparse.BooleanOptionalAction)
    parser.add_argument('--delyml', type=bool, default=False, action=argparse.BooleanOptionalAction)
    parser.add_argument('--stable', type=bool, default=True, action=argparse.BooleanOptionalAction)
    parser.add_argument('--tpu', type=str, default='v5')
    parser.add_argument('--jobre', type=str, default='.*')
    parser.add_argument('--sweep', type=str, default='')
    parser.add_argument('--attempt', type=str, default='')
    parser.add_argument('--jax_14_cl', type=bool, default=True, action=argparse.BooleanOptionalAction)
    pargs = parser.parse_args()
    global args
    args = pargs.__dict__
    sweep_name = args['sweep']
    attempt = args['attempt']

    sweep_fn_name = f'run_{sweep_name}'
    assert sweep_fn_name in globals(), f'{sweep_fn_name}() not defined.'
    assert attempt != ''
    sweep_fn = globals()[sweep_fn_name]
    sweep_fn()


main()
