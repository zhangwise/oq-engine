#  -*- coding: utf-8 -*-
#  vim: tabstop=4 shiftwidth=4 softtabstop=4

#  Copyright (c) 2014, GEM Foundation

#  OpenQuake is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

#  OpenQuake is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.

#  You should have received a copy of the GNU Affero General Public License
#  along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

import collections
import itertools
import operator
import logging
import random
import os

import numpy

from openquake.hazardlib.calc import gmf, filters
from openquake.hazardlib.site import SiteCollection
from openquake.risklib import scientific, workflows

from openquake.commonlib.parallel import apply_reduce
from openquake.commonlib.readinput import get_sitecol_assets, \
    get_gsim, get_rupture, get_correl_model, get_imts
from openquake.commonlib.riskmodels import get_risk_model

############### facilities for the classical calculator ################

SourceRuptureSites = collections.namedtuple(
    'SourceRuptureSites',
    'source rupture sites')


def gen_ruptures(sources, site_coll, maximum_distance, monitor):
    """
    Yield (source, rupture, affected_sites) for each rupture
    generated by the given sources.

    :param sources: a sequence of sources
    :param site_coll: a SiteCollection instance
    :param maximum_distance: the maximum distance
    :param monitor: a Monitor object
    """
    filtsources_mon = monitor.copy('filtering sources')
    genruptures_mon = monitor.copy('generating ruptures')
    filtruptures_mon = monitor.copy('filtering ruptures')
    for src in sources:
        with filtsources_mon:
            s_sites = src.filter_sites_by_distance_to_source(
                maximum_distance, site_coll)
            if s_sites is None:
                continue

        with genruptures_mon:
            ruptures = list(src.iter_ruptures())
        if not ruptures:
            continue

        for rupture in ruptures:
            with filtruptures_mon:
                r_sites = filters.filter_sites_by_distance_to_rupture(
                    rupture, maximum_distance, s_sites)
                if r_sites is None:
                    continue
            yield SourceRuptureSites(src, rupture, r_sites)
    filtsources_mon.flush()
    genruptures_mon.flush()
    filtruptures_mon.flush()


def gen_ruptures_for_site(site, sources, maximum_distance, monitor):
    """
    Yield source, <ruptures close to site>

    :param site: a Site object
    :param sources: a sequence of sources
    :param monitor: a Monitor object
    """
    source_rupture_sites = gen_ruptures(
        sources, SiteCollection([site]), maximum_distance, monitor)
    for src, rows in itertools.groupby(
            source_rupture_sites, key=operator.attrgetter('source')):
        yield src, [row.rupture for row in rows]


############### facilities for the scenario calculators ################


def add_dicts(acc, dic):
    """
    Add two dictionaries containing summable objects. For instance:

    >>> a = dict(x=1, y=2)
    >>> b = dict(x=1, z=0)
    >>> sorted(add_dicts(a, b).iteritems())
    [('x', 2), ('y', 2), ('z', 0)]

    As a special case, None values are ignored in the sum:

    >>> add_dicts({'x': 1}, {'x': None})
    {'x': 1}
    """
    new = acc.copy()
    for k, v in dic.iteritems():
        if v is not None:
            new[k] = new.get(k, 0) + v
    return new


def calc_gmfs_fast(oqparam, sitecol):
    """
    Build all the ground motion fields for the whole site collection in
    a single step.
    """
    max_dist = oqparam.maximum_distance
    correl_model = get_correl_model(oqparam)
    seed = getattr(oqparam, 'random_seed', 42)
    imts = get_imts(oqparam)
    gsim = get_gsim(oqparam)
    trunc_level = getattr(oqparam, 'truncation_level', None)
    n_gmfs = getattr(oqparam, 'number_of_ground_motion_fields', 1)
    rupture = get_rupture(oqparam)
    res = gmf.ground_motion_fields(
        rupture, sitecol, imts, gsim,
        trunc_level, n_gmfs, correl_model,
        filters.rupture_site_distance_filter(max_dist), seed)
    return {str(imt): matrix for imt, matrix in res.iteritems()}


def calc_gmfs(oqparam, sitecol):
    """
    Build all the ground motion fields for the whole site collection
    """
    correl_model = get_correl_model(oqparam)
    rnd = random.Random()
    rnd.seed(getattr(oqparam, 'random_seed', 42))
    imts = get_imts(oqparam)
    gsim = get_gsim(oqparam)
    trunc_level = getattr(oqparam, 'truncation_level', None)
    n_gmfs = getattr(oqparam, 'number_of_ground_motion_fields', 1)
    rupture = get_rupture(oqparam)
    computer = gmf.GmfComputer(rupture, sitecol, imts, [gsim], trunc_level,
                               correl_model)
    seeds = [rnd.randint(0, 2 ** 31 - 1) for _ in xrange(n_gmfs)]
    res = collections.defaultdict(list)
    for seed in seeds:
        for (_gname, imt), gmvs in computer.compute(seed):
            res[imt].append(gmvs)
    return {imt: numpy.array(matrix).T for imt, matrix in res.iteritems()}


def add_epsilons(assets_by_site, num_samples, seed, correlation):
    """
    Add an attribute named .epsilons to each asset in the assets_by_site
    container.
    """
    assets_by_taxonomy = collections.defaultdict(list)
    for assets in assets_by_site:
        for asset in assets:
            assets_by_taxonomy[asset.taxonomy].append(asset)
    for taxonomy, assets in assets_by_taxonomy.iteritems():
        logging.info('Building (%d, %d) epsilons for taxonomy %s',
                     len(assets), num_samples, taxonomy)
        eps_matrix = scientific.make_epsilons(
            numpy.zeros((len(assets), num_samples)),
            seed, correlation)
        for asset, epsilons in zip(assets, eps_matrix):
            asset.epsilons = epsilons


def run_scenario(oqparam):
    """
    Run a scenario damage or scenario risk computation and returns
    the result dictionary.
    """
    logging.info('Reading the exposure')
    sitecol, assets_by_site = get_sitecol_assets(oqparam)

    logging.info('Computing the GMFs')
    gmfs_by_imt = calc_gmfs(oqparam, sitecol)

    logging.info('Preparing the risk input')
    risk_model = get_risk_model(oqparam)
    risk_inputs = []
    for imt in gmfs_by_imt:
        for site, assets, gmvs in zip(
                sitecol, assets_by_site, gmfs_by_imt[imt]):
            risk_inputs.append(
                workflows.RiskInput(imt, site.id, gmvs, assets))

    if oqparam.calculation_mode == 'scenario_risk':
        # build the epsilon matrix and add the epsilons to the assets
        num_samples = oqparam.number_of_ground_motion_fields
        seed = getattr(oqparam, 'master_seed', 42)
        correlation = getattr(oqparam, 'asset_correlation', 0)
        add_epsilons(assets_by_site, num_samples, seed, correlation)
        calc = calc_scenario
    elif oqparam.calculation_mode == 'scenario_damage':
        calc = calc_damage
    else:
        raise NotImplementedError
    return apply_reduce(calc, (risk_inputs, risk_model),
                        agg=add_dicts, acc={},
                        key=lambda ri: ri.imt,
                        weight=lambda ri: ri.weight)


def calc_damage(riskinputs, riskmodel):
    """
    """
    logging.info('Process %d, considering %d risk input(s) of weight %d',
                 os.getpid(), len(riskinputs),
                 sum(ri.weight for ri in riskinputs))
    result = {}  # taxonomy -> aggfractions
    for loss_type, (assets, fractions) in riskmodel.gen_outputs(riskinputs):
        for asset, fraction in zip(assets, fractions):
            result = add_dicts(
                result, {asset.taxonomy: fraction * asset.number})
    return result


def calc_scenario(riskinputs, riskmodel):
    """
    """
    logging.info('Process %d, considering %d risk input(s) of weight %d',
                 os.getpid(), len(riskinputs),
                 sum(ri.weight for ri in riskinputs))

    result = {}  # agg_type, loss_type -> losses
    for loss_type, outs in riskmodel.gen_outputs(riskinputs):
        (_assets, _loss_ratio_matrix, aggregate_losses,
         _insured_loss_matrix, insured_losses) = outs
        result = add_dicts(result,
                           {('agg', loss_type): aggregate_losses,
                            ('ins', loss_type): insured_losses})
    return result
