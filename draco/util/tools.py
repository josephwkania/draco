"""Collection of miscellaneous routines.

Miscellaneous tasks should be placed in :module:`draco.core.misc`.
"""

import numpy as np


def cmap(i, j, n):
    """Given a pair of feed indices, return the pair index.

    Parameters
    ----------
    i, j : integer
        Feed index.
    n : integer
        Total number of feeds.

    Returns
    -------
    pi : integer
        Pair index.
    """
    if i <= j:
        return (n * (n + 1) / 2) - ((n - i) * (n - i + 1) / 2) + (j - i)
    else:
        return cmap(j, i, n)


def icmap(ix, n):
    """Inverse feed map.

    Parameters
    ----------
    ix : integer
        Pair index.
    n : integer
        Total number of feeds.

    Returns
    -------
    fi, fj : integer
        Feed indices.
    """
    for ii in range(n):
        if cmap(ii, n - 1, n) >= ix:
            break

    i = ii
    j = ix - cmap(i, i, n) + i
    return i, j


def apply_gain(vis, gain, axis=1, out=None, prod_map=None):
    """Apply per input gains to a set of visibilities packed in upper
    triangular format.

    This allows us to apply the gains while minimising the intermediate
    products created.

    Parameters
    ----------
    vis : np.ndarray[..., nprod, ...]
        Array of visibility products.
    gain : np.ndarray[..., ninput, ...]
        Array of gains. One gain per input.
    axis : integer, optional
        The axis along which the inputs (or visibilities) are
        contained. Currently only supports axis=1.
    out : np.ndarray
        Array to place output in. If :obj:`None` create a new
        array. This routine can safely use `out = vis`.
    prod_map : ndarray of integer pairs
        Gives the mapping from product axis to input pairs. If not supplied,
        :func:`icmap` is used.

    Returns
    -------
    out : np.ndarray
        Visibility array with gains applied. Same shape as :obj:`vis`.
    """

    nprod = vis.shape[axis]
    ninput = gain.shape[axis]

    if prod_map is None and nprod != (ninput * (ninput + 1) / 2):
        raise Exception("Number of inputs does not match the number of products.")

    if prod_map is not None:
        if len(prod_map) != nprod:
            msg = ("Length of *prod_map* does not match number of input"
                   " products.")
            raise ValueError(msg)
        # Could check prod_map contents as well, but the loop should give a
        # sensible error if this is wrong, and checking is expensive.
    else:
        prod_map = [icmap(pp, ninput) for pp in range(nprod)]

    if out is None:
        out = np.empty_like(vis)
    elif out.shape != vis.shape:
        raise Exception("Output array is wrong shape.")

    # Iterate over input pairs and set gains
    for pp in range(nprod):

        # Determine the inputs.
        ii, ij = prod_map[pp]

        # Fetch the gains
        gi = gain[:, ii]
        gj = gain[:, ij].conj()

        # Apply the gains and save into the output array.
        out[:, pp] = vis[:, pp] * gi * gj

    return out


def invert_no_zero(x):
    """Return the reciprocal, but ignoring zeros.

    Where `x != 0` return 1/x, or just return 0. Importantly this routine does
    not produce a warning about zero division.

    Parameters
    ----------
    x : np.ndarray

    Returns
    -------
    r : np.ndarray
        Return the reciprocal of x.
    """
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(x == 0, 0.0, 1.0 / x)


def extract_diagonal(utmat, axis=1):
    """Extract the diagonal elements of an upper triangular array.

    Parameters
    ----------
    utmat : np.ndarray[..., nprod, ...]
        Upper triangular array.
    axis : int, optional
        Axis of array that is upper triangular.

    Returns
    -------
    diag : np.ndarray[..., ninput, ...]
        Diagonal of the array.
    """

    # Estimate nside from the array shape
    nside = int((2 * utmat.shape[axis])**0.5)

    # Check that this nside is correct
    if utmat.shape[axis] != (nside * (nside + 1) / 2):
        msg = ('Array length (%i) of axis %i does not correspond upper triangle\
                of square matrix' % (utmat.shape[axis], axis))
        raise RuntimeError(msg)

    # Find indices of the diagonal
    diag_ind = [cmap(ii, ii, nside) for ii in range(nside)]

    # Construct slice objects representing the axes before and after the product axis
    slice0 = (np.s_[:],) * axis
    slice1 = (np.s_[:],) * (len(utmat.shape) - axis - 1)

    # Extract wanted elements with a giant slice
    sl = slice0 + (diag_ind,) + slice1
    diag_array = utmat[sl]

    return diag_array