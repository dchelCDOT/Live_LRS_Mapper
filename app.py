# @title 🛣️ LRS Mapper: Streamlined Edition
# @markdown ### Instructions
# @markdown 1. Run this cell.
# @markdown 2. Upload your CSV.
# @markdown 3. Match your spreadsheet columns.
# @markdown 4. Click **Run Analysis**.

import ipywidgets as widgets
from IPython.display import display, clear_output, IFrame
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString, MultiLineString
from shapely.ops import substring
import folium
import requests
import io
import os
import shutil
import base64 
from zipfile import ZipFile
from google.colab import files

# --- CONSTANTS ---
ROUTE_SERVICE_URL = "https://services.arcgis.com/yzB9WM8W0BO3Ql7d/arcgis/rest/services/Routes_gdb/FeatureServer/0"
CALC_CRS = "EPSG:3857" # Web Mercator (Meters)
MAP_CRS = "EPSG:4326"  # WGS84 (Lat/Long)

# --- GLOBAL STATE ---
state = {
    'df': None,
    'routes': None,
    'results_pts': None,
    'results_lns': None,
    'errors': None,
    'zip_name': None,
    'gis_cols': []
}

# --- GIS UTILS ---
def get_layer_columns(service_url):
    params = {'where': '1=1', 'outFields': '*', 'f': 'json', 'resultRecordCount': 1}
    try:
        r = requests.get(f"{service_url}/query", params=params)
        data = r.json()
        if 'fields' in data: return [f['name'] for f in data['fields']]
        return []
    except: return []

def get_arcgis_features(service_url):
    all_features = []
    offset = 0
    while True:
        params = {
            'where': '1=1', 'outFields': '*', 'f': 'geojson',
            'resultOffset': offset, 'resultRecordCount': 2000
        }
        try:
            r = requests.get(f"{service_url}/query", params=params)
            data = r.json()
            if 'features' not in data or not data['features']: break
            all_features.extend(data['features'])
            offset += len(data['features'])
            if 'exceededTransferLimit' not in data or not data['exceededTransferLimit']: break
        except: break
            
    fc = {"type": "FeatureCollection", "features": all_features}
    gdf = gpd.GeoDataFrame.from_features(fc['features'])
    gdf.set_crs(MAP_CRS, inplace=True)
    return gdf

# --- UI LAYOUT ---
style = {'description_width': 'initial'}
layout_full = widgets.Layout(width='98%')

# 1. Upload
btn_upload = widgets.FileUpload(accept='.csv', multiple=False, description='1. Upload CSV')
out_log = widgets.Output()

# 2. Configuration
lbl_config = widgets.HTML("<h3>⚙️ Step 2: Configure</h3>")
dd_csv_route = widgets.Dropdown(description="Spreadsheet Route ID:", style=style, layout=layout_full)

# Note: GIS Route Dropdown & Unit Dropdown are REMOVED per request

rad_geom = widgets.RadioButtons(
    options=['Point', 'Line', 'Both'], value='Both',
    description='Feature Type:', style=style
)

dd_begin_m = widgets.Dropdown(description="Begin Milepost Col:", style=style, layout=layout_full)
dd_end_m = widgets.Dropdown(description="End Milepost Col:", style=style, layout=layout_full)

txt_filename = widgets.Text(value='LRS_Results', description='Output Name:', style=style, layout=layout_full)

btn_run = widgets.Button(
    description='Run Analysis', button_style='primary', 
    layout=widgets.Layout(width='100%', height='50px'), icon='play'
)

# 3. Results Area
out_map = widgets.Output()
btn_download = widgets.Button(
    description='Download Results (.zip)', button_style='success', 
    layout=widgets.Layout(width='100%', height='50px'), icon='download'
)
btn_download.layout.display = 'none' # Hidden initially

vbox_config = widgets.VBox([
    lbl_config,
    widgets.HTML("<b>Match IDs</b>"), dd_csv_route,
    widgets.HTML("<hr><b>Geometry Rules</b>"), rad_geom, dd_begin_m, dd_end_m,
    widgets.HTML("<hr><b>Export</b>"), txt_filename,
    widgets.HTML("<br>"), btn_run
])
vbox_config.layout.display = 'none'

# --- LOGIC ---

def on_upload(change):
    up_file = list(btn_upload.value.keys())[0]
    content = btn_upload.value[up_file]['content']
    
    with out_log:
        clear_output()
        print("⏳ Reading data and connecting to ArcGIS...")
        try:
            state['df'] = pd.read_csv(io.BytesIO(content))
            csv_cols = list(state['df'].columns)
            state['gis_cols'] = get_layer_columns(ROUTE_SERVICE_URL)
            
            dd_csv_route.options = csv_cols
            dd_begin_m.options = csv_cols
            dd_end_m.options = ['(None)'] + csv_cols
            
            # Smart Defaults
            for c in csv_cols:
                low = c.lower()
                if 'route' in low or 'id' in low: dd_csv_route.value = c
                if 'begin' in low or 'start' in low or 'from' in low: dd_begin_m.value = c
                if 'end' in low or 'to' in low: dd_end_m.value = c
                
            print(f"✅ Loaded {len(state['df'])} rows. Configure below.")
            vbox_config.layout.display = 'block'
            
        except Exception as e:
            print(f"❌ Error: {e}")

def run_analysis(b):
    # Clear map explicitly before starting new run to avoid flickering/stacking
    out_map.clear_output()
    btn_download.layout.display = 'none'
    
    with out_log:
        clear_output()
        print("⏳ Fetching Routes & Calculating... (Please wait)")
        
    if state['routes'] is None:
        raw_routes = get_arcgis_features(ROUTE_SERVICE_URL)
        if raw_routes is None:
            with out_log: print("❌ Failed to load routes."); return
        state['routes'] = raw_routes.to_crs(CALC_CRS)
    
    routes = state['routes']
    df = state['df']
    
    # User Settings
    csv_rid = dd_csv_route.value
    bm_col = dd_begin_m.value
    em_col = dd_end_m.value
    mode = rad_geom.value.lower()
    
    # --- AUTO-SETTINGS (HIDDEN) ---
    unit_factor = 1609.34 # Hardcoded to Miles
    
    # Auto-find "ROUTE" column in GIS
    gis_rid = None
    possible_names = ['ROUTE', 'Route', 'route', 'RouteID', 'Route_ID']
    
    # 1. Try exact matches from our priority list
    for name in possible_names:
        if name in routes.columns:
            gis_rid = name
            break
            
    # 2. If not found, try case-insensitive match
    if not gis_rid:
        for col in routes.columns:
            if col.upper() == 'ROUTE':
                gis_rid = col
                break
    
    # 3. Fallback
    if not gis_rid:
        gis_rid = routes.columns[0] # Default to first column if "ROUTE" is missing
        print(f"⚠️ Warning: Could not find 'ROUTE' column. Using '{gis_rid}' instead.")
    else:
        print(f"ℹ️ Auto-selected GIS Route Column: {gis_rid}")

    # Processing
    routes[gis_rid] = routes[gis_rid].astype(str)
    df[csv_rid] = df[csv_rid].astype(str)
    
    valid_pts, valid_lns, errors = [], [], []
    
    print(f"Processing {len(df)} rows...")
    
    for idx, row in df.iterrows():
        rid = row[csv_rid]
        match = routes[routes[gis_rid] == rid]
        
        if match.empty:
            errors.append({**row, "Error": "Route Not Found"})
            continue
            
        geom_meters = match.iloc[0].geometry
        
        try:
            bm_val = float(row[bm_col])
        except:
            errors.append({**row, "Error": "Invalid Begin Measure"})
            continue
            
        bm_meters = bm_val * unit_factor
        
        is_point = False
        if mode == 'point': is_point = True
        elif mode == 'line': is_point = False
        else:
            if em_col == '(None)' or pd.isna(row.get(em_col)): is_point = True
            
        if is_point:
            try:
                pt_geom = geom_meters.interpolate(bm_meters)
                res = row.copy()
                res['geometry'] = pt_geom
                valid_pts.append(res)
            except Exception as e:
                errors.append({**row, "Error": str(e)})
        else:
            try:
                em_val = float(row[em_col])
                em_meters = em_val * unit_factor
                
                if bm_meters >= em_meters:
                    msg = "End < Begin."
                    if bm_meters == em_meters: msg = "End == Begin (Use Point mode)."
                    errors.append({**row, "Error": msg})
                    continue
                
                ln_geom = substring(geom_meters, bm_meters, em_meters)
                
                if ln_geom.is_empty:
                    errors.append({**row, "Error": "Result is empty"})
                elif ln_geom.geom_type in ['Point', 'MultiPoint']:
                     errors.append({**row, "Error": "Geometry collapsed to Point"})
                else:
                    res = row.copy()
                    res['geometry'] = ln_geom
                    valid_lns.append(res)
                    
            except Exception as e:
                errors.append({**row, "Error": str(e)})

    state['results_pts'] = valid_pts
    state['results_lns'] = valid_lns
    state['errors'] = errors
    state['out_name'] = txt_filename.value.replace(" ", "_")
    
    with out_log:
        print("✅ Analysis Complete.")
        print(f"   Points Mapped: {len(valid_pts)}")
        print(f"   Lines Mapped:  {len(valid_lns)}")
        print(f"   Errors:        {len(errors)}")
        
    generate_map()
    btn_download.layout.display = 'block'

def generate_map():
    # We clear the output inside the function to ensure the frame is fresh
    with out_map:
        clear_output(wait=True)
        m = folium.Map(location=[39.0, -105.5], zoom_start=7)
        
        def add_layer(data_list, name, color):
            if not data_list: return
            gdf_m = gpd.GeoDataFrame(data_list, crs=CALC_CRS)
            gdf_geo = gdf_m.to_crs(MAP_CRS)
            cols = [c for c in gdf_geo.columns if c != 'geometry']
            
            folium.GeoJson(
                gdf_geo,
                name=name,
                style_function=lambda x: {'color': color, 'weight': 5, 'opacity': 0.8},
                popup=folium.GeoJsonPopup(fields=cols)
            ).add_to(m)

        add_layer(state['results_lns'], "Mapped Lines", "blue")
        add_layer(state['results_pts'], "Mapped Points", "red")
        
        folium.LayerControl().add_to(m)
        
        # Save and Encode
        m.save("map_preview.html")
        html_data = open('map_preview.html', 'r').read()
        encoded = base64.b64encode(html_data.encode()).decode()
        
        # Display IFrame
        display(IFrame(f"data:text/html;base64,{encoded}", width="100%", height="600px"))

def download_results(b):
    out_name = state['out_name']
    zip_name = f"{out_name}.zip"
    
    with ZipFile(zip_name, 'w') as zipf:
        if state['errors']:
            pd.DataFrame(state['errors']).to_csv("Error_Report.csv", index=False)
            zipf.write("Error_Report.csv")
            
        if state['results_lns']:
            gdf = gpd.GeoDataFrame(state['results_lns'], crs=CALC_CRS)
            gdf.to_crs(MAP_CRS).to_file(f"{out_name}_Lines.shp")
            for f in os.listdir('.'):
                if f.startswith(f"{out_name}_Lines"): zipf.write(f)
                
        if state['results_pts']:
            gdf = gpd.GeoDataFrame(state['results_pts'], crs=CALC_CRS)
            gdf.to_crs(MAP_CRS).to_file(f"{out_name}_Points.shp")
            for f in os.listdir('.'):
                if f.startswith(f"{out_name}_Points"): zipf.write(f)
                
    files.download(zip_name)

# --- BINDINGS ---
btn_upload.observe(on_upload, names='value')
btn_run.on_click(run_analysis)
btn_download.on_click(download_results)

# --- DISPLAY ---
display(widgets.VBox([btn_upload, out_log, vbox_config, out_map, btn_download]))
