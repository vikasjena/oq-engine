"""
Wrapper around the OpenSHA-lite java library.

"""

import os

import numpy

from opengem import hazard
from opengem import java
from opengem import shapes
from opengem.job import mixins
from opengem.hazard import job
from opengem import kvs
from opengem import settings
from opengem.logs import LOG
from opengem.output import geotiff


class MonteCarloMixin:
    """Implements the JobMixin, which has a primary entry point of execute().
    Execute is responsible for dispatching celery tasks.
    Note that this Mixin, during execution, will always be an instance of the Job
    class, and thus has access to the self.params dict, full of config params
    loaded from the Job configuration file."""
    
    def preload(fn):
        """A decorator for preload steps that must run on the Jobber node"""
        def preloader(self, *args, **kwargs):
            """Validate job"""
            assert(self.base_path)
            self.cache = java.jclass("KVS")(
                    settings.MEMCACHED_HOST, 
                    settings.MEMCACHED_PORT)
            return fn(self, *args, **kwargs)
        return preloader
        
    def _get_command_line_calc(self):
        return java.jclass("CommandLineCalculator")(self.cache, self.key)

    def store_source_model(self, config_file):
        """Generates an Earthquake Rupture Forecast, using the source zones and
        logic trees specified in the job config file. Note that this has to be
        done currently using the file itself, since it has nested references to
        other files.
    
        config_file should be an absolute path."""
        engine = java.jclass("CommandLineCalculator")(config_file)
        key = kvs.generate_product_key(self.id, hazard.SOURCE_MODEL_TOKEN)
        engine.sampleAndSaveERFTree(self.cache, key)
    
    def store_gmpe_map(self, config_file):
        """Generates a hash of tectonic regions and GMPEs, using the logic tree
        specified in the job config file.
        
        In the future, this file *could* be passed as a string, since it does 
        not have any included references."""
        engine = java.jclass("CommandLineCalculator")(config_file)
        key = kvs.generate_product_key(self.id, hazard.GMPE_TOKEN)
        engine.sampleAndSaveGMPETree(self.cache, key)

    def site_list_generator(self):
        yield [shapes.Site(40.0, 40.0),]

    @preload
    def execute(self):
        # Chop up subregions
        # For each subregion, take a subset of the source model
        # 
        # Spawn task for subregion, sending in source-subset and 
        # GMPE subset
        
        histories = int(self.params['NUMBER_OF_SEISMICITY_HISTORIES'])
        realizations = int(self.params['NUMBER_OF_HAZARD_CURVE_CALCULATIONS'])
        for i in range(0, histories):
            for j in range(0, realizations):
                self.store_source_model(self.config_file)
                self.store_gmpe_map(self.config_file)
                # TODO(JMC): Don't use the seed again each time
                # TODO(JMC): Get real site list from boundary
                
                for site_list in self.site_list_generator():
                    gmf_id = "%s!%s" % (i, j)
                    self.compute_ground_motion_fields(site_list, gmf_id)
        
            # TODO(JMC): Wait here for the results to be computed
            # if self.params['OUTPUT_GMF_FILES']
            for j in range(0, realizations):
                gmf_id = "%s!%s" % (i, j)
                gmf_key = "%s!GMF!%s" % (self.key, gmf_id)
                print gmf_key
                print kvs.get_client().get(gmf_key)
                gmf = kvs.get_value_json_decoded(gmf_key)
                print gmf
                if gmf:
                    self.write_gmf_file(gmf)
                # results['history%s' % i]['realization%s' %j] = gmf_json
        # print "Fully populated results is %s" % results
        # return results
    
    def write_gmf_file(self, gmfs):
        for gmf in gmfs:
            for rupture in gmfs[gmf]:
                # TODO(JMC): Fix rupture and gmf ids into name
                path = os.path.join(self.base_path, 
                        self.params['OUTPUT_DIR'], "gmfab.tiff") # % gmf.keys()[0].replace("!", ""))
        
                # TODO(JMC): Make this valid region
                switzerland = shapes.Region.from_coordinates(
                    [(10.0, 100.0), (100.0, 100.0), (100.0, 10.0), (10.0, 10.0)])
                image_grid = switzerland.grid
                gwriter = geotiff.GeoTiffFile(path, image_grid)
                for site_key in gmfs[gmf][rupture]:
                    site = gmfs[gmf][rupture][site_key]
                    gwriter.write((site['lon'], site['lat']), int(site['mag']*-254/10))
                gwriter.close()
        
        
    def generate_erf(self):
        jpype = java.jvm()
        erfclass = java.jclass("GEM1ERF")
        key = kvs.generate_product_key(self.id, hazard.SOURCE_MODEL_TOKEN)
        sources = java.jclass("JsonSerializer").getSourceListFromCache(self.cache, key)
        timespan = self.params['INVESTIGATION_TIME']
        return erfclass.getGEM1ERF(sources, jpype.JDouble(float(timespan)))

    def generate_gmpe_map(self):
        key = kvs.generate_product_key(self.id, hazard.GMPE_TOKEN)
        gmpe_map = java.jclass("JsonSerializer").getGmpeMapFromCache(self.cache,key)
        return gmpe_map

    def set_gmpe_params(self, gmpe_map):
        jpype = java.jvm()
        calc = self._get_command_line_calc()
        gmpeLogicTreeData = calc.createGmpeLogicTreeData()
        for tect_region in gmpe_map.keySet():
            gmpe = gmpe_map.get(tect_region)
            gmpeLogicTreeData.setGmpeParams(self.params['COMPONENT'], 
                self.params['INTENSITY_MEASURE_TYPE'], 
                jpype.JDouble(float(self.params['PERIOD'])), 
                jpype.JDouble(float(self.params['DAMPING'])), 
                self.params['GMPE_TRUNCATION_TYPE'], 
                jpype.JDouble(float(self.params['TRUNCATION_LEVEL'])), 
                self.params['STANDARD_DEVIATION_TYPE'], 
                jpype.JDouble(float(self.params['REFERENCE_VS30_VALUE'])), 
                jpype.JObject(gmpe, java.jclass("AttenuationRelationship")))
            gmpe_map.put(tect_region,gmpe)
    
    # def load_ruptures(self):
    #     
    #     erf = self.generate_erf()
    #     
    #     seed = 0 # TODO(JMC): Real seed please
    #     rn = jclass("Random")(seed)
    #     event_set_gen = jclass("EventSetGen")
    #     self.ruptures = event_set_gen.getStochasticEventSetFromPoissonianERF(
    #                         erf, rn)
    
    def get_IML_list(self):
        """Build the appropriate Arbitrary Discretized Func from the IMLs,
        based on the IMT"""
        jpype = java.jvm()
        
        iml_vals = {'PGA' : numpy.log,
                    'MMI' : lambda iml: iml,
                    'PGV' : numpy.log,
                    'PGD' : numpy.log,
                    'SA' : numpy.log,
                     }
        
        iml_list = java.jclass("ArrayList")()
        for val in self.params['INTENSITY_MEASURE_LEVELS'].split(","):
            iml_list.add(
                iml_vals[self.params['INTENSITY_MEASURE_TYPE']](
                float(val)))
        return iml_list

    def parameterize_sites(self, site_list):
        jpype = java.jvm()
        jsite_list = java.jclass("ArrayList")()
        for x in site_list:
            site = x.to_java()
            
            vs30 = java.jclass("DoubleParameter")(jpype.JString("Vs30"))
            vs30.setValue(float(self.params['REFERENCE_VS30_VALUE']))
            depth25 = java.jclass("DoubleParameter")("Depth 2.5 km/sec")
            depth25.setValue(float(self.params['REFERENCE_DEPTH_TO_2PT5KM_PER_SEC_PARAM']))
            sadigh = java.jclass("StringParameter")("Sadigh Site Type")
            sadigh.setValue(self.params['SADIGH_SITE_TYPE'])
            site.addParameter(vs30)
            site.addParameter(depth25)
            site.addParameter(sadigh)
            jsite_list.add(site)
        return jsite_list

    def compute_hazard_curve(self, site_list):
        """Actual hazard curve calculation, runs on the workers.
        Takes a list of Site objects."""
        jpype = java.jvm()

        erf = self.generate_erf()
        gmpe_map = self.generate_gmpe_map()
        self.set_gmpe_params(gmpe_map)

        ## here the site list should be the one appropriate for each worker. Where do I get it?
        ch_iml = self.get_IML_list()
        integration_distance = jpype.JDouble(float(self.params['MAXIMUM_DISTANCE']))
        jsite_list = self.parameterize_sites(site_list)
        LOG.debug("jsite_list: %s" % jsite_list)
        # TODO(JMC): There's Java code for this already, sets each site to have
        # The same default parameters
        
        # hazard curves are returned as Map<Site, DiscretizedFuncAPI>
        hazardCurves = java.jclass("HazardCalculator").getHazardCurves(
            jsite_list, #
            erf,
            gmpe_map,
            ch_iml,
            integration_distance)

        # from hazard curves, probability mass functions are calculated
        # pmf = jclass("DiscretizedFuncAPI")
        pmf_calculator = java.jclass("ProbabilityMassFunctionCalc")
        for site in hazardCurves.keySet():
            pmf = pmf_calculator.getPMF(hazardCurves.get(site))
            hazardCurves.put(site,pmf)
           

    def compute_ground_motion_fields(self, site_list, gmf_id):
        """Ground motion field calculation, runs on the workers."""
        jpype = java.jvm()

        erf = self.generate_erf()
        gmpe_map = self.generate_gmpe_map()
        self.set_gmpe_params(gmpe_map)

        jsite_list = self.parameterize_sites(site_list)

        seed = 0 # TODO(JMC): Real seed please
        rn = java.jclass("Random")(seed)
        key = "%s!GMF!%s" % (self.key, gmf_id)
        java.jclass("HazardCalculator").generateAndSaveGMFs(
                self.cache, key, gmf_id, jsite_list, erf, 
                gmpe_map, rn, jpype.JBoolean(False))


job.HazJobMixin.register("Monte Carlo", MonteCarloMixin)


def guarantee_file(base_path, file_spec):
    """Resolves a file_spec (http, local relative or absolute path, git url,
    etc.) to an absolute path to a (possibly temporary) file."""
    # TODO(JMC): Parse out git, http, or full paths here...
    return os.path.join(base_path, file_spec)
