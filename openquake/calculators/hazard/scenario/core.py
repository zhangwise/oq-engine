# -*- coding: utf-8 -*-
# Copyright (c) 2010-2012, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

"""
Scenario calculator core functionality
"""
import random
from cStringIO import StringIO
from django.db import transaction
import numpy

from nrml.hazard.parsers import RuptureModelParser

# NHLIB
from nhlib.calc import ground_motion_fields
from nhlib import correlation
import nhlib.gsim

from openquake.calculators.hazard import general as haz_general
from openquake import utils, logs
from openquake.db import models
from openquake.input import source
from openquake import writer
from openquake.job.validation import MAX_SINT_32

# FIXME! Duplication in EventBased Hazard Calculator
#: Ground motion correlation model map
GM_CORRELATION_MODEL_MAP = {
    'JB2009': correlation.JB2009CorrelationModel,
}

AVAILABLE_GSIMS = nhlib.gsim.get_available_gsims()


@utils.tasks.oqtask
@utils.stats.count_progress('h')
def gmfs(job_id, rupture_ids, output_id, task_seed, task_no):
    """
    A celery task wrapper function around :func:`compute_gmfs`.
    See :func:`compute_gmfs` for parameter definitions.
    """
    logs.LOG.debug('> starting task: job_id=%s, task_no=%s'
                   % (job_id, task_no))

    numpy.random.seed(task_seed)
    compute_gmfs(job_id, rupture_ids, output_id, task_seed, task_no)
    # Last thing, signal back the control node to indicate the completion of
    # task. The control node needs this to manage the task distribution and
    # keep track of progress.
    logs.LOG.debug('< task complete, signalling completion')
    haz_general.signal_task_complete(job_id=job_id, num_items=1)


def compute_gmfs(job_id, rupture_ids, output_id, task_seed, task_no):
    hc = models.HazardCalculation.objects.get(oqjob=job_id)
    rupture_mdl = source.nrml_to_nhlib(
        models.ParsedRupture.objects.get(id=rupture_ids[0]).nrml,
        hc.rupture_mesh_spacing, None, None)
    sites = haz_general.get_site_collection(hc)
    imts = [haz_general.imt_to_nhlib(x) for x in hc.intensity_measure_types]
    GSIM = AVAILABLE_GSIMS[hc.gsim]

    gmf = ground_motion_fields(
        rupture_mdl, sites, imts, GSIM(),
        hc.truncation_level, realizations=1,
        correlation_model=None)
    points_to_compute = hc.points_to_compute()
    save_gmf(output_id, gmf, points_to_compute, task_no)


@transaction.commit_on_success(using='reslt_writer')
def save_gmf(output_id, gmf_dict, points_to_compute, result_grp_ordinal):
    """
    Helper method to save computed GMF data to the database.

    :param int output_id:
        Output_id identifies the reference to the output record.
    :param dict gmf_dict:
        The GMF results during the calculation.
    :param points_to_compute:
        An :class:`nhlib.geo.mesh.Mesh` object, representing all of the points
        of interest for a calculation.
    :param int result_grp_ordinal:
        The sequence number (1 to N) of the task which computed these results.

        A calculation consists of N tasks, so this tells us which task computed
        the data.
    """

    inserter = writer.BulkInserter(models.GmfScenario)

    for imt, gmfs in gmf_dict.iteritems():
        # ``gmfs`` comes in as a numpy.matrix
        # we want it is an array; it handles subscripting
        # in the way that we want
        gmfs = numpy.array(gmfs)

        sa_period = None
        sa_damping = None
        if isinstance(imt, nhlib.imt.SA):
            sa_period = imt.period
            sa_damping = imt.damping
        imt_name = imt.__class__.__name__

        for i, location in enumerate(points_to_compute):
            inserter.add_entry(
                output_id=output_id,
                imt=imt_name,
                sa_period=sa_period,
                sa_damping=sa_damping,
                location=location.wkt2d,
                gmvs=gmfs[i].tolist(),
                result_grp_ordinal=result_grp_ordinal,
            )

    inserter.flush()


class ScenarioHazardCalculator(haz_general.BaseHazardCalculatorNext):

    core_calc_task = gmfs

    def initialize_sources(self):
        """
        """
        # Get the rupture model in input
        [inp] = models.inputs4hcalc(self.hc.id, input_type='rupture_model')

        # Associate the source input to the calculation:
        models.Input2hcalc.objects.get_or_create(
            input=inp, hazard_calculation=self.hc)

        # Store the ParsedRupture record
        src_content = StringIO(inp.model_content.raw_content)
        rupt_parser = RuptureModelParser(src_content)
        src_db_writer = source.RuptureDBWriter(inp, rupt_parser.parse())
        src_db_writer.serialize()

    def pre_execute(self):
        """
        Do pre-execution work. At the moment, this work entails: parsing and
        initializing sources, parsing and initializing the site model (if there
        is one), and generating logic tree realizations. (The latter piece
        basically defines the work to be done in the `execute` phase.)
        """

        # Create source Inputs.
        self.initialize_sources()

        # Deal with the site model and compute site data for the calculation
        # If no site model file was specified, reference parameters are used
        # for all sites.
        self.initialize_site_model()
        self.progress['total'] = self.hc.number_of_ground_motion_fields

        # Store a record in the output table.
        self.output = models.Output.objects.create(
            owner=self.job.owner,
            oq_job=self.job,
            display_name="gmf_scenario",
            output_type="gmf_scenario")
        self.output.save()

    def task_arg_gen(self, block_size):
        """
        Loop through realizations and sources to generate a sequence of
        task arg tuples. Each tuple of args applies to a single task.

        Yielded results are quadruples of (job_id, task_no,
        rupture_id, random_seed). (random_seed will be used to seed
        numpy for temporal occurence sampling.)

        :param int block_size:
            The number of work items for each task. Fixed to 1.
        """
        rnd = random.Random()
        rnd.seed(self.hc.random_seed)

        inp = models.inputs4hcalc(self.hc.id, 'rupture_model')[0]
        ruptures = models.ParsedRupture.objects.filter(input__id=inp.id)
        rupture_ids = [rupture.id for rupture in ruptures]
        for task_no in range(self.hc.number_of_ground_motion_fields):
            task_seed = rnd.randint(0, MAX_SINT_32)
            task_args = (self.job.id, rupture_ids,
                         self.output.id, task_seed, task_no)
            yield task_args
