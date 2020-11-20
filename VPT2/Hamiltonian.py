"""
Provides support for build perturbation theory Hamiltonians
"""

import numpy as np, itertools, time

from McUtils.Numputils import SparseArray, vec_outer
from McUtils.Misc import Logger

from ..Molecools import Molecule
from ..BasisReps import BasisStateSpace, BasisMultiStateSpace, SelectionRuleStateSpace, HarmonicOscillatorProductBasis

from .Common import PerturbationTheoryException
from .Terms import PotentialTerms, KineticTerms, CoriolisTerm, PotentialLikeTerm

__all__ = [
    'PerturbationTheoryHamiltonian',
    'PerturbationTheoryCorrections'
]

class PerturbationTheoryCorrections:
    """
    Represents a set of corrections from perturbation theory.
    Can be used to correct other operators in the basis of the original calculation.

    """
    def __init__(self,
                 states,
                 corrections,
                 hamiltonians
                 ):
        """
        :param states: a dict with the states described by the corrections, the set of states coupled, and the size of the overall basis
        :type states: dict
        :param corrections: the corrections generated, including the corrections for the energies, wavefunctions, and a transformation from degenerate PT
        :type corrections: dict
        :param hamiltonians: the set of Hamiltonian matrices used as an expansion
        :type hamiltonians: Iterable[np.ndarray]
        """
        self.states = states['states']
        self.coupled_states = states['coupled_states'] # type: BasisMultiStateSpace
        self.total_basis = states['total_states']
        self.energy_corrs = corrections['energies']
        self.wfn_corrections = corrections['wavefunctions']
        if 'degenerate_states' in states:
            self.degenerate_states = states['degenerate_states']
        else:
            self.degenerate_states = None

        if 'degenerate_transformation' in corrections:
            self.degenerate_transf = corrections['degenerate_transformation']
        else:
            self.degenerate_transf = None

        if 'degenerate_energies' in corrections:
            self.degenerate_energies = corrections['degenerate_energies']
        else:
            self.degenerate_energies = None
        self.hams = hamiltonians

    @property
    def degenerate(self):
        return self.degenerate_transf is not None

    @property
    def energies(self):
        if self.degenerate:
            return self.degenerate_energies
        else:
            return np.sum(self.energy_corrs, axis=1)

    @property
    def order(self):
        return len(self.energy_corrs[0])

    def operator_representation(self, operator_expansion, order=None, subspace=None):
        """
        Generates the representation of the operator in the basis of stored states

        :param operator_expansion: the expansion of the operator
        :type operator_expansion: Iterable[float] | Iterable[np.ndarray]
        :param order: the order of correction to go up to
        :type order: Iterable[float] | Iterable[np.ndarray]
        :param subspace: the subspace of terms in which the operator expansion is defined
        :type subspace: None | BasisStateSpace
        :return: the set of representation matrices for this operator
        :rtype: Iterable[np.ndarray]
        """

        mordor = self.order - 1
        if order is None:
            order = mordor
        if order > mordor:
            raise PerturbationTheoryException("{}: can't correct up to order {} when zero-order states were only corrected up to order {}".format(
                type(self).__name__,
                order,
                mordor
            ))
        order = order + 1 # so that we actually do get up to the request order after accounting for the zeros...
        if len(operator_expansion) < order:
            operator_expansion = list(operator_expansion) + [0]*(order - len(operator_expansion))

        # we stopped supporting indexing based on the total set of inds...
        if subspace is None:
            wfn_corrs = self.wfn_corrections
        else:
            # need to make the subspace good with the subspace in which the corrections are defined...
            subspace_inds = subspace.indices
            corr_inds = self.coupled_states.indices
            searcher = np.argsort(corr_inds)
            subspace_sel = np.searchsorted(corr_inds, subspace_inds, sorter=searcher)

            wfn_corrs = [
                    self.wfn_corrections[:, k, subspace_sel]
                    for k in range(order)
                ]

        # wfn_corrs = [
        #     self.wfn_corrections[:, k, subspace] if subspace is not None else self.wfn_corrections[:, k, subspace]
        #     for k in range(order)
        # ]

        reps = [np.zeros(1)] * order
        for k in range(order):
            op = None
            for a in range(k+1):
                for b in range(k-a+1):
                    c = k - (a + b)
                    rop = operator_expansion[c]
                    if isinstance(rop, (int, float, np.integer, np.floating)): # constant reps...
                        if rop != 0: # cheap easy check
                            subrep = rop * np.dot(wfn_corrs[a], wfn_corrs[b].T)
                            if op is None:
                                op = subrep
                            else:
                                op += subrep
                    else:
                        subrep = np.dot(np.dot(wfn_corrs[a], rop), wfn_corrs[b].T)
                        if op is None:
                            op = subrep
                        else:
                            op += subrep
            reps[k] = op

        return reps

    def savez(self, file):
        keys = dict(
            states=self.states,
            coupled_states=self.coupled_states,
            total_states=self.total_basis,
            energies=self.energy_corrs,
            wavefunctions=self.wfn_corrections,
            hamiltonians=self.hams
        )
        if self.degenerate_states is not None:
            keys['degenerate_states'] = self.degenerate_states
        if self.degenerate_transf is not None:
            keys['degenerate_transformation'] = self.degenerate_transf
        if self.degenerate_energies is not None:
            keys['degenerate_energies'] = self.degenerate_energies
        np.savez(file, **keys)

    @classmethod
    def loadz(cls, file):
        keys = np.load(file)
        return cls(
            {
                "states":keys['states'],
                 "coupled_states":keys['coupled_states'],
                 "total_states":keys['total_states'],
                 "degenerate_states":keys['degenerate_states'] if 'degenerate_states' in keys else None
             },
            {
                "energies":keys['energies'],
                "wavefunctions":keys['wavefunctions'],
                "degenerate_transformation": keys['degenerate_transformation'] if 'degenerate_transformation' in keys else None,
                "degenerate_energies": keys['degenerate_energies'] if 'degenerate_energies' in keys else None
            },
            keys['hamiltonians']
        )

class PerturbationTheoryHamiltonian:
    """
    Represents the main Hamiltonian used in the perturbation theory calculation.
    Uses a harmonic oscillator basis for representing H0, H1, and H2 (and only goes up to H2 for now)
    """

    def __init__(self,
                 molecule=None,
                 n_quanta=None,
                 modes=None,
                 mode_selection=None,
                 coriolis_coupling = True,
                 log = None
                 ):
        """
        :param molecule: the molecule on which we're doing perturbation theory
        :type molecule:  Molecule
        :param n_quanta: the numbers of quanta to use when representing the entire state space
        :type n_quanta: int | None
        :param modes: the set of modes to use as the basis
        :type modes: None | MolecularNormalModes
        :param mode_selection: the subset of modes to use when doing expansions
        :type mode_selection: None | Iterable[int]
        :param coriolis_coupling: whether to add coriolis coupling if not in internals
        :type coriolis_coupling: bool
        """

        if log is None or isinstance(log, Logger):
            self.logger = log
        elif log is True:
            self.logger = Logger(padding="    ")
        elif log is False:
            self.logger = None
        else:
            self.logger = Logger(log, padding="    ")

        if molecule is None:
            raise PerturbationTheoryException("{} requires a Molecule to do its dirty-work")
        # molecule = molecule.get_embedded_molecule()
        self.molecule = molecule
        if modes is None:
            modes = molecule.normal_modes
        mode_n = modes.basis.matrix.shape[1] if mode_selection is None else len(mode_selection)
        self.mode_n = mode_n
        if n_quanta is None:
            n_quanta = 10 # dunno yet how I want to handle this since it should really be defined by the order of state requested...
        self.n_quanta = np.full((mode_n,), n_quanta) if isinstance(n_quanta, (int, np.int)) else tuple(n_quanta)
        self.modes = modes
        self.V_terms = PotentialTerms(self.molecule, modes=modes, mode_selection=mode_selection)#, logger=self.logger)
        self.G_terms = KineticTerms(self.molecule, modes=modes, mode_selection=mode_selection)#, logger=self.logger)
        if coriolis_coupling and (self.molecule.internal_coordinates is None):
            self.coriolis_terms = CoriolisTerm(self.molecule, modes=modes, mode_selection=mode_selection)
        else:
            self.coriolis_terms = None
        self.watson_term = PotentialLikeTerm(self.molecule, modes=modes, mode_selection=mode_selection)

        self._h0 = self._h1 = self._h2 = None

        self.basis = HarmonicOscillatorProductBasis(self.n_quanta)
        # self.basis = SimpleProductBasis(HarmonicOscillatorBasis, self.n_quanta)

    @classmethod
    def from_fchk(cls, file,
                  internals=None,
                  mode_selection=None,
                  **kw
                  ):
        """
        :param file: fchk file to load from
        :type file: str
        :param internals: internal coordinate specification as a Z-matrix ordering
        :type internals: Iterable[Iterable[int]]
        :param n_quanta:
        :type n_quanta: int | Iterable[int]
        :return:
        :rtype:
        """

        molecule = Molecule.from_file(file, zmatrix=internals, mode='fchk')
        return cls(molecule=molecule, mode_selection=mode_selection, **kw)

    @property
    def H0(self):
        """
        Provides the representation for H0 in this basis
        """
        if self._h0 is None:
            if isinstance(self.basis, HarmonicOscillatorProductBasis):
                iphase = 1
            else:
                iphase = -1
            self._h0 = (
                    (iphase * 1 / 2) * self.basis.representation('p', 'p', coeffs=self.G_terms[0],
                                                                 logger=self.logger
                                                                 )
                    + 1 / 2 * self.basis.representation('x', 'x', coeffs=self.V_terms[0],
                                                        logger=self.logger
                                                        )
            )

        return self._h0

    @property
    def H1(self):
        """
        Provides the representation for H1 in this basis
        """
        if self._h1 is None:
            if isinstance(self.basis, HarmonicOscillatorProductBasis):
                iphase = 1
            else:
                iphase = -1
            self._h1 = (
                    (iphase * 1 / 2) * self.basis.representation('p', 'x', 'p', coeffs=self.G_terms[1],
                                                                 axes=[[0, 1, 2], [1, 0, 2]],
                                                                 logger=self.logger
                                                                 )
                    + 1 / 6 * self.basis.representation('x', 'x', 'x', coeffs=self.V_terms[1],
                                                        logger=self.logger
                                                        )
            )
        return self._h1

    @property
    def H2(self):
        """
        Provides the representation for H2 in this basis
        """
        if self._h2 is None:
            if isinstance(self.basis, HarmonicOscillatorProductBasis):
                iphase = 1
            else:
                iphase = -1
            self._h2 = (
                    (iphase * 1 / 4) * self.basis.representation('p', 'x', 'x', 'p',
                                                                 coeffs=self.G_terms[2],
                                                                 axes=[[0, 1, 2, 3], [2, 0, 1, 3]],
                                                                 logger=self.logger
                                                                 )
                    + 1 / 24 * self.basis.representation('x', 'x', 'x', 'x', coeffs=self.V_terms[2],
                                                         logger=self.logger
                                                         )
            )
            if self.coriolis_terms is not None:
                total_cor = self.coriolis_terms[0] + self.coriolis_terms[1] + self.coriolis_terms[2]
                # import McUtils.Plots as plt
                # plt.ArrayPlot(
                #     total_cor.reshape(total_cor.shape[0] ** 2, total_cor.shape[0] ** 2)
                # ).show()
                self._h2 += iphase * self.basis.representation('x', 'p', 'x', 'p', coeffs=total_cor,
                                                               logger=self.logger
                                                               )
            else:
                self._h2 += 0 * self.basis.representation(coeffs=0,
                                                          logger=self.logger
                                                          )

            self._h2 += 1 / 8 * self.basis.representation(coeffs=self.watson_term[0],
                                                          logger=self.logger
                                                          )

        return self._h2

    @property
    def perturbations(self):
        return (self.H0, self.H1, self.H2)

    @staticmethod
    def _Nielsen_xss(s, w, v3, v4, zeta, Be, ndim):
        # actually pulled from the Stanton VPT4 paper since they had
        # the same units as I do...
        # we split this up into 3rd derivative, 4th derivative, and coriolis terms

        xss_4 = 1 / 16 * v4[s, s, s, s]
        xss_3 = -(
                5/48 * (v3[s, s, s] ** 2 / w[s])
                + 1/16 * sum((
                              (v3[s, s, t] ** 2) / w[t]
                              * (8 * (w[s] ** 2) - 3 * (w[t] ** 2))
                              / (4 * (w[s] ** 2) - (w[t] ** 2))
                      ) for t in range(ndim) if t != s
                      )
        )
        xss_cor = 0.
        return [xss_3, xss_4, xss_cor]

    @staticmethod
    def _Nielsen_xst(s, t, w, v3, v4, zeta, Be, ndim):
        # actually pulled from Stanton VPT4 paper
        xst_4 = 1 / 4 * v4[s, s, t, t]
        xst_3 = - 1 / 2 * (
                v3[s, s, t] ** 2 * w[s] / (4 * w[s] ** 2 - w[t] ** 2)
                + v3[s, t, t] ** 2 * w[t] / (4 * w[t] ** 2 - w[s] ** 2)
                + v3[s, s, s] * v3[s, t, t] / (2 * w[s])
                + v3[t, t, t] * v3[t, s, s] / (2 * w[t])
                - sum((
                              (
                                      (v3[s, t, r] ** 2) * w[r] * (w[s] ** 2 + w[t] ** 2 - w[r] ** 2)
                                      / (
                                          # This fucking delta_ijk term I don't know what it should be
                                          # because no one has it in my units
                                          # and none of the force-field definitions are consistent
                                              w[s] ** 4 + w[t] ** 4 + w[r] ** 4
                                              - 2 * ((w[s] * w[t]) ** 2 + (w[s] * w[r]) ** 2 + (w[t] * w[r]) ** 2)
                                      )
                              )
                              - v3[s, s, r] * v3[t, t, r] / (2 * w[r])
                      ) for r in range(ndim) if r != s and r != t
                      )
        )
        xst_cor = sum((
                                  Be[a] * (zeta[a, s, t] ** 2) * (w[t] / w[s] + w[t] / w[s])
                          ) for a in range(3))

        return [xst_3, xst_4, xst_cor]

    @classmethod
    def _get_Nielsen_xmat(cls, freqs, v3, v4, zeta, Be):
        ndim = len(freqs)
        x_mat_linear = np.array([
            cls._Nielsen_xss(s, freqs, v3, v4, zeta, Be, ndim) if s == t else
            cls._Nielsen_xst(s, t, freqs, v3, v4, zeta, Be, ndim)
            for s in range(ndim) for t in range(s, ndim)
        ]).T
        x_mat = np.zeros((3, ndim, ndim))
        ri, ci = np.triu_indices(ndim)
        x_mat[:, ri, ci] = x_mat_linear
        x_mat[:, ci, ri] = x_mat_linear
        return x_mat

    @classmethod
    def _get_Nielsen_energies(cls, states, freqs, v3, v4, zeta, Be):
        """
        Returns energies using Harald Nielsen's formulae up to second order. Assumes no degeneracies.
        If implemented smarter, would be much, much faster than doing full-out perturbation theory, but less flexible.
        Good for validation, too.


        :param states: states to get energies for as lists of quanta in degrees of freedom
        :type states: Iterable[Iterable[int]]
        :param freqs: Harmonic frequencies
        :type freqs: np.ndarray
        :param v3: Cubic force constants
        :type v3: np.ndarray
        :param v4: Quartic force constants
        :type v4: np.ndarray
        :param zeta: Coriolis couplings
        :type zeta: np.ndarray
        :param Be: Moments of inertia
        :type Be: np.ndarray
        :return:
        :rtype:
        """

        x_mat = cls._get_Nielsen_xmat(freqs, v3, v4, zeta, Be)

        from McUtils.Data import UnitsData
        h2w = UnitsData.convert("Hartrees", "Wavenumbers")

        raise Exception(x_mat * h2w)

        states = np.array(states) + 1/2 # n+1/2 for harmonic vibrations

        x_mat = np.sum(x_mat, axis=0)
        e_harm = np.tensordot(freqs, states, axes=[0, 1])
        outer_states = vec_outer(states, states)
        # raise Exception(states, outer_states)
        e_anharm = np.tensordot(x_mat, outer_states, axes=[[0, 1], [1, 2]])

        return e_harm, e_anharm

    def get_Nielsen_xmatrix(self):

        from McUtils.Data import UnitsData
        h2w = UnitsData.convert("Hartrees", "Wavenumbers")

        # TODO: figure out WTF the units on this have to be...

        freqs = self.modes.freqs
        v3 = self.V_terms[1]
        v4 = self.V_terms[2]

        # raise Exception(np.round( 6 * v3 * h2w))

        zeta, Be = self.coriolis_terms.get_zetas_and_momi()

        x = self._get_Nielsen_xmat(freqs, v3, v4, zeta, Be)

        return x

    def get_Nielsen_energies(self, states):
        """

        :param states:
        :type states:
        :return:
        :rtype:
        """


        from McUtils.Data import UnitsData
        h2w = UnitsData.convert("Hartrees", "Wavenumbers")

        # TODO: figure out WTF the units on this have to be...

        freqs = self.modes.freqs
        v3 = self.V_terms[1]
        v4 = self.V_terms[2]

        # raise Exception(np.round( 6 * v3 * h2w))

        zeta, Be = self.coriolis_terms.get_zetas_and_momi()

        harm, anharm = self._get_Nielsen_energies(states, freqs, v3, v4, zeta, Be)

        # harm = harm / h2w
        anharm = anharm

        return harm, anharm

    def get_coupled_space(self, states, order):
        """
        Returns the set of states that couple the given states up to the given order at each level of perturbation (beyond zero order).
        We keep track of how each individual state in states is transformed, as we only need to compute elements within those
        blocks, allowing for relatively dramatic speed-ups.

        :param state: the states of interest
        :type state: BasisStateSpace
        :param order: the order of perturbation theory we're doing
        :type order: int
        :param freqs: the zero-order frequencies in each vibrational mode being coupled
        :type freqs: Iterable[float]
        :param freq_threshold: the threshold for the maximum frequency difference between states to be considered
        :type freq_threshold: None | float
        :return: the sets of coupled states
        :rtype: tuple[BasisMultiStateSpace]
        """

        # the states that can be coupled through H1
        # first we generate the possible transformations +-1, +-3, +1+2, +1-2 -1+2, -1-2

        transitions_h1 = self.basis.selection_rules("x", "x", "x")

        h1_space = states.apply_selection_rules(
            transitions_h1,
            iterations=(order - 1)
        )

        # from second order corrections
        # first we generate the possible transformations +-2, +-4, +2-2, +2+2 -2+2, -2-2, -1+3, -1-3, +1+3, +1-3
        transitions_h2 = self.basis.selection_rules("x", "x", "x", "x")
        # raise Exception(transitions_h2)
        #     [
        #     [],
        #     [-2], [2],
        #     [-4], [4],
        #     [-2, -2], [-2, 2], [2, 2],
        #     [-1, -3], [-1, 3], [1, -3], [-3, 1]
        # ]
        h2_space = states.apply_selection_rules(
            transitions_h2,
            iterations=(order - 1)
        )

        # h1_space = h1_space.to_single()
        # h2_space = h2_space.to_single()

        # h1_states = [x[1] for x in h1_couplings]
        # h2_states = [x[1] for x in h2_couplings]

        return h1_space, h2_space

    @classmethod
    def _get_coupled_state_inds(cls, m):
        if not isinstance(m[0], (int, np.integer)):  # got inds for each state individually
            m_pairs = []
            for s in m:
                m_pairs.append(
                    np.concatenate([
                        np.column_stack([s, s]),
                        np.array(list(itertools.combinations(s, 2)))
                        ])
                )
            m_pairs = np.unique(
                np.concatenate(m_pairs),
                axis=0).T
        else:
            m_pairs = np.unique(np.concatenate([
                np.column_stack([m, m]),
                np.array(list(itertools.combinations(m, 2)))
            ], axis=0), axis=0).T

        return m_pairs

    @classmethod
    def _get_VPT_representations(
            cls,
            h_reps,
            states,
            coupled_states,
            logger=None,
            freq_threshold=None
    ):
        """
        Gets the sparse representations of h_reps inside the basis of coupled states

        :param h_reps:
        :type h_reps: Iterable[Representation]
        :param states:
        :type states: BasisStateSpace
        :param coupled_states:
        :type coupled_states: Iterable[BasisStateSpace | BasisMultiStateSpace]
        :return:
        :rtype:
        """

        if len(coupled_states) != len(h_reps) - 1:
            raise ValueError("coupled states must be specified for all perturbations (got {}, expected {})".format(
                len(coupled_states),
                len(h_reps) - 1
            ))

        # determine the total coupled space
        coupled_spaces = []
        input_state_space = states

        # print(coupled_states)
        space_list = [input_state_space] + list(coupled_states)
        total_state_space = BasisMultiStateSpace(np.array(space_list,  dtype=object))
        # determine indices of subspaces within this total space
        total_coupled_space = total_state_space.indices
        tot_space_indexer = np.argsort(total_coupled_space)

        # get explicit matrix reps inside the separate coupled subspaces
        N = len(total_coupled_space)

        if logger is not None:
            logger.log_print(
                ["total coupled space dimensions: {}"],
                N
            )

        # I should try to walk away from using scipy.sparse here and instead
        # shift to SparseArray, since it'll support swapping out the back end better...
        H = [np.zeros(1)] * len(h_reps)
        if logger is not None:
            start = time.time()
            logger.log_print(["calculating diagonal elements of H0..."])
        diag = h_reps[0][total_coupled_space, total_coupled_space] # this is just diagonal
        # print(total_coupled_space, diag)

        if logger is not None:
            start = time.time()
            logger.log_print(
                ["constructing sparse representation of H0..."]
            )
        H[0] = SparseArray.from_diag(diag)
        if logger is not None:
            end = time.time()
            logger.log_print(
                [
                    "finished H0...",
                    "took {}s"
                ],
                round(end - start, 3)
            )

        for i,h in enumerate(h_reps[1:]):
            # calculate matrix elements in the coupled subspace
            cs = total_state_space[i+1]

            m_pairs = cs.get_representation_indices(freq_threshold=freq_threshold)
            # blebs = BasisStateSpace(cs.basis,
            #     ((0, 0, 0, 0, 0, 1), (0, 0, 1, 1, 0, 0))
            # , mode='excitations')

            # print(any(tuple(x-blebs.indices) == (0, 0) for x in m_pairs.T))

            if len(m_pairs[0]) > 0:
                if logger is not None:
                    start = time.time()
                    logger.log_print(
                        [
                            "calculating H{}...",
                            "(coupled space dimension {})"
                        ],
                        i + 1,
                        len(m_pairs[0])
                    )
                # print(m_pairs)
                sub = h[m_pairs[0], m_pairs[1]]
                SparseArray.clear_ravel_caches()
                if logger is not None:
                    end = time.time()
                    logger.log_print(
                        [
                            "finished H{}...",
                            "took {}s"
                        ],
                        i+1,
                        round(end - start, 3)
                    )
            else:
                if logger is not None:
                    logger.log_print(
                        [
                            "calculating H{}...",
                            "no states to couple!"
                        ],
                        i + 1
                    )
                sub = 0

            if logger is not None:
                logger.log_print(
                    "constructing sparse representation..."
                )

            if isinstance(sub, (int, np.integer, np.floating, float)):
                if sub == 0:
                    sub = SparseArray.empty((N, N), dtype=float)
                else:
                    raise ValueError("Using a constant shift of {} will force Hamiltonians to be dense...".format(sub))
                    sub = np.full((N, N), sub)
            else:
                # figure out the appropriate inds for this data in the sparse representation
                row_inds = np.searchsorted(total_coupled_space, m_pairs[0], sorter=tot_space_indexer)
                col_inds = np.searchsorted(total_coupled_space, m_pairs[1], sorter=tot_space_indexer)

                # upper triangle of indices
                up_tri = np.array([row_inds, col_inds]).T
                # lower triangle is made by transposition
                low_tri = np.array([col_inds, row_inds]).T
                # but now we need to remove the duplicates, because many sparse matrix implementations
                # will sum up any repeated elements
                full_inds = np.concatenate([up_tri, low_tri])
                full_dat = np.concatenate([sub, sub])
                # zeroes = np.where(sub == 0)
                # if len(zeroes[0]) > 0:
                #     raise Exception(
                #         len(zeroes[0]),
                #         up_tri[zeroes],
                #         BasisStateSpace(
                #             states.basis,
                #             up_tri[zeroes][:, 0]
                #         ).excitations,
                #         BasisStateSpace(
                #             states.basis,
                #             up_tri[zeroes][:, 1]
                #         ).excitations
                #     )
                full_inds, idx = np.unique(full_inds, axis=0, return_index=True)
                full_dat = full_dat[idx]
                sub = SparseArray((full_dat, full_inds.T), shape=(N, N))

            H[i+1] = sub #type: np.ndarray

        # raise Exception("....")
        return H, total_state_space

    @staticmethod
    def _martin_test(h_reps, states, threshold, total_coupled_space):
        """
        Applies the Martin Test to a set of states and perturbations to determine which resonances need to be
        treated variationally. Everything is done within the set of indices for the representations.

        :param h_reps: The representation matrices of the perturbations we're applying.
        :type h_reps: Iterable[np.ndarray | SparseArray]
        :param states: The indices of the states to which we're going apply to the Martin test.
        :type states: np.ndarray
        :param threshold: The threshold for what should be treated variationally (in the same energy units as the Hamiltonians)
        :type threshold: float
        :return: Pairs of coupled states
        :rtype: tuple[BasisStateSpace, BasisStateSpace]
        """

        H0 = h_reps[0]
        H1 = h_reps[1]
        energies = np.diag(H0) if isinstance(H0, np.ndarray) else H0.diag

        # the 'states' should already be indices within the space over which we do the H1 calculation
        # basically whichever states we need to treat as degenerate for
        state_energies = energies[states]
        diffs = state_energies[:, np.newaxis] - energies[np.newaxis, :]
        for n, s in enumerate(states):
            diffs[n, s] = 1

        deg_states = []
        for s in states:
            # pull the blocks out of H1 that correspond to each the `states` we fed in...
            H1_block = H1[s, :]
            if isinstance(H1_block, SparseArray):
                nzvals = H1_block.block_vals
                nzinds, _ = H1_block.block_inds
                H1_block = nzvals
                diffs = energies[s] - energies[nzinds] # do I need an abs ?
            else:
                # compute the energy differences
                diffs = energies[s] - energies # do I need an abs ?
                nzinds = np.arange(len(energies))

            s_pos = np.where(nzinds == s)[0]
            H1_block[s_pos] = 0
            diffs[s_pos] = 1

            anh_eff = (np.abs(H1_block) ** 4) / (diffs ** 3)
            big = np.where(np.abs(anh_eff) > threshold)[0]
            if len(big) > 0:
                deg_states.extend((s, nzinds[d]) for d in big)

        if len(deg_states) == 0:
            return None
        else:
            new_degs = np.array(deg_states).T

            # raise Exception(new_degs)

            # we now have indices inside the space of coupled states...
            # so now we need to broadcast these back into their indices in the overall basis of states
            tc_inds = total_coupled_space.indices
            basis = total_coupled_space.basis

            degs = (
                BasisStateSpace(basis, tc_inds[new_degs[0]], mode='indices'),
                BasisStateSpace(basis, tc_inds[new_degs[1]], mode='indices')
            )

            # raise Exception(
            #     energies[new_degs[0],],
            #     energies[new_degs[1],],
            #     degs[0].excitations,
            #     degs[1].excitations
            # )

            return degs

    @classmethod
    def _prep_degenerate_perts(cls,
                               perts,
                               degenerate_states,
                               total_state_space,
                               logger=None
                               ):

        deg_i, deg_j = degenerate_states
        deg_iinds = deg_i.indices.flatten()
        deg_jinds = deg_j.indices.flatten()
        # raise Exception(
        #     deg_iinds,
        #     deg_i.excitations,
        #     deg_jinds,
        #     deg_j.excitations
        # )
        if logger is not None:
            all_degs = np.unique(np.concatenate([deg_iinds, deg_jinds]))
            logger.log_print(
                "got {} degenerate states",
                len(all_degs)
            )

        # now we have to figure out how these inds map onto the representation indices...
        deg_iinds = total_state_space.find(deg_iinds)
        deg_jinds = total_state_space.find(deg_jinds)
        deg_vals = np.array([h[deg_iinds, deg_jinds] for h in perts[1:]])
        # raise Exception(deg_vals,
        #                 deg_i.excitations,
        #                 deg_j.excitations,
        #                 deg_iinds, deg_jinds)
        H_non_deg = [np.zeros(1)] * len(perts)
        for i, h in enumerate(perts):
            if i == 0:
                H_non_deg[i] = h
            else:
                h = h.copy()
                h[deg_iinds, deg_jinds] = 0.
                h[deg_jinds, deg_iinds] = 0.
                H_non_deg[i] = h

        return H_non_deg, deg_vals, (deg_iinds, deg_jinds)

    @classmethod
    def _apply_degenerate_PT(cls,
                             # corrs,
                             states,
                             degenerate_states,
                             deg_vals,
                             corrs,
                             logger=None
                             ):
        # means we need to do the second level of corrections
        # we're going to need to do a full diagonalization, but we handle this
        # by pulling only the coupled elements down to a dense matrix and
        # diagonalizing there before returning the degenerate rotation as a sparse
        # matrix

        # first we figure out what the space of degenerate states is
        # has to be an NxN, so we figure out what the total space of potential degeneracies is
        dinds_i = degenerate_states[0].indices
        dinds_j = degenerate_states[1].indices
        deg_inds_all = np.unique(np.concatenate([dinds_i, dinds_j]))
        deg_dim = len(deg_inds_all)
        degenerate_space = BasisStateSpace(states.basis, deg_inds_all, mode='indices')

        # then we have to work out how indices in the larger space map onto those in the smaller one
        remap = {i: k for k, i in enumerate(deg_inds_all)}
        mapped_inds = (
            np.array([remap[i] for i in dinds_i]),
            np.array([remap[j] for j in dinds_j])
        )
        # at this point, we can fill H_deg in the basis of degenerate states
        H_deg = [np.zeros(1)] * len(deg_vals)
        for i, v in enumerate(deg_vals):
            H_deg[i] = np.zeros((deg_dim, deg_dim))
            H_deg[i][mapped_inds[0], mapped_inds[1]] = v
            H_deg[i][mapped_inds[1], mapped_inds[0]] = v

        # raise Exception(H_deg)
        # now we need to transform from the basis of zero-order states to the basis of non-degenerate states
        # which we do by using our previously built PerturbationTheoryCorrections
        if logger is not None:
            logger.log_print(
                [
                    "generating representation of resonance terms in Hamiltonian",
                    "({} terms)"
                ],
                len(dinds_i)
            )
        H_deg_transf = corrs.operator_representation(H_deg, subspace=degenerate_space)
        # now that we've corrected those elements, we add on the diagonal terms,
        # add things up, and diagonalize
        H_to_diag = np.sum(H_deg_transf, axis=0)

        import McUtils.Plots as plt
        plt.ArrayPlot(np.sum(H_deg, axis=0))
        plt.ArrayPlot(H_to_diag).show()

        H_to_diag[np.diag_indices_from(H_to_diag)] = corrs.energies
        deg_engs, deg_transf = np.linalg.eigh(H_to_diag)

        # finally we re-sort so that the new wavefunctions look maximally like the non-degenerate ones
        deg_transf = deg_transf.T  # easier for the moment...
        # state_set = set(np.arange(len(deg_transf)))
        sort_transf = deg_transf.copy()

        for i in range(len(deg_transf)):
            max_ov = np.max(deg_transf[:, i] ** 2)
            ov_thresh = .5
            if max_ov < ov_thresh:  # there must be a single mode that has more than 50% of the initial state character?
                if logger is not None:
                    logger.log_print(
                        "    state {} is more than 50% mixed",
                        i
                    )
            #     raise PerturbationTheoryException("mode {} is has no contribution of greater than {}".format(
            #         i, ov_thresh
            #     ))
        sorting = [-1] * len(deg_transf)
        for i in range(len(deg_transf)):
            o = np.argmax(abs(sort_transf[:, i]))
            sorting[i] = o
            sort_transf[o] = np.zeros(len(sort_transf))
        # sorting = [ np.argmax(abs(sort_transf[:, i])) for i in range(len(deg_transf)) ]
        # if len(sorting) != len(np.unique(sorting)):
        #     raise PerturbationTheoryException("After diagonalizing can't distinguish modes...")
        deg_engs = deg_engs[sorting,]
        deg_transf = deg_transf[sorting, :]

        return deg_engs, deg_transf

    @classmethod
    def _apply_nondegenerate_VPT(cls,
                                 H,
                                 states,
                                 order,
                                 total_state_space,
                                 state_inds,
                                 degenerate_states=None,
                                 logger=None
                                 ):
        # We use the iterative equations
        #            En^(k) = <n^(0)|H^(k)|n^(0)> + sum(<n^(0)|H^(k-i)|n^(i)> - E^(k-i)<n^(0)|n^(i)>, i=1...k-1)
        #     <n^(0)|n^(k)> = -1/2 sum(<n^(i)|n^(k-i)>, i=1...k-1)
        #           |n^(k)> = sum(Pi_n (En^(k-i) - H^(k-i)) |n^(i)>, i=1...k-1) + <n^(0)|n^(k)> |n^(0)>
        #  where Pi_n is the perturbation operator [1/(E_m-E_n) for m!=n]

        total_coupled_space = total_state_space.indices
        N = len(total_coupled_space)

        all_energies = np.zeros((len(states), order + 1))
        all_overlaps = np.zeros((len(states), order + 1))
        all_corrs = np.zeros((len(states), order + 1, N))
        # all_wfns = [] #np.zeros((len(states), order + 1, total_states))

        H0 = H[0]
        e_vec_full = np.diag(H0) if isinstance(H0, np.ndarray) else H0.diag
        if isinstance(e_vec_full, SparseArray):
            e_vec_full = e_vec_full.toarray()
            # raise Exception(e_vec_full)

        if logger is not None:
            logger.log_print(
                "calculating perturbation theory correction up to order {} for {} states",
                order,
                len(states.indices)
            )
            start = time.time()

        # loop on a state-by-state basis
        for n, energies, overlaps, corrs in zip(
                states.indices, all_energies, all_overlaps, all_corrs
        ):
            # taking advantage of mutability of the arrays here...

            # find the state index in the coupled subspace
            n_ind = np.where(total_coupled_space == n)[0][0]
            # generate the perturbation operator
            E0 = e_vec_full[n_ind]
            e_vec = e_vec_full - E0
            e_vec[n_ind] = 1
            pi = 1 / e_vec
            pi[n_ind] = 0
            pi = SparseArray.from_diag(pi)

            energies[0] = E0
            overlaps[0] = 1
            corrs[0, n_ind] = 1

            def dot(a, b):
                if isinstance(a, (int, np.integer, float, np.floating)) and a ==0:
                    return 0

                if isinstance(a, np.ndarray):
                    return np.dot(a, b)
                else:
                    return a.dot(b)


            for k in range(1, order + 1):  # to actually go up to k
                #         En^(k) = <n^(0)|H^(k)|n^(0)> + sum(<n^(0)|H^(k-i)|n^(i)> - E^(k-i)<n^(0)|n^(i)>, i=1...k-1)


                Ek = (
                   (H[k][n_ind, n_ind] if not isinstance(H[k], (int, np.integer, float, np.floating)) else 0.)
                        + sum(
                    dot(
                        H[k - i][n_ind] if not isinstance(H[k - i], (int, np.integer, float, np.floating)) else 0.,
                        corrs[i]
                        )
                    - energies[k - i] * overlaps[i]
                    for i in range(1, k)
                ))
                energies[k] = Ek
                #   <n^(0)|n^(k)> = -1/2 sum(<n^(i)|n^(k-i)>, i=1...k-1)
                ok = -1 / 2 * sum(dot(corrs[i], corrs[k - i]) for i in range(1, k))
                overlaps[k] = ok
                #         |n^(k)> = sum(Pi_n (En^(k-i) - H^(k-i)) |n^(i)>, i=0...k-1) + <n^(0)|n^(k)> |n^(0)>
                corrs[k] = sum(
                    dot(pi, energies[k - i] * corrs[i] - dot(H[k - i], corrs[i]))
                    for i in range(0, k)
                )
                corrs[k][n_ind] = ok  # pi (the perturbation operator) ensures it's zero before this

        if logger is not None:
            end = time.time()
            logger.log_print(
                "took {}s",
                round(end - start, 3)
            )

        # now we recompute reduced state spaces for use in results processing
        # and we also convert the correction vectors to sparse representations
        tci = total_state_space.indices
        N = len(tci)
        nstates = len(all_corrs)
        corr_inds = [[] for i in range(nstates)]
        corr_mats = [None] * (order + 1)
        si = state_inds

        if logger is not None:
            logger.log_print(
                "constructing sparse correction matrices...",
            )

        for o in range(order + 1):
            non_zeros = []
            for i, corr in enumerate(all_corrs):
                # we find the non-zero elements within the o level of correction for the ith state
                nonzi = np.where(np.abs(corr[o]) > 1.0e-14)[0]
                # print(nonzi)
                # then we pull these out
                vals = corr[o][nonzi,]
                # and we add the values and indices to the list
                me = si[i]
                non_zeros.append(
                    (
                        vals,
                        np.column_stack([
                            np.full(len(nonzi), i),
                            nonzi
                        ])
                    )
                )

                # and then we add the appropriate basis indices to the list of basis data
                wat = tci[nonzi,]
                corr_inds[i].append(wat)

            # now we build the full mat rep for this level of correction
            vals = np.concatenate([x[0] for x in non_zeros])
            inds = np.concatenate([x[1] for x in non_zeros], axis=0).T
            # print(inds, N)
            corr_mats[o] = SparseArray(
                (
                    vals,
                    inds
                ),
                shape=(nstates, N)
            )

        # now we build state reps from corr_inds
        for i, dat in enumerate(corr_inds):
            full_dat = np.unique(np.concatenate(dat))
            corr_inds[i] = BasisStateSpace(states.basis, full_dat, mode="indices")

        cs_states = SelectionRuleStateSpace(states, corr_inds, None)
        total_states = total_state_space
        corrs = PerturbationTheoryCorrections(
            {
                "states": states,
                "coupled_states": cs_states,
                "total_states": total_states,
                "degenerate_states": degenerate_states
            },
            {
                "energies": all_energies,
                "wavefunctions": corr_mats,
                "degenerate_transformation": None,
                "degenerate_energies": None
            },
            H
        )

        return corrs

    @classmethod
    def _get_true_degenerate_states(cls, H, coupled_states, state_inds, degenerate_states, total_state_space, logger=None):
        if degenerate_states is not None:
            # we check this twice because the Martin test can return None
            if isinstance(degenerate_states, (int, np.integer, np.floating, float)):
                thresh = degenerate_states
                if logger is not None:
                    logger.log_print(
                        "applying Martin test with threshold {}",
                        thresh
                    )
                degenerate_states = cls._martin_test(
                    H,
                    state_inds,  # state indices in the coupled_states
                    thresh,
                    total_state_space
                )

            elif all(isinstance(x, (BasisStateSpace, BasisMultiStateSpace)) for x in degenerate_states):
                # means we just got the degenerate subspace to work with
                pass
            elif degenerate_states is not None:
                try:
                    degenerate_states = degenerate_states(H, coupled_states)
                except (TypeError, ValueError):
                    pass

                if not isinstance(degenerate_states[0], (BasisStateSpace, BasisMultiStateSpace)):
                    raise NotImplementedError("can't deal with non-BasisStateSpace specs for degeneracies")

        return degenerate_states

    @classmethod
    def _apply_VPT(cls,
                   H,
                   states,
                   coupled_states,
                   order,
                   total_state_space,
                   degenerate_states=None,
                   logger=None
                   ):

        state_inds = total_state_space.find(total_state_space[0].indices)

        degenerate_states = cls._get_true_degenerate_states(
            H, coupled_states, state_inds, degenerate_states, total_state_space,
            logger=logger
        )

        if degenerate_states is None:
            deg_vals = None
        else:
            H, deg_vals, deg_inds = cls._prep_degenerate_perts(
                H,
                degenerate_states,
                total_state_space,
                logger=logger
            )

        corrs = cls._apply_nondegenerate_VPT(
            H,
            states,
            order,
            total_state_space,
            state_inds,
            degenerate_states=degenerate_states,
            logger=logger
        )

        if degenerate_states is not None:
            if logger is not None:
                logger.log_print(
                    "handling degeneracies...",
                )
            deg_engs, deg_transf = cls._apply_degenerate_PT(
                states,
                degenerate_states,
                deg_vals,
                corrs,
                logger=logger
            )
            corrs.degenerate_energies = deg_engs
            corrs.degenerate_transf = deg_transf

        return corrs

    @classmethod
    def _get_VPT_corrections(
            cls,
            h_reps,
            states,
            coupled_states,
            # total_states,
            order,
            degenerate_states=None,
            logger=None
    ):
        """
        Applies perturbation theory to the constructed representations of H0, H1, etc.

        :param h_reps: series of perturbations as indexable objects
        :type h_reps: Iterable[np.ndarray]
        :param states: index of the states to get corrections for
        :type states: Iterable[int]
        :param coupled_states: indices of states to couple for each level of perturbation
        :type coupled_states: Iterable[Iterable[int]]
        :param total_states: the full number of state indices
        :type total_states: int
        :param order: the order of perturbation theory to apply
        :type order: int
        :param degenerate_states: the pairs of degeneracies to account for
        :type degenerate_states: None | (Iterable[int], Iterable[int])
        :return: the vibrational perturbation theory corrections for a single target state
        :rtype: PerturbationTheoryCorrections
        """

        H, total_state_space = cls._get_VPT_representations(h_reps, states, coupled_states, logger)

        corrs = cls._apply_VPT(
                   H,
                   states,
                   coupled_states,
                   order,
                   total_state_space,
                   degenerate_states=degenerate_states,
                   logger=logger
                   )

        return corrs

    def _prep_coupled_states(self, states, coupled_states, order):
        """
        Preps coupled states as input for `get_wavefunctions` and `get_representations`

        :param states:
        :type states:
        :param coupled_states:
        :type coupled_states:
        :param order:
        :type order:
        :return:
        :rtype:
        """
        if coupled_states is None or isinstance(coupled_states, (int, np.integer, float, np.floating)):
            # pull the states that we really want to couple
            coupled_states = self.get_coupled_space(states,
                                                    order
                                                    # freqs=self.modes.freqs,
                                                    # freq_threshold=coupled_states
                                                    )

        elif isinstance(coupled_states, (BasisStateSpace, BasisMultiStateSpace)):
            # same spec for both perturbations
            coupled_states = [coupled_states, coupled_states]
        elif isinstance(coupled_states[0], (BasisStateSpace, BasisMultiStateSpace)):
            pass

        elif isinstance(coupled_states[0], (int, np.integer)): # got a single `BasisStateSpace` as indices
            coupled_states = BasisStateSpace(self.basis, coupled_states, mode="indices")
            coupled_states = [coupled_states, coupled_states]

        elif isinstance(coupled_states[0][0], (int, np.integer)): # got a single `BasisStateSpace` as excitations
            coupled_states = BasisStateSpace(self.basis, coupled_states, mode="excitations")
            coupled_states = [coupled_states, coupled_states]

        else:
            raise ValueError("Not sure what to do with coupled space spec {}".format(
                coupled_states
            ))

        return coupled_states

    def _prep_degeneracies_spec(self, degeneracies):
        if (
                degeneracies is not None
                and not isinstance(degeneracies, (int, float, np.integer, np.floating))
        ):
            if isinstance(degeneracies[0], (int, np.integer)):
                degs = BasisStateSpace(self.basis, degeneracies, mode="indices")
                degeneracies = (degs, degs)
            elif isinstance(degeneracies[0][0], (int, np.integer)):
                degs = BasisStateSpace(self.basis, degeneracies, mode="excitations")
                degeneracies = (degs, degs)
            else:
                degeneracies = (
                                   BasisStateSpace(self.basis, degeneracies[0]),
                                   BasisStateSpace(self.basis, degeneracies[1])
                )

        return degeneracies

    def get_representations(self,
                            states,
                            coupled_states=None,
                            degeneracies=None,
                            order=2
                            ):
        """
        Returns the representations of the perturbations in the basis of coupled states

        :param coupled_states:
        :type coupled_states:
        :return:
        :rtype:
        """

        if self.logger is not None:
            self.logger.log_print("Computing PT corrections:", padding="")
            start = time.time()
            self.logger.log_print("getting coupled states...")

        states, coupled_states, degeneracies = self.get_input_state_spaces(states, coupled_states, degeneracies)

        if self.logger is not None:
            end = time.time()
            self.logger.log_print("took {}s...", round(end - start, 3))

        H, tot_space = self._get_VPT_representations(
            self.perturbations,
            states,
            coupled_states,
            logger=self.logger
        )

        return H

    def get_input_state_spaces(self,
                               states,
                               coupled_states=None,
                               degeneracies=None,
                               order=2
                               ):
        """
        Converts the input state specs into proper `BasisStateSpace` specs that
        will directly feed into the code

        :param states:
        :type states:
        :param coupled_states:
        :type coupled_states:
        :param degeneracies:
        :type degeneracies:
        :return:
        :rtype:
        """

        # need to rewrite this to work better with BasisStateSpace
        if not isinstance(states, BasisStateSpace):
            states = BasisStateSpace(self.basis, states)

        coupled_states = self._prep_coupled_states(
            states,
            coupled_states,
            order
        )

        degeneracies = self._prep_degeneracies_spec(
            degeneracies
        )

        return states, coupled_states, degeneracies

    def get_wavefunctions(self,
                          states,
                          coupled_states=None,
                          degeneracies=None,
                          order=2
                          ):
        """
        Gets a set of `PerturbationTheoryWavefunctions` from the perturbations defined by the Hamiltonian

        :param states: the states to get the index for, given either as indices or as a numbers of quanta
        :type states: BasisStateSpace | Iterable[int] | Iterable[Iterable[int]]
        :param coupled_states: the list of states to explicitly allow to couple in
        :type coupled_states: BasisStateSpace | Iterable[int] | Iterable[Iterable[int]]
        :param degeneracies: the pairs of states to be treated via degenerate perturbation theory
        :type degeneracies: (Iterable[int], Iterable[int])  | (Iterable[Iterable[int]], Iterable[Iterable[int]])
        :return: generated wave functions
        :rtype: PerturbationTheoryWavefunctions
        """

        from .Wavefunctions import PerturbationTheoryWavefunctions

        if self.logger is not None:
            self.logger.log_print("Computing PT corrections:", padding="")
            start = time.time()
            self.logger.log_print("getting coupled states...")

        states, coupled_states, degeneracies = self.get_input_state_spaces(states, coupled_states, degeneracies)

        if self.logger is not None:
            end = time.time()
            self.logger.log_print("took {}s...", round(end - start, 3))

        h_reps = self.perturbations
        if self.logger is not None:
            bs = []
            for b in coupled_states:
                bs.append(len(b))
            self.logger.log_print(
                [
                    "perturbations: {pert_num}",
                    "order: {ord}",
                    "states: {state_num}",
                    "basis sizes {basis_size}"
                ],
                pert_num=len(h_reps) - 1,
                ord=order,
                state_num=len(states),
                basis_size=bs
            )

        corrs = self._get_VPT_corrections(
            h_reps,
            states,
            coupled_states,
            order,
            degenerate_states=degeneracies,
            logger=self.logger
            )

        return PerturbationTheoryWavefunctions(self.molecule, self.basis, corrs, logger=self.logger)

    @classmethod
    def _invert_action_expansion_tensors(cls,
                                         states,
                                         energies,
                                         order
                                         ):
        """
        Obtains expansions of the relevant tensors in terms of their classical actions.
        Only applicable to a Harmonic PT approach, really, but quite useful.

        :param states: states up to `n` quanta of excitation, where n=order of expansion
        :type states: BasisStateSpace
        :param energies: energies from PT for the states
        :type energies: np.ndarray
        :param order: the order of perturbation theory we applied
        :type order: int
        :return:
        :rtype: list[np.array | float]
        """

        nmodes = states.ndim
        exc = states.excitations

        c_mat = np.zeros((len(states), len(states)), dtype=float)  # to invert

        col = 0
        blocks = []  # to more easily recompose the tensors later
        nterms = 1 + order // 2 # second order should be [2, 1], 4th order should be [3, 2, 1], 6th should be [4, 3, 2, 1]
        for k in range(nterms, 0, -1):
            ninds = []
            # generate the index tensors to loop over
            inds = np.indices((nmodes,) * k)
            inds = inds.transpose(tuple(range(1, k + 1)) + (0,))
            inds = np.reshape(inds, (-1, k))
            for perm in inds:
                if (np.sort(perm) != perm).any():
                    continue # only want the _unique_ permutations
                # generate the action coefficients
                coeffs = np.prod(exc[:, perm] + 1 / 2, axis=1)
                c_mat[:, col] = coeffs
                col += 1
                ninds.append(perm)
            blocks.append(ninds)
        # finally we add in the coefficient from k=0
        c_mat[:, col] = 1

        # get the solutions to the linear equation
        tensor_terms = np.linalg.solve(c_mat, energies)

        # reconstruct the tensors
        tens = [np.zeros(1)] * (nterms + 1)
        where_am_i = 0
        for i, b in enumerate(blocks):
            s = where_am_i
            nb = len(b)
            vec = tensor_terms[s:s+nb]
            k = nterms - i
            term = np.zeros((nmodes,) * k)
            bi = tuple(np.transpose(np.array(b)))
            term[bi] = vec
            where_am_i += nb
            tens[k] = term

        tens[0] = tensor_terms[-1]

        return tens

    def get_action_expansion(self, order=2):
        """
        Gets the expansion of the energies in terms of Miller's "classical actions" by
        doing just enough PT to invert the matrix

        :param order:
        :type order:
        :return:
        :rtype:
        """

        ndim = len(self.n_quanta)
        nterms = 1 + order // 2

        # clumsy buy w/e it works for now
        def get_states(n_quanta, n_modes, max_quanta=None):
            import itertools as ip

            if max_quanta is None:
                max_quanta = n_quanta
            return tuple(sorted(
                [p for p in ip.product(*(range(n_quanta + 1) for i in range(n_modes))) if
                 all(x <= max_quanta for x in p) and sum(p) <= n_quanta],
                key=lambda p: (
                        sum(p)
                        + sum(1 for v in p if v != 0) * n_quanta ** (-1)
                        + sum(v * n_quanta ** (-i - 2) for i, v in enumerate(p))
                )
            ))


        states = get_states(nterms, ndim)

        wfns = self.get_wavefunctions(states)

        expansion = self._invert_action_expansion_tensors(wfns.corrs.states, wfns.energies, order)

        return expansion, wfns


    def get_breakdown(self,
                      states,
                      coupled_states=None,
                      degeneracies=None,
                      order=2
                      ):

            from collections import OrderedDict
            from .Wavefunctions import PerturbationTheoryWavefunctions

            if self.logger is not None:
                self.logger.log_print("Computing PT breakdown:", padding="")
                start = time.time()
                self.logger.log_print("getting coupled states...")

            states, coupled_states, degeneracies = self.get_input_state_spaces(states, coupled_states, degeneracies)

            if self.logger is not None:
                end = time.time()
                self.logger.log_print("took {}s...", round(end - start, 3))

            h_reps = self.perturbations
            if self.logger is not None:
                bs = []
                for b in coupled_states:
                    bs.append(len(b))
                self.logger.log_print(
                    [
                        "perturbations: {pert_num}",
                        "order: {ord}",
                        "states: {state_num}",
                        "basis sizes {basis_size}"
                    ],
                    pert_num=len(h_reps) - 1,
                    ord=order,
                    state_num=len(states),
                    basis_size=bs
                )

            H, total_state_space = self._get_VPT_representations(h_reps, states, coupled_states, self.logger)

            specs = OrderedDict((
                ("Harmonic",   (True, False, False)),
                ("Cubic",      (True, True,  False)),
                ("Quartic",    (True, False, True)),
                ("Full",       (True, True,  True))
            ))

            for k in specs:
                this_h = [H[i] if len(H) > i and s else 0 for i, s in enumerate(specs[k])]
                if self.logger is not None:
                    self.logger.log_print(
                        [
                            "getting breakdown for {} terms...",
                            "(non-zero terms {})"
                            ],
                        k,
                        len(this_h) - this_h.count(0)
                    )
                corrs = self._apply_VPT(
                    this_h,
                    states,
                    coupled_states,
                    order,
                    total_state_space,
                    degenerate_states=degeneracies,
                    logger=self.logger
                )

                wfns = PerturbationTheoryWavefunctions(self.molecule, self.basis, corrs, logger=self.logger)
                specs[k] = wfns

            return specs