import collections, functools, numpy as np
# might be worth excising theses since this could be standalone otherwise?
from McUtils.Data import AtomData, UnitsData
from McUtils.Zachary import FiniteDifferenceDerivative, RBFDInterpolator

from ..Molecools import Molecule

__all__ = [
    "AIMDSimulator",
    "PairwisePotential"
]

class AIMDSimulator:
    def __init__(self,
                 atoms,
                 coordinates,
                 force_function,
                 velocities=0,
                 timestep=.1,
                 sampling_rate=1
                 ):
        self.masses = np.array([AtomData[a, "Mass"] if isinstance(a, str) else a for a in atoms]) * UnitsData.convert("AtomicMassUnits", "ElectronMass")
        self._mass = self.masses[np.newaxis, :, np.newaxis]
        self.coords = np.asanyarray(coordinates)
        if isinstance(velocities, (int, float, np.integer, np.floating)):
            velocities = np.full_like(self.coords, velocities)
        self.velocities = velocities
        self.force_function = force_function
        self._prev_forces = None

        self.trajectory = collections.deque()
        self.trajectory.append(self.coords)
        self.steps = 0
        self.dt = timestep
        self.sampling_rate = sampling_rate

    def step(self):
        forces = self._prev_forces
        if forces is None:
            forces = self.force_function(self.coords)

        v = self.velocities
        coords = self.coords + v * self.dt + forces / (2 * self._mass) * self.dt**2
        com = np.tensordot(self.masses, coords, axes=[0, -2]) / np.sum(self._mass)
        coords = coords - com[:, np.newaxis, :] # don't let COM move
        forces_new = self.force_function(coords)
        vels = v + self.dt * (forces + forces_new) / (2 * self._mass)

        self._prev_forces = forces_new
        self.velocities = vels
        self.coords = coords
        self.steps += 1

        return coords, vels, forces_new

    def propagate(self, num_steps=1):

        for _ in range(num_steps):
            c, v, f = self.step()
            if self.steps % self.sampling_rate == 0:
                self.trajectory.append(c)

        return self.trajectory

    def build_interpolation(self, energy_function, interpolation_order=2, interpolator_class=None, eckart_embed=True, **interpolator_options):

        traj = np.array(self.trajectory)
        traj = traj.reshape((-1,) + traj.shape[-2:])

        vals = [energy_function(traj)]

        # if clustering_criterion > ...:
        #     ...

        # TODO: add clustering radius + energy cutoff within disks

        if interpolation_order > 0:
            vals.append(self.force_function(traj))
        if interpolation_order > 1:
            hess = FiniteDifferenceDerivative(
                    self.force_function,
                    function_shape=(traj.shape[-2:], traj.shape[-2:])
                ).derivatives(traj).compute_derivatives(1)
            hess = np.moveaxis(hess, -3, 0).reshape(
                (hess.shape[-3],) + hess.shape[-2:] * 2
            )
            vals.append(hess)
        if interpolation_order > 2:
            raise ValueError("can only do order 2 interps for now because I am laaazy")

        if eckart_embed:
            ref_pos = np.argmin(vals[0])
            ref = Molecule(
                ["H"] * len(self.masses),
                traj[ref_pos],
                masses=self.masses,
            ).get_embedded_molecule(load_properties=False)
            all_crds = []
            all_ders = [[] for _ in range(interpolation_order)]
            for crd_ders in zip(traj, *vals[1:]): #TODO: speed this up...
                c = crd_ders[0]
                mol = Molecule(
                    ["H"] * len(self.masses),
                    c,
                    masses=self.masses,
                    potential_derivatives=crd_ders[1:]
                ).get_embedded_molecule(ref, load_properties=False)
                all_crds.append(mol.coords)
                pes = mol.potential_derivatives
                for i, p in enumerate(pes):
                    all_ders[i].append(p)

            traj = np.array(all_crds)
            vals = [vals[0]] + [np.array(v) for v in all_ders]

        if interpolator_class is None:
            interpolator_class = RBFDInterpolator

        return interpolator_class(traj, *vals, **interpolator_options)



class PairwisePotential:
    def __init__(self, fun, deriv=None):
        self.fun = fun
        if deriv is None:
            if hasattr(deriv, 'deriv'):
                fun.deriv()
            else:
                deriv = self.numerical_deriv(fun)
        self.deriv = deriv
        self._inds = None # to cache indices when applied to same-shape data

    @classmethod
    def numerical_deriv(cls, fun, step_size=.01):
        @functools.wraps(fun)
        def deriv(dists):
            return (fun(dists + step_size) - fun(dists - step_size)) / (2 * step_size)
        return deriv

    def _get_inds_cached(self, coords):
        if self._inds is not None and len(coords) != self._inds[0]:
            self._inds = None
        if self._inds is None:
            row_inds, col_inds = np.triu_indices(len(coords), k=1)
            _, row_map = np.unique(row_inds, return_index=True) # One element less than the coords
            sorting = np.argsort(col_inds)
            _, col_map = np.unique(col_inds, return_index=True) # One element less than the coords
            self._inds = [len(coords), (row_inds, col_inds, row_map, sorting, col_map)]
        return self._inds[1]

    def eval(self, coords):
        row_inds, col_inds, row_map, col_sort, col_map = self._get_inds_cached(coords)
        pot = self.fun

        # compute unsigned force for each pair
        diffs = coords[row_inds] - coords[col_inds]
        dists = np.linalg.norm(diffs, axis=1)
        pot_vals = pot(dists)

        print(row_map)

        # now reduce over different chunks for the positive contribs...
        pos_chunks = np.add.reduceat(
            pot_vals,
            row_map
        )
        neg_chunks = np.add.reduceat(
            pot_vals[col_sort],
            col_map
        )

        return np.concatenate(
            [
                pos_chunks[:1],
                pos_chunks[1:] + neg_chunks[:-1],
                pos_chunks[-1:]
            ],
            axis=0
        )

    def forces(self, coords):
        row_inds, col_inds, row_map, col_sort, col_map = self._get_inds_cached(coords)
        pot_deriv = self.deriv

        # compute unsigned force for each pair
        diffs = coords[row_inds] - coords[col_inds]
        dists = np.linalg.norm(diffs, axis=1)
        base_force = pot_deriv(dists)
        normals = diffs / dists[:, np.newaxis]
        force_list = -normals * base_force[:, np.newaxis]

        # now reduce over different chunks for the positive contribs...
        pos_chunks = np.add.reduceat(
            force_list,
            row_map
        )
        neg_chunks = np.add.reduceat(
            force_list[col_sort],
            col_map
        )

        return np.concatenate(
            [
                pos_chunks[:1],
                pos_chunks[1:] - neg_chunks[:-1],
                pos_chunks[-1:]
            ],
            axis=0
        )

    def __call__(self, coords):
        return self.eval(coords)
