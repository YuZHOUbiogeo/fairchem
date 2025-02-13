from __future__ import annotations

import collections.abc
import glob
import os
import tempfile
from pathlib import Path

import numpy as np
import numpy.testing as npt
import pytest
import yaml
from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

from fairchem.core._cli import Runner
from fairchem.core.common.flags import flags
from fairchem.core.common.test_utils import (
    PGConfig,
    init_env_rank_and_launch_test,
    spawn_multi_process,
)
from fairchem.core.common.utils import build_config, setup_logging
from fairchem.core.scripts.make_lmdb_sizes import get_lmdb_sizes_parser, make_lmdb_sizes

setup_logging()


@pytest.fixture()
def configs():
    return {
        "scn": Path("tests/core/models/test_configs/test_scn.yml"),
        "escn": Path("tests/core/models/test_configs/test_escn.yml"),
        "escn_hydra": Path("tests/core/models/test_configs/test_escn_hydra.yml"),
        "schnet": Path("tests/core/models/test_configs/test_schnet.yml"),
        "gemnet_dt": Path("tests/core/models/test_configs/test_gemnet_dt.yml"),
        "gemnet_dt_hydra": Path(
            "tests/core/models/test_configs/test_gemnet_dt_hydra.yml"
        ),
        "gemnet_dt_hydra_grad": Path(
            "tests/core/models/test_configs/test_gemnet_dt_hydra_grad.yml"
        ),
        "gemnet_oc": Path("tests/core/models/test_configs/test_gemnet_oc.yml"),
        "gemnet_oc_hydra": Path(
            "tests/core/models/test_configs/test_gemnet_oc_hydra.yml"
        ),
        "gemnet_oc_hydra_grad": Path(
            "tests/core/models/test_configs/test_gemnet_oc_hydra_grad.yml"
        ),
        "dimenet++": Path("tests/core/models/test_configs/test_dpp.yml"),
        "dimenet++_hydra": Path("tests/core/models/test_configs/test_dpp_hydra.yml"),
        "painn": Path("tests/core/models/test_configs/test_painn.yml"),
        "painn_hydra": Path("tests/core/models/test_configs/test_painn_hydra.yml"),
        "equiformer_v2": Path("tests/core/models/test_configs/test_equiformerv2.yml"),
        "equiformer_v2_hydra": Path(
            "tests/core/models/test_configs/test_equiformerv2_hydra.yml"
        ),
    }


@pytest.fixture()
def tutorial_train_src(tutorial_dataset_path):
    return tutorial_dataset_path / "s2ef/train_100"


@pytest.fixture()
def tutorial_val_src(tutorial_dataset_path):
    return tutorial_dataset_path / "s2ef/val_20"


def oc20_lmdb_train_and_val_from_paths(
    train_src, val_src, test_src=None, otf_norms=False
):
    datasets = {}
    if train_src is not None:
        datasets["train"] = {
            "src": train_src,
            "format": "lmdb",
            "key_mapping": {"y": "energy", "force": "forces"},
        }
        if otf_norms is True:
            datasets["train"].update(
                {
                    "transforms": {
                        "element_references": {
                            "fit": {
                                "targets": ["energy"],
                                "batch_size": 4,
                                "num_batches": 10,
                                "driver": "gelsd",
                            }
                        },
                        "normalizer": {
                            "fit": {
                                "targets": {"energy": None, "forces": {"mean": 0.0}},
                                "batch_size": 4,
                                "num_batches": 10,
                            }
                        },
                    }
                }
            )
        else:
            datasets["train"].update(
                {
                    "transforms": {
                        "normalizer": {
                            "energy": {
                                "mean": -0.7554450631141663,
                                "stdev": 2.887317180633545,
                            },
                            "forces": {"mean": 0.0, "stdev": 2.887317180633545},
                        }
                    }
                }
            )
    if val_src is not None:
        datasets["val"] = {"src": val_src, "format": "lmdb"}
    if test_src is not None:
        datasets["test"] = {"src": test_src, "format": "lmdb"}
    return datasets


def get_tensorboard_log_files(logdir):
    return glob.glob(f"{logdir}/tensorboard/*/events.out*")


def get_tensorboard_log_values(logdir):
    tf_event_files = get_tensorboard_log_files(logdir)
    assert len(tf_event_files) == 1
    tf_event_file = tf_event_files[0]
    acc = EventAccumulator(tf_event_file)
    acc.Reload()
    return acc


def merge_dictionary(d, u):
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            d[k] = merge_dictionary(d.get(k, {}), v)
        else:
            d[k] = v
    return d


def _run_main(
    rundir,
    input_yaml,
    update_dict_with=None,
    update_run_args_with=None,
    save_checkpoint_to=None,
    save_predictions_to=None,
    world_size=0,
):
    config_yaml = Path(rundir) / "train_and_val_on_val.yml"

    with open(input_yaml) as yaml_file:
        yaml_config = yaml.safe_load(yaml_file)
    if update_dict_with is not None:
        yaml_config = merge_dictionary(yaml_config, update_dict_with)
        yaml_config["backend"] = "gloo"
    with open(str(config_yaml), "w") as yaml_file:
        yaml.dump(yaml_config, yaml_file)
    run_args = {
        "run_dir": rundir,
        "logdir": f"{rundir}/logs",
        "config_yml": config_yaml,
    }
    if update_run_args_with is not None:
        run_args.update(update_run_args_with)

    # run
    parser = flags.get_parser()
    args, override_args = parser.parse_known_args(
        ["--mode", "train", "--seed", "100", "--config-yml", "config.yml", "--cpu"]
    )
    for arg_name, arg_value in run_args.items():
        setattr(args, arg_name, arg_value)
    config = build_config(args, override_args)

    if world_size > 0:
        pg_config = PGConfig(
            backend="gloo", world_size=world_size, gp_group_size=1, use_gp=False
        )
        spawn_multi_process(
            pg_config,
            Runner(distributed=True),
            init_env_rank_and_launch_test,
            config,
        )
    else:
        Runner()(config)

    if save_checkpoint_to is not None:
        checkpoints = glob.glob(f"{rundir}/checkpoints/*/checkpoint.pt")
        assert len(checkpoints) == 1
        os.rename(checkpoints[0], save_checkpoint_to)
    if save_predictions_to is not None:
        predictions_filenames = glob.glob(f"{rundir}/results/*/s2ef_predictions.npz")
        assert len(predictions_filenames) == 1
        os.rename(predictions_filenames[0], save_predictions_to)
    return get_tensorboard_log_values(
        f"{rundir}/logs",
    )


"""
These tests are intended to be as quick as possible and test only that the network is runnable and outputs training+validation to tensorboard output
These should catch errors such as shape mismatches or otherways to code wise break a network
"""


class TestSmoke:
    def smoke_test_train(self, input_yaml, tutorial_val_src, otf_norms=False):
        with tempfile.TemporaryDirectory() as tempdirname:
            # first train a very simple model, checkpoint
            train_rundir = Path(tempdirname) / "train"
            train_rundir.mkdir()
            checkpoint_path = str(train_rundir / "checkpoint.pt")
            training_predictions_filename = str(train_rundir / "train_predictions.npz")
            acc = _run_main(
                rundir=str(train_rundir),
                input_yaml=input_yaml,
                update_dict_with={
                    "optim": {"max_epochs": 2, "eval_every": 8, "batch_size": 5},
                    "dataset": oc20_lmdb_train_and_val_from_paths(
                        train_src=str(tutorial_val_src),
                        val_src=str(tutorial_val_src),
                        test_src=str(tutorial_val_src),
                        otf_norms=otf_norms,
                    ),
                },
                save_checkpoint_to=checkpoint_path,
                save_predictions_to=training_predictions_filename,
            )
            assert "train/energy_mae" in acc.Tags()["scalars"]
            assert "val/energy_mae" in acc.Tags()["scalars"]

            # second load the checkpoint and predict
            predictions_rundir = Path(tempdirname) / "predict"
            predictions_rundir.mkdir()
            predictions_filename = str(predictions_rundir / "predictions.npz")
            _run_main(
                rundir=str(predictions_rundir),
                input_yaml=input_yaml,
                update_dict_with={
                    "optim": {"max_epochs": 2, "eval_every": 8, "batch_size": 5},
                    "dataset": oc20_lmdb_train_and_val_from_paths(
                        train_src=str(tutorial_val_src),
                        val_src=str(tutorial_val_src),
                        test_src=str(tutorial_val_src),
                        otf_norms=otf_norms,
                    ),
                },
                update_run_args_with={
                    "mode": "predict",
                    "checkpoint": checkpoint_path,
                },
                save_predictions_to=predictions_filename,
            )

            if otf_norms is True:
                norm_path = glob.glob(
                    str(train_rundir / "checkpoints" / "*" / "normalizers.pt")
                )
                assert len(norm_path) == 1
                assert os.path.isfile(norm_path[0])
                ref_path = glob.glob(
                    str(train_rundir / "checkpoints" / "*" / "element_references.pt")
                )
                assert len(ref_path) == 1
                assert os.path.isfile(ref_path[0])

            # verify predictions from train and predict are identical
            energy_from_train = np.load(training_predictions_filename)["energy"]
            energy_from_checkpoint = np.load(predictions_filename)["energy"]
            npt.assert_allclose(
                energy_from_train, energy_from_checkpoint, rtol=1e-6, atol=1e-6
            )

    # not all models are tested with otf normalization estimation
    # only gemnet_oc, escn, equiformer, and their hydra versions
    @pytest.mark.parametrize(
        ("model_name", "otf_norms"),
        [
            ("schnet", False),
            ("scn", False),
            ("gemnet_dt", False),
            ("gemnet_dt_hydra", False),
            ("gemnet_dt_hydra_grad", False),
            ("gemnet_oc", False),
            ("gemnet_oc", True),
            ("gemnet_oc_hydra", False),
            ("gemnet_oc_hydra", True),
            ("gemnet_oc_hydra_grad", False),
            ("dimenet++", False),
            ("dimenet++_hydra", False),
            ("painn", False),
            ("painn_hydra", False),
            ("escn", False),
            ("escn", True),
            ("escn_hydra", False),
            ("escn_hydra", True),
            ("equiformer_v2", False),
            ("equiformer_v2", True),
            ("equiformer_v2_hydra", False),
            ("equiformer_v2_hydra", True),
        ],
    )
    def test_train_and_predict(
        self,
        model_name,
        otf_norms,
        configs,
        tutorial_val_src,
    ):
        self.smoke_test_train(
            input_yaml=configs[model_name],
            tutorial_val_src=tutorial_val_src,
            otf_norms=otf_norms,
        )

    def test_use_pbc_single(self, configs, tutorial_val_src, torch_deterministic):
        with tempfile.TemporaryDirectory() as tempdirname:
            tempdir = Path(tempdirname)
            extra_args = {"seed": 0}
            _ = _run_main(
                rundir=str(tempdir),
                update_dict_with={
                    "optim": {"max_epochs": 1},
                    "model": {"use_pbc_single": True},
                    "dataset": oc20_lmdb_train_and_val_from_paths(
                        train_src=str(tutorial_val_src),
                        val_src=str(tutorial_val_src),
                        test_src=str(tutorial_val_src),
                    ),
                },
                update_run_args_with=extra_args,
                input_yaml=configs["equiformer_v2"],
            )

    @pytest.mark.parametrize(
        ("world_size", "ddp"),
        [
            pytest.param(2, True),
            pytest.param(0, False),
        ],
    )
    def test_ddp(self, world_size, ddp, configs, tutorial_val_src, torch_deterministic):
        with tempfile.TemporaryDirectory() as tempdirname:
            tempdir = Path(tempdirname)
            extra_args = {"seed": 0}
            if not ddp:
                extra_args["no_ddp"] = True
            _ = _run_main(
                rundir=str(tempdir),
                update_dict_with={
                    "optim": {"max_epochs": 1},
                    "dataset": oc20_lmdb_train_and_val_from_paths(
                        train_src=str(tutorial_val_src),
                        val_src=str(tutorial_val_src),
                        test_src=str(tutorial_val_src),
                    ),
                },
                update_run_args_with=extra_args,
                input_yaml=configs["equiformer_v2"],
                world_size=world_size,
            )

    @pytest.mark.parametrize(
        ("world_size", "ddp"),
        [
            pytest.param(2, True),
            pytest.param(0, False),
        ],
    )
    def test_balanced_batch_sampler_ddp(
        self, world_size, ddp, configs, tutorial_val_src, torch_deterministic
    ):
        # make dataset metadata
        parser = get_lmdb_sizes_parser()
        args, override_args = parser.parse_known_args(
            ["--data-path", str(tutorial_val_src)]
        )
        make_lmdb_sizes(args)

        with tempfile.TemporaryDirectory() as tempdirname:
            tempdir = Path(tempdirname)
            extra_args = {"seed": 0}
            if not ddp:
                extra_args["no_ddp"] = True
            _ = _run_main(
                rundir=str(tempdir),
                update_dict_with={
                    "optim": {"max_epochs": 1, "load_balancing": "atoms"},
                    "dataset": oc20_lmdb_train_and_val_from_paths(
                        train_src=str(tutorial_val_src),
                        val_src=str(tutorial_val_src),
                        test_src=str(tutorial_val_src),
                    ),
                },
                update_run_args_with=extra_args,
                input_yaml=configs["equiformer_v2"],
                world_size=world_size,
            )

    # train for a few steps and confirm same seeds get same results
    def test_different_seeds(
        self,
        configs,
        tutorial_val_src,
        torch_deterministic,
    ):
        with tempfile.TemporaryDirectory() as tempdirname:
            tempdir = Path(tempdirname)

            seed0_take1_rundir = tempdir / "seed0take1"
            seed0_take1_rundir.mkdir()
            seed0_take1_acc = _run_main(
                rundir=str(seed0_take1_rundir),
                update_dict_with={
                    "optim": {"max_epochs": 2},
                    "dataset": oc20_lmdb_train_and_val_from_paths(
                        train_src=str(tutorial_val_src),
                        val_src=str(tutorial_val_src),
                        test_src=str(tutorial_val_src),
                    ),
                },
                update_run_args_with={"seed": 0},
                input_yaml=configs["escn"],
            )

            seed1000_rundir = tempdir / "seed1000"
            seed1000_rundir.mkdir()
            seed1000_acc = _run_main(
                rundir=str(seed1000_rundir),
                update_dict_with={
                    "optim": {"max_epochs": 2},
                    "dataset": oc20_lmdb_train_and_val_from_paths(
                        train_src=str(tutorial_val_src),
                        val_src=str(tutorial_val_src),
                        test_src=str(tutorial_val_src),
                    ),
                },
                update_run_args_with={"seed": 1000},
                input_yaml=configs["escn"],
            )

            seed0_take2_rundir = tempdir / "seed0_take2"
            seed0_take2_rundir.mkdir()
            seed0_take2_acc = _run_main(
                rundir=str(seed0_take2_rundir),
                update_dict_with={
                    "optim": {"max_epochs": 2},
                    "dataset": oc20_lmdb_train_and_val_from_paths(
                        train_src=str(tutorial_val_src),
                        val_src=str(tutorial_val_src),
                        test_src=str(tutorial_val_src),
                    ),
                },
                update_run_args_with={"seed": 0},
                input_yaml=configs["escn"],
            )

            assert not np.isclose(
                seed0_take1_acc.Scalars("train/energy_mae")[-1].value,
                seed1000_acc.Scalars("train/energy_mae")[-1].value,
            )
            assert np.isclose(
                seed0_take1_acc.Scalars("train/energy_mae")[-1].value,
                seed0_take2_acc.Scalars("train/energy_mae")[-1].value,
            )


"""
These tests intend to test if optimization is not obviously broken on a time scale of a few minutes
"""


class TestSmallDatasetOptim:
    @pytest.mark.parametrize(
        ("model_name", "expected_energy_mae", "expected_force_mae"),
        [
            pytest.param("gemnet_oc", 0.41, 0.06, id="gemnet_oc"),
            pytest.param("escn", 0.41, 0.06, id="escn"),
            pytest.param("equiformer_v2", 0.41, 0.06, id="equiformer_v2"),
        ],
    )
    def test_train_optimization(
        self,
        model_name,
        expected_energy_mae,
        expected_force_mae,
        configs,
        tutorial_val_src,
        torch_deterministic,
    ):
        with tempfile.TemporaryDirectory() as tempdirname:
            acc = _run_main(
                rundir=tempdirname,
                input_yaml=configs[model_name],
                update_dict_with={
                    "dataset": oc20_lmdb_train_and_val_from_paths(
                        train_src=str(tutorial_val_src),
                        val_src=str(tutorial_val_src),
                    ),
                },
            )
            assert acc.Scalars("train/energy_mae")[-1].value < expected_energy_mae
            assert acc.Scalars("train/forces_mae")[-1].value < expected_force_mae
