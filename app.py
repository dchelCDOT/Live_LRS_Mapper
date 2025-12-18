import streamlit as st
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString
from shapely.ops import substring
import folium
from streamlit_folium import st_folium
import requests
import io
import os
from zipfile import ZipFile

# --- PAGE CONFIG ---
st.set_page_config(page_title="LRS Mapper Pro", page_icon="🛣️", layout="wide")

st.title("🛣️ LRS Mapper Pro")
st.markdown("""
**Instructions:**
1. Upload your CSV spreadsheet.
2. Map your columns.
3. Click **Run Analysis** to see the map and download results.
""")

# --- CONSTANTS ---
ROUTE_SERVICE_URL = "https://services.arcgis.com/yzB9WM8W0BO3Ql7d/arcgis/rest/services/Routes_gdb/FeatureServer/0"
CALC_CRS = "EPSG:3857" # Web Mercator (Meters)
MAP_CRS = "EPSG:4326"  # WGS84 (Lat/Long)

# --- UTILS ---
@st.cache_data
def get_layer_columns(service_url):
    params = {'where': '1=1', 'outFields': '*', 'f': 'json', 'resultRecordCount': 1}
    try:
        r = requests.get(f"{service_url}/query", params=params)
        data = r.json()
        if 'fields' in data: return [f['name'] for f in data['fields']]
        return []
    except: return []

@st.cache_data
def get_arcgis_features(service_url):
    all_features = []
    offset = 0
    with st.spinner("Fetching Route Network from ArcGIS..."):
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

# --- APP LOGIC ---

uploaded_file = st.file_uploader("Upload Spreadsheet (.csv)", type="csv")

if uploaded_file is not None:
    df = pd.read_csv(uploaded_file)
    csv_cols = list(df.columns)
    
    st.divider()
    st.subheader("⚙️ Configuration")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.info("Spreadsheet Mapping")
        csv_rid = st.selectbox("Spreadsheet Route ID", csv_cols, index=0 if csv_cols else None)
        bm_col = st.selectbox("Begin Milepost Column", csv_cols, index=1 if len(csv_cols)>1 else 0)
        em_col_opts = ['(None)'] + csv_cols
        em_col = st.selectbox("End Milepost Column", em_col_opts, index=0)
        
    with col2:
        st.warning("Settings")
        mode = st.radio("Feature Type", ["Point", "Line", "Both"], index=2)
        out_name = st.text_input("Output Filename", "LRS_Results")
        # HIDDEN: Units hardcoded to Miles (1609.34)
        # HIDDEN: GIS Route Column Auto-detected
    
    if st.button("🚀 Run Analysis", type="primary"):
        # 1. Load Routes
        raw_routes = get_arcgis_features(ROUTE_SERVICE_URL)
        if raw_routes is None:
            st.error("Failed to load routes from ArcGIS.")
            st.stop()
            
        routes = raw_routes.to_crs(CALC_CRS)
        
        # 2. Auto-Detect GIS Route Column
        gis_rid = None
        possible_names = ['ROUTE', 'Route', 'route', 'RouteID', 'Route_ID', 'RteID']
        for name in possible_names:
            if name in routes.columns:
                gis_rid = name
                break
        
        # Fallback if specific names not found, try case-insensitive
        if not gis_rid:
            for c in routes.columns:
                if c.upper() == 'ROUTE':
                    gis_rid = c
                    break
                    
        # Final fallback
        if not gis_rid:
            gis_rid = routes.columns[0]
        
        # 3. Process
        routes[gis_rid] = routes[gis_rid].astype(str)
        df[csv_rid] = df[csv_rid].astype(str)
        
        valid_pts, valid_lns, errors = [], [], []
        unit_factor = 1609.34 # Hardcoded Miles
        
        progress_bar = st.progress(0)
        total_rows = len(df)
        
        for idx, row in df.iterrows():
            if idx % max(1, int(total_rows/10)) == 0:
                progress_bar.progress(idx / total_rows)

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
            
            # Logic Type
            is_point = False
            if mode.lower() == 'point': is_point = True
            elif mode.lower() == 'line': is_point = False
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
