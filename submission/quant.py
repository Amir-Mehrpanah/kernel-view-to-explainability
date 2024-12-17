import pandas as pd
import submitit
import os
import sys
import debugpy

# vscode changes the cwd to the file's directory, so we need to add the workspace to the path
# Set the working directory to the base of the workspace
workspace_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(workspace_dir)
sys.path.insert(0, workspace_dir)

from src import paths
from src.datasets import (
    extract_the_dataset_on_compute_node,
    move_data_to_compute_node,
    resolve_data_directories,
)
from src.utils import determine_device
from src import quant_measures_grads


def main(args):
    print(args)

    if args["port"] is not None and args["port"] > 0:
        job_env = submitit.JobEnvironment()
        print(f"Debugger is running on node {job_env.hostname} port {args['port']}")
        debugpy.listen((job_env.hostname, args["port"]))
        print("Waiting for debugger attach")
        debugpy.wait_for_client()

    determine_device(args)

    (
        DATA_DIR,
        COMPUTE_DATA_DIR,
        EXT,
        COMPUTE_DATA_DIR_BASE_DIR,
        TARGET_DIR,
        COMPUTE_OUTPUT_DIR,
        LOCAL_OUTPUT_DIR,
    ) = resolve_data_directories(args)

    os.system("module load Fpart/1.5.1-gcc-8.5.0")

    move_data_to_compute_node(LOCAL_OUTPUT_DIR, EXT == "tgz", COMPUTE_DATA_DIR)

    extract_the_dataset_on_compute_node(COMPUTE_DATA_DIR, EXT, TARGET_DIR)

    print("Running main job...")
    print(f"Data is in {COMPUTE_DATA_DIR_BASE_DIR}")
    results = quant_measures_grads.main(
        root_path=COMPUTE_DATA_DIR,
        **args,
    )

    os.makedirs(paths.LOCAL_QUANTS_DIR, exist_ok=True)
    pd.DataFrame(results).to_csv(
        os.path.join(
            paths.LOCAL_QUANTS_DIR,
            "quants.csv",
        ),
        index=False,
    )
