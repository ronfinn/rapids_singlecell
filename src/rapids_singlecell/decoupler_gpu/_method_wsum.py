from __future__ import annotations

import cupy as cp
import numpy as np
import pandas as pd
from anndata import AnnData
from cupyx.scipy.sparse import csr_matrix as cp_csr_matrix
from scipy.sparse import csr_matrix
from tqdm.auto import tqdm

from rapids_singlecell.preprocessing._utils import _sparse_to_dense

from ._pre import extract, filt_min_n, get_net_mat, match, rename_net


def run_perm(mat, net, idxs, times, seed):
    estimate = mat.dot(net)
    cp.random.seed(seed)
    # Init null distribution
    pvals = cp.zeros((mat.shape[0], net.shape[1]), dtype=np.float32)
    abs_estimate = cp.abs(estimate)
    sum_permuted = cp.zeros((mat.shape[0], net.shape[1]), dtype=cp.float32)
    sum_squares_permuted = cp.zeros((mat.shape[0], net.shape[1]), dtype=cp.float32)
    # Permute
    for i in range(times):
        cp.random.shuffle(idxs)
        permuted = mat.dot(net[idxs])
        pvals += cp.abs(permuted) > abs_estimate
        sum_permuted += permuted
        sum_squares_permuted += permuted**2

    # Compute empirical p-value
    pvals = cp.where(pvals == 0.0, 1.0, pvals).astype(np.float32)
    pvals = cp.where(pvals == times, times - 1, pvals).astype(np.float32)
    pvals = pvals / times
    pvals = cp.where(pvals >= 0.5, 1 - (pvals), pvals)
    pvals = pvals * 2

    # Compute z-score
    mean_permuted = sum_permuted / times
    variance_permuted = (sum_squares_permuted / times) - (mean_permuted**2) * times / (
        times - 1
    )
    std_permuted = cp.sqrt(variance_permuted)
    norm = (estimate - mean_permuted) / std_permuted
    # Compute corr score
    corr = (estimate * -cp.log10(pvals)).astype(np.float32)

    estimate_return = estimate.get()
    norm_return = norm.get()
    corr_return = corr.get()
    pvals_return = pvals.get()
    return estimate_return, norm_return, corr_return, pvals_return


def wsum(mat, net, times, batch_size, seed, verbose):
    # Get dims
    n_samples = mat.shape[0]
    n_features, n_fsets = net.shape

    # Init empty acts
    estimate = np.zeros((n_samples, n_fsets), dtype=np.float32)
    if times > 1:
        norm = np.zeros((n_samples, n_fsets), dtype=np.float32)
        corr = np.zeros((n_samples, n_fsets), dtype=np.float32)
        pvals = np.zeros((n_samples, n_fsets), dtype=np.float32)
        idxs = cp.arange(n_features, dtype=np.int64)
    else:
        norm, corr, pvals = None, None, None
    net = cp.array(net)
    if isinstance(mat, csr_matrix) or isinstance(mat, cp_csr_matrix):
        n_batches = int(np.ceil(n_samples / batch_size))
        for i in tqdm(range(n_batches), disable=not verbose):
            # Subset batch
            srt, end = i * batch_size, i * batch_size + batch_size
            if isinstance(mat, csr_matrix):
                tmp = cp.array(mat[srt:end].toarray())
            else:
                tmp = _sparse_to_dense(mat[srt:end])
            # Run WSUM
            if times > 1:
                (
                    estimate[srt:end],
                    norm[srt:end],
                    corr[srt:end],
                    pvals[srt:end],
                ) = run_perm(tmp, net, idxs, times, seed)
            else:
                estimate[srt:end] = tmp.dot(net)
    else:
        estimate = mat.dot(net)
        if times > 1:
            estimate, norm, corr, pvals = run_perm(
                cp.ascontiguousarray(mat), net, idxs, times, seed
            )
        else:
            estimate = cp.array(mat).dot(net)

    return estimate, norm, corr, pvals


def run_wsum(
    mat: AnnData | pd.DataFrame | list,
    net: pd.DataFrame,
    *,
    source="source",
    target="target",
    weight="weight",
    times=1000,
    batch_size: int = 10000,
    min_n: int = 5,
    seed: int = 42,
    verbose: bool = False,
    use_raw: bool | None = None,
    layer: str | None = None,
    pre_load: bool | None = False,
) -> tuple | None:
    """
    Weighted sum (WSUM).
    WSUM infers regulator activities by first multiplying each target feature by its associated weight which then are summed
    to an enrichment score (`wsum_estimate`). Furthermore, permutations of random target features can be performed to obtain a
    null distribution that can be used to compute a z-score (`wsum_norm`), or a corrected estimate (`wsum_corr`) by multiplying
    `wsum_estimate` by the minus log10 of the obtained empirical p-value.

    Parameters
    ----------
        mat
            List of [features, matrix], dataframe (samples x features) or an AnnData instance.
        net
            Network in long format.
        source
            Column name in net with source nodes.
        target
            Column name in net with target nodes.
        weight
            Column name in net with weights.
        times
            How many random permutations to do.
        batch_size
            Size of the batches to use. Increasing this will consume more memory but it will run faster.
        min_n
            Minimum of targets per source. If less, sources are removed.
        seed
            Random seed to use.
        verbose
            Whether to show progress.
        use_raw
            Use raw attribute of mat.
        layer
            Layer to use in AnnData object.
        pre_load
            Whether to pre-load the data into memory. This can be faster for small datasets.

    Returns
    -------
        Updates `adata` with the following fields.

            **estimate** : DataFrame
                WSUM scores. Stored in `.obsm['wsum_estimate']` if `mat` is AnnData.
            **norm**: DataFrame
                Normalized WSUM scores. Stored in `.obsm['wsum_norm']` if `mat` is AnnData.
            **corr** : DataFrame
                Corrected WSUM scores. Stored in `.obsm['wsum_corr']` if `mat` is AnnData.
            **pvals** : DataFrame
                Obtained p-values. Stored in `.obsm['wsum_pvals']` if `mat` is AnnData.
    """
    # Extract sparse matrix and array of genes
    m, r, c = extract(
        mat, use_raw=use_raw, layer=layer, verbose=verbose, pre_load=pre_load
    )
    # Transform net
    net = rename_net(net, source=source, target=target, weight=weight)
    net = filt_min_n(c, net, min_n=min_n)
    sources, targets, net = get_net_mat(net)

    # Match arrays
    net = match(c, targets, net)

    if verbose:
        print(
            f"Running wsum on mat with {m.shape[0]} samples and {len(c)} targets for {net.shape[1]} sources."
        )

    # Run WSUM
    estimate, norm, corr, pvals = wsum(m, net, times, batch_size, seed, verbose)

    # Transform to df
    estimate = pd.DataFrame(estimate, index=r, columns=sources)
    estimate.name = "wsum_estimate"
    if pvals is not None:
        norm = pd.DataFrame(norm, index=r, columns=sources)
        norm.name = "wsum_norm"
        corr = pd.DataFrame(corr, index=r, columns=sources)
        corr.name = "wsum_corr"
        pvals = pd.DataFrame(pvals, index=r, columns=sources)
        pvals.name = "wsum_pvals"

    # AnnData support
    if isinstance(mat, AnnData):
        # Update obsm AnnData object
        mat.obsm[estimate.name] = estimate
        if pvals is not None:
            mat.obsm[norm.name] = norm
            mat.obsm[corr.name] = corr
            mat.obsm[pvals.name] = pvals
    else:
        if pvals is not None:
            return estimate, norm, corr, pvals
        else:
            return estimate
