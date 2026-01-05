import streamlit as st
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString, MultiLineString
from shapely.ops import substring, linemerge
import folium
from folium import JsCode
from streamlit_folium import st_folium
import requests
import io
import os
import shutil
import numpy as np 
from zipfile import ZipFile

# --- ARCGIS LIBRARY CHECK & PATCH ---
try:
    from arcgis.gis import GIS
    from arcgis.features import FeatureLayerCollection
    import arcgis.features.geo
    
    # --- MONKEY PATCH FOR 'is_geoenabled' ERROR ---
    if not hasattr(arcgis.features.geo, '_is_geoenabled'):
        def _is_geoenabled(df):
            return hasattr(df, 'spatial')
        arcgis.features.geo._is_geoenabled = _is_geoenabled
        
    ARCGIS_AVAILABLE = True
except ImportError:
    ARCGIS_AVAILABLE = False
except Exception:
    ARCGIS_AVAILABLE = True 

# --- PAGE CONFIG ---
st.set_page_config(
    page_title="CDOT Route and Reference Mapper Developed with Gemini",
    page_icon="🛣️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- BRANDING CONSTANTS ---
CDOT_BLUE = "#004899"
CDOT_GREEN = "#245436"
CDOT_LOGO_URL = "https://www.codot.gov/assets/sitelogo.png"
REF_SHEET_URL = "https://docs.google.com/spreadsheets/d/1XGOEZdtynaX3ox40GBD9SqhP1VlM69HSxIVsagxa3O8/export?format=csv"

# --- CUSTOM CSS ---
st.markdown(f"""
    <style>
        h1, h2, h3 {{ color: {CDOT_BLUE} !important; }}
        .cdot-banner {{
            background-color: white; padding: 20px;
            border-bottom: 5px solid {CDOT_GREEN}; border-radius: 10px;
            display: flex; align-items: center; margin-bottom: 20px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .cdot-banner img {{ height: 60px; margin-right: 25px; }}
        .cdot-banner h1 {{
            color: {CDOT_BLUE} !important; margin: 0; padding: 0;
            font-size: 2.5rem; font-weight: 800;
        }}
        div.stButton > button:first-child {{
            background-color: {CDOT_GREEN}; color: white;
            border: none; font-weight: bold; font-size: 1.1rem;
        }}
        div.stButton > button:first-child:hover {{
            background-color: {CDOT_BLUE}; border: 2px solid {CDOT_GREEN};
        }}
        .warning-box {{
            background-color: #fff3cd;
            border-left: 5px solid #ffc107;
            padding: 15px;
            margin-bottom: 15px;
            color: #856404;
        }}
    </style>
""", unsafe_allow_html=True)

# --- HEADER ---
st.markdown(f"""
    <div class="cdot-banner">
        <img src="{CDOT_LOGO_URL}" alt="CDOT Logo">
        <h1>CDOT Route and Reference Mapper</h1>
    </div>
""", unsafe_allow_html=True)

# --- INSTRUCTIONS ---
with st.expander("ℹ️ About this Tool & How to Use (Click to Expand)", expanded=True):
    col_about, col_how = st.columns(2)
    with col_about:
        st.subheader("What is this tool?")
        st.markdown("""
        This tool maps tabular data (spreadsheets with Mileposts) onto the official CDOT Route Network.
        
        **Features:**
        * **Smart Validation:** Checks MP limits against the official CDOT Reference table.
        * **Offset Correction:** Correctly handles routes that do not start at MP 0.
        * **Resampling Engine:** Generates valid lines even on broken/complex GIS topology.
        * **Portal Upload:** Publish/Overwrite layers to ArcGIS Online or GeoHub using GeoPackages.
        """)
    with col_how:
        st.subheader("How to use it")
        st.markdown("""
        1.  **Upload** your CSV or Excel file.
        2.  **Map Columns** (Route ID, Begin MP, End MP).
        3.  **Run Analysis** & Fix Errors.
        4.  **Download** results OR **Login** below to upload to GeoHub/AGOL.
        """)

st.divider()

# --- CONSTANTS ---
ROUTE_SERVICE_URL = "https://services.arcgis.com/yzB9WM8W0BO3Ql7d/arcgis/rest/services/Routes_gdb/FeatureServer/0"
CALC_CRS = "EPSG:3857" # Meters
MAP_CRS = "EPSG:4326"  # Lat/Long

# --- STATE MANAGEMENT ---
if 'success_pts' not in st.session_state: st.session_state['success_pts'] = []
if 'success_lns' not in st.session_state: st.session_state['success_lns'] = []
if 'error_df' not in st.session_state: st.session_state['error_df'] = None
if 'processed' not in st.session_state: st.session_state['processed'] = False
if 'gis' not in st.session_state: st.session_state['gis'] = None
if 'user_layers' not in st.session_state: st.session_state['user_layers'] = []
if 'user_folders' not in st.session_state: st.session_state['user_folders'] = []
if 'portal_url' not in st.session_state: st.session_state['portal_url'] = "https://maps.codot.gov/portal/"

# --- UTILS ---
@st.cache_data
def get_arcgis_features(service_url):
    all_features = []
    offset = 0
    with st.spinner("Fetching Official CDOT Route Network... (One time load)"):
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

@st.cache_data
def get_reference_data(url):
    try:
        df = pd.read_csv(url)
        df.columns = [c.strip().upper() for c in df.columns]
        
        rid_col = next((c for c in df.columns if 'ROUTE' in c), None)
        min_col = next((c for c in df.columns if 'MINIMUM' in c and 'EXTENT' in c), None)
        max_col = next((c for c in df.columns if 'MAXIMUM' in c and 'EXTENT' in c), None)
        
        if rid_col and min_col and max_col:
            ref_dict = {}
            for _, row in df.iterrows():
                try:
                    r_id = str(row[rid_col]).strip()
                    r_min = float(row[min_col])
                    r_max = float(row[max_col])
                    ref_dict[r_id] = {'min': r_min, 'max': r_max}
                except: continue
            return ref_dict
        return None
    except: return None

# --- EXPORT UTILS ---
def prep_geopackage_zip(data_list, layer_title):
    if not data_list: return None, []
    
    valid_data = [d for d in data_list if d.get('geometry') is not None]
    if not valid_data: return None, []
    
    gdf = gpd.GeoDataFrame(valid_data, crs=CALC_CRS).to_crs(MAP_CRS)
    for col in gdf.columns:
        if pd.api.types.is_datetime64_any_dtype(gdf[col]):
            gdf[col] = gdf[col].astype(str)
            
    temp_dir = f"/tmp/upload_{layer_title}"
    if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
    os.makedirs(temp_dir)
    
    gpkg_name = f"{layer_title}.gpkg"
    # Use 'w' to ensure fresh write
    gdf.to_file(os.path.join(temp_dir, gpkg_name), layer=layer_title, driver="GPKG", mode="w")
    
    zip_path = f"/tmp/{layer_title}.zip"
    with ZipFile(zip_path, 'w') as zipf:
        zipf.write(os.path.join(temp_dir, gpkg_name), gpkg_name)
                
    return zip_path, list(gdf.columns)

def handle_arcgis_upload(gis, zip_path, layer_title, folder_name, item_props, overwrite_item_id=None):
    try:
        # Publish Parameters to prevent Job Failed error
        publish_parameters = {
            "name": layer_title,
            "targetSR": {"wkid": 4326},
            "maxRecordCount": 2000,
            "enforceInputFileSizeLimit": True,
            "layerInfo": {"capabilities": "Query,Extract"}
        }

        if overwrite_item_id:
            # Overwrite Logic
            item = gis.content.get(overwrite_item_id)
            if not item: return "ERROR", "Target Item not found."
            
            flc = FeatureLayerCollection.fromitem(item)
            flc.manager.overwrite(zip_path)
            if item_props: item.update(item_properties=item_props)
            return "OVERWRITTEN", item.homepage
        else:
            # New Upload Logic
            # Check Folder logic: 'folder' arg in add() expects None for root, or string for folder
            target_folder = folder_name if (folder_name and folder_name.strip() != "") else None

            # Create folder if it doesn't exist? 
            # gis.content.add() usually fails if folder name is passed but doesn't exist.
            # We'll trust the dropdown logic, but if "Create New" was used, we must create it.
            if target_folder:
                folders = [f['title'] for f in gis.users.me.folders]
                if target_folder not in folders:
                    gis.content.create_folder(target_folder)

            # Check for existing name collision
            query = f"title:\"{layer_title}\" AND owner:\"{gis.users.me.username}\" AND type:\"Feature Service\""
            search_res = gis.content.search(query=query, max_items=1)
            if search_res: return "EXISTS", search_res[0].homepage

            final_props = {'type': 'GeoPackage', 'title': layer_title, 'tags': 'CDOT, LRS'}
            if item_props: final_props.update(item_props)
            
            # Add Item
            shp_item = gis.content.add(final_props, data=zip_path, folder=target_folder)
            
            # Publish
            feat_item = shp_item.publish(publish_parameters=publish_parameters)
            return "PUBLISHED", feat_item.homepage
    except Exception as e:
        return "ERROR", str(e)

def check_schema_match(gis, item_id, new_columns):
    try:
        item = gis.content.get(item_id)
        if not item or not item.layers: return "UNKNOWN", []
        existing_fields = [f['name'] for f in item.layers[0].properties.fields]
        missing_cols = [col for col in existing_fields if col not in new_columns and col.lower() not in ['fid', 'objectid', 'shape', 'globalid']]
        return "MATCH" if not missing_cols else "MISMATCH", missing_cols
    except:
        return "UNKNOWN", []

def process_batch(df_batch, routes, col_map, mode, ref_lookup=None):
    rid_col = col_map['rid']
    bm_col = col_map['bm']
    em_col = col_map['em']
    gis_rid = col_map['gis_rid']
    
    routes[gis_rid] = routes[gis_rid].astype(str)
    df_batch[rid_col] = df_batch[rid_col].astype(str)
    
    v_pts, v_lns, errs = [], [], []
    unit_factor = 1609.34
    
    for idx, row in df_batch.iterrows():
        try:
            rid = str(row[rid_col]).strip()
            
            route_min_mp = 0.0
            if ref_lookup and rid in ref_lookup:
                limits = ref_lookup[rid]
                route_min_mp = limits['min'] 
                
                try: bm_val = float(row[bm_col])
                except: raise ValueError(f"Invalid Begin Measure format: {row[bm_col]}")
                
                if bm_val < limits['min']:
                    raise ValueError(f"Begin MP ({bm_val}) is below Route {rid} Minimum ({limits['min']})")
                if bm_val > limits['max']:
                    raise ValueError(f"Begin MP ({bm_val}) exceeds Route {rid} Maximum ({limits['max']})")
                
                if mode != 'Point' and em_col != '(None)' and not pd.isna(row.get(em_col)):
                    try: em_val = float(row[em_col])
                    except: raise ValueError(f"Invalid End Measure format: {row.get(em_col)}")
                    
                    if em_val > limits['max']:
                         if em_val > (limits['max'] + 0.1): 
                             raise ValueError(f"End MP ({em_val}) exceeds Route {rid} Maximum ({limits['max']})")

            matches = routes[routes[gis_rid] == rid]
            if matches.empty:
                raise ValueError(f"Route ID '{rid}' Not Found in GIS Network")
            
            try: bm_val = float(row[bm_col])
            except: raise ValueError(f"Invalid Begin Measure: {row[bm_col]}")
            
            is_point = False
            if mode == 'Point': is_point = True
            elif mode == 'Line': is_point = False
            else:
                if em_col == '(None)' or pd.isna(row.get(em_col)): is_point = True
            
            final_geom = None
            
            for _, feat in matches.iterrows():
                geom = feat.geometry
                
                if geom.geom_type == 'MultiLineString':
                    merged = linemerge(geom)
                    if merged.geom_type in ['LineString', 'MultiLineString']:
                        geom = merged
                
                relative_bm = max(0.0, bm_val - route_min_mp)
                bm_meters = relative_bm * unit_factor
                
                if is_point:
                    if bm_meters <= geom.length:
                        final_geom = geom.interpolate(bm_meters)
                        break
                else:
                    try: em_val = float(row[em_col])
                    except: raise ValueError(f"Invalid End Measure: {row.get(em_col)}")
                    
                    relative_em = max(0.0, em_val - route_min_mp)
                    em_meters = relative_em * unit_factor
                    
                    if bm_meters >= em_meters:
                        if bm_meters == em_meters: raise ValueError("Begin MP == End MP (Use Point mode)")
                        else: raise ValueError(f"End MP ({em_val}) < Begin MP ({bm_val})")
                    
                    try:
                        candidate_ln = substring(geom, bm_meters, em_meters)
                        if not candidate_ln.is_empty and candidate_ln.geom_type in ['LineString', 'MultiLineString'] and candidate_ln.length > 0.1:
                            final_geom = candidate_ln
                            break 
                    except: pass
                    
                    if bm_meters < (geom.length + 50): 
                         actual_end_m = min(em_meters, geom.length)
                         actual_start_m = min(bm_meters, geom.length)
                         
                         if actual_end_m > actual_start_m:
                             segment_len = actual_end_m - actual_start_m
                             num_points = max(2, int(segment_len / 10))
                             distances = np.linspace(actual_start_m, actual_end_m, num=num_points)
                             points = [geom.interpolate(d) for d in distances]
                             
                             if points[0].distance(points[-1]) > 0.1:
                                 final_geom = LineString(points)
                                 break 

            if final_geom is None:
                 if is_point:
                     last_geom = matches.iloc[-1].geometry
                     if (bm_val - route_min_mp) * unit_factor > last_geom.length:
                         final_geom = last_geom.interpolate(last_geom.length)
                     else:
                         raise ValueError("Measure out of range of all found route segments.")
                 else:
                     raise ValueError("Could not generate geometry. Segment likely falls in a gap or outside GIS limits.")

            res = row.copy()
            res['geometry'] = final_geom
            if 'Error_Message' in res: del res['Error_Message']
            
            if is_point: v_pts.append(res)
            else: v_lns.append(res)

        except Exception as e:
            row['Error_Message'] = str(e)
            errs.append(row)
            
    return v_pts, v_lns, errs

# --- UI SECTION 1: UPLOAD ---
st.subheader("1. Data Upload")
uploaded_file = st.file_uploader("Upload Data (.csv or .xlsx)", type=["csv", "xlsx"])

df_main = None
if uploaded_file:
    try:
        if uploaded_file.name.endswith('.csv'):
            df_main = pd.read_csv(uploaded_file)
            default_out_name = os.path.splitext(uploaded_file.name)[0]
        else:
            xls = pd.ExcelFile(uploaded_file)
            df_main = pd.read_excel(uploaded_file, sheet_name=0)
            default_out_name = os.path.splitext(uploaded_file.name)[0]
    except Exception as e:
        st.error(f"Error reading input file: {e}")
        st.stop()

if df_main is not None:
    cols = list(df_main.columns)
    
    # --- UI SECTION 2: CONFIG ---
    st.divider()
    st.subheader("2. Configuration")
    
    c1, c2, c3 = st.columns(3)
    with c1:
        rid_col = st.selectbox("Route ID Column", cols, index=0)
    with c2:
        bm_col = st.selectbox("Begin MP Column", cols, index=1 if len(cols)>1 else 0)
    with c3:
        em_opts = ['(None)'] + cols
        em_col = st.selectbox("End MP Column (Optional)", em_opts, index=0)

    st.write("")
    cc1, cc2, cc3 = st.columns(3)
    with cc1:
        mode = st.radio("Feature Type", ["Point", "Line", "Both"], index=2)
    with cc2:
        out_name = st.text_input("Output Filename", default_out_name)
    with cc3:
        feature_color = st.color_picker("Feature Color", CDOT_GREEN)
        st.write("")
        ignore_errors = st.checkbox("Ignore rows with mapping errors to allow valid rows to map", value=True)

    before_len = len(df_main)
    df_main = df_main.dropna(subset=[rid_col, bm_col], how='any')
    if len(df_main) > 0:
        mask = df_main[rid_col].astype(str).str.strip() == ''
        df_main = df_main[~mask]
    
    st.divider()
    
    if st.button("🚀 Run Analysis", type="primary"):
        st.session_state['success_pts'] = []
        st.session_state['success_lns'] = []
        st.session_state['error_df'] = None
        
        raw_routes = get_arcgis_features(ROUTE_SERVICE_URL)
        if raw_routes is None: st.stop()
        routes = raw_routes.to_crs(CALC_CRS)
        
        with st.spinner("Loading Official Route Limits..."):
            ref_lookup = get_reference_data(REF_SHEET_URL)
        
        gis_rid = None
        for c in routes.columns:
            if c.upper() in ['ROUTE', 'ROUTEID', 'RTEID', 'ROUTE_ID']:
                gis_rid = c; break
        if not gis_rid: gis_rid = routes.columns[0]
        
        col_map = {'rid': rid_col, 'bm': bm_col, 'em': em_col, 'gis_rid': gis_rid}
        st.session_state['col_map'] = col_map
        st.session_state['ref_lookup'] = ref_lookup
        
        with st.spinner("Processing..."):
            pts, lns, errs = process_batch(
                df_main, routes, col_map, mode, ref_lookup
            )
            
            st.session_state['success_pts'] = pts
            st.session_state['success_lns'] = lns
            if errs:
                err_df = pd.DataFrame(errs)
                err_df = err_df.sort_values('Error_Message')
                cols = list(err_df.columns)
                cols.insert(0, cols.pop(cols.index('Error_Message')))
                st.session_state['error_df'] = err_df[cols]
                
        st.session_state['processed'] = True

# --- UI SECTION 3: RESULTS ---
if st.session_state['processed']:
    
    if st.session_state['error_df'] is not None and not st.session_state['error_df'].empty:
        st.markdown(f"### ⚠️ Errors Found: {len(st.session_state['error_df'])}")
        st.warning("Below are rows that failed. **Edit them directly in the table below** to fix typos, then click 'Re-Run Fixes'.")
        
        edited_errors = st.data_editor(st.session_state['error_df'], num_rows="dynamic", use_container_width=True, key="editor")
        
        col_fix, col_skip = st.columns([1, 4])
        with col_fix:
            if st.button("🔄 Re-Run Fixes"):
                raw_routes = get_arcgis_features(ROUTE_SERVICE_URL)
                routes = raw_routes.to_crs(CALC_CRS)
                col_map = st.session_state['col_map']
                ref_lookup = st.session_state.get('ref_lookup', None)
                
                with st.spinner("Re-processing fixes..."):
                    new_pts, new_lns, new_errs = process_batch(
                        edited_errors, routes, col_map, mode, ref_lookup
                    )
                    st.session_state['success_pts'].extend(new_pts)
                    st.session_state['success_lns'].extend(new_lns)
                    
                    if new_errs:
                        err_df = pd.DataFrame(new_errs)
                        err_df = err_df.sort_values('Error_Message')
                        cols = list(err_df.columns)
                        if 'Error_Message' in cols: cols.insert(0, cols.pop(cols.index('Error_Message')))
                        st.session_state['error_df'] = err_df[cols]
                        st.error(f"{len(new_errs)} rows still have errors.")
                    else:
                        st.session_state['error_df'] = None
                        st.success("All errors fixed!")
                        st.rerun()

    st.divider()
    st.subheader("3. Results & Download")
    
    n_pts = len(st.session_state['success_pts'])
    n_lns = len(st.session_state['success_lns'])
    n_err = len(st.session_state['error_df']) if st.session_state['error_df'] is not None else 0
    
    m1, m2, m3 = st.columns(3)
    m1.metric("Mapped Points", n_pts)
    m2.metric("Mapped Lines", n_lns)
    m3.metric("Remaining Errors", n_err, delta_color="inverse")
    
    m = folium.Map(location=[39.1, -105.5], zoom_start=7, prefer_canvas=True)
    
    if n_pts > 0:
        valid_pts = [p for p in st.session_state['success_pts'] if p['geometry'] is not None]
        if valid_pts:
            pts_gdf = gpd.GeoDataFrame(valid_pts, crs=CALC_CRS).to_crs(MAP_CRS)
            for col in pts_gdf.columns:
                if pd.api.types.is_datetime64_any_dtype(pts_gdf[col]):
                    pts_gdf[col] = pts_gdf[col].astype(str)

            folium.GeoJson(
                pts_gdf,
                name="Mapped Points",
                point_to_layer=JsCode(f"""
                    function(feature, latlng) {{
                        return L.circleMarker(latlng, {{
                            radius: 5,
                            fillColor: '{feature_color}',
                            color: 'white',
                            weight: 1,
                            opacity: 1,
                            fillOpacity: 0.8
                        }});
                    }}
                """),
                popup=folium.GeoJsonPopup(
                    fields=[c for c in pts_gdf.columns if c != 'geometry'],
                    style="max-width: 400px; max-height: 300px; overflow-y: auto; display: block;"
                )
            ).add_to(m)

    if n_lns > 0:
        valid_lns = [l for l in st.session_state['success_lns'] if l['geometry'] is not None]
        if valid_lns:
            lns_gdf = gpd.GeoDataFrame(valid_lns, crs=CALC_CRS).to_crs(MAP_CRS)
            for col in lns_gdf.columns:
                if pd.api.types.is_datetime64_any_dtype(lns_gdf[col]):
                    lns_gdf[col] = lns_gdf[col].astype(str)
            
            folium.GeoJson(
                lns_gdf,
                name="Mapped Lines",
                style_function=lambda x: {'color': feature_color, 'weight': 3},
                popup=folium.GeoJsonPopup(
                    fields=[c for c in lns_gdf.columns if c != 'geometry'],
                    style="max-width: 400px; max-height: 300px; overflow-y: auto; display: block;"
                )
            ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=1000, height=600)
    
    # --- DOWNLOAD & UPLOAD SECTION ---
    st.divider()
    st.subheader("4. Publish & Export")
    
    col_dl, col_ul = st.columns(2)
    
    with col_dl:
        st.write("##### 💾 Download Local Files")
        dl_fmt = st.radio("Format:", ["Shapefile (ZIP)", "GeoPackage (.gpkg)"], horizontal=True)
        
        # --- DOWNLOAD HANDLER ---
        file_ready = False
        dl_data = None
        dl_name = ""
        dl_mime = ""
        
        if dl_fmt == "Shapefile (ZIP)":
            zip_buffer = io.BytesIO()
            with ZipFile(zip_buffer, 'w') as zipf:
                if n_err > 0:
                    csv_data = st.session_state['error_df'].to_csv(index=False)
                    zipf.writestr("Remaining_Errors.csv", csv_data)
                
                def save_shp(data, suffix):
                    valid_data = [d for d in data if d.get('geometry') is not None]
                    if not valid_data: return
                    tmp_gdf = gpd.GeoDataFrame(valid_data, crs=CALC_CRS).to_crs(MAP_CRS)
                    for col in tmp_gdf.columns:
                        if tmp_gdf[col].dtype == 'object': tmp_gdf[col] = tmp_gdf[col].astype(str)
                    name = f"{out_name}_{suffix}"
                    path = f"/tmp/{name}.shp"
                    tmp_gdf.to_file(path)
                    for f in os.listdir("/tmp"):
                        if f.startswith(name):
                            zipf.write(os.path.join("/tmp", f), f)
                            os.remove(os.path.join("/tmp", f))

                save_shp(st.session_state['success_pts'], "Points")
                save_shp(st.session_state['success_lns'], "Lines")
            
            dl_data = zip_buffer.getvalue()
            dl_name = f"{out_name}.zip"
            dl_mime = "application/zip"
            file_ready = True
            
        elif dl_fmt == "GeoPackage (.gpkg)":
            gpkg_path = f"/tmp/{out_name}.gpkg"
            if os.path.exists(gpkg_path): os.remove(gpkg_path)
            
            has_layers = False
            if n_pts > 0:
                valid_pts = [d for d in st.session_state['success_pts'] if d.get('geometry') is not None]
                if valid_pts:
                    gpd.GeoDataFrame(valid_pts, crs=CALC_CRS).to_crs(MAP_CRS).to_file(gpkg_path, layer="Points", driver="GPKG")
                    has_layers = True
            
            if n_lns > 0:
                valid_lns = [d for d in st.session_state['success_lns'] if d.get('geometry') is not None]
                if valid_lns:
                    mode = 'a' if has_layers else 'w'
                    gpd.GeoDataFrame(valid_lns, crs=CALC_CRS).to_crs(MAP_CRS).to_file(gpkg_path, layer="Lines", driver="GPKG", mode=mode)
                    has_layers = True
            
            if has_layers:
                with open(gpkg_path, "rb") as f:
                    dl_data = f.read()
                dl_name = f"{out_name}.gpkg"
                dl_mime = "application/octet-stream"
                file_ready = True

        if file_ready:
            st.download_button(
                label=f"📦 Download {dl_fmt}",
                data=dl_data,
                file_name=dl_name,
                mime=dl_mime,
                type="primary"
            )

    with col_ul:
        if ARCGIS_AVAILABLE:
            with st.expander("☁️ Upload to ArcGIS / GeoHub", expanded=False):
                # LOGIN UI
                if st.session_state['gis'] is None:
                    st.warning("Please Login to proceed.")
                    
                    def update_portal_url():
                        if st.session_state.portal_type_selector == "ArcGIS Online":
                            st.session_state.portal_url = "https://www.arcgis.com"
                        else:
                            st.session_state.portal_url = "https://maps.codot.gov/portal/"

                    p_type = st.selectbox("Select Portal", ["GeoHub (Enterprise)", "ArcGIS Online"], key="portal_type_selector", on_change=update_portal_url)
                    p_url = st.text_input("Portal URL", key="portal_url")
                    p_user = st.text_input("Username")
                    p_pass = st.text_input("Password", type="password")
                    disable_ssl = st.checkbox("Disable SSL Verification (Fix for some Enterprise Portals)")
                    
                    if st.button("Connect"):
                        try:
                            gis = GIS(p_url, p_user, p_pass, verify_cert=not disable_ssl)
                            st.session_state['gis'] = gis
                            st.session_state['agol_creds'] = (p_url, p_user, p_pass)
                            
                            with st.spinner("Fetching content & folders..."):
                                # Fetch Content
                                query = f"owner:{p_user} AND type:\"Feature Service\""
                                user_content = gis.content.search(query=query, max_items=200)
                                # Store Sorted Tuple List (Title, ID) for cleaner dropdown
                                content_list = [(item.title, item.id) for item in user_content]
                                content_list.sort(key=lambda x: x[0])
                                st.session_state['user_layers'] = content_list
                                
                                # Fetch Folders
                                folder_list = [f['title'] for f in gis.users.me.folders]
                                folder_list.sort()
                                st.session_state['user_folders'] = folder_list

                            st.success(f"Connected as {gis.users.me.username}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Login Failed: {e}")
                
                # UPLOAD UI
                else:
                    gis = st.session_state['gis']
                    st.success(f"Logged in as: **{gis.users.me.username}**")
                    if st.button("Logout"):
                        st.session_state['gis'] = None
                        st.rerun()
                    
                    st.divider()
                    
                    up_mode = st.radio("Upload Mode", ["New Layer", "Overwrite Existing"], horizontal=True)
                    
                    target_item_id = None
                    up_name = ""
                    item_props = {}
                    ready_to_upload = False
                    selected_folder_name = None
                    
                    if up_mode == "New Layer":
                        # NAME & METADATA
                        if (n_pts > 0 and n_lns > 0):
                            st.warning("⚠️ You are uploading both Points and Lines. You must provide unique names for each.")
                            up_name_pts = st.text_input("Layer Name for POINTS", f"{out_name}_Points")
                            up_name_lns = st.text_input("Layer Name for LINES", f"{out_name}_Lines")
                        else:
                            up_name = st.text_input("New Layer Name", f"{out_name}")
                        
                        up_tags = st.text_input("Tags (comma separated)", "CDOT, LRS")
                        up_summary = st.text_area("Summary (Abstract)", "Generated by CDOT LRS Mapper")
                        
                        # FOLDER SELECTION
                        f_options = ["[Root Folder]"] + st.session_state['user_folders'] + ["➕ Create New Folder"]
                        folder_choice = st.selectbox("Destination Folder", f_options)
                        
                        if folder_choice == "➕ Create New Folder":
                            new_f_name = st.text_input("Enter New Folder Name")
                            if new_f_name: selected_folder_name = new_f_name
                        elif folder_choice != "[Root Folder]":
                            selected_folder_name = folder_choice
                        else:
                            selected_folder_name = None # Root
                        
                        item_props = {'tags': up_tags, 'snippet': up_summary}
                        ready_to_upload = True
                        
                    else:
                        # OVERWRITE MODE
                        st.markdown('<div class="warning-box">⚠️ <b>DANGER ZONE:</b> Overwriting replaces the entire dataset. This can break maps, dashboards, and apps that rely on specific field names or data.</div>', unsafe_allow_html=True)
                        
                        if st.session_state['user_layers']:
                            # CLEANER DROPDOWN: Shows "Title" only, maps back to ID
                            layer_options = [x[0] for x in st.session_state['user_layers']]
                            selected_title = st.selectbox("Select Layer to Overwrite", layer_options)
                            
                            # Find ID for selected title
                            target_item_id = next((x[1] for x in st.session_state['user_layers'] if x[0] == selected_title), None)
                            up_name = selected_title # Just for labelling
                            
                            # Pre-check schema
                            if n_lns > 0 or n_pts > 0:
                                sample_data = st.session_state['success_lns'] if n_lns > 0 else st.session_state['success_pts']
                                zip_path_check, new_cols = prep_geopackage_zip(sample_data, "schema_check")
                                
                                status, missing = check_schema_match(gis, target_item_id, new_cols)
                                
                                if status == "MISMATCH":
                                    st.error(f"❌ **SCHEMA MISMATCH DETECTED**")
                                    st.write(f"The new dataset is missing these fields found in the existing layer: **{', '.join(missing)}**")
                                    st.write("Overwriting now will delete these fields and likely break downstream applications.")
                                else:
                                    st.success("✅ New schema appears compatible (all existing fields present).")
                            
                            ack = st.checkbox("⚠️ I acknowledge that overwriting is permanent and may break dependent apps.")
                            if ack: ready_to_upload = True
                        else:
                            st.warning("No Feature Layers found in your content.")

                    if st.button("Upload & Publish", disabled=not ready_to_upload):
                        with st.spinner("Processing Upload..."):
                            
                            if up_mode == "New Layer" and (n_pts > 0 and n_lns > 0):
                                # Dual Upload
                                zip_path, _ = prep_geopackage_zip(st.session_state['success_pts'], up_name_pts)
                                if zip_path:
                                    status, msg = handle_arcgis_upload(gis, zip_path, up_name_pts, selected_folder_name, item_props, None)
                                    if status in ["PUBLISHED", "OVERWRITTEN"]: st.success(f"Points {status}: [View Item]({msg})")
                                    else: st.error(f"Points Error: {msg}")
                                    
                                zip_path, _ = prep_geopackage_zip(st.session_state['success_lns'], up_name_lns)
                                if zip_path:
                                    status, msg = handle_arcgis_upload(gis, zip_path, up_name_lns, selected_folder_name, item_props, None)
                                    if status in ["PUBLISHED", "OVERWRITTEN"]: st.success(f"Lines {status}: [View Item]({msg})")
                                    else: st.error(f"Lines Error: {msg}")
                            
                            else:
                                # Standard Single Upload
                                if n_pts > 0:
                                    zip_path, _ = prep_geopackage_zip(st.session_state['success_pts'], up_name)
                                    if zip_path:
                                        status, msg = handle_arcgis_upload(gis, zip_path, up_name, selected_folder_name, item_props, target_item_id)
                                        if status in ["PUBLISHED", "OVERWRITTEN"]: st.success(f"Points {status}: [View Item]({msg})")
                                        elif status == "EXISTS": st.warning(f"Layer '{up_name}' already exists.")
                                        else: st.error(f"Points Error: {msg}")
                                
                                if n_lns > 0:
                                    zip_path, _ = prep_geopackage_zip(st.session_state['success_lns'], up_name)
                                    if zip_path:
                                        status, msg = handle_arcgis_upload(gis, zip_path, up_name, selected_folder_name, item_props, target_item_id)
                                        if status in ["PUBLISHED", "OVERWRITTEN"]: st.success(f"Lines {status}: [View Item]({msg})")
                                        elif status == "EXISTS": st.warning(f"Layer '{up_name}' already exists.")
                                        else: st.error(f"Lines Error: {msg}")
        else:
            st.info("The 'arcgis' library is missing. Upload features are disabled.")

