
import numpy as np
from McUtils.Scaffolding import ParameterManager

from .ColbertMiller import PolarDVR, RingDVR, CartesianDVR
from .DirectProduct import DirectProductDVR
from .SCF import SelfConsistentDVR
from .PotentialOptimized import PotentialOptimizedDVR

__all__ = [
    "DVR"
]

class DVRConstructor:

    _domain_map = None
    @classmethod
    def load_domain_map(cls):

        return {
            (0, np.pi): PolarDVR,
            (0, 2*np.pi): RingDVR,
            None: CartesianDVR
        }
    @classmethod
    def infer_DVR_type(cls, domain):
        if cls._domain_map is None:
            cls._domain_map = cls.load_domain_map()
        for k,v in cls._domain_map.items():
            if k is not None:
                if np.allclose(k, domain):
                    return v
        else:
            return cls._domain_map[None]

    @classmethod
    def construct(cls,
                  domain=None,
                  divs=None,
                  potential_function=None,
                  g=None,
                  g_deriv=None,
                  mass=None,
                  classes=None,
                  logger=None,
                  scf=False,
                  potential_optimize=False,
                  **base_opts
                  ):

        # dispatches based on domain to construct the appropriate DVR
        if domain is None or divs is None:
            raise ValueError("can't have `None` for `domain` or `divs`")
        if isinstance(domain[0], (int, float, np.integer, np.floating)): # 1D
            domain = [domain]
            divs = [divs]
        if classes is None:
            classes = [None] * len(domain)
        if g is not None:
            if callable(g):
                subg = [g]
            else:
                subg = [g[i][i] for i in range(len(g))]
            mass = [None] * len(subg)
            if g_deriv is None:
                g_deriv = [None] * len(subg)
        else:
            subg = [None]*len(mass)
            g_deriv = [None]*len(mass)

        ndim = len(list(zip(domain, divs, classes, mass, subg, g_deriv)))
        if ndim == 1:
            dvr = classes[0](
                domain=domain[0],
                divs=divs[0],
                potential_function=potential_function,
                g=g,
                mass=mass,
                g_deriv=g_deriv,
                **base_opts
            )
        else:
            dvrs_1D = [
                cls.infer_DVR_type(r)(domain=r, divs=n, mass=m, g=sg, g_deriv=gd) if c is None else c(domain=r, divs=n)
                for r, n, c, m, sg, gd in zip(domain, divs, classes, mass, subg, g_deriv)
            ]
            dvr = DirectProductDVR(
                dvrs_1D,
                domain=domain,
                divs=divs,
                potential_function=potential_function,
                g=g,
                g_deriv=g_deriv,
                logger=logger,
                **ParameterManager(base_opts).exclude((SelfConsistentDVR))#, PotentialOptimizedDVR))
            )
            if potential_optimize:
                raise NotImplementedError("...")
            elif scf:
                dvr = SelfConsistentDVR(dvr, **ParameterManager(base_opts).filter(SelfConsistentDVR))
        return dvr


def DVR(
        domain=None,
        divs=None,
        classes=None,
        potential_function=None,
        g=None,
        g_deriv=None,
        scf=False,
        potential_optimize=False,
        **base_opts
):
    """
    Constructs a DVR object

    :param domain:
    :type domain:
    :param divs:
    :type divs:
    :param classes:
    :type classes:
    :param potential_function:
    :type potential_function:
    :param g:
    :type g:
    :param g_deriv:
    :type g_deriv:
    :param base_opts:
    :type base_opts:
    :return:
    :rtype:
    """

    return DVRConstructor.construct(
        domain=domain,
        divs=divs,
        classes=classes,
        potential_function=potential_function,
        g=g,
        g_deriv=g_deriv,
        scf=scf,
        potential_optimize=potential_optimize,
        **base_opts
    )