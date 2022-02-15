
import numpy as np, itertools, time, gc
from collections import namedtuple

from McUtils.Numputils import SparseArray
from McUtils.Scaffolding import Logger, NullLogger, NullCheckpointer
from McUtils.Parallelizers import Parallelizer, SerialNonParallelizer
from McUtils.Data import UnitsData
from McUtils.Combinatorics import LatticePathGenerator

from ..BasisReps import Representation, BasisStateSpace, BasisMultiStateSpace, SelectionRuleStateSpace, BraKetSpace

from .StateFilters import PerturbationTheoryStateSpaceFilter
from .DegeneracySpecs import DegenerateMultiStateSpace
from .Common import *
from .Corrections import *

__reload_hook__ = [ "..BasisReps" ]

__all__ = [
    "PerturbationTheorySolver"
]

class PerturbationTheorySolver:
    """
    A solver that applies perturbation theory
    given a series of corrections and population of states.
    Supports degenerate and non-degenerate PT.
    """

    def __init__(self,
                 perturbations, states,
                 coupled_states=None,
                 order=2,
                 total_space=None,
                 flat_total_space=None,
                 state_space_iterations=None,
                 state_space_terms=None,
                 state_space_filters=None,
                 target_property_rules=None,
                 allow_sakurai_degs=False,
                 allow_post_PT_calc=True,
                 modify_degenerate_perturbations=False,
                 gaussian_resonance_handling=False,
                 ignore_odd_order_energies=False,
                 intermediate_normalization=False,
                 zero_element_warning=True,
                 degenerate_states=None,
                 handle_strong_couplings=False,
                 zero_order_energy_corrections=None,
                 memory_constrained=False,
                 keep_hamiltonians=None,
                 logger=None,
                 parallelizer=None,
                 checkpointer=None,
                 results=None,
                 checkpoint_keys=None,
                 use_cached_representations=True,
                 use_cached_basis=True
                 ):
        """

        :param perturbations:
        :type perturbations: Iterable[Representation]
        :param states:
        :type states: BasisStateSpace
        :param coupled_states:
        :type coupled_states: BasisMultiStateSpace
        :param order:
        :type order:
        :param degenerate_states:
        :type degenerate_states:
        :param degeneracy_mode:
        :type degeneracy_mode:
        :param logger:
        :type logger:
        :param parallelizer:
        :type parallelizer:
        :param checkpointer:
        :type checkpointer:
        :param results:
        :type results:
        """

        # if memory_constrained:
        #     raise NotImplementedError('memory constraint handling currently broken')

        self.perts = perturbations
        self._reps = None
        self.order = order
        self.state_space_iterations=state_space_iterations
        self.state_space_terms=state_space_terms

        self.state_space_filters=PerturbationTheoryStateSpaceFilter.from_data(states, state_space_filters)
        self.target_property_rules=target_property_rules

        self.logger = logger
        self.parallelizer = parallelizer
        self.checkpointer = checkpointer
        self.results = results
        self.checkpoint_keys = checkpoint_keys
        self.use_cached_representations=use_cached_representations
        self.use_cached_basis=use_cached_basis

        self.states = states
        self.full_basis = states.full_basis

        self.degeneracy_spec = degenerate_states
        self._deg_states = None
        if handle_strong_couplings is None:
            handle_strong_couplings = self.degeneracy_spec is None
        self.handle_strong_couplings = handle_strong_couplings
        # self.degeneracy_mode = degeneracy_mode
        self.allow_sakurai_degs = allow_sakurai_degs
        self.allow_post_PT_calc = allow_post_PT_calc
        self.zero_order_energy_corrections = zero_order_energy_corrections
        self.ignore_odd_orders = ignore_odd_order_energies
        self.drop_perturbation_degs = modify_degenerate_perturbations
        self.intermediate_normalization = intermediate_normalization
        self.gaussian_resonance_handling = gaussian_resonance_handling
        self.zero_element_warning = zero_element_warning

        self.memory_constrained=memory_constrained
        self.keep_hamiltonians=keep_hamiltonians

        self._coupled_states = coupled_states
        self._total_space = total_space
        self._flat_space = flat_total_space
        self._total_dim = None

        self._zo_engs = None
    @property
    def coupled_states(self):
        if self._coupled_states is None:
            self.load_state_spaces()
        return self._coupled_states
    @property
    def total_space_dim(self):
        if self._total_dim is None:
            self.load_state_spaces()
        return self._total_dim
    @property
    def flat_total_space(self):
        if self._flat_space is None:
            self.load_state_spaces()
        return self._flat_space
    @property
    def total_state_space(self):
        if self._total_space is None:
            self.load_state_spaces()
        return self._total_space

    class PastIndexableTuple(tuple):
        def __getitem__(self, item):
            if isinstance(item, (int, np.integer)) and item >= len(self):
                return 0
            else:
                return super().__getitem__(item)

    @property
    def representations(self):
        """
        :return:
        :rtype: Iterable[SparseArray]
        """
        if self._reps is None:
            self._reps = self.PastIndexableTuple(self.get_VPT_representations())
        return self._reps
    @representations.setter
    def representations(self, reps):
        self._reps = reps

    @property
    def degenerate_spaces(self):
        if self._deg_states is None:
            self._deg_states = DegenerateMultiStateSpace.from_spec(self.degeneracy_spec, solver=self, full_basis=self.full_basis)
        return self._deg_states

    @property
    def zero_order_energies(self):
        if self._zo_engs is None:
            H0 = self.representations[0]
            e_vec_full = np.diag(H0) if isinstance(H0, np.ndarray) else H0.diag
            if isinstance(e_vec_full, SparseArray):
                e_vec_full = e_vec_full.asarray()
            self._zo_engs = e_vec_full
            if self.zero_order_energy_corrections is not None:
                for k,v in self.zero_order_energy_corrections:
                    if not isinstance(k, int):
                        k = self.flat_total_space.find([k]) # slow but w/e
                    if k >= len(self._zo_engs):
                        self.logger.log_print(
                            "WARNING: zero-order correction {} for state {} not used as state is not in basis".format(
                                v * UnitsData.convert("Hartrees", "Wavenumbers"),
                                k
                            )
                        )
                    self._zo_engs[k] = v
        return self._zo_engs

    def apply_VPT(self):
        """
        Applies perturbation theory to the held basis of states using the
        built representations and degenerate state spaces

        :return:
        :rtype: PerturbationTheoryCorrections
        """


        # wat = sum(self.representations)[:100, :100]
        # analytic_engs = np.linalg.eigh(wat.asarray())[0]

        corrs = self.get_corrections()
        degenerate_states = corrs.degeneracies
        # print(degenerate_states)
        corrs = corrs.corrections

        # import McUtils.Plots as plt
        #
        # wat = corrs.get_overlap_matrices()

        # degeneracy_mode = self.degeneracy_mode
        # degenerate_states = self.degenerate_spaces
        if (
                self.allow_post_PT_calc
                and degenerate_states is not None
                and any(len(x) > 1 for x in degenerate_states)
        ):
            with self.logger.block(tag="Applying post-PT variational calc."):
                if self.gaussian_resonance_handling:
                    self.logger.log_print('WARNING: Doing Gaussian resonance handling and not doing variational calculation involving states with more than 2 quanta of excitation')
                deg_engs, deg_transf = self.apply_post_PT_variational_calc(degenerate_states, corrs)
                corrs.degenerate_energies = deg_engs
                corrs.degenerate_transf = deg_transf

        # if (
        #         self.keep_hamiltonians is None and self.memory_constrained
        #         or (self.keep_hamiltonians is not None and not self.keep_hamiltonians)
        # ):
        #     corrs.hams = None # just to allow the memory to be freed

        return corrs

    #region Get Matrix Inputs
    def get_VPT_representations(self):
        """
        Gets the sparse representations of the passed perturbation inside the basis of coupled states.

        :return:
        :rtype: Iterable[SparseArray]
        """

        logger = self.logger
        if logger is None:
            logger = NullLogger()

        with self.checkpointer as checkpointer:
            with self.logger.block(tag='getting representations'):
                self.logger.log_print('trying to load from checkpoint...')
                try:
                    if self.use_cached_representations:
                        H = checkpointer['representations']
                    else:
                        H = None
                except (OSError, KeyError):
                    H = None
                if H is None:
                    self.logger.log_print('failed to load, building instead...')

                    par = Parallelizer.lookup(self.parallelizer)
                    with par:  # we put an outermost block here to just make sure everything is clean

                        # diag_inds = BraKetSpace(self.flat_total_space, self.flat_total_space)
                        # N = len(self.flat_total_space)

                        n_spaces = len(self.total_state_space.spaces)
                        # raise Exception(len(self.total_state_space.spaces))
                        H = [np.zeros(1)] * min(len(self.perts), n_spaces)
                        with logger.block(tag="building {}".format(self.perts[0])):
                            start = time.time()
                            H[0] = self.perts[0].get_representation_matrix(self.flat_total_space, self.flat_total_space,
                                                                           zero_element_warning=self.zero_element_warning,
                                                                           diagonal=True
                                                                           )
                            end = time.time()
                            logger.log_print("took {t:.3f}s", t=end - start)
                            self.perts[0].clear_cache()
                            # if self.memory_constrained and not isinstance(self.checkpointer, NullCheckpointer):
                            #     self.checkpointer['H[0]'] = H[0]
                            #     H[0] = None
                            #     gc.collect()

                        # import tracemalloc
                        for i, h in enumerate(self.perts[1:]):
                            # calculate matrix elements in the coupled subspace
                            if n_spaces > i + 1:
                                cs = self.total_state_space[i + 1]
                                with logger.block(tag="building {}".format(h)):
                                    start = time.time()
                                    # tracemalloc.start()
                                    # pre = tracemalloc.take_snapshot()
                                    H[i + 1] = h.get_representation_matrix(cs, self.flat_total_space,
                                                                           zero_element_warning=self.zero_element_warning,
                                                                           diagonal=False,
                                                                           memory_constrained=self.memory_constrained
                                                                           )
                                    h.clear_cache()
                                    # post = tracemalloc.take_snapshot()
                                    # for stat in post.compare_to(pre, 'lineno')[:10]:
                                    #     print(stat)
                                    # tracemalloc.stop()
                                    # if self.memory_constrained:
                                    #     if not isinstance(self.checkpointer, NullCheckpointer):
                                    #         self.checkpointer['H[{}]'.format(i+1)] = H[i+1]
                                    #         H[i+1] = None
                                    #         gc.collect()
                                    #     # H[i + 1] = H[i + 1].ascs(inplace=True) # attempt to control memory footprint...
                                    # cs.clear_cache()
                                    end = time.time()
                                    logger.log_print("took {t:.3f}s", t=end - start)
                                    # raise Exception("woop")

                    # raise Exception("...")

                    if self.use_cached_representations:
                        try:
                            checkpointer['representations'] = H
                        except KeyError:
                            pass

            return H

    def _take_subham(self, rep, inds):
        """
        Builds a subsampled version of a representation Hamiltonian
        to allow equations to be efficiently solved in subspaces.

        :param rep: representation matrix from which to pull the subspace
        :type rep: SparseArray
        :param inds: indices for the subspace
        :type inds: np.ndarray
        :return:
        :rtype:
        """
        ind_pairs = np.array(list(itertools.product(inds, inds))).T
        return np.reshape(rep[ind_pairs], (len(inds), len(inds)))
    def _build_projector(self, inds):
        """
        Builds a projector where only inds will
        be included

        :param inds: indices for the subspace
        :type inds: np.ndarray
        :return:
        :rtype:
        """
        shp = self.representations[0].shape
        return SparseArray.from_data(
                (
                    np.ones(len(inds)),
                    (inds, inds)
                ),
                shape=shp
            )
    def _get_Pi0(self, degenerate_subspace, non_zero_cutoff=None, E0=None):
        # generate the perturbation operator
        e_vec_full = self.zero_order_energies
        if E0 is None:
            E0 = np.average(e_vec_full[degenerate_subspace]) # better to use the first or the average? Not clear...not even sure this is a useful codepath
        e_vec = e_vec_full - E0
        e_vec[degenerate_subspace] = 1
        if non_zero_cutoff is None:
            non_zero_cutoff = Settings.non_zero_cutoff
        zero_checks = np.where(np.abs(e_vec) < non_zero_cutoff)[0]
        if len(zero_checks) > 0:
            if isinstance(E0, (int, float, np.integer, np.floating)):
                Et = [E0]
            else:
                Et = E0
            bad_vec = np.concatenate([Et, e_vec_full[zero_checks]])
            if len(zero_checks) > 10:
                raise ValueError(
                    "degeneracies encountered: states {} and {} other states are degenerate (average energy: {} stddev: {})".format(
                        degenerate_subspace,
                        len(zero_checks),
                        np.average(bad_vec),
                        np.std(bad_vec)
                    ))
            else:
                raise ValueError(
                    "degeneracies encountered: states {} and {} are degenerate (average energy: {} stddev: {})".format(
                        degenerate_subspace,
                        zero_checks,
                        np.average(bad_vec),
                        np.std(bad_vec)
                    ))
        pi = 1 / e_vec
        pi[degenerate_subspace] = 0
        return SparseArray.from_diag(pi)
    #endregion

    #region Get Coupled Spaces
    def load_state_spaces(self):

        logger = self.logger

        with self.logger.block(tag='getting basis'):
            if self._coupled_states is None:
                self.logger.log_print('trying to load from checkpoint...')
                with self.checkpointer:
                    try:
                        if self.use_cached_basis:
                            self._coupled_states = self.checkpointer['coupled_states']
                    except (OSError, KeyError):
                        self._coupled_states = None
                    if self._coupled_states is None:
                        self.logger.log_print('fail to load, building instead...')
                        parallelizer = Parallelizer.lookup(self.parallelizer)
                        if parallelizer.nprocs > 1:
                            parallelizer.printer = self.logger.log_print
                            self.logger.log_print('parallelizing over {nproc} processors',
                                                  nproc=parallelizer.nprocs
                                                  )
                        with parallelizer:  # we put an outermost block here to just make sure everything is clean

                            start = time.time()
                            self._coupled_states = self.load_coupled_spaces()
                            # _ = [len(s) for s in [self.states] + list(self._coupled_states)]
                            end = time.time()

                            self.logger.log_print(
                                ['H({i}): {s}'.format(i=i, s=s) for i,s in enumerate([self.states] + list(self._coupled_states))]
                                + ["took {t}s..."],
                                t=round(end - start, 3)
                            )
                        # raise Exception('break')
                        if self.use_cached_basis:
                            try:
                                self.checkpointer['coupled_states'] = self._coupled_states
                            except KeyError:
                                pass

                    # raise Exception('break')

            elif len(self._coupled_states) != len(self.perts) - 1:
                raise ValueError("coupled states must be specified for all perturbations (got {}, expected {})".format(
                    len(self._coupled_states),
                    len(self.perts) - 1
                ))
            elif any(not isinstance(cs, (BasisStateSpace, SelectionRuleStateSpace)) for cs in self._coupled_states):
                self._coupled_states = [
                    BasisStateSpace(self.states.basis, cs, full_basis=self.full_basis)
                    if not isinstance(cs, (BasisStateSpace, SelectionRuleStateSpace))
                    else cs
                    for cs in self._coupled_states
                ]

            with logger.block(tag="precomputing coupled space indices"):
                # otherwise they get calculated twice
                start = time.time()
                for s in self._coupled_states:
                    if s is not None:
                        logger.log_print('generating indices for {s}', s=s)
                        new = s.indices

                # inds = [s.indices for s in self._coupled_states]
                end = time.time()
                logger.log_print(
                    [
                        "took {t:.3f}s"
                    ],
                    t=end - start
                )

            if self._total_space is None:
                with logger.block(tag="generating total space"):

                    start = time.time()

                    space_list = [self.states] + [s for s in self._coupled_states if s is not None]
                    self._total_space = BasisMultiStateSpace(np.array(space_list, dtype=object))
                    flat_space = self.states.take_unique().to_single(track_excitations=False)
                    for s in self._coupled_states:
                        if s is not None:
                            flat_space = flat_space.union(s.to_single(track_excitations=False), track_excitations=False)
                    # flat_space = self._total_space.to_single()
                    self._flat_space = flat_space.take_unique(track_excitations=False)
                    # raise Exception(
                    #     self._flat_space.find(
                    #         space_list[1].get_representation_brakets(other=None).bras
                    #     )
                    # )
                    self._total_dim = len(self.flat_total_space)

                    end = time.time()
                    logger.log_print(
                        [
                            "total coupled space dimension: {d} (contracted from {f})",
                            "took {t:.3f}s"
                        ],
                        d=self.total_space_dim,
                        f=len(flat_space),
                        t=end - start
                    )
                    # raise Exception("break")
            else:
                if self._flat_space is None:
                    with logger.block(tag="generating total space"):
                        start = time.time()
                        self._flat_space = self._total_space.to_single().take_unique()
                        self._total_dim = len(self._flat_space)

                        end = time.time()
                        logger.log_print(
                            [
                                "total coupled space dimension: {d}",
                                "took {t:.3f}s"
                            ],
                            d=self.total_space_dim,
                            t=end - start
                        )
                else:
                    self._total_dim = len(self._flat_space)

    def load_coupled_spaces(self):
        """
        Determines which states need to be coupled at which levels of correction
        to handle the PT
        :return:
        :rtype:
        """

        total_state_spaces = []
        # loop over the degenerate sets and build a full
        # set of connected states
        spaces=None
        simple_spaces = []
        for deg_group in self.degenerate_spaces:
            # self.logger.log_print('loading {g} coupled states...', g=deg_group.indices)
            if len(deg_group) > 1 and self.allow_sakurai_degs:
                raise NotImplementedError("True degeneracy handling needs some patches")
                second_deg = self._use_second_deg_PT(deg_group)
                deg_space = deg_group
                spaces = self.get_coupled_space(None, deg_space, second_deg,
                                                      allow_PT_degs=True, spaces=spaces)
                total_state_spaces.append(spaces)
            else:
                deg_space = None if self.allow_sakurai_degs else deg_group
                simple_spaces.append(deg_space)

        if len(simple_spaces) > 0:
            space = simple_spaces[0]
            for s in simple_spaces[1:]:
                space = space.union(s)

            # raise Exception(simple_spaces[0].full_basis)

            spaces = self.get_coupled_space(
                space,
                None, False,
                allow_PT_degs=self.allow_sakurai_degs,
                spaces=spaces,
                wavefunction_terms=self.state_space_terms,
                property_filter=self.target_property_rules,
                filter_spaces=self.state_space_filters
                )
            total_state_spaces.append(spaces)

        coupled_states = [spaces[h][1] if spaces[h] is not None else None for h in self.perts]

        return coupled_states[1:]

    def _use_second_deg_PT(self, deg_group, degeneracy_cutoff=1e-8): # within a few wavenumbers or so
        """
        Diagonalizes the perturbations in the degenerate subspace to
        get a cleaner basis in which to do the perturbation theory.
        Used here only to first order to determine whether or not to do 2nd order or 1st order
        degenerate PT
        This comes from Sakurai.

        :param deg_group:
        :type deg_group: BasisStateSpace
        :return:
        :rtype: tuple[Iterable[SparseArray], SparseArray, SparseArray]
        """

        small_space = deg_group.get_representation_brakets()
        N_D = len(deg_group)
        # raise Exception(deg_group.excitations, deg_group.indices)
        subham = self.perts[1][small_space]
        # raise Exception(subham)
        subham = subham.reshape((N_D, N_D))

        new_eng1, deg_transf = np.linalg.eigh(subham)

        diffs = np.diff(new_eng1)
        if (np.abs(diffs) < degeneracy_cutoff).any():
            return True
        else:
            return False

    class StateSpaceWrapper:
        """
        Wraps a state space so that it can define stuff like __add__, __mul__, and __neg__
        """
        def __init__(self, space):
            self.space = space
        def __neg__(self):
            return self
        def simple_union(self, other):
            if (
                    isinstance(other, (int, np.integer, float, np.floating)) and other == 0
            ):
                return self
            if isinstance(other, type(self)):
                other = other.space
            return type(self)(self.space.union(other))
        def __sub__(self, other):
            return self.simple_union(other)
        def __rsub__(self, other):
            return self.simple_union(other)
        def __add__(self, other):
            return self.simple_union(other)
        def __radd__(self, other):
            return self.simple_union(other)
        # def __mul__(self, other):
        #     return type(self)(self.space.intersection(other))
    class ProjectionOperatorWrapper:
        """
        Generates a symbolic form of a perturbation operator that
        either projects onto a degenerate space or projects it out
        """
        def __init__(self, space, complement=False):
            self.space = space
            self.complement = complement
        def get_transformed_space(self, other):
            """
            :param other:
            :type other: SelectionRuleStateSpace
            :return:
            :rtype:
            """
            if self.complement:
                return other.drop_states(self.space)
            else:
                return other.take_states(self.space)
    class ProjectedOperator:
        """
        Generates a symbolic form of an operator where
        an operator can first be applied, then unused terms projected
        out, before returning the state space
        """
        def __init__(self, projector, operator):
            self.proj = projector
            self.op = operator

        def get_transformed_space(self, other):
            """
            :param other:
            :type other: BasisStateSpace
            :return:
            :rtype:
            """
            a = self.op
            if (
                    isinstance(a, (int, np.integer, float, np.floating)) and a == 0
            ):
                return None

            wtf1 = self.op.get_transformed_space(other)
            contracted = self.proj.get_transformed_space(wtf1)
            return contracted

    def _could_be_a_space(self, test): # nasty checking code that I don't want to redupe all the time
        if isinstance(test, (BasisStateSpace, BasisMultiStateSpace)):
            return True
        else:
            try:
                lt = len(test)
            except TypeError:
                return False

            try:
                if lt > 0 and isinstance(test[0], (int, np.integer)):
                    return True
                else:
                    lt = len(test[0])
                    if lt > 0 and isinstance(test[0][0], (int, np.integer)):
                        return True
                    else:
                        return False
            except TypeError:
                return False

    def _could_be_rules(self, test):
        # selection rule options
        if test is None:
            return True

        try:
            lt = len(test)
        except TypeError:
            return False

        if lt == 0:
            return True

        try:
            lt = len(test[0])
            res = (
                    lt == 0
                    or isinstance(test[0][0], (int, np.integer))
            )
        except TypeError:
            return False

        return res

    def _could_be_a_prefilter(self, test):
        return (
            test is None
            or self._could_be_a_space(test)
            or (
                    len(test) == 2 and
                    test[0] is None or self._could_be_a_space(test[0])
                    and self._could_be_rules(test[1])
            )
        )

    def _apply_transformation_with_filters(self, a, b, filter_space:PerturbationTheoryStateSpaceFilter, **opts):

        if filter_space is not None:
            prefilters = filter_space.prefilters
            postfilter = filter_space.postfilter
        else:
            prefilters = None
            postfilter = None

        if prefilters is not None:
            # this means we are able to filter _before_ we apply the selection rules
            # which we do by iteratively breaking the space to transform up into
            # chunks that match the prefilters and applying the associated selection
            # rules

            new = None # the new space we will slowly build
            b_remainder = b #.as_sorted()

            for n, (filter_space, filter_rules) in enumerate(prefilters):

                # take the intersection/remainder
                if filter_space is None:
                    b = b_remainder
                    b_remainder = None
                elif len(b_remainder) > 0:
                    b = b_remainder.intersection(filter_space)
                    if n < len(prefilters): # just a cheap opt...
                        b_remainder = b_remainder.difference(filter_space)
                else:
                    b = b_remainder

                if len(b) > 0:
                    if new is None:
                        new = a.get_transformed_space(b, rules=filter_rules, **opts)
                    else:
                        new = new.union(
                            a.get_transformed_space(b, rules=filter_rules, **opts)
                        )

        else:  # no prefilters
            new = a.get_transformed_space(b, **opts)

        if new is not None: # possible to have None if we had to do no work
            if postfilter is not None:
                if not isinstance(postfilter, (BasisStateSpace, BasisMultiStateSpace)):
                    postfilter = BasisStateSpace(b.basis, postfilter)
                if isinstance(postfilter, SelectionRuleStateSpace):
                    postfilter = postfilter.to_single().take_unique()
                new = new.take_states(postfilter)

        return new

    def _get_new_coupled_space(self, a, b, spaces=None, ret_space=True, filter_space=None):
        """
        A symbolic version of the dot product appropriate for getting
        transformed state spaces under the operation of a on b

        :param a:
        :type a:
        :param b:
        :type b: SelectionRuleStateSpace
        :param spaces:
        :type spaces: the set of operators to which we can assign transformations
        :return:
        :rtype: StateSpaceWrapper
        """
        if spaces is None:
            raise ValueError("...spaces shouldn't be None")

        if isinstance(b, self.StateSpaceWrapper):
            b = b.space

        if isinstance(a, self.ProjectedOperator):
            op = a.op
        else:
            op = a
        if (
                isinstance(op, (int, np.integer, float, np.floating)) and op == 0
                or isinstance(b, (int, np.integer, float, np.floating)) and b == 0
                or op is None
                or b is None
        ):
            return 0

        logger = self.logger
        if isinstance(a, self.StateSpaceWrapper):
            raise NotImplementedError("we shouldn't be here")
            new = a * b
        elif isinstance(a, self.ProjectionOperatorWrapper):
            # uhhh...
            new = a.get_transformed_space(b, parallelizer=self.parallelizer, logger=logger)
        elif isinstance(a, (self.ProjectedOperator, Representation)):
            cur = spaces[op] #type: SelectionRuleStateSpace
            proj = None if not isinstance(a, self.ProjectedOperator) else a.proj
            if cur is None:

                new = self._apply_transformation_with_filters(
                    a, b, filter_space,
                    track_excitations= not self.memory_constrained,
                    parallelizer=self.parallelizer, logger=logger
                )

                # we track not only the output SelectionRuleStateSpace
                # but also which projection operators have been applied
                # so that we can make sure we calculate any pieces that
                # need to be calculated

                spaces[op] = (
                    {proj:b},
                    new
                )
                # reduce to a single space to feed to the next round
                # of terms

                new = new.to_single().take_unique()
            else:
                projections, cur = cur
                # figure out what stuff we've already calculated
                rep_space = projections[None] if None in projections else None
                if proj is not None:
                    sub_rep = projections[proj] if proj in projections else None
                    if sub_rep is not None:
                        if rep_space is not None:
                            rep_space = rep_space.union(sub_rep, track_excitations=False)
                        else:
                            rep_space = sub_rep

                if rep_space is None:
                    # means we can't determine which parts we have and have not calculated
                    # so we calculate everything and associate it to proj

                    new = self._apply_transformation_with_filters(
                        a, b, filter_space,
                        track_excitations=not self.memory_constrained,
                        parallelizer=self.parallelizer, logger=logger
                    )

                    cur = cur.union(new)
                    projections[proj] = b
                    spaces[op] = (projections, cur)
                    # reduce to a single space to feed to the next round
                    # of terms
                    new = new.to_single(track_excitations=not self.memory_constrained).take_unique()
                else:
                    # means we've potentially calculated some of this already,
                    # so we figure out what parts we've already calculated in this
                    # projected space (rep_space is the current space of the representations)
                    diffs = b.difference(rep_space)
                    # if diffs.full_basis is None:
                    #     raise ValueError(diffs.full_basis, b.full_basis)
                    if len(diffs) > 0:
                        # raise Exception(projections, rep_space, diffs)
                        # we have an initial space we've already transformed, so we
                        # make sure not to recompute that
                        b_sels = SelectionRuleStateSpace(b, [], ignore_shapes=True)  # just some type fuckery
                        existing = cur.intersection(b_sels, handle_subspaces=False)
                        # and now we do extra transformations where we need to


                        if len(diffs) > 0:
                            new_new = self._apply_transformation_with_filters(
                                a, diffs, filter_space,
                                track_excitations=not self.memory_constrained,
                                parallelizer=self.parallelizer, logger=logger
                            )

                            if new_new is None:
                                new = b
                            else:
                                # next we add the new stuff to the cache
                                cur = cur.union(new_new)
                                projections[proj] = rep_space.union(diffs)
                                spaces[op] = (projections, cur)

                                # TODO: find a way to make this not cause memory spikes...
                                if ret_space:
                                    new = existing.union(new_new).to_single(track_excitations=not self.memory_constrained).take_unique()
                                else:
                                    new = b
                        else:
                            new = b

                    else:
                        # means we already calculated everything
                        # so we don't need to worry about this
                        b_sels = SelectionRuleStateSpace(b, [], ignore_shapes=True) # just some type fuckery
                        new = cur.intersection(b_sels, handle_subspaces=False)
                        new = new.to_single().take_unique()

        else:
            raise TypeError("don't know what to do with {} and {}".format(a, b))

        return self.StateSpaceWrapper(new)

    def _reduce_new_coupled_space(self, *terms,
                                  spaces=None,
                                  ret_space=True,
                                  filter_space=None
                                  ):
        """
        Reduces through `_get_new_coupled_space` from right to left
        :param terms:
        :type terms: Iterable[SelectionRuleStateSpace]
        :param spaces:
        :type spaces: dict | None
        :return:
        :rtype:
        """
        import functools
        return functools.reduce(
            lambda a, b:self._get_new_coupled_space(b, a, spaces, ret_space=ret_space, filter_space=filter_space),
            reversed(terms[:-1]),
            terms[-1]
        )

    def get_coupled_space(self,
                            input_state_space,
                            degenerate_space,
                            use_second_deg,
                            allow_PT_degs=True,
                            wavefunction_terms=None,
                            spaces=None,
                            property_filter=None,
                            filter_spaces=None
                            ):
        """
        Applies the VPT equations semi-symbolically, dispatching based on how many
        degeneracies we need to handle

        :return:
        :rtype:
        """

        if not allow_PT_degs:
            spaces = self.get_nondeg_coupled_space(
                input_state_space,
                degenerate_space,
                spaces=spaces,
                wavefunction_terms=wavefunction_terms,
                property_filter=property_filter,
                filter_spaces=filter_spaces
            )
        else:
            raise NotImplementedError("True degeneracy handling is getting a rewrite")
            if degenerate_space is None:
                spaces = self.get_nondeg_coupled_space(input_state_space, spaces=spaces)
            elif not use_second_deg:
                spaces = self.get_deg_coupled_space(degenerate_space, spaces=spaces)
            else:
                spaces = self.get_second_deg_coupled_space(degenerate_space, spaces=spaces)

        # raise Exception(spaces)
        return spaces

    def get_nondeg_coupled_space(self,
                                 input_state_space,
                                 degenerate_space=None,
                                 spaces=None,
                                 wavefunction_terms=None,
                                 property_filter=None,
                                 filter_spaces=None
                                 ):
        """
        Applies the non-degenerate equations in semi-symbolic form to determine
        which states needs to be calculated.
        This will always be the initial input to a calculation and then
        certain additional states may be calculated on the fly if they are needed to handle
        truly degenerate stuff.
        The only difference there will be to add on

        :param input_state_space:
        :type input_state_space: BasisStateSpace
        :param degenerate_space:
        :type degenerate_space: BasisStateSpace
        :param spaces:
        :type spaces:
        :param wavefunction_terms: which terms to include when calculating corrections
        :type wavefunction_terms: None | Iterable[Iterable[int]]
        :param property_filter: a set of states and selection rules to allow for being targeted in state to calculate
        :type property_filter: None | Iterable[Iterable[int], Iterable[Iterable[int]]]
        :return:
        :rtype:
        """

        # holder for perts to map to their stored states
        spaces = {h:None for h in self.perts} if spaces is None else spaces
        # final state spaces for each energy, corr, overlap, etc.
        input_wrapper = self.StateSpaceWrapper(input_state_space)

        order = self.state_space_iterations if self.state_space_iterations is not None else self.order
        order = order+1
        E = [None]*order
        E[0] = input_wrapper
        corrs = [None]*order
        corrs[0] = input_wrapper

        D = degenerate_space
        if D is None:
            D = input_state_space
        pi = self.ProjectionOperatorWrapper(D, complement=True)
        # piD = self.ProjectionOperatorWrapper(D, complement=False)

        dot = lambda *terms, spaces=spaces,ret_space=True,filter_space=None: self._reduce_new_coupled_space(*terms,
                                                                                          spaces=spaces,
                                                                                          ret_space=ret_space,
                                                                                          filter_space=filter_space
                                                                                          )

        H = self.PastIndexableTuple(self.perts)

        if property_filter is None:
            # This is intentionally written to parallel the non-degenerate VPT equations
            for k in range(1, order):
                ####################################################################################################
                #                                       *IMPLEMENTATION NOTE*
                # The states for the energy end up being totally subsumed in the states
                # for the corrections, so we just leave this part off
                ####################################################################################################
                #         En^(k) = <n^(0)|H^(k)|n^(0)> + sum(<n^(0)|H^(k-i)|n^(i)> - E^(k-i)<n^(0)|n^(i)>, i=1...k-1)
                # but we drop the shift part because that doesn't affect the state space at all
                # E[k] = sum(
                #             dot(self.ProjectedOperator(piD, H[k - i]), corrs[i])
                #             for i in range(0, k)
                #             )

                #   <n^(0)|n^(k)> = -1/2 sum(<n^(i)|n^(k-i)>, i=1...k-1)
                #         |n^(k)> = sum(Pi_n (En^(k-i) - H^(k-i)) |n^(i)>, i=0...k-1) + <n^(0)|n^(k)> |n^(0)>
                # but we drop the energy and overlap parts of this because they don't affect the overall state space

                self.logger.log_print(
                    'getting states for ' +
                        '+'.join('H({})|n({})>'.format(k-i, i)
                                 for i in range(0, k)
                                 if (
                                         not isinstance(H[k - i], (int, np.integer))
                                         and (wavefunction_terms is None or (k-i, i) in wavefunction_terms)
                                 )
                                 )
                    )

                with self.logger.block(tag='getting states for order {k}'.format(k=k)):
                    corrs[k] = sum(corrs[i] for i in range(0, k)) # this all in here from energies
                    for i in range(0, k):
                        if not isinstance(H[k - i], (int, np.integer)):
                            if wavefunction_terms is None or (k-i, i) in wavefunction_terms:
                                self.logger.log_print('H({a})|n({b})>', a=k - i, b=i)
                                if k < order-1:
                                    corrs[k] += dot(H[k - i], corrs[i],
                                                    filter_space=filter_spaces[(k-i, i)] if filter_spaces is not None and (k-i, i) in filter_spaces else None
                                                    )
                                else:
                                    dot(H[k - i], corrs[i], ret_space=False,
                                                    filter_space=filter_spaces[(k-i, i)] if filter_spaces is not None and (k-i, i) in filter_spaces else None
                                                    )
        else:
            raise NotImplementedError("property filters not here yet")

        # else:
        #     # indicates we were given target property terms to work with so rather
        #     # than just allowing us to recursively build up we use the selection
        #     # rules for these terms to determine which selection rules we actually
        #     # need to calculate both the energy and target property correctly
        #     target_states, target_sel_rules = property_filter
        #     exc = input_state_space.excitations
        #     if isinstance(target_states, (int, np.integer)):
        #         target_states = (target_states,)
        #     else:
        #         target_states = tuple(target_states)
        #     target_rules = exc[:, np.newaxis, :] - exc[np.newaxis, target_states, :]
        #
        #     # Now at each order we determine which selection rule terms we'll need
        #     all_sels = [
        #         [ [] ]
        #         if i == 0 else h.selection_rules for i,h in enumerate(self.perts)
        #     ]
        #
        #     # we populate the entire list of selection rules before filtering
        #     # this tells us what _would_ be applied and then we'll filter it
        #     # down to see what _will_ be applied
        #     base_sel_paths = [
        #         [((), ())]
        #     ]
        #     for k in range(1, order):
        #         new_paths = []
        #         for i in range(0, k):
        #             if not isinstance(H[k - i], (int, np.integer)):
        #                 sels = all_sels[k-i]
        #                 if sels is None:
        #                     raise NotImplementedError("how are we here")
        #                 for cur_hams, cur_sels in base_sel_paths[i]:
        #                     new_paths.append((
        #                         cur_hams + (H[k - i],),
        #                         cur_sels + (sels,)
        #                     ))
        #         base_sel_paths.append(new_paths)
        #
        #     # basic filter coming from needing to calculate the energy correctly
        #     sel_filts = [
        #         LatticePathGenerator([x]) for x in all_sels
        #     ]
        #     for k in range(1, order):
        #         sel_terms = [a[1] for a in base_sel_paths[k]]
        #         ham_terms = [a[0] for a in base_sel_paths[k]]
        #         for i in range(0, k):
        #             main_terms = LatticePathGenerator(sel_terms[i])
        #             for n,terms in enumerate(sel_filts[1:k+1]):
        #                 n+=1
        #                 # we figure out which sets of raising/lowering
        #                 # operations will get us from start to finish
        #                 if not isinstance(terms, list):
        #                     terms = [terms]
        #
        #                 # then we turn these into proper selection rules and
        #                 # determine which selection rules will go with which
        #                 # operators
        #                 for t in terms:
        #                     bit_strings = main_terms.find_intersections(t)
        #                     print(bit_strings)
        #                     # for now we'll just let this mean we need these specific
        #                     # selection rules but in the future we'll be a bit more
        #                     # targeted in terms of the terms we need to calculate
        #                     for b in bit_strings:
        #                         new_space = input_state_space
        #                         for h, j, r in zip(ham_terms[i], b, main_terms.steps):
        #                             if len(r[j]) > 0:
        #                                 if isinstance(new_space, SelectionRuleStateSpace):
        #                                     new_space = new_space.to_single().take_unique()
        #                                 new_space = new_space.apply_selection_rules([r[j]])
        #                                 if new_space is None:
        #                                     break
        #
        #                                 if spaces[h] is None:
        #                                     spaces[h] = (None, new_space)
        #                                 else:
        #                                     spaces[h] = (None, spaces[h][1].union(new_space))
        #                             elif spaces[h] is None:
        #                                 spaces[h] = (None, new_space)
        #
        #
        # raise Exception(spaces)
        #
        #     # filter needed to get _excitations_ correctly
        #
        # raise Exception(spaces)

        return spaces

    def get_deg_coupled_space(self, degenerate_space, spaces=None):
        """
        Applies the degenerate equations in semi-symbolic form to determine
        which states needs to be calculated at which orders of correction

        :return:
        :rtype:
        """

        # raise NotImplementedError("haven't checked this yet...")

        D = degenerate_space

        # holder for perts to map to their stored states
        spaces = {h:None for h in self.perts} if spaces is None else spaces
        # final state spaces for each energy, corr, overlap, etc.
        input_wrapper = self.StateSpaceWrapper(D)
        order = self.order + 1
        E = [None] * (order + 1)
        E[0] = input_wrapper
        corrs = [None] * order
        corrs[0] = input_wrapper

        piU = self.ProjectionOperatorWrapper(D, complement=True)
        piD = self.ProjectionOperatorWrapper(D, complement=False)
        dot = lambda *terms, spaces=spaces: self._reduce_new_coupled_space(*terms, spaces=spaces)

        H = self.PastIndexableTuple(self.perts)

        for k in range(1, order):
            # Pu |n^(k)>
            corrs[k] = sum(
                (
                   dot(piU, corrs[i]) + dot(piU, H[k - i], corrs[i])
                    for i in range(0, k)
                ),
                corrs[0]
            )

            corrs[k] = (
                    sum(dot(piU, corrs[i]) for i in range(0, k))
                    + sum(dot(piD, H[k - i], corrs[i]) for i in range(0, k))
            )

            # E^(k+1)
            E[k+1] = sum(dot(piD, H[k+1-i], corrs[i]) for i in range(0, k+1))
            # PD|n^(k)>
            corrs[k] += sum(
                (
                   dot(piU, corrs[i]) + dot(piU, H[k+1-i], corrs[i])
                    for i in range(0, k+1)
                ),
                corrs[0]
            )

        return spaces
    def get_second_deg_coupled_space(self, degenerate_space, spaces=None):
        """
        Does the dirty work of doing the VPT iterative equations.
        Needs to be adapted to include the two types of degeneracies that can
        be introduced in Sakurai's approach.

        :return:
        :rtype:
        """

        D = degenerate_space

        # holder for perts to map to their stored states
        spaces = {h:None for h in self.perts} if spaces is None else spaces
        # final state spaces for each energy, corr, overlap, etc.
        input_wrapper = self.StateSpaceWrapper(D)
        order = self.order + 1
        E = [None] * (order + 2)
        E[0] = input_wrapper
        corrs = [None] * order
        corrs[0] = input_wrapper

        piU = self.ProjectionOperatorWrapper(D, complement=True)
        piV = self.ProjectionOperatorWrapper(D, complement=False)
        piG = piV
        dot = lambda *terms, spaces=spaces: self._reduce_new_coupled_space(*terms, spaces=spaces)

        H = self.PastIndexableTuple(self.perts)

        for k in range(1, order):  # to actually go up to the total order
            # we condense the corr equations to reflect
            # only the important stuff
            corrs[k] = (
                    sum(dot(piU, corrs[i]) for i in range(0, k))
                    + sum(dot(piU, H[k - i], corrs[i]) for i in range(0, k))  # goes up to k-1
            )

            # Pv |n^(k)>
            corrs[k] += (
                    sum(dot(piV, corrs[i]) for i in range(0, k))
                    + sum(dot(piV, H[k + 1 - i], corrs[i]) for i in range(0, k+1))  # goes up to k
            )

            corrs[k] += (
                    sum(dot(piG, corrs[i]) for i in range(0, k + 1))
                    + sum(dot(piG, H[k + 2 - i], corrs[i]) for i in range(0, k + 1))  # goes up to k
                    + sum(dot(piG, H[1], piU, corrs[i]) for i in range(0, k + 1))
                    + sum(dot(piG, H[1], piU, H[k + 1 - i], corrs[i]) for i in range(0, k + 1))
            )

            E[k+2] = (
                    sum(
                        dot(H[k + 2 - i], corrs[i])
                        for i in range(0, k+1)
                    )
                    + sum(
                        dot(H[k + 1 - i], corrs[i])
                        for i in range(0, k+1)
                    )
            )

        return spaces
    #endregion

    #region Apply Equations

    def get_corrections(self,
                        non_zero_cutoff=None,
                        handle_strong_couplings=None,
                        check_overlap=True
                        ):
        """
        Applies the perturbation theory equations to obtain
        corrections to the wave functions and energies

        :return:
        :rtype:
        """
        # We use the iterative equations
        #            En^(k) = <n^(0)|H^(k)|n^(0)> + sum(<n^(0)|H^(k-i)|n^(i)> - E^(k-i)<n^(0)|n^(i)>, i=1...k-1)
        #     <n^(0)|n^(k)> = -1/2 sum(<n^(i)|n^(k-i)>, i=1...k-1)
        #           |n^(k)> = sum(Pi_n (En^(k-i) - H^(k-i)) |n^(i)>, i=1...k-1) + <n^(0)|n^(k)> |n^(0)>
        #  where Pi_n is the perturbation operator [1/(E_m-E_n) for m!=n]

        perturbations = self.representations
        states = self.states
        order = self.order

        flat_total_space = self.flat_total_space
        N = self.total_space_dim

        checkpointer = self.checkpointer
        logger = self.logger

        degenerate_states = self.degenerate_spaces
        if handle_strong_couplings is None:
            handle_strong_couplings = self.handle_strong_couplings
        # degenerate_states = None,
        # degeneracy_mode = None,
        # logger = None,
        # checkpointer = None,

        if non_zero_cutoff is None:
            non_zero_cutoff = Settings.non_zero_cutoff

        return self._get_corrections(
            perturbations,
            states,
            order,
            flat_total_space,
            N,
            checkpointer,
            logger,
            degenerate_states,
            handle_strong_couplings=handle_strong_couplings,
            non_zero_cutoff=non_zero_cutoff
        )

    def _get_corrections(self,
                         perturbations,
                         states,
                         order,
                         flat_total_space,
                         N,
                         checkpointer,
                         logger,
                         degenerate_states,
                         non_zero_cutoff=None,
                         handle_strong_couplings=None
                         ):
        """
        Just exists so we can do a recursion on this...

        :param perturbations:
        :type perturbations:
        :param states:
        :type states:
        :param order:
        :type order:
        :param flat_total_space:
        :type flat_total_space:
        :param N:
        :type N:
        :param checkpointer:
        :type checkpointer:
        :param logger:
        :type logger:
        :param degenerate_states:
        :type degenerate_states:
        :param non_zero_cutoff:
        :type non_zero_cutoff:
        :param handle_strong_couplings:
        :type handle_strong_couplings:
        :return:
        :rtype:
        """

        # checkpointer['indices'] = self.total_state_space
        with checkpointer:

            all_energies = np.zeros((len(states), order + 1))
            all_overlaps = np.zeros((len(states), order + 1))
            all_corrs = np.zeros((len(states), order + 1, N))
            all_energy_corrs = np.full((len(states), order + 1), None, dtype=object)

            with logger.block(tag="Applying Perturbation Theory"):
                logger.log_print(
                    [
                        "order: {o}",
                        "states: {n}",
                        "deg. spaces: {d}",
                        'deg. handling: {dm}',
                    ],
                    o=order,
                    n=len(states.indices),
                    d=len([1 for x in degenerate_states if len(x) > 1]),
                    dm=(
                        'Sakurai' if self.allow_sakurai_degs else
                        'mod. H' if self.drop_perturbation_degs else
                        'standard'
                    )
                )
                start = time.time()

                _ = []
                for deg_group in degenerate_states:
                    if not hasattr(deg_group, 'indices'):
                        deg_group = BasisStateSpace(flat_total_space.basis, deg_group, full_basis=self.full_basis)
                    deg_group.deg_find_inds = None
                    _.append(deg_group)
                degenerate_states = _

                if self.drop_perturbation_degs:
                    dropped_els, perturbations = self.drop_deg_pert_els(perturbations, degenerate_states)
                    for deg_group in degenerate_states:
                        for n in deg_group.indices:
                            d2 = deg_group.take_states([n])
                            d2.deg_find_inds = None
                            energies, overlaps, corrs, ecorrs = self.apply_VPT_equations(n, deg_group,
                                                                                 None, None, None, None,
                                                                                 allow_PT_degs=False,
                                                                                 non_zero_cutoff=non_zero_cutoff,
                                                                                 perturbations=perturbations
                                                                                 )

                            res_index = states.find(n)
                            all_energies[res_index] = energies
                            all_energy_corrs[res_index] = ecorrs
                            all_corrs[res_index] = corrs
                            all_overlaps[res_index] = overlaps
                else:
                    # loop over the degenerate sets
                    for deg_group in degenerate_states:
                        # we use this to build a pertubation operator that removes
                        # then entire set of degenerate states
                        deg_inds = flat_total_space.find(deg_group)
                        if len(deg_group) > 1:
                            if self.allow_sakurai_degs:
                                deg_engs, zero_order_states, main_zero_states, subspaces = self._get_deg_eq_inputs(deg_inds)
                                main_subspace = (np.array([d[0] for d in deg_engs]), main_zero_states)
                            else:
                                deg_engs = zero_order_states = subspaces = main_subspace = [None]*len(deg_group)
                        else:
                            deg_engs = zero_order_states = subspaces = main_subspace = [None]

                        res_inds = states.find(deg_group.indices)
                        for n, res_index, de, zo, s in zip(deg_group.indices, res_inds, deg_engs, zero_order_states, subspaces):
                            energies, overlaps, corrs, ecorrs = self.apply_VPT_equations(n, deg_group, de, zo, main_subspace, s,
                                                                                 allow_PT_degs=self.allow_sakurai_degs,
                                                                                 non_zero_cutoff=non_zero_cutoff,
                                                                                 perturbations=perturbations
                                                                                 )

                            all_energies[res_index] = energies
                            all_energy_corrs[res_index] = ecorrs
                            all_corrs[res_index] = corrs
                            all_overlaps[res_index] = overlaps

                        # now we reorthogonalize degenerate states
                        if not self.intermediate_normalization:
                            for k in range(1, order+1):
                                for i, (n, x) in enumerate(zip(res_inds, deg_inds)):
                                    for m, y in zip(res_inds[i + 1:], deg_inds[i+1:]):
                                        onn = -1 / 2 * np.sum(np.dot(all_corrs[n][i], all_corrs[n][k - i]) for i in range(1, k))
                                        onm = -1 / 2 * np.sum(np.dot(all_corrs[n][i], all_corrs[m][k - i]) for i in range(1, k))
                                        omm = -1 / 2 * np.sum(np.dot(all_corrs[m][i], all_corrs[m][k - i]) for i in range(1, k))
                                        all_corrs[n][k][x] = onn
                                        all_corrs[n][k][y] = onm
                                        all_corrs[m][k][x] = onm
                                        all_corrs[m][k][y] = omm

                end = time.time()
                logger.log_print(
                    "took {t}s",
                    t=round(end - start, 3)
                )

            # raise Exception(
            #     (np.sum(all_energies, axis=1) * UnitsData.convert("Hartrees", "Wavenumbers") - 4605.5)
            # )

            # now we recompute reduced state spaces for use in results processing
            # and we also convert the correction vectors to sparse representations
            tci = flat_total_space.indices
            N = len(tci)
            nstates = len(all_corrs)

            corr_mats, corr_inds = PerturbationTheoryCorrections.create_coupling_matrix(
                all_corrs,
                states,
                flat_total_space,
                nstates,
                order,
                non_zero_cutoff=non_zero_cutoff,
                # filters=self.state_space_filters
                filters=None
            )

            cs_states = SelectionRuleStateSpace(states, corr_inds, None)
            total_states = self.flat_total_space
            corrs = PerturbationTheoryCorrections.from_dicts(
                {
                    "states": states,
                    "coupled_states": cs_states,
                    "total_states": total_states,
                    "degenerate_states": degenerate_states
                },
                {
                    "energies": all_energies,
                    'energy_corrections':all_energy_corrs,
                    "wavefunctions": corr_mats,
                    "degenerate_transformation": None,
                    "degenerate_energies": None
                },
                perturbations # we probably want to ditch this for memory reasons...
                , logger=self.logger
            )


            if (
                    degenerate_states is None
                    or all(len(x) == 1 for x in degenerate_states)
            ):

                if handle_strong_couplings is True or handle_strong_couplings is False:
                    degenerate_correction_threshold = .5
                elif not isinstance(handle_strong_couplings, (int, float, np.integer, np.floating)):
                    degenerate_correction_threshold=.5
                else:
                    degenerate_correction_threshold = handle_strong_couplings
                    handle_strong_couplings = handle_strong_couplings > 0

                sc = corrs.find_strong_couplings(threshold=degenerate_correction_threshold)
                if len(sc) > 0:
                    with self.logger.block(tag="Strongly coupled states (threshold={})".format(degenerate_correction_threshold)):
                        self.logger.log_print(
                            corrs.format_strong_couplings_report(sc, join=False)
                        )

                        if handle_strong_couplings and (sc is not None and len(sc) > 0):
                            sc = corrs.collapse_strong_couplings(sc)
                            degenerate_states = DegenerateMultiStateSpace.from_spec({'couplings':sc}, solver=self)
                            with self.logger.block(tag="Redoing PT with strong couplings handled (degs={})".format(degenerate_states)):
                                return self._get_corrections(
                                    perturbations,
                                    states,
                                    order,
                                    flat_total_space,
                                    N,
                                    checkpointer,
                                    logger,
                                    degenerate_states,
                                    handle_strong_couplings=False,
                                    non_zero_cutoff=non_zero_cutoff
                                )
            else:
                sc = None


            # raise Exception(sc)

            if self.results is None or isinstance(self.results, NullCheckpointer):
                try:
                    checkpointer['corrections'] = {
                        "states": states.excitations,
                        "total_states": total_states.excitations,
                        'energies': all_energies,
                        'wavefunctions': corr_mats
                    }
                except KeyError:
                    pass
            else:
                with self.results:
                    try:
                        self.results['corrections'] = {
                            "states": states.excitations,
                            "total_states": total_states.excitations,
                            'energies': all_energies,
                            'wavefunctions': corr_mats
                        }
                    except KeyError:
                        pass

            # with self.logger.block(tag="overlap matrix"):
            #     self.logger.log_print(str(np.sum(corrs.get_overlap_matrices(), axis=0)).splitlines())

        return self.PTResults(corrs, degenerate_states)

    PTResults = namedtuple("PTResults", ["corrections", "degeneracies"])

    @staticmethod
    def _safe_dot(a, b):
        # generalizes the dot product so that we can use 0 as a special value...
        if (
                isinstance(a, (int, np.integer, float, np.floating)) and a == 0
                or isinstance(b, (int, np.integer, float, np.floating)) and b == 0
        ):
            return 0

        if isinstance(a, np.ndarray):
            doots = np.dot(a, b)
        else:
            doots = a.dot(b)

        if isinstance(b, np.ndarray) and isinstance(doots, SparseArray):
            doots = doots.asarray()

        return doots
    def apply_VPT_equations(self,
                            state_index,
                            degenerate_space_indices,
                            degenerate_energies,
                            zero_order_state,
                            degenerate_subspace,
                            degenerate_subsubspace,
                            perturbations=None,
                            allow_PT_degs=None,
                            ignore_odd_orders=None,
                            intermediate_normalization=None,
                            non_zero_cutoff=None
                            ):
        """
        Applies VPT equations, dispatching based on how many
        degeneracies we need to handle

        :param state_index: the index of the primary state being treated using the PT
        :type state_index: int
        :param degenerate_space_indices: the indices corresponding to degeneracies with the primary state in the zero-order picture
        :type degenerate_space_indices: np.ndarray[int]
        :param degenerate_energies: the first and (possibly) second order correction to the energies
        :type degenerate_energies: Iterable[float | None]
        :param zero_order_states: the vector for the proper zero-order state corresponding ot state_index
        :type zero_order_states: np.ndarray[float]
        :param degenerate_subsubspace: the set of vectors for the zero-order states in the secondary degenerate subspace
        :type degenerate_subsubspace: tuple[np.ndarray[float], np.ndarray[int]]
        :param non_zero_cutoff: cutoff for when a term can be called zero for performance reasons
        :type non_zero_cutoff: float
        :return:
        :rtype:
        """
        if non_zero_cutoff is None:
            non_zero_cutoff = Settings.non_zero_cutoff

        if ignore_odd_orders is None:
            ignore_odd_orders=self.ignore_odd_orders
        if allow_PT_degs is None:
            allow_PT_degs = self.allow_sakurai_degs
        if intermediate_normalization is None:
            intermediate_normalization = self.intermediate_normalization
        return self.apply_VPT_nondeg_equations(state_index, degenerate_space_indices, non_zero_cutoff=non_zero_cutoff,
                                               ignore_odd_orders=ignore_odd_orders,
                                               intermediate_normalization=intermediate_normalization,
                                               perturbations=perturbations
                                               )

    def apply_VPT_nondeg_equations(self,
                                   state_index,
                                   deg_group,
                                   perturbations=None,
                                   non_zero_cutoff=None,
                                   check_overlap=True,
                                   intermediate_normalization=False,
                                   ignore_odd_orders=False
                                   ):
        """
        Does the dirty work of doing the VPT iterative equations.

        :return:
        :rtype:
        """

        if non_zero_cutoff is None:
            non_zero_cutoff = Settings.non_zero_cutoff

        if intermediate_normalization:
            check_overlap=False

        n = state_index
        e_vec_full = self.zero_order_energies

        order = self.order
        total_state_space = self.flat_total_space

        energies = np.zeros((order + 1,), dtype=float)
        overlaps = np.zeros((order + 1,), dtype=float)
        energy_corrs = [None] * (order + 1)
        corrs = np.zeros((order + 1, len(total_state_space)), dtype=float)  # can I make this less expensive in general?

        # find the state index in the coupled subspace
        n_ind = total_state_space.find(n)

        logger = NullLogger() if self.logger is None else self.logger

        block_tag_formatter = lambda:'getting corrections for state {}/{}'.format(
            n,
            total_state_space.take_subspace([n_ind]).excitations[0]
        )

        with logger.block(
            tag=block_tag_formatter,
            log_level=logger.LogLevel.Debug
        ):
            D = deg_group
            deg_inds = (n_ind,)
            if D is not None:
                if D.deg_find_inds is None:
                    D.deg_find_inds = total_state_space.find(D)
                deg_inds = D.deg_find_inds
                if len(D) > 1:
                    logger.log_print('Degenerate space: {D}',
                                          D=D,
                                          preformatter=logger.preformat_keys({"D":lambda D:D.indices}),
                                          log_level=logger.LogLevel.Debug
                                          )
            E0 = e_vec_full[n_ind]
            pi = self._get_Pi0(deg_inds, E0=E0, non_zero_cutoff=non_zero_cutoff)

            energies[0] = E0
            logger.log_print('Zero-order energy: {e}',
                                  e=E0[0] * UnitsData.convert("Hartrees", "Wavenumbers"),
                                  log_level=logger.LogLevel.Debug
                                  )
            # self.logger.log_print("{n}: E0={E}", n=n_ind, E=E0)
            overlaps[0] = 1
            corrs[0, n_ind] = 1
            H = self.representations if perturbations is None else perturbations

            dot = self._safe_dot
            takeDiag = lambda h, n_ind: h[n_ind, n_ind] if not isinstance(h, (int, np.integer, float, np.floating)) else 0.
            take = lambda h, el: h[el] if not isinstance(h, (int, np.integer, float, np.floating)) else 0.
            for k in range(1, order + 1):  # to actually go up to target order
                #         En^(k) = <n^(0)|H^(k)|n^(0)> + sum(<n^(0)|H^(k-i)|n^(i)> - E^(k-i)<n^(0)|n^(i)>, i=1...k-1)
                if ignore_odd_orders and k % 2 == 1:
                    logger.log_print('Skipping order {k} for the energy (assumed to be 0)', k=k, log_level=logger.LogLevel.Debug)
                    energy_terms = None
                    Ek = 0
                # elif ignore_odd_orders: # Tried to get the 2n + 1 trick working but...it doesn't work?
                #     Ek = (
                #             takeDiag(H[k], n_ind)
                #             + sum(dot(take(H[k - i], n_ind), corrs[i]) - energies[k - i] * overlaps[i]
                #                   for i in range(1, (k + 1) // 2))
                #     )
                else:
                    energy_terms = [takeDiag(H[k], n_ind)] + [
                            dot(take(H[k - i], n_ind), corrs[i]) - energies[k - i] * overlaps[i]
                            for i in range(1, k)
                        ]
                    energy_terms = np.array([
                        x.flatten()[0] if not isinstance(x, (int, float, np.integer, np.floating)) else x
                        for x in energy_terms
                    ])
                    logger.log_print(
                        energy_terms,
                        message_prepper=lambda energy_terms:['Energy terms at order {k} in cm^-1:'] + [
                            '{} = {}'.format(s, e) for s, e in
                            zip(
                                ["<n(0)|H({})|n(0)>".format(k)] + [
                                    "<n(0)|H({0})-E({0})|n({1})>".format(
                                        k - i, i
                                    ) for i in range(1, k)
                                ],
                                energy_terms * UnitsData.convert("Hartrees", "Wavenumbers")
                            )
                        ],
                        k=k,
                        log_level=logger.LogLevel.Debug
                    )
                    Ek = np.sum(energy_terms)
                energy_corrs[k] = energy_terms
                energies[k] = Ek
                #   <n^(0)|n^(k)> = -1/2 sum(<n^(i)|n^(k-i)>, i=1...k-1)
                #         |n^(k)> = sum(Pi_n (En^(k-i) - H^(k-i)) |n^(i)>, i=0...k-1) + <n^(0)|n^(k)> |n^(0)>
                corrs[k] = sum(
                    dot(pi, energies[k - i] * corrs[i] - dot(H[k - i], corrs[i]))
                        if abs(energies[k - i]) > non_zero_cutoff else
                     -dot(pi, dot(H[k - i], corrs[i])) # just cut out a potentially unnecessary dense cast
                    for i in range(0, k)
                )

                if check_overlap:
                    should_be_zero = corrs[k][deg_inds]
                    if (should_be_zero > 0).any():
                        raise ValueError("Perturbation operator should have made overlap of state {} with {} zero...got {} instead".format(
                            n, D, should_be_zero
                        ))

                if intermediate_normalization:
                    ok = 0.0
                else:
                    ok = -1 / 2 * np.sum(dot(corrs[i], corrs[k - i]) for i in range(1, k))
                    logger.log_print([
                        'Overlap at order {k}:'
                        '<n(0)|n({k})> = {ok}'
                        ], k=k, ok=ok, log_level=logger.LogLevel.Debug)
                overlaps[k] = ok
                corrs[k][n_ind] = ok  # pi (the perturbation operator) ensures it's zero before this

            if check_overlap:
                # full_wfn = np.sum(corrs, axis=0)
                # ov = np.dot(full_wfn, full_wfn)
                ov_parts = [ [dot(corrs[k-i], corrs[i]) for i in range(k+1)] for k in range(order+1)]
                ov = sum(np.sum(v) for v in ov_parts)
                if abs(ov - 1) > .005:
                    raise ValueError(
                        "state {} isn't normalized (overlap = {}, bits {})".format(
                            state_index, ov, ov_parts
                        ))

        return energies, overlaps, corrs, energy_corrs

    def apply_VPT_2k1_rules(self,
                           existing_corrs,
                           perturbations=None
                           ):
        """
        Apply expressions allowing for obtaining higher-order
        corrections to the energies from lower-order corrections to the
        wavefunctions

        :param existing_corrs:
        :type existing_corrs:
        :param perturbations:
        :type perturbations:
        :return:
        :rtype:
        """
        ...

    #endregion

    #region Handle Post-PT Variational Stuff
    @staticmethod
    def _fmt_depert_engs(total_state_space, base_energies):

        def fmt(*a, base_energies=base_energies):
            states = total_state_space.excitations
            state_sums = np.sum(total_state_space.excitations, axis=1)
            gs_pos = np.where(state_sums == 0)
            label = "Deperturbed States/Energies:"
            if len(gs_pos) > 0:
                label = "Deperturbed States/Frequencies:"
                gs_pos = gs_pos[0]
                if len(gs_pos) > 0:
                    gs_pos = gs_pos[0]
                gs_eng = base_energies[gs_pos]
                base_energies = base_energies - gs_eng
                base_energies[gs_pos] = gs_eng

            ndim = len(states[0])
            fmt_str = "{:.0f} " * ndim + " {:12.4f}"

            return [label] + [
                fmt_str.format(*s, e)
                for s, e in zip(
                    total_state_space.excitations,
                    UnitsData.convert("Hartrees", "Wavenumbers") * base_energies
                )
            ]

        return fmt
    def apply_post_PT_variational_calc(self, degenerate_states, corrs):
        """
        Applies degenerate perturbation theory by building a representation
        for the degenerate terms in the Hamiltonian.
        This is then diagonalized, allowing the degenerate states to be expressed
        in the basis of non-degenerate states

        :param H:
        :type H: Iterable[SparseArray]
        :param corrs: the standard PerturbationTheory Corrections object that comes out of the application of non-deg PT
        :type corrs: PerturbationTheoryCorrections
        :param degenerate_states: population of degenerate states
        :type degenerate_states:
        :param logger:
        :type logger: Logger
        :return:
        :rtype:
        """
        # we pull the states and total states from the corrections object

        total_state_space = corrs.states  # type: BasisStateSpace

        # set up space to store the degenerate energies and rotations coming from the
        # sets of diagonalizations
        energies = np.zeros(len(total_state_space))
        base_energies = corrs.energies  # for when we're not rotating

        logger = NullLogger() if self.logger is None else self.logger
        logger.log_print(
            None,
            message_prepper=self._fmt_depert_engs(total_state_space, base_energies)
        )

        # this will be built from a series of block-diagonal matrices
        # so we store the relevant values and indices to compose the SparseArray
        rotations = []
        rotation_vals = []
        rotation_row_inds = []
        rotation_col_inds = []

        ndeg_ham_corrs = []
        for group in degenerate_states:
            # we apply the degenerate PT on a group-by-group basis
            # by transforming the H reps into the non-degenerate basis
            deg_inds = total_state_space.find(group)
            if len(deg_inds) == 1 or (self.gaussian_resonance_handling and np.max(np.sum(group.excitations, axis=1)) > 2):
                for i in deg_inds:
                    # we'll be a little bit inefficient for now and speed up later
                    energies[i] = base_energies[i]
                    rotation_vals.append([1.])
                    rotation_row_inds.append([i])
                    rotation_col_inds.append([i])
            elif len(deg_inds) > 1:
                H_nd, deg_engs, deg_rot = self.get_degenerate_rotation(group, corrs)
                ndeg_ham_corrs.append(H_nd)
                rotations.append(deg_rot)
                energies[deg_inds] = deg_engs
                rotation_vals.append(deg_rot.flatten())
                deg_rows, deg_cols = np.array([p for p in itertools.product(deg_inds, deg_inds)]).T
                rotation_row_inds.append(deg_rows)
                rotation_col_inds.append(deg_cols)
            else:
                self.logger.log_print("WARNING: got degeneracy spec that is not in total space")

        if self.results is None or isinstance(self.results, NullCheckpointer):
            try:
                self.checkpointer["degenerate_data"] = {
            "states": [d.excitations for d in degenerate_states],
            "energies": energies,
            "hamiltonians": ndeg_ham_corrs,
            "rotations": rotations
        }
            except KeyError:
                pass
        else:
            with self.results:
                try:
                    self.results["degenerate_data"] = {
                        "states": [d.excitations for d in degenerate_states],
                        "energies": energies,
                        "hamiltonians": ndeg_ham_corrs,
                        "rotations": rotations
                    }
                except KeyError:
                    pass

        rotation_vals = np.concatenate(rotation_vals)
        rotation_row_inds = np.concatenate(rotation_row_inds)
        rotation_col_inds = np.concatenate(rotation_col_inds)

        rotations = SparseArray.from_data(
            (
                rotation_vals,
                (
                    rotation_row_inds,
                    rotation_col_inds
                )
            ),
            shape=(len(energies), len(energies))
        )

        return energies, rotations
    def drop_deg_pert_els(self, perts, deg_groups):
        deg_grop_inds = []
        for g in deg_groups:
            if g.deg_find_inds is None:
                g.deg_find_inds = self.flat_total_space.find(g)
            deg_grop_inds.append(g.deg_find_inds)
        pert_blocks = []
        perts = self.PastIndexableTuple([perts[0]] + [p.copy() for p in perts[1:]])

        block_logger = NullLogger() if self.logger is None else self.logger

        with block_logger.block(tag='modifying perturbations', log_level=block_logger.LogLevel.Debug):
            for d,g in zip(deg_grop_inds, deg_groups):
                if len(d) > 1:
                    block_logger.log_print(
                        None,
                        lambda *a:["dropping elements coupling degenerate space:"] + str(g.excitations).splitlines(),
                        log_level=block_logger.LogLevel.Debug
                    )
                    idx = tuple(np.array([x for x in itertools.product(d, d) if x[0] != x[1]]).T)
                    els = []
                    for p in perts[1:]:
                        els.append(p[idx].flatten())
                        p[idx] = 0.
                    pert_blocks.append([idx, els])

                    triu = np.where(idx[0] > idx[1])
                    def pad_els(el, triu=triu):
                        e = np.zeros((len(d), len(d)))
                        e[np.triu_indices_from(e, k=1)] = el[triu]
                        e = np.round(e * UnitsData.convert("Hartrees", "Wavenumbers")).astype(int)
                        e[np.tril_indices_from(e, k=-1)] = e[np.triu_indices_from(e, k=1)]
                        return e
                    block_logger.log_print(
                        None,
                        message_prepper = lambda *a: ["zeroed out coupling elements:"] + sum(
                            (str(pad_els(e)).splitlines() for e in els),
                            []
                        ),
                        log_level=block_logger.LogLevel.Debug
                    )

        return pert_blocks, perts
    def get_transformed_Hamiltonians(self, corrs, deg_group=None):
        if deg_group is not None:
            subcorrs = corrs.take_subspace(deg_group)
            inds = self.flat_total_space.find(deg_group)
            subhams = [SparseArray.from_data(self._take_subham(H, inds)) for H in self.representations]
            # H_nd =[
            #     x.asarray() if isinstance(x, SparseArray) else x
            #     for x in subcorrs.operator_representation(subhams, subspace=deg_group)
            # ]
            H_nd = [
                x.asarray() if isinstance(x, SparseArray) else x
                for x in subcorrs.operator_representation(subhams, subspace=deg_group, logger_symbol="H")
            ]

        else:
            subhams = self.representations
            H_nd = [
                x.asarray() if isinstance(x, SparseArray) else x
                for x in corrs.operator_representation(subhams, logger_symbol="H", logger_conversion=UnitsData.convert("Hartrees", "Wavenumbers"))
            ]
        return H_nd
    def get_degenerate_rotation(self, deg_group, corrs):

        logger = self.logger

        with logger.block(tag="states"):
            logger.log_print(
                str(
                    corrs.states.take_states(deg_group).excitations
                ).splitlines()
            )

        subdegs = corrs.take_subspace(deg_group)

        # from McUtils.Scaffolding import JSONSerializer
        # import os
        # with open(os.path.expanduser("~/Desktop/wat6.json"), "w+") as woof:
        #     JSONSerializer().serialize(woof, subdegs)

        # H_nd = self.get_transformed_Hamiltonians(corrs, deg_group)
        # for h in H_nd[1:]:
        #     np.fill_diagonal(h, 0.)
        H_nd_corrs = self.get_transformed_Hamiltonians(subdegs, None)
        # import McUtils.Plots as plt
        # plt.TensorPlot(np.array(H_nd)).show()
        H_nd = np.sum(H_nd_corrs, axis=0)
        # overlaps = np.sum(subdegs.get_overlap_matrices(), axis=0)

        with logger.block(tag="non-degenerate Hamiltonian"):
            logger.log_print(
                str(
                    np.round(H_nd * UnitsData.convert("Hartrees", "Wavenumbers")).astype(int)
                ).splitlines()
            )

        deg_engs, deg_transf = np.linalg.eigh(H_nd)

        ov_thresh = .5
        for i in range(len(deg_transf)):
            max_ov = np.max(deg_transf[:, i] ** 2)
            if max_ov < ov_thresh:  # there must be a single mode that has more than 50% of the initial state character?
                logger.log_print(
                    "    state {i} is more than 50% mixed",
                    i=i
                )
            #     raise PerturbationTheoryException("mode {} is has no contribution of greater than {}".format(
            #         i, ov_thresh
            #     ))

        # we pick the terms with the max contribution from each input state
        # and zero out the contributions so that two states can't map
        # to the same input state
        sort_transf = np.abs(deg_transf.copy())
        sorting = [-1] * len(deg_transf)
        for i in range(len(deg_transf)):
            o = np.argmax(sort_transf[i, :])
            sorting[i] = o
            sort_transf[:, o] = 0.  # np.zeros(len(sort_transf))

        with logger.block(tag='contributions'):
            logger.log_print(
                str(np.round(100 * (deg_transf ** 2)).astype(int)).splitlines()
            )

        logger.log_print('sorting: {s}', s=sorting)

        # sorting = np.argsort(sorting)

        #
        # # if len(sorting) != len(np.unique(sorting)):
        # #     raise PerturbationTheoryException("After diagonalizing can't distinguish modes...")
        deg_engs = deg_engs[sorting,]

        self.logger.log_print("degenerate energies {e}",
                              e=np.round(deg_engs * UnitsData.convert("Hartrees", "Wavenumbers")))

        deg_transf = deg_transf[:, sorting]

        return H_nd_corrs, deg_engs, deg_transf
    #endregion

    def _martin_test(cls, h_reps, states, threshold, total_coupled_space):
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

        raise NotImplementedError("This is fucked up :weep:; need to do full non-degenerate calc per pair of states")

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
                diffs = energies[s] - energies[nzinds]  # do I need an abs ?
            else:
                # compute the energy differences
                diffs = energies[s] - energies  # do I need an abs ?
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

            return degs

    # def _prep_degeneracies_spec(self, degeneracies):
    #     if (
    #             degeneracies is not None
    #             and not isinstance(degeneracies, (int, float, np.integer, np.floating))
    #     ):
    #         if isinstance(degeneracies[0], (int, np.integer)):
    #             degs = BasisStateSpace(self.basis, degeneracies, mode="indices")
    #             degeneracies = (degs, degs)
    #         elif isinstance(degeneracies[0][0], (int, np.integer)):
    #             degs = BasisStateSpace(self.basis, degeneracies, mode="excitations")
    #             degeneracies = (degs, degs)
    #         else:
    #             degeneracies = (
    #                 BasisStateSpace(self.basis, degeneracies[0]),
    #                 BasisStateSpace(self.basis, degeneracies[1])
    #             )
    #
    #     return degeneracies