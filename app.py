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
from zipfile import ZipFile

# --- PAGE CONFIG ---
st.set_page_config(
    page_title="CDOT Route and Reference Mapper",
    page_icon="🛣️",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# --- BRANDING CONSTANTS ---
CDOT_BLUE = "#004899"
CDOT_GREEN = "#245436"
CDOT_LOGO_URL = "https://www.codot.gov/assets/sitelogo.png"
# Official CDOT Route Reference Table
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
        * **Multi-Segment Support:** Handles routes split into multiple GIS features.
        * **Smart Validation:** Checks MP limits against the official CDOT Reference table.
        * **Robust Mapping:** Automatically handles complex geometries.
        """)
    with col_how:
        st.subheader("How to use it")
        st.markdown("""
        1.  **Upload** your CSV or Excel file.
        2.  **Map Columns** (Route ID, Begin MP, End MP).
        3.  **Run Analysis**.
        4.  **Fix Errors** (if any) in the table that appears.
        5.  **Download** Shapefiles and Error Reports.
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
    """Loads Google Sheet for Route Validation."""
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

def process_batch(df_batch, routes, col_map, mode, ref_lookup=None):
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
            rid = str(row[rid_col]).strip()
            
            # --- 1. REFERENCE VALIDATION ---
            if ref_lookup and rid in ref_lookup:
                limits = ref_lookup[rid]
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
                        raise ValueError(f"End MP ({em_val}) exceeds Route {rid} Maximum ({limits['max']})")
            
            # --- 2. GIS PROCESSING (MULTI-SEGMENT SEARCH) ---
            # Match ALL segments for this Route ID
            matches = routes[routes[gis_rid] == rid]
            
            if matches.empty:
                raise ValueError(f"Route ID '{rid}' Not Found in GIS Network")
            
            # Parse User Inputs
            try: bm_val = float(row[bm_col])
            except: raise ValueError(f"Invalid Begin Measure: {row[bm_col]}")
            bm_meters = bm_val * unit_factor
            
            # Determine Mode
            is_point = False
            if mode == 'Point': is_point = True
            elif mode == 'Line': is_point = False
            else:
                if em_col == '(None)' or pd.isna(row.get(em_col)): is_point = True
            
            final_geom = None
            
            # --- ITERATE THROUGH ALL GIS SEGMENTS ---
            # Many routes are split into multiple rows in the database.
            # We check each one to see if it can produce valid geometry.
            
            if is_point:
                # Point Mode: Find the segment containing the MP
                for _, feat in matches.iterrows():
                    geom = feat.geometry
                    # Interpolate clamps points to the ends.
                    # We check if the interpolated point is physically ON the line (not clamped distance)
                    # But simpler approach: Use 'project' if available or just check valid range if LRS attributes exist.
                    # Fallback: Just try interpolate.
                    
                    candidate_pt = geom.interpolate(bm_meters)
                    
                    # Check if "clamped" (i.e. if point is at exact start/end, checks if MP is within length)
                    feat_len = geom.length
                    # If bm_meters is > length, interpolate returns the end point.
                    # This implies this wasn't the right segment (unless it's the very end).
                    
                    # Heuristic: We accept the first one that doesn't look purely clamped 
                    # OR we just take the last one if all fail.
                    # Better: If your GIS data has Min/Max measures, use them. 
                    # Since we don't know the schema, we rely on geometry length.
                    
                    if bm_meters <= feat_len:
                        final_geom = candidate_pt
                        break # Found it!
                
                if final_geom is None:
                    # If we looped all and didn't find one where MP <= Length,
                    # It likely means the MP is beyond the end of the entire route system.
                    # We default to the LAST matched segment's end point (clamped).
                    final_geom = matches.iloc[-1].geometry.interpolate(bm_meters)

            else:
                # Line Mode: Find segment containing the range
                try: em_val = float(row[em_col])
                except: raise ValueError(f"Invalid End Measure: {row.get(em_col)}")
                em_meters = em_val * unit_factor
                
                if bm_meters >= em_meters:
                     if bm_meters == em_meters: raise ValueError("Begin MP == End MP (Use Point mode)")
                     else: raise ValueError(f"End MP ({em_val}) < Begin MP ({bm_val})")

                # Loop through all segments to find one that creates a valid line
                for _, feat in matches.iterrows():
                    geom = feat.geometry
                    
                    # Attempt Substring
                    # Note: substring works even if coords are clamped, we must check the result
                    candidate_ln = substring(geom, bm_meters, em_meters)
                    
                    # Valid if it is a LineString and has length > 0 (tolerance 1m)
                    if not candidate_ln.is_empty and candidate_ln.length > 1.0:
                        final_geom = candidate_ln
                        break # Found a valid segment!
                
                # If still no valid geometry, use "Aggressive Fallback" on the LONGEST matching segment
                # (This assumes the user is right and the GIS is short/gapped)
                if final_geom is None:
                    # Pick the longest GIS segment to project onto
                    longest_feat = matches.loc[matches.geometry.length.idxmax()]
                    geom = longest_feat.geometry
                    
                    p_start = geom.interpolate(bm_meters)
                    p_end = geom.interpolate(em_meters)
                    
                    dist = p_start.distance(p_end)
                    if dist > 1.0:
                        final_geom = LineString([p_start, p_end])
                    else:
                        # Total Failure
                        raise ValueError(f"Geometry collapsed. Input range ({bm_val}-{em_val}) likely falls within a gap or outside all {len(matches)} known GIS segments.")
            
            # --- FINALIZE ---
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
st.info(f"**Need help formatting?** Use our [Highway Route & Reference Mapping Toolbox]({'https://script.google.com/a/macros/state.co.us/s/AKfycbxNe4UVfAAngo5W0M_fTrduePeip-yW4zbGRm6NIjNY-87rQy3D86jshHVBUXpPxb5p7A/exec'}).")

uploaded_file = st.file_uploader("Upload Data (.csv or .xlsx)", type=["csv", "xlsx"])

# --- LOAD DATA ---
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

    # --- EMPTY ROW CLEANING ---
    before_len = len(df_main)
    df_main = df_main.dropna(subset=[rid_col, bm_col], how='any')
    if len(df_main) > 0:
        mask = df_main[rid_col].astype(str).str.strip() == ''
        df_main = df_main[~mask]
    
    # --- ACTION BUTTONS ---
    st.divider()
    
    if st.button("🚀 Run Analysis", type="primary"):
        st.session_state['success_pts'] = []
        st.session_state['success_lns'] = []
        st.session_state['error_df'] = None
        
        # Load GIS
        raw_routes = get_arcgis_features(ROUTE_SERVICE_URL)
        if raw_routes is None: st.stop()
        routes = raw_routes.to_crs(CALC_CRS)
        
        # Load Reference Data
        with st.spinner("Loading Official Route Limits..."):
            ref_lookup = get_reference_data(REF_SHEET_URL)
        
        # Detect GIS Column
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
                # Auto-sort errors
                err_df = err_df.sort_values('Error_Message')
                cols = list(err_df.columns)
                cols.insert(0, cols.pop(cols.index('Error_Message')))
                st.session_state['error_df'] = err_df[cols]
                
        st.session_state['processed'] = True

# --- UI SECTION 3: RESULTS & FIXING ---
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
