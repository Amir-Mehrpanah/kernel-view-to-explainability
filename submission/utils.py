from datetime import datetime
from itertools import product
import os
import submitit
import pandas as pd
from src import paths
from submission import training, grads, quant
from src.utils import get_experiment_prefix, get_save_path


def submit_training(
    *,
    block_main,
    port,
    timeout,
    batch_size,
    warmup_epochs_ratio,
    **args,
):
    now = datetime.now().strftime("%Y%m%d-%H")
    args = pd.DataFrame(
        list(
            product(
                *args.values(),
            )
        ),
        columns=args.keys(),
    )
    args["tb_postfix"] = args.apply(
        lambda x: get_experiment_prefix(**x),
        axis=1,
    )
    args["checkpoint_path"] = args.apply(
        lambda x: get_save_path(
            **x,
        ),
        axis=1,
    )

    checkpoint_exists = args["checkpoint_path"].apply(lambda x: os.path.exists(x))
    print("Checkpoints skipped because they do already exist")
    args[checkpoint_exists]["checkpoint_path"].apply(print)

    valid_args = args[~checkpoint_exists]
    valid_args["port"] = port
    valid_args["block_main"] = block_main
    valid_args["timeout"] = timeout
    valid_args["batch_size"] = valid_args["activation"].map(batch_size)
    valid_args["warmup_epochs"] = (valid_args["epochs"] * warmup_epochs_ratio).astype(int)

    return execute_job_submission(block_main, port, timeout, valid_args, training.main)


def submit_grads(
    *,
    block_main,
    port,
    timeout,
    batch_size,
    **args,
):
    print(f"time: {datetime.now()}")
    args = pd.DataFrame(
        list(
            product(
                *args.values(),
            )
        ),
        columns=args.keys(),
    )

    args["port"] = port
    args["block_main"] = block_main
    args["timeout"] = timeout
    args["batch_size"] = args["activation"].map(batch_size)
    args["experiment_prefix"] = args.apply(
        lambda x: get_experiment_prefix(**x),
        axis=1,
    )
    args["experiment_output_dir"] = args.apply(
        lambda x: os.path.join(
            paths.LOCAL_OUTPUT_DIR,
            x.experiment_prefix,
        ),
        axis=1,
    )
    args["checkpoint_path"] = args.apply(
        lambda x: get_save_path(
            **x,
        ),
        axis=1,
    )
    output_dir_exists = args["experiment_output_dir"].apply(lambda x: os.path.exists(x))
    checkpoint_exists = args["checkpoint_path"].apply(lambda x: os.path.exists(x))
    valid_ids = checkpoint_exists & ~output_dir_exists
    valid_args = args[valid_ids]

    print("Checkpoints skipped:")
    args[~checkpoint_exists]["checkpoint_path"].apply(print)
    print("Output dirs skipped:")
    args[output_dir_exists]["experiment_output_dir"].apply(print)
    print("Valid args:")
    args[valid_ids]["experiment_output_dir"].apply(print)

    return execute_job_submission(block_main, port, timeout, valid_args, grads.main)


def submit_measurements(
    *,
    block_main,
    port,
    timeout,
    **args,
):
    print(f"time: {datetime.now()}")
    args = pd.DataFrame(
        list(
            product(
                *args.values(),
            )
        ),
        columns=args.keys(),
    )

    args["port"] = port
    args["block_main"] = block_main
    args["timeout"] = timeout

    return execute_job_submission(block_main, port, timeout, args, quant.main)


def execute_job_submission(block_main, port, timeout, args, func):
    jobs_args = args.to_dict(orient="records")

    repr_args = args.copy()
    repr_args = repr_args.map(str)
    nunique = repr_args.nunique()
    print(nunique)
    print("total num of jobs", len(args))

    if port != None:
        print("Running only the first job because of the debug flag")
        jobs_args = [jobs_args[0]]
    if len(args) == 0:
        print("No jobs to run exiting")
        return
    print("Do you want to continue? [y/n]")
    if input() != "y":
        print("Aborted")
        return
    print("submitting jobs")
    executor = submitit.AutoExecutor(folder="logs/%j")
    executor.update_parameters(
        timeout_min=timeout,
        cpus_per_task=8,
        slurm_additional_parameters={
            "constraint": "thin",
            "reservation": "safe",
            "gpus": 1,
        },
    )

    if port == 0:
        print("Running in locally")
        func(jobs_args)
    else:
        jobs = executor.map_array(func, jobs_args)
        print("Job submitted")
        # wait until the job has finished
        if block_main:
            print("Waiting for job to finish")
            results = [job.result() for job in jobs]
            print("All jobs finished")
            return results
