# Name:     Valley Bottom
#
# Purpose:  Perform initial VBET analysis that can be used by the BRAT conservation
#           module
#
# Author:   Philip Bailey
#
# Date:     7 Oct 2019
#
# Vectorize polygons from raster
# https://gis.stackexchange.com/questions/187877/how-to-polygonize-raster-to-shapely-polygons
# -------------------------------------------------------------------------------
import argparse
import os
import sys
import uuid
import traceback
import datetime
import time
import json
import math
import shutil
from osgeo import gdal
from osgeo import ogr
import osgeo.osr as osr
from shapely.wkb import loads as wkb_load
from shapely.geometry import mapping, shape, Polygon, MultiPolygon
from shapely.ops import unary_union
import rasterio
from rasterio.mask import mask
from rasterio.features import shapes
from rasterio import features
import numpy as np
from math import sqrt
from scipy.interpolate import interp1d
from vbet.vbet_network import vbet_network
from vbet.vbet_report import VBETReport
from rscommons.util import safe_makedirs, parse_metadata
from rscommons import RSProject, RSLayer, ModelConfig, ProgressBar, Logger, dotenv
from rscommons.shapefile import _rough_convert_metres_to_raster_units, polygonize, copy_feature_class, intersect_feature_classes, remove_holes, get_pts, get_rings, get_geometry_unary_union, export_geojson
from rscommons.prism import buffer_by_field
from vbet.__version__ import __version__

cfg = ModelConfig('http://xml.riverscapes.xyz/Projects/XSD/V1/VBET.xsd', __version__)

thresh_vals = {"50": 0.5, "60": 0.6, "70": 0.7, "80": 0.8, "90": 0.9, "100": 1}

LayerTypes = {
    'SLOPE_RASTER': RSLayer('Slope Raster', 'SLOPE_RASTER', 'Raster', 'inputs/slope.tif'),
    'HAND_RASTER': RSLayer('Hand Raster', 'HAND_RASTER', 'Raster', 'inputs/hand.tif'),
    'HILLSHADE': RSLayer('DEM Hillshade', 'HILLSHADE', 'Raster', 'inputs/dem_hillshade.tif'),
    'CHANNEL_RASTER': RSLayer('Channel Raster', 'CHANNEL_RASTER', 'Raster', 'inputs/channel.tif'),
    'FLOWLINES': RSLayer('NHD Flowlines', 'FLOWLINES', 'Vector', 'inputs/flowlines.shp'),
    'FLOW_AREA': RSLayer('NHD Flow Areas', 'FLOW_AREA', 'Vector', 'inputs/flow_areas.shp'),
    'SLOPE_EV': RSLayer('Evidence Raster', 'SLOPE_EV_TMP', 'Raster', 'intermediates/nLoE_Slope.tif'),
    'HAND_EV': RSLayer('Evidence Raster', 'HAND_EV_TMP', 'Raster', 'intermediates/nLoE_HAND.tif'),
    'CHANNEL_MASK': RSLayer('Evidence Raster', 'CH_MASK', 'Raster', 'intermediates/nLOE_Channels.tif'),
    'EVIDENCE': RSLayer('Evidence Raster', 'EVIDENCE', 'Raster', 'intermediates/Evidence.tif'),
    'COMBINED_VRT': RSLayer('Combined VRT', 'COMBINED_VRT', 'VRT', 'intermediates/slope-hand-channel.vrt'),
    'VBET_NETWORK': RSLayer('VBET Network', 'VBET_NETWORK', 'Vector', 'intermediates/vbet_network.shp'),
    'CHANNEL_POLYGON': RSLayer('Combined VRT', 'CHANNEL_POLYGON', 'Vector', 'intermediates/channel.shp'),
    'REPORT': RSLayer('RSContext Report', 'REPORT', 'HTMLFile', 'outputs/vbet.html')
}
# Build our threshold Layers programmatically
for k, v in thresh_vals.items():
    LayerTypes['THRESH_{}'.format(k)] = RSLayer('Threshold at {}%'.format(k), 'THRESH_{}'.format(k), 'Vector', 'intermediates/thresh_{}.shp'.format(k))
    LayerTypes['VBET_{}'.format(k)] = RSLayer('VBET {}%'.format(k), 'VBET_{}'.format(k), 'Vector', 'outputs/vbet_{}.shp'.format(k))


def vbet(huc, flowlines, flowareas, orig_slope, max_slope, orig_hand, hillshade, max_hand, min_hole_area_m, project_folder, meta):

    log = Logger('VBET')
    log.info('Starting VBET v.{}'.format(cfg.version))

    project, realization, proj_nodes = create_project(huc, project_folder)

    # Copy the inp
    _proj_slope_node, proj_slope = project.add_project_raster(proj_nodes['Inputs'], LayerTypes['SLOPE_RASTER'], orig_slope)
    _proj_hand_node, proj_hand = project.add_project_raster(proj_nodes['Inputs'], LayerTypes['HAND_RASTER'], orig_hand)
    _hillshade_node, hillshade = project.add_project_raster(proj_nodes['Inputs'], LayerTypes['HILLSHADE'], hillshade)

    # Create a copy of the flow lines with just the perennial and also connectors inside flow areas
    project.add_project_vector(proj_nodes['Inputs'], LayerTypes['FLOWLINES'], flowlines)
    project.add_project_vector(proj_nodes['Inputs'], LayerTypes['FLOW_AREA'], flowareas)

    vbet_network_path = os.path.join(project_folder, LayerTypes['VBET_NETWORK'].rel_path)
    vbet_network(flowlines, flowareas, cfg.OUTPUT_EPSG, vbet_network_path)
    project.add_project_vector(proj_nodes['Intermediates'], LayerTypes['VBET_NETWORK'])

    # Get raster resolution as min buffer and apply bankfull width buffer to reaches
    with rasterio.open(proj_slope) as raster:
        t = raster.transform
        min_buffer = (t[0] + abs(t[4])) / 2

    reach_polygon = buffer_by_field(vbet_network_path, "BFwidth", cfg.OUTPUT_EPSG, min_buffer)
    log.info("Buffering Polyine by bankfull width buffers")

    # Old single 25m buffer
    # Load all the reaches into single polyline and also buffer them into single polygon
    # polyline = get_geometry_unary_union(vbet_network_path, cfg.OUTPUT_EPSG)
    # reach_buffer = _rough_convert_metres_to_raster_units(proj_slope, 25)
    # log.info('Buffering Polyline by: {}'.format(reach_buffer))
    # reach_polygon = polyline.buffer(reach_buffer)

    # Create channel polygon by combining the reach polygon with the flow area polygon
    area_polygon = get_geometry_unary_union(flowareas, cfg.OUTPUT_EPSG)
    log.info('Unioning reach and area polygons')

    # Union the buffered reach and area polygons
    if area_polygon is None or area_polygon.area == 0:
        log.warning('Area of the area polygon is 0, we will disregard it')
        channel_polygon = reach_polygon
    else:
        channel_polygon = unary_union([reach_polygon, area_polygon])
        reach_polygon = None  # free up some memory
        area_polygon = None

    # Rasterize the channel polygon and write to raster
    log.info('Writing channel raster using slope as a template')
    channel_raster = os.path.join(project_folder, LayerTypes['CHANNEL_RASTER'].rel_path)
    with rasterio.open(proj_slope) as slope_src:
        chl_meta = slope_src.meta
        chl_meta['dtype'] = rasterio.uint8
        chl_meta['nodata'] = 0
        chl_meta['compress'] = 'deflate'
        image = features.rasterize([(mapping(channel_polygon), 1)], out_shape=slope_src.shape, transform=slope_src.transform, fill=0)
        with rasterio.open(channel_raster, 'w', **chl_meta) as dst:
            dst.write(image, indexes=1)
    project.add_project_raster(proj_nodes['Intermediates'], LayerTypes['CHANNEL_RASTER'])

    # Create a VRT that combines all the evidence rasters
    log.info('Creating combined evidence VRT')
    vrtpath = os.path.join(project_folder, LayerTypes['COMBINED_VRT'].rel_path)
    vrt_options = gdal.BuildVRTOptions(resampleAlg='nearest', separate=True, resolution='average')

    gdal.BuildVRT(vrtpath, [
        proj_slope,
        proj_hand,
        channel_raster
    ], options=vrt_options)

    # Generate the evidence raster from the VRT. This is a little annoying but reading across
    # different dtypes in one VRT is not supported in GDAL > 3.0 so we dump them into individual rasters
    slope_ev = os.path.join(project_folder, LayerTypes['SLOPE_EV'].rel_path)
    translateoptions = gdal.TranslateOptions(gdal.ParseCommandLine("-of Gtiff -b 1 -co COMPRESS=DEFLATE"))
    gdal.Translate(slope_ev, vrtpath, options=translateoptions)

    hand_ev = os.path.join(project_folder, LayerTypes['HAND_EV'].rel_path)
    translateoptions = gdal.TranslateOptions(gdal.ParseCommandLine("-of Gtiff -b 2 -co COMPRESS=DEFLATE"))
    gdal.Translate(hand_ev, vrtpath, options=translateoptions)

    channel_msk = os.path.join(project_folder, LayerTypes['CHANNEL_MASK'].rel_path)
    translateoptions = gdal.TranslateOptions(gdal.ParseCommandLine("-of Gtiff -b 3 -co COMPRESS=DEFLATE"))
    gdal.Translate(channel_msk, vrtpath, options=translateoptions)

    evidence_raster = os.path.join(project_folder, LayerTypes['EVIDENCE'].rel_path)

    # Open evidence rasters concurrently. We're looping over windows so this shouldn't affect
    # memory consumption too much
    with rasterio.open(slope_ev) as slp_src, rasterio.open(hand_ev) as hand_src:
        # All 3 rasters should have the same extent and properties. They differ only in dtype
        out_meta = slp_src.meta
        # Rasterio can't write back to a VRT so rest the driver and number of bands for the output
        out_meta['driver'] = 'GTiff'
        out_meta['count'] = 1
        out_meta['compress'] = 'deflate'
        chl_meta['dtype'] = rasterio.uint8
        # We use this to buffer the output
        cell_size = abs(slp_src.get_transform()[1])

        # Evidence raster logic
        def ffunc(x, y):

            # Retain slope less than threshold. Invert and scale 0 (high slope) to 1 (low slope).
            z1 = 1 + (x / (-1 * max_slope))
            z1.mask = np.ma.mask_or(z1.mask, z1 < 0)

            # Retain HAND under threshold. Invert and scale to 0 (high HAND) to 1 (low HAND).
            z2 = 1 + (y / (-1 * max_hand))
            z2.mask = np.ma.mask_or(z2.mask, z2 < 0)
            z3 = z1 * z2
            return z3

        with rasterio.open(evidence_raster, "w", **out_meta) as dest:
            progbar = ProgressBar(len(list(slp_src.block_windows(1))), 50, "Calculating evidence layer")
            counter = 0
            # Again, these rasters should be orthogonal so their windows should also line up
            for ji, window in slp_src.block_windows(1):
                progbar.update(counter)
                counter += 1
                slope_data = slp_src.read(1, window=window, masked=True)
                hand_data = hand_src.read(1, window=window, masked=True)

                # Fill the masked values with the appropriate nodata vals
                fvals = ffunc(slope_data, hand_data)
                # Unthresholded in the base band (mostly for debugging)
                dest.write(np.ma.filled(np.float32(fvals), out_meta['nodata']), window=window, indexes=1)

            progbar.finish()

        # Hand and slope are duplicated so we can safely remove them
        # rasterio.shutil.delete(slope_ev)
        # rasterio.shutil.delete(hand_ev)
        # The remaining rasters get added to the project
        project.add_project_raster(proj_nodes['Intermediates'], LayerTypes['EVIDENCE'])

    # Get the length of a meter (roughly)
    degree_factor = _rough_convert_metres_to_raster_units(proj_slope, 1)
    buff_dist = cell_size
    min_hole_degrees = min_hole_area_m * (degree_factor ** 2)

    # Create our threshold rasters in a temporary folder
    # These files get immediately polygonized so they are of very little value afterwards
    tmp_folder = os.path.join(project_folder, 'tmp')
    safe_makedirs(tmp_folder)

    for str_val, thr_val in thresh_vals.items():
        thresh_raster_path = os.path.join(tmp_folder, 'evidence_mask_{}.tif'.format(str_val))
        with rasterio.open(evidence_raster) as fval_src, rasterio.open(channel_msk) as ch_msk_src:
            out_meta = fval_src.meta
            out_meta['count'] = 1
            out_meta['compress'] = 'deflate'
            out_meta['dtype'] = rasterio.uint8
            out_meta['nodata'] = 0

            log.info('Thresholding at {}'.format(thr_val))
            with rasterio.open(thresh_raster_path, "w", **out_meta) as dest:
                progbar = ProgressBar(len(list(fval_src.block_windows(1))), 50, "Thresholding at {}".format(thr_val))
                counter = 0
                for ji, window in fval_src.block_windows(1):
                    progbar.update(counter)
                    counter += 1
                    fval_data = fval_src.read(1, window=window, masked=True)
                    ch_data = ch_msk_src.read(1, window=window, masked=True)
                    # Fill an array with "1" values to give us a nice mask for polygonize
                    fvals_mask = np.full(fval_data.shape, np.uint8(1))

                    # Create a raster with 1.0 as a value everywhere in the same shape as fvals
                    new_fval_mask = np.ma.mask_or(fval_data.mask, fval_data < thr_val)
                    masked_arr = np.ma.array(fvals_mask, mask=[new_fval_mask & ch_data.mask])
                    dest.write(np.ma.filled(masked_arr, out_meta['nodata']), window=window, indexes=1)
                progbar.finish()

        log.info('Polygonizing')
        thresh_type = LayerTypes['THRESH_{}'.format(str_val)]
        polygonize_path = os.path.join(project_folder, thresh_type.rel_path)
        polygonize(thresh_raster_path, 1, polygonize_path, cfg.OUTPUT_EPSG)
        project.add_project_vector(proj_nodes['Intermediates'], thresh_type)

        log.info('Sanitizing')
        vbet_type = LayerTypes['VBET_{}'.format(str_val)]
        final_path = os.path.join(project_folder, vbet_type.rel_path)
        sanitize(polygonize_path, channel_polygon, final_path, min_hole_degrees, buff_dist)
        project.add_project_vector(proj_nodes['Outputs'], vbet_type)

    # Channel mask is duplicated from inputs so we delete it.
    rasterio.shutil.delete(channel_msk)

    report_path = os.path.join(project.project_dir, LayerTypes['REPORT'].rel_path)
    project.add_report(realization, LayerTypes['REPORT'], replace=True)

    report = VBETReport(report_path, project, project_folder)
    report.write()

    # No need to keep the masks around
    try:
        shutil.rmtree(tmp_folder)

    except OSError as e:
        print("Error cleaning up tmp dir: {}".format(e.strerror))

    # Incorporate project metadata to the riverscapes project
    project.add_metadata(meta)

    log.info('VBET Completed Successfully')


def sanitize(input_path, channel_polygon, output_path, min_hole_sq_deg, buff_dist):
    """It's important to make sure we have the right kinds of geometries. Here we:
        1. Buffer out then back in by the same amount. TODO: THIS IS SUPER SLOW.
        2. Simply: for some reason area isn't calculated on inner rings so we need to simplify them first
        3. Remove small holes: Do we have donuts? Filter anythign smaller than a certain area

    Args:
        input_path ([type]): [description]
        channel_polygon ([type]): [description]
        output_path ([type]): [description]
        min_hole_sq_deg ([type]): Size of the minimul hole you want to keep.
        buff_dist ([type]): Usually this is the cell size of the slope raster
    """
    log = Logger('VBET Simplify')
    driver = ogr.GetDriverByName("ESRI Shapefile")
    data_source_in = driver.Open(input_path, 0)
    layer_in = data_source_in.GetLayer()

    ogr_polygon = ogr.CreateGeometryFromWkb(channel_polygon.wkb)

    if os.path.exists(output_path):
        driver.DeleteDataSource(output_path)

    data_source_out = driver.CreateDataSource(output_path)
    out_spatial_ref = layer_in.GetSpatialRef()
    layer_out = data_source_out.CreateLayer('vbet', out_spatial_ref, geom_type=ogr.wkbPolygon)
    featureDefn = layer_in.GetLayerDefn()
    layer_in.SetSpatialFilter(ogr_polygon)

    geoms = []
    pts = 0
    square_buff = buff_dist * buff_dist
    # NOTE: Order of operations really matters here.
    counter = 0

    # DEBUGGING
    # debug_dir = os.path.join(os.path.dirname(input_path), 'debug')
    # safe_makedirs(debug_dir)
    # def debug_writer(shapely_geom, filename):
    #     with open(os.path.join(debug_dir, filename), 'w') as f:
    #         json.dump(export_geojson(shapely_geom), f)

    for inFeature in layer_in:
        counter += 1
        geom = wkb_load(inFeature.GetGeometryRef().ExportToWkb())
        # debug_writer(geom, '{}_A_ORIG.geojson'.format(counter))

        # First check. Just make sure this is a valid shape we can work with
        if geom.is_empty or geom.area < square_buff:
            # debug_writer(geom, '{}_C_BROKEN.geojson'.format(counter))
            continue

        pts += len(geom.exterior.coords)
        f_geom = geom
        # 1. Buffer out then back in by the same amount. TODO: THIS IS SUPER SLOW.
        f_geom = geom.buffer(buff_dist, resolution=1).buffer(-buff_dist, resolution=1)
        # debug_writer(f_geom, '{}_B_AFTER_BUFFER.geojson'.format(counter))
        # 2. Simply: for some reason area isn't calculated on inner rings so we need to simplify them first
        f_geom = f_geom.simplify(buff_dist, preserve_topology=True)
        # 3. Remove small holes: Do we have donuts? Filter anythign smaller than a certain area
        f_geom = remove_holes(f_geom, min_hole_sq_deg)

        # Second check here for validity after simplification
        if not f_geom.is_empty and f_geom.is_valid and f_geom.area > 0:
            geoms.append(f_geom)
            # debug_writer(f_geom, '{}_Z_FINAL.geojson'.format(counter))
            log.debug('simplified: pts: {} ==> {}, rings: {} ==> {}'.format(get_pts(geom), get_pts(f_geom), get_rings(geom), get_rings(f_geom)))
        else:
            log.warning('Invalid GEOM')
            # debug_writer(f_geom, '{}_Z_REJECTED.geojson'.format(counter))
        # print('loop')

    # 5. Now we can do unioning fairly cheaply
    log.info('Unioning {} geometries'.format(len(geoms)))
    new_geom = unary_union(geoms)

    log.info('Writing to disk')
    outFeature = ogr.Feature(featureDefn)

    outFeature.SetGeometry(ogr.CreateGeometryFromJson(json.dumps(mapping(new_geom))))
    layer_out.CreateFeature(outFeature)
    outFeature = None

    data_source_in = None
    data_source_out = None


def create_project(huc, output_dir):
    project_name = 'VBET for HUC {}'.format(huc)
    project = RSProject(cfg, output_dir)
    project.create(project_name, 'VBET')

    project.add_metadata({
        'HUC{}'.format(len(huc)): str(huc),
        'HUC': str(huc),
        'VBETVersion': cfg.version,
        'VBETTimestamp': str(int(time.time()))
    })

    realizations = project.XMLBuilder.add_sub_element(project.XMLBuilder.root, 'Realizations')
    realization = project.XMLBuilder.add_sub_element(realizations, 'VBET', None, {
        'id': 'VBET',
        'dateCreated': datetime.datetime.now().isoformat(),
        'guid': str(uuid.uuid1()),
        'productVersion': cfg.version
    })

    project.XMLBuilder.add_sub_element(realization, 'Name', project_name)
    proj_nodes = {
        'Inputs': project.XMLBuilder.add_sub_element(realization, 'Inputs'),
        'Intermediates': project.XMLBuilder.add_sub_element(realization, 'Intermediates'),
        'Outputs': project.XMLBuilder.add_sub_element(realization, 'Outputs')
    }

    # Make sure we have these folders
    proj_dir = os.path.dirname(project.xml_path)
    safe_makedirs(os.path.join(proj_dir, 'inputs'))
    safe_makedirs(os.path.join(proj_dir, 'intermediates'))
    safe_makedirs(os.path.join(proj_dir, 'outputs'))

    project.XMLBuilder.write()
    return project, realization, proj_nodes


def main():

    parser = argparse.ArgumentParser(
        description='Riverscapes VBET Tool',
        # epilog="This is an epilog"
    )
    parser.add_argument('huc', help='NHD flow line ShapeFile path', type=str)
    parser.add_argument('flowlines', help='NHD flow line ShapeFile path', type=str)
    parser.add_argument('flowareas', help='NHD flow areas ShapeFile path', type=str)
    parser.add_argument('slope', help='Slope raster path', type=str)
    parser.add_argument('hand', help='HAND raster path', type=str)
    parser.add_argument('hillshade', help='Hillshade raster path', type=str)
    parser.add_argument('output_dir', help='Folder where output VBET project will be created', type=str)
    parser.add_argument('--max_slope', help='Maximum slope to be considered', type=float, default=12)
    parser.add_argument('--max_hand', help='Maximum HAND to be considered', type=float, default=50)
    parser.add_argument('--min_hole_area', help='Minimum hole retained in valley bottom (sq m)', type=float, default=50000)
    parser.add_argument('--verbose', help='(optional) a little extra logging ', action='store_true', default=False)
    parser.add_argument('--meta', help='riverscapes project metadata as comma separated key=value pairs', type=str)

    args = dotenv.parse_args_env(parser)

    # make sure the output folder exists
    safe_makedirs(args.output_dir)

    # Initiate the log file
    log = Logger('VBET')
    log.setup(logPath=os.path.join(args.output_dir, 'vbet.log'), verbose=args.verbose)
    log.title('Riverscapes VBET For HUC: {}'.format(args.huc))

    meta = parse_metadata(args.meta)

    try:
        vbet(args.huc, args.flowlines, args.flowareas, args.slope, args.max_slope, args.hand, args.hillshade, args.max_hand, args.min_hole_area, args.output_dir, meta)

    except Exception as e:
        log.error(e)
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
