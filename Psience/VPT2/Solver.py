
import numpy as np, itertools, time, gc

from McUtils.Numputils import SparseArray
from McUtils.Scaffolding import Logger, NullLogger
from McUtils.Parallelizers import Parallelizer, SerialNonParallelizer
from McUtils.Data import UnitsData

from ..BasisReps import Representation, BasisStateSpace, BasisMultiStateSpace, SelectionRuleStateSpace, BraKetSpace
from .Common import PerturbationTheoryException

__reload_hook__ = [ "..BasisReps" ]

__all__ = [
    "PerturbationTheorySolver",
    "PerturbationTheoryCorrections"
]

class DegenerateMultiStateSpace(BasisMultiStateSpace):

    @classmethod
    def from_spec(cls,
                  solver,
                  degenerate_states,
                  full_basis=None
                  ):
        """
        Generates a DegenerateMultiStateSpace object from a number
        of possible specs

        :param solver: the actual applier of the perturbation theory which makes use of the degenerate states
        :type solver: PerturbationTheorySolver
        :return:
        :rtype:
        """

        logger = solver.logger
        H0 = solver.perts[0]
        states = solver.states

        if degenerate_states is not None:

            with logger.block(tag="getting degeneracies"):
                if isinstance(degenerate_states, dict):
                    if 'MartinTest' in degenerate_states:
                        martin_test = degenerate_states['MartinTest']
                    if 'states' in degenerate_states:
                        degenerate_states = degenerate_states['states']
                    elif 'NT' in degenerate_states:
                        logger.log_print(
                            "NT vector: {s}",
                            s=degenerate_states
                        )
                        degenerate_states = cls._group_states_by_nt_spec(H0, states, total_state_space,
                                                                         degenerate_states['NT'])
                    elif 'energy_cutoff' in degenerate_states:
                        logger.log_print(
                            "energy cutoff: {s}",
                            s=degenerate_states
                        )
                        degenerate_states = cls._group_states_by_energy_cutoff(H0, states, total_state_space,
                                                                               degenerate_states['energy_cutoff'])
                    elif 'generator' in degenerate_states:
                        logger.log_print(
                            "callable: {s}",
                            s=degenerate_states
                        )

                        # we assume we have some kind of callable
                        try:
                            degenerate_states = degenerate_states['generator'](H0, states)
                        except (TypeError, ValueError):
                            pass

                        if not isinstance(degenerate_states[0], (BasisStateSpace, BasisMultiStateSpace)):
                            raise NotImplementedError("can't deal with non-BasisStateSpace specs for degeneracies")

                    else:
                        raise NotImplementedError("unsure what to do with degeneracy spec {}".format(degenerate_states))

                else:

                    def _is_degenerate_NT_spec(spec):
                        test1 = isinstance(spec, np.ndarray) and spec.dtype == np.dtype(int)
                        if test1:
                            return test1
                        else:
                            try:
                                it = iter(spec)
                            except TypeError:
                                return False
                            else:
                                return all(isinstance(x, int) for x in it)

                    def _is_degenerate_state_spec(spec):
                        test1 = all(isinstance(x, (BasisStateSpace, BasisMultiStateSpace)) for x in spec)
                        if test1:
                            return test1
                        else:
                            specs = [np.asanyarray(s) for s in spec]
                            return all(
                                (
                                    s.dtype == int
                                    and (s.ndim == 2 or s.ndim == 3)
                                ) for s in specs
                            )

                    # we dispatch on the types of degeneracy specs we support
                    if isinstance(degenerate_states, (int, np.integer, np.floating, float)):
                        logger.log_print(
                            "energy cutoff: {s}",
                            s=degenerate_states
                        )
                        degenerate_states = cls._group_states_by_energy_cutoff(H0, states, degenerate_states)
                    elif _is_degenerate_NT_spec(degenerate_states):
                        logger.log_print(
                            "N_T vector: {s}",
                            s=degenerate_states
                        )
                        degenerate_states = cls._group_states_by_nt_spec(H0, states, degenerate_states)
                    elif _is_degenerate_state_spec(degenerate_states):
                        if not isinstance(degenerate_states[0], (BasisStateSpace, BasisMultiStateSpace)):
                            degenerate_states = [BasisStateSpace(states.basis, x) for x in degenerate_states]
                    elif degenerate_states is not None:
                        logger.log_print(
                            "callable: {s}",
                            s=degenerate_states
                        )

                        # we assume we have some kind of callable
                        try:
                            degenerate_states = degenerate_states(H0, states)
                        except (TypeError, ValueError):
                            pass

                        if not isinstance(degenerate_states[0], (BasisStateSpace, BasisMultiStateSpace)):
                            raise NotImplementedError("can't deal with non-BasisStateSpace specs for degeneracies")

                logger.log_print(
                    "{n} degenerate state sets found",
                    n=len([x for x in degenerate_states if len(x) > 1]),
                    # s=[x for x in degenerate_states if len(x) > 1]
                )

                # if martin_test:
                #     # need to apply Martin test to every pair of states to figure out if they are truly
                #     # going to be significantly affected by the near degeneracy
                #     raise NotImplementedError("Don't have Martin test applying cleanly yet")
                #     logger.log_print(
                #         "applying Martin test with threshold {}",
                #         thresh
                #     )
                #     degenerate_states = cls._martin_test(
                #         H,
                #         state_inds,  # state indices in the coupled_states
                #         thresh,
                #         total_state_space
                #     )
                # else:
                #     logger.log_print(
                #         "skipping Martin test"
                #     )

        # build groups of degenerate states for use later
        if degenerate_states is None:
            groups = states.split(1) #[[x] for x in states.indices]  # we're gonna loop through this later so why not destructure now...
        else:
            groups = [[]] * len(degenerate_states)
            deg_sets = [set(d.indices) for d in degenerate_states]
            for x in states.indices:
                for i, d in enumerate(deg_sets):
                    if x in d:
                        if len(groups[i]) == 0:
                            groups[i] = []
                        groups[i].append(x)
                        break
                else:
                    groups.append([x])

        # now turn these into proper BasisStateSpace objects so we can work with them more easily
        ugh = np.full(len(groups), None)
        for i,g in enumerate(groups):
            # g = np.sort(np.array(g))
            if not isinstance(g, BasisStateSpace):
                g =  BasisStateSpace(states.basis, np.array(g), mode=BasisStateSpace.StateSpaceSpec.Indices, full_basis=full_basis)
            ugh[i] = g
            # if len(g) > 1:
            #     raise Exception(ugh[i].indices, g, ugh[i].excitations,
            #             states.basis.unravel_state_inds(np.arange(10)))

        return cls(ugh)

    @classmethod
    def _group_states_by_energy_cutoff(cls, H0, states, cutoff):
        """
        :type H: Iterable[SparseArray]
        :type states: BasisStateSpace
        :type total_state_space: BasisMultiStateSpace
        :type cutoff: float
        :rtype: Iterable[BasisStateSpace]
        """
        # we look for states with energies within a range...
        # so initially we pull the sets of energies

        diag_inds = BraKetSpace(states, states)
        energies = H0[diag_inds, diag_inds]
        degenerate_groups = []
        # then we look through the input states
        for n, e in enumerate(energies):
            # we only want to apply this once per degenerate group
            # NOTE: this is a path to subtlety, since
            #   if state a is within 50 cm^-1 of state b, and state b is within of c,
            #   you might argue a and c are degenerate
            #   we are wagering that states are distinct _enough_ such that this is not
            #   an issue, but if it is a different strategy will be required
            if all(n not in d for d in degenerate_groups):
                e_diffs = np.abs(energies - e)
                inds = np.where(e_diffs < cutoff)[0]
                degenerate_groups.append(set(inds))
        # raise Exception(degenerate_groups)
        degenerate_groups = [states.take_subspace(np.array(list(d), dtype=int)) for d in degenerate_groups]
        return degenerate_groups

    @classmethod
    def _group_states_by_nt_spec(cls, H, states, q_vec):
        """
        :type H: Iterable[SparseArray]
        :type states: BasisStateSpace
        :type total_state_space: BasisMultiStateSpace
        :type cutoff: Iterable[int]
        :rtype: Iterable[BasisStateSpace]
        """
        # we build the total N_t to compare against once...
        tot_n_t = np.dot(states.excitations, q_vec)
        degenerate_groups = {}
        # then we look through the input states
        for vec in states.excitations:
            # base n_t
            n_t = np.dot(q_vec, vec)
            if n_t not in degenerate_groups:
                degenerate_groups[n_t] = np.where(tot_n_t == n_t)[0]
        degenerate_groups = [states.take_subspace(np.array(d)) for d in degenerate_groups.values()]
        return degenerate_groups

class PerturbationTheoryCorrections:
    """
    Represents a set of corrections from perturbation theory.
    Can be used to correct other operators in the basis of the original calculation.

    """
    def __init__(self,
                 hamiltonians,
                 states,
                 coupled_states,
                 total_basis,
                 energy_corrs,
                 wfn_corrections,
                 degenerate_states=None,
                 degenerate_transformation=None,
                 degenerate_energies=None
                 ):
        """
        :param hamiltonians:
        :type hamiltonians: Iterable[SparseArray]
        :param states:
        :type states: BasisStateSpace
        :param coupled_states:
        :type coupled_states: BasisMultiStateSpace
        :param total_basis:
        :type total_basis: BasisMultiStateSpace
        :param energy_corrs:
        :type energy_corrs: np.ndarray
        :param wfn_corrections:
        :type wfn_corrections: Iterable[SparseArray]
        :param degenerate_states:
        :type degenerate_states: None | np.ndarray
        :param degenerate_transformation:
        :type degenerate_transformation: None | np.ndarray
        :param degenerate_energies:
        :type degenerate_energies: None | np.ndarray
        """
        self.hams = hamiltonians
        self.states = states
        self.coupled_states = coupled_states
        self.total_basis = total_basis
        self.energy_corrs = energy_corrs
        self.wfn_corrections = wfn_corrections
        self.degenerate_states = degenerate_states
        self.degenerate_transf = degenerate_transformation
        self.degenerate_energies = degenerate_energies

    @classmethod
    def from_dicts(cls,
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
        state_space = states['states']
        coupled_states = states['coupled_states']
        total_basis = states['total_states']
        energy_corrs = corrections['energies']
        wfn_corrections = corrections['wavefunctions']
        if 'degenerate_states' in states:
            degenerate_states = states['degenerate_states']
        else:
            degenerate_states = None

        if 'degenerate_transformation' in corrections:
            degenerate_transf = corrections['degenerate_transformation']
        else:
            degenerate_transf = None

        if 'degenerate_energies' in corrections:
            degenerate_energies = corrections['degenerate_energies']
        else:
            degenerate_energies = None

        return cls(
            hamiltonians,
            state_space,
            coupled_states,
            total_basis,
            energy_corrs,
            wfn_corrections,
            degenerate_states=degenerate_states,
            degenerate_transformation=degenerate_transf,
            degenerate_energies=degenerate_energies
        )

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

    def take_subspace(self, space):
        """
        Takes only those elements that are in space
        :param space:
        :type space:
        :return:
        :rtype:
        """

        new_states = self.states.find(space)
        # raise Exception(new_states)
        return type(self)(
            self.hams,
            self.states.take_states(space),
            self.coupled_states.take_states(space),
            self.total_basis,
            self.energy_corrs[new_states],
            [w[new_states, :] for w in self.wfn_corrections],
            # not sure what to do with all this...
            degenerate_states=self.degenerate_states,
            degenerate_transformation=self.degenerate_transf,
            degenerate_energies=self.degenerate_energies
        )

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
            wfn_corrs = self.wfn_corrections[:order]
        else:
            # need to make the subspace good with the subspace in which the corrections are defined...
            subspace_sel = self.total_basis.find(subspace, check=True)
            wfn_corrs = []
            # raise Exception(subspace_sel)
            for k in range(order):
                wfn_corrs.append(self.wfn_corrections[k][:, subspace_sel])
        # raise Exception(wfn_corrs)

        # generalizes the dot product so that we can use 0 as a special value...
        dot = PerturbationTheorySolver._safe_dot

        # does the dirty work of acutally applying the rep...
        reps = [np.zeros(1)] * order
        for k in range(order):
            op = None
            # apply each thing up to requested order...
            for a in range(k+1): # if k == 2: a=0, a=1, a=2
                for b in range(k-a+1): # if k==2, a==0: b=0, b=1, b=2; a==1: b=0, b=1
                    c = k - (a + b) # a + b + c == k
                    rop = operator_expansion[c]
                    if isinstance(rop, (int, float, np.integer, np.floating)): # constant reps...
                        if rop != 0: # cheap easy check
                            subrep = rop * dot(wfn_corrs[a], wfn_corrs[b].T)
                            if op is None:
                                op = subrep
                            else:
                                op += subrep
                    else:
                        subrep = dot(dot(wfn_corrs[a], rop), wfn_corrs[b].T)
                        if op is None:
                            op = subrep
                        else:
                            op += subrep
            reps[k] = op

        return reps

    def get_overlap_matrices(self):
        """
        Returns the overlap matrices for the set of corrections
        at each order of correction

        :return:
        :rtype:
        """

        wat = []
        for k in range(2 + 1):
            ov = None
            for i in range(k + 1):
                c1 = self.wfn_corrections[i].asarray()
                c2 = self.wfn_corrections[k - i].asarray()
                if ov is None:
                    ov = np.dot(c1, c2.T)
                else:
                    ov += np.dot(c1, c2.T)
            wat.append(ov)

        return wat

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
        return cls.from_dicts(
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

    def to_state(self, serializer=None):
        keys = dict(
            states=self.states,
            coupled_states=self.coupled_states,
            total_states=self.total_basis,
            energies=self.energy_corrs,
            wavefunctions=self.wfn_corrections,
            hamiltonians=self.hams,
            degenerate_states=self.degenerate_states,
            degenerate_transformations=self.degenerate_transf,
            degenerate_energies=self.degenerate_energies
        )
        return keys
    @classmethod
    def from_state(cls, data, serializer=None):
        return cls.from_dicts(
            {
                "states": serializer.deserialize(data['states']),
                "coupled_states": serializer.deserialize(data['coupled_states']),
                "total_states": serializer.deserialize(data['coupled_states']),
                "degenerate_states": serializer.deserialize(data['degenerate_states']),
            },
            {
                "energies": data['energies'],
                "wavefunctions": data['wavefunctions'],
                "degenerate_transformation": data['degenerate_transformations'],
                "degenerate_energies": data['degenerate_energies']
            },
            data['hamiltonians'] # we probably want to ditch this for memory reasons...
        )

class PerturbationTheorySolver:
    """
    A solver that applies perturbation theory
    given a series of corrections and population of states.
    Supports degenerate and non-degenerate PT.
    """

    def __init__(self, perturbations, states,
                 coupled_states=None,
                 order=2, total_space=None,
                 flat_total_space=None,
                 state_space_iterations=None,
                 allow_sakurai_degs=False,
                 allow_post_PT_calc=True,
                 modify_degenerate_perturbations=False,
                 gaussian_resonance_handling=False,
                 ignore_odd_order_energies=False,
                 intermediate_normalization=False,
                 zero_element_warning=True,
                 degenerate_states=None,
                 memory_constrained=False,
                 logger=None,
                 verbose=False,
                 parallelizer=None,
                 checkpointer=None
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
        """

        # if memory_constrained:
        #     raise NotImplementedError('memory constraint handling currently broken')

        self.perts = perturbations
        self._reps = None
        self.order = order
        self.state_space_iterations=state_space_iterations

        self.logger = logger
        self.verbose = verbose
        self.parallelizer = parallelizer
        self.checkpointer = checkpointer

        self.states = states
        self.full_basis = states.full_basis

        self.degeneracy_spec = degenerate_states
        self._deg_states = None
        # self.degeneracy_mode = degeneracy_mode
        self.allow_sakurai_degs = allow_sakurai_degs
        self.allow_post_PT_calc = allow_post_PT_calc
        self.ignore_odd_orders = ignore_odd_order_energies
        self.drop_perturbation_degs = modify_degenerate_perturbations
        self.intermediate_normalization = intermediate_normalization
        self.gaussian_resonance_handling = gaussian_resonance_handling
        self.zero_element_warning = zero_element_warning

        self.memory_constrained=memory_constrained

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

    @property
    def degenerate_spaces(self):
        if self._deg_states is None:
            self._deg_states = DegenerateMultiStateSpace.from_spec(self, self.degeneracy_spec, full_basis=self.full_basis)
        return self._deg_states

    @property
    def zero_order_energies(self):
        if self._zo_engs is None:
            H0 = self.representations[0]
            e_vec_full = np.diag(H0) if isinstance(H0, np.ndarray) else H0.diag
            if isinstance(e_vec_full, SparseArray):
                e_vec_full = e_vec_full.asarray()
            self._zo_engs = e_vec_full
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
        # raise Exception((analytic_engs - analytic_engs[0])*UnitsData.convert("Hartrees", "Wavenumbers"))

        corrs = self.get_corrections()

        # import McUtils.Plots as plt
        #
        # wat = corrs.get_overlap_matrices()

        # degeneracy_mode = self.degeneracy_mode
        degenerate_states = self.degenerate_spaces
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

        return corrs

    #region Get Matrix Inputs
    use_cached_representations=True
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
                except KeyError:
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

                        for i, h in enumerate(self.perts[1:]):
                            # calculate matrix elements in the coupled subspace
                            if n_spaces > i + 1:
                                cs = self.total_state_space[i + 1]
                                with logger.block(tag="building {}".format(h)):
                                    start = time.time()
                                    H[i + 1] = h.get_representation_matrix(cs, self.flat_total_space,
                                                                           zero_element_warning=self.zero_element_warning,
                                                                           diagonal=False
                                                                           )
                                    h.clear_cache()
                                    # cs.clear_cache()
                                    end = time.time()
                                    logger.log_print("took {t:.3f}s", t=end - start)

                    if self.use_cached_representations:
                        checkpointer['representations'] = H

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
    def _build_subprojector(self, states, inds):
        """
        Builds a subspace projector where only inds will
        be included but also the projection will be onto states

        :param states: state vectors in the inds subspace
        :type states: np.ndarray
        :param inds: indices for the subspace
        :type inds: np.ndarray
        :return:
        :rtype: SparseArray
        """

        raise NotImplementedError("I'm not sure I need this?")

        shp = self.representations[0].shape
        ind_pairs = np.array(list(itertools.product(inds, inds))).T
        vals = np.dot(states.T, states)
        return SparseArray.from_data(
                (
                    np.ones(len(inds)),
                    (inds, inds)
                ),
                shape=shp
            )
    def _get_Pi0(self, degenerate_subspace, non_zero_cutoff=1.0e-14, E0=None):
        # generate the perturbation operator
        e_vec_full = self.zero_order_energies
        if E0 is None:
            E0 = np.average(e_vec_full[degenerate_subspace]) # better to use the first or the average? Not clear, we'll go with first
            # h2w = UnitsData.convert("Hartrees", "Wavenumbers")
            # gs = self.zero_order_energies[0] * h2w
            # raise Exception(E0*h2w - gs, e_vec_full[degenerate_subspace]*h2w - gs)
        e_vec = e_vec_full - E0
        e_vec[degenerate_subspace] = 1
        zero_checks = np.where(np.abs(e_vec) < non_zero_cutoff)[0]
        if len(zero_checks) > 0:
            bad_vec = np.concatenate([[E0], e_vec_full[zero_checks]])
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
    def _get_Pi1(self, D, E1, G=None, non_zero_cutoff=1.0e-14, singular_check=1e10):
        """
        Returns the first-order perturbation operator in the D_n subspace

        :param D: degenerate subspace
        :type D: np.ndarray[int]
        :param G: degenerate subspace within D corresponding to E1
        :type G: np.ndarray
        :param E1:
        :type E1: float
        :return:
        :rtype:
        """

        D_engs, D_vecs = D
        if G is None:
            n_ind = np.where(np.abs(D_engs - E1) < non_zero_cutoff)[0]
            rest_ind = np.delete(np.arange(len(D_engs)), n_ind)
        else:
            G_inds, G_ends, G_vecs = G
            rest_ind = np.setdiff1d(np.arange(len(D_engs)), G_inds)

        if len(rest_ind) == 0:
            return np.zeros((len(D_engs), len(D_engs)))

        rest_engs = D_engs[rest_ind] - E1
        rest_vecs = D_vecs[rest_ind, :]

        pi = rest_vecs.T @ np.diag(1 / rest_engs) @ rest_vecs

        if np.max(np.abs(pi)) > singular_check:
            raise ValueError("singular perturbation operator in degenerate subspace {}".format(D))

        return pi
        # raise Exception(pi, Rn@pi@Rn)
    def _get_Pi2(self, D, G, n, E2, singular_check=1e10):
        """
        Returns the first-order perturbation operator in the D_n subspace

        :param D: degenerate subspace
        :type D: np.ndarray[int]
        :param G: degenerate energies and subsubspace
        :type G: Tuple[np.ndarray, np.ndarray]
        :param E1:
        :type E1: float
        :return:
        :rtype:
        """

        G_inds, G_engs, G_vecs = G
        n_ind = np.where(np.abs(G_engs - E2) < 1e-14)[0][0]
        rest_ind = np.delete(np.arange(len(G_engs)), n_ind)

        rest_engs = G_engs[rest_ind] - E2
        rest_vecs = G_vecs[rest_ind, :]

        pi = rest_vecs.T @ np.diag(1/rest_engs) @ rest_vecs

        if np.max(np.abs(pi)) > singular_check:
            raise ValueError("singular perturbation operator in degenerate subspace {}".format(G))

        return pi
    def _get_V_projector(self, D, G):
        Pn = np.dot(G.T, G)
        Rn = np.eye(len(Pn)) - Pn

        ind_pairs = np.array(list(itertools.product(D, D))).T
        vals = Rn.flatten()

        shp = self.representations[0].shape
        return SparseArray.from_data(
            (
                vals,
                ind_pairs
            ),
            shape=shp
        )
    def _get_secondary_degenerate_inputs(self, deg_inds, deg_transf, subspace):
        """
        Gets the eigenvalues of Ps (H2  - H1 Pi_U H1) Ps where E_n is defined to be the
        average zero-order energy in s.
        The closer to degenerate the proper zero-order states are, the better this
        approximation (it is exact when the degeneracy is perfect)

        :param deg_inds:
        :type deg_inds:
        :param deg_transf: column oriented
        :type deg_transf:
        :param subspace: secondary degenerate subspace (I called it G in my OG stuff...)
        :type subspace:
        :return:
        :rtype:
        """

        try:
            PiU = self._get_Pi0(deg_inds)
        except ValueError:
            PiU = self._get_Pi0(deg_inds, E0=self.zero_order_energies[deg_inds[0]])
        subtf = deg_transf[:, subspace]
        Ps = subtf.T@subtf

        H2 = self._take_subham(self.representations[2], deg_inds)
        H1UH1 = self._safe_dot(self._safe_dot(self.representations[1], PiU), self.representations[1])
        if not isinstance(H1UH1, (int, float, np.integer, np.floating)):
            H1UH1 = self._take_subham(H1UH1, deg_inds)

        subham = np.dot(np.dot(Ps, H2 - H1UH1), Ps)

        # raise Exception(subham)

        # import McUtils.Plots as plt
        # h1 = self._take_subham(self.representations[1], deg_inds)
        # plt.ArrayPlot(h1)
        # plt.ArrayPlot(H2)
        # plt.ArrayPlot(H1UH1)
        # plt.ArrayPlot(Ps)
        # plt.ArrayPlot(subham).show()

        eng2, deg_transf2 = np.linalg.eigh(subham)

        # raise Exception(eng2*UnitsData.convert("Hartrees", "Wavenumbers"))

        # now we need to get the appropriate sorting to match up the
        # secondary degenerate transformation and the OG terms
        overlaps = np.dot(deg_transf2.T, subtf.T)

        sort_transf = np.abs(overlaps)
        sorting = [-1] * len(deg_transf2)
        for i in range(len(deg_transf2)):
            o = np.argmax(sort_transf[i, :])
            sorting[i] = o
            sort_transf[:, o] = 0.

        new_eng = eng2[sorting]
        deg_transf2 = deg_transf2.T[sorting]

        # raise Exception(sorting)

        # raise Exception(deg_transf2, new_eng, sorting)

        return new_eng, deg_transf2
    def _get_deg_eq_inputs(self, deg_inds, degeneracy_cutoff=1e-8): # within a few wavenumbers or so
        """
        Diagonalizes the perturbations in the degenerate subspace to
        get a cleaner basis in which to do the perturbation theory.
        This comes from Sakurai.

        :param deg_inds:
        :type deg_inds:
        :return:
        :rtype: tuple[Iterable[SparseArray], SparseArray, SparseArray]
        """

        H1 = self.representations[1]
        subham = self._take_subham(H1, deg_inds)
        # raise Exception(self.flat_total_space.take_subspace(deg_inds).excitations,
        #                 self.flat_total_space.take_subspace(deg_inds).indices)
        # raise Exception(subham)

        new_eng1, deg_transf = np.linalg.eigh(subham)
        main_transf = deg_transf.T.copy()

        # now we split this into degenerate subspaces by grouping up
        # runs of degenerate states
        # ...except I fucked this up
        deg_spaces = []
        deg_set = set()
        for i,a in enumerate(new_eng1):
            deg_set.add(i)
            if i+1 < len(new_eng1):
                b_ind = i+1
            else:
                b_ind = i
            b = new_eng1[b_ind]
            if abs(a - b) < degeneracy_cutoff:
                deg_set.add(b_ind)
            else:
                deg_spaces.append(deg_set)
                deg_set = set()
        deg_spaces.append(deg_set)
        deg_spaces = [np.sort(np.array(list(s))) for s in deg_spaces]
        # now we handle secondary degeneracies...

        self.logger.log_print(
            "handling degeneracies for {space}...",
            space=deg_inds
        )
        subspaces = [ ]
        new_engs = [ ]
        for s in deg_spaces:
            if len(s) > 1:
                self.logger.log_print(
                    "need second-level degeneracies for {sub}",
                    sub=deg_inds[s]
                )
                new_eng2, subs_transf = self._get_secondary_degenerate_inputs(deg_inds, deg_transf, s)
                deg_transf[:, s] = subs_transf.T
            else:
                new_eng2 = [None]
                subs_transf = None
            new_engs.extend(zip(new_eng1[s], new_eng2))
            subspaces.extend([(s, new_eng2, subs_transf)] * len(s))

        new_eng = np.full(len(new_engs), None)
        new_subspace = np.full(len(subspaces), None)
        for i in range(len(new_engs)):
            new_eng[i] = new_engs[i]
            new_subspace[i] = subspaces[i]

        # we sort now to get the "best" mapping back onto the OG states
        sort_transf = np.abs(deg_transf.copy())
        sorting = [-1] * len(deg_transf)
        for i in range(len(deg_transf)):
            o = np.argmax(sort_transf[i, :])
            sorting[i] = o
            sort_transf[:, o] = 0.  # np.zeros(len(sort_transf))

        new_eng = new_eng[sorting]
        new_subspace = new_subspace[sorting]
        deg_transf = deg_transf.T[sorting]

        return new_eng, deg_transf, main_transf, new_subspace
    #endregion

    #region Get Coupled Spaces
    use_cached_basis=True
    def load_state_spaces(self):

        logger = self.logger

        with self.logger.block(tag='getting basis'):
            if self._coupled_states is None:
                self.logger.log_print('trying to load from checkpoint...')
                with self.checkpointer:
                    try:
                        if self.use_cached_basis:
                            self._coupled_states = self.checkpointer['coupled_states']
                    except KeyError:
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
                            self.checkpointer['coupled_states'] = self._coupled_states

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
                spaces=spaces
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

    def _get_new_coupled_space(self, a, b, spaces=None, ret_space=True):
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

        logger = self.logger if self.verbose else None
        if isinstance(a, self.StateSpaceWrapper):
            raise NotImplementedError("we shouldn't be here")
            new = a * b
        elif isinstance(a, self.ProjectionOperatorWrapper):
            new = a.get_transformed_space(b, parallelizer=self.parallelizer, logger=logger)
        elif isinstance(a, (self.ProjectedOperator, Representation)):
            cur = spaces[op] #type: SelectionRuleStateSpace
            proj = None if not isinstance(a, self.ProjectedOperator) else a.proj
            if cur is None:
                new = a.get_transformed_space(b, parallelizer=self.parallelizer, logger=logger)
                # b.check_indices()
                # new.check_indices()
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
                    new = a.get_transformed_space(b,
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
                        new_new = a.get_transformed_space(diffs,
                                                          track_excitations = not self.memory_constrained,
                                                          parallelizer=self.parallelizer, logger=logger
                                                          )

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
                        # means we already calculated everything
                        # so we don't need to worry about this
                        b_sels = SelectionRuleStateSpace(b, [], ignore_shapes=True) # just some type fuckery
                        new = cur.intersection(b_sels, handle_subspaces=False)
                        new = new.to_single().take_unique()
        else:
            raise TypeError("don't know what to do with {} and {}".format(a, b))

        return self.StateSpaceWrapper(new)

    def _reduce_new_coupled_space(self, *terms, spaces=None, ret_space=True):
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
            lambda a, b:self._get_new_coupled_space(b, a, spaces, ret_space=ret_space),
            reversed(terms[:-1]),
            terms[-1]
        )

    def get_coupled_space(self,
                            input_state_space,
                            degenerate_space,
                            use_second_deg,
                            allow_PT_degs=True,
                            spaces=None
                            ):
        """
        Applies the VPT equations semi-symbolically, dispatching based on how many
        degeneracies we need to handle

        :return:
        :rtype:
        """

        if not allow_PT_degs:
            spaces = self.get_nondeg_coupled_space(input_state_space, degenerate_space, spaces=spaces)
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
                                 spaces=None
                                 ):
        """
        Applies the non-degenerate equations in semi-symbolic form to determine
        which states needs to be calculated.
        This will always be the initial input to a calculation and then
        certain additional states may be calculated on the fly if they are needed to handle
        truly degenerate stuff.
        The only difference there will be to add on

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

        dot = lambda *terms, spaces=spaces,ret_space=True: self._reduce_new_coupled_space(*terms, spaces=spaces,ret_space=ret_space)

        H = self.PastIndexableTuple(self.perts)

        # raise Exception( (dot(self.ProjectedOperator(pi, H[1]), corrs[0]) + corrs[0]).space )

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
                             if not isinstance(H[k - i], (int, np.integer))
                             )
                )
            with self.logger.block(tag='getting states for order {k}'.format(k=k)):
                corrs[k] = sum(corrs[i] for i in range(0, k)) # this all in here from energies
                for i in range(0, k):
                    if not isinstance(H[k - i], (int, np.integer)):
                        self.logger.log_print('H({a})|n({b})>', a=k - i, b=i)
                        if k < order-1:
                            corrs[k] += dot(H[k - i], corrs[i])
                        else:
                            dot(H[k - i], corrs[i], ret_space=False)

        # raise Exception(
        #     [x[1] for x in spaces.values() if x is not None][0].get_representation_indices().T
        #     # [x.space for x in corrs]
        # )

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

    def get_corrections(self, non_zero_cutoff=1.0e-14):
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
        # degenerate_states = None,
        # degeneracy_mode = None,
        # logger = None,
        # checkpointer = None,

        # checkpointer['indices'] = self.total_state_space
        with checkpointer:

            # import McUtils.Plots as plt
            # for r in perturbations:
            #     wat = plt.ArrayPlot(r.asarray())
            # wat.show()

            all_energies = np.zeros((len(states), order + 1))
            all_overlaps = np.zeros((len(states), order + 1))
            all_corrs = np.zeros((len(states), order + 1, N))

            with logger.block(tag="applying PT"):
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
                        deg_group = BasisStateSpace(self.flat_total_space.basis, deg_group, full_basis=self.full_basis)
                    deg_group.deg_find_inds = None
                    _.append(deg_group)
                degenerate_states = _

                if self.drop_perturbation_degs:
                    dropped_els, perturbations = self.drop_deg_pert_els(perturbations, degenerate_states)
                    for deg_group in degenerate_states:
                        for n in deg_group.indices:
                            d2 = deg_group.take_states([n])
                            d2.deg_find_inds = None
                            energies, overlaps, corrs = self.apply_VPT_equations(n, deg_group,
                                                                                 None, None, None, None,
                                                                                 allow_PT_degs=False,
                                                                                 non_zero_cutoff=non_zero_cutoff,
                                                                                 perturbations=perturbations
                                                                                 )

                            res_index = states.find(n)
                            all_energies[res_index] = energies
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

                        for n, de, zo, s in zip(deg_group.indices, deg_engs, zero_order_states, subspaces):
                            energies, overlaps, corrs = self.apply_VPT_equations(n, deg_group, de, zo, main_subspace, s,
                                                                                 allow_PT_degs=self.allow_sakurai_degs,
                                                                                 non_zero_cutoff=non_zero_cutoff,
                                                                                 perturbations=perturbations
                                                                                 )

                            res_index = states.find(n)
                            all_energies[res_index] = energies
                            all_corrs[res_index] = corrs
                            all_overlaps[res_index] = overlaps

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

            corr_inds = [[] for i in range(nstates)]
            corr_mats = [None] * (order + 1)

            for o in range(order + 1):
                non_zeros = []
                for i, corr in enumerate(all_corrs):
                    # we find the non-zero elements within the o level of correction for the ith state
                    nonzi = np.where(np.abs(corr[o]) > non_zero_cutoff)[0]
                    # then we pull these out
                    vals = corr[o][nonzi,]
                    # and we add the values and indices to the list
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
                corr_mats[o] = SparseArray.from_data(
                    (
                        vals,
                        inds
                    ),
                    shape=(nstates, N)
                )

            # now we build state reps from corr_inds
            for i, dat in enumerate(corr_inds):
                cat = np.concatenate(dat)
                _, upos = np.unique(cat, return_index=True)
                full_dat = cat[np.sort(upos)]
                corr_inds[i] = flat_total_space.take_states(full_dat)  # BasisStateSpace(states.basis, full_dat, mode="indices")

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
                    "wavefunctions": corr_mats,
                    "degenerate_transformation": None,
                    "degenerate_energies": None
                },
                perturbations # we probably want to ditch this for memory reasons...
            )

            checkpointer['corrections'] = {
                'energies': all_energies,
                'wavefunctions': corr_mats
            }

        return corrs

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
                            non_zero_cutoff=1.0e-14
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
        if ignore_odd_orders is None:
            ignore_odd_orders=self.ignore_odd_orders
        if allow_PT_degs is None:
            allow_PT_degs = self.allow_sakurai_degs
        if intermediate_normalization is None:
            intermediate_normalization = self.intermediate_normalization
        if not allow_PT_degs:
            return self.apply_VPT_nondeg_equations(state_index, degenerate_space_indices, non_zero_cutoff=non_zero_cutoff,
                                                   ignore_odd_orders=ignore_odd_orders,
                                                   intermediate_normalization=intermediate_normalization,
                                                   perturbations=perturbations
                                                   )
        if len(degenerate_space_indices) == 1:
            return self.apply_VPT_nondeg_equations(state_index, None, non_zero_cutoff=non_zero_cutoff)
        elif len(degenerate_subsubspace[0]) == 1:
            return self.apply_VPT_deg_equations(state_index, degenerate_space_indices, degenerate_energies[0],
                                                zero_order_state, degenerate_subspace, non_zero_cutoff=non_zero_cutoff)
        else:
            return self.apply_VPT_second_deg_equations(state_index, degenerate_space_indices, degenerate_energies,
                                                zero_order_state, degenerate_subspace, degenerate_subsubspace, non_zero_cutoff=non_zero_cutoff)
    def apply_VPT_nondeg_equations(self,
                                   state_index,
                                   deg_group,
                                   perturbations=None,
                                   non_zero_cutoff=1.0e-14,
                                   check_overlap=True,
                                   intermediate_normalization=False,
                                   ignore_odd_orders=False
                                   ):
        """
        Does the dirty work of doing the VPT iterative equations.
        Needs to be adapted to include the two types of degeneracies that can
        be introduced in Sakurai's approach.

        :return:
        :rtype:
        """

        verbose = self.verbose
        if intermediate_normalization:
            check_overlap=False

        n = state_index
        e_vec_full = self.zero_order_energies

        order = self.order
        total_state_space = self.flat_total_space

        energies = np.zeros((order + 1,), dtype=float)
        overlaps = np.zeros((order + 1,), dtype=float)
        corrs = np.zeros((order + 1, len(total_state_space)), dtype=float)  # can I make this less expensive in general?

        # find the state index in the coupled subspace
        n_ind = total_state_space.find(n)
        if verbose:
            block_logger = self.logger
            n_exc = total_state_space.take_subspace([n_ind]).excitations[0]
        else:
            n_exc = None
            block_logger = NullLogger()

        with block_logger.block(tag='getting corrections for state {}/{}'.format(n, n_exc)):

            D = deg_group
            deg_inds = (n_ind,)
            if D is not None:
                if D.deg_find_inds is None:
                    D.deg_find_inds = total_state_space.find(D)
                deg_inds = D.deg_find_inds
                if verbose and len(D) > 1:
                    self.logger.log_print('Degenerate space: {D}', D=D.indices)
            E0 = e_vec_full[n_ind]
            pi = self._get_Pi0(deg_inds, E0=E0, non_zero_cutoff=non_zero_cutoff)

            energies[0] = E0
            if verbose:
                self.logger.log_print('Zero-order energy: {e}', e=E0[0] * UnitsData.convert("Hartrees", "Wavenumbers"))
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
                    if verbose:
                        self.logger.log_print('Skipping order {k} for the energy (assumed to be 0)', k=k)
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
                    if verbose:
                        self.logger.log_print(
                            ['Energy terms at order {k} in cm^-1:'] + [
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
                            k=k
                        )
                    Ek = np.sum(energy_terms)
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
                    if verbose:
                        self.logger.log_print([
                            'Overlap at order {k}:'
                            '<n(0)|n({k})> = {ok}'
                            ], k=k, ok=ok)
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

        return energies, overlaps, corrs
    def apply_VPT_deg_equations(self,
                                state_index,
                                degenerate_space_indices,
                                degenerate_energy,
                                zero_order_state,
                                degenerate_subspace,
                                non_zero_cutoff=1.0e-14,
                                check_overlap=True
                                ):
        """
        Does the dirty work of doing the VPT iterative equations.
        Needs to be adapted to include the two types of degeneracies that can
        be introduced in Sakurai's approach.

        :return:
        :rtype:
        """

        # raise NotImplementedError("need to check this...")

        n = state_index
        e_vec_full = self.zero_order_energies

        order = self.order
        total_state_space = self.flat_total_space

        energies = np.zeros((order + 2,), dtype=float)
        overlaps = np.zeros((order + 1,), dtype=float)
        corrs = np.zeros((order + 1, len(total_state_space)), dtype=float)  # can I make this less expensive in general?

        # find the state index in the coupled subspace
        n_ind = total_state_space.find(n)
        E0 = e_vec_full[n_ind]
        E1 = degenerate_energy

        D = degenerate_space_indices
        piU = self._get_Pi0(D, E0=E0, non_zero_cutoff=non_zero_cutoff)
        piDn = self._get_Pi1(degenerate_subspace, E1)

        energies[0] = E0
        energies[1] = E1
        corrs[0, D] = zero_order_state

        overlaps[0] = 1

        H = self.representations
        dot = self._safe_dot
        for k in range(1, order + 1):  # to actually go up to k
            # Pu |n^(k)>
            corrs[k] = sum(
                dot(piU, energies[k - i] * corrs[i] - dot(H[k - i], corrs[i]))
                for i in range(1, k) # goes up to k-1
            ) - dot(piU, dot(H[k], corrs[0]))
            # E^(k+1)
            #
            energies[k+1] = (
                    dot(corrs[0], dot(H[k+1], corrs[0]))
                    + dot(corrs[0], dot(H[1], corrs[k]))
                    + sum(
                          dot(corrs[0], dot(H[k+1 - i], corrs[i])) - energies[k+1 - i] * overlaps[i]
                          for i in range(1, k)
                          )
            )
            # PDn|n^(k)>
            dHDnk = sum(
                energies[k + 1 - i] * corrs[i] - dot(H[k + 1 - i], corrs[i])
                for i in range(0, k)
            ) - dot(H[1], corrs[k])
            corrs[k][D] = dot(piDn, dHDnk[D])

            cur_ov = dot(corrs[0][D], corrs[k][D])
            if cur_ov > .005:
                raise ValueError(
                    "overlap of zero-order state {} with rest of D should be zero after applying PiD (currently={}; order={})".format(
                        state_index,
                        cur_ov,
                        k
                    ))

            # <n^(0)|n^(k)> = -1/2 sum(<n^(i)|n^(k-i)>, i=1...k-1)
            ok = -1 / 2 * sum(dot(corrs[i], corrs[k - i]) for i in range(1, k))
            overlaps[k] = ok  # dot(corrs[0], corrs[k])
            corrs[k][D] += corrs[0][D] * ok  # - corrs[0][D]*dot(corrs[0][D], corrs[k][D])

            check_overlap = True
            if check_overlap:
                true_ov = dot(corrs[0], corrs[k])
                overlaps[k] = true_ov
                if abs(true_ov - ok) > .005:
                    raise ValueError(
                        "state {} fails overlap relationship at order {} (contrib {}, expected {})".format(
                            state_index, order, true_ov, ok
                        ))

        if check_overlap:
            # full_wfn = np.sum(corrs, axis=0)
            # ov = np.dot(full_wfn, full_wfn)
            ov_parts = [[dot(corrs[k - i], corrs[i]) for i in range(k + 1)] for k in range(order + 1)]
            ov = sum(np.sum(v) for v in ov_parts)
            if abs(ov - 1) > .005:
                raise ValueError(
                    "state {} isn't normalized (overlap = {}, bits {})".format(
                        state_index, ov, ov_parts
                    ))

        return energies[:order+1], overlaps, corrs

    def apply_VPT_second_deg_equations(self,
                                       state_index,
                                       degenerate_space_indices,
                                       degenerate_energies,
                                       zero_order_state,
                                       degenerate_subspace,
                                       degenerate_subsubspace,
                                       non_zero_cutoff=1.0e-14,
                                       check_overlap=True
                                       ):
        """
        Does the dirty work of doing the VPT iterative equations.
        Needs to be adapted to include the two types of degeneracies that can
        be introduced in Sakurai's approach.

        :return:
        :rtype:
        """
        n = state_index
        e_vec_full = self.zero_order_energies

        order = self.order
        total_state_space = self.flat_total_space

        energies = np.zeros((order + 3,), dtype=float)
        overlaps = np.zeros((order + 1,), dtype=float)
        corrs = np.zeros((order + 1, len(total_state_space)), dtype=float)  # can I make this less expensive in general?

        # find the state index in the coupled subspace
        n_ind = total_state_space.find(n)
        E0 = e_vec_full[n_ind]
        E1 = degenerate_energies[0]
        E2 = degenerate_energies[1]

        H = self.representations
        dot = self._safe_dot
        E = energies

        D = degenerate_space_indices
        piU = self._get_Pi0(D, E0=E0, non_zero_cutoff=non_zero_cutoff)
        piV = self._get_Pi1(degenerate_subspace, E1, G=degenerate_subsubspace)

        piG = self._get_Pi2(D, degenerate_subsubspace, zero_order_state, E2)
        H1PiU = dot(H[1], piU)
        # projV = self._get_V_projector(D, degenerate_subsubspace)

        energies[0] = E0
        energies[1] = E1
        energies[2] = E2
        corrs[0, D] = zero_order_state

        overlaps[0] = 1

        # eval_wf_bits(1)
        # eval_wf_bits(2)
        # raise Exception(piV)#dot(projV, corrs[1])[D])
        # c1 = corrs[1].copy()
        # c1[D] = 0.
        # raise Exception(dot(c1, c1))

        def drop_D(vec):
            vec = vec.copy()
            vec[D] = 0.
            return vec

        for k in range(1, order + 1):  # to actually go up to the total order

            # Pu |n^(k)>
            corrs[k] = sum(
                dot(piU, E[k - i] * corrs[i] - dot(H[k - i], corrs[i]))
                for i in range(1, k)  # goes up to k-1
            ) - dot(piU, dot(H[k], corrs[0]))
            # Pv |n^(k)>
            dHV = sum(
                energies[k + 1 - i] * corrs[i] - dot(H[k + 1 - i], corrs[i])
                for i in range(0, k)
            ) - dot(H[1], corrs[k])
            corrs[k][D] = dot(piV, dHV[D])
            watt = dot(corrs[0], corrs[k])
            if np.abs(watt) > 0.005:
                raise ValueError(
                    "state {} isn't orthogonal to V at order {} (contrib {})".format(
                        state_index, order, watt
                    )
                )

            # E^(k+2)
            puN1 = drop_D(corrs[1])
            Ekp2 = (
                    dot(corrs[0], dot(H[k + 2], corrs[0]))
                    + dot(puN1, dot(H[k + 1], corrs[0]))
                    + sum(
                        dot(corrs[0], dot(H[k + 2 - i], corrs[i]))
                        - energies[k + 2 - i] * overlaps[i]
                        for i in range(1, k+1)
                    )
                    + sum(
                        dot(puN1, dot(H[k + 1 - i], corrs[i]))
                        - energies[k + 1 - i] * dot(puN1, corrs[i])
                        for i in range(1, k+1)
                    )
            )
            energies[k + 2] = Ekp2

            dH = sum(
                energies[k+2 - i] * corrs[i] -
                    dot(H[k+2 - i], corrs[i])
                for i in range(0, k+1)
            ) - dot(H1PiU,
                    sum(
                        energies[k + 1 - i] * corrs[i] -
                        dot(H[k + 1 - i], corrs[i])
                        for i in range(0, k + 1)
                    ))

            # testFleh = sum(
            #     dot(piG, dot(projV, corrs[k + 2 - j])[D])
            #     for j in range(1, 3)
            # )

            # dHG = dHG1 - dHG2
            ketG = dot(piG, dH[D])
            corrs[k][D] += ketG

            cur_ov = dot(corrs[0][D], corrs[k][D])
            if cur_ov > .005:
                raise ValueError("overlap of zero-order state {} with rest of G should be zero after applying PiG (currently={}; order={})".format(
                    state_index,
                    cur_ov,
                    k
                ))
            # <n^(0)|n^(k)> = -1/2 sum(<n^(i)|n^(k-i)>, i=1...k-1)
            ok = -1 / 2 * sum(dot(corrs[i], corrs[k - i]) for i in range(1, k))
            overlaps[k] = ok # dot(corrs[0], corrs[k])
            corrs[k][D] += corrs[0][D]*ok #- corrs[0][D]*dot(corrs[0][D], corrs[k][D])

            check_overlap = True
            if check_overlap:
                true_ov = dot(corrs[0], corrs[k])
                overlaps[k] = true_ov
                if abs(true_ov - ok) > .005:
                    raise ValueError(
                        "state {} fails overlap relationship at order {} (contrib {}, expected {})".format(
                            state_index, order, true_ov, ok
                        ))

            # overlaps[k] = ok
            # corrs[k][D] += zero_order_state * ok

        if check_overlap:
            # full_wfn = np.sum(corrs, axis=0)
            # ov = np.dot(full_wfn, full_wfn)
            ov_parts = [[dot(corrs[k - i], corrs[i]) for i in range(k + 1)] for k in range(order + 1)]
            ov = sum(np.sum(v) for v in ov_parts)
            if abs(ov - 1) > .005:
                raise ValueError(
                    "state {} isn't normalized (overlap = {}, bits {})".format(
                        state_index, ov, ov_parts
                    ))

        return energies[:order+1], overlaps, corrs[:order+1]

    #endregion

    #region Handle Post-PT Variational Stuff
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

        if self.verbose:
            self.logger.log_print(["Deperturbed States/Energies:"] + str(
                np.column_stack([
                    total_state_space.excitations,
                    np.round(UnitsData.convert("Hartrees", "Wavenumbers") * base_energies).astype(int)
                    ])
            ).splitlines()
                                  )

        # this will be built from a series of block-diagonal matrices
        # so we store the relevant values and indices to compose the SparseArray
        rotation_vals = []
        rotation_row_inds = []
        rotation_col_inds = []

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
                deg_engs, deg_rot = self.get_degenerate_rotation(group, corrs)
                energies[deg_inds] = deg_engs
                rotation_vals.append(deg_rot.flatten())
                deg_rows, deg_cols = np.array([p for p in itertools.product(deg_inds, deg_inds)]).T
                rotation_row_inds.append(deg_rows)
                rotation_col_inds.append(deg_cols)
            else:
                self.logger.log_print("WARNING: got degeneracy spec that is not in total space")

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

        if self.verbose:
            block_logger = self.logger
        else:
            block_logger = NullLogger()
        with block_logger.block(tag='modifying perturbations'):
            for d,g in zip(deg_grop_inds, deg_groups):
                if len(d) > 1:
                    if self.verbose:
                        self.logger.log_print(
                            ["dropping elements coupling degenerate space:"] + str(g.excitations).splitlines()
                        )
                    idx = tuple(np.array([x for x in itertools.product(d, d) if x[0] != x[1]]).T)
                    els = []
                    for p in perts[1:]:
                        els.append(p[idx].flatten())
                        p[idx] = 0.
                    pert_blocks.append([idx, els])
                    if self.verbose:
                        triu = np.where(idx[0] > idx[1])
                        def pad_els(el, triu=triu):
                            e = np.zeros((len(d), len(d)))
                            e[np.triu_indices_from(e, k=1)] = el[triu]
                            e = np.round(e * UnitsData.convert("Hartrees", "Wavenumbers")).astype(int)
                            e[np.tril_indices_from(e, k=-1)] = e[np.triu_indices_from(e, k=1)]
                            return e

                        self.logger.log_print(
                            ["zeroed out coupling elements:"] +
                            sum(
                                (str(pad_els(e)).splitlines() for e in els),
                                []
                            )
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
                for x in subcorrs.operator_representation(subhams, subspace=deg_group)
            ]

        else:
            subhams = self.representations
            H_nd = [
                x.asarray() if isinstance(x, SparseArray) else x
                for x in corrs.operator_representation(subhams)
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
        H_nd = self.get_transformed_Hamiltonians(subdegs, None)
        # import McUtils.Plots as plt
        # plt.TensorPlot(np.array(H_nd)).show()
        H_nd = np.sum(H_nd, axis=0)


        with logger.block(tag="non-degenerate Hamiltonian"):
            logger.log_print(
                str(
                    np.round(H_nd * UnitsData.convert("Hartrees", "Wavenumbers")).astype(int)
                ).splitlines()
            )

        deg_engs, deg_transf = np.linalg.eigh(H_nd)

        for i in range(len(deg_transf)):
            max_ov = np.max(deg_transf[:, i] ** 2)
            ov_thresh = .5
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

        return deg_engs, deg_transf
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