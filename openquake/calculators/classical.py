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

import numpy
import logging
import operator
import collections
from functools import partial

from openquake.hazardlib.geo.utils import get_spherical_bounding_box
from openquake.hazardlib.geo.utils import get_longitudinal_extent
from openquake.hazardlib.geo.geodetic import npoints_between
from openquake.hazardlib.site import SiteCollection
from openquake.hazardlib.calc.filters import source_site_distance_filter
from openquake.hazardlib.calc.hazard_curve import (
    hazard_curves_per_trt, zero_curves, zero_maps, agg_curves)
from openquake.risklib import scientific
from openquake.commonlib import parallel, source, datastore
from openquake.calculators.views import get_data_transfer
from openquake.baselib.general import AccumDict, split_in_blocks

from openquake.calculators import base, calc


HazardCurve = collections.namedtuple('HazardCurve', 'location poes')


# this is needed for the disaggregation
class BoundingBox(object):
    """
    A class to store the bounding box in distances, longitudes and magnitudes,
    given a source model and a site. This is used for disaggregation
    calculations. The goal is to determine the minimum and maximum
    distances of the ruptures generated from the model from the site;
    moreover the maximum and minimum longitudes and magnitudes are stored, by
    taking in account the international date line.
    """
    def __init__(self, lt_model_id, site_id):
        self.lt_model_id = lt_model_id
        self.site_id = site_id
        self.min_dist = self.max_dist = None
        self.east = self.west = self.south = self.north = None

    def update(self, dists, lons, lats):
        """
        Compare the current bounding box with the value in the arrays
        dists, lons, lats and enlarge it if needed.

        :param dists:
            a sequence of distances
        :param lons:
            a sequence of longitudes
        :param lats:
            a sequence of latitudes
        """
        if self.min_dist is not None:
            dists = [self.min_dist, self.max_dist] + dists
        if self.west is not None:
            lons = [self.west, self.east] + lons
        if self.south is not None:
            lats = [self.south, self.north] + lats
        self.min_dist, self.max_dist = min(dists), max(dists)
        self.west, self.east, self.north, self.south = \
            get_spherical_bounding_box(lons, lats)

    def update_bb(self, bb):
        """
        Compare the current bounding box with the given bounding box
        and enlarge it if needed.

        :param bb:
            an instance of :class:
            `openquake.engine.calculators.hazard.classical.core.BoundingBox`
        """
        if bb:  # the given bounding box must be non-empty
            self.update([bb.min_dist, bb.max_dist], [bb.west, bb.east],
                        [bb.south, bb.north])

    def bins_edges(self, dist_bin_width, coord_bin_width):
        """
        Define bin edges for disaggregation histograms, from the bin data
        collected from the ruptures.

        :param dists:
            array of distances from the ruptures
        :param lons:
            array of longitudes from the ruptures
        :param lats:
            array of latitudes from the ruptures
        :param dist_bin_width:
            distance_bin_width from job.ini
        :param coord_bin_width:
            coordinate_bin_width from job.ini
        """
        dist_edges = dist_bin_width * numpy.arange(
            int(self.min_dist / dist_bin_width),
            int(numpy.ceil(self.max_dist / dist_bin_width) + 1))

        west = numpy.floor(self.west / coord_bin_width) * coord_bin_width
        east = numpy.ceil(self.east / coord_bin_width) * coord_bin_width
        lon_extent = get_longitudinal_extent(west, east)

        lon_edges, _, _ = npoints_between(
            west, 0, 0, east, 0, 0,
            numpy.round(lon_extent / coord_bin_width) + 1)

        lat_edges = coord_bin_width * numpy.arange(
            int(numpy.floor(self.south / coord_bin_width)),
            int(numpy.ceil(self.north / coord_bin_width) + 1))

        return dist_edges, lon_edges, lat_edges

    def __nonzero__(self):
        """
        True if the bounding box is non empty.
        """
        return (self.min_dist is not None and self.west is not None and
                self.south is not None)


@parallel.litetask
def classical(sources, sitecol, rlzs_assoc, monitor):
    """
    :param sources:
        a non-empty sequence of sources of homogeneous tectonic region type
    :param sitecol:
        a SiteCollection instance
    :param rlzs_assoc:
        a RlzsAssoc instance
    :param monitor:
        a monitor instance
    :returns:
        an AccumDict rlz -> curves
    """
    max_dist = monitor.oqparam.maximum_distance
    truncation_level = monitor.oqparam.truncation_level
    imtls = monitor.oqparam.imtls
    trt_model_id = sources[0].trt_model_id
    gsims = rlzs_assoc.gsims_by_trt_id[trt_model_id]
    for smodel in rlzs_assoc.csm_info.source_models:
        for trt_model in smodel.trt_models:
            if trt_model.id == trt_model_id:
                sm_id = smodel.ordinal
                break

    if monitor.oqparam.poes_disagg:
        bbs = [BoundingBox(sm_id, sid) for sid in sitecol.sids]
    else:
        bbs = []
    # NB: the source_site_filter below is ESSENTIAL for performance inside
    # hazard_curves_per_trt, since it reduces the full site collection
    # to a filtered one *before* doing the rupture filtering
    curves_by_gsim = hazard_curves_per_trt(
        sources, sitecol, imtls, gsims, truncation_level,
        source_site_filter=source_site_distance_filter(max_dist),
        maximum_distance=max_dist, bbs=bbs, monitor=monitor)
    dic = dict(calc_times=monitor.calc_times, bbs=bbs)
    for gsim, curves in zip(gsims, curves_by_gsim):
        dic[trt_model_id, str(gsim)] = curves
    return dic


def agg_dicts(acc, val):
    """
    Aggregate dictionaries of hazard curves by updating the accumulator
    """
    for key in val:
        if key == 'calc_times':
            acc[key].extend(val[key])
        elif key == 'bbs':
            for bb in val['bbs']:
                acc['bb_dict'][bb.lt_model_id, bb.site_id].update_bb(bb)
        else:  # aggregate curves
            acc[key] = agg_curves(acc[key], val[key])
    return acc


source_info_dt = numpy.dtype(
    [('trt_model_id', numpy.uint32),
     ('source_id', (bytes, 20)),
     ('calc_time', numpy.float32)])


def store_source_chunks(dstore):
    """
    Get information about the source data transfer and store it
    in the datastore, under the name 'source_chunks'.

    This is a composite array (num_srcs, weight) displaying info the
    block of sources internally generated by the grouping procedure
    :function:openquake.baselib.split_in_blocks

    :param dstore: the datastore of the current calculation
    """
    dstore['source_chunks'], forward, back = get_data_transfer(dstore)
    attrs = dstore['source_chunks'].attrs
    attrs['to_send_forward'] = forward
    attrs['to_send_back'] = back
    dstore.hdf5.flush()


@base.calculators.add('classical')
class ClassicalCalculator(base.HazardCalculator):
    """
    Classical PSHA calculator
    """
    core_func = classical
    source_info = datastore.persistent_attribute('source_info')

    def execute(self):
        """
        Run in parallel `core_func(sources, sitecol, monitor)`, by
        parallelizing on the sources according to their weight and
        tectonic region type.
        """
        monitor = self.monitor.new(self.core_func.__name__)
        monitor.oqparam = self.oqparam
        sources = self.csm.get_sources()
        zc = zero_curves(len(self.sitecol.complete), self.oqparam.imtls)
        zerodict = AccumDict((key, zc) for key in self.rlzs_assoc)
        zerodict['calc_times'] = []
        if self.oqparam.poes_disagg:
            zerodict['bb_dict'] = {
                (smodel.ordinal, site.id): BoundingBox(smodel.ordinal, site.id)
                for site in self.sitecol
                for smodel in self.csm.source_models}
        curves_by_trt_gsim = parallel.apply_reduce(
            self.core_func.__func__,
            (sources, self.sitecol, self.rlzs_assoc, monitor),
            agg=agg_dicts, acc=zerodict,
            concurrent_tasks=self.oqparam.concurrent_tasks,
            weight=operator.attrgetter('weight'),
            key=operator.attrgetter('trt_model_id'))
        self.bb_dict = curves_by_trt_gsim.pop('bb_dict', {})
        if self.persistent:
            store_source_chunks(self.datastore)
        return curves_by_trt_gsim

    def post_execute(self, curves_by_trt_gsim):
        """
        Collect the hazard curves by realization and export them.

        :param curves_by_trt_gsim:
            a dictionary (trt_id, gsim) -> hazard curves
        """
        # save calculation time per source
        try:
            calc_times = curves_by_trt_gsim.pop('calc_times')
        except KeyError:
            pass
        else:
            sources = self.csm.get_sources()
            info = []
            for i, dt in calc_times:
                src = sources[i]
                info.append((src.trt_model_id, src.source_id, dt))
            info.sort(key=operator.itemgetter(2), reverse=True)
            self.source_info = numpy.array(info, source_info_dt)

        # save curves_by_trt_gsim
        for sm in self.rlzs_assoc.csm_info.source_models:
            group = self.datastore.hdf5.create_group(
                'curves_by_sm/' + '_'.join(sm.path))
            group.attrs['source_model'] = sm.name
            for tm in sm.trt_models:
                for gsim in tm.gsims:
                    try:
                        curves = curves_by_trt_gsim[tm.id, gsim]
                    except KeyError:  # no data for the trt_model
                        pass
                    else:
                        ts = '%03d-%s' % (tm.id, gsim)
                        group[ts] = curves
                        group[ts].attrs['trt'] = tm.trt
        oq = self.oqparam
        zc = zero_curves(len(self.sitecol.complete), oq.imtls)
        curves_by_rlz = self.rlzs_assoc.combine_curves(
            curves_by_trt_gsim, agg_curves, zc)
        rlzs = self.rlzs_assoc.realizations
        nsites = len(self.sitecol)
        if oq.individual_curves:
            for rlz, curves in curves_by_rlz.items():
                self.store_curves('rlz-%03d' % rlz.ordinal, curves, rlz)

        if len(rlzs) == 1:  # cannot compute statistics
            [self.mean_curves] = curves_by_rlz.values()
            return

        weights = (None if oq.number_of_logic_tree_samples
                   else [rlz.weight for rlz in rlzs])
        mean = oq.mean_hazard_curves
        if mean:
            self.mean_curves = numpy.array(zc)
            for imt in oq.imtls:
                self.mean_curves[imt] = scientific.mean_curve(
                    [curves_by_rlz[rlz][imt] for rlz in rlzs], weights)

        self.quantile = {}
        for q in oq.quantile_hazard_curves:
            self.quantile[q] = qc = numpy.array(zc)
            for imt in oq.imtls:
                curves = [curves_by_rlz[rlz][imt] for rlz in rlzs]
                qc[imt] = scientific.quantile_curve(
                    curves, q, weights).reshape((nsites, -1))

        if mean:
            self.store_curves('mean', self.mean_curves)
        for q in self.quantile:
            self.store_curves('quantile-%s' % q, self.quantile[q])

    def hazard_maps(self, curves):
        """
        Compute the hazard maps associated to the curves
        """
        n, p = len(self.sitecol), len(self.oqparam.poes)
        maps = zero_maps((n, p), self.oqparam.imtls)
        for imt in curves.dtype.fields:
            maps[imt] = calc.compute_hazard_maps(
                curves[imt], self.oqparam.imtls[imt], self.oqparam.poes)
        return maps

    def store_curves(self, kind, curves, rlz=None):
        """
        Store all kind of curves, optionally computing maps and uhs curves.

        :param kind: the kind of curves to store
        :param curves: an array of N curves to store
        :param rlz: hazard realization, if any
        """
        if not self.persistent:  # do nothing
            return
        oq = self.oqparam
        self._store('hcurves/' + kind, curves, rlz)
        if oq.hazard_maps or oq.uniform_hazard_spectra:
            # hmaps is a composite array of shape (N, P)
            hmaps = self.hazard_maps(curves)
            if oq.hazard_maps:
                self._store('hmaps/' + kind, hmaps, rlz, poes=oq.poes)
            if oq.uniform_hazard_spectra:
                # uhs is an array of shape (N, I, P)
                self._store('uhs/' + kind, calc.make_uhs(hmaps, oq.poes), rlz)

    def _store(self, name, curves, rlz, **kw):
        self.datastore.hdf5[name] = curves
        dset = self.datastore.hdf5[name]
        if rlz is not None:
            dset.attrs['uid'] = rlz.uid
        for k, v in kw.items():
            dset.attrs[k] = v


def is_effective_trt_model(result_dict, trt_model):
    """
    Returns True on tectonic region types
    which ID in contained in the result_dict.

    :param result_dict: a dictionary with keys (trt_id, gsim)
    """
    return any(trt_model.id == key[0] for key in result_dict)


@parallel.litetask
def classical_tiling(calculator, sitecol, position, tileno, monitor):
    """
    :param calculator:
        a ClassicalCalculator instance
    :param sitecol:
        the site collection of the current tile
    :param position:
        position of the current tile in the full site collection
    :param tileno:
        the tile ordinal
    :param monitor:
        a monitor instance
    :returns:
        a dictionary file name -> full path for each exported file
    """
    calculator.sitecol = sitecol
    calculator.tileno = '.%04d' % tileno
    curves_by_trt_gsim = calculator.execute()
    curves_by_trt_gsim.indices = list(range(position, position + len(sitecol)))
    # build the correct realizations from the (reduced) logic tree
    calculator.rlzs_assoc = calculator.csm.get_rlzs_assoc(
        partial(is_effective_trt_model, curves_by_trt_gsim))
    n_levels = sum(len(imls) for imls in calculator.oqparam.imtls.values())
    tup = (len(calculator.sitecol), n_levels, len(calculator.rlzs_assoc),
           len(calculator.rlzs_assoc.realizations))
    logging.info('Processed tile %d, (sites, levels, keys, rlzs)=%s',
                 tileno, tup)
    return curves_by_trt_gsim


def agg_curves_by_trt_gsim(acc, curves_by_trt_gsim):
    """
    :param acc: AccumDict (trt_id, gsim) -> N curves
    :param curves_by_trt_gsim: AccumDict (trt_id, gsim) -> T curves

    where N is the total number of sites and T the number of sites
    in the current tile. Works by side effect, by updating the accumulator.
    """
    for k in curves_by_trt_gsim:
        if k == 'calc_times':
            acc[k].extend(curves_by_trt_gsim[k])
        else:
            acc[k][curves_by_trt_gsim.indices] = curves_by_trt_gsim[k]
    return acc


@base.calculators.add('classical_tiling')
class ClassicalTilingCalculator(ClassicalCalculator):
    """
    Classical Tiling calculator
    """
    SourceProcessor = source.SourceFilter

    def execute(self):
        """
        Split the computation by tiles which are run in parallel.
        """
        monitor = self.monitor.new(self.core_func.__name__)
        monitor.oqparam = oq = self.oqparam
        self.tiles = split_in_blocks(
            self.sitecol, self.oqparam.concurrent_tasks or 1)
        oq.concurrent_tasks = 0
        calculator = ClassicalCalculator(
            self.oqparam, monitor, persistent=False)
        calculator.csm = self.csm
        rlzs_assoc = self.csm.get_rlzs_assoc()
        self.rlzs_assoc = calculator.rlzs_assoc = rlzs_assoc

        # parallelization
        all_args = []
        position = 0
        for (i, tile) in enumerate(self.tiles):
            all_args.append((calculator, SiteCollection(tile),
                             position, i, monitor))
            position += len(tile)
        acc = {trt_gsim: zero_curves(len(self.sitecol), oq.imtls)
               for trt_gsim in calculator.rlzs_assoc}
        acc['calc_times'] = []
        return parallel.starmap(classical_tiling, all_args).reduce(
            agg_curves_by_trt_gsim, acc)
