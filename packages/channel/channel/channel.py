"""Name:     Channel Area Tool

   Purpose:  Create a new RS project that generates bankfull and merges with flowareas/waterbody to create channel polygons

   Author:   Kelly Whitehead

   Date:     July 14, 2021"""

import argparse
import os
import sys
import traceback
import time
import json
from typing import List, Dict
from venv import create

from osgeo import ogr
from rscommons.classes.rs_project import RSMeta, RSMetaTypes
from rscommons.classes.vector_base import get_utm_zone_epsg

from rscommons.util import safe_makedirs, parse_metadata, pretty_duration
from rscommons import RSProject, RSLayer, ModelConfig, Logger, dotenv, initGDALOGRErrors
from rscommons import GeopackageLayer, VectorBase, get_shp_or_gpkg
from rscommons.math import safe_eval
from rscommons.raster_buffer_stats import raster_buffer_stats2
from rscommons.vector_ops import get_geometry_unary_union, buffer_by_field, copy_feature_class, merge_feature_classes, remove_holes_feature_class, difference
from rscommons.vbet_network import vbet_network

from channel.channel_report import ChannelReport
from channel.__version__ import __version__

Path = str

DEFAULT_FUNCTION = "0.177 * (a ** 0.397) * (p ** 0.453)"
DEFAULT_FUNCTION_PARAMS = "a=TotDASqKm"

initGDALOGRErrors()

cfg = ModelConfig('https://xml.riverscapes.net/Projects/XSD/V2/RiverscapesProject.xsd', __version__)

LYR_DESCRIPTIONS_JSON = os.path.join(os.path.dirname(__file__), 'layer_descriptions.json')
LayerTypes = {
    'INPUTS': RSLayer('Inputs', 'INPUTS', 'Geopackage', 'inputs/inputs.gpkg', {
        'FLOWLINES': RSLayer('NHD Flowlines', 'NHDFlowline', 'Vector', 'flowlines'),
        # 'FLOWAREAS': RSLayer('NHD Flow Areas', 'FLOWAREAS', 'Vector', 'flowareas'),
        # 'WATERBODY': RSLayer('NHD Water Body Areas', 'WATER_BODIES', 'Vector', 'waterbody'),
        # 'OTHER_POLYGONS': RSLayer('Other Custom channel Polygons', "CUSTOM_POLYGONS", 'Vector', 'other_channels')
    }),
    'INTERMEDIATES': RSLayer('Intermediates', 'Intermediates', 'Geopackage', 'intermediates/intermediates.gpkg', {
        'FILTERED_WATERBODY': RSLayer('NHD Waterbodies (Filtered)', 'FILTERED_WATERBODY', 'Vector', 'waterbody_filtered'),
        'FILTERED_FLOWAREAS': RSLayer('NHD Flow Areas (Filtered)', 'FILTERED_FLOWAREAS', 'Vector', 'flowarea_filtered'),
        # 'FLOW_AREA_NO_ISLANDS': RSLayer('Flow Areas No Islands', 'FLOW_AREA_NO_ISLANDS', 'Vector', 'flowarea_no_islands'),
        'COMBINED_FA_WB': RSLayer('Combined Flow Area and Waterbody', 'COMBINED_FA_WB', 'Vector', 'combined_fa_wb'),
        'BANKFULL_NETWORK': RSLayer('Bankfull Network', 'BANKFULL_NETWORK', 'Vector', 'bankfull_network'),
        'BANKFULL_POLYGONS': RSLayer('Bankfull Polygons', 'BANKFULL_POLYGONS', 'Vector', 'bankfull_polygons'),
        'DIFFERENCE_POLYGONS': RSLayer('Difference Polygons', 'DIFFERENCE_POLYGONS', 'Vector', 'difference_polygons'),
    }),
    'OUTPUTS': RSLayer('Outputs', 'OUTPUTS', 'Geopackage', 'outputs/channel_area.gpkg', {
        'CHANNEL_AREA': RSLayer('Channel Area Polygons', 'CHANNEL_AREA', 'Vector', 'channel_area'),
    }),
    'REPORT': RSLayer('RSContext Report', 'REPORT', 'HTMLFile', 'outputs/channel_area.html')
}


def channel(huc: int,
            flowlines: Path,
            flowareas: Path,
            waterbodies: Path,
            bankfull_function: str,
            bankfull_function_params: dict,
            project_folder: Path,
            reach_code_field: str = None,
            reach_codes: Dict[str, List[str]] = None,
            epsg: int = cfg.OUTPUT_EPSG,
            meta: Dict[str, str] = None,
            other_polygons: Path = None,
            bankfull_field: str = None):
    """Create a new RS project that generates bankfull and merges with flowareas/waterbody to create channel polygons

    Args:
        huc (int): NHD huc id
        flowlines (Path): NHD flowlines or other stream line network
        flowareas (Path): NHD flowareas or other stream polygon areas
        waterbodies (Path): NHD waterbodies or other water polygon areas
        bankfull_function (str): equation to generate bankfull
        bankfull_function_params (dict): dict with entry for each bankfull equation param as value or fieldname
        project_folder (Path): location to save output project
        reach_code_field (str, optional): field to read for reach code filter for flowlines, flowareas and waterbodies, Defaults to None.
        reach_codes (Dict[str, List[str]], optional): dict entry for flowline, flowarea and waterbody and associated reach codes. Defaults to None.
        epsg ([int], optional): epsg spatial reference value. Defaults to cfg.OUTPUT_EPSG.
        meta (Dict[str, str], optional): metadata key-value pairs. Defaults to None.
    """

    timer = time.time()
    log = Logger('ChannelAreaTool')
    log.info('Starting Channel Area Tool v.{}'.format(cfg.version))
    log.info('Using Equation: "{}" and params: "{}"'.format(bankfull_function, bankfull_function_params))

    # Add the layer metadata immediately before we write anything
    augment_layermeta()

    meta['BankfullEquation'] = bankfull_function
    for param, value in bankfull_function_params.items():
        meta[f'BankfullParameter:{param}'] = str(value)
    for layer, codes in reach_codes.items():
        meta[f'{layer}ReachCodes'] = str(codes)

    project_name = 'Channel Area for HUC {}'.format(huc)
    project = RSProject(cfg, project_folder)
    project.create(project_name, 'ChannelArea', [
        RSMeta('HUC{}'.format(len(huc)), str(huc)),
        RSMeta('HUC', str(huc)),
        RSMeta('ChannelAreaVersion', cfg.version),
        RSMeta('ChannelAreaTimestamp', str(int(time.time())), RSMetaTypes.TIMESTAMP)
    ], meta)

    _realization, proj_nodes = project.add_realization(project_name, 'REALIZATION1', cfg.version, data_nodes=['Inputs', 'Intermediates', 'Outputs'], create_folders=True)

    # Input Preparation
    inputs_gpkg_path = os.path.join(project_folder, LayerTypes['INPUTS'].rel_path)
    intermediates_gpkg_path = os.path.join(project_folder, LayerTypes['INTERMEDIATES'].rel_path)
    output_gpkg_path = os.path.join(project_folder, LayerTypes['OUTPUTS'].rel_path)

    GeopackageLayer.delete(inputs_gpkg_path)
    GeopackageLayer.delete(intermediates_gpkg_path)

    if flowlines is not None:
        proj_flowlines = os.path.join(inputs_gpkg_path, LayerTypes['INPUTS'].sub_layers['FLOWLINES'].rel_path)
        copy_feature_class(flowlines, proj_flowlines, epsg=epsg)
    else:
        proj_flowlines = None

    if flowareas is not None:
        LayerTypes['INPUTS'].add_sub_layer('FLOWAREAS', RSLayer('NHD Flow Areas', 'NHDArea', 'Vector', 'flowareas'))
        proj_flowareas = os.path.join(inputs_gpkg_path, LayerTypes['INPUTS'].sub_layers['FLOWAREAS'].rel_path)
        copy_feature_class(flowareas, proj_flowareas, epsg=epsg)
    else:
        proj_flowareas = None
        filtered_flowareas = None
        # filtered_flowarea_no_islands = None

    if waterbodies is not None:
        LayerTypes['INPUTS'].add_sub_layer('WATERBODY', RSLayer('NHD Water Body Areas', 'NHDWaterbody', 'Vector', 'waterbody'))
        proj_waterbodies = os.path.join(inputs_gpkg_path, LayerTypes['INPUTS'].sub_layers['WATERBODY'].rel_path)
        copy_feature_class(waterbodies, proj_waterbodies, epsg=epsg)
    else:
        proj_waterbodies = None
        filtered_waterbodies = None

    if other_polygons is not None:
        LayerTypes['INPUTS'].add_sub_layer('OTHER_POLYGONS', RSLayer('Other Custom channel Polygons', "CUSTOM_POLYGONS", 'Vector', 'other_channels'))
        proj_custom_polygons = os.path.join(inputs_gpkg_path, LayerTypes['INPUTS'].sub_layers['OTHER_POLYGONS'].rel_path)
        copy_feature_class(other_polygons, proj_custom_polygons, epsg=epsg)
    else:
        proj_custom_polygons = None

    project.add_project_geopackage(proj_nodes['Inputs'], LayerTypes['INPUTS'])

    # Generate Intermediates
    if proj_flowareas is not None:
        log.info('Filtering flowarea polygons')
        filtered_flowareas = os.path.join(intermediates_gpkg_path, LayerTypes['INTERMEDIATES'].sub_layers['FILTERED_FLOWAREAS'].rel_path)
        fcode_filter = ""
        if reach_code_field is not None and reach_codes['flowarea'] is not None:
            fcode_filter = f"{reach_code_field} = " + f" or {reach_code_field} = ".join([f"'{fcode}'" for fcode in reach_codes['flowarea']])
        copy_feature_class(proj_flowareas, filtered_flowareas, attribute_filter=fcode_filter)

        # log.info('Removing flowarea islands')
        # filtered_flowarea_no_islands = os.path.join(intermediates_gpkg_path, LayerTypes['INTERMEDIATES'].sub_layers['FLOW_AREA_NO_ISLANDS'].rel_path)
        # remove_holes_feature_class(filtered_flowareas, filtered_flowarea_no_islands, min_hole_area=500)

    if proj_waterbodies is not None:
        log.info('Filtering waterbody polygons')
        filtered_waterbodies = os.path.join(intermediates_gpkg_path, LayerTypes['INTERMEDIATES'].sub_layers['FILTERED_WATERBODY'].rel_path)
        fcode_filter = ""
        if reach_code_field is not None and reach_codes['waterbody'] is not None:
            fcode_filter = f"{reach_code_field} = " + f" or {reach_code_field} = ".join([f"'{fcode}'" for fcode in reach_codes['waterbody']])

        copy_feature_class(proj_waterbodies, filtered_waterbodies, attribute_filter=fcode_filter)

    combined_flow_polygons = os.path.join(intermediates_gpkg_path, LayerTypes['INTERMEDIATES'].sub_layers['COMBINED_FA_WB'].rel_path)
    if filtered_waterbodies is not None and filtered_flowareas is not None:
        log.info('Merging waterbodies and flowareas')
        merge_feature_classes([filtered_waterbodies, filtered_flowareas], combined_flow_polygons)
    elif filtered_flowareas is not None:
        log.info('No waterbodies found, copying flowareas')
        copy_feature_class(filtered_flowareas, combined_flow_polygons)
    elif filtered_waterbodies is not None:
        log.info('No flowareas found, copying waterbodies')
        copy_feature_class(filtered_waterbodies, combined_flow_polygons)
    else:
        log.info('No waterbodies or flowareas in project')
        combined_flow_polygons = None

    bankfull_polygons = os.path.join(intermediates_gpkg_path, LayerTypes['INTERMEDIATES'].sub_layers['BANKFULL_POLYGONS'].rel_path)
    if proj_flowlines is not None:
        log.info('Filtering bankfull flowline network')
        bankfull_network = os.path.join(intermediates_gpkg_path, LayerTypes['INTERMEDIATES'].sub_layers['BANKFULL_NETWORK'].rel_path)
        if reach_code_field is not None and reach_codes['flowline'] is not None:
            vbet_network(proj_flowlines, None, bankfull_network, epsg, reach_codes['flowline'], reach_code_field, flow_areas_path_exclude=None)
        else:
            copy_feature_class(proj_flowlines, bankfull_network)

        if bankfull_field is not None:
            buffer_by_field(bankfull_network, bankfull_polygons, bankfull_field, epsg=epsg, centered=True)
        elif bankfull_function is not None:
            log.info("Calculing bankfull width")
            calculate_bankfull(bankfull_network, 'bankfull_m', bankfull_function, bankfull_function_params)
            buffer_by_field(bankfull_network, bankfull_polygons, "bankfull_m", epsg=epsg, centered=True)
        else:
            log.info("No field or equation for bankfull width was provided")
            bankfull_polygons = None
    else:
        bankfull_polygons = None

    output_channel_area = os.path.join(output_gpkg_path, LayerTypes['OUTPUTS'].sub_layers['CHANNEL_AREA'].rel_path)
    if bankfull_polygons is not None and combined_flow_polygons is not None:
        log.info('Combining Bankfull polygons with flowarea/waterbody polygons into final channel area output')
        channel_polygons = os.path.join(intermediates_gpkg_path, LayerTypes['INTERMEDIATES'].sub_layers['DIFFERENCE_POLYGONS'].rel_path)
        difference(combined_flow_polygons, bankfull_polygons, channel_polygons)
        merge_feature_classes([channel_polygons, combined_flow_polygons], output_channel_area)
    elif bankfull_polygons is not None:
        log.info('Copying Bankfull polygons into final channel area output')
        copy_feature_class(bankfull_polygons, output_channel_area)
    elif combined_flow_polygons is not None:
        log.info('Copying filtered flowarea/waterbody polygons into final channel area output')
        copy_feature_class(combined_flow_polygons, output_channel_area)
    elif proj_custom_polygons is not None:
        log.info('Copying custom polygons into final channel area output')
        copy_feature_class(proj_custom_polygons, output_channel_area)
    else:
        log.warning('No output channel polygons were produced')

    # Now add our Geopackages to the project XML
    project.add_project_geopackage(proj_nodes['Intermediates'], LayerTypes['INTERMEDIATES'])
    project.add_project_geopackage(proj_nodes['Outputs'], LayerTypes['OUTPUTS'])

    # Processing time in hours
    ellapsed_time = time.time() - timer
    project.add_metadata([
        RSMeta("ProcTimeS", "{:.2f}".format(ellapsed_time), RSMetaTypes.HIDDEN),
        RSMeta("ProcessingTime", pretty_duration(ellapsed_time))
    ])

    # Report
    report_path = os.path.join(project.project_dir, LayerTypes['REPORT'].rel_path)
    project.add_report(proj_nodes['Outputs'], LayerTypes['REPORT'], replace=True)
    # report = ChannelReport(report_path, project)
    # report.write()

    log.info('Channel Area Completed Successfully')


def calculate_bankfull(network_layer: Path, out_field: str, eval_fn: str, function_params: dict):
    """caluclate bankfull value for each feature in network layer

    Args:
        network_layer (Path): netowrk layer
        out_field (str): field to store bankfull values
        eval_fn (str): equation to use in eval function
        function_params (dict): parameters to use in eval function
    """
    with GeopackageLayer(network_layer, write=True) as layer:

        layer.create_field(out_field, ogr.OFTReal)

        layer.ogr_layer.StartTransaction()
        for feat, *_ in layer.iterate_features("Calculating bankfull"):

            fn_params = {}
            for param, value in function_params.items():
                if isinstance(value, str):
                    field_value = feat.GetField(value)
                    fn_params[param] = field_value if field_value is not None else 0
                else:
                    fn_params[param] = value
            # eval seems to mutate the fn_params object so we pass in a copy so that we can report on the errors if needed
            result = safe_eval(eval_fn, fn_params)
            feat.SetField(out_field, result)
            layer.ogr_layer.SetFeature(feat)

        layer.ogr_layer.CommitTransaction()


def augment_layermeta():
    """
    For RSContext we've written a JSON file with extra layer meta. We may use this pattern elsewhere but it's just here for now
    """
    with open(LYR_DESCRIPTIONS_JSON, 'r') as f:
        json_data = json.load(f)

    for k, lyr in LayerTypes.items():
        if lyr.sub_layers is not None:
            for h, sublyr in lyr.sub_layers.items():
                if h in json_data and len(json_data[h]) > 0:
                    sublyr.lyr_meta = [
                        RSMeta('Description', json_data[h][0]),
                        RSMeta('SourceUrl', json_data[h][1], RSMetaTypes.URL),
                        RSMeta('DataProductVersion', json_data[h][2]),
                        RSMeta('DocsUrl', 'https://tools.riverscapes.net/channel/data.html#{}'.format(sublyr.id), RSMetaTypes.URL)
                    ]
        if k in json_data and len(json_data[k]) > 0:
            lyr.lyr_meta = [
                RSMeta('Description', json_data[k][0]),
                RSMeta('SourceUrl', json_data[k][1], RSMetaTypes.URL),
                RSMeta('DataProductVersion', json_data[k][2]),
                RSMeta('DocsUrl', 'https://tools.riverscapes.net/channel/data.html#{}'.format(lyr.id), RSMetaTypes.URL)
            ]


def main():
    """Create a new RS project that generates bankfull and merges with flowareas/waterbody to create channel polygons
    """
    parser = argparse.ArgumentParser(
        description='Riverscapes Channel Area Tool',
        # epilog="This is an epilog"
    )
    parser.add_argument('huc', help='NHD huc id', type=str)
    parser.add_argument('flowlines', help='NHD flowlines feature class', type=str)
    parser.add_argument('output_dir', help='Folder where output VBET project will be created', type=str)
    parser.add_argument('--flowareas', help='NHD flowareas feature class', type=str)
    parser.add_argument('--waterbodies', help='NHD waterbodies', type=str)
    parser.add_argument('--bankfull_function', help='width field in flowlines feature class (e.g. BFWidth). Default: "{}"'.format(DEFAULT_FUNCTION), type=str, default=DEFAULT_FUNCTION)
    parser.add_argument('--bankfull_function_params', help='Field that contains reach code (e.g. FCode). Omitting this option retains all features. Default: "{}"'.format(DEFAULT_FUNCTION_PARAMS), type=str, default=DEFAULT_FUNCTION_PARAMS)
    parser.add_argument('--reach_code_field', help='Field that contains reach code (e.g. FCode). Omitting this option retains all features.', type=str)
    parser.add_argument('--flowline_reach_codes', help='Comma delimited reach codes (FCode) to retain when filtering features. Omitting this option retains all features.', type=str)
    parser.add_argument('--flowarea_reach_codes', help='Comma delimited reach codes (FCode) to retain when filtering features. Omitting this option retains all features.', type=str)
    parser.add_argument('--waterbody_reach_codes', help='Comma delimited reach codes (FCode) to retain when filtering features. Omitting this option retains all features.', type=str)
    parser.add_argument('--precip', help='mean annual precipiation in cm')
    parser.add_argument('--prism_data')
    parser.add_argument('--huc8boundary')
    parser.add_argument('--other_polygons')
    parser.add_argument('--bankfull_field')
    parser.add_argument('--epsg', help='output epsg', type=int)
    parser.add_argument('--meta', help='riverscapes project metadata as comma separated key=value pairs', type=str)
    parser.add_argument('--verbose', help='(optional) a little extra logging ', action='store_true', default=False)
    parser.add_argument('--debug', help='Add debug tools for tracing things like memory usage at a performance cost.', action='store_true', default=False)

    args = dotenv.parse_args_env(parser)

    # make sure the output folder exists
    safe_makedirs(args.output_dir)

    # Initiate the log file
    log = Logger('Channel Area')
    log.setup(logPath=os.path.join(args.output_dir, 'channel_area.log'), verbose=args.verbose)
    log.title('Riverscapes Channel Area For HUC: {}'.format(args.huc))

    meta = parse_metadata(args.meta)
    bankfull_params = parse_metadata(args.bankfull_function_params)

    reach_codes = {}
    reach_codes['flowline'] = args.flowline_reach_codes.split(',') if args.flowline_reach_codes else None
    reach_codes['flowarea'] = args.flowarea_reach_codes.split(',') if args.flowarea_reach_codes else None
    reach_codes['waterbody'] = args.waterbody_reach_codes.split(',') if args.waterbody_reach_codes else None

    if args.precip is not None:
        precip = args.precip
    elif args.prism_data is not None and args.huc8boundary is not None:
        polygon = get_geometry_unary_union(args.huc8boundary)
        precip = raster_buffer_stats2({1: polygon}, args.prism_data)[1]['Mean'] / 10
        log.info('Mean annual precipitation for HUC {} is {} cm'.format(args.huc, precip))

    else:
        raise ValueError('precip or prism_data and huc8boundary not provided.')

    bankfull_params['p'] = precip
    epsg = int(args.epsg) if args.epsg is not None else cfg.OUTPUT_EPSG

    try:
        if args.debug is True:
            from rscommons.debug import ThreadRun
            memfile = os.path.join(args.output_dir, 'vbet_mem.log')
            retcode, max_obj = ThreadRun(channel, memfile, args.huc, args.flowlines, args.flowareas, args.waterbodies, args.bankfull_function, bankfull_params, args.output_dir, args.reach_code_field, reach_codes, epsg=epsg, meta=meta, other_polygons=args.other_polygons, bankfull_field=args.bankfull_field)
            log.debug('Return code: {}, [Max process usage] {}'.format(retcode, max_obj))

        else:
            channel(args.huc, args.flowlines, args.flowareas, args.waterbodies, args.bankfull_function, bankfull_params, args.output_dir, args.reach_code_field, reach_codes, epsg=epsg, meta=meta, other_polygons=args.other_polygons, bankfull_field=args.bankfull_field)

    except Exception as e:
        log.error(e)
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
