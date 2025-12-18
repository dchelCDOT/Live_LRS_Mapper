import streamlit as st
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString
from shapely.ops import substring
import folium
from folium import JsCode
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium
import requests
import io
import os
from zipfile import ZipFile

# --- PAGE CONFIG ---
st.set_page_config(
    page_title="CDOT LRS Mapper Pro",
    page_icon="🛣️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# --- BRANDING CONSTANTS ---
CDOT_BLUE = "#004899"
CDOT_GREEN = "#78BE20"
CDOT_LOGO_URL = "https://www.codot.gov/assets/sitelogo.png"

# --- CUSTOM CSS ---
st.markdown(f"""
    <style>
        /* Headers */
        h1, h2, h3 {{ color: {CDOT_BLUE} !important; }}
        
        /* Custom Banner */
        .cdot-banner {{
            background-color: white;
            padding: 20px;
            border-bottom: 5px solid {CDOT_GREEN};
            border-radius: 10px;
            display: flex;
            align-items: center;
            margin-bottom: 20px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .cdot-banner img {{ height: 60px; margin-right: 25px; }}
        .cdot-banner h1 {{
            color: {CDOT_BLUE} !important;
            margin: 0; padding: 0;
            font-size: 2.5rem; font-weight: 800;
        }}
        
        /* Buttons */
        div.stButton > button:first-child {{
            background-color: {CDOT_GREEN}; color: white;
            border: none; font-weight: bold; font-size: 1.1rem;
        }}
        div.stButton > button:first-child:hover {{
            background-color: {CDOT_BLUE}; border: 2px solid {CDOT_GREEN};
        }}
    </style>
""", unsafe_allow_html=True)

# --- HEADER ---
st.markdown(f"""
    <div class="cdot-banner">
        <img src="{CDOT_LOGO_URL}" alt="CDOT Logo">
        <h1>LRS Mapper Pro</h1>
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
        * **Live Error Fixing:** Edit bad data directly in the app and re-map it.
        * **High Performance:** optimized for speed.
        * **Export:** Generates Shapefiles and Error Reports.
        """)
    with col_how:
        st.subheader("How to use it")
        st.markdown("""
        1.  **Upload** your CSV or Excel file.
        2.  **Map Columns** (Route ID, Begin MP, End MP).
        3.  **Run Analysis**.
        4.  **Fix Errors** (if any) in the table that appears.
        5.  **Download** final results.
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

# --- GIS UTILS ---
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

def process_batch(df_batch, routes, col_map, mode):
    # Unpack columns
    rid_col = col_map['rid']
    bm_col = col_map['bm']
    em_col = col_map['em']
    gis_rid = col_map['gis_rid']
    
    # Ensure string matching
    routes[gis_rid] = routes[gis_rid].astype(str)
    df_batch[rid_col] = df_batch[rid_col].astype(str)
    
    v_pts, v_lns, errs = [], [], []
    unit_factor = 1609.34 # Miles to Meters
    
    for idx, row in df_batch.iterrows():
        try:
            # 1. Match Route
            rid = row[rid_col]
            match = routes[routes[gis_rid] == rid]
            
            if match.empty:
                raise ValueError(f"Route ID '{rid}' Not Found")
            
            geom_meters = match.iloc[0].geometry
            
            # 2. Parse Begin Measure
            try: bm_val = float(row[bm_col])
            except: raise ValueError(f"Invalid Begin Measure: {row[bm_col]}")
            
            bm_meters = bm_val * unit_factor
            
            # 3. Determine Geometry Type
            is_point = False
            if mode == 'Point': is_point = True
            elif mode == 'Line': is_point = False
            else: # Both
                if em_col == '(None)' or pd.isna(row.get(em_col)): is_point = True
            
            # 4. Generate Geometry
            if is_point:
                pt_geom = geom_meters.interpolate(bm_meters)
                res = row.copy()
                res['geometry'] = pt_geom
                if 'Error_Message' in res: del res['Error_Message']
                v_pts.append(res)
            else:
                try: em_val = float(row[em_col])
                except: raise ValueError(f"Invalid End Measure: {row.get(em_col)}")
                
                em_meters = em_val * unit_factor
                
                if bm_meters >= em_meters:
                    if bm_meters == em_meters: raise ValueError("Begin MP == End MP (Use Point mode)")
                    else: raise ValueError(f"End MP ({em_val}) < Begin MP ({bm_val})")
                
                ln_geom = substring(geom_meters, bm_meters, em_meters)
                
                if ln_geom.is_empty: raise ValueError("Result empty")
                elif ln_geom.geom_type in ['Point', 'MultiPoint']: raise ValueError("Collapsed to Point")
                else:
                    res = row.copy()
                    res['geometry'] = ln_geom
                    if 'Error_Message' in res: del res['Error_Message']
                    v_lns.append(res)

        except Exception as e:
            row['Error_Message'] = str(e)
            errs.append(row)
            
    return v_pts, v_lns, errs

# --- UI SECTION 1: UPLOAD ---
st.subheader("1. Data Upload")
st.info(f"**Need help formatting?** Use our [Highway Route & Reference Mapping Toolbox]({'https://script.google.com/a/macros/state.co.us/s/AKfycbxNe4UVfAAngo5W0M_fTrduePeip-yW4zbGRm6NIjNY-87rQy3D86jshHVBUXpPxb5p7A/exec'}).")

uploaded_file = st.file_uploader("Upload .csv or .xlsx", type=["csv", "xlsx"])

if uploaded_file:
    # Load Data
    try:
        if uploaded_file.name.endswith('.csv'):
            df_main = pd.read_csv(uploaded_file)
            default_out_name = os.path.splitext(uploaded_file.name)[0]
        else:
            xls = pd.ExcelFile(uploaded_file)
            if len(xls.sheet_names) > 1:
                sheet = st.selectbox("Select Sheet", xls.sheet_names)
                df_main = pd.read_excel(uploaded_file, sheet_name=sheet)
                default_out_name = sheet
            else:
                df_main = pd.read_excel(uploaded_file, sheet_name=xls.sheet_names[0])
                default_out_name = os.path.splitext(uploaded_file.name)[0]
    except Exception as e:
        st.error(f"Error reading file: {e}")
        st.stop()
        
    cols = list(df_main.columns)
    
    # --- UI SECTION 2: CONFIG ---
    st.divider()
    st.subheader("2. Configuration")
    
    c1, c2, c3 = st.columns(3)
    with c1:
        rid_col = st.selectbox("Route ID Column", cols, index=0)
        bm_col = st.selectbox("Begin MP Column", cols, index=1 if len(cols)>1 else 0)
        em_opts = ['(None)'] + cols
        em_col = st.selectbox("End MP Column (Optional)", em_opts, index=0)
    with c2:
        mode = st.radio("Feature Type", ["Point", "Line", "Both"], index=2)
        out_name = st.text_input("Output Filename", default_out_name)
    with c3:
        st.write("<b>Map Visualization</b>", unsafe_allow_html=True)
        # --- COLOR PICKER ---
        feature_color = st.color_picker("Feature Color", CDOT_GREEN)
        
        # --- IGNORE ERROR TOGGLE ---
        st.write("")
        ignore_errors = st.checkbox("Ignore rows with mapping errors to allow valid rows to map", value=True)

    # --- EMPTY ROW CLEANING ---
    before_len = len(df_main)
    df_main = df_main.dropna(subset=[rid_col, bm_col], how='any')
    if len(df_main) > 0:
        mask = df_main[rid_col].astype(str).str.strip() == ''
        df_main = df_main[~mask]
    
    # --- ACTION BUTTONS ---
    st.divider()
    
    # Run Button
    if st.button("🚀 Run Analysis", type="primary"):
        st.session_state['success_pts'] = []
        st.session_state['success_lns'] = []
        st.session_state['error_df'] = None
        
        # Load GIS
        raw_routes = get_arcgis_features(ROUTE_SERVICE_URL)
        if raw_routes is None: st.stop()
        routes = raw_routes.to_crs(CALC_CRS)
        
        # Detect GIS Column
        gis_rid = None
        for c in routes.columns:
            if c.upper() in ['ROUTE', 'ROUTEID', 'RTEID', 'ROUTE_ID']:
                gis_rid = c; break
        if not gis_rid: gis_rid = routes.columns[0]
        
        col_map = {'rid': rid_col, 'bm': bm_col, 'em': em_col, 'gis_rid': gis_rid}
        st.session_state['col_map'] = col_map
        
        with st.spinner("Processing..."):
            pts, lns, errs = process_batch(df_main, routes, col_map, mode)
            
            st.session_state['success_pts'] = pts
            st.session_state['success_lns'] = lns
            if errs:
                err_df = pd.DataFrame(errs)
                cols = list(err_df.columns)
                cols.insert(0, cols.pop(cols.index('Error_Message')))
                st.session_state['error_df'] = err_df[cols]
                
        st.session_state['processed'] = True

# --- UI SECTION 3: RESULTS & FIXING ---
if st.session_state['processed']:
    
    # 1. LIVE ERROR FIXING
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
                
                with st.spinner("Re-processing fixes..."):
                    new_pts, new_lns, new_errs = process_batch(edited_errors, routes, col_map, mode)
                    st.session_state['success_pts'].extend(new_pts)
                    st.session_state['success_lns'].extend(new_lns)
                    
                    if new_errs:
                        err_df = pd.DataFrame(new_errs)
                        cols = list(err_df.columns)
                        if 'Error_Message' in cols: cols.insert(0, cols.pop(cols.index('Error_Message')))
                        st.session_state['error_df'] = err_df[cols]
                        st.error(f"{len(new_errs)} rows still have errors.")
                    else:
                        st.session_state['error_df'] = None
                        st.success("All errors fixed!")
                        st.rerun()

    # 2. MAP & DOWNLOAD
    st.divider()
    st.subheader("3. Results & Download")
    
    n_pts = len(st.session_state['success_pts'])
    n_lns = len(st.session_state['success_lns'])
    n_err = len(st.session_state['error_df']) if st.session_state['error_df'] is not None else 0
    
    m1, m2, m3 = st.columns(3)
    m1.metric("Mapped Points", n_pts)
    m2.metric("Mapped Lines", n_lns)
    m3.metric("Remaining Errors", n_err, delta_color="inverse")
    
    # --- PERFORMANCE MAP ---
    m = folium.Map(location=[39.1, -105.5], zoom_start=7, prefer_canvas=True)
    
    # Helper for Points (Optimized GeoJSON)
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
                # --- UPDATED: SCROLLABLE POPUP ---
                popup=folium.GeoJsonPopup(
                    fields=[c for c in pts_gdf.columns if c != 'geometry'],
                    style="max-width: 400px; max-height: 300px; overflow-y: auto; display: block;"
                )
            ).add_to(m)

    # Helper for Lines (Standard)
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
                # --- UPDATED: SCROLLABLE POPUP ---
                popup=folium.GeoJsonPopup(
                    fields=[c for c in lns_gdf.columns if c != 'geometry'],
                    style="max-width: 400px; max-height: 300px; overflow-y: auto; display: block;"
                )
            ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, width=1000, height=600)
    
    # DOWNLOAD
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
        
    st.download_button(
        label=f"📦 Download ZIP ({out_name}.zip)",
        data=zip_buffer.getvalue(),
        file_name=f"{out_name}.zip",
        mime="application/zip",
        type="primary"
    )
