# Standard library
from collections import OrderedDict
import copy

# Third-party
from astropy.time import Time
import numpy as np

# Package
from ..utils import quantity_to_hdf5, quantity_from_hdf5

__all__ = ['JokerSamples']


class JokerSamples(OrderedDict):

    def __init__(self, t0=None, **kwargs):
        """A dictionary-like object for storing posterior samples from
        The Joker, with some extra functionality.

        Parameters
        ----------
        t0 : `astropy.time.Time`, numeric (optional)
            The reference time for the orbital parameters.
        **kwargs
            These are the orbital element names.
        """

        self._valid_keys = ['P', 'M0', 'e', 'omega', 'jitter', 'K', 'v0']

        # reference time
        self.t0 = t0

        kw = kwargs.copy()

        self._n_samples = None
        for key, val in kw.items():
            self._validate_key(key)
            kw[key] = self._validate_val(val)

            if self._n_samples is None:
                self._n_samples = len(val)

        super(JokerSamples, self).__init__(**kw)

    def _validate_key(self, key):
        if key not in self._valid_keys:
            raise ValueError("Invalid key '{0}'.".format(key))

    def _validate_val(self, val):
        val = np.atleast_1d(val)
        if self._n_samples is not None and len(val) != self._n_samples:
            raise ValueError("Length of new samples must match those already "
                             "stored! ({0}, expected {1})"
                             .format(len(val), self._n_samples))

        return val

    def __getitem__(self, slc):
        if isinstance(slc, str):
            return super(JokerSamples, self).__getitem__(slc)

        else:
            new = copy.copy(self)
            new._n_samples = None # reset number of samples

            for k in self.keys():
                new[k] = self[k][slc]

            return new

    def __setitem__(self, key, val):
        self._validate_key(key)
        val = self._validate_val(val)

        if self._n_samples is None:
            self._n_samples = len(val)

        super(JokerSamples, self).__setitem__(key, val)

    @property
    def n_samples(self):
        if self._n_samples is None:
            raise ValueError("No samples stored!")
        return self._n_samples

    def __len__(self):
        return self.n_samples

    def __str__(self):
        return ("<JokerSamples in [{0}], {1} samples>"
                .format(','.join(self.keys(), len(self))))

    @classmethod
    def from_hdf5(cls, f, n=None, **kwargs):
        """
        Parameters
        ----------
        f : :class:`h5py.File`, :class:`h5py.Group`
        n : int (optional)
            The number of samples to load.
        **kwargs
            All other keyword arguments are passed to the class initializer.
        """

        if 't0_bmjd' in f.attrs:
            # Read the reference time:
            t0 = Time(f.attrs['t0_bmjd'], format='mjd', scale='tcb')
        else:
            t0 = None

        samples = cls(t0=t0, **kwargs)
        for key in f.keys():
            samples[key] = quantity_from_hdf5(f, key, n=n)

        return samples

    def to_hdf5(self, f):
        """
        Parameters
        ----------
        f : :class:`h5py.File`, :class:`h5py.Group`
        """

        for key in self.keys():
            quantity_to_hdf5(f, key, self[key])

        if self.t0 is not None:
            f.attrs['t0_bmjd'] = self.t0.tcb.mjd
