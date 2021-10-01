"""Tasks for simulating sidereal and time stream data.

A typical pattern would be to turn a map into a
:class:`containers.SiderealStream` with the :class:`SimulateSidereal` task, then
expand any redundant products with :class:`ExpandProducts` and finally generate
a set of time stream files with :class:`MakeTimeStream`.
"""

import numpy as np
from scipy.interpolate import interp1d
import scipy.fftpack as fftpack

from cora.util import hputil, units
from cora.util.cosmology import Cosmology
from caput import mpiutil, pipeline, config, mpiarray

from ..core import containers, task, io


class SimulateSidereal(task.SingleTask):
    """Create a simulated sidereal dataset from an input map.

    Attributes
    ----------
    stacked : bool
        When set, in the case that the beam transfer matrices are not full triangle
        treat them as having been generated by collating the baselines of full triangle
        data, and set appropriate `index_map/stack` and `reverse_map/stack` entries. If
        not set, treat the entries as having been generated by down selecting the full
        set of baselines, and thus don't create the `stack` entries. Default is `True`.
    """

    stacked = config.Property(proptype=bool, default=True)

    def setup(self, bt):
        """Setup the simulation.

        Parameters
        ----------
        bt : ProductManager or BeamTransfer
            Beam Transfer maanger.
        """
        self.beamtransfer = io.get_beamtransfer(bt)
        self.telescope = io.get_telescope(bt)

    def process(self, map_):
        """Simulate a SiderealStream

        Parameters
        ----------
        map : :class:`containers.Map`
            The sky map to process to into a sidereal stream. Frequencies in the
            map, must match the Beam Transfer matrices.

        Returns
        -------
        ss : SiderealStream
            Stacked sidereal day.
        feeds : list of CorrInput
            Description of the feeds simulated.
        """

        # Read in telescope system
        bt = self.beamtransfer
        tel = self.telescope

        lmax = tel.lmax
        mmax = tel.mmax
        nfreq = tel.nfreq
        npol = tel.num_pol_sky

        lfreq, sfreq, efreq = mpiutil.split_local(nfreq)

        lm, sm, em = mpiutil.split_local(mmax + 1)

        # Set the minimum resolution required for the sky.
        ntime = 2 * mmax + 1

        freqmap = map_.index_map["freq"][:]
        row_map = map_.map[:]

        # if (tel.frequencies != freqmap["centre"]).any():
        #     raise ValueError("Frequencies in map do not match those in Beam Transfers.")

        # Calculate the alm's for the local sections
        row_alm = hputil.sphtrans_sky(row_map, lmax=lmax).reshape(
            (lfreq, npol * (lmax + 1), lmax + 1)
        )

        # Trim off excess m's and wrap into MPIArray
        row_alm = row_alm[..., : (mmax + 1)]
        row_alm = mpiarray.MPIArray.wrap(row_alm, axis=0)

        # Perform the transposition to distribute different m's across processes. Neat
        # tip, putting a shorter value for the number of columns, trims the array at
        # the same time
        col_alm = row_alm.redistribute(axis=2)

        # Transpose and reshape to shift m index first.
        col_alm = col_alm.transpose((2, 0, 1)).reshape((None, nfreq, npol, lmax + 1))

        # Create storage for visibility data
        vis_data = mpiarray.MPIArray(
            (mmax + 1, nfreq, bt.ntel), axis=0, dtype=np.complex128
        )
        vis_data[:] = 0.0

        # Iterate over m's local to this process and generate the corresponding
        # visibilities
        for mp, mi in vis_data.enumerate(axis=0):
            vis_data[mp] = bt.project_vector_sky_to_telescope(
                mi, col_alm[mp].view(np.ndarray)
            )

        # Rearrange axes such that frequency is last (as we want to divide
        # frequencies across processors)
        row_vis = vis_data.transpose((0, 2, 1))

        # Parallel transpose to get all m's back onto the same processor
        col_vis_tmp = row_vis.redistribute(axis=2)
        col_vis_tmp = col_vis_tmp.reshape((mmax + 1, 2, tel.npairs, None))

        # Transpose the local section to make the m's the last axis and unwrap the
        # positive and negative m at the same time.
        col_vis = mpiarray.MPIArray(
            (tel.npairs, nfreq, ntime), axis=1, dtype=np.complex128
        )
        col_vis[:] = 0.0
        col_vis[..., 0] = col_vis_tmp[0, 0]
        for mi in range(1, mmax + 1):
            col_vis[..., mi] = col_vis_tmp[mi, 0]
            col_vis[..., -mi] = col_vis_tmp[
                mi, 1
            ].conj()  # Conjugate only (not (-1)**m - see paper)

        del col_vis_tmp

        # Fourier transform m-modes back to get final timestream.
        vis_stream = np.fft.ifft(col_vis, axis=-1) * ntime
        vis_stream = vis_stream.reshape((tel.npairs, lfreq, ntime))
        vis_stream = vis_stream.transpose((1, 0, 2)).copy()

        # Try and fetch out the feed index and info from the telescope object.
        try:
            feed_index = tel.input_index
        except AttributeError:
            feed_index = tel.nfeed

        kwargs = {}

        if tel.npairs != (tel.nfeed + 1) * tel.nfeed // 2 and self.stacked:
            # If we should treat this as stacked, then pull the information straight
            # from the telescope class
            kwargs["prod"] = tel.index_map_prod
            kwargs["stack"] = tel.index_map_stack
            kwargs["reverse_map_stack"] = tel.reverse_map_stack
        else:
            # Construct a product map as if this was a down selection
            prod_map = np.zeros(
                tel.uniquepairs.shape[0], dtype=[("input_a", int), ("input_b", int)]
            )
            prod_map["input_a"] = tel.uniquepairs[:, 0]
            prod_map["input_b"] = tel.uniquepairs[:, 1]

            kwargs["prod"] = prod_map

        # Construct container and set visibility data
        sstream = containers.SiderealStream(
            freq=freqmap,
            ra=ntime,
            input=feed_index,
            distributed=True,
            comm=map_.comm,
            **kwargs,
        )
        sstream.vis[:] = mpiarray.MPIArray.wrap(vis_stream, axis=0)
        sstream.weight[:] = 1.0

        return sstream


class ExpandProducts(task.SingleTask):
    """Un-wrap collated products to full triangle."""

    def setup(self, telescope):
        """Get a reference to the telescope class.

        Parameters
        ----------
        tel : :class:`drift.core.TransitTelescope`
            Telescope object.
        """
        self.telescope = io.get_telescope(telescope)

    def process(self, sstream):
        """Transform a sidereal stream to having a full product matrix.

        Parameters
        ----------
        sstream : :class:`containers.SiderealStream`
            Sidereal stream to unwrap.

        Returns
        -------
        new_sstream : :class:`containers.SiderealStream`
            Unwrapped sidereal stream.
        """

        sstream.redistribute("freq")

        ninput = len(sstream.input)
        prod = np.array(
            [(fi, fj) for fi in range(ninput) for fj in range(fi, ninput)],
            dtype=[("input_a", int), ("input_b", int)],
        )
        nprod = len(prod)

        new_stream = containers.SiderealStream(prod=prod, stack=None, axes_from=sstream)
        new_stream.redistribute("freq")
        new_stream.vis[:] = 0.0
        new_stream.weight[:] = 0.0

        # Create dummpy index and reverse map for the stack axis to match the behaviour
        # of reading in an N^2 file through andata
        fwd_stack = np.empty(nprod, dtype=[("prod", "<u4"), ("conjugate", "u1")])
        fwd_stack["prod"] = np.arange(nprod)
        fwd_stack["conjugate"] = False
        new_stream.create_index_map("stack", fwd_stack)
        rev_stack = np.empty(nprod, dtype=[("stack", "<u4"), ("conjugate", "u1")])
        rev_stack["stack"] = np.arange(nprod)
        rev_stack["conjugate"] = False
        new_stream.create_reverse_map("stack", rev_stack)

        # Iterate over all feed pairs and work out which is the correct index in the sidereal stack.
        for pi, (fi, fj) in enumerate(prod):
            unique_ind = self.telescope.feedmap[fi, fj]
            conj = self.telescope.feedconj[fi, fj]

            # unique_ind is less than zero it has masked out
            if unique_ind < 0:
                continue

            prod_stream = sstream.vis[:, unique_ind]
            new_stream.vis[:, pi] = prod_stream.conj() if conj else prod_stream

            new_stream.weight[:, pi] = 1.0

        return new_stream


class MakeTimeStream(task.SingleTask):
    """Generate a series of time streams files from a sidereal stream.

    Parameters
    ----------
    start_time, end_time : float or datetime
        Start and end times of the timestream to simulate. Needs to be either a
        `float` (UNIX time) or a `datetime` objects in UTC.
    integration_time : float, optional
        Integration time in seconds. Takes precedence over `integration_frame_exp`.
    integration_frame_exp: int, optional
        Specify the integration time in frames. The integration time is
        `2**integration_frame_exp * 2.56 us`.
    samples_per_file : int, optional
        Number of samples per file.
    """

    start_time = config.utc_time()
    end_time = config.utc_time()

    integration_time = config.Property(proptype=float, default=None)
    integration_frame_exp = config.Property(proptype=int, default=23)

    samples_per_file = config.Property(proptype=int, default=1024)

    _cur_time = 0.0  # Hold the current file start time

    def setup(self, sstream, manager):
        """Get the sidereal stream to turn into files.

        Parameters
        ----------
        sstream : SiderealStream
            The sidereal data to use.
        manager : ProductManager or BeamTransfer
            Beam Transfer and telescope manager
        """
        self.sstream = sstream
        # Need an Observer object holding the geographic location of the telescope.
        self.observer = io.get_telescope(manager)
        # Initialise the current start time
        self._cur_time = self.start_time

    def process(self):
        """Create a timestream file.

        Returns
        -------
        tstream : :class:`containers.TimeStream`
            Time stream object.
        """

        from ..util import regrid

        # First check to see if we have reached the end of the requested time,
        # and if so stop the iteration.
        if self._cur_time >= self.end_time:
            raise pipeline.PipelineStopIteration

        time = self._next_time_axis()

        # Make the timestream container
        tstream = containers.empty_timestream(axes_from=self.sstream, time=time)

        # Make the interpolation array
        ra = self.observer.unix_to_lsa(tstream.time)
        lza = regrid.lanczos_forward_matrix(self.sstream.ra, ra, periodic=True)
        lza = lza.T.astype(np.complex64)

        # Apply the interpolation matrix to construct the new timestream, place
        # the output directly into the container
        np.dot(self.sstream.vis[:], lza, out=tstream.vis[:])

        # Set the weights array to the maximum value for CHIME
        tstream.weight[:] = 1.0

        # Output the timestream
        return tstream

    def _next_time_axis(self):

        # Calculate the integration time
        if self.integration_time is not None:
            int_time = self.integration_time
        else:
            int_time = 2.56e-6 * 2 ** self.integration_frame_exp

        # Calculate number of samples in file and timestamps
        nsamp = min(
            int(np.ceil((self.end_time - self._cur_time) / int_time)),
            self.samples_per_file,
        )
        timestamps = (
            self._cur_time + (np.arange(nsamp) + 1) * int_time
        )  # +1 as timestamps are at the end of each sample

        # Construct the time axis index map
        if self.integration_time is not None:
            time = timestamps
        else:
            _time_dtype = [("fpga_count", np.uint64), ("ctime", np.float64)]
            time = np.zeros(nsamp, _time_dtype)
            time["ctime"] = timestamps
            time["fpga_count"] = (
                (timestamps - self.start_time)
                / int_time
                * 2 ** self.integration_frame_exp
            ).astype(np.uint64)

        # Increment the current start time for the next iteration
        self._cur_time += nsamp * int_time

        return time


class MakeSiderealDayStream(task.SingleTask):
    """Task for simulating a set of sidereal days from a given stream.

    This creates a copy of the base stream for every LSD within the provided time
    range.

    Attributes
    ----------
    start_time, end_time : float or datetime
        Start and end times of the sidereal streams to simulate. Needs to be either a
        `float` (UNIX time) or a `datetime` objects in UTC.
    """

    start_time = config.utc_time()
    end_time = config.utc_time()

    def setup(self, bt, sstream):
        """Set up an observer and the data to use for this simulation.

        Parameters
        ----------
        bt : beamtransfer.BeamTransfer or manager.ProductManager
            Sets up an observer holding the geographic location of the telscope.
        sstream : containers.SiderealStream
            The base sidereal data to use for this simulation.
        """
        self.observer = io.get_telescope(bt)
        self.lsd_start = self.observer.unix_to_lsd(self.start_time)
        self.lsd_end = self.observer.unix_to_lsd(self.end_time)

        self.log.info(
            "Sidereal period requested: LSD=%i to LSD=%i",
            int(self.lsd_start),
            int(self.lsd_end),
        )

        # Initialize the current lsd time
        self._current_lsd = None
        self.sstream = sstream

    def process(self):
        """Generate a sidereal stream for the specific sidereal day.

        Returns
        -------
        ss : :class:`containers.SiderealStream`
            Simulated sidereal day stream.
        """

        # If current_lsd is None then this is the first time we've run
        if self._current_lsd is None:
            # Check if lsd is an integer, if not add an lsd
            if isinstance(self.lsd_start, int):
                self._current_lsd = int(self.lsd_start)
            else:
                self._current_lsd = int(self.lsd_start + 1)

        # Check if we have reached the end of the requested time
        if self._current_lsd >= self.lsd_end:
            raise pipeline.PipelineStopIteration

        ss = self.sstream.copy()
        ss.attrs["tag"] = f"lsd_{self._current_lsd}"
        ss.attrs["lsd"] = self._current_lsd

        self._current_lsd += 1

        return ss


class SimulateSingleHarmonicSidereal(task.SingleTask):
    """Create a simulated sidereal dataset from a single
    nonzero spherical harmonic coefficient."""

    done = False

    ell = config.Property(proptype=int, default=None)
    m = config.Property(proptype=int, default=None)

    kperp = config.Property(proptype=float, default=None)

    kpar = config.Property(proptype=float, default=None)
    kpar_as_kf_mult = config.Property(proptype=bool, default=True)

    def setup(self, bt):
        """Set up the simulation.

        Parameters
        ----------
        bt : ProductManager or BeamTransfer
            Beam Transfer maanger.
        """
        self.beamtransfer = io.get_beamtransfer(bt)
        self.telescope = io.get_telescope(bt)

        if self.ell is None and self.kperp is None:
            raise config.CaputConfigError("Must specify either ell or kperp!")

        if self.kperp is not None:
            c = Cosmology()
            self.ell_arr = np.rint(
                self.kperp
                * c.comoving_distance(units.nu21/self.telescope.frequencies - 1)
            ).astype(int)
        else:
            self.ell_arr = (
                np.ones_like(self.telescope.frequencies, dtype=int)
                * self.ell
            )

        self.log.info("Ells being simulated: %s" % self.ell_arr)

        if self.ell_arr.max() > self.telescope.mmax:
            raise RuntimeError("User has requested ell values greater than telescope's m_max!")

    def process(self):
        """Simulate a SiderealStream.

        Returns
        -------
        ss : SiderealStream
            Stacked sidereal day.
        feeds : list of CorrInput
            Description of the feeds simulated.
        """

        if self.done:
            raise pipeline.PipelineStopIteration

        # Read in telescope system
        bt = self.beamtransfer
        tel = self.telescope

        lmax = tel.lmax
        mmax = tel.mmax
        nfreq = tel.nfreq
        npol = tel.num_pol_sky

        lfreq, sfreq, efreq = mpiutil.split_local(nfreq)

        lm, sm, em = mpiutil.split_local(mmax + 1)

        # Set the minimum resolution required for the sky.
        ntime = 2 * mmax + 1

        # Set the maximum m we actually need to compute for
        if self.m is not None:
            mmax_compute = self.m
        else:
            mmax_compute = self.ell_arr.max()

        # Construct frequency index map, assuming equal-width channels
        freqmap = np.zeros(len(tel.frequencies), dtype=[("centre", np.float64), ("width", np.float64)])
        freqmap["centre"][:] = tel.frequencies
        freqmap["width"][:] = np.abs(np.diff(tel.frequencies)[0])

        # Get local section of ell array
        local_ell_arr = self.ell_arr[sfreq : efreq]

        if self.kpar is None:
            # If not input kpar specified, use all ones as input values
            vals = np.ones(lfreq)
        else:
            # If input kpar specified, set map values along frequency
            # axis based on a single Fourier mode with that kpar
            vals, kpar_value = channel_values_from_kpar(
                freqmap, self.kpar, self.kpar_as_kf_mult
            )
            vals = vals[sfreq: efreq]

        # Calculate the alm's for the local sections.
        # Only set pol=0 a_lm to 1
        # If input m is not specified, set a_lm=1 for all m's
        row_alm = np.zeros((lfreq, npol, lmax + 1, lmax + 1), dtype=np.complex128)

        if self.m is not None:
            for li in range(lfreq):
                row_alm[li, 0, local_ell_arr[li], self.m] = vals[li]
        else:
            for li in range(lfreq):
                row_alm[
                    li, 0, local_ell_arr[li], : local_ell_arr[li] + 1
                ] = vals[li] * np.ones(local_ell_arr[li] + 1)
            self.log.debug("No input m found! Setting a_lm=1 for all m")

        row_alm = row_alm.reshape((lfreq, npol * (lmax + 1), lmax + 1))
        # row_alm = hputil.sphtrans_sky(row_map, lmax=lmax).reshape(
        #     (lfreq, npol * (lmax + 1), lmax + 1)
        # )

        # Trim off excess m's and wrap into MPIArray.
        # Until the final iFFT from m to sidereal time, we only need the m's we'll
        # actually compute for.
        row_alm = row_alm[..., : (mmax_compute + 1)]
        row_alm = mpiarray.MPIArray.wrap(row_alm, axis=0)

        # Perform the transposition to distribute different m's across processes. Neat
        # tip, putting a shorter value for the number of columns, trims the array at
        # the same time
        col_alm = row_alm.redistribute(axis=2)

        # Transpose and reshape to shift m index first.
        col_alm = col_alm.transpose((2, 0, 1)).reshape((None, nfreq, npol, lmax + 1))

        # Create storage for visibility data
        vis_data = mpiarray.MPIArray(
            (mmax_compute + 1, nfreq, bt.ntel), axis=0, dtype=np.complex128
        )
        vis_data[:] = 0.0

        # Iterate over m's local to this process and generate the corresponding
        # visibilities
        for mp, mi in vis_data.enumerate(axis=0):
            self.log.debug('Computing for m = %d (%d/%d locally)' % (mi, mp, vis_data.shape[0]))
            vis_data[mp] = bt.project_vector_sky_to_telescope(
                mi, col_alm[mp].view(np.ndarray)
            )

        # Rearrange axes such that frequency is last (as we want to divide
        # frequencies across processors)
        row_vis = vis_data.transpose((0, 2, 1))

        # Parallel transpose to get all m's back onto the same processor
        col_vis_tmp = row_vis.redistribute(axis=2)
        col_vis_tmp = col_vis_tmp.reshape((mmax_compute + 1, 2, tel.npairs, None))

        # Transpose the local section to make the m's the last axis and unwrap the
        # positive and negative m at the same time. ntime is set according to the
        # telescope's mmax, but we only fill up to mmax_compute+1, because the
        # other entries are zero
        col_vis = mpiarray.MPIArray(
            (tel.npairs, nfreq, ntime), axis=1, dtype=np.complex128
        )
        col_vis[:] = 0.0
        col_vis[..., 0] = col_vis_tmp[0, 0]
        for mi in range(1, mmax_compute + 1):
            col_vis[..., mi] = col_vis_tmp[mi, 0]
            col_vis[..., -mi] = col_vis_tmp[
                mi, 1
            ].conj()  # Conjugate only (not (-1)**m - see paper)

        del col_vis_tmp

        # Fourier transform m-modes back to get final timestream.
        vis_stream = np.fft.ifft(col_vis, axis=-1) * ntime
        vis_stream = vis_stream.reshape((tel.npairs, lfreq, ntime))
        vis_stream = vis_stream.transpose((1, 0, 2)).copy()

        # Try and fetch out the feed index and info from the telescope object.
        try:
            feed_index = tel.input_index
        except AttributeError:
            feed_index = tel.nfeed

        kwargs = {}

        if tel.npairs != (tel.nfeed + 1) * tel.nfeed // 2:
            # If we should treat this as stacked, then pull the information straight
            # from the telescope class
            kwargs["prod"] = tel.index_map_prod
            kwargs["stack"] = tel.index_map_stack
            kwargs["reverse_map_stack"] = tel.reverse_map_stack
        else:
            # Construct a product map as if this was a down selection
            prod_map = np.zeros(
                tel.uniquepairs.shape[0], dtype=[("input_a", int), ("input_b", int)]
            )
            prod_map["input_a"] = tel.uniquepairs[:, 0]
            prod_map["input_b"] = tel.uniquepairs[:, 1]

            kwargs["prod"] = prod_map

        # Construct container and set visibility data
        sstream = containers.SiderealStream(
            freq=freqmap,
            ra=ntime,
            input=feed_index,
            distributed=True,
            comm=self.comm,
            **kwargs,
        )
        sstream.vis[:] = mpiarray.MPIArray.wrap(vis_stream, axis=0)
        sstream.weight[:] = 1.0

        # Save ell array and kperp to attributes
        sstream.attrs["ell"] = self.ell_arr
        if self.kperp is not None:
            sstream.attrs["kperp"] = self.kperp

        self.done = True

        return sstream


def relative_freq_channel_distances(freqs):

    # Set up cora cosmology calculator
    cosmo = Cosmology()

    # Get frequency channel centers and corresponding comoving distances
    abs_chi = cosmo.comoving_distance(units.nu21 / freqs - 1.0)
    n_chi = len(abs_chi)

    # Define distances relative to lowest distance
    if abs_chi[0] < abs_chi[1]:
        rel_chi = abs_chi - abs_chi[0]
    else:
        rel_chi = (abs_chi - abs_chi[-1])[::-1]

    return rel_chi


def channel_values_from_kpar(freqmap, kpar_in, kpar_as_kf_mult=True, unit_amplitude=True):
    """For an input k_parallel value, sample the corresponding
    Fourier mode at the channel frequency centers, relative to
    the lowest channel.

    kpar_as_kf_mult=True means that the kpar_in is specified as a
    multiple of the fundamental k corresponding to the width
    of the entire band.
    """

    # Get comoving distances corresponding to frequency channel
    # centers, relative to lowest comoving distance from z=0
    try:
        freqs = freqmap["centre"]
    except:
        freqs = freqmap
    # freqs = np.array([x[0] for x in freqmap])
    rel_chi = relative_freq_channel_distances(freqs)

    # Get max comoving distance (i.e. distance spanned by band),
    # and lowest pairwise distance difference between two channel centers
    chi_max = rel_chi.max()
    chi_min = np.diff(rel_chi).min()

    # Define k_fundamental as 2pi/chi_max
    kf = 2*np.pi / chi_max

    # Set parameters for FFT along radial distance
    kparmax = 20.0
    nkpar = 32768
    kpar = np.linspace(0, kparmax, nkpar)
    mode_k = np.zeros_like(kpar)

    # Set input mode values to be unity at specified kpar
    # and zero elsewhere
    kpar_value = kpar_in * kf if kpar_as_kf_mult else kpar_in
    mode_k[np.searchsorted(kpar, kpar_value)] = 1

    # Do DCT, normalizing results so that iDCT gives unity at input kpar (if desired)
    mode_ft = fftpack.dct(mode_k, type=1)
    if not unit_amplitude:
        mode_ft /= (2 * nkpar)
    elif kpar_in != 0:
        mode_ft /= 2
    mode_ft_x = np.arange(nkpar) * np.pi / kparmax

    # Define interpolating function over results, and return results
    # sampled at channel centers
    mode_ft_interp = interp1d(mode_ft_x, mode_ft, kind='cubic')
    return mode_ft_interp(rel_chi), kpar_value
