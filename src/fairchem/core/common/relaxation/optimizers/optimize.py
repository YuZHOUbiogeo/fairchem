"""
Copyright (c) Meta, Inc. and its affiliates.

This source code is licensed under the MIT license found in the
LICENSE file in the root directory of this source tree.

Code based on ase.optimize
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import torch
from ase.calculators.calculator import PropertyNotImplementedError
from ase.optimize.optimize import Optimizable

if TYPE_CHECKING:
    from numpy.typing import NDArray
    from torch_geometric.data import Batch
    from fairchem.core.trainers import BaseTrainer


ALL_CHANGES = [
    "pos",
    "atomic_numbers",
    "cell",
    "pbc",
]


def compare_batches(
    batch1: Batch | None,
    batch2: Batch,
    tol: float = 1e-6,
    excluded_properties: list[str] | None = None,
) -> list[str]:
    """Compare properties between two batches

    Args:
        batch1: atoms batch
        batch2: atoms batch
        tol: tolerance used to compare equility of floating point properties
        excluded_properties: list of properties to exclude from comparison

    Returns:
        list of system changes, property names that are differente between batch1 and batch2
    """
    system_changes = []

    if batch1 is None:
        system_changes = ALL_CHANGES
    else:
        properties_to_check = set(ALL_CHANGES)
        if excluded_properties:
            properties_to_check -= set(excluded_properties)

        # Check properties that aren't
        for prop in ALL_CHANGES:
            if prop in properties_to_check:
                properties_to_check.remove(prop)
                if not torch.allclose(
                    getattr(batch1, prop), getattr(batch2, prop), atol=tol
                ):
                    system_changes.append(prop)

    return system_changes


class OptimizableBatch(Optimizable):
    """A Batch version of ase Optimizable Atoms

    This class can be used with ML relaxations in fairchem.core.relaxations.ml_relaxation
    or in ase relaxations classes, i.e. ase.optimize.lbfgs
    """

    ignored_changes: set[str] = {}

    def __init__(self, batch: Batch, trainer: BaseTrainer, numpy: bool = False):
        """Initialize Optimizable Batch

        Args:
            batch: A batch of atoms graph data
            model: An instance of a BaseTrainer derived class
            numpy: wether to cast results to numpy arrays
        """
        self.batch = batch
        self.cached_batch = None
        self.trainer = trainer
        self.numpy = numpy
        self.results = {}

    def check_state(self, batch: Batch, tol: float = 1e-12):
        """Check for any system changes since last calculation."""
        return compare_batches(
            self.cached_batch,
            batch,
            tol=tol,
            excluded_properties=set(self.ignored_changes),
        )

    def get_property(self, name):
        """Get a predicted property by name."""
        system_changes = self.check_state(self.cached_batch, self.batch)

        if len(system_changes) > 0:
            self.results = self.trainer.predict(
                self.batch, per_image=False, disable_tqdm=True
            )
            if self.numpy:
                self.results = {
                    key: pred.item() if pred.numel() == 1 else pred.cpu().numpy()
                    for key, pred in self.results.items()
                }
            self.cached_batch = self.batch.clone()

        if name not in self.results:
            raise PropertyNotImplementedError(
                f"{name} not present in this " "calculation"
            )

        return self.results[name]

    def get_positions(self):
        return self.batch.pos

    def set_positions(self, positions: torch.Tensor | NDArray):
        if isinstance(positions, np.ndarray):
            positions = torch.tensor(positions, dtype=torch.float32)

        self.batch.pos = positions.to(dtype=torch.float32)

    def get_forces(self):
        return self.get_property("forces")

    def get_potential_energy(self):
        return self.get_property("energy").sum()

    def get_potential_energies(self):
        return self.get_property("energy")

    def iterimages(self):
        # XXX document purpose of iterimages
        yield self.batch

    def converged(self, forces, fmax):
        if self.numpy:
            return np.linalg.norm(forces, axis=1).max() < fmax

        return torch.linalg.norm(forces, axis=1).max() < fmax

    def __len__(self):
        # TODO: return 3 * len(self.atoms), because we want the length
        # of this to be the number of DOFs
        return len(self.batch.pos)
