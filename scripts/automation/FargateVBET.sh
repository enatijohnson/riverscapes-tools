#!/bin/bash
set -eu
IFS=$'\n\t'

# These environment variables need to be present before the script starts
(: "${HUC?}")
(: "${PROGRAM?}")
(: "${RS_CONFIG?}")
(: "${RSCONTEXT_TAGS?}")
(: "${VBET_TAGS?}")
(: "${CHANNEL_TAGS}")
(: "${TAUDEM_TAGS}")

echo "$RS_CONFIG" > /root/.riverscapes

cat<<EOF

    ▄   ███   ▄███▄     ▄▄▄▄▀   
     █  █  █  █▀   ▀ ▀▀▀ █      
█     █ █ ▀ ▄ ██▄▄       █      
 █    █ █  ▄▀ █▄   ▄▀   █       
  █  █  ███   ▀███▀    ▀        
   █▐                           
   ▐                            

EOF

echo "HUC: $HUC"
echo "PROGRAM: $PROGRAM"
echo "RSCONTEXT_TAGS: $RSCONTEXT_TAGS"
echo "VBET_TAGS: $VBET_TAGS"
echo "CHANNEL_TAGS: $CHANNEL_TAGS"
echo "TAUDEM_TAGS: $TAUDEM_TAGS"

# Drop into our venv immediately
source /usr/local/venv/bin/activate

# Install latest pip dependencies
pip --timeout=120 install -r /usr/local/requirements.txt


echo "======================  GDAL Version ======================="
gdal-config --version

# Define some folders that we can easily clean up later
DATA_DIR=/usr/local/data
RS_CONTEXT_DIR=$DATA_DIR/rs_context/$HUC
CHANNEL_AREA_DIR=$DATA_DIR/channel_area/$HUC
TAUDEM_DIR=$DATA_DIR/taudem/$HUC
VBET_DIR=$DATA_DIR/output

##########################################################################################
# First Get RS_Context, ChannelArea and Taudem inputs
##########################################################################################

# Get the RSCli project we need to make this happen
rscli download $RS_CONTEXT_DIR --type "RSContext" --meta "huc=$HUC" \
  --file-filter "(hillshade|slope|dem|hydrology|project_bounds.geojson)" \
  --tags "$RSCONTEXT_TAGS" --no-input --verbose --program "$PROGRAM"

rscli download $CHANNEL_AREA_DIR --type "ChannelArea" --meta "huc=$HUC" \
  --tags "$CHANNEL_TAGS" --no-input --verbose --program "$PROGRAM"

rscli download $TAUDEM_DIR --type "TauDEM" --meta "huc=$HUC" \
  --file-filter "(twi.tif|pitfill.tif|dinfflowdir_ang.tif|dinfflowdir_slp.tif)" \
  --tags "$TAUDEM_TAGS" --no-input --verbose --program "$PROGRAM"

##########################################################################################
# Now Run VBET
##########################################################################################
try() {

vbet $HUC \
  "APRIL_2022" \
  $RS_CONTEXT_DIR/hydrology/hydrology.gpkg/network \
  $RS_CONTEXT_DIR/topography/dem.tif \
  $RS_CONTEXT_DIR/topography/slope.tif \
  $RS_CONTEXT_DIR/topography/hillshade.tif \
  $RS_CONTEXT_DIR/hydrology/NHDPlusCatchment.shp \
  $CHANNEL_AREA_DIR/outputs/channel_area.gpkg/channel_area \
  $RS_CONTEXT_DIR/hydrology/nhd_data.sqlite/NHDPlusFlowlineVAA \
  $VBET_DIR \
  --pitfill {env:DATA_ROOT}/taudem/${input:HUC}/intermediates/pitfill.tif \
  --dinfflowdir_ang {env:DATA_ROOT}/taudem/${input:HUC}/intermediates/dinfflowdir_ang.tif \
  --dinfflowdir_slp {env:DATA_ROOT}/taudem/${input:HUC}/outputs/dinfflowdir_slp.tif \
  --twi_raster $TAUDEM_DIR/outputs/twi.tif \
  --reach_codes 33400,46000,46003,46006,46007,55800 \
  --meta "Runner=Cybercastor" \
  --verbose
if [[ $? != 0 ]]; then return 1; fi

cd /usr/local/src/riverscapes-tools/packages/vbet
/usr/local/venv/bin/python -m vbet.vbet_rs \
  $VBET_DIR/project.rs.xml \
  $RS_CONTEXT_DIR/project.rs.xml,$TAUDEM_DIR/project.rs.xml,$CHANNEL_AREA_DIR/project.rs.xml

echo "======================  Final Disk space usage ======================="
df -h

echo "======================  Upload to the warehouse ======================="

# Upload the HUC into the warehouse
cd $VBET_DIR
rscli upload . --replace --tags "$VBET_TAGS" --no-input --verbose --program "$PROGRAM"
if [[ $? != 0 ]]; then return 1; fi


echo "<<PROCESS COMPLETE>>"


}
try || {
  # Emergency Cleanup
  echo "<<RS CONTEXT PROCESS ENDED WITH AN ERROR>>"
  exit 1
}
