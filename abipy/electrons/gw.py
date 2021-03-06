# coding: utf-8
"""Classes for the analysis of GW calculations."""
from __future__ import print_function, division, unicode_literals, absolute_import

import sys
import copy
import numpy as np

from collections import namedtuple, OrderedDict, Iterable, defaultdict
from monty.string import list_strings, is_string, marquee
from monty.collections import AttrDict, dict2namedtuple
from monty.functools import lazy_property
from monty.termcolor import cprint
from monty.dev import deprecated
from monty.bisect import find_le, find_ge
from prettytable import PrettyTable
from six.moves import cStringIO
from abipy.core.func1d import Function1D
from abipy.core.kpoints import Kpoint, KpointList, Kpath, IrredZone, has_timrev_from_kptopt
from abipy.core.mixins import AbinitNcFile, Has_Structure, Has_ElectronBands, NotebookWriter
from abipy.iotools import ETSF_Reader
from abipy.electrons.ebands import ElectronBands
from abipy.electrons.scissors import Scissors
from abipy.tools.plotting import ArrayPlotter, plot_array, add_fig_kwargs, get_ax_fig_plt, Marker
from abipy.tools import duck

import logging
logger = logging.getLogger(__name__)

__all__ = [
    "QPState",
    "SigresFile",
    "SigresPlotter",
]


class QPState(namedtuple("QPState", "spin kpoint band e0 qpe qpe_diago vxcme sigxme sigcmee0 vUme ze0")):
    """
    Quasi-particle result for given (spin, kpoint, band).

    .. Attributes:

        spin: spin index (C convention, i.e >= 0)
        kpoint: :class:`Kpoint` object.
        band: band index. (C convention, i.e >= 0).
        e0: Initial KS energy.
        qpe: Quasiparticle energy (complex) computed with the perturbative approach.
        qpe_diago: Quasiparticle energy (real) computed by diagonalizing the self-energy.
        vxcme: Matrix element of vxc[n_val] with nval the valence charge density.
        sigxme: Matrix element of Sigma_x.
        sigcmee0: Matrix element of Sigma_c(e0) with e0 being the KS energy.
        vUme: Matrix element of the vU term of the LDA+U Hamiltonian.
        ze0: Renormalization factor computed at e=e0.

    .. note::

        Energies are in eV.
    """
    @property
    def qpeme0(self):
        """E_QP - E_0"""
        return self.qpe - self.e0

    @property
    def skb(self):
        """Tuple with (spin, kpoint, band)"""
        return self.spin, self.kpoint, self.band

    def copy(self):
        """Shallow copy."""
        d = {f: copy.copy(getattr(self, f)) for f in self._fields}
        return QPState(**d)

    @classmethod
    def get_fields(cls, exclude=()):
        fields = list(cls._fields) + ["qpeme0"]
        for e in exclude:
            fields.remove(e)
        return tuple(fields)

    def as_dict(self, **kwargs):
        """Convert self into a dictionary."""
        od = OrderedDict(zip(self._fields, self))
        od["qpeme0"] = self.qpeme0
        return od

    def to_strdict(self, fmt=None):
        """Ordered dictionary mapping fields --> strings."""
        d = self.as_dict()
        for k, v in d.items():
            if duck.is_intlike(v):
                d[k] = "%d" % int(v)

            elif isinstance(v, Kpoint):
                d[k] = "%s" % v

            elif np.iscomplexobj(v):
                if abs(v.imag) < 1.e-3:
                    d[k] = "%.2f" % v.real
                else:
                    d[k] = "%.2f%+.2fj" % (v.real, v.imag)

            else:
                try:
                    d[k] = "%.2f" % v
                except TypeError as exc:
                    #print("k", k, str(exc))
                    d[k] = str(v)
        return d

    @property
    def tips(self):
        """Bound method of self that returns a dictionary with the description of the fields."""
        return self.__class__.TIPS()

    @classmethod
    def TIPS(cls):
        """
        Class method that returns a dictionary with the description of the fields.
        The string are extracted from the class doc string.
        """
        try:
            return cls._TIPS

        except AttributeError:
            # Parse the doc string.
            cls._TIPS = _TIPS = {}
            lines = cls.__doc__.splitlines()

            for i, line in enumerate(lines):
                if line.strip().startswith(".. Attributes"):
                    lines = lines[i+1:]
                    break

            def num_leadblanks(string):
                """Returns the number of the leading whitespaces."""
                return len(string) - len(string.lstrip())

            for field in cls._fields:
                for i, line in enumerate(lines):

                    if line.strip().startswith(field + ":"):
                        nblanks = num_leadblanks(line)
                        desc = []
                        for s in lines[i+1:]:
                            if nblanks == num_leadblanks(s) or not s.strip():
                                break
                            desc.append(s.lstrip())

                        _TIPS[field] = "\n".join(desc)

            diffset = set(cls._fields) - set(_TIPS.keys())
            if diffset:
                raise RuntimeError("The following fields are not documented: %s" % str(diffset))

            return _TIPS


def _get_fields_for_plot(with_fields, exclude_fields):
    """
    Return list of fields to plot from input arguments.
    """
    all_fields = list(QPState.get_fields(exclude=["spin", "kpoint"]))

    # Initialize fields.
    if is_string(with_fields) and with_fields == "all":
        fields = all_fields
    else:
        fields = list_strings(with_fields)
        for f in fields:
            if f not in all_fields:
                raise ValueError("Field %s not in allowed values %s" % (f, all_fields))

    # Remove entries
    if exclude_fields:
        if is_string(exclude_fields):
            exclude_fields = exclude_fields.split()
        for e in exclude_fields:
            fields.remove(e)

    return fields


class QPList(list):
    """
    A list of quasiparticle corrections for a given spin.
    """
    def __init__(self, *args, **kwargs):
        super(QPList, self).__init__(*args)
        self.is_e0sorted = kwargs.get("is_e0sorted", False)

    def __repr__(self):
        return "<%s at %s, len=%d>" % (self.__class__.__name__, id(self), len(self))

    def __str__(self):
        """String representation."""
        return self.to_string()

    def to_string(self, **kwargs):
        """String representation."""
        table = self.to_table()
        strio = cStringIO()
        print(table, file=strio)
        strio.write("\n")
        strio.seek(0)

        return "".join(strio)

    def copy(self):
        """Copy of self."""
        return self.__class__([qp.copy() for qp in self], is_e0sorted=self.is_e0sorted)

    def sort_by_e0(self):
        """Return a new object with the E0 energies sorted in ascending order."""
        return QPList(sorted(self, key=lambda qp: qp.e0), is_e0sorted=True)

    def get_e0mesh(self):
        """Return the E0 energies."""
        if not self.is_e0sorted:
            raise ValueError("QPState corrections are not sorted. Use sort_by_e0")

        return np.array([qp.e0 for qp in self])

    def get_field(self, field):
        """`ndarray` containing the values of field."""
        return np.array([getattr(qp, field) for qp in self])

    def get_skb_field(self, skb, field):
        """Return the value of field for the given spin kp band tuple, None if not found"""
        for qp in self:
            if qp.skb == skb:
                return getattr(qp, field)
        return None

    def get_qpenes(self):
        """Return an array with the :class:`QPState` energies."""
        return self.get_field("qpe")

    def get_qpeme0(self):
        """Return an arrays with the :class:`QPState` corrections."""
        return self.get_field("qpeme0")

    def to_table(self):
        """Return a table (list of list of strings)."""
        header = QPState.get_fields(exclude=["spin", "kpoint"])
        table = PrettyTable(header)

        for qp in self:
            d = qp.to_strdict(fmt=None)
            table.add_row([d[k] for k in header])

        return table

    @add_fig_kwargs
    def plot_qps_vs_e0(self, with_fields="all", exclude_fields=None, axlist=None, label=None, **kwargs):
        """
        Plot the QP results as function of the initial KS energy.

        Args:
            with_fields: The names of the qp attributes to plot as function of e0.
                Accepts: List of strings or string with tokens separated by blanks.
                See :class:`QPState` for the list of available fields.
            exclude_fields: Similar to `with_field` but excludes fields
            axlist: List of matplotlib axes for plot. If None, new figure is produced
            label: Label for plot.

        ==============  ==============================================================
        kwargs          Meaning
        ==============  ==============================================================
        fermi           True to plot the Fermi level.
        ==============  ==============================================================

        Returns:
            `matplotlib` figure.
        """
        fermi = kwargs.pop("fermi", None)

        fields = _get_fields_for_plot(with_fields, exclude_fields)
        if not fields:
            return None

        num_plots, ncols, nrows = len(fields), 1, 1
        if num_plots > 1:
            ncols = 2
            nrows = (num_plots//ncols) + (num_plots % ncols)

        # Build grid of plots.
        import matplotlib.pyplot as plt
        if axlist is None:
            fig, axlist = plt.subplots(nrows=nrows, ncols=ncols, sharex=True, squeeze=False)
            axlist = axlist.ravel()
        else:
            axlist = np.reshape(axlist, (1, len(fields))).ravel()
            fig = plt.gcf()

        # Get qplist and sort it.
        qps = self if self.is_e0sorted else self.sort_by_e0()
        e0mesh = qps.get_e0mesh()

        linestyle = kwargs.pop("linestyle", "o")
        for ii, (field, ax) in enumerate(zip(fields, axlist)):
            irow, icol = divmod(ii, ncols)
            ax.grid(True)
            if irow == nrows - 1: ax.set_xlabel('e0 [eV]')
            ax.set_ylabel(field)
            yy = qps.get_field(field)
            lbl = label if ii == 0 and label is not None else None

            ax.plot(e0mesh, yy.real, linestyle, label=lbl, **kwargs)
            #ax.plot(e0mesh, e0mesh)

            if fermi is not None:
                ax.plot(2*[fermi], [min(yy), max(yy)])

        # Get around a bug in matplotlib
        if num_plots % ncols != 0:
            axlist[-1].plot([0,1], [0,1], lw=0)
            axlist[-1].axis('off')

        if label is not None:
            axlist[0].legend(loc="best")

        return fig

    def build_scissors(self, domains, bounds=None, k=3, **kwargs):
        """
        Construct a scissors operator by interpolating the QPState corrections
        as function of the initial energies E0.

        Args:
            domains: list in the form [ [start1, stop1], [start2, stop2]
                     Domains should not overlap, cover e0mesh, and given in increasing order.
                     Holes are permitted but the interpolation will raise an exception if the point is not in domains.
            bounds: Specify how to handle out-of-boundary conditions, i.e. how to treat
                    energies that do not fall inside one of the domains (not used at present)

        ==============  ==============================================================
        kwargs          Meaning
        ==============  ==============================================================
        plot             If true, use `matplolib` to compare input data  and fit.
        ==============  ==============================================================

        Return:
            instance of :class:`Scissors`operator

        Usage example:

        .. code-block:: python

            # Build the scissors operator.
            scissors = qplist_spin[0].build_scissors(domains)

            # Compute list of interpolated QP energies.
            qp_enes = [scissors.apply(e0) for e0 in ks_energies]
        """
        # Sort QP corrections according to the initial KS energy.
        qps = self.sort_by_e0()
        e0mesh, qpcorrs = qps.get_e0mesh(), qps.get_qpeme0()

        # Check domains.
        domains = np.atleast_2d(domains)
        dsize, dflat = domains.size, domains.ravel()

        for idx, v in enumerate(dflat):
            if idx == 0 and v > e0mesh[0]:
                raise ValueError("min(e0mesh) %s is not included in domains" % e0mesh[0])
            if idx == dsize-1 and v < e0mesh[-1]:
                raise ValueError("max(e0mesh) %s is not included in domains" % e0mesh[-1])
            if idx != dsize-1 and dflat[idx] > dflat[idx+1]:
                raise ValueError("domain boundaries should be given in increasing order.")
            if idx == dsize-1 and dflat[idx] < dflat[idx-1]:
                raise ValueError("domain boundaries should be given in increasing order.")

        # Create the sub_domains and the spline functions in each subdomain.
        func_list, residues = [], []

        if len(domains) == 2:
            #print('forcing extremal point on the scissor')
            ndom = 0
        else:
            ndom = 99

        for dom in domains[:]:
            ndom += 1
            low, high = dom[0], dom[1]
            start, stop = find_ge(e0mesh, low), find_le(e0mesh, high)

            dom_e0 = e0mesh[start:stop+1]
            dom_corr = qpcorrs[start:stop+1]

            # todo check if the number of non degenerate data points > k
            from scipy.interpolate import UnivariateSpline
            w = len(dom_e0)*[1]
            if ndom == 1:
                w[-1] = 1000
            elif ndom == 2:
                w[0] = 1000
            else:
                w = None
            f = UnivariateSpline(dom_e0, dom_corr, w=w, bbox=[None, None], k=k, s=None)
            func_list.append(f)
            residues.append(f.get_residual())

        # Build the scissors operator.
        sciss = Scissors(func_list, domains, residues, bounds)

        # Compare fit with input data.
        if kwargs.pop("plot", False):
            title = kwargs.pop("title", None)
            import matplotlib.pyplot as plt
            plt.plot(e0mesh, qpcorrs, 'o', label="input data")
            if title: plt.suptitle(title)
            for dom in domains[:]:
                plt.plot(2*[dom[0]], [min(qpcorrs), max(qpcorrs)])
                plt.plot(2*[dom[1]], [min(qpcorrs), max(qpcorrs)])
            intp_qpc = [sciss.apply(e0) for e0 in e0mesh]
            plt.plot(e0mesh, intp_qpc, label="scissor")
            plt.legend(bbox_to_anchor=(0.9, 0.2))
            plt.show()

        # Return the object.
        return sciss

    def merge(self, other, copy=False):
        """
        Merge self with other. Return new :class:`QPList` object

        Raise:
            ValueError if merge cannot be done.
        """
        skb0_list = [qp.skb for qp in self]
        for qp in other:
            if qp.skb in skb0_list:
                raise ValueError("Found duplicated (s,b,k) indexes: %s" % str(qp.skb))

        if copy:
            qps = self.copy() + other.copy()
        else:
            qps = self + other

        return self.__class__(qps)


class Sigmaw(object):
    """
    This object stores the values of the self-energy as function of frequency
    """

    def __init__(self, spin, kpoint, band, wmesh, sigmaxc_values, spfunc_values):
        self.spin, self.kpoint, self.band = spin, kpoint, band
        self.wmesh = np.array(wmesh)

        self.xc = Function1D(self.wmesh, sigmaxc_values)
        self.spfunc = Function1D(self.wmesh, spfunc_values)

    def plot_ax(self, ax, w="a", **kwargs):
        """Helper function to plot data on the axis ax."""
        #if not kwargs:
        #    kwargs = {"color": "black", "linewidth": 2.0}

        lines = []
        extend = lines.extend

        if w == "s":
            f = self.xc
            label = kwargs.get("label", r"$\Sigma(\omega)$")
            extend(f.plot_ax(ax, cplx_mode="re", label="Re " + label))
            extend(f.plot_ax(ax, cplx_mode="im", label="Im " + label))
            ax.legend(loc="best")
            #ax.set_ylabel('Energy [eV]')

        elif w == "a":
            f = self.spfunc
            label = kwargs.get("label", r"$A(\omega)$")
            extend(f.plot_ax(ax, label=label))
            # Plot I(w)
            #ax2 = ax.twinx()
            #extend(f.cumintegral().plot_ax(ax2, label="$I(\omega) = \int_{-\infty}^{\omega} A(\omega')d\omega'$"))
            #ax.set_ylabel('Energy [eV]')
            ax.legend(loc="best")

        else:
            raise ValueError("Don't know how to handle what option %s" % w)

        return lines

    @add_fig_kwargs
    def plot(self, what="sa", **kwargs):
        """
        Plot the self-energy and the spectral function

        Args:
            what: String specifying what to plot:
                    - s for the self-energy
                    - a for spectral function
                  Characters can be concatenated.

        Returns:
            `matplotlib` figure.
        """
        import matplotlib.pyplot as plt

        nrows = len(what)
        fig, axlist = plt.subplots(nrows=nrows, ncols=1, sharex=True, squeeze=False)
        axlist = axlist.ravel()

        title = 'spin %s, k-point %s, band %s' % (self.spin, self.kpoint, self.band)
        fig.suptitle(title)

        for i, w in enumerate(what):
            ax = axlist[i]
            ax.grid(True)

            if i == len(what):
                ax.set_xlabel('Frequency [eV]')

            if not kwargs:
                kwargs = {"color": "black", "linewidth": 2.0}

            self.plot_ax(ax, w=w, **kwargs)

        return fig


def torange(obj):
    """
    Convert obj into a range. Accepts integer, slice object  or any object
    with an __iter__ method. Note that an integer is converted into range(int, int+1)

    >>> list(torange(1))
    [1]
    >>> list(torange(slice(0, 4, 2)))
    [0, 2]
    >>> list(torange([1, 4, 2]))
    [1, 4, 2]
    """
    if duck.is_intlike(obj):
        return range(obj, obj + 1)

    elif isinstance(obj, slice):
        start = obj.start if obj.start is not None else 0
        step = obj.step if obj.step is not None else 1
        return range(start, obj.stop, step)

    else:
        try:
            return obj.__iter__()
        except:
            raise TypeError("Don't know how to convert %s into a range object" % str(obj))


class SigresPlotter(Iterable):
    """
    This object receives a list of :class:`SigresFile` objects and provides methods
    to inspect/analyze the GW results (useful for convergence studies)

    .. Attributes:

        nsppol:
            Number of spins (must be the same in each file)

        computed_gwkpoints:
            List of k-points where the QP energies have been evaluated.
            (must be the same in each file)

    Usage example:

    .. code-block:: python

        plotter = SigresPlotter()
        plotter.add_file("foo_SIGRES.nc", label="foo bands")
        plotter.add_file("bar_SIGRES.nc", label="bar bands")
        plotter.plot_qpgaps()
    """
    def __init__(self):
        self._sigres_files = OrderedDict()
        self._labels = []

    def __len__(self):
        return len(self._sigres_files)

    def __iter__(self):
        return iter(self._sigres_files.values())

    def __str__(self):
        return self.to_string()

    def to_string(self, verbose=0):
        """String representation."""
        s = ""
        for sigres in self:
            s += str(sigres) + "\n"
        return s

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Activated at the end of the with statement. It automatically closes the file."""
        self.close()

    def close(self):
        """Close files."""
        for sigres in self:
            try:
                sigres.close()
            except:
                pass

    def add_files(self, filepaths, labels=None):
        """Add a list of filenames to the plotter"""
        for i, filepath in enumerate(list_strings(filepaths)):
            label = None if labels is None else labels[i]
            self.add_file(filepath, label=label)

    def add_file(self, filepath, label=None):
        """Add a filename to the plotter"""
        from abipy.abilab import abiopen
        sigres = abiopen(filepath)
        self._sigres_files[sigres.filepath] = sigres
        # TODO: Not used
        self._labels.append(label)

        # Initialize/check useful quantities.
        #
        # 1) Number of spins
        if not hasattr(self, "nsppol"): self.nsppol = sigres.nsppol
        if self.nsppol != sigres.nsppol:
            raise ValueError("Found two SIGRES files with different nsppol")

        # The set of k-points where GW corrections have been computed.
        if not hasattr(self, "computed_gwkpoints"):
            self.computed_gwkpoints = sigres.gwkpoints

        if self.computed_gwkpoints != sigres.gwkpoints:
            raise ValueError("Found two SIGRES files with different list of GW k-points.")
            #self.computed_gwkpoints = (self.computed_gwkpoints + sigres.gwkpoints).remove_duplicated()

        if not hasattr(self, "max_gwbstart"):
            self.max_gwbstart = sigres.max_gwbstart
        else:
            self.max_gwbstart = max(self.max_gwbstart, sigres.max_gwbstart)

        if not hasattr(self, "min_gwbstop"):
            self.min_gwbstop = sigres.min_gwbstop
        else:
            self.min_gwbstop = min(self.min_gwbstop, sigres.min_gwbstop)

    @property
    def param_name(self):
        """
        The name of the parameter whose value is checked for convergence.
        This attribute is automatically individuated by inspecting the differences
        inf the sigres.params dictionaries of the files provided.
        """
        try:
            return self._param_name
        except AttributeError:
            self.set_param_name(param_name=None)
            return self.param_name

    def _get_param_list(self):
        """Return a dictionary with the values of the parameters extracted from the SIGRES files."""
        param_list = defaultdict(list)

        for sigres in self:
            for pname in sigres.params.keys():
                param_list[pname].append(sigres.params[pname])

        return param_list

    def set_param_name(self, param_name):
        """
        Set the name of the parameter whose value is checked for convergence.
        if param_name is None, we try to find its name by inspecting
        the values in the sigres.params dictionaries.
        """
        self._param_name = param_name

    def prepare_plot(self):
        """
        This method must be called before plotting data.
        It tries to figure the name of paramenter we are converging
        by looking at the set of parameters used to compute the different SIGRES files.
        """
        param_list = self._get_param_list()

        param_name, problem = None, False
        for key, value_list in param_list.items():
            if any(v != value_list[0] for v in value_list):
                if param_name is None:
                    param_name = key
                else:
                    problem = True
                    logger.warning("Cannot perform automatic detection of convergence parameter.\n" +
                                   "Found multiple parameters with different values. Will use filepaths as plot labels.")

        self.set_param_name(param_name if not problem else None)

        if self.param_name is None:
            # Could not figure the name of the parameter.
            xvalues = range(len(self))
        else:
            xvalues = param_list[self.param_name]

            # Sort xvalues and rearrange the files.
            items = sorted([iv for iv in enumerate(xvalues)], key=lambda item: item[1])
            indices = [item[0] for item in items]

            files = list(self._sigres_files.values())

            newd = OrderedDict()
            for i in indices:
                sigres = files[i]
                newd[sigres.filepath] = sigres

            self._sigres_files = newd

            # Use sorted xvalues for the plot.
            param_list = self._get_param_list()
            xvalues = param_list[self.param_name]

        self.set_xvalues(xvalues)

    @property
    def xvalues(self):
        """The values used for the X-axis."""
        return self._xvalues

    def set_xvalues(self, xvalues):
        """xvalues setter."""
        assert len(xvalues) == len(self)
        self._xvalues = xvalues

    def decorate_ax(self, ax, **kwargs):
        ax.grid(True)
        if self.param_name is not None:
            ax.set_xlabel(self.param_name)
        ax.set_ylabel('Energy [eV]')
        ax.legend(loc="best")

        title = kwargs.pop("title", None)
        if title is not None: ax.set_title(title)

        # Set ticks and labels.
        if self.param_name is None:
            # Could not figure the name of the parameter ==> Use the basename of the files
            ticks, labels = list(range(len(self))), [f.basename for f in self]
        else:
            ticks, labels = self.xvalues, [f.params[self.param_name] for f in self]

        ax.set_xticks(ticks, minor=False)
        ax.set_xticklabels(labels, fontdict=None, minor=False)

    def extract_qpgaps(self, spin, kpoint):
        """
        Returns a `ndarray` with the QP gaps for the given spin, kpoint.
        Values are ordered with the list of SIGRES files in self.
        """
        qpgaps = []
        for sigres in self:
            ik = sigres.ibz.index(kpoint)
            qpgaps.append(sigres.qpgaps[spin, ik])

        return np.array(qpgaps)

    def extract_qpenes(self, spin, kpoint, band):
        """
        Returns a complex array with the QP energies for the given spin, kpoint.
        Values are ordered with the list of SIGRES files in self.
        """
        qpenes = []
        for sigres in self:
            ik = sigres.ibz.index(kpoint)
            qpenes.append(sigres.qpenes[spin, ik, band])

        return np.array(qpenes, dtype=np.complex)

    @add_fig_kwargs
    def plot_qpgaps(self, ax=None, spin=None, kpoint=None, hspan=0.01, **kwargs):
        """
        Plot the QP gaps as function of the convergence parameter.

        Args:
            ax: matplotlib :class:`Axes` or None if a new figure should be created.
            spin:
            kpoint:
            hspan:
            kwargs:

        Returns:
            `matplotlib` figure
        """
        spin_range = range(self.nsppol) if spin is None else torange(spin)

        if kpoint is None:
            kpoints_for_plot = self.computed_gwkpoints  #if kpoint is None else KpointList.as_kpoints(kpoint)
        else:
            kpoints_for_plot = np.reshape(kpoint, (-1, 3))

        self.prepare_plot()

        ax, fig, plt = get_ax_fig_plt(ax)

        xx = self.xvalues
        for spin in spin_range:
            for kpoint in kpoints_for_plot:
                label = "spin %d, kpoint %s" % (spin, repr(kpoint))
                gaps = self.extract_qpgaps(spin, kpoint)
                ax.plot(xx, gaps, "o-", label=label, **kwargs)

                if hspan is not None:
                    last = gaps[-1]
                    ax.axhspan(last-hspan, last+hspan, facecolor='0.5', alpha=0.5)

        self.decorate_ax(ax)
        return fig

    @add_fig_kwargs
    def plot_qpenes(self, spin=None, kpoint=None, band=None, hspan=0.01, **kwargs):
        """
        Plot the QP energies as function of the convergence parameter.

        Args:
            spin:
            kpoint:
            band:
            hspan:
            kwargs:

        Returns:
            `matplotlib` figure
        """
        spin_range = range(self.nsppol) if spin is None else torange(spin)
        band_range = range(self.max_gwbstart, self.min_gwbstop) if band is None else torange(band)
        if kpoint is None:
            kpoints_for_plot = self.computed_gwkpoints
        else:
            kpoints_for_plot = np.reshape(kpoint, (-1, 3))

        self.prepare_plot()

        # Build grid of plots.
        import matplotlib.pyplot as plt
        num_plots, ncols, nrows = len(kpoints_for_plot), 1, 1
        if num_plots > 1:
            ncols = 2
            nrows = (num_plots//ncols) + (num_plots % ncols)

        fig, axlist = plt.subplots(nrows=nrows, ncols=ncols, sharex=False, squeeze=False)
        axlist = axlist.ravel()

        if num_plots % ncols != 0:
            axlist[-1].axis('off')

        xx = self.xvalues
        for kpoint, ax in zip(kpoints_for_plot, axlist):

            for spin in spin_range:
                for band in band_range:
                    label = "spin %d, band %d" % (spin, band)
                    qpenes = self.extract_qpenes(spin, kpoint, band).real
                    ax.plot(xx, qpenes, "o-", label=label, **kwargs)

                    if hspan is not None:
                        last = qpenes[-1]
                        ax.axhspan(last - hspan, last + hspan, facecolor='0.5', alpha=0.5)

            self.decorate_ax(ax, title="kpoint %s" % repr(kpoint))

        return fig

    @add_fig_kwargs
    def plot_qps_vs_e0(self, with_fields="all", exclude_fields=None, **kwargs):
        """
        Plot the QP results as function of the initial KS energy for all SIGRES files stored in the plotter.

        Args:
            with_fields: The names of the qp attributes to plot as function of e0.
                Accepts: List of strings or string with tokens separated by blanks.
                See :class:`QPState` for the list of available fields.
            exclude_fields: Similar to `with_field` but excludes fields
            axlist: List of matplotlib axes for plot. If None, new figure is produced

        Returns:
            `matplotlib` figure.
        """
        fields = _get_fields_for_plot(with_fields, exclude_fields)
        if not fields:
            return None

        # Build plot grid
        import matplotlib.pyplot as plt
        num_plots, ncols, nrows = len(fields), 1, 1
        if num_plots > 1:
            ncols = 2
            nrows = (num_plots//ncols) + (num_plots % ncols)

        fig, axlist = plt.subplots(nrows=nrows, ncols=ncols, sharex=True, squeeze=False)

        for sigres in self:
            label = sigres.basename
            fig = sigres.plot_qps_vs_e0(with_fields=fields, axlist=axlist,
                                        label=label, show=False, **kwargs)
        return fig


class SigresFile(AbinitNcFile, Has_Structure, Has_ElectronBands, NotebookWriter):
    """
    Container storing the GW results reported in the SIGRES.nc file.

    Usage example:

    .. code-block:: python

        sigres = SigresFile("foo_SIGRES.nc")
        sigres.plot_qps_vs_e0()
    """
    @classmethod
    def from_file(cls, filepath):
        """Initialize an instance from file."""
        return cls(filepath)

    def __init__(self, filepath):
        """Read data from the netcdf file path."""
        super(SigresFile, self).__init__(filepath)

        # Keep a reference to the SigresReader.
        self.reader = reader = SigresReader(self.filepath)

        self._structure = reader.read_structure()
        self.gwcalctyp = reader.gwcalctyp
        self.ibz = reader.ibz
        self.gwkpoints = reader.gwkpoints

        self.gwbstart_sk = reader.gwbstart_sk
        self.gwbstop_sk = reader.gwbstop_sk

        self.min_gwbstart = reader.min_gwbstart
        self.max_gwbstart = reader.max_gwbstart

        self.min_gwbstop = reader.min_gwbstop
        self.max_gwbstop = reader.max_gwbstop

        self._ebands = ebands = reader.ks_bands

        qplist_spin = self.qplist_spin

        # TODO handle the case in which nkptgw < nkibz
        self.qpgaps = reader.read_qpgaps()
        self.qpenes = reader.read_qpenes()

    def get_marker(self, qpattr):
        """
        Return :class:`Marker` object associated to the QP attribute qpattr.
        Used to prepare plots of KS bands with markers.
        """
        # Each marker is a list of tuple(x, y, value)
        x, y, s = [], [], []

        for spin in range(self.nsppol):
            for qp in self.qplist_spin[spin]:
                ik = self.ebands.kpoints.index(qp.kpoint)
                x.append(ik)
                y.append(qp.e0)
                size = getattr(qp, qpattr)
                # Handle complex quantities
                if np.iscomplex(size): size = size.real
                s.append(size)

        return Marker(*(x, y, s))

    @lazy_property
    def params(self):
        """AttrDict dictionary with the GW convergence parameters, e.g. ecuteps"""
        return self.reader.read_params()

    def close(self):
        """Close the netcdf file."""
        self.reader.close()

    def __str__(self):
        return self.to_string()

    def to_string(self, verbose=0):
        """String representation."""
        lines = []; app = lines.append

        app(marquee("File Info", mark="="))
        app(self.filestat(as_string=True))
        app("")
        app(self.ebands.to_string(title="Kohn-Sham bands"))

        # TODO: Finalize the implementation: add GW quantities.

        return "\n".join(lines)

    @property
    def structure(self):
        """Structure` instance."""
        return self._structure

    @property
    def ebands(self):
        """`ElectronBands with the KS energies."""
        return self._ebands

    @lazy_property
    def qplist_spin(self):
        """Tuple of :class:`QPList` objects indexed by spin."""
        return self.reader.read_allqps()

    def get_qplist(self, spin, kpoint):
        """Return :class`QPList` for the given (spin, kpoint)"""
        return self.reader.read_qplist_sk(spin, kpoint)

    def get_qpcorr(self, spin, kpoint, band):
        """Returns the :class:`QPState` object for the given (s, k, b)"""
        return self.reader.read_qp(spin, kpoint, band)

    @lazy_property
    def qpgaps(self):
        """ndarray with shape [nsppol, nkibz] in eV"""
        return self.reader.read_qpgaps()

    def get_qpgap(self, spin, kpoint):
        """Return the QP gap in eV at the given (spin, kpoint)"""
        k = self.reader.kpt2fileindex(kpoint)
        return self.qpgaps[spin, k]

    def get_sigmaw(self, spin, kpoint, band):
        """"
        Read self-energy(w) for (spin, kpoint, band)
        Return :class:`Function1D` object
        """
        wmesh, sigxc_values = self.reader.read_sigmaw(spin, kpoint, band)
        wmesh, spf_values = self.reader.read_spfunc(spin, kpoint, band)

        return Sigmaw(spin, kpoint, band, wmesh, sigxc_values, spf_values)

    def get_spfunc(self, spin, kpoint, band):
        """"
        Read spectral function for (spin, kpoint, band)
        Return :class:`Function1D` object
        """
        wmesh, spf_values = self.reader.read_spfunc(spin, kpoint, band)
        return Function1D(wmesh, spf_values)

    @deprecated(message="print_qps is deprecated and will be removed in version 0.4")
    def print_qps(self, **kwargs):
        self.reader.print_qps(**kwargs)

    @add_fig_kwargs
    def plot_qps_vs_e0(self, with_fields="all", exclude_fields=None, axlist=None, label=None, **kwargs):
        """
        Plot QP result as function of the KS energy.

        Args:
            with_fields: The names of the qp attributes to plot as function of e0.
                Accepts: List of strings or string with tokens separated by blanks.
                See :class:`QPState` for the list of available fields.
            exclude_fields: Similar to `with_field` but excludes fields
            axlist: List of matplotlib axes for plot. If None, new figure is produced
            label: Label for plot.

        Returns:
            `matplotlib` figure.
        """
        with_fields = _get_fields_for_plot(with_fields, exclude_fields)

        for spin in range(self.nsppol):
            qps = self.qplist_spin[spin].sort_by_e0()
            fig = qps.plot_qps_vs_e0(with_fields=with_fields, axlist=axlist, label=label, show=False, **kwargs)

        return fig

    @add_fig_kwargs
    def plot_spectral_functions(self, spin, kpoint, bands, ax=None, **kwargs):
        """
        Args:
            spin: Spin index.
            kpoint: Required kpoint.
            bands: List of bands
            ax: matplotlib :class:`Axes` or None if a new figure should be created.

        Returns:
            `matplotlib` figure
        """
        if not isinstance(bands, Iterable): bands = [bands]

        ax, fig, plt = get_ax_fig_plt(ax)
        ax.grid(True)

        errlines = []
        for band in bands:
            try:
                sigw = self.get_sigmaw(spin, kpoint, band)
            except ValueError as exc:
                errlines.append(str(exc))
                continue
            label = r"$A(\omega)$: skb = %s, %s, %s" % (spin, kpoint, band)
            sigw.plot_ax(ax, label=label, **kwargs)

        if errlines:
            cprint("\n".join(errlines), "red")
            return None

        return fig

    @add_fig_kwargs
    def plot_eigvec_qp(self, spin, kpoint, band=None, **kwargs):

        if kpoint is None:
            plotter = ArrayPlotter()
            for kpoint in self.ibz:
                ksqp_arr = self.reader.read_eigvec_qp(spin, kpoint, band=band)
                plotter.add_array(str(kpoint), ksqp_arr)

            return plotter.plot(show=False, **kwargs)

        else:
            ksqp_arr = self.reader.read_eigvec_qp(spin, kpoint, band=band)
            return plot_array(ksqp_arr, show=False, **kwargs)

    @add_fig_kwargs
    def plot_ksbands_with_qpmarkers(self, qpattr="qpeme0", e0="fermie", fact=1, ax=None, **kwargs):
        """
        Plot the KS energies as function of k-points and add markers whose size
        is proportional to the QPState attribute `qpattr`

        Args:
            qpattr: Name of the QP attribute to plot. See :class:`QPState`.
            e0: Option used to define the zero of energy in the band structure plot. Possible values:
                - `fermie`: shift all eigenvalues to have zero energy at the Fermi energy (`self.fermie`).
                -  Number e.g e0=0.5: shift all eigenvalues to have zero energy at 0.5 eV
                -  None: Don't shift energies, equivalent to e0=0
            fact: Markers are multiplied by this factor.
            ax: matplotlib :class:`Axes` or None if a new figure should be created.

        Returns:
            `matplotlib` figure
        """
        ax, fig, plt = get_ax_fig_plt(ax)

        gwband_range = self.min_gwbstart, self.max_gwbstop
        self.ebands.plot(band_range=gwband_range, e0=e0, ax=ax, show=False, **kwargs)

        e0 = self.ebands.get_e0(e0)
        marker = self.get_marker(qpattr)
        pos, neg = marker.posneg_marker()

        # Use different symbols depending on the value of s. Cannot use negative s.
        if pos:
            ax.scatter(pos.x, pos.y - e0, s=np.abs(pos.s) * fact, marker="^", label=qpattr + " >0")
        if neg:
            ax.scatter(neg.x, neg.y - e0, s=np.abs(neg.s) * fact, marker="v", label=qpattr + " <0")

        return fig

    def to_dataframe(self):
        """
        Returns pandas DataFrame with QP results for all k-points included in the GW calculation
        """
        import pandas as pd
        df_list = []
        for spin in range(self.nsppol):
            for gwkpoint in self.gwkpoints:
                df_sk = self.get_dataframe_sk(spin, gwkpoint)
                df_list.append(df_sk)

        return pd.concat(df_list)

    def get_dataframe_sk(self, spin, kpoint, index=None):
        """
        Returns pandas DataFrame with QP results for the given (spin, k-point).
        """
        rows, bands = [], []
        # FIXME start and stop should depend on k
        for band in range(self.min_gwbstart, self.max_gwbstop):
            bands.append(band)
            # Build dictionary with the QP results.
            qpstate = self.reader.read_qp(spin, kpoint, band)
            d = qpstate.as_dict()
            # Add other entries that may be useful when comparing different calculations.
            d.update(self.params)
            rows.append(d)

        import pandas as pd
        index = len(bands) * [index] if index is not None else bands
        return pd.DataFrame(rows, index=index, columns=list(rows[0].keys()))

    #def plot_matrix_elements(self, mel_name, spin, kpoint, *args, **kwargs):
    #   matrix = self.reader.read_mel(mel_name, spin, kpoint):
    #   return plot_matrix(matrix, *args, **kwargs)

    #def plot_mlda_to_qps(self, spin, kpoint, *args, **kwargs):
    #    matrix = self.reader.read_mlda_to_qps(spin, kpoint)
    #    return plot_matrix(matrix, *args, **kwargs)

    def interpolate(self, lpratio=5, ks_ebands_kpath=None, ks_ebands_kmesh=None, ks_degatol=1e-4,
                    vertices_names=None, line_density=20, filter_params=None, only_corrections=False, verbose=0):
        """
        Interpolated the GW corrections in k-space on a k-path and, optionally, on a k-mesh.

        Args:
            lpratio: Ratio between the number of star functions and the number of ab-initio k-points.
                The default should be OK in many systems, larger values may be required for accurate derivatives.
            ks_ebands_kpath: KS :class:`ElectronBands` on a k-path. If present,
                the routine interpolates the QP corrections and apply them on top of the KS band structure
                This is the recommended option because QP corrections are usually smoother than the
                QP energies and therefore easier to interpolate. If None, the QP energies are interpolated
                along the path defined by `vertices_names` and `line_density`.
            ks_ebands_kmesh: KS :class:`ElectronBands` on a homogeneous k-mesh. If present, the routine
                interpolates the corrections on the k-mesh (used to compute QP DOS)
            ks_degatol: Energy tolerance in eV. Used when either `ks_ebands_kpath` or `ks_ebands_kmesh` are given.
                KS energies are assumed to be degenerate if they differ by less than this value.
                The interpolator may break band degeneracies (the error is usually smaller if QP corrections
                are interpolated instead of QP energies). This problem can be partly solved by averaging
                the interpolated values over the set of KS degenerate states.
                A negative value disables this ad-hoc symmetrization.
            vertices_names: Used to specify the k-path for the interpolated QP band structure
                when `ks_ebands_kpath` is None.
                It's a list of tuple, each tuple is of the form (kfrac_coords, kname) where
                kfrac_coords are the reduced coordinates of the k-point and kname is a string with the name of
                the k-point. Each point represents a vertex of the k-path. `line_density` defines
                the density of the sampling. If None, the k-path is automatically generated according
                to the point group of the system.
            line_density: Number of points in the smallest segment of the k-path. Used with `vertices_names`.
            filter_params: TO BE DESCRIBED
            only_corrections: If True, the output contains the interpolated QP corrections instead of the QP energies.
                Available only if ks_ebands_kpath and/or ks_ebands_kmesh are used.
            verbose: Verbosity level

        Returns:

            :class:`namedtuple` with the following attributes:

                qp_ebands_kpath: :class:`ElectronBands` with the QP energies interpolated along the k-path.
                qp_ebands_kmesh: :class:`ElectronBands` with the QP energies interpolated on the k-mesh.
                    None if `ks_ebands_kmesh` is not passed.
                ks_ebands_kpath: :class:`ElectronBands` with the KS energies interpolated along the k-path.
                ks_ebands_kmesh: :class:`ElectronBands` with the KS energies on the k-mesh..
                    None if `ks_ebands_kmesh` is not passed.
                interpolator: :class:`SkwInterpolator` object.
        """
        # TODO: Consistency check.
        errlines = []
        eapp = errlines.append
        if len(self.gwkpoints) != len(self.ibz):
            eapp("QP energies should be computed for all k-points in the IBZ but nkibz != nkptgw")
        if len(self.gwkpoints) == 1:
            eapp("QP Interpolation requires nkptgw > 1.")
        #if (np.any(self.gwbstop_sk[0, 0] != self.gwbstop_sk):
        #    cprint("Highest bdgw band is not constant over k-points. QP Bands will be interpolated up to...")
        #if (np.any(self.gwbstart_sk[0, 0] != self.gwbstart_sk):
        #if (np.any(self.gwbstart_sk[0, 0] != 0):

        if errlines:
            raise ValueError("\n".join(errlines))

        # Get symmetries from abinit spacegroup (read from file).
        abispg = self.structure.abi_spacegroup
        fm_symrel = [s for (s, afm) in zip(abispg.symrel, abispg.symafm) if afm == 1]

        if ks_ebands_kpath is None:
            # Generate k-points for interpolation. Will interpolate all bands available in the sigres file.
            bstart, bstop = 0, -1
            if vertices_names is None:
                vertices_names = [(k.frac_coords, k.name) for k in self.structure.hsym_kpoints]
            kpath = Kpath.from_vertices_and_names(self.structure, vertices_names, line_density=line_density)
            kfrac_coords, knames = kpath.frac_coords, kpath.names

        else:
            # Use list of k-points from ks_ebands_kpath.
            ks_ebands_kpath = ElectronBands.as_ebands(ks_ebands_kpath)
            kfrac_coords = [k.frac_coords for k in ks_ebands_kpath.kpoints]
            knames = [k.name for k in ks_ebands_kpath.kpoints]

            # Find the band range for the interpolation.
            bstart, bstop = 0, ks_ebands_kpath.nband
            bstop = min(bstop, self.min_gwbstop)
            if ks_ebands_kpath.nband < self.min_gwbstop:
                cprint("Number of bands in KS band structure smaller than the number of bands in GW corrections", "red")
                cprint("Highest GW bands will be ignored", "red")

        # Interpolate QP energies if ks_ebands_kpath is None else interpolate QP corrections
        # and re-apply them on top of the KS band structure.
        gw_kcoords = [k.frac_coords for k in self.gwkpoints]

        # Read GW energies from file (real part) and compute corrections if ks_ebands_kpath.
        egw_rarr = self.reader.read_value("egw", cmode="c").real
        if ks_ebands_kpath is not None:
            if ks_ebands_kpath.structure != self.structure:
                cprint("sigres.structure and ks_ebands_kpath.structures differ. Check your files!", "red")
            egw_rarr -= self.reader.read_value("e0")

        # Note there's no guarantee that the gw_kpoints and the corrections have the same k-point index.
        # Be careful because the order of the k-points and the band range stored in the SIGRES file may differ ...
        qpdata = np.empty(egw_rarr.shape)
        for gwk in self.gwkpoints:
            ik_ibz = self.reader.kpt2fileindex(gwk)
            for spin in range(self.nsppol):
                qpdata[spin, ik_ibz, :] = egw_rarr[spin, ik_ibz, :]

        # Build interpolator for QP corrections.
        from abipy.core.skw import SkwInterpolator
        cell = (self.structure.lattice.matrix, self.structure.frac_coords, self.structure.atomic_numbers)
        qpdata = qpdata[:, :, bstart:bstop]
        has_timrev = has_timrev_from_kptopt(self.reader.read_value("kptopt"))

        skw = SkwInterpolator(lpratio, gw_kcoords, qpdata, self.ebands.fermie, self.ebands.nelect,
                              cell, fm_symrel, has_timrev,
                              filter_params=filter_params, verbose=verbose)

        if ks_ebands_kpath is None:
            # Interpolate QP energies.
            eigens_kpath = skw.interp_kpts(kfrac_coords).eigens
        else:
            # Interpolate QP energies corrections and add them to KS.
            ref_eigens = ks_ebands_kpath.eigens[:, :, bstart:bstop]
            qp_corrs = skw.interp_kpts_and_enforce_degs(kfrac_coords, ref_eigens, atol=ks_degatol).eigens
            eigens_kpath = qp_corrs if only_corrections else ref_eigens + qp_corrs

        # Build new ebands object with k-path.
        kpts_kpath = Kpath(self.structure.reciprocal_lattice, kfrac_coords, weights=None, names=knames)
        occfacts_kpath = np.zeros(eigens_kpath.shape)

        # Finding the new Fermi level of the interpolated bands is not trivial, in particular if metallic.
        # because one should first interpolate the QP bands on a mesh. Here I align the QP bands
        # at the HOMO of the KS bands.
        homos = ks_ebands_kpath.homos if ks_ebands_kpath is not None else self.ebands.homos
        qp_fermie = max([eigens_kpath[e.spin, e.kidx, e.band] for e in homos])
        #qp_fermie = self.ebands.fermie
        #qp_fermie = 0.0

        qp_ebands_kpath = ElectronBands(self.structure, kpts_kpath, eigens_kpath, qp_fermie, occfacts_kpath,
                                        self.ebands.nelect, self.ebands.nspinor, self.ebands.nspden)

        qp_ebands_kmesh = None
        if ks_ebands_kmesh is not None:
            # Interpolate QP corrections on the same k-mesh as the one used in the KS run.
            ks_ebands_kmesh = ElectronBands.as_ebands(ks_ebands_kmesh)
            if bstop > ks_ebands_kmesh.nband:
                raise ValueError("Not enough bands in ks_ebands_kmesh, found %s, minimum expected %d\n" % (
                    ks_ebands_kmesh%nband, bstop))

            if ks_ebands_kpath.structure != self.structure:
                raise ValueError("sigres.structure and ks_ebands_kmesh.structures differ. Check your files!")

            # K-points and weight for DOS are taken from ks_ebands_kmesh
            dos_kcoords = [k.frac_coords for k in ks_ebands_kmesh.kpoints]
            dos_weights = [k.weight for k in ks_ebands_kmesh.kpoints]

            # Interpolate QP corrections from bstart to bstop
            ref_eigens = ks_ebands_kmesh.eigens[:, :, bstart:bstop]
            qp_corrs = skw.interp_kpts_and_enforce_degs(dos_kcoords, ref_eigens, atol=ks_degatol).eigens
            eigens_kmesh = qp_corrs if only_corrections else ref_eigens + qp_corrs

            # Build new ebands object with k-mesh
            #ksampling = KSamplingInfo.from_mpdivs(mpdivs=kmesh, shifts=[0,0,0], kptopt=1)
            kpts_kmesh = IrredZone(self.structure.reciprocal_lattice, dos_kcoords, weights=dos_weights,
                                   names=None, ksampling=ks_ebands_kmesh.kpoints.ksampling)
            occfacts_kmesh = np.zeros(eigens_kmesh.shape)
            qp_ebands_kmesh = ElectronBands(self.structure, kpts_kmesh, eigens_kmesh, qp_fermie, occfacts_kmesh,
                                            self.ebands.nelect, self.ebands.nspinor, self.ebands.nspden)

        return dict2namedtuple(qp_ebands_kpath=qp_ebands_kpath,
                               qp_ebands_kmesh=qp_ebands_kmesh,
                               ks_ebands_kpath=ks_ebands_kpath,
                               ks_ebands_kmesh=ks_ebands_kmesh,
                               interpolator=skw,
                               )

    def write_notebook(self, nbpath=None):
        """
        Write a jupyter notebook to nbpath. If nbpath is None, a temporay file in the current
        working directory is created. Return path to the notebook.
        """
        nbformat, nbv, nb = self.get_nbformat_nbv_nb(title=None)

        nb.cells.extend([
            nbv.new_code_cell("sigres = abilab.abiopen('%s')" % self.filepath),
            nbv.new_code_cell("print(sigres)"),
            nbv.new_code_cell("fig = sigres.plot_qps_vs_e0()"),
            nbv.new_code_cell("fig = sigres.plot_spectral_functions(spin=0, kpoint=[0, 0, 0], bands=0)"),
            nbv.new_code_cell("#fig = sigres.plot_ksbands_with_qpmarkers(qpattr='qpeme0', fact=100)"),
            nbv.new_code_cell("r = sigres.interpolate(ks_ebands_kpath=None, ks_ebands_kmesh=None); print(r.interpolator)"),
            nbv.new_code_cell("fig = r.qp_ebands_kpath.plot()"),
            nbv.new_code_cell("""
if r.ks_ebands_kpath is not None:
    plotter = abilab.ElectronBandsPlotter()
    plotter.add_ebands("KS", r.ks_ebands_kpath) # dos=r.ks_ebands_kmesh.get_edos())
    plotter.add_ebands("GW (interpolated)", r.qp_ebands_kpath) # dos=r.qp_ebands_kmesh.get_edos()))
    plotter.ipw_select_plot()"""),
        ])

        return self._write_nb_nbpath(nb, nbpath)


class SigresReader(ETSF_Reader):
    r"""
    This object provides method to read data from the SIGRES file produced ABINIT.

    # See 70gw/m_sigma_results.F90

    # Name of the diagonal matrix elements stored in the file.
    # b1gw:b2gw,nkibz,nsppol*nsig_ab))
    #_DIAGO_MELS = [
    #    "sigxme",
    #    "vxcme",
    #    "vUme",
    #    "dsigmee0",
    #    "sigcmee0",
    #    "sigxcme",
    #    "ze0",
    #]

    integer :: b1gw,b2gw      ! min and Max gw band indeces over spin and k-points (used to dimension)
    integer :: gwcalctyp      ! Flag defining the calculation type.
    integer :: nkptgw         ! No. of points calculated
    integer :: nkibz          ! No. of irreducible k-points.
    integer :: nbnds          ! Total number of bands
    integer :: nomega_r       ! No. of real frequencies for the spectral function.
    integer :: nomega_i       ! No. of frequencies along the imaginary axis.
    integer :: nomega4sd      ! No. of real frequencies to evaluate the derivative of $\Sigma(E)$.
    integer :: nsig_ab        ! 1 if nspinor=1,4 for noncollinear case.
    integer :: nsppol         ! No. of spin polarizations.
    integer :: usepawu        ! 1 if we are using LDA+U as starting point (only for PAW)

    real(dp) :: deltae       ! Frequency step for the calculation of d\Sigma/dE
    real(dp) :: maxomega4sd  ! Max frequency around E_ks for d\Sigma/dE.
    real(dp) :: maxomega_r   ! Max frequency for spectral function.
    real(dp) :: scissor_ene  ! Scissor energy value. zero for None.

    integer,pointer :: maxbnd(:,:)
    ! maxbnd(nkptgw,nsppol)
    ! Max band index considered in GW for this k-point.

    integer,pointer :: minbnd(:,:)
    ! minbnd(nkptgw,nsppol)
    ! Min band index considered in GW for this k-point.

    real(dp),pointer :: degwgap(:,:)
    ! degwgap(nkibz,nsppol)
    ! Difference btw the QPState and the KS optical gap.

    real(dp),pointer :: egwgap(:,:)
    ! egwgap(nkibz,nsppol))
    ! QPState optical gap at each k-point and spin.

    real(dp),pointer :: en_qp_diago(:,:,:)
    ! en_qp_diago(nbnds,nkibz,nsppol))
    ! QPState energies obtained from the diagonalization of the Hermitian approximation to Sigma (QPSCGW)

    real(dp),pointer :: e0(:,:,:)
    ! e0(nbnds,nkibz,nsppol)
    ! KS eigenvalues for each band, k-point and spin. In case of self-consistent?

    real(dp),pointer :: e0gap(:,:)
    ! e0gap(nkibz,nsppol),
    ! KS gap at each k-point, for each spin.

    real(dp),pointer :: omega_r(:)
    ! omega_r(nomega_r)
    ! real frequencies used for the self energy.

    real(dp),pointer :: kptgw(:,:)
    ! kptgw(3,nkptgw)
    ! ! TODO there is a similar array in sigma_parameters
    ! List of calculated k-points.

    real(dp),pointer :: sigxme(:,:,:)
    ! sigxme(b1gw:b2gw,nkibz,nsppol*nsig_ab))
    ! Diagonal matrix elements of $\Sigma_x$ i.e $\<nks|\Sigma_x|nks\>$

    real(dp),pointer :: vxcme(:,:,:)
    ! vxcme(b1gw:b2gw,nkibz,nsppol*nsig_ab))
    ! $\<nks|v_{xc}[n_val]|nks\>$ matrix elements of vxc (valence-only contribution).

    real(dp),pointer :: vUme(:,:,:)
    ! vUme(b1gw:b2gw,nkibz,nsppol*nsig_ab))
    ! $\<nks|v_{U}|nks\>$ for LDA+U.

    complex(dpc),pointer :: degw(:,:,:)
    ! degw(b1gw:b2gw,nkibz,nsppol))
    ! Difference between the QPState and the KS energies.

    complex(dpc),pointer :: dsigmee0(:,:,:)
    ! dsigmee0(b1gw:b2gw,nkibz,nsppol*nsig_ab))
    ! Derivative of $\Sigma_c(E)$ calculated at the KS eigenvalue.

    complex(dpc),pointer :: egw(:,:,:)
    ! egw(nbnds,nkibz,nsppol))
    ! QPState energies, $\epsilon_{nks}^{QPState}$.

    complex(dpc),pointer :: eigvec_qp(:,:,:,:)
    ! eigvec_qp(nbnds,nbnds,nkibz,nsppol))
    ! Expansion of the QPState amplitude in the KS basis set.

    complex(dpc),pointer :: hhartree(:,:,:,:)
    ! hhartree(b1gw:b2gw,b1gw:b2gw,nkibz,nsppol*nsig_ab)
    ! $\<nks|T+v_H+v_{loc}+v_{nl}|mks\>$

    complex(dpc),pointer :: sigcme(:,:,:,:)
    ! sigcme(b1gw:b2gw,nkibz,nomega_r,nsppol*nsig_ab))
    ! $\<nks|\Sigma_{c}(E)|nks\>$ at each nomega_r frequency

    complex(dpc),pointer :: sigmee(:,:,:)
    ! sigmee(b1gw:b2gw,nkibz,nsppol*nsig_ab))
    ! $\Sigma_{xc}E_{KS} + (E_{QPState}- E_{KS})*dSigma/dE_KS

    complex(dpc),pointer :: sigcmee0(:,:,:)
    ! sigcmee0(b1gw:b2gw,nkibz,nsppol*nsig_ab))
    ! Diagonal mat. elements of $\Sigma_c(E)$ calculated at the KS energy $E_{KS}$

    complex(dpc),pointer :: sigcmesi(:,:,:,:)
    ! sigcmesi(b1gw:b2gw,nkibz,nomega_i,nsppol*nsig_ab))
    ! Matrix elements of $\Sigma_c$ along the imaginary axis.
    ! Only used in case of analytical continuation.

    complex(dpc),pointer :: sigcme4sd(:,:,:,:)
    ! sigcme4sd(b1gw:b2gw,nkibz,nomega4sd,nsppol*nsig_ab))
    ! Diagonal matrix elements of \Sigma_c around the zeroth order eigenvalue (usually KS).

    complex(dpc),pointer :: sigxcme(:,:,:,:)
    ! sigxme(b1gw:b2gw,nkibz,nomega_r,nsppol*nsig_ab))
    ! $\<nks|\Sigma_{xc}(E)|nks\>$ at each real frequency frequency.

    complex(dpc),pointer :: sigxcmesi(:,:,:,:)
    ! sigxcmesi(b1gw:b2gw,nkibz,nomega_i,nsppol*nsig_ab))
    ! Matrix elements of $\Sigma_{xc}$ along the imaginary axis.
    ! Only used in case of analytical continuation.

    complex(dpc),pointer :: sigxcme4sd(:,:,:,:)
    ! sigxcme4sd(b1gw:b2gw,nkibz,nomega4sd,nsppol*nsig_ab))
    ! Diagonal matrix elements of \Sigma_xc for frequencies around the zeroth order eigenvalues.

    complex(dpc),pointer :: ze0(:,:,:)
    ! ze0(b1gw:b2gw,nkibz,nsppol))
    ! renormalization factor. $(1-\dfrac{\partial\Sigma_c} {\partial E_{KS}})^{-1}$

    complex(dpc),pointer :: omega_i(:)
    ! omegasi(nomega_i)
    ! Frequencies along the imaginary axis used for the analytical continuation.

    complex(dpc),pointer :: omega4sd(:,:,:,:)
    ! omega4sd(b1gw:b2gw,nkibz,nomega4sd,nsppol).
    ! Frequencies used to evaluate the Derivative of Sigma.
    """
    def __init__(self, path):
        self.ks_bands = ElectronBands.from_file(path)
        self.nsppol = self.ks_bands.nsppol

        super(SigresReader, self).__init__(path)

        try:
            self.nomega_r = self.read_dimvalue("nomega_r")
        except self.Error:
            self.nomega_r = 0

        #self.nomega_i = self.read_dim("nomega_i")

        # Save important quantities needed to simplify the API.
        self.structure = self.read_structure()

        self.gwcalctyp = self.read_value("gwcalctyp")
        self.usepawu = self.read_value("usepawu")

        # 1) The K-points of the homogeneous mesh.
        self.ibz = self.ks_bands.kpoints

        # 2) The K-points where QPState corrections have been calculated.
        gwred_coords = self.read_redc_gwkpoints()
        self.gwkpoints = KpointList(self.structure.reciprocal_lattice, gwred_coords)

        # minbnd[nkptgw,nsppol] gives the minimum band index computed
        # Note conversion between Fortran and python convention.
        self.gwbstart_sk = self.read_value("minbnd") - 1
        self.gwbstop_sk = self.read_value("maxbnd")

        # min and Max band index for GW corrections.
        self.min_gwbstart = np.min(self.gwbstart_sk)
        self.max_gwbstart = np.max(self.gwbstart_sk)

        self.min_gwbstop = np.min(self.gwbstop_sk)
        self.max_gwbstop = np.max(self.gwbstop_sk)

        self._egw = self.read_value("egw", cmode="c")

        # Read and save important matrix elements.
        # All these arrays are dimensioned
        # vxcme(b1gw:b2gw,nkibz,nsppol*nsig_ab))
        self._vxcme = self.read_value("vxcme")
        self._sigxme = self.read_value("sigxme")
        self._hhartree = self.read_value("hhartree", cmode="c")
        self._vUme = self.read_value("vUme")
        #if self.usepawu == 0: self._vUme.fill(0.0)

        # Complex arrays
        self._sigcmee0 = self.read_value("sigcmee0", cmode="c")
        self._ze0 = self.read_value("ze0", cmode="c")

        # Frequencies for the spectral function.
        if self.has_spfunc:
            self._omega_r = self.read_value("omega_r")
            self._sigcme = self.read_value("sigcme", cmode="c")
            self._sigxcme = self.read_value("sigxcme", cmode="c")

        # Self-consistent case
        self._en_qp_diago = self.read_value("en_qp_diago")

        # <KS|QPState>
        self._eigvec_qp = self.read_value("eigvec_qp", cmode="c")

        #self._mlda_to_qp

    #def is_selfconsistent(self, mode):
    #    return self.gwcalctyp

    @property
    def has_spfunc(self):
        """True if self contains the spectral function."""
        return self.nomega_r

    def kpt2fileindex(self, kpoint):
        """
        Helper function that returns the index of kpoint in the netcdf file.
        Accepts `Kpoint` instance or integer

        Raise:
            `KpointsError` if kpoint cannot be found.

        .. note::

            This function is needed since arrays in the netcdf file are dimensioned
            with the total number of k-points in the IBZ.
        """
        if duck.is_intlike(kpoint): return int(kpoint)
        return self.ibz.index(kpoint)

    def gwkpt2seqindex(self, gwkpoint):
        """
        This function returns the index of the GW k-point in (0:nkptgw)
        Used to access data in the arrays that are dimensioned [0:nkptgw] e.g. minbnd.
        """
        if duck.is_intlike(gwkpoint):
            return int(gwkpoint)
        else:
            return self.gwkpoints.index(gwkpoint)

    def read_redc_gwkpoints(self):
        return self.read_value("kptgw")

    def read_allqps(self):
        qps_spin = self.nsppol * [None]

        for spin in range(self.nsppol):
            qps = []
            for gwkpoint in self.gwkpoints:
                ik = self.gwkpt2seqindex(gwkpoint)
                for band in range(self.gwbstart_sk[spin,ik], self.gwbstop_sk[spin,ik]):
                    qps.append(self.read_qp(spin, gwkpoint, band))

            qps_spin[spin] = QPList(qps)

        return tuple(qps_spin)

    def read_qplist_sk(self, spin, kpoint):
        ik = self.gwkpt2seqindex(kpoint)
        bstart, bstop = self.gwbstart_sk[spin, ik], self.gwbstop_sk[spin, ik]

        return QPList([self.read_qp(spin, kpoint, band) for band in range(bstart, bstop)])

    #def read_qpene(self, spin, kpoint, band)

    def read_qpenes(self):
        return self._egw[:, :, :]

    def read_qp(self, spin, kpoint, band):
        ik_file = self.kpt2fileindex(kpoint)
        ib_file = band - self.gwbstart_sk[spin, self.gwkpt2seqindex(kpoint)]

        return QPState(
            spin=spin,
            kpoint=kpoint,
            band=band,
            e0=self.read_e0(spin, ik_file, band),
            qpe=self._egw[spin, ik_file, band],
            qpe_diago=self._en_qp_diago[spin, ik_file, band],
            vxcme=self._vxcme[spin, ik_file, ib_file],
            sigxme=self._sigxme[spin, ik_file, ib_file],
            sigcmee0=self._sigcmee0[spin, ik_file, ib_file],
            vUme=self._vUme[spin, ik_file, ib_file],
            ze0=self._ze0[spin, ik_file, ib_file],
        )

    def read_qpgaps(self):
        """Read the QP gaps. Returns [nsppol, nkibz] array with QP gaps in eV"""
        return self.read_value("egwgap")

    def read_e0(self, spin, kfile, band):
        return self.ks_bands.eigens[spin, kfile, band]

    def read_sigmaw(self, spin, kpoint, band):
        """Returns the real and the imaginary part of the self energy."""
        if not self.has_spfunc:
            raise ValueError("%s does not contain spectral function data" % self.path)

        ik = self.kpt2fileindex(kpoint)
        return self._omega_r, self._sigxcme[spin,:,ik,band]

    def read_spfunc(self, spin, kpoint, band):
        """
        Returns the spectral function.

         one/pi * ABS(AIMAG(Sr%sigcme(ib,ikibz,io,is))) /
         ( (REAL(Sr%omega_r(io)-Sr%hhartree(ib,ib,ikibz,is)-Sr%sigxcme(ib,ikibz,io,is)))**2 &
        +(AIMAG(Sr%sigcme(ib,ikibz,io,is)))**2) / Ha_eV,&
        """
        if not self.has_spfunc:
            raise ValueError("%s does not contain spectral function data" % self.path)

        ik = self.kpt2fileindex(kpoint)
        ib = band - self.gwbstart_sk[spin, self.gwkpt2seqindex(kpoint)]

        aim_sigc = np.abs(self._sigcme[spin,:,ik,ib].imag)

        den = np.zeros(self.nomega_r)
        for io, omega in enumerate(self._omega_r):
            den[io] = (omega - self._hhartree[spin,ik,ib,ib].real - self._sigxcme[spin,io,ik,ib].real) ** 2 + \
                self._sigcme[spin,io,ik,ib].imag ** 2

        return self._omega_r, 1./np.pi * (aim_sigc/den)

    def read_eigvec_qp(self, spin, kpoint, band=None):
        """
        Returns <KS|QPState> for the given spin, kpoint and band. If band is None, <KS_b|QP_{b'}> is returned.
        """
        ik = self.kpt2fileindex(kpoint)
        if band is not None:
            return self._eigvec_qp[spin,ik,:,band]
        else:
            return self._eigvec_qp[spin,ik,:,:]

    def read_params(self):
        """
        Read the parameters of the calculation.
        Returns :class:`AttrDict` instance with the value of the parameters.
        """
        param_names = [
            "ecutwfn", "ecuteps", "ecutsigx", "scr_nband", "sigma_nband",
            "gwcalctyp", "scissor_ene",
        ]

        params = AttrDict()
        for pname in param_names:
            params[pname] = self.read_value(pname, default=None)

        # Other quantities that might be subject to convergence studies.
        params["nkibz"] = len(self.ibz)

        return params

    def print_qps(self, spin=None, kpoints=None, bands=None, fmt=None, stream=sys.stdout):
        """
        Args:
            spin: Spin index, if None all spins are considered
            kpoints: List of k-points to select. Default: all kpoints
            bands: List of bands to select. Default is all bands
            fmt: Format string passe to `to_strdict`
            stream: file-like object.

        Returns
            List of tables.
        """
        spins = range(self.nsppol) if spin is None else [spin]
        kpoints = self.gwkpoints if kpoints is None else [kpoints]
        if bands is not None: bands = [bands]

        header = QPState.get_fields(exclude=["spin", "kpoint"])
        tables = []

        for spin in spins:
            for kpoint in kpoints:
                table_sk = PrettyTable(header)
                if bands is None:
                    ik = self.gwkpt2seqindex(kpoint)
                    bands = range(self.gwbstart_sk[spin,ik], self.gwbstop_sk[spin,ik])

                for band in bands:
                    qp = self.read_qp(spin, kpoint, band)
                    d = qp.to_strdict(fmt=fmt)
                    table_sk.add_row([d[k] for k in header])

                stream.write("\nkpoint: %s, spin: %s, energy units: eV (NB: bands start from zero)\n" % (kpoint, spin))
                print(table_sk, file=stream)
                stream.write("\n")

                # Add it to tables.
                tables.append(table_sk)

        return tables

    #def read_mel(self, mel_name, spin, kpoint, band, band2=None):
    #    array = self.read_value(mel_name)

    #def read_mlda_to_qp(self, spin, kpoint, band=None):
    #    """Returns the unitary transformation KS --> QPS"""
    #    ik = self.kpt2fileindex(kpoint)
    #    if band is not None:
    #        return self._mlda_to_qp[spin,ik,:,band]
    #    else:
    #        return self._mlda_to_qp[spin,ik,:,:]

    #def read_qprhor(self):
    #    """Returns the QPState density in real space."""
